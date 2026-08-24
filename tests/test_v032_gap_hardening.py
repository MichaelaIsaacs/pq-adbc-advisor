"""Regression tests for v0.3.2 — the 5-gap hardening pass.

Gap 1: Fabric runtime paths (sempy + notebookutils) work correctly in
       Fabric because we've locked them under mock harnesses.
Gap 2: HTML render survives 1000+ connector calls at reasonable byte
       count and wall time (stress harness).
Gap 3: fabric_portal_url is cloud-aware (GCC / GCC-High / DoD / China)
       and renders "My Workspace" URLs correctly.
Gap 4: fabric_api._request_with_retry backs off on 429 / 503, honors
       Retry-After, and gives up after the cap.
Gap 5: A per-item 401 / 403 on one artifact is labeled
       ``permission_denied`` and does not crash the scan.
"""

from __future__ import annotations

import os
import sys
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from pq_adbc_advisor.constants import (
    FABRIC_PORTAL_BASE,
    FABRIC_PORTAL_BASES,
    fabric_portal_url,
)
from pq_adbc_advisor.mcode import ConnectorCall
from pq_adbc_advisor.report import ImpactReport, ImpactedArtifact


def _mk_hit(**overrides) -> ConnectorCall:
    defaults = dict(
        connector_kind="Snowflake",
        m_function="Snowflake.Databases",
        migration="odbc_to_adbc:snowflake",
        implementation="1.0",
        endpoint_hint="acct.snowflakecomputing.com",
        excerpt="excerpt",
        custom_dsn=False,
    )
    defaults.update(overrides)
    return ConnectorCall(**defaults)


# --------------------------------------------------------------------------- #
# Gap 3: sovereign cloud + My Workspace URLs
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cloud,expected", [
    ("commercial", "https://app.fabric.microsoft.com/groups/ws/datasets/ds"),
    ("gcc",       "https://app.powerbigov.us/groups/ws/datasets/ds"),
    ("gcc-high",  "https://app.high.powerbigov.us/groups/ws/datasets/ds"),
    ("dod",       "https://app.mil.powerbigov.us/groups/ws/datasets/ds"),
    ("china",     "https://app.powerbi.cn/groups/ws/datasets/ds"),
])
def test_portal_url_each_sovereign_cloud(cloud, expected):
    assert fabric_portal_url("ws", "ds", "SemanticModel", cloud=cloud) == expected


def test_portal_url_unknown_cloud_falls_back_to_commercial():
    url = fabric_portal_url("ws", "ds", "SemanticModel", cloud="mars")
    assert url.startswith(FABRIC_PORTAL_BASE)


def test_portal_url_env_full_override_wins_over_cloud_default():
    env = {"PQ_ADBC_ADVISOR_PORTAL_BASE": "https://preview.fabric.example.com"}
    with patch.dict(os.environ, env, clear=False):
        # cloud= is None, so env override applies.
        url = fabric_portal_url("ws", "ds", "SemanticModel")
        assert url == "https://preview.fabric.example.com/groups/ws/datasets/ds"


def test_portal_url_explicit_cloud_beats_env():
    # Explicit cloud= wins over PQ_ADBC_ADVISOR_PORTAL_BASE.
    env = {"PQ_ADBC_ADVISOR_PORTAL_BASE": "https://preview.fabric.example.com"}
    with patch.dict(os.environ, env, clear=False):
        url = fabric_portal_url("ws", "ds", "SemanticModel", cloud="gcc-high")
        assert url == "https://app.high.powerbigov.us/groups/ws/datasets/ds"


def test_portal_url_env_cloud_key_picked_up_when_arg_missing():
    env = {
        "PQ_ADBC_ADVISOR_CLOUD": "gcc-high",
        # Ensure the full-URL override isn't set for this case.
    }
    # patch.dict merges, so pop the full override defensively:
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("PQ_ADBC_ADVISOR_PORTAL_BASE", None)
        url = fabric_portal_url("ws", "ds", "SemanticModel")
        assert url == "https://app.high.powerbigov.us/groups/ws/datasets/ds"


def test_portal_url_my_workspace_uses_me_segment():
    url = fabric_portal_url("ignored-ws", "ds-42", "SemanticModel", is_personal=True)
    assert url == "https://app.fabric.microsoft.com/me/datasets/ds-42"


def test_portal_url_my_workspace_ignores_workspace_id():
    with_ws = fabric_portal_url("ws", "ds-42", "SemanticModel", is_personal=True)
    no_ws   = fabric_portal_url("",   "ds-42", "SemanticModel", is_personal=True)
    assert with_ws == no_ws


def test_portal_url_my_workspace_unknown_type_falls_back_to_list():
    url = fabric_portal_url("ignored", "ignored", "UnknownType", is_personal=True)
    assert url == "https://app.fabric.microsoft.com/me/list"


def test_portal_url_my_workspace_missing_item_id_returns_none():
    assert fabric_portal_url("ws", "", "SemanticModel", is_personal=True) is None


def test_impacted_artifact_carries_cloud_and_personal_through():
    a = ImpactedArtifact(
        workspace_id="ws", item_id="ds", item_name="M",
        item_type="SemanticModel", hits=[],
        cloud="gcc-high", is_personal=False,
    )
    assert a.fabric_portal_url() == "https://app.high.powerbigov.us/groups/ws/datasets/ds"

    b = ImpactedArtifact(
        workspace_id="ws", item_id="ds", item_name="M",
        item_type="SemanticModel", hits=[],
        is_personal=True,
    )
    assert b.fabric_portal_url() == "https://app.fabric.microsoft.com/me/datasets/ds"


def test_portal_bases_dict_has_five_clouds():
    # If someone adds a cloud, the parametrized test above must cover it too.
    assert set(FABRIC_PORTAL_BASES) == {"commercial", "gcc", "gcc-high", "dod", "china"}


# --------------------------------------------------------------------------- #
# Gap 4: 429 / 503 retry + Retry-After
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, body: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body or {}
    def json(self):
        return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_request_with_retry_honors_retry_after(monkeypatch):
    from pq_adbc_advisor import fabric_api

    calls = {"n": 0}
    sleep_log: list[float] = []

    def fake_request(method, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(429, headers={"Retry-After": "0.01"})
        return _FakeResponse(200, body={"ok": True})

    monkeypatch.setattr(fabric_api.requests, "request", fake_request)
    monkeypatch.setattr(fabric_api.time, "sleep", lambda s: sleep_log.append(s))

    r = fabric_api._request_with_retry("GET", "https://x")
    assert r.status_code == 200
    assert calls["n"] == 2
    # We slept exactly once with the Retry-After value.
    assert sleep_log == [pytest.approx(0.01)]


def test_request_with_retry_exponential_when_no_retry_after(monkeypatch):
    from pq_adbc_advisor import fabric_api

    responses = [
        _FakeResponse(503),
        _FakeResponse(503),
        _FakeResponse(200, body={"ok": True}),
    ]
    def fake_request(method, url, **kw):
        return responses.pop(0)

    monkeypatch.setattr(fabric_api.requests, "request", fake_request)
    slept: list[float] = []
    monkeypatch.setattr(fabric_api.time, "sleep", lambda s: slept.append(s))
    # Force jitter to deterministic
    monkeypatch.setattr(fabric_api.random, "uniform", lambda lo, hi: hi)

    r = fabric_api._request_with_retry("GET", "https://x")
    assert r.status_code == 200
    assert len(slept) == 2  # two backoffs between three attempts
    # Exponential: second sleep should be >= first sleep.
    assert slept[1] >= slept[0]


def test_request_with_retry_gives_up_after_cap(monkeypatch):
    from pq_adbc_advisor import fabric_api

    def fake_request(method, url, **kw):
        return _FakeResponse(429, headers={"Retry-After": "0"})
    monkeypatch.setattr(fabric_api.requests, "request", fake_request)
    monkeypatch.setattr(fabric_api.time, "sleep", lambda s: None)

    r = fabric_api._request_with_retry("GET", "https://x", max_attempts=3)
    # We return the last 429 rather than raising or looping forever.
    assert r.status_code == 429


def test_request_with_retry_network_error_retries_then_returns_none(monkeypatch):
    from pq_adbc_advisor import fabric_api

    def fake_request(method, url, **kw):
        raise fabric_api.requests.RequestException("boom")

    monkeypatch.setattr(fabric_api.requests, "request", fake_request)
    monkeypatch.setattr(fabric_api.time, "sleep", lambda s: None)

    r = fabric_api._request_with_retry("GET", "https://x", max_attempts=2)
    assert r is None


# --------------------------------------------------------------------------- #
# Gap 5: per-item permission_denied
# --------------------------------------------------------------------------- #

def test_get_item_definition_raises_permission_error_on_403(monkeypatch):
    from pq_adbc_advisor import fabric_api

    def fake_retry(method, url, **kw):
        return _FakeResponse(403)
    monkeypatch.setattr(fabric_api, "_request_with_retry", fake_retry)

    with pytest.raises(PermissionError):
        fabric_api.get_item_definition("ws", "item", "token", item_type="SemanticModel")


def test_get_item_definition_raises_permission_error_on_401(monkeypatch):
    from pq_adbc_advisor import fabric_api

    def fake_retry(method, url, **kw):
        return _FakeResponse(401)
    monkeypatch.setattr(fabric_api, "_request_with_retry", fake_retry)

    with pytest.raises(PermissionError):
        fabric_api.get_item_definition("ws", "item", "token", item_type="Dataflow")


def test_get_item_definition_returns_none_on_other_4xx(monkeypatch):
    from pq_adbc_advisor import fabric_api

    def fake_retry(method, url, **kw):
        return _FakeResponse(404)
    monkeypatch.setattr(fabric_api, "_request_with_retry", fake_retry)

    assert fabric_api.get_item_definition("ws", "item", "token") is None


def test_discovery_fetch_records_permission_denied(monkeypatch):
    from pq_adbc_advisor import discovery, fabric_api

    def raise_perm(*a, **kw):
        raise PermissionError("403")
    monkeypatch.setattr(fabric_api, "get_item_definition", raise_perm)

    result = discovery._fetch_definition_and_scan(
        "ws", "tok",
        {"id": "ds-1", "displayName": "Locked", "type": "SemanticModel"},
        use_sempy=False,
    )
    assert result["skip_reason"] == "permission_denied"
    assert result["calls"] == []


def test_discovery_fetch_permission_denied_on_pipeline(monkeypatch):
    from pq_adbc_advisor import discovery, fabric_api

    def raise_perm(*a, **kw):
        raise PermissionError("403")
    monkeypatch.setattr(fabric_api, "get_item_definition", raise_perm)

    result = discovery._fetch_definition_and_scan(
        "ws", "tok",
        {"id": "p-1", "displayName": "LockedPipeline", "type": "DataPipeline"},
        use_sempy=False,
    )
    assert result["skip_reason"] == "permission_denied"


# --------------------------------------------------------------------------- #
# Gap 1: Fabric runtime paths — sempy + notebookutils, mocked
# --------------------------------------------------------------------------- #

def test_sempy_available_true_when_module_present(monkeypatch):
    from pq_adbc_advisor import sempy_path

    # Inject a fake sempy.fabric so the import inside sempy_available succeeds.
    fake_sempy = types.ModuleType("sempy")
    fake_fabric = types.ModuleType("sempy.fabric")
    fake_sempy.fabric = fake_fabric
    monkeypatch.setitem(sys.modules, "sempy", fake_sempy)
    monkeypatch.setitem(sys.modules, "sempy.fabric", fake_fabric)

    assert sempy_path.sempy_available() is True


def test_sempy_available_false_when_module_missing(monkeypatch):
    from pq_adbc_advisor import sempy_path

    # Ensure the module is not importable.
    monkeypatch.setitem(sys.modules, "sempy", None)
    assert sempy_path.sempy_available() is False


def test_sempy_extract_uses_partitions_frame(monkeypatch):
    from pq_adbc_advisor import sempy_path

    import pandas as pd  # test-time dep already used by sempy_path indirectly

    fake_sempy = types.ModuleType("sempy")
    fake_fabric = types.ModuleType("sempy.fabric")

    parts = pd.DataFrame([
        {"Table Name": "Sales", "Partition Name": "P1",
         "Source Type": "M", "Source Expression": 'let s = Snowflake.Databases("a","b") in s'},
        {"Table Name": "Empty", "Partition Name": "P2",
         "Source Type": "Calculated", "Source Expression": "1+1"},
    ])
    exprs = pd.DataFrame([{"Name": "SharedExpr", "Expression": 'Sql.Database("srv","db")'}])
    fake_fabric.list_partitions = lambda dataset, workspace: parts
    fake_fabric.list_expressions = lambda dataset, workspace: exprs
    fake_sempy.fabric = fake_fabric
    monkeypatch.setitem(sys.modules, "sempy", fake_sempy)
    monkeypatch.setitem(sys.modules, "sempy.fabric", fake_fabric)

    got = sempy_path.extract_semantic_model_expressions_via_sempy("ws", "ds", "Model")
    assert got is not None
    # Must skip the "Calculated" partition, keep the M one, and include the shared expression.
    assert len(got) == 2
    names = [g["name"] for g in got]
    assert any("Sales" in n for n in names)
    assert any(n.startswith("shared:") for n in names)


def test_sempy_extract_returns_none_when_import_fails(monkeypatch):
    from pq_adbc_advisor import sempy_path

    monkeypatch.setitem(sys.modules, "sempy", None)
    assert sempy_path.extract_semantic_model_expressions_via_sempy("ws", "ds") is None


def test_get_token_via_mocked_notebookutils(monkeypatch):
    """Simulate running inside a Fabric notebook — notebookutils resolves."""
    from pq_adbc_advisor import fabric_api

    fake_creds = MagicMock()
    fake_creds.getToken = MagicMock(return_value="fake-token-abc")
    fake_notebookutils = types.ModuleType("notebookutils")
    fake_notebookutils.credentials = fake_creds
    monkeypatch.setitem(sys.modules, "notebookutils", fake_notebookutils)

    # Make sure the SP fast-path does not intercept.
    monkeypatch.setattr(
        fabric_api, "auth" if hasattr(fabric_api, "auth") else "get_token",
        fabric_api.auth if hasattr(fabric_api, "auth") else fabric_api.get_token,
        raising=False,
    )
    # Force SP path off:
    from pq_adbc_advisor import auth as auth_mod
    monkeypatch.setattr(auth_mod, "service_principal_env_set", lambda: False)

    token = fabric_api.get_token()
    assert token == "fake-token-abc"
    fake_creds.getToken.assert_called_once_with("pbi")


def test_get_token_raises_when_notebookutils_missing(monkeypatch):
    from pq_adbc_advisor import fabric_api
    from pq_adbc_advisor import auth as auth_mod

    monkeypatch.setattr(auth_mod, "service_principal_env_set", lambda: False)
    monkeypatch.setitem(sys.modules, "notebookutils", None)

    with pytest.raises(RuntimeError) as exc:
        fabric_api.get_token()
    msg = str(exc.value)
    assert "notebookutils" in msg
    assert "PQ_ADBC_ADVISOR_SP" in msg


# --------------------------------------------------------------------------- #
# Gap 2: stress test — 1000 connector calls
# --------------------------------------------------------------------------- #

def _synthetic_report(n_artifacts: int) -> ImpactReport:
    r = ImpactReport(workspace_id="ws-stress")
    r.observed_types = {"SemanticModel": n_artifacts}
    kinds = ["Snowflake", "BigQuery", "Databricks", "Redshift"]
    for i in range(n_artifacts):
        hits = [
            _mk_hit(
                connector_kind=kinds[i % len(kinds)],
                m_function=f"{kinds[i % len(kinds)]}.Databases",
                implementation="1.0" if i % 3 == 0 else None,
            )
            for _ in range(2)  # two calls per artifact
        ]
        r.add(ImpactedArtifact(
            workspace_id="ws-stress",
            item_id=f"ds-{i:04d}",
            item_name=f"Model{i:04d}",
            item_type="SemanticModel",
            has_gateway=(i % 2 == 0),
            hits=hits,
        ))
    return r


def test_stress_render_1000_artifacts_within_budget(tmp_path):
    """1000 artifacts × 2 calls = 2000 connector calls.

    v0.3.0 pagination caps the DOM at 400 rows precisely so a workspace
    of this size doesn't produce a multi-MB HTML that hangs the kernel.
    This test verifies that safety net still works.

    Budget (warn-fence, not a benchmark):
      * wall time < 10s
      * HTML file < 3 MB (pagination is doing its job)
      * pagination "and N more" hint present
      * filter toolbar rendered exactly once
      * risk-filter attribute present on rows
    """
    r = _synthetic_report(1000)
    out = tmp_path / "stress.html"

    t0 = time.perf_counter()
    r.to_html(str(out))
    elapsed = time.perf_counter() - t0

    size_mb = out.stat().st_size / (1024 * 1024)
    html = out.read_text(encoding="utf-8")
    row_count = html.count('class="pqa-connection"')

    # Rows exist and are capped by pagination.
    assert row_count > 0, "expected connection rows to render"
    assert row_count <= 500, f"pagination cap should hold; rendered {row_count}"
    # Pagination hint fires when input > cap.
    assert "more in this group" in html or "more" in html, \
        "pagination hint should appear on 1000-artifact input"
    # Structural surfaces still present.
    assert 'data-risk-filter="' in html
    assert html.count('class="pqa-filter-toolbar"') == 1
    assert "<script" in html

    # Perf fences.
    assert elapsed < 10.0, f"render took {elapsed:.2f}s; regression?"
    assert size_mb < 3.0, f"HTML is {size_mb:.2f} MB; pagination regression?"

    print(f"[stress] 1000 artifacts → {row_count} rows, "
          f"{elapsed*1000:.0f} ms, {size_mb:.2f} MB, {len(html):,} bytes")


def test_stress_render_100_artifacts_baseline(tmp_path):
    """Small-scale baseline so the perf regression is visible."""
    r = _synthetic_report(100)
    out = tmp_path / "baseline.html"

    t0 = time.perf_counter()
    r.to_html(str(out))
    elapsed = time.perf_counter() - t0

    row_count = out.read_text(encoding="utf-8").count('class="pqa-connection"')
    assert row_count > 0
    assert elapsed < 3.0
    print(f"[baseline] 100 artifacts → {row_count} rows, "
          f"{elapsed*1000:.0f} ms, {out.stat().st_size:,} bytes")
