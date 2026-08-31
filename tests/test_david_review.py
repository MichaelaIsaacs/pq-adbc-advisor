"""Regression tests for the David Coe PR review (2026-08-19).

Each test is tagged with the review-comment path so future reviewers can
trace back to the original comment.
"""

from __future__ import annotations

import pytest

from pq_adbc_advisor.mcode import find_all_connectors
from pq_adbc_advisor import state as _state
from pq_adbc_advisor import telemetry as _telemetry


# --------------------------------------------------------------------------- #
# constants.py — connector M-function corrections
# --------------------------------------------------------------------------- #

def test_snowflake_contents_not_detected():
    """Snowflake.Contents does not exist in the connector; do not match it."""
    calls = find_all_connectors('let S = Snowflake.Contents("h") in S')
    # Snowflake.Contents falls back to prefix-only friendly name and is NOT
    # in the impacted list.
    assert not any(c.migration.startswith("odbc_to_adbc:snowflake") for c in calls)


def test_snowflake_databases_still_detected():
    calls = find_all_connectors('let S = Snowflake.Databases("h","w") in S')
    assert len(calls) == 1
    assert calls[0].migration == "odbc_to_adbc:snowflake"


def test_bigquery_aad_contents_not_detected():
    """GoogleBigQueryAad.Contents does not exist per David's review."""
    calls = find_all_connectors('let S = GoogleBigQueryAad.Contents([]) in S')
    assert not any(c.migration.startswith("odbc_to_adbc:bigquery") for c in calls)


def test_databricks_query_direct_query_detected():
    """Databricks.Query is the DirectQuery entry point; must be detected."""
    calls = find_all_connectors('let S = Databricks.Query("host","warehouse","SELECT 1") in S')
    assert len(calls) == 1
    assert calls[0].connector_kind == "Databricks"
    assert calls[0].migration == "odbc_to_adbc:databricks"


def test_databricks_multicloud_catalogs_detected():
    calls = find_all_connectors('let S = DatabricksMultiCloud.Catalogs("host","wh") in S')
    assert len(calls) == 1
    assert calls[0].connector_kind == "Databricks"


def test_databricks_multicloud_query_detected():
    calls = find_all_connectors('let S = DatabricksMultiCloud.Query("host","wh","SELECT 1") in S')
    assert len(calls) == 1
    assert calls[0].connector_kind == "Databricks"


def test_azure_databricks_no_longer_in_impacted_list():
    """AzureDatabricks.* was invented; the real name is Databricks / DatabricksMultiCloud."""
    calls = find_all_connectors('let S = AzureDatabricks.Catalogs("host","wh") in S')
    # It still shows up (unknown external connector) but should NOT be tagged as migrating.
    databricks_hits = [c for c in calls if c.migration == "odbc_to_adbc:databricks"]
    assert databricks_hits == []


def test_dremio_versioned_variants_detected():
    for fn in ("Dremio.DatabasesV300", "Dremio.DatabasesV370",
               "DremioCloud.DatabasesByServer",
               "DremioCloud.DatabasesByServerV330",
               "DremioCloud.DatabasesByServerV370"):
        m = f'let S = {fn}("host") in S'
        calls = find_all_connectors(m)
        assert len(calls) == 1, f"{fn} should be detected"
        assert calls[0].migration == "odbc_to_adbc:dremio", f"{fn} wrong migration bucket"


def test_dremio_contents_removed():
    """Dremio.Contents doesn't exist in the real connector."""
    calls = find_all_connectors('let S = Dremio.Contents("host") in S')
    assert not any(c.migration == "odbc_to_adbc:dremio" for c in calls)


def test_redshift_tables_no_longer_migrating():
    """AmazonRedshift.Tables doesn't exist; only .Database."""
    calls = find_all_connectors('let S = AmazonRedshift.Tables("h","d") in S')
    assert not any(c.migration == "odbc_to_adbc:redshift" for c in calls)


def test_redshift_database_still_detected():
    calls = find_all_connectors('let S = AmazonRedshift.Database("h:5439","prod") in S')
    assert len(calls) == 1
    assert calls[0].migration == "odbc_to_adbc:redshift"


def test_azure_spark_detected():
    calls = find_all_connectors('let S = AzureSpark.Tables("host") in S')
    assert len(calls) == 1
    assert calls[0].connector_kind == "Spark / HDInsight"
    assert calls[0].migration == "odbc_to_adbc:spark"


def test_apache_spark_detected():
    calls = find_all_connectors('let S = ApacheSpark.Tables("host") in S')
    assert len(calls) == 1
    assert calls[0].connector_kind == "Spark / HDInsight"
    assert calls[0].migration == "odbc_to_adbc:spark"


def test_hdinsight_contents_no_longer_migrating():
    """HDInsight.Contents was the HDFS connector, not Spark. Removed."""
    calls = find_all_connectors('let S = HDInsight.Contents("host") in S')
    assert not any(c.migration == "odbc_to_adbc:spark" for c in calls)


# --------------------------------------------------------------------------- #
# Hive deprecation — new migration family
# --------------------------------------------------------------------------- #

def test_azure_hive_llap_detected_as_deprecation():
    calls = find_all_connectors('let S = AzureHiveLLAP.Database("host","port") in S')
    assert len(calls) == 1
    assert calls[0].connector_kind == "Hive LLAP"
    assert calls[0].migration == "deprecation:hive"
    assert calls[0].is_migrating is True


def test_apache_hive_llap_detected_as_deprecation():
    calls = find_all_connectors('let S = ApacheHiveLLAP.Database("host","port") in S')
    assert len(calls) == 1
    assert calls[0].migration == "deprecation:hive"


def test_hive_unpinned_is_medium_risk_regardless_of_gateway():
    """For deprecation, any use = medium (needs a real migration plan)."""
    calls = find_all_connectors('let S = AzureHiveLLAP.Database("h","p") in S')
    c = calls[0]
    assert c.risk(has_gateway=None) == "medium"
    assert c.risk(has_gateway=True) == "medium"
    assert c.risk(has_gateway=False) == "medium"


def test_hive_pinned_is_high_risk_even_with_gateway():
    """Unlike odbc_to_adbc where gateway rescues pinned, deprecation has no such rescue."""
    calls = find_all_connectors(
        'let S = AzureHiveLLAP.Database("h","p",[Implementation="1.0"]) in S'
    )
    c = calls[0]
    assert c.is_pinned_odbc is True
    assert c.risk(has_gateway=True) == "high"   # gateway does NOT save you
    assert c.risk(has_gateway=False) == "high"


def test_hive_diagnosis_recommends_target_connector():
    from pq_adbc_advisor.troubleshoot import diagnose_connector_call
    d = diagnose_connector_call(
        is_pinned_odbc=False, is_pinned_adbc=False, is_migrating=True,
        custom_dsn=False, has_gateway=None, migration="deprecation:hive",
    )
    assert d is not None
    assert "deprecated" in d.issue.lower()
    # Fix guidance must point at rewriting to a target, not toggling ADBC
    action_text = " ".join(d.suggested_actions).lower()
    assert "target connector" in action_text or "rewrite" in action_text


# --------------------------------------------------------------------------- #
# Telemetry consent — persistent opt-out + first-run notice
# --------------------------------------------------------------------------- #

@pytest.fixture
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_state, "_LAKEHOUSE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(_state, "_LAKEHOUSE_OPT_OUT", str(tmp_path / "opt_out.json"))
    monkeypatch.setattr(_state, "_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(_state, "_home_fallback_path", lambda: str(tmp_path / "home" / "state.json"))
    # Reset the process-scoped first-run-notice flag
    if hasattr(_telemetry._maybe_print_first_run_notice, "_printed"):
        delattr(_telemetry._maybe_print_first_run_notice, "_printed")
    yield


def test_disable_telemetry_persists_across_restarts(_isolated_state):
    """After disable_telemetry(), a fresh is_telemetry_opted_out() sees the flag.

    v0.3.5: opt-out lives in its own file (not the baseline), so we check
    the public API rather than the baseline blob directly.
    """
    assert _state.is_telemetry_opted_out() is False
    _telemetry.disable_telemetry()
    assert _state.is_telemetry_opted_out() is True
    # Simulate a kernel restart by re-checking through the public API.
    assert _state.is_telemetry_opted_out() is True
    # And the baseline is NOT polluted by opt-out data (Bug 1 fix).
    assert _state.load_state() is None or "telemetry_opt_out" not in _state.load_state()


def test_enable_telemetry_clears_opt_out(_isolated_state):
    _telemetry.disable_telemetry()
    assert _state.is_telemetry_opted_out() is True
    _telemetry.enable_telemetry()
    assert _state.is_telemetry_opted_out() is False


def test_telemetry_status_reflects_opt_out(_isolated_state, monkeypatch):
    monkeypatch.setattr(
        _telemetry, "BAKED_IN_CONNECTION_STRING",
        "InstrumentationKey=abc12345-1111-2222-3333-444455556666;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/;",
    )
    _telemetry.disable_telemetry()
    status = _telemetry.telemetry_status()
    assert status["persistent_opt_out"] is True
    assert status["effectively_enabled"] is False


def test_telemetry_blocked_by_persistent_opt_out(_isolated_state, monkeypatch):
    """emit_scan_summary must NOT fire the network POST when opted out."""
    monkeypatch.setattr(
        _telemetry, "BAKED_IN_CONNECTION_STRING",
        "InstrumentationKey=abc12345-1111-2222-3333-444455556666;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/;",
    )
    captured = []
    monkeypatch.setattr(_telemetry, "_post", lambda name, props: captured.append((name, props)))

    _telemetry.disable_telemetry()

    from pq_adbc_advisor.report import ImpactReport, ImpactedArtifact
    r = ImpactReport(workspace_id="ws", scope="workspace")
    hits = find_all_connectors('let S = Snowflake.Databases("h","w") in S')
    r.add(ImpactedArtifact(workspace_id="ws", item_id="m", item_name="M",
                           item_type="SemanticModel", has_gateway=False, hits=hits))
    _telemetry.emit_scan_summary(r, enabled=True)
    assert captured == []


def test_first_run_notice_prints_once_per_process(_isolated_state, monkeypatch, capsys):
    monkeypatch.setattr(
        _telemetry, "BAKED_IN_CONNECTION_STRING",
        "InstrumentationKey=abc12345-1111-2222-3333-444455556666;"
        "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/;",
    )
    monkeypatch.setattr(_telemetry, "_post", lambda *a, **k: None)

    from pq_adbc_advisor.report import ImpactReport, ImpactedArtifact
    def make_report():
        r = ImpactReport(workspace_id="ws", scope="workspace")
        hits = find_all_connectors('let S = Snowflake.Databases("h","w") in S')
        r.add(ImpactedArtifact(workspace_id="ws", item_id="m", item_name="M",
                               item_type="SemanticModel", has_gateway=False, hits=hits))
        return r

    _telemetry.emit_scan_summary(make_report(), enabled=True)
    out1 = capsys.readouterr().out
    assert "anonymous telemetry" in out1.lower()
    assert "disable_telemetry()" in out1

    # Second call must NOT reprint (would be spammy)
    _telemetry.emit_scan_summary(make_report(), enabled=True)
    out2 = capsys.readouterr().out
    assert "anonymous telemetry" not in out2.lower()


def test_public_api_exports_opt_out_functions():
    """The customer-facing API surface must expose the opt-out functions."""
    import pq_adbc_advisor
    assert hasattr(pq_adbc_advisor, "disable_telemetry")
    assert hasattr(pq_adbc_advisor, "enable_telemetry")
    assert hasattr(pq_adbc_advisor, "telemetry_status")
    assert "disable_telemetry" in pq_adbc_advisor.__all__
    assert "enable_telemetry" in pq_adbc_advisor.__all__
