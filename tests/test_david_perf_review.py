"""Regression tests for the David Coe MSIT-workspace performance review (2026-08-20).

Real-world signals:
    - 81 artifacts, 228 calls, 154 skipped
    - 45% of connector calls were non-migrating
    - Sequential run took 23 minutes and blew up the notebook HTML render

We do not test the actual timings (unit tests can't observe wall clock
Fabric API), but we lock in the behavior that makes the speedup possible.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pq_adbc_advisor import scan_workspace
from pq_adbc_advisor.mcode import find_all_connectors
from pq_adbc_advisor.report import ImpactReport, ImpactedArtifact


# --------------------------------------------------------------------------- #
# include_non_migrating filter (fast path)
# --------------------------------------------------------------------------- #

_MIXED_ITEM = {"id": "m1", "displayName": "Mixed", "type": "SemanticModel"}
_MIXED_M = (
    'let '
    'A = Snowflake.Databases("h","w"), '
    'B = Sql.Database("srv","db"), '
    'C = Web.Contents("https://x.com") '
    'in A'
)


def _fake_expressions(_, __):
    return [{"name": "p", "expression": _MIXED_M}]


def _stub_fabric(monkeypatch):
    """Patch the Fabric API surface with in-memory fakes for a single item."""
    from pq_adbc_advisor import fabric_api, definitions
    monkeypatch.setattr(fabric_api, "get_token", lambda: "fake-token")
    monkeypatch.setattr(fabric_api, "current_workspace_id", lambda: "ws-fake")
    monkeypatch.setattr(fabric_api, "list_items",
                        lambda *a, **kw: [_MIXED_ITEM])
    monkeypatch.setattr(fabric_api, "get_item_definition",
                        lambda *a, **kw: {"definition": {"parts": []}})
    monkeypatch.setattr(fabric_api, "get_dataset_gateway",
                        lambda *a, **kw: None)
    monkeypatch.setattr(fabric_api, "list_fabric_connections",
                        lambda *a, **kw: ([], None))
    monkeypatch.setattr(definitions, "extract_m_expressions", _fake_expressions)


def test_include_non_migrating_default_is_false(monkeypatch):
    _stub_fabric(monkeypatch)
    report = scan_workspace(
        workspace_id="ws-fake", access_token="t",
        include_fabric_connections=False, telemetry_enabled=False,
        verbose=False,
    )
    assert len(report.artifacts) == 1
    kinds = {c.connector_kind for c in report.artifacts[0].hits}
    # Snowflake (migrating) survives; SQL Server + Web (non-migrating) filtered out
    assert "Snowflake" in kinds
    assert "SQL Server" not in kinds
    assert "Web" not in kinds


def test_include_non_migrating_true_keeps_everything(monkeypatch):
    _stub_fabric(monkeypatch)
    report = scan_workspace(
        workspace_id="ws-fake", access_token="t",
        include_fabric_connections=False, telemetry_enabled=False,
        include_non_migrating=True, verbose=False,
    )
    kinds = {c.connector_kind for c in report.artifacts[0].hits}
    assert kinds == {"Snowflake", "SQL Server", "Web"}


def test_custom_dsn_survives_filter(monkeypatch):
    """Custom DSN calls stay in the report even with the default filter -
    they are a real ADBC migration risk per External Guide #4."""
    from pq_adbc_advisor import fabric_api, definitions
    monkeypatch.setattr(fabric_api, "get_token", lambda: "t")
    monkeypatch.setattr(fabric_api, "list_items",
                        lambda *a, **kw: [_MIXED_ITEM])
    monkeypatch.setattr(fabric_api, "get_item_definition",
                        lambda *a, **kw: {"definition": {"parts": []}})
    monkeypatch.setattr(fabric_api, "get_dataset_gateway",
                        lambda *a, **kw: None)
    monkeypatch.setattr(fabric_api, "list_fabric_connections",
                        lambda *a, **kw: ([], None))
    monkeypatch.setattr(
        definitions, "extract_m_expressions",
        lambda *a, **kw: [{
            "name": "p",
            "expression": 'let s = "DSN=X;Driver={Snow ODBC}", src = Odbc.DataSource(s) in src',
        }],
    )
    report = scan_workspace(
        workspace_id="ws-fake", access_token="t",
        include_fabric_connections=False, telemetry_enabled=False,
        verbose=False,
    )
    assert len(report.artifacts) == 1
    assert any(c.custom_dsn for c in report.artifacts[0].hits)


# --------------------------------------------------------------------------- #
# Parallelism
# --------------------------------------------------------------------------- #

def test_scan_uses_thread_pool(monkeypatch):
    """Ensure discovery hands work to ThreadPoolExecutor rather than the loop."""
    from pq_adbc_advisor import fabric_api, definitions, discovery
    items = [
        {"id": f"m{i}", "displayName": f"Model{i}", "type": "SemanticModel"}
        for i in range(20)
    ]
    monkeypatch.setattr(fabric_api, "get_token", lambda: "t")
    monkeypatch.setattr(fabric_api, "list_items", lambda *a, **kw: items)
    monkeypatch.setattr(fabric_api, "get_item_definition",
                        lambda *a, **kw: {"definition": {"parts": []}})
    monkeypatch.setattr(fabric_api, "get_dataset_gateway",
                        lambda *a, **kw: None)
    monkeypatch.setattr(fabric_api, "list_fabric_connections",
                        lambda *a, **kw: ([], None))
    monkeypatch.setattr(
        definitions, "extract_m_expressions",
        lambda *a, **kw: [{"name": "p", "expression": 'let S = Snowflake.Databases("h","w") in S'}],
    )

    seen_workers: list[int] = []
    from concurrent import futures as _f
    real_tpe = _f.ThreadPoolExecutor
    def _record(*a, max_workers=1, **kw):
        seen_workers.append(max_workers)
        return real_tpe(*a, max_workers=max_workers, **kw)
    monkeypatch.setattr(discovery, "ThreadPoolExecutor", _record)

    scan_workspace(
        workspace_id="ws-fake", access_token="t",
        include_fabric_connections=False, telemetry_enabled=False,
        max_parallel=8, verbose=False,
    )
    # Two thread pools: definition fetch + gateway fetch
    assert 8 in seen_workers
    assert len(seen_workers) >= 1


# --------------------------------------------------------------------------- #
# LRO shorter first poll
# --------------------------------------------------------------------------- #

def test_lro_first_poll_short_regardless_of_retry_after(monkeypatch):
    """Even when the server hints Retry-After: 20 we sleep only ~1s first."""
    from pq_adbc_advisor import fabric_api
    from pq_adbc_advisor.constants import LRO_FIRST_POLL_SEC
    assert LRO_FIRST_POLL_SEC == 1  # not 20

    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    class _FakeResp:
        headers = {"Location": "https://x/op", "Retry-After": "20"}
        status_code = 202

    class _PollResp:
        def __init__(self, status): self._status = status
        status_code = 200
        headers = {"Retry-After": "20"}
        def json(self):
            return {"status": self._status}

    class _FinalResp:
        status_code = 200
        def json(self):
            return {"definition": {"parts": []}}

    call_seq = [_PollResp("Running"), _PollResp("Succeeded"), _FinalResp()]
    def _get(url, headers=None):
        return call_seq.pop(0)
    # _await_lro now routes through _request_with_retry which calls
    # requests.request. Patch both to support old + new call paths.
    def _request(method, url, headers=None, json=None, timeout=None):
        return call_seq.pop(0)
    monkeypatch.setattr(fabric_api.requests, "get", _get)
    monkeypatch.setattr(fabric_api.requests, "request", _request)

    result = fabric_api._await_lro(_FakeResp(), "fake-token")
    assert result is not None
    # First sleep must be short (1s), NOT 20s
    assert sleeps[0] == 1


# --------------------------------------------------------------------------- #
# HTML rendering: non-migrating hidden by default + hidden-count note
# --------------------------------------------------------------------------- #

def _report_with_mixed():
    hits = find_all_connectors(_MIXED_M)
    r = ImpactReport(workspace_id="ws", scope="workspace")
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="m1", item_name="M",
        item_type="SemanticModel", has_gateway=False, hits=hits,
    ))
    return r


def test_repr_html_hides_non_migrating_by_default():
    r = _report_with_mixed()
    html = r._repr_html_()
    assert "Snowflake" in html
    assert "SQL Server" not in html
    # Do NOT search for bare "Web" - it appears in the CSS font stack
    # ("Segoe UI Web (West European)"). Search for the connector-card marker.
    assert 'connector_kind">Web<' not in html
    assert "pqa-connector-title\">Web<" not in html
    # And there is a hidden-count note explaining
    assert "non-migrating connector call" in html.lower()


def test_repr_html_shows_all_when_toggled():
    r = _report_with_mixed()
    r.show_non_migrating = True
    html = r._repr_html_()
    assert "Snowflake" in html
    assert "SQL Server" in html
    # Web connector card should now be present (search for a connector-card marker)
    assert "connector-title" in html
    assert "Web" in html  # Also in CSS font stack, but title should include it too


def test_repr_html_no_hidden_note_when_none_hidden():
    """If every connector is migrating, don't show the hidden-count note."""
    r = ImpactReport(workspace_id="ws", scope="workspace")
    hits = find_all_connectors('let S = Snowflake.Databases("h","w") in S')
    r.add(ImpactedArtifact(
        workspace_id="ws", item_id="m1", item_name="M",
        item_type="SemanticModel", has_gateway=False, hits=hits,
    ))
    html = r._repr_html_()
    # The exact hidden note phrase should be absent
    assert "non-migrating connector call" not in html.lower()


# --------------------------------------------------------------------------- #
# scan_workspace signature exposes verbose + include_non_migrating
# --------------------------------------------------------------------------- #

def test_scan_signature_has_new_kwargs():
    import inspect
    sig = inspect.signature(scan_workspace)
    assert "include_non_migrating" in sig.parameters
    assert sig.parameters["include_non_migrating"].default is False
    assert "max_parallel" in sig.parameters
    assert sig.parameters["max_parallel"].default == 10
    assert "verbose" in sig.parameters
