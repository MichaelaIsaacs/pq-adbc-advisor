"""Regression tests for v0.3.0 features.

Covers:
1. Data Pipeline connection reference extraction.
2. Pipeline refs -> ConnectorCall resolution with connection mapping.
3. Pipeline refs -> ConnectorCall when connection is unresolvable.
4. Legacy inline linkedService type parsing.
5. Nested activities (ForEach / If) discovered recursively.
6. sempy fast path live-runs against a mock sempy.fabric package.
7. sempy hits get recorded on report.
8. Service Principal env detection.
9. SP acquire_token via mocked MSAL.
10. SP raises when env missing.
11. get_token uses SP when env is set.
12. HTML pagination caps per-group rows.
13. HTML pagination caps total rows across groups.
14. Huge workspace (500 calls) render stays under 500KB.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import types

import pytest

from pq_adbc_advisor import (
    auth, discovery, fabric_api, pipeline_scan, report as report_mod, sempy_path,
)
from pq_adbc_advisor.mcode import ConnectorCall


# --------------------------------------------------------------------------- #
# 1-5. Pipeline scan
# --------------------------------------------------------------------------- #

def _pipeline_definition(pipeline_content: dict) -> dict:
    """Wrap pipeline JSON in the Fabric getDefinition envelope."""
    payload = base64.b64encode(json.dumps(pipeline_content).encode()).decode()
    return {
        "definition": {
            "parts": [
                {"path": "pipeline-content.json", "payload": payload,
                 "payloadType": "InlineBase64"},
            ],
        },
    }


def test_pipeline_extract_copy_activity_conn_refs():
    doc = {
        "properties": {
            "activities": [
                {
                    "name": "Copy from Snowflake",
                    "type": "Copy",
                    "typeProperties": {
                        "source": {
                            "type": "SnowflakeSource",
                            "externalReferences": {"connection": "conn-src-abc"},
                        },
                        "sink": {
                            "type": "LakehouseSink",
                            "externalReferences": {"connection": "conn-sink-xyz"},
                        },
                    },
                },
            ],
        },
    }
    refs = pipeline_scan.extract_connection_refs(_pipeline_definition(doc))
    assert len(refs) == 2
    src = next(r for r in refs if r["role"] == "source")
    assert src["connection_id"] == "conn-src-abc"
    assert src["activity_type"] == "Copy"
    sink = next(r for r in refs if r["role"] == "sink")
    assert sink["connection_id"] == "conn-sink-xyz"


def test_pipeline_extract_lookup_and_script_activities():
    doc = {"properties": {"activities": [
        {"name": "L", "type": "Lookup", "typeProperties": {
            "dataset": {"externalReferences": {"connection": "conn-a"}}
        }},
        {"name": "S", "type": "Script", "typeProperties": {
            "linkedService": {"referenceName": "conn-b"}
        }},
    ]}}
    refs = pipeline_scan.extract_connection_refs(_pipeline_definition(doc))
    conn_ids = {r["connection_id"] for r in refs}
    assert conn_ids == {"conn-a", "conn-b"}


def test_pipeline_extract_walks_foreach_and_if():
    doc = {"properties": {"activities": [
        {"name": "outer", "type": "ForEach", "typeProperties": {
            "activities": [
                {"name": "inner-copy", "type": "Copy", "typeProperties": {
                    "source": {"externalReferences": {"connection": "conn-nested"}}
                }}
            ]
        }},
        {"name": "cond", "type": "IfCondition", "typeProperties": {
            "ifTrueActivities": [
                {"name": "true-lookup", "type": "Lookup", "typeProperties": {
                    "dataset": {"externalReferences": {"connection": "conn-true"}}
                }}
            ]
        }},
    ]}}
    refs = pipeline_scan.extract_connection_refs(_pipeline_definition(doc))
    conn_ids = {r["connection_id"] for r in refs}
    assert conn_ids == {"conn-nested", "conn-true"}


def test_pipeline_extract_legacy_inline_linked_service():
    doc = {"properties": {"activities": [
        {"name": "old", "type": "Copy", "typeProperties": {
            "source": {
                "linkedService": {
                    "properties": {
                        "type": "SnowflakeLinkedService",
                        "typeProperties": {"connectionId": "old-conn"},
                    }
                }
            }
        }}
    ]}}
    refs = pipeline_scan.extract_connection_refs(_pipeline_definition(doc))
    assert len(refs) == 1
    assert refs[0]["inline_type"] == "SnowflakeLinkedService"


def test_pipeline_refs_resolve_to_migrating_when_connection_maps_to_snowflake():
    connections = {"conn-1": {
        "id": "conn-1",
        "displayName": "SF Prod",
        "connectionDetails": {"type": "Snowflake.Databases", "path": "acct.snowflakecomputing.com"},
    }}
    refs = [{
        "activity_name": "Copy",
        "activity_type": "Copy",
        "role": "source",
        "connection_id": "conn-1",
        "inline_type": None,
        "excerpt": "{}",
    }]
    calls = pipeline_scan.refs_to_connector_calls(refs, connections)
    assert len(calls) == 1
    c = calls[0]
    assert "Snowflake" in c.connector_kind
    assert c.is_migrating is True
    assert "snowflake" in c.migration


def test_pipeline_refs_resolve_to_unresolved_when_conn_id_unknown():
    refs = [{
        "activity_name": "X", "activity_type": "Copy", "role": "source",
        "connection_id": "conn-unknown-cannot-see",
        "inline_type": None, "excerpt": "{}",
    }]
    calls = pipeline_scan.refs_to_connector_calls(refs, {})
    assert len(calls) == 1
    assert calls[0].connector_kind == "Unresolved connection"
    assert calls[0].is_migrating is False


def test_pipeline_scan_end_to_end_via_discovery(monkeypatch):
    """A DataPipeline in the workspace produces a resolved ConnectorCall
    in the impact report, via the full discovery pipeline."""
    doc = {"properties": {"activities": [
        {"name": "C", "type": "Copy", "typeProperties": {
            "source": {"externalReferences": {"connection": "conn-sf"}}
        }}
    ]}}
    monkeypatch.setattr(fabric_api, "get_token", lambda: "t")
    monkeypatch.setattr(fabric_api, "list_items", lambda *a, **kw: [
        {"id": "p1", "displayName": "MyPipe", "type": "DataPipeline"},
    ])
    monkeypatch.setattr(fabric_api, "get_item_definition",
                        lambda ws, iid, tok, item_type=None: _pipeline_definition(doc))
    monkeypatch.setattr(fabric_api, "get_dataset_gateway", lambda *a, **kw: None)
    monkeypatch.setattr(fabric_api, "list_fabric_connections",
                        lambda tok: ([
                            {"id": "conn-sf", "displayName": "SF",
                             "connectionDetails": {"type": "Snowflake.Databases"}}
                        ], None))
    monkeypatch.setattr(sempy_path, "sempy_available", lambda: False)

    r = discovery.scan_workspace(
        workspace_id="ws", access_token="t",
        include_fabric_connections=True, telemetry_enabled=False, verbose=False,
    )
    assert len(r.artifacts) == 1
    assert r.artifacts[0].item_type == "DataPipeline"
    assert r.pipeline_calls == 1
    call = r.artifacts[0].hits[0]
    assert "Snowflake" in call.connector_kind
    assert call.is_migrating is True
    # Coverage should now count DataPipeline as inspected.
    assert "DataPipeline" in r.coverage()["inspected_types"]


# --------------------------------------------------------------------------- #
# 6-7. Sempy live-proof via mock package
# --------------------------------------------------------------------------- #

@pytest.fixture
def mock_sempy(monkeypatch):
    """Install a fake sempy.fabric module that returns pandas DataFrames
    matching the real API shape."""
    import pandas as pd
    fake_fabric = types.ModuleType("sempy.fabric")
    parts_df = pd.DataFrame({
        "Table Name": ["Sales", "Sales"],
        "Partition Name": ["p1", "p2"],
        "Source Type": ["m", "m"],
        "Source Expression": [
            'let s = Snowflake.Databases("acct.snowflakecomputing.com") in s',
            'let s = Sql.Database("localhost", "db") in s',
        ],
    })
    exprs_df = pd.DataFrame({
        "Name": ["Region"],
        "Expression": ['"WEST"'],
    })
    fake_fabric.list_partitions = lambda dataset, workspace: parts_df
    fake_fabric.list_expressions = lambda dataset, workspace: exprs_df
    fake_sempy = types.ModuleType("sempy")
    fake_sempy.fabric = fake_fabric
    monkeypatch.setitem(sys.modules, "sempy", fake_sempy)
    monkeypatch.setitem(sys.modules, "sempy.fabric", fake_fabric)
    return fake_fabric


def test_sempy_available_returns_true_with_mock(mock_sempy):
    assert sempy_path.sempy_available() is True


def test_sempy_extract_returns_expressions(mock_sempy):
    result = sempy_path.extract_semantic_model_expressions_via_sempy(
        "ws-x", "ds-1", "Sales Model"
    )
    assert result is not None
    assert len(result) == 3  # 2 partitions + 1 shared
    # Verify the Snowflake M expression is preserved.
    assert any("Snowflake.Databases" in r["expression"] for r in result)


def test_sempy_path_wires_into_discovery(mock_sempy, monkeypatch):
    """Full end-to-end: sempy path used, results appear in report,
    sempy_hits > 0 and no REST getDefinition was called."""
    rest_call_count = [0]
    def _explode(*a, **kw):
        rest_call_count[0] += 1
        return None
    monkeypatch.setattr(fabric_api, "get_token", lambda: "t")
    monkeypatch.setattr(fabric_api, "list_items", lambda *a, **kw: [
        {"id": "ds1", "displayName": "Model", "type": "SemanticModel"},
    ])
    monkeypatch.setattr(fabric_api, "get_item_definition", _explode)
    monkeypatch.setattr(fabric_api, "get_dataset_gateway", lambda *a, **kw: "gw-1")
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([], None))

    r = discovery.scan_workspace(
        workspace_id="ws-x", access_token="t",
        include_fabric_connections=False, telemetry_enabled=False,
        verbose=False, use_sempy=True,
    )
    assert r.used_sempy_path is True
    assert r.sempy_hits == 1
    assert rest_call_count[0] == 0  # REST fallback never fired
    assert len(r.artifacts) == 1
    assert any("Snowflake" in c.connector_kind for c in r.artifacts[0].hits)


# --------------------------------------------------------------------------- #
# 8-11. Service Principal auth
# --------------------------------------------------------------------------- #

def test_sp_env_detected_with_secret(monkeypatch):
    monkeypatch.setenv(auth.ENV_TENANT, "tid")
    monkeypatch.setenv(auth.ENV_CLIENT, "cid")
    monkeypatch.setenv(auth.ENV_SECRET, "shhh")
    assert auth.service_principal_env_set() is True


def test_sp_env_detected_with_cert(monkeypatch):
    monkeypatch.setenv(auth.ENV_TENANT, "tid")
    monkeypatch.setenv(auth.ENV_CLIENT, "cid")
    monkeypatch.delenv(auth.ENV_SECRET, raising=False)
    monkeypatch.setenv(auth.ENV_CERT_PATH, "/x/y.pem")
    assert auth.service_principal_env_set() is True


def test_sp_env_missing_when_only_tenant_set(monkeypatch):
    monkeypatch.setenv(auth.ENV_TENANT, "tid")
    monkeypatch.delenv(auth.ENV_CLIENT, raising=False)
    monkeypatch.delenv(auth.ENV_SECRET, raising=False)
    monkeypatch.delenv(auth.ENV_CERT_PATH, raising=False)
    assert auth.service_principal_env_set() is False


def test_sp_acquire_token_via_mock_msal(monkeypatch):
    """Simulate the MSAL ConfidentialClientApplication path."""
    calls = {}
    class FakeApp:
        def __init__(self, client_id, authority, client_credential):
            calls["client_id"] = client_id
            calls["authority"] = authority
            calls["credential"] = client_credential
        def acquire_token_for_client(self, scopes):
            calls["scopes"] = scopes
            return {"access_token": "sp-token-xyz"}
    fake_msal = types.ModuleType("msal")
    fake_msal.ConfidentialClientApplication = FakeApp
    monkeypatch.setitem(sys.modules, "msal", fake_msal)

    token = auth.acquire_token_service_principal(
        tenant_id="t1", client_id="c1", client_secret="s1"
    )
    assert token == "sp-token-xyz"
    assert calls["client_id"] == "c1"
    assert "t1" in calls["authority"]
    assert calls["credential"] == "s1"


def test_sp_acquire_token_raises_when_msal_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "msal", None)
    with pytest.raises(ImportError, match="msal"):
        auth.acquire_token_service_principal(
            tenant_id="t", client_id="c", client_secret="s"
        )


def test_sp_acquire_token_raises_on_msal_error(monkeypatch):
    class FakeApp:
        def __init__(self, *a, **kw): pass
        def acquire_token_for_client(self, scopes):
            return {"error": "invalid_client", "error_description": "bad secret"}
    fake_msal = types.ModuleType("msal")
    fake_msal.ConfidentialClientApplication = FakeApp
    monkeypatch.setitem(sys.modules, "msal", fake_msal)
    with pytest.raises(RuntimeError, match="invalid_client"):
        auth.acquire_token_service_principal(
            tenant_id="t", client_id="c", client_secret="s"
        )


def test_get_token_uses_sp_when_env_set(monkeypatch):
    monkeypatch.setenv(auth.ENV_TENANT, "tid")
    monkeypatch.setenv(auth.ENV_CLIENT, "cid")
    monkeypatch.setenv(auth.ENV_SECRET, "shhh")
    class FakeApp:
        def __init__(self, *a, **kw): pass
        def acquire_token_for_client(self, scopes):
            return {"access_token": "from-sp"}
    fake_msal = types.ModuleType("msal")
    fake_msal.ConfidentialClientApplication = FakeApp
    monkeypatch.setitem(sys.modules, "msal", fake_msal)

    assert fabric_api.get_token() == "from-sp"


# --------------------------------------------------------------------------- #
# 12-14. HTML pagination
# --------------------------------------------------------------------------- #

def _huge_report(n_calls: int) -> report_mod.ImpactReport:
    r = report_mod.ImpactReport(workspace_id="ws-huge")
    r.observed_types = {"SemanticModel": n_calls}
    hits = [
        ConnectorCall(
            connector_kind="Snowflake",
            m_function="Snowflake.Databases",
            migration="odbc_to_adbc:snowflake",
            implementation="1.0",
            endpoint_hint=f"acct{i}.snowflakecomputing.com",
            excerpt=f"call {i}",
            custom_dsn=False,
        )
        for i in range(n_calls)
    ]
    # One big artifact holds all hits so we can test per-group cap.
    r.add(report_mod.ImpactedArtifact(
        workspace_id="ws", item_id="ds", item_name="Big",
        item_type="SemanticModel", has_gateway=False, hits=hits,
    ))
    return r


def test_html_pagination_caps_rows_per_group():
    r = _huge_report(60)
    html = r._repr_html_()
    # Only the first 25 rows in a group render; a "…and N more" note appears.
    assert "and 35 more in this group" in html
    # Report still contains the connector group header.
    assert "Snowflake" in html


def test_html_pagination_total_cap_across_groups():
    """500 calls should stay well under 500KB rendered."""
    r = _huge_report(500)
    html = r._repr_html_()
    # The whole report renders quickly and stays bounded in size.
    assert len(html) < 500_000, f"HTML too large: {len(html)} chars"
    # Per-group cap message is present.
    assert "more in this group" in html


def test_html_pagination_render_time_under_1s():
    """Regression guard: even at 500 calls the render must be interactive."""
    import time
    r = _huge_report(500)
    t0 = time.time()
    html = r._repr_html_()
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"HTML render too slow: {elapsed:.2f}s"
    assert len(html) > 0
