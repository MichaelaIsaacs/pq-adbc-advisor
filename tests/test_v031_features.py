"""Regression tests for v0.3.1 (David's Thursday feedback).

Covers:
1. fabric_portal_url helper builds correct URLs per item type.
2. ImpactedArtifact.fabric_portal_url delegates.
3. HTML render hyperlinks the artifact NAME per row with the right href
   (so clicking "HighRiskModel" opens that semantic model in Fabric).
4. HTML render contains the filter toolbar with per-risk counts.
5. HTML render contains the <script> filter block exactly once.
6. HTML render tags each row with data-risk-filter attribute.
7. Explicit tri-state gateway chip appears: via / no / unknown.
8. filtered("needs_review") returns only medium+unknown rows.
9. filtered("will_fail") returns only high rows.
10. filtered("ready") returns only low rows.
11. needs_review()/will_fail()/ready_only() shortcuts.
12. filtered() preserves fabric_connections, observed_types, etc.
13. Empty filter result returns a valid (empty) ImpactReport.
14. filtered() accepts a list of risk levels.
"""

from __future__ import annotations

import re

from pq_adbc_advisor.constants import fabric_portal_url
from pq_adbc_advisor.mcode import ConnectorCall
from pq_adbc_advisor.report import ImpactReport, ImpactedArtifact


def _mk_hit(**overrides) -> ConnectorCall:
    defaults = dict(
        connector_kind="Snowflake",
        m_function="Snowflake.Databases",
        migration="odbc_to_adbc:snowflake",
        implementation=None,
        endpoint_hint="acct.snowflakecomputing.com",
        excerpt="excerpt",
        custom_dsn=False,
    )
    defaults.update(overrides)
    return ConnectorCall(**defaults)


# --------------------------------------------------------------------------- #
# 1-2. Portal URL helper
# --------------------------------------------------------------------------- #

def test_fabric_portal_url_semantic_model():
    url = fabric_portal_url("ws-1", "ds-1", "SemanticModel")
    assert url == "https://app.fabric.microsoft.com/groups/ws-1/datasets/ds-1"


def test_fabric_portal_url_dataflow():
    url = fabric_portal_url("ws-1", "df-1", "Dataflow")
    assert url == "https://app.fabric.microsoft.com/groups/ws-1/dataflows/df-1"


def test_fabric_portal_url_data_pipeline():
    url = fabric_portal_url("ws-1", "p-1", "DataPipeline")
    assert url == "https://app.fabric.microsoft.com/groups/ws-1/pipelines/p-1"


def test_fabric_portal_url_unknown_type_falls_back_to_list():
    url = fabric_portal_url("ws-1", "x", "UnknownType")
    assert url == "https://app.fabric.microsoft.com/groups/ws-1/list"


def test_impacted_artifact_delegates_to_helper():
    a = ImpactedArtifact(workspace_id="ws-42", item_id="ds-7", item_name="M",
                         item_type="SemanticModel", hits=[])
    assert a.fabric_portal_url() == "https://app.fabric.microsoft.com/groups/ws-42/datasets/ds-7"


# --------------------------------------------------------------------------- #
# 3-6. HTML surfaces
# --------------------------------------------------------------------------- #

def _mk_report_one_of_each() -> ImpactReport:
    r = ImpactReport(workspace_id="ws-a")
    r.observed_types = {"SemanticModel": 3}
    # HIGH: pinned ODBC + no gateway
    r.add(ImpactedArtifact(
        workspace_id="ws-a", item_id="ds-fail", item_name="FailModel",
        item_type="SemanticModel", has_gateway=False,
        hits=[_mk_hit(implementation="1.0")],
    ))
    # MEDIUM: pinned ODBC + gateway
    r.add(ImpactedArtifact(
        workspace_id="ws-a", item_id="ds-warn", item_name="WarnModel",
        item_type="SemanticModel", has_gateway=True,
        hits=[_mk_hit(implementation="1.0")],
    ))
    # LOW: not pinned
    r.add(ImpactedArtifact(
        workspace_id="ws-a", item_id="ds-ok", item_name="OkModel",
        item_type="SemanticModel", has_gateway=True,
        hits=[_mk_hit(implementation=None)],
    ))
    return r


def test_html_render_includes_open_link_per_row():
    r = _mk_report_one_of_each()
    html = r._repr_html_()
    # Deep-link href for each item is present.
    assert "app.fabric.microsoft.com/groups/ws-a/datasets/ds-fail" in html
    assert "app.fabric.microsoft.com/groups/ws-a/datasets/ds-warn" in html
    assert "app.fabric.microsoft.com/groups/ws-a/datasets/ds-ok" in html
    # v0.3.1 (revised per David): the artifact NAME itself is the link
    # target — no separate "Open" pill.
    assert 'class="pqa-artifact-link"' in html
    # target=_blank so it doesn't kill the notebook session.
    assert 'target="_blank"' in html
    # rel=noopener/noreferrer for safety.
    assert "noopener noreferrer" in html


def test_html_render_includes_filter_toolbar_with_counts():
    r = _mk_report_one_of_each()
    html = r._repr_html_()
    assert 'class="pqa-filter-toolbar"' in html
    # All four buttons present.
    for label in ["All", "Will fail", "Needs review", "Ready"]:
        assert label in html
    # Per-button counts match our fixture (1 each).
    assert 'data-filter="will_fail">Will fail<span class="pqa-filter-count">(1)</span>' in html
    assert 'data-filter="needs_review">Needs review<span class="pqa-filter-count">(1)</span>' in html
    assert 'data-filter="ready">Ready<span class="pqa-filter-count">(1)</span>' in html


def test_html_render_ships_filter_script_exactly_once():
    r = _mk_report_one_of_each()
    html = r._repr_html_()
    # Exactly one <script> block for the filter.
    assert html.count("<script>") == 1
    # Data attribute is present on each row for the JS to filter on.
    rows = re.findall(r'data-risk-filter="([a-z_]+)"', html)
    assert set(rows) == {"will_fail", "needs_review", "ready"}


def test_html_render_data_attribute_matches_status():
    """Ensures each row is tagged with the correct filter key."""
    r = _mk_report_one_of_each()
    html = r._repr_html_()
    # Find the row containing ds-fail's deep-link and inspect the whole row.
    idx = html.index("ds-fail")
    row_start = html.rfind('<div class="pqa-connection"', 0, idx)
    row_end = html.find('<div class="pqa-connection"', idx + 10)
    if row_end == -1:
        row_end = idx + 4000
    row_block = html[row_start:row_end]
    assert 'data-risk-filter="will_fail"' in row_block


# --------------------------------------------------------------------------- #
# 7. Tri-state gateway chip
# --------------------------------------------------------------------------- #

def test_html_renders_via_gateway_chip():
    r = ImpactReport(workspace_id="ws")
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="1", item_name="X",
        item_type="SemanticModel", has_gateway=True,
        hits=[_mk_hit(implementation="1.0")],
    ))
    assert "via gateway" in r._repr_html_()


def test_html_renders_no_gateway_chip():
    r = ImpactReport(workspace_id="ws")
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="1", item_name="X",
        item_type="SemanticModel", has_gateway=False,
        hits=[_mk_hit(implementation="1.0")],
    ))
    html = r._repr_html_()
    assert "no gateway" in html


def test_html_renders_gateway_unknown_chip():
    """Regression for David's ambiguity — has_gateway=None used to render
    nothing at all, leaving readers to guess the state."""
    r = ImpactReport(workspace_id="ws")
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="1", item_name="X",
        item_type="SemanticModel", has_gateway=None,
        hits=[_mk_hit(implementation="1.0")],
    ))
    html = r._repr_html_()
    assert "gateway: unknown" in html


# --------------------------------------------------------------------------- #
# 8-14. Python filter API
# --------------------------------------------------------------------------- #

def test_filtered_needs_review_returns_only_medium_and_unknown():
    r = _mk_report_one_of_each()
    n = r.needs_review()
    assert len(n.artifacts) == 1
    assert n.artifacts[0].item_id == "ds-warn"


def test_filtered_will_fail_returns_only_high():
    r = _mk_report_one_of_each()
    f = r.will_fail()
    assert len(f.artifacts) == 1
    assert f.artifacts[0].item_id == "ds-fail"


def test_filtered_ready_only_returns_only_low():
    r = _mk_report_one_of_each()
    ok = r.ready_only()
    assert len(ok.artifacts) == 1
    assert ok.artifacts[0].item_id == "ds-ok"


def test_filtered_preserves_context():
    r = _mk_report_one_of_each()
    r.fabric_connections = [{"id": "c1", "displayName": "SF"}]
    r.observed_types = {"SemanticModel": 5, "Report": 2}
    r.used_sempy_path = True
    r.sempy_hits = 3
    r.pipeline_calls = 7
    n = r.needs_review()
    assert n.fabric_connections == r.fabric_connections
    assert n.observed_types == r.observed_types
    assert n.used_sempy_path is True
    assert n.sempy_hits == 3
    assert n.pipeline_calls == 7


def test_filtered_empty_result_is_valid_report():
    r = ImpactReport(workspace_id="ws-empty")
    n = r.needs_review()
    assert isinstance(n, ImpactReport)
    assert n.artifacts == []
    # Still renders without error.
    assert n._repr_html_()


def test_filtered_accepts_list_of_risks():
    r = _mk_report_one_of_each()
    combined = r.filtered(["will_fail", "needs_review"])
    ids = {a.item_id for a in combined.artifacts}
    assert ids == {"ds-fail", "ds-warn"}


def test_filtered_preserves_only_matching_hits_within_artifact():
    """When one artifact has both a HIGH and a LOW hit, filtering to
    will_fail should keep only the HIGH one."""
    hits = [
        _mk_hit(implementation="1.0"),  # HIGH given no gateway
        _mk_hit(implementation=None),   # LOW
    ]
    r = ImpactReport(workspace_id="ws")
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="1", item_name="Mix",
        item_type="SemanticModel", has_gateway=False, hits=hits,
    ))
    f = r.will_fail()
    assert len(f.artifacts) == 1
    assert len(f.artifacts[0].hits) == 1
    assert f.artifacts[0].hits[0].implementation == "1.0"


def test_kpi_needs_review_matches_filter_button_count():
    """Regression: KPI 'Needs review' card must include RISK_UNKNOWN so it
    agrees with the filter button (which buckets MEDIUM+UNKNOWN together).
    Prior bug: KPI counted MEDIUM only → mismatched filter (2) vs KPI (1)."""
    r = ImpactReport(workspace_id="ws")
    # 1 MEDIUM (pinned + gateway), 1 UNKNOWN (pinned + gateway unknown)
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="m", item_name="Med", item_type="SemanticModel",
        has_gateway=True, hits=[_mk_hit(implementation="1.0")],
    ))
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="u", item_name="Unk", item_type="SemanticModel",
        has_gateway=None, hits=[_mk_hit(implementation="1.0")],
    ))
    html = r._repr_html_()
    # KPI card should read "Needs review" with value 2
    kpi_match = re.search(
        r'Needs review.*?class="pqa-kpi-value[^"]*">(\d+)<',
        html, re.DOTALL,
    )
    assert kpi_match, "KPI 'Needs review' card not found"
    assert kpi_match.group(1) == "2", f"KPI shows {kpi_match.group(1)}, expected 2"
    # Filter button count should also be 2
    assert 'Needs review<span class="pqa-filter-count">(2)</span>' in html


def test_artifact_link_has_noopener_noreferrer():
    """Security: artifact-name links open in a new tab with noopener +
    noreferrer so the linked Fabric portal page cannot access
    window.opener."""
    r = ImpactReport(workspace_id="ws")
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="a", item_name="A", item_type="SemanticModel",
        has_gateway=False, hits=[_mk_hit(implementation="1.0")],
    ))
    html = r._repr_html_()
    m = re.search(r'<a[^>]*class="pqa-artifact-link"[^>]*>', html)
    assert m, "no pqa-artifact-link anchor"
    tag = m.group(0)
    assert 'target="_blank"' in tag
    assert 'rel="noopener noreferrer"' in tag


def test_xss_in_item_name_is_escaped():
    """Regression: item names carrying HTML must be escaped, not injected."""
    r = ImpactReport(workspace_id="ws")
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="a", item_type="SemanticModel",
        item_name='<img src=x onerror="alert(1)">',
        has_gateway=False, hits=[_mk_hit(implementation="1.0")],
    ))
    html = r._repr_html_()
    assert '<img src=x onerror' not in html   # unescaped attack
    assert '&lt;img src=x onerror=' in html    # properly escaped


def test_missing_ids_render_plain_text_not_broken_href():
    """Regression: if workspace_id or item_id is missing, the artifact
    name must render as plain text, never as an anchor to
    /groups/None/... which would 404 in the Fabric portal."""
    from pq_adbc_advisor.constants import fabric_portal_url

    # constants: missing IDs return None
    assert fabric_portal_url(None, "iid", "SemanticModel") is None
    assert fabric_portal_url("wid", None, "SemanticModel") is None
    assert fabric_portal_url("", "iid", "SemanticModel") is None
    assert fabric_portal_url("wid", "", "SemanticModel") is None
    # unknown type still deep-links to workspace list when workspace known
    assert fabric_portal_url("wid", "iid", "Frob") == \
        "https://app.fabric.microsoft.com/groups/wid/list"

    # report: missing IDs → no anchor, name is plain <b>
    r = ImpactReport(workspace_id="")  # no workspace
    r.add(ImpactedArtifact(
        workspace_id="", item_id="", item_name="Orphaned Model",
        item_type="SemanticModel", has_gateway=False,
        hits=[_mk_hit(implementation="1.0")],
    ))
    html = r._repr_html_()
    # Must not emit a href with literal None or empty groups segment
    assert "/groups/None/" not in html
    assert "/groups//datasets" not in html
    # Name still appears, just not as an anchor
    assert "Orphaned Model" in html
    # No anchor with pqa-artifact-link class wrapping this name
    import re as _re
    for m in _re.finditer(r'<a[^>]*class="pqa-artifact-link"[^>]*>([^<]*)</a>', html):
        assert "Orphaned Model" not in m.group(1), \
            "missing-ID artifact must not be wrapped in a broken href anchor"
