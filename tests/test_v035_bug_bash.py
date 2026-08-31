"""Regression tests for the v0.3.5 telemetry deep bug bash (Bugs 1–9).

Every test locks in a specific defect the code-review agent found in
v0.3.4 telemetry / state code so it can't silently regress. Numbering
matches the bug bash report.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from pq_adbc_advisor import state, telemetry
from pq_adbc_advisor.mcode import ConnectorCall
from pq_adbc_advisor.report import ImpactedArtifact, ImpactReport


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "_LAKEHOUSE_PATH", str(tmp_path / "lakehouse.json"))
    monkeypatch.setattr(state, "_LAKEHOUSE_OPT_OUT", str(tmp_path / "opt_out_lakehouse.json"))
    monkeypatch.setattr(state, "_HOME_DIR", str(tmp_path / "home"))
    # keep legacy shim in sync with the tmp home dir
    monkeypatch.setattr(state, "_home_fallback_path",
                        lambda: str(tmp_path / "home" / "state.json"))
    # Reset the process-scoped warn-once flags for each test.
    for fn_name in ("_warn_once_home_fallback", "_warn_once_no_backend",
                    "_warn_once_schema_downgrade"):
        fn = getattr(state, fn_name, None)
        if fn and hasattr(fn, "_printed"):
            delattr(fn, "_printed")
    if hasattr(telemetry._maybe_print_first_run_notice, "_printed"):
        delattr(telemetry._maybe_print_first_run_notice, "_printed")


def _mk_report(pinned: int = 1, workspace_id: str = "ws-bb") -> ImpactReport:
    r = ImpactReport(workspace_id=workspace_id, scope="workspace")
    r.observed_types = {"SemanticModel": pinned}
    for i in range(pinned):
        r.add(ImpactedArtifact(
            workspace_id=workspace_id, item_id=f"sm-{i}", item_name=f"sm-{i}",
            item_type="SemanticModel", has_gateway=False,
            hits=[ConnectorCall("Snowflake", "Snowflake.Databases",
                                "odbc_to_adbc:snowflake", "1.0",
                                "acme", "src", False)],
        ))
    return r


# --------------------------------------------------------------------------- #
# Bug 1 — set_telemetry_opt_out MUST NOT corrupt baseline lifecycle
# --------------------------------------------------------------------------- #

def test_bug1_disable_enable_then_first_scan_still_gets_baseline():
    """After disable_telemetry() → enable_telemetry() → first real scan,
    the scan must (a) report is_first_run=True and (b) have real baseline
    numbers (not zeros left over from a stub opt-out file)."""
    telemetry.disable_telemetry()
    telemetry.enable_telemetry()

    first = state.compute_run_state(
        current_counts={
            "risk_high": 3, "risk_medium": 2, "risk_low": 0,
            "risk_unknown": 0, "risk_na": 0, "custom_dsn": 1,
            "total_calls": 5, "migrating_artifacts": 3, "pinned_odbc": 3,
            "counts_by_connector": {"Snowflake": 3, "Redshift": 2},
        },
        run_id="run-1",
        workspace_id="ws-bb1",
    )
    assert first["is_first_run"] is True, "must be treated as first run"
    assert first["first_risk_high"] == 3, "baseline must reflect the real scan, not zeros"
    assert first["first_pinned_odbc"] == 3
    assert first["run_count"] == 1


def test_bug1_first_run_notice_fires_after_disable_enable_cycle():
    """The privacy notice must still print on the first REAL scan after
    an opt-out roundtrip."""
    telemetry.disable_telemetry()
    telemetry.enable_telemetry()

    captured: list[str] = []
    with patch("builtins.print", side_effect=lambda *a, **kw: captured.append(" ".join(str(x) for x in a))):
        # Simulate first-run detection + notice.
        combined = state.compute_run_state(
            {"risk_high": 1, "pinned_odbc": 1, "custom_dsn": 0,
             "counts_by_connector": {"Snowflake": 1}},
            "r1", workspace_id="ws-bb1-notice",
        )
        telemetry._maybe_print_first_run_notice(bool(combined["is_first_run"]))
    printed = "\n".join(captured)
    assert "anonymous telemetry" in printed.lower(), \
        "first-run privacy notice must fire on the first real scan after enable/disable"


# --------------------------------------------------------------------------- #
# Bug 2 — home fallback keyed by workspace + tenant
# --------------------------------------------------------------------------- #

def test_bug2_two_workspaces_get_independent_baselines(monkeypatch):
    """Scanning workspace A then workspace B from the same machine must
    NOT report B's scan as a subsequent run against A's baseline."""
    # Force home-fallback by making the lakehouse path unwritable.
    bad = os.path.join(str(monkeypatch), "no-such")  # nonsense path
    monkeypatch.setattr(state, "_LAKEHOUSE_PATH", "/definitely/does/not/exist/x.json")

    a = state.compute_run_state(
        {"risk_high": 5, "pinned_odbc": 5, "custom_dsn": 0,
         "counts_by_connector": {"Snowflake": 5}},
        "r-a", workspace_id="ws-A", tenant_hash="tenant-1",
    )
    b = state.compute_run_state(
        {"risk_high": 1, "pinned_odbc": 1, "custom_dsn": 0,
         "counts_by_connector": {"Redshift": 1}},
        "r-b", workspace_id="ws-B", tenant_hash="tenant-1",
    )
    assert a["is_first_run"] is True
    assert b["is_first_run"] is True, "workspace B must have its own first-run baseline"
    assert b["first_risk_high"] == 1, "B's baseline must be B's numbers, not A's"


def test_bug2_home_path_actually_encodes_workspace_and_tenant():
    p1 = state._home_baseline_path("ws-alpha", "tenant-A")
    p2 = state._home_baseline_path("ws-beta", "tenant-A")
    p3 = state._home_baseline_path("ws-alpha", "tenant-B")
    assert p1 != p2, "different workspaces must resolve to different files"
    assert p1 != p3, "different tenants must resolve to different files"
    # Special chars in workspace_id are sanitized (path traversal safety).
    # The sanitizer replaces `/` and other separators — `..` on its own is
    # inert as a filename component. Guarantee: no path separators sneak
    # through so `open()` can't escape the state directory.
    p_bad = state._home_baseline_path("../etc/passwd", "t")
    base = os.path.basename(p_bad)
    assert "/" not in base and os.sep not in base


# --------------------------------------------------------------------------- #
# Bug 3 — is_telemetry_opted_out consults ALL backends
# --------------------------------------------------------------------------- #

def test_bug3_opt_out_in_home_beats_no_flag_in_lakehouse(monkeypatch, tmp_path):
    """Opt-out persisted in home backend must NOT be silently overridden
    by a lakehouse baseline that has no opt-out flag."""
    # Write a lakehouse baseline (no opt-out flag).
    lakehouse_baseline = tmp_path / "lakehouse.json"
    lakehouse_baseline.write_text(json.dumps({
        "schema_version": 1,
        "first_run_at": "2026-01-01T00:00:00Z",
        "first_risk_high": 2,
    }))
    # Write an opt-out file to the HOME opt-out path.
    home_opt = tmp_path / "home" / "opt_out.json"
    home_opt.parent.mkdir(parents=True, exist_ok=True)
    home_opt.write_text(json.dumps({
        "schema_version": 1,
        "telemetry_opt_out": True,
    }))
    assert state.is_telemetry_opted_out() is True, \
        "any opt-out backend must win over any non-opted-out backend"


# --------------------------------------------------------------------------- #
# Bug 4 — read-modify-write race in compute_run_state
# --------------------------------------------------------------------------- #

def test_bug4_concurrent_compute_run_state_from_threads_does_not_lose_updates():
    """N threads each computing a run must produce run_count == 1 + N-1
    resolutions with no lost updates."""
    N = 16
    # Seed baseline by running once serially.
    state.compute_run_state(
        {"risk_high": 3, "pinned_odbc": 3, "custom_dsn": 0,
         "counts_by_connector": {"Snowflake": 3}},
        "seed", workspace_id="ws-race", tenant_hash="tenant-1",
    )
    results: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            r = state.compute_run_state(
                {"risk_high": 1, "pinned_odbc": 1, "custom_dsn": 0,
                 "counts_by_connector": {"Snowflake": 1}},
                f"r-{i}", workspace_id="ws-race", tenant_hash="tenant-1",
            )
            with lock:
                results.append(r)
        except BaseException as e:  # pragma: no cover
            with lock:
                errors.append(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(N):
            pool.submit(worker, i)
    assert not errors
    # After N subsequent runs, the persisted run_count must equal 1 (seed) + N.
    final = state.load_state("ws-race", "tenant-1")
    assert final is not None
    assert final["run_count"] == 1 + N, (
        f"lost update: expected {1 + N} runs, got {final['run_count']}"
    )
    # And every worker saw a monotonically increasing, unique run_count.
    counts = sorted(r["run_count"] for r in results)
    assert counts == list(range(2, 2 + N)), f"non-unique run_counts: {counts}"


# --------------------------------------------------------------------------- #
# Bug 5 — shared tmp path corruption
# --------------------------------------------------------------------------- #

def test_bug5_unique_tmp_paths_per_writer(tmp_path):
    """Two writers hitting save_state at once must use different tmp paths
    (checked by observing the actual write files created), so neither can
    truncate the other's in-flight write."""
    tmp_files: list[str] = []

    real_open = open
    lock = threading.Lock()

    def spy_open(*args, **kwargs):
        # Capture any path that looks like a state tmp file.
        p = args[0] if args else kwargs.get("file")
        if isinstance(p, str) and ".tmp." in p:
            with lock:
                tmp_files.append(p)
        return real_open(*args, **kwargs)

    with patch("pq_adbc_advisor.state.open", side_effect=spy_open):
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i in range(8):
                pool.submit(
                    state.save_state,
                    {"schema_version": 1, "n": i, "workspace_id": f"ws-{i}"},
                    f"ws-{i}", "tenant",
                )
    # Every tmp file recorded must be unique.
    assert len(tmp_files) == len(set(tmp_files)), \
        f"shared tmp path detected: {[f for f in tmp_files if tmp_files.count(f) > 1]}"


# --------------------------------------------------------------------------- #
# Bug 6 — schema_version downgrade guard
# --------------------------------------------------------------------------- #

def test_bug6_write_refuses_to_overwrite_higher_schema(tmp_path, capsys):
    """A future v0.4 client writes schema_version=2; this v0.3.5 client
    must NOT clobber it on write."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "schema_version": 2,
        "first_risk_high": 42,
        "workspace_id": "ws-future",
    }))
    ok = state._write_json_atomic(str(p), {
        "schema_version": 1,  # current client's schema
        "first_risk_high": 0,
        "workspace_id": "ws-future",
    })
    assert ok is False, "must refuse to overwrite a higher schema"
    # The v2 file must survive intact.
    surviving = json.loads(p.read_text())
    assert surviving["schema_version"] == 2
    assert surviving["first_risk_high"] == 42


# --------------------------------------------------------------------------- #
# Bug 7 — hours-saved credits INSPECTED items only
# --------------------------------------------------------------------------- #

def test_bug7_hours_saved_uses_inspected_items_only(tmp_path, monkeypatch):
    """A workspace with 1 inspected + 99 permission_denied must NOT claim
    100 items' worth of manual triage saved."""
    r = ImpactReport(workspace_id="ws-bb7", scope="workspace")
    r.observed_types = {"SemanticModel": 100}
    # 1 inspected.
    r.add(ImpactedArtifact(
        workspace_id="ws-bb7", item_id="sm-0", item_name="sm-0",
        item_type="SemanticModel", has_gateway=False,
        hits=[ConnectorCall("Snowflake", "Snowflake.Databases",
                            "odbc_to_adbc:snowflake", "1.0", "e", "src", False)],
    ))
    # 99 gated.
    for i in range(1, 100):
        r.record_skipped(f"sm-{i}", "n", "SemanticModel", "permission_denied")

    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=cafebabe-1111-2222-3333-444455556666;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    events: list[dict] = []
    class R:
        status_code = 200
        text = ""
        def json(self): return {"itemsAccepted": 1}
    with patch.object(telemetry.requests, "post",
                      side_effect=lambda url, data=None, **kw:
                      (events.append(json.loads(data)) or R())):
        telemetry.emit_scan_summary(r, enabled=True)
    p = events[0]["data"]["baseData"]["properties"]
    # 1 inspected * 3 min / 60 = 0.05h. Old buggy behavior would have been
    # 100 * 3 / 60 = 5.0h — a 100x overclaim.
    assert float(p["estimated_manual_hours_saved"]) == pytest.approx(0.05, abs=0.01)
    assert int(p["inspected_artifacts"]) == 1
    assert int(p["artifacts_scanned"]) == 100


# --------------------------------------------------------------------------- #
# Bug 8 — empty workspace coverage
# --------------------------------------------------------------------------- #

def test_bug8_empty_workspace_reports_100_percent_coverage():
    """A workspace with zero items must not look 0% covered."""
    r = ImpactReport(workspace_id="ws-empty", scope="workspace")
    r.observed_types = {}
    cov = r.coverage()
    assert cov["score_pct"] == 100, "empty workspace must not look 0% covered"


def test_bug8_normal_workspace_coverage_math_unchanged():
    r = ImpactReport(workspace_id="ws-normal")
    r.observed_types = {"SemanticModel": 5, "Notebook": 5}
    cov = r.coverage()
    # 5 inspected out of 10 total = 50%.
    assert cov["score_pct"] == 50


# --------------------------------------------------------------------------- #
# Bug 9 — _post observability
# --------------------------------------------------------------------------- #

def test_bug9_send_history_records_ok(monkeypatch):
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=cafebabe-9999-8888-7777-666655554444;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    class R:
        status_code = 200
        text = "{'itemsAccepted': 1}"
        def json(self): return {"itemsAccepted": 1}
    with patch.object(telemetry.requests, "post",
                      side_effect=lambda url, data=None, **kw: R()):
        telemetry._post("scan_complete", {"version": "0.3.5", "workspace_id": "ws"})
    hist = telemetry.get_send_history()
    assert hist and hist[-1]["status"] == "ok"


def test_bug9_send_history_records_captive_portal(monkeypatch):
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=cafebabe-aaaa-bbbb-cccc-ddddeeeeffff;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    class R:
        status_code = 200
        text = "<!DOCTYPE html><html><body>Please sign in to your corporate proxy.</body></html>"
        def json(self): return {}
    with patch.object(telemetry.requests, "post",
                      side_effect=lambda url, data=None, **kw: R()):
        telemetry._post("scan_complete", {"version": "0.3.5"})
    hist = telemetry.get_send_history()
    assert hist[-1]["status"] == "captive_portal", \
        f"captive-portal HTML in 200 body must be detected; got {hist[-1]}"


def test_bug9_send_history_records_429_and_retries_once(monkeypatch):
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=cafebabe-1234-1234-1234-123412341234;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    seq = [429, 200]
    class R:
        def __init__(self, code):
            self.status_code = code
            self.text = "{}"
        def json(self): return {}
    def fake(url, data=None, **kw):
        return R(seq.pop(0))
    with patch.object(telemetry.requests, "post", side_effect=fake):
        telemetry._post("scan_complete", {"version": "0.3.5"})
    hist = telemetry.get_send_history()
    assert hist[-1]["status"] == "retried_ok", \
        f"429 must trigger one retry and be recorded; got {hist[-1]}"
    assert not seq, "retry must have consumed the second response"


def test_bug9_send_history_records_network_failure_and_retries(monkeypatch):
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=cafebabe-dead-dead-dead-deaddeaddead;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    attempts = {"n": 0}
    def fake(url, data=None, **kw):
        attempts["n"] += 1
        raise ConnectionError("simulated DNS blip")
    with patch.object(telemetry.requests, "post", side_effect=fake):
        telemetry._post("scan_complete", {"version": "0.3.5"})
    assert attempts["n"] == 2, "network failure must trigger exactly one retry"
    hist = telemetry.get_send_history()
    assert hist[-1]["status"] == "network"


def test_bug9_telemetry_status_exposes_last_send(monkeypatch):
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=cafebabe-1111-1111-1111-111111111111;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    class R:
        status_code = 200
        text = "{}"
        def json(self): return {}
    with patch.object(telemetry.requests, "post",
                      side_effect=lambda url, data=None, **kw: R()):
        telemetry._post("scan_complete", {"version": "0.3.5"})
    status = telemetry.telemetry_status()
    assert status["last_send"] is not None
    assert status["last_send"]["status"] == "ok"
    assert "ok" in status["recent_send_statuses"]
