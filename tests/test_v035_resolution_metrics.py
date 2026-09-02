"""Regression tests for v0.3.5 — resolution metrics + time-to-second-scan.

Scope:
  A. First scan emits resolved_* fields, all zero (baseline == current).
  B. Second scan after a real reduction emits POSITIVE resolved_* values
     for the connectors that went down, and per-connector deltas.
  C. Second scan with GROWTH emits NEGATIVE resolved_* (workspace grew).
  D. resolved_by_connector_<Kind> covers both connectors that existed at
     baseline AND connectors that appeared after (never dropped from the
     roll-up just because they weren't in the first-run snapshot).
  E. time_since_first_scan_days is empty on scan #1 and a positive
     number on scan #2 (clock-skew safe).
  F. Field is present on the wire with the exact spelling the KQL
     queries will consume.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
    monkeypatch.setattr(state, "_home_fallback_path", lambda: str(tmp_path / "home.json"))
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=deadbeef-2222-3333-4444-555566667777;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/",
    )
    for fn_name in ("_warn_once_home_fallback", "_warn_once_no_backend",
                    "_maybe_print_first_run_notice"):
        fn = getattr(state, fn_name, None) or getattr(telemetry, fn_name, None)
        if fn and hasattr(fn, "_printed"):
            delattr(fn, "_printed")


def _report(snowflake_pinned: int, redshift_pinned: int, custom_dsn: int) -> ImpactReport:
    r = ImpactReport(workspace_id="ws-resolution-test", scope="workspace")
    r.observed_types = {"SemanticModel": snowflake_pinned + redshift_pinned + custom_dsn}
    for i in range(snowflake_pinned):
        r.add(ImpactedArtifact(
            workspace_id=r.workspace_id, item_id=f"sf-{i}", item_name=f"sf-{i}",
            item_type="SemanticModel", has_gateway=False,
            hits=[ConnectorCall("Snowflake", "Snowflake.Databases",
                                "odbc_to_adbc:snowflake", "1.0", "e", "src", False)],
        ))
    for i in range(redshift_pinned):
        r.add(ImpactedArtifact(
            workspace_id=r.workspace_id, item_id=f"rs-{i}", item_name=f"rs-{i}",
            item_type="SemanticModel", has_gateway=False,
            hits=[ConnectorCall("Amazon Redshift", "AmazonRedshift.Database",
                                "odbc_to_adbc:redshift", "1.0", "e", "src", False)],
        ))
    for i in range(custom_dsn):
        r.add(ImpactedArtifact(
            workspace_id=r.workspace_id, item_id=f"dsn-{i}", item_name=f"dsn-{i}",
            item_type="Dataflow", has_gateway=True,
            hits=[ConnectorCall("Generic ODBC", "Odbc.Query", "none",
                                None, None, "src", True)],
        ))
    return r


def _capture():
    events: list[dict] = []
    class R:
        status_code = 200
        def json(self): return {"itemsAccepted": 1}
    def post(url, data=None, **kw):
        events.append(json.loads(data))
        return R()
    return events, post


def _props(event: dict) -> dict:
    return event["data"]["baseData"]["properties"]


# --------------------------------------------------------------------------- #
# A — first scan: resolved_* all zero
# --------------------------------------------------------------------------- #

def test_first_scan_resolved_fields_all_zero():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_report(3, 2, 1), enabled=True)
    p = _props(events[0])
    assert p["is_first_run"] == "True"
    for field in ("resolved_risk_high", "resolved_risk_medium",
                  "resolved_pinned_odbc", "resolved_custom_dsn",
                  "resolved_migrating_artifacts"):
        assert p[field] == "0", f"{field} must be zero on first run, got {p[field]!r}"
    # Per-connector deltas all zero on first run too.
    assert p["resolved_by_connector_Snowflake"] == "0"
    assert p["resolved_by_connector_Amazon_Redshift"] == "0"
    assert p["resolved_by_connector_Generic_ODBC"] == "0"


# --------------------------------------------------------------------------- #
# B — second scan after cleanup: positive resolved_*
# --------------------------------------------------------------------------- #

def test_second_scan_after_reduction_reports_positive_deltas():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        # Baseline: 3 Snowflake pins, 2 Redshift pins, 1 custom DSN.
        telemetry.emit_scan_summary(_report(3, 2, 1), enabled=True)
        # After cleanup: 1 Snowflake pin, 0 Redshift, 1 custom DSN.
        telemetry.emit_scan_summary(_report(1, 0, 1), enabled=True)
    p = _props(events[1])
    assert p["is_first_run"] == "False"
    assert p["run_count"] == "2"
    # Aggregate rolls up correctly (2 pinned resolved: 2 Snowflake + 2 Redshift = 4).
    assert p["resolved_pinned_odbc"] == "4"
    # Top-level risk deltas (both Snowflake and Redshift were risk_high pins;
    # 5 -> 1 means resolved_risk_high == 4).
    assert p["resolved_risk_high"] == "4"
    # Per-connector deltas.
    assert p["resolved_by_connector_Snowflake"] == "2"
    assert p["resolved_by_connector_Amazon_Redshift"] == "2"
    # Custom DSN stayed the same.
    assert p["resolved_by_connector_Generic_ODBC"] == "0"
    assert p["resolved_custom_dsn"] == "0"


# --------------------------------------------------------------------------- #
# C — second scan with GROWTH: negative deltas
# --------------------------------------------------------------------------- #

def test_second_scan_with_growth_reports_negative_deltas():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        # Baseline: 1 Snowflake pin only.
        telemetry.emit_scan_summary(_report(1, 0, 0), enabled=True)
        # After growth: 3 Snowflake pins.
        telemetry.emit_scan_summary(_report(3, 0, 0), enabled=True)
    p = _props(events[1])
    assert int(p["resolved_pinned_odbc"]) == -2, "growth must produce a negative delta"
    assert int(p["resolved_by_connector_Snowflake"]) == -2


# --------------------------------------------------------------------------- #
# D — per-connector delta covers new connectors added after baseline
# --------------------------------------------------------------------------- #

def test_resolved_by_connector_includes_connectors_added_after_baseline():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        # Baseline: only Snowflake.
        telemetry.emit_scan_summary(_report(2, 0, 0), enabled=True)
        # New scan adds Redshift AND removes Snowflake.
        telemetry.emit_scan_summary(_report(0, 2, 0), enabled=True)
    p = _props(events[1])
    # Snowflake: baseline 2, current 0 → delta +2 (resolved).
    assert p["resolved_by_connector_Snowflake"] == "2"
    # Redshift: baseline 0, current 2 → delta -2 (added).
    assert int(p["resolved_by_connector_Amazon_Redshift"]) == -2


# --------------------------------------------------------------------------- #
# E — time_since_first_scan_days
# --------------------------------------------------------------------------- #

def test_time_since_first_scan_days_empty_on_first_run():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_report(1, 0, 0), enabled=True)
    assert _props(events[0])["time_since_first_scan_days"] == ""


def test_time_since_first_scan_days_positive_on_second_run(monkeypatch):
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_report(1, 0, 0), enabled=True)

    # Rewrite the baseline first_run_at to 5 days ago, so scan #2 reports
    # a stable, positive delta regardless of wall-clock timing.
    persisted = state.load_state()
    five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    persisted["first_run_at"] = five_days_ago
    state.save_state(persisted)

    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_report(1, 0, 0), enabled=True)
    delta = float(_props(events[1])["time_since_first_scan_days"])
    assert 4.9 <= delta <= 5.1, f"expected ~5.0 days, got {delta}"


def test_time_since_first_scan_days_never_negative_under_clock_skew():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_report(1, 0, 0), enabled=True)

    persisted = state.load_state()
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    persisted["first_run_at"] = future
    state.save_state(persisted)

    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_report(1, 0, 0), enabled=True)
    delta = float(_props(events[1])["time_since_first_scan_days"])
    assert delta == 0.0, "clock-skew must be clamped to 0, not go negative"


# --------------------------------------------------------------------------- #
# F — field spellings on the wire (KQL query safety)
# --------------------------------------------------------------------------- #

def test_wire_field_spellings_match_kql_contract():
    events, fake_post = _capture()
    with patch.object(telemetry.requests, "post", side_effect=fake_post):
        telemetry.emit_scan_summary(_report(1, 0, 0), enabled=True)
    p = _props(events[0])
    # Every v0.3.5 field the KQL query pack will reference:
    expected = {
        "resolved_risk_high", "resolved_risk_medium", "resolved_pinned_odbc",
        "resolved_custom_dsn", "resolved_migrating_artifacts",
        "time_since_first_scan_days",
    }
    assert expected.issubset(p.keys()), f"missing fields: {expected - p.keys()}"
