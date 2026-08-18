"""Report object smoke tests - no Fabric calls."""

from pq_adbc_advisor.mcode import find_hits
from pq_adbc_advisor.report import ImpactedArtifact, ImpactReport


def test_impact_report_dataframe_and_summary(tmp_path):
    hits = find_hits(
        'let Source = Snowflake.Databases("h","w",[Implementation="1.0"]) in Source'
    )
    artifact = ImpactedArtifact(
        workspace_id="ws-1",
        item_id="ds-1",
        item_name="Sales",
        item_type="SemanticModel",
        has_gateway=False,
        hits=hits,
    )
    report = ImpactReport(workspace_id="ws-1")
    report.add(artifact)

    df = report.to_dataframe()
    assert len(df) == 1
    assert df.iloc[0]["risk"] == "high"

    summary = report.summary()
    assert summary["artifact_count"] == 1
    assert summary["connector_calls_pinned_to_odbc"] == 1
    assert summary["counts_by_risk"]["high"] == 1

    html_path = tmp_path / "impact.html"
    report.to_html(str(html_path))
    assert html_path.exists()
    assert "PQ Connector Inventory" in html_path.read_text()


def test_worst_risk_prefers_high():
    m = (
        'let '
        'A = Snowflake.Databases("h","w",[Implementation="1.0"]), '
        'B = Snowflake.Databases("h","w",[Implementation="2.0"]) '
        'in A'
    )
    hits = find_hits(m)
    artifact = ImpactedArtifact(
        workspace_id="w",
        item_id="i",
        item_name="mixed",
        item_type="SemanticModel",
        has_gateway=False,
        hits=hits,
    )
    assert artifact.worst_risk == "high"
