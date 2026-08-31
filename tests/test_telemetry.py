"""Tests for spec-compatible telemetry composition."""

from __future__ import annotations

import pytest

from pq_adbc_advisor import telemetry as _telemetry
from pq_adbc_advisor import state as _state
from pq_adbc_advisor.mcode import find_all_connectors
from pq_adbc_advisor.report import ImpactReport, ImpactedArtifact


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(_state, "_LAKEHOUSE_PATH", str(tmp_path / "state.json"))
    # v0.3.4: also isolate the home fallback so tests don't leak state
    # into the developer's real ~/.pq-adbc-advisor.
    monkeypatch.setattr(_state, "_home_fallback_path", lambda: str(tmp_path / "home.json"))
    monkeypatch.setattr(
        _telemetry, "BAKED_IN_CONNECTION_STRING",
        "InstrumentationKey=deadbeef-dead-beef-dead-beefdeadbeef;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/;",
    )
    monkeypatch.delenv("PQ_ADBC_ADVISOR_TELEMETRY", raising=False)
    monkeypatch.delenv("PQ_ADBC_ADVISOR_TENANT_RAW", raising=False)
    monkeypatch.delenv("PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("PQ_ADBC_ADVISOR_APPINSIGHTS_KEY", raising=False)
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(_telemetry, "_post", lambda name, props: captured.append((name, props)))
    yield captured


def _report(pinned: int = 0, unpinned: int = 0, sql: int = 0, has_gateway=None) -> ImpactReport:
    r = ImpactReport(workspace_id="ws-abc-123", scope="workspace")
    hits = []
    for _ in range(pinned):
        hits += find_all_connectors('let S = Snowflake.Databases("h","w",[Implementation="1.0"]) in S')
    for _ in range(unpinned):
        hits += find_all_connectors('let S = Snowflake.Databases("h","w") in S')
    for _ in range(sql):
        hits += find_all_connectors('let S = Sql.Database("h","w") in S')
    r.add(ImpactedArtifact(
        workspace_id="ws-abc-123", item_id="m1", item_name="Model",
        item_type="SemanticModel", has_gateway=has_gateway, hits=hits,
    ))
    return r


# ---- Spec compatibility: event name + field names ---------------------- #

def test_scan_uses_spec_event_name(_isolate):
    _telemetry.emit_scan_summary(_report(pinned=2), enabled=True)
    name, props = _isolate[0]
    assert name == "scan_complete"


def test_scan_carries_spec_fields(_isolate):
    _telemetry.emit_scan_summary(_report(pinned=2, sql=1), enabled=True)
    _, props = _isolate[0]
    # Fields defined verbatim in the spec:
    assert "surface" in props and props["surface"] == "notebook"
    assert "mode" in props and props["mode"] == "diagnose"
    assert "tenant_hash" in props
    assert "artifacts_scanned" in props
    assert "impacted_count" in props
    assert "odbc_pinned_count" in props
    assert "odbc_pinned_at_risk_count" in props
    assert "version" in props
    assert "duration_seconds" in props
    # Spec's impacts_by_connector rendered as flat properties:
    assert props["impacts_by_connector_Snowflake"] == 2
    assert props["impacts_by_connector_SQL_Server"] == 1


def test_pinned_at_risk_requires_no_gateway(_isolate):
    # Pinned + no gateway = at risk
    _telemetry.emit_scan_summary(_report(pinned=3, has_gateway=False), enabled=True)
    _, props = _isolate[0]
    assert props["odbc_pinned_count"] == 3
    assert props["odbc_pinned_at_risk_count"] == 3
    _isolate.clear()
    # Pinned + gateway present = pinned but NOT at risk
    _telemetry.emit_scan_summary(_report(pinned=3, has_gateway=True), enabled=True)
    _, props = _isolate[0]
    assert props["odbc_pinned_count"] == 3
    assert props["odbc_pinned_at_risk_count"] == 0


# ---- Improvement fields (additive) ------------------------------------ #

def test_first_scan_locks_baseline(_isolate):
    _telemetry.emit_scan_summary(_report(pinned=3), enabled=True)
    _, props = _isolate[0]
    assert props["run_count"] == 1
    assert props["is_first_run"] is True
    assert props["first_pinned_odbc"] == 3
    assert props["current_pinned_odbc"] == 3


def test_second_scan_shows_improvement(_isolate):
    captured = _isolate
    _telemetry.emit_scan_summary(_report(pinned=3), enabled=True)
    captured.clear()
    _telemetry.emit_scan_summary(_report(unpinned=3), enabled=True)  # customer removed the pins
    _, props = captured[0]
    assert props["run_count"] == 2
    assert props["is_first_run"] is False
    assert props["first_pinned_odbc"] == 3
    assert props["current_pinned_odbc"] == 0


# ---- Privacy: tenant raw off by default ------------------------------- #

def test_tenant_raw_not_sent_by_default(_isolate):
    _telemetry.emit_scan_summary(_report(pinned=1), enabled=True)
    _, props = _isolate[0]
    assert "tenant_id" not in props
    assert "tenant_hash" in props


def test_tenant_raw_opt_in_env_var(monkeypatch, _isolate):
    monkeypatch.setenv("PQ_ADBC_ADVISOR_TENANT_RAW", "1")
    # We can't force a real tenant guid without notebookutils, so we
    # monkeypatch _tenant_id to simulate a Fabric runtime.
    monkeypatch.setattr(_telemetry, "_tenant_id", lambda: "72f988bf-86f1-41af-91ab-2d7cd011db47")
    _telemetry.emit_scan_summary(_report(pinned=1), enabled=True)
    _, props = _isolate[0]
    assert props["tenant_id"] == "72f988bf-86f1-41af-91ab-2d7cd011db47"


# ---- Opt out ---------------------------------------------------------- #

def test_opt_out_via_kwarg(_isolate):
    _telemetry.emit_scan_summary(_report(pinned=1), enabled=False)
    assert _isolate == []


def test_opt_out_via_env(monkeypatch, _isolate):
    monkeypatch.setenv("PQ_ADBC_ADVISOR_TELEMETRY", "off")
    _telemetry.emit_scan_summary(_report(pinned=1), enabled=True)
    assert _isolate == []


def test_placeholder_ikey_disables(monkeypatch, _isolate):
    monkeypatch.setattr(
        _telemetry, "BAKED_IN_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://x/",
    )
    _telemetry.emit_scan_summary(_report(pinned=1), enabled=True)
    assert _isolate == []


# ---- Connection string resolution ------------------------------------- #

def test_env_connection_string_overrides_baked(monkeypatch, _isolate):
    monkeypatch.setenv(
        "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=env-key-1111-2222-3333-444455556666;"
        "IngestionEndpoint=https://westus3-1.in.applicationinsights.azure.com/;",
    )
    resolved = _telemetry._resolve_connection()
    assert resolved is not None
    ikey, endpoint = resolved
    assert ikey == "env-key-1111-2222-3333-444455556666"
    assert "westus3-1" in endpoint


def test_ikey_env_var_fallback(monkeypatch, _isolate):
    monkeypatch.setattr(_telemetry, "BAKED_IN_CONNECTION_STRING",
                        "InstrumentationKey=00000000-0000-0000-0000-000000000000;")
    monkeypatch.setenv("PQ_ADBC_ADVISOR_APPINSIGHTS_KEY", "ikey-env-only-abc")
    resolved = _telemetry._resolve_connection()
    assert resolved is not None
    assert resolved[0] == "ikey-env-only-abc"


# ---- Validation event names ------------------------------------------ #

def test_validation_event_name(monkeypatch, _isolate):
    from pq_adbc_advisor.report import ValidationReport
    v = ValidationReport(baseline_workspace_id="ws-abc-123")
    _telemetry.emit_validation_summary(v, enabled=True)
    assert _isolate[0][0] == "validation_complete"
