"""Regression tests for v0.3.6 — the unique-user KPI.

Every scan_complete event must carry a stable-per-user, opt-in-raw,
SHA-256-truncated user_hash so `dcount(user_hash)` answers the
'how many people use this' question directly.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest

from pq_adbc_advisor import state, telemetry
from pq_adbc_advisor.mcode import ConnectorCall
from pq_adbc_advisor.report import ImpactedArtifact, ImpactReport


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_LAKEHOUSE_PATH", str(tmp_path / "lakehouse.json"))
    monkeypatch.setattr(state, "_LAKEHOUSE_OPT_OUT", str(tmp_path / "opt_out.json"))
    monkeypatch.setattr(state, "_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(state, "_home_fallback_path",
                        lambda: str(tmp_path / "home" / "state.json"))
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=deadbeef-0006-0006-0006-000600060006;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    monkeypatch.delenv("PQ_ADBC_ADVISOR_USER_ID", raising=False)
    monkeypatch.delenv("PQ_ADBC_ADVISOR_USER_RAW", raising=False)
    monkeypatch.delenv("PQ_ADBC_ADVISOR_TENANT_RAW", raising=False)
    if hasattr(telemetry._maybe_print_first_run_notice, "_printed"):
        delattr(telemetry._maybe_print_first_run_notice, "_printed")


def _mk_report() -> ImpactReport:
    r = ImpactReport(workspace_id="ws-u", scope="workspace")
    r.observed_types = {"SemanticModel": 1}
    r.add(ImpactedArtifact(
        workspace_id="ws-u", item_id="sm-1", item_name="sm-1",
        item_type="SemanticModel", has_gateway=False,
        hits=[ConnectorCall("Snowflake", "Snowflake.Databases",
                            "odbc_to_adbc:snowflake", "1.0", "e", "src", False)],
    ))
    return r


def _capture():
    events: list[dict] = []
    class R:
        status_code = 200
        text = "{}"
        def json(self): return {"itemsAccepted": 1}
    def post(url, data=None, **kw):
        events.append(json.loads(data))
        return R()
    return events, post


def _props(event: dict) -> dict:
    return event["data"]["baseData"]["properties"]


# --------------------------------------------------------------------------- #
# _user_id resolution
# --------------------------------------------------------------------------- #

def test_user_id_returns_userId_and_source_aad_when_notebookutils_present():
    """When the Fabric runtime exposes userId, we tag source='aad'."""
    fake_ctx = {"userId": "aad-guid-0001", "userName": "alice@contoso.com"}
    class Ctx(dict):
        def get(self, k, default=""):  # noqa: A003 — matches ctx API
            return super().get(k, default)
    ctx_obj = Ctx(fake_ctx)
    fake_mod = type("M", (), {"runtime": type("R", (), {"context": ctx_obj})()})
    with patch.dict("sys.modules", {"notebookutils": fake_mod}):
        assert telemetry._user_id() == ("aad-guid-0001", "aad")


def test_user_id_refuses_upn_fallback_when_userId_missing():
    """Bug 1 (HIGH privacy): NEVER hash userName/UPN — it's enumerable.
    When userId is absent, return empty even if userName is available."""
    class Ctx(dict):
        def get(self, k, default=""):
            return super().get(k, default)
    ctx_obj = Ctx({"userName": "alice@contoso.com", "ExecutingUser": "alice"})
    fake_mod = type("M", (), {"runtime": type("R", (), {"context": ctx_obj})()})
    with patch.dict("sys.modules", {"notebookutils": fake_mod}):
        result = telemetry._user_id()
        assert result[0] == "", (
            "must NOT hash a UPN — that's deanonymizable via rainbow "
            f"table against the tenant directory; got {result}"
        )


def test_user_id_env_var_used_only_when_notebookutils_absent(monkeypatch):
    """Bug 5 (MEDIUM): env var only wins in non-Fabric environments so
    a hostile lakehouse notebook can't shadow the real AAD user."""
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "ci-runner-42")
    # No notebookutils importable → env var applies, source='env'.
    assert telemetry._user_id() == ("ci-runner-42", "env")


def test_user_id_env_var_ignored_when_notebookutils_importable():
    """Bug 5 (MEDIUM): if notebookutils is importable but ctx has no
    userId, env var must NOT step in — that would let a Fabric lakehouse
    notebook overwrite the real user id."""
    class Ctx(dict):
        def get(self, k, default=""):
            return super().get(k, default)
    ctx_obj = Ctx({"userName": "alice@contoso.com"})  # no userId
    fake_mod = type("M", (), {"runtime": type("R", (), {"context": ctx_obj})()})
    import os as _os
    with patch.dict("sys.modules", {"notebookutils": fake_mod}), \
         patch.dict(_os.environ, {"PQ_ADBC_ADVISOR_USER_ID": "shadow-attempt"}):
        result = telemetry._user_id()
        assert result == ("", ""), (
            f"env var must NOT shadow when notebookutils is present; got {result}"
        )


def test_user_id_empty_when_nothing_available():
    assert telemetry._user_id() == ("", "")


def test_user_id_survives_per_key_ctx_exception():
    """Bug 3 (MEDIUM): if reading one ctx key raises, the loop should
    continue to the next key instead of collapsing to env var."""
    class Ctx:
        _count = 0
        def get(self, k, default=""):
            # First key raises; subsequent keys return the real value.
            Ctx._count += 1
            if Ctx._count == 1:
                raise RuntimeError("simulated ctx hiccup")
            if k == "UserId":
                return "aad-recovered-0002"
            return default
    fake_mod = type("M", (), {"runtime": type("R", (), {"context": Ctx()})()})
    with patch.dict("sys.modules", {"notebookutils": fake_mod}):
        result = telemetry._user_id()
    assert result == ("aad-recovered-0002", "aad"), (
        f"per-key exception must not collapse the chain; got {result}"
    )


def test_user_id_strips_whitespace():
    """Bug 4 (MEDIUM): trailing whitespace / newlines must not cause
    two hashes for the same person."""
    class Ctx(dict):
        def get(self, k, default=""):
            return super().get(k, default)
    ctx_obj = Ctx({"userId": "  aad-guid-0003\n"})
    fake_mod = type("M", (), {"runtime": type("R", (), {"context": ctx_obj})()})
    with patch.dict("sys.modules", {"notebookutils": fake_mod}):
        assert telemetry._user_id() == ("aad-guid-0003", "aad")


# --------------------------------------------------------------------------- #
# _user_hash correctness + determinism
# --------------------------------------------------------------------------- #

def test_user_hash_is_sha256_first_12_chars_after_strip():
    """Bug 4 (MEDIUM): the hasher itself must also strip, so external
    callers who bypass _user_id() don't produce inconsistent hashes."""
    uid = "  aad-guid-x  "
    expected = hashlib.sha256(b"aad-guid-x").hexdigest()[:12]
    assert telemetry._user_hash(uid) == expected


def test_user_hash_empty_input_returns_empty():
    assert telemetry._user_hash("") == ""


def test_user_hash_is_deterministic_across_runs():
    a = telemetry._user_hash("user-x")
    b = telemetry._user_hash("user-x")
    assert a == b and len(a) == 12


def test_user_hash_differs_for_different_users():
    a = telemetry._user_hash("alice-guid")
    b = telemetry._user_hash("bob-guid")
    assert a != b


# --------------------------------------------------------------------------- #
# Wire-level: user_hash on scan_complete
# --------------------------------------------------------------------------- #

def test_scan_complete_carries_user_hash_on_the_wire(monkeypatch):
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "alice-uuid")
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True, duration_seconds=1.0)
    p = _props(events[0])
    assert "user_hash" in p, "user_hash must land on every scan_complete"
    expected = hashlib.sha256(b"alice-uuid").hexdigest()[:12]
    assert p["user_hash"] == expected
    # Bug 5 provenance tag lands too so KQL can filter to trusted rows.
    assert p["user_hash_source"] == "env", (
        f"expected env-source when using env-var override; got {p.get('user_hash_source')!r}"
    )


def test_scan_complete_user_hash_empty_when_unresolved():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)
    p = _props(events[0])
    assert p["user_hash"] == "", (
        "no resolvable user identity → empty user_hash (still a valid row, "
        "just doesn't count toward the dcount)"
    )
    assert p["user_hash_source"] == ""


def test_scan_complete_does_not_send_raw_user_id_by_default(monkeypatch):
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "alice-uuid")
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)
    p = _props(events[0])
    # 'user_id' MUST NOT appear unless the raw-flag env var is set.
    assert "user_id" not in p
    # And the raw value MUST NOT leak into any other field.
    for k, v in p.items():
        assert "alice-uuid" not in str(v), (
            f"raw user id leaked into field {k}={v!r}"
        )


def test_bug2_raw_flag_omits_hash_to_prevent_correlation(monkeypatch):
    """Bug 2 (HIGH privacy): when the opt-in raw flag is on, the event
    carries ONLY the raw user_id and OMITS user_hash. Otherwise anyone
    with historical App Insights read could build a {hash → raw} lookup
    and retroactively deanonymize every prior scan_complete."""
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "alice-uuid")
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_RAW", "1")
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)
    p = _props(events[0])
    assert p.get("user_id") == "alice-uuid"
    assert "user_hash" not in p, (
        "when raw flag is on, user_hash MUST be omitted so the raw+hash "
        "pair can't be used to build a lookup table against historical rows"
    )


# --------------------------------------------------------------------------- #
# Unique-user KQL semantics: repeated scans by SAME user → 1 unique;
# different users → distinct hashes.
# --------------------------------------------------------------------------- #

def test_two_scans_same_user_produce_identical_user_hash(monkeypatch):
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "alice-uuid")
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)
        telemetry.emit_scan_summary(_mk_report(), enabled=True)
    p1, p2 = _props(events[0]), _props(events[1])
    assert p1["user_hash"] == p2["user_hash"]
    # But run_id differs — that's the "scans" count.
    assert p1["run_id"] != p2["run_id"]


def test_two_users_produce_different_hashes(monkeypatch):
    events, fake_post = _capture()

    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "alice-uuid")
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)

    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "bob-uuid")
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)

    hashes = {_props(e)["user_hash"] for e in events}
    assert len(hashes) == 2, (
        f"two distinct user IDs must yield two distinct hashes; got {hashes}"
    )


def test_same_user_with_trailing_whitespace_produces_same_hash(monkeypatch):
    """Bug 4 (MEDIUM): normalization end-to-end — a user id with/without
    trailing whitespace must count as the SAME person for dcount()."""
    events, fake_post = _capture()

    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "alice-uuid")
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "  alice-uuid\n")
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)

    hashes = {_props(e)["user_hash"] for e in events}
    assert len(hashes) == 1, (
        f"same user with whitespace variance MUST collapse to one hash; got {hashes}"
    )


# --------------------------------------------------------------------------- #
# Opt-out still blocks user_hash from being sent (privacy sanity)
# --------------------------------------------------------------------------- #

def test_opt_out_blocks_scan_complete_entirely(monkeypatch):
    monkeypatch.setenv("PQ_ADBC_ADVISOR_USER_ID", "alice-uuid")
    telemetry.disable_telemetry()
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_mk_report(), enabled=True)
    assert events == [], "opt-out must suppress the entire event, user_hash and all"
