"""Tests for the persistent state + telemetry composition.

We don't test the network POST here (App Insights ingestion) - we only
test that the state persists correctly and that the telemetry payload
carries the first-run and current-run counters.
"""

from __future__ import annotations

import json
import os

import pytest

from pq_adbc_advisor import state as _state


@pytest.fixture(autouse=True)
def _isolated_state_path(monkeypatch, tmp_path):
    """Redirect both backend paths to per-test temp files (v0.3.4)."""
    monkeypatch.setattr(_state, "_LAKEHOUSE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(_state, "_home_fallback_path", lambda: str(tmp_path / "home.json"))
    yield


def test_first_run_records_baseline():
    current = {
        "risk_high": 3, "risk_medium": 5, "risk_low": 12,
        "risk_unknown": 0, "risk_na": 14, "custom_dsn": 2,
        "total_calls": 34, "migrating_artifacts": 4, "pinned_odbc": 3,
        "counts_by_connector": {"Snowflake": 4, "Amazon Redshift": 2},
    }
    combined = _state.compute_run_state(current, run_id="run-0001")
    assert combined["is_first_run"] is True
    assert combined["run_count"] == 1
    assert combined["first_risk_high"] == 3
    assert combined["first_risk_medium"] == 5
    assert combined["first_custom_dsn"] == 2
    assert combined["first_counts_by_connector"]["Snowflake"] == 4


def test_second_run_preserves_first_run_snapshot():
    # First run: 3 high, 5 medium
    _state.compute_run_state(
        {"risk_high": 3, "risk_medium": 5, "risk_low": 12,
         "risk_unknown": 0, "risk_na": 14, "custom_dsn": 2,
         "total_calls": 34, "migrating_artifacts": 4, "pinned_odbc": 3,
         "counts_by_connector": {"Snowflake": 4}},
        run_id="run-0001",
    )
    # Second run: customer resolved 2 high-risk; still same first-run baseline
    combined = _state.compute_run_state(
        {"risk_high": 1, "risk_medium": 5, "risk_low": 14,
         "risk_unknown": 0, "risk_na": 14, "custom_dsn": 2,
         "total_calls": 34, "migrating_artifacts": 4, "pinned_odbc": 1,
         "counts_by_connector": {"Snowflake": 4}},
        run_id="run-0002",
    )
    assert combined["is_first_run"] is False
    assert combined["run_count"] == 2
    # First-run snapshot preserved
    assert combined["first_risk_high"] == 3
    assert combined["first_risk_medium"] == 5
    # (We don't return current_* fields from compute_run_state - the caller
    # provides them.  This test just proves the first-run snapshot is
    # locked in and increment works.)


def test_third_run_increments_run_count():
    for i in range(3):
        combined = _state.compute_run_state(
            {"risk_high": 3 - i, "risk_medium": 0, "risk_low": 0,
             "risk_unknown": 0, "risk_na": 0, "custom_dsn": 0,
             "total_calls": 3, "migrating_artifacts": 1, "pinned_odbc": 3 - i,
             "counts_by_connector": {}},
            run_id=f"run-{i}",
        )
    assert combined["run_count"] == 3
    assert combined["first_risk_high"] == 3  # locked in from first run


def test_reset_state_returns_to_first_run():
    _state.compute_run_state(
        {"risk_high": 5, "risk_medium": 0, "risk_low": 0,
         "risk_unknown": 0, "risk_na": 0, "custom_dsn": 0,
         "total_calls": 5, "migrating_artifacts": 1, "pinned_odbc": 5,
         "counts_by_connector": {}},
        run_id="r1",
    )
    assert _state.load_state()["run_count"] == 1
    _state.reset_state()
    assert _state.load_state() is None
    combined = _state.compute_run_state(
        {"risk_high": 0, "risk_medium": 0, "risk_low": 0,
         "risk_unknown": 0, "risk_na": 0, "custom_dsn": 0,
         "total_calls": 0, "migrating_artifacts": 0, "pinned_odbc": 0,
         "counts_by_connector": {}},
        run_id="r2",
    )
    assert combined["is_first_run"] is True
    assert combined["first_risk_high"] == 0  # new first-run baseline is 0


def test_load_state_returns_none_when_missing():
    assert _state.load_state() is None


def test_load_state_ignores_wrong_schema_version(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(_state, "_LAKEHOUSE_PATH", str(path))
    path.write_text(json.dumps({"schema_version": 99, "first_risk_high": 999}))
    assert _state.load_state() is None
