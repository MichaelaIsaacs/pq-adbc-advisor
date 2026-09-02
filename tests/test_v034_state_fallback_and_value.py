"""Regression tests for v0.3.4 — state fallback + value-story metrics.

Scope:
  A. state.save_state falls back to ~/.pq-adbc-advisor when the
     lakehouse path isn't writable (fixes the "is_first_run=True forever"
     bug seen in App Insights).
  B. state.state_backend() reports which backend is in use so telemetry
     can prove baselines are persisting.
  C. Compound baseline / delta round-trip works through the fallback
     path — a second scan sees is_first_run=False and preserved
     first_* fields.
  D. New value-story telemetry fields ride on every scan_complete:
     coverage_score_pct, sempy_hits, sempy_used,
     estimated_manual_hours_saved, session_id,
     skipped_by_reason_<reason>.
  E. session_id is stable across two emissions in the same process.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from pq_adbc_advisor import state, telemetry
from pq_adbc_advisor.mcode import ConnectorCall
from pq_adbc_advisor.report import ImpactedArtifact, ImpactReport


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_backends(tmp_path, monkeypatch):
    """Point all state paths at temp dirs (baseline + opt-out), and reset
    the warn-once flags so each test can observe them fresh."""
    lakehouse = tmp_path / "lakehouse" / "pq_adbc_advisor_state.json"
    home      = tmp_path / "home"      / "state.json"

    monkeypatch.setattr(state, "_LAKEHOUSE_PATH", str(lakehouse))
    monkeypatch.setattr(state, "_LAKEHOUSE_OPT_OUT", str(tmp_path / "lakehouse" / "opt_out.json"))
    monkeypatch.setattr(state, "_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(state, "_home_fallback_path", lambda: str(home))

    # Reset warn-once markers.
    for fn_name in ("_warn_once_home_fallback", "_warn_once_no_backend"):
        fn = getattr(state, fn_name)
        if hasattr(fn, "_printed"):
            delattr(fn, "_printed")

    return {"lakehouse": lakehouse, "home": home, "tmp": tmp_path}


def _make_report() -> ImpactReport:
    r = ImpactReport(workspace_id="ws-1234", scope="workspace")
    r.observed_types = {"SemanticModel": 4, "Dataflow": 1, "Notebook": 3}
    r.used_sempy_path = True
    r.sempy_hits = 3
    r.add(ImpactedArtifact(
        workspace_id=r.workspace_id, item_id="sm-1", item_name="High",
        item_type="SemanticModel", has_gateway=False,
        hits=[ConnectorCall("Snowflake", "Snowflake.Databases",
                            "odbc_to_adbc:snowflake", "1.0", "acme", "src", False)],
    ))
    r.record_skipped("sm-97", "Ghost",  "SemanticModel", "deleted_during_scan")
    r.record_skipped("sm-98", "Locked", "SemanticModel", "permission_denied")
    return r


# --------------------------------------------------------------------------- #
# A + B — save_state fallback + state_backend()
# --------------------------------------------------------------------------- #

def test_save_state_writes_to_lakehouse_when_writable(isolated_backends):
    ok = state.save_state({"schema_version": 1, "hello": "world"})
    assert ok is True
    assert isolated_backends["lakehouse"].exists()
    assert not isolated_backends["home"].exists()
    assert state.state_backend() == "lakehouse"


def _legacy_home_state(tmp: Any) -> Any:
    """v0.3.5: the home fallback now uses a per-workspace filename.
    Legacy callers (no workspace_id passed) hit ``state-notenant-legacy.json``
    inside the sanitized home dir.
    """
    return tmp / "home" / "state-notenant-legacy.json"


def test_save_state_falls_back_to_home_when_lakehouse_read_only(isolated_backends, monkeypatch):
    # Make the lakehouse dir unwritable by pointing it at a path that
    # can't be created. os.makedirs on a real file will raise NotADirectory.
    bad_path = isolated_backends["tmp"] / "block-me"
    bad_path.write_text("i am a file, not a directory")
    monkeypatch.setattr(state, "_LAKEHOUSE_PATH", str(bad_path / "state.json"))

    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: captured.append(" ".join(str(x) for x in a)))
    ok = state.save_state({"schema_version": 1, "hello": "world"})
    assert ok is True
    # v0.3.5: legacy (no workspace_id) writes land in state-notenant-legacy.json
    assert _legacy_home_state(isolated_backends["tmp"]).exists()
    assert any("~/.pq-adbc-advisor" in m or "home" in m.lower() or ".pq-adbc-advisor" in m for m in captured), \
        f"expected home-fallback warning, got: {captured}"
    assert state.state_backend() == "home"


def test_save_state_reports_memory_when_no_backend_writable(isolated_backends, monkeypatch):
    bad_lakehouse = isolated_backends["tmp"] / "no-write-lh"
    bad_lakehouse.write_text("blocked")
    bad_home = isolated_backends["tmp"] / "no-write-home"
    bad_home.write_text("blocked")
    monkeypatch.setattr(state, "_LAKEHOUSE_PATH", str(bad_lakehouse / "state.json"))
    # v0.3.5: both the legacy home function AND _HOME_DIR need to point at
    # blocked paths so the state module cannot create the workspace-keyed file.
    monkeypatch.setattr(state, "_HOME_DIR", str(bad_home))
    monkeypatch.setattr(state, "_home_fallback_path", lambda: str(bad_home / "state.json"))

    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **kw: captured.append(" ".join(str(x) for x in a)))
    ok = state.save_state({"schema_version": 1})
    assert ok is False
    assert any("could not persist" in m.lower() for m in captured), \
        f"expected no-backend warning, got: {captured}"
    assert state.state_backend() == "memory"


# --------------------------------------------------------------------------- #
# C — baseline / delta round-trip through the fallback
# --------------------------------------------------------------------------- #

def test_first_and_second_run_round_trip_through_home_fallback(isolated_backends, monkeypatch):
    # Force home-only writes by giving lakehouse a "blocking" file.
    bad = isolated_backends["tmp"] / "blocked"
    bad.write_text("x")
    monkeypatch.setattr(state, "_LAKEHOUSE_PATH", str(bad / "s.json"))

    # First run: baseline gets locked in on disk.
    first = state.compute_run_state(
        current_counts={
            "risk_high": 3, "risk_medium": 1, "risk_low": 0,
            "risk_unknown": 0, "risk_na": 0, "custom_dsn": 1,
            "total_calls": 4, "migrating_artifacts": 3, "pinned_odbc": 3,
            "counts_by_connector": {"Snowflake": 3, "Generic ODBC": 1},
        },
        run_id="run-1",
    )
    assert first["is_first_run"] is True
    assert first["run_count"] == 1
    assert first["first_risk_high"] == 3
    assert isolated_backends["home"].exists() or _legacy_home_state(isolated_backends["tmp"]).exists()

    # Second run: baseline preserved, run_count bumps to 2.
    second = state.compute_run_state(
        current_counts={
            "risk_high": 1, "risk_medium": 1, "risk_low": 0,
            "risk_unknown": 0, "risk_na": 0, "custom_dsn": 1,
            "total_calls": 2, "migrating_artifacts": 1, "pinned_odbc": 1,
            "counts_by_connector": {"Snowflake": 1, "Generic ODBC": 1},
        },
        run_id="run-2",
    )
    assert second["is_first_run"] is False
    assert second["run_count"] == 2
    # The baseline stayed the same — this is the "improvement over time"
    # single-row KQL trick.
    assert second["first_risk_high"] == 3
    assert second["first_pinned_odbc"] == 3


# --------------------------------------------------------------------------- #
# D + E — value-story telemetry fields + session_id stability
# --------------------------------------------------------------------------- #

def test_scan_complete_payload_carries_value_story_fields(isolated_backends, monkeypatch):
    # Force a non-placeholder ikey so telemetry actually POSTs.
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=deadbeef-0001-0002-0003-000000000001;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )

    captured: list[dict] = []
    class FakeResp:
        status_code = 200
        def json(self): return {"itemsAccepted": 1}
    def fake_post(url, data=None, **kw):
        captured.append(json.loads(data))
        return FakeResp()

    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_make_report(), enabled=True, duration_seconds=42.5)
        telemetry.emit_scan_summary(_make_report(), enabled=True, duration_seconds=11.0)

    assert len(captured) == 2, "both scans must send an event"
    props1 = captured[0]["data"]["baseData"]["properties"]
    props2 = captured[1]["data"]["baseData"]["properties"]

    # v0.3.4 base fields
    for key in ("session_id", "state_backend", "coverage_score_pct",
                "sempy_hits", "sempy_used", "estimated_manual_hours_saved"):
        assert key in props1, f"scan_complete missing value-story field: {key}"

    # session_id stable across two scans in the same process.
    assert props1["session_id"] == props2["session_id"], \
        "session_id must be stable across scan+validate in one kernel"
    # Two DIFFERENT run_ids for two DIFFERENT scans in that session.
    assert props1["run_id"] != props2["run_id"]

    # Coverage: our fake workspace has 5 inspectable of 8 (SemanticModel:4 +
    # Dataflow:1 = 5 of 8 observed) = 62%.
    assert 60 <= int(props1["coverage_score_pct"]) <= 63

    # Skip-reason breakdown lands as one field per reason.
    # (App Insights receives all custom dimensions as strings.)
    assert props1["skipped_by_reason_deleted_during_scan"] == "1"
    assert props1["skipped_by_reason_permission_denied"] == "1"

    # v0.3.5 Bug 7 fix: hours-saved now uses INSPECTED items only, not
    # scanned+skipped. This report inspects 1 artifact → 1*3/60 = 0.05h.
    assert float(props1["estimated_manual_hours_saved"]) == pytest.approx(0.05, abs=0.01)
    # And the new inspected_artifacts field lands on the wire.
    assert int(props1["inspected_artifacts"]) == 1
    assert int(props1["artifacts_scanned"]) == 3  # 1 inspected + 2 skipped

    # state_backend reflects the isolated_backends fixture (lakehouse writable).
    assert props1["state_backend"] in ("lakehouse", "home")


def test_session_id_module_helper_is_stable():
    a = telemetry.session_id()
    b = telemetry.session_id()
    assert a == b
    assert len(a) >= 16, "session_id should be a real uuid hex"
