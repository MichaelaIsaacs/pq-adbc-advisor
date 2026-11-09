"""Regression tests for v0.3.3 — the deep bug bash pass.

Approved scope (8 issues, 4 high + 4 medium; low deferred):

  #1 Conditional M expressions with dynamic connector selection
     (``if Env="prod" then Odbc.Query(...) else Sql.Database(...)``) must
     surface BOTH branches so a migration plan doesn't miss the ODBC path.
  #2 Dataflow Gen2 refresh-history correlation — the semantic-model
     endpoint returns 4xx for DFG2, so ``get_refresh_history`` silently
     returns []. A router now uses the Job Scheduler API for DFG2.
  #3 Parameterized queries + connection-string secrets are redacted
     before excerpt / endpoint_hint land on ConnectorCall.
  #4 Concurrent execution / shared-state safety — parallel worker returns
     must all land in the ImpactReport without dropping or racing.
  #5 Unicode / non-ASCII names render safely in the HTML output.
  #6 Very long connector strings (>4KB) are truncated so the HTML table
     can never blow up.
  #7 Semantic models with no M (DirectLake-only) are skipped cleanly
     with an informative reason.
  #8 A getDefinition returning 404 mid-scan is labeled
     ``deleted_during_scan`` instead of a scary generic error.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from pq_adbc_advisor import fabric_api
from pq_adbc_advisor.mcode import (
    _ENDPOINT_MAX_LEN,
    _EXCERPT_MAX_LEN,
    find_all_connectors,
    redact_secrets,
)
from pq_adbc_advisor.report import ImpactReport, _escape


# --------------------------------------------------------------------------- #
# #1 Conditional M expressions
# --------------------------------------------------------------------------- #

def test_conditional_m_surfaces_both_branches():
    m = (
        'let\n'
        '  Src = if Environment = "prod" '
        'then Odbc.Query("dsn=foo", "SELECT * FROM t") '
        'else Sql.Database("srv", "db")\n'
        'in Src'
    )
    calls = find_all_connectors(m)
    fns = {c.m_function for c in calls}
    assert "Odbc.Query" in fns, "ODBC branch of the conditional must be flagged"
    assert "Sql.Database" in fns, "Sibling SQL branch must also be surfaced"


def test_nested_conditional_m_finds_deep_branch():
    m = (
        'let\n'
        '  Src = if a then (if b then Snowflake.Databases("acct.snowflake.com") '
        'else Odbc.Query("dsn=x", "select 1")) else Sql.Database("s","d")\n'
        'in Src'
    )
    calls = find_all_connectors(m)
    kinds = {c.connector_kind for c in calls}
    # All three connectors — nested and outer — must be flagged.
    assert "Snowflake" in kinds
    assert {"Generic ODBC", "SQL Server"} <= kinds


# --------------------------------------------------------------------------- #
# #2 Dataflow Gen2 refresh-history correlation
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self.headers: dict = {}
        self._body = body or {}
    def json(self):
        return self._body


def test_dfg2_refresh_history_uses_job_scheduler_endpoint(monkeypatch):
    captured: dict = {}
    def fake_request(method, url, **kw):
        captured["url"] = url
        return _FakeResp(200, body={
            "value": [
                {
                    "id": "job-123",
                    "status": "Completed",
                    "startTimeUtc": "2026-08-24T10:00:00Z",
                    "endTimeUtc": "2026-08-24T10:05:00Z",
                },
                {
                    "id": "job-122",
                    "status": "Failed",
                    "startTimeUtc": "2026-08-24T09:00:00Z",
                    "endTimeUtc": "2026-08-24T09:04:00Z",
                    "failureReason": {"errorCode": "OdbcMissingDriver", "message": "driver not found"},
                },
            ]
        })
    monkeypatch.setattr(fabric_api.requests, "request", fake_request)
    hist = fabric_api.get_dataflow_refresh_history("ws", "df", "tok", top=5)
    assert "jobs/instances" in captured["url"]
    assert "jobType=Refresh" in captured["url"]
    assert hist[0]["requestId"] == "job-123"
    assert hist[0]["status"] == "Completed"
    assert hist[1]["serviceExceptionJson"] == "driver not found"


def test_refresh_history_router_picks_correct_endpoint(monkeypatch):
    seen: list[str] = []
    def fake_request(method, url, **kw):
        seen.append(url)
        return _FakeResp(200, body={"value": []})
    monkeypatch.setattr(fabric_api.requests, "request", fake_request)

    fabric_api.get_refresh_history_for_item("ws", "id", "tok", "SemanticModel")
    fabric_api.get_refresh_history_for_item("ws", "id", "tok", "Dataflow")
    other = fabric_api.get_refresh_history_for_item("ws", "id", "tok", "Notebook")

    assert "datasets/id/refreshes" in seen[0], "SemanticModel must route to PBI datasets API"
    assert "items/id/jobs/instances" in seen[1], "Dataflow must route to Fabric Job Scheduler"
    assert other == [], "Non-refreshable types must return [] rather than a stray call"
    assert len(seen) == 2, "Router must not call any endpoint for uninspectable types"


def test_dfg2_refresh_history_survives_4xx(monkeypatch):
    def fake_request(method, url, **kw):
        return _FakeResp(404)
    monkeypatch.setattr(fabric_api.requests, "request", fake_request)
    assert fabric_api.get_dataflow_refresh_history("ws", "df", "tok") == []


# --------------------------------------------------------------------------- #
# #3 Parameterized queries + connection-string secrets
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("secret_key", [
    "password", "PWD", "Token", "secret", "apikey", "AccountKey",
    "SharedAccessSignature", "sas", "Authorization",
])
def test_redact_secrets_scrubs_common_connection_string_keys(secret_key):
    raw = f"driver={{SQL}};server=prod;{secret_key}=SUPER-SECRET-VALUE;db=x"
    out = redact_secrets(raw)
    assert "SUPER-SECRET-VALUE" not in out
    assert "***REDACTED***" in out
    # Non-secret fields survive.
    assert "server=prod" in out
    assert "db=x" in out


def test_redact_secrets_scrubs_jwt_shaped_tokens():
    raw = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefg"
    out = redact_secrets(raw)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "***REDACTED***" in out


def test_redact_secrets_handles_m_shared_parameter_default():
    raw = 'shared ApiKey = "sk-live-1234567890" meta [IsParameterQuery=true, Type="Text"];'
    out = redact_secrets(raw)
    assert "sk-live-1234567890" not in out
    assert "***REDACTED***" in out


def test_find_all_connectors_redacts_secret_in_excerpt():
    m = (
        'let\n'
        '  Src = Odbc.Query("driver={SQL};server=prod;pwd=hunter2;", '
        '"SELECT * FROM Orders")\n'
        'in Src'
    )
    calls = find_all_connectors(m)
    assert calls
    assert "hunter2" not in calls[0].excerpt
    assert "hunter2" not in (calls[0].endpoint_hint or "")


def test_find_all_connectors_redacts_but_keeps_useful_context():
    """Secret redaction MUST NOT hide the M function name or server."""
    m = 'let S = Snowflake.Databases("acct.snowflakecomputing.com", "wh", [Password="LEAKED"]) in S'
    calls = find_all_connectors(m)
    assert calls
    c = calls[0]
    assert c.connector_kind == "Snowflake"
    assert "acct.snowflakecomputing.com" in (c.endpoint_hint or "")
    assert "LEAKED" not in c.excerpt


# --------------------------------------------------------------------------- #
# #4 Concurrent execution / shared-state safety
# --------------------------------------------------------------------------- #

def test_find_all_connectors_is_thread_safe():
    """Scanning the same expression from many threads must be deterministic."""
    m = (
        'let A = Odbc.Query("dsn=x","q"), B = Snowflake.Databases("a"), '
        'C = Sql.Database("s","d") in A'
    )
    baseline = find_all_connectors(m)
    baseline_fns = sorted(c.m_function for c in baseline)

    results: list[list[str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            got = find_all_connectors(m)
            with lock:
                results.append(sorted(c.m_function for c in got))
        except BaseException as e:  # pragma: no cover
            with lock:
                errors.append(e)

    with ThreadPoolExecutor(max_workers=16) as pool:
        for _ in range(64):
            pool.submit(worker)
    assert not errors
    assert all(r == baseline_fns for r in results)


def test_impact_report_survives_concurrent_appends():
    """Discovery collects worker results in the main thread — but a defensive
    test locks in that the report's core mutators (add/record_skipped) don't
    drop entries even when called from many threads simultaneously.
    """
    from pq_adbc_advisor.report import ImpactedArtifact
    report = ImpactReport(workspace_id="ws")
    N = 200

    def add(i: int) -> None:
        report.add(ImpactedArtifact(
            workspace_id="ws", item_id=f"id-{i}", item_name=f"name-{i}",
            item_type="SemanticModel", hits=[],
        ))

    def skip(i: int) -> None:
        report.record_skipped(f"skip-{i}", f"n-{i}", "Notebook", reason="type_not_inspected")

    with ThreadPoolExecutor(max_workers=32) as pool:
        for i in range(N):
            pool.submit(add, i)
            pool.submit(skip, i)

    assert len(report.artifacts) == N
    assert len(report.skipped) == N
    ids = {a.item_id for a in report.artifacts}
    assert len(ids) == N, "No artifacts dropped or duplicated"


# --------------------------------------------------------------------------- #
# #5 Unicode / non-ASCII rendering
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "北京仓库 Snowflake",
    "Ordersübersicht",
    "מודל_סמנטי",
    "café ☕ pipeline",
    "🚀 launch dataset",
])
def test_escape_preserves_unicode_verbatim(name):
    out = _escape(name)
    # HTML entities are only used for &, <, >, ".
    assert name.replace("&", "&amp;") in out or name in out
    # No mojibake or ASCII fallback.
    for ch in name:
        if ch not in ('&', '<', '>', '"'):
            assert ch in out


def test_report_html_writes_utf8_and_declares_charset(tmp_path):
    report = ImpactReport(workspace_id="ws-🚀")
    path = report.to_html(str(tmp_path / "impact.html"), title="报告 Impact")
    with open(path, "rb") as f:
        raw = f.read()
    assert b"charset='utf-8'" in raw or b'charset="utf-8"' in raw
    # UTF-8 bytes for the workspace and title survive the round-trip.
    assert "报告 Impact".encode("utf-8") in raw
    assert "ws-🚀".encode("utf-8") in raw


# --------------------------------------------------------------------------- #
# #6 Very long connector strings (>4KB) get truncated
# --------------------------------------------------------------------------- #

def test_excerpt_capped_when_m_is_pathologically_long():
    """Even with an 8KB query string, the excerpt stays bounded so the HTML
    render can't blow up. The upstream window (~200 chars) already caps it
    tightly; the ``_truncate`` step is defense-in-depth on top of that.
    """
    long_tail = "z" * 8000
    m = f'let S = Odbc.Query("dsn=x", "SELECT * FROM Orders_{long_tail}") in S'
    calls = find_all_connectors(m)
    assert calls
    assert len(calls[0].excerpt) <= _EXCERPT_MAX_LEN + len(" ...(truncated)")


def test_excerpt_truncation_marker_fires_when_window_exceeds_cap(monkeypatch):
    """Directly exercise the ``_truncate`` marker path by shrinking the cap
    so the natural window (~200 chars) crosses it. This locks in that the
    ``(truncated)`` sentinel actually renders when needed.
    """
    from pq_adbc_advisor import mcode
    monkeypatch.setattr(mcode, "_EXCERPT_MAX_LEN", 50)
    long_tail = "z" * 400
    m = f'let S = Odbc.Query("dsn=x", "SELECT * FROM Orders_{long_tail}") in S'
    calls = mcode.find_all_connectors(m)
    assert calls
    assert "(truncated)" in calls[0].excerpt


def test_endpoint_hint_capped_for_gigantic_first_string():
    huge = "a" * 5000
    m = f'let S = Odbc.Query("{huge}", "select 1") in S'
    calls = find_all_connectors(m)
    assert calls
    hint = calls[0].endpoint_hint or ""
    assert len(hint) <= _ENDPOINT_MAX_LEN + len(" ...(truncated)")
    assert "(truncated)" in hint


# --------------------------------------------------------------------------- #
# #7 Semantic models with no M (DirectLake-only) skip cleanly
# --------------------------------------------------------------------------- #

def test_semantic_model_with_no_m_expressions_records_clean_skip():
    """DirectLake semantic models have no M in their definition. The worker
    must return a clean ``definition_parsed_but_no_expressions`` skip, and
    that string must round-trip into skipped_by_reason() without crashing.
    """
    from pq_adbc_advisor import definitions, discovery, sempy_path

    fake_item = {"id": "sm-1", "displayName": "DirectLake Model", "type": "SemanticModel"}

    with patch.object(discovery.fabric_api, "get_item_definition", return_value={"parts": []}), \
         patch.object(definitions, "extract_m_expressions", return_value=[]), \
         patch.object(sempy_path, "extract_semantic_model_expressions_via_sempy", return_value=None):
        result = discovery._fetch_definition_and_scan(
            workspace_id="ws", access_token="tok", item=fake_item, use_sempy=False,
        )

    assert result is not None
    assert result["skip_reason"] == "definition_parsed_but_no_expressions"
    assert result["calls"] == []

    # And the aggregate call path stays clean.
    report = ImpactReport(workspace_id="ws")
    report.record_skipped("sm-1", "DirectLake Model", "SemanticModel", reason=result["skip_reason"])
    counts = report.skipped_by_reason()
    assert counts["definition_parsed_but_no_expressions"] == 1


# --------------------------------------------------------------------------- #
# #8 Deleted-mid-scan artifact (404 on getDefinition)
# --------------------------------------------------------------------------- #

def test_get_item_definition_raises_file_not_found_on_404(monkeypatch):
    def fake_request(method, url, **kw):
        return _FakeResp(404)
    monkeypatch.setattr(fabric_api.requests, "request", fake_request)

    with pytest.raises(FileNotFoundError):
        fabric_api.get_item_definition("ws", "gone", "tok", item_type="SemanticModel")


def test_discovery_worker_labels_deleted_during_scan():
    from pq_adbc_advisor import discovery

    fake_item = {"id": "gone", "displayName": "Ghost", "type": "SemanticModel"}
    with patch.object(
        discovery.fabric_api, "get_item_definition",
        side_effect=FileNotFoundError("HTTP 404"),
    ):
        result = discovery._fetch_definition_and_scan(
            workspace_id="ws", access_token="tok", item=fake_item, use_sempy=False,
        )
    assert result["skip_reason"] == "deleted_during_scan"


def test_discovery_worker_labels_deleted_datapipeline():
    from pq_adbc_advisor import discovery

    fake_item = {"id": "gone-pipe", "displayName": "Ghost Pipeline", "type": "DataPipeline"}
    with patch.object(
        discovery.fabric_api, "get_item_definition",
        side_effect=FileNotFoundError("HTTP 404"),
    ):
        result = discovery._fetch_definition_and_scan(
            workspace_id="ws", access_token="tok", item=fake_item, use_sempy=False,
        )
    assert result["skip_reason"] == "deleted_during_scan"


def test_scan_workspace_labels_404_from_worker_future(monkeypatch):
    """End-to-end: a future that raises FileNotFoundError becomes a
    ``deleted_during_scan`` row in ImpactReport.skipped, not a generic error.
    """
    from pq_adbc_advisor import discovery

    items = [
        {"id": "keep", "displayName": "Live",  "type": "SemanticModel"},
        {"id": "gone", "displayName": "Ghost", "type": "SemanticModel"},
    ]
    monkeypatch.setattr(discovery.fabric_api, "current_workspace_id", lambda: "ws")
    monkeypatch.setattr(discovery.fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(discovery.fabric_api, "list_items", lambda *a, **kw: items)
    monkeypatch.setattr(discovery.fabric_api, "list_fabric_connections", lambda *a, **kw: ([], None))
    monkeypatch.setattr(discovery.sempy_path, "sempy_available", lambda: False)

    def fake_worker(workspace_id, access_token, item, use_sempy):
        if item["id"] == "gone":
            raise FileNotFoundError("deleted mid-scan")
        return {
            "item": item, "calls": [], "skip_reason": "definition_parsed_but_no_expressions",
            "source": "rest", "pipeline_refs": None,
        }
    monkeypatch.setattr(discovery, "_fetch_definition_and_scan", fake_worker)

    report = discovery.scan_workspace(
        workspace_id="ws", access_token="tok",
        include_fabric_connections=False, telemetry_enabled=False, verbose=False,
        max_parallel=2, use_sempy=False,
    )
    reasons = report.skipped_by_reason()
    assert reasons.get("deleted_during_scan") == 1
    assert "error: FileNotFoundError" not in reasons
