"""Customer-feedback regressions for v0.3.9.

These cover the telemetry-observed ``error: TypeError`` skip pattern from
large real workspaces: Fabric APIs sometimes return object-valued connection
references, keyed activity maps, or malformed definition parts.
"""

from __future__ import annotations

import base64
import json
import sys
import types
from unittest.mock import patch

from pq_adbc_advisor import definitions, discovery, fabric_api, pipeline_scan


def _pipeline_definition(pipeline_content: dict) -> dict:
    payload = base64.b64encode(json.dumps(pipeline_content).encode()).decode()
    return {
        "definition": {
            "parts": [
                {"path": "pipeline-content.json", "payload": payload},
            ],
        },
    }


def test_pipeline_connection_object_reference_resolves_to_id():
    doc = {"properties": {"activities": [{
        "name": "Copy object ref",
        "type": "Copy",
        "typeProperties": {
            "source": {
                "externalReferences": {
                    "connection": {"id": "conn-sf", "displayName": "SF"}
                }
            }
        },
    }]}}
    refs = pipeline_scan.extract_connection_refs(_pipeline_definition(doc))
    assert refs[0]["connection_id"] == "conn-sf"

    calls = pipeline_scan.refs_to_connector_calls(refs, {
        "conn-sf": {
            "id": "conn-sf",
            "connectionDetails": {"type": "Snowflake.Databases"},
        }
    })
    assert calls[0].connector_kind == "Snowflake"
    assert calls[0].is_migrating is False
    assert calls[0].migration == "none"


def test_pipeline_keyed_activity_map_and_malformed_refs_do_not_typeerror():
    doc = {"properties": {"activities": {
        "copy1": {
            "name": "Copy malformed",
            "type": "Copy",
            "typeProperties": {
                "source": {"externalReferences": {"connection": ["not", "hashable"]}},
                "sink": {"connectionReference": {"connectionId": {"id": "conn-redshift"}}},
            },
        }
    }}}
    refs = pipeline_scan.extract_connection_refs(_pipeline_definition(doc))
    conn_ids = [r["connection_id"] for r in refs]
    assert "conn-redshift" in conn_ids
    assert all(not isinstance(cid, (list, dict)) for cid in conn_ids)

    calls = pipeline_scan.refs_to_connector_calls(refs, {
        "conn-redshift": {
            "id": "conn-redshift",
            "connectionDetails": {"type": "AmazonRedshift.Database"},
        }
    })
    assert any(c.connector_kind == "Amazon Redshift" for c in calls)


def test_pipeline_definition_null_parts_are_clean_no_refs():
    definition = {"definition": {"parts": [
        {"path": None, "payload": None},
        None,
        {"path": "pipeline-content.json", "payload": None},
    ]}}
    assert pipeline_scan.extract_connection_refs(definition) == []


def test_definition_parser_tolerates_sparse_tmsl_shapes():
    sparse_model = {
        "model": {
            "tables": [
                None,
                {"name": "Bad", "partitions": [None, {"source": None}]},
                {"name": "Good", "partitions": [{
                    "name": "p",
                    "source": {
                        "type": "m",
                        "expression": ["let", 'S = Snowflake.Databases("acct")', "in S"],
                    },
                }]},
            ],
            "expressions": [None, {"name": "Shared", "expression": {"not": "m"}}],
        }
    }
    payload = base64.b64encode(json.dumps(sparse_model).encode()).decode()
    out = definitions.extract_m_expressions({
        "definition": {"parts": [{"path": "model.bim", "payload": payload}]}
    }, "SemanticModel")
    assert any("Snowflake.Databases" in e["expression"] for e in out)


def test_discovery_worker_skips_non_string_expression_payloads():
    fake_item = {"id": "sm-1", "displayName": "Sparse model", "type": "SemanticModel"}
    with patch.object(discovery.fabric_api, "get_item_definition", return_value={"definition": {"parts": []}}), \
         patch.object(discovery.definitions, "extract_m_expressions", return_value=[
             {"name": "bad", "expression": {"unexpected": "shape"}},
             {"name": "good", "expression": 'let S = Snowflake.Databases("acct") in S'},
         ]), \
         patch.object(discovery.sempy_path, "extract_semantic_model_expressions_via_sempy", return_value=None):
        result = discovery._fetch_definition_and_scan("ws", "tok", fake_item, use_sempy=False)

    assert result["skip_reason"] is None
    assert result["malformed_expression_count"] == 1
    assert result["calls"][0].connector_kind == "Snowflake"


def test_discovery_worker_labels_all_malformed_expression_payloads():
    fake_item = {"id": "sm-2", "displayName": "Malformed model", "type": "SemanticModel"}
    with patch.object(discovery.fabric_api, "get_item_definition", return_value={"definition": {"parts": []}}), \
         patch.object(discovery.definitions, "extract_m_expressions", return_value=[
             {"name": "bad", "expression": {"unexpected": "shape"}},
             {"name": "also-bad", "expression": None},
         ]), \
         patch.object(discovery.sempy_path, "extract_semantic_model_expressions_via_sempy", return_value=None):
        result = discovery._fetch_definition_and_scan("ws", "tok", fake_item, use_sempy=False)

    assert result["skip_reason"] == "malformed_expression_payload"
    assert result["malformed_expression_count"] == 2


def test_scan_tenant_empty_scanner_payload_explains_no_supported_artifacts(monkeypatch):
    monkeypatch.setattr(fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(
        fabric_api,
        "scan_workspaces_modified",
        lambda tok: [{"id": f"ws-{i}"} for i in range(150)],
    )
    monkeypatch.setattr(
        fabric_api,
        "scan_tenant_workspaces",
        lambda tok, ids: {"workspaces": [{"id": "ws-1", "name": "Workspace 1"}]},
    )
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([], None))
    monkeypatch.setattr(fabric_api, "list_items", lambda ws, tok, item_type=None: [])

    report = discovery.scan_tenant(
        include_fabric_connections=False,
        telemetry_enabled=False,
        verbose=False,
    )

    assert report.scanned_workspace_count == 150
    assert report.coverage()["score_pct"] == 0
    assert report.skipped_by_reason()["scanner_returned_no_supported_artifacts"] == 1
    html = report._repr_html_()
    assert "Tenant scan completed across 150 workspace(s)" in html
    assert "Scanner API returned workspaces but no supported artifact metadata" in html


def test_scan_tenant_no_migrating_connectors_is_not_blank(monkeypatch):
    monkeypatch.setattr(fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([], None))
    monkeypatch.setattr(fabric_api, "scan_tenant_workspaces", lambda tok, ids: {
        "workspaces": [{
            "id": "ws-1",
            "name": "Workspace 1",
            "datasets": [{
                "id": "ds-1",
                "name": "SQL Model",
                "tables": [{"name": "T", "source": [{
                    "expression": 'let S = Sql.Database("srv","db") in S'
                }]}],
            }],
        }]
    })

    report = discovery.scan_tenant(
        access_token="tok",
        workspace_ids=["ws-1"],
        include_fabric_connections=False,
        telemetry_enabled=False,
        verbose=False,
    )

    assert report.observed_types == {"SemanticModel": 1}
    assert report.skipped_by_reason()["no_migrating_connectors"] == 1
    assert "Skipped items" in report._repr_html_()


def test_scan_tenant_sparse_scanner_payload_does_not_typeerror(monkeypatch):
    monkeypatch.setattr(fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([], None))
    monkeypatch.setattr(fabric_api, "scan_tenant_workspaces", lambda tok, ids: {
        "workspaces": [{
            "id": "ws-1",
            "datasets": [{
                "id": "ds-1",
                "name": "Sparse",
                "tables": [None, {"source": [{"expression": {"bad": "shape"}}]}],
                "expressions": [None],
            }],
            "reports": [{"id": "r-1", "name": "Report"}],
        }]
    })

    report = discovery.scan_tenant(
        access_token="tok",
        workspace_ids=["ws-1"],
        include_fabric_connections=False,
        telemetry_enabled=False,
        verbose=False,
    )

    reasons = report.skipped_by_reason()
    assert reasons["definition_parsed_but_no_expressions"] == 1
    assert reasons["type_not_inspected"] == 1


def test_scan_tenant_hybrid_fallback_adds_fabric_dataflow(monkeypatch):
    dataflow_definition = {
        "definition": {
            "parts": [{
                "path": "mashup.pq",
                "payload": base64.b64encode(
                    b'let S = Snowflake.Databases("acct", "wh") in S'
                ).decode(),
            }]
        }
    }
    monkeypatch.setattr(fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(fabric_api, "scan_tenant_workspaces", lambda tok, ids: {
        "workspaces": [{"id": "ws-1", "name": "Workspace 1"}]
    })
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([], None))
    monkeypatch.setattr(fabric_api, "list_items", lambda ws, tok, item_type=None: [
        {"id": "df-1", "displayName": "Fabric DFG2", "type": "Dataflow"}
    ] if item_type == "Dataflow" else [])
    monkeypatch.setattr(
        fabric_api,
        "get_item_definition",
        lambda ws, iid, tok, item_type=None: dataflow_definition,
    )

    report = discovery.scan_tenant(
        access_token="tok",
        workspace_ids=["ws-1"],
        include_fabric_connections=False,
        telemetry_enabled=False,
        verbose=False,
    )

    assert report.used_fabric_item_fallback is True
    assert len(report.artifacts) == 1
    assert report.artifacts[0].item_type == "Dataflow"
    assert report.artifacts[0].hits[0].connector_kind == "Snowflake"


def test_scan_tenant_hybrid_fallback_counts_copy_pipeline_as_covered_not_impacted(monkeypatch):
    pipeline_doc = {"properties": {"activities": [{
        "name": "Copy Snowflake",
        "type": "Copy",
        "typeProperties": {
            "source": {"externalReferences": {"connection": {"id": "conn-sf"}}}
        },
    }]}}
    pipeline_definition = _pipeline_definition(pipeline_doc)

    monkeypatch.setattr(fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(fabric_api, "scan_tenant_workspaces", lambda tok, ids: {
        "workspaces": [{"id": "ws-1", "name": "Workspace 1"}]
    })
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([
        {"id": "conn-sf", "connectionDetails": {"type": "Snowflake.Databases"}}
    ], None))
    monkeypatch.setattr(fabric_api, "list_items", lambda ws, tok, item_type=None: [
        {"id": "pipe-1", "displayName": "Copy Pipe", "type": "DataPipeline"}
    ] if item_type == "DataPipeline" else [])
    monkeypatch.setattr(
        fabric_api,
        "get_item_definition",
        lambda ws, iid, tok, item_type=None: pipeline_definition,
    )

    report = discovery.scan_tenant(
        access_token="tok",
        workspace_ids=["ws-1"],
        include_fabric_connections=True,
        telemetry_enabled=False,
        verbose=False,
    )

    assert len(report.artifacts) == 0
    assert report.skipped_by_reason()["no_migrating_connectors"] == 1
    assert report.pipeline_calls == 1
    assert report.coverage()["inspected_types"]["DataPipeline"] == 1


def test_scan_tenant_hybrid_copy_pipeline_can_show_as_non_migrating_inventory(monkeypatch):
    pipeline_doc = {"properties": {"activities": [{
        "name": "Copy Snowflake",
        "type": "Copy",
        "typeProperties": {
            "source": {"externalReferences": {"connection": {"id": "conn-sf"}}}
        },
    }]}}
    pipeline_definition = _pipeline_definition(pipeline_doc)

    monkeypatch.setattr(fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(fabric_api, "scan_tenant_workspaces", lambda tok, ids: {
        "workspaces": [{"id": "ws-1", "name": "Workspace 1"}]
    })
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([
        {"id": "conn-sf", "connectionDetails": {"type": "Snowflake.Databases"}}
    ], None))
    monkeypatch.setattr(fabric_api, "list_items", lambda ws, tok, item_type=None: [
        {"id": "pipe-1", "displayName": "Copy Pipe", "type": "DataPipeline"}
    ] if item_type == "DataPipeline" else [])
    monkeypatch.setattr(
        fabric_api,
        "get_item_definition",
        lambda ws, iid, tok, item_type=None: pipeline_definition,
    )

    report = discovery.scan_tenant(
        access_token="tok",
        workspace_ids=["ws-1"],
        include_fabric_connections=True,
        include_non_migrating=True,
        telemetry_enabled=False,
        verbose=False,
    )

    assert len(report.artifacts) == 1
    call = report.artifacts[0].hits[0]
    assert call.connector_kind == "Snowflake"
    assert call.migration == "none"


def test_scan_tenant_hybrid_fallback_merges_with_scanner_dataflow(monkeypatch):
    fabric_definition = {
        "definition": {
            "parts": [{
                "path": "mashup.pq",
                "payload": base64.b64encode(
                    b'let B = GoogleBigQuery.Database([BillingProject="p"]) in B'
                ).decode(),
            }]
        }
    }
    monkeypatch.setattr(fabric_api, "get_token", lambda: "tok")
    monkeypatch.setattr(fabric_api, "scan_tenant_workspaces", lambda tok, ids: {
        "workspaces": [{
            "id": "ws-1",
            "name": "Workspace 1",
            "dataflows": [{
                "objectId": "df-1",
                "name": "DF",
                "queries": [{"name": "q", "expression": 'let S = Snowflake.Databases("acct") in S'}],
            }],
        }]
    })
    monkeypatch.setattr(fabric_api, "list_fabric_connections", lambda tok: ([], None))
    monkeypatch.setattr(fabric_api, "list_items", lambda ws, tok, item_type=None: [
        {"id": "df-1", "displayName": "DF", "type": "Dataflow"}
    ] if item_type == "Dataflow" else [])
    monkeypatch.setattr(
        fabric_api,
        "get_item_definition",
        lambda ws, iid, tok, item_type=None: fabric_definition,
    )

    report = discovery.scan_tenant(
        access_token="tok",
        workspace_ids=["ws-1"],
        include_fabric_connections=False,
        telemetry_enabled=False,
        verbose=False,
    )

    assert len(report.artifacts) == 1
    kinds = {h.connector_kind for h in report.artifacts[0].hits}
    assert {"Snowflake", "Google BigQuery"} <= kinds


def test_current_workspace_id_prefers_notebook_runtime_context(monkeypatch):
    fake_notebookutils = types.ModuleType("notebookutils")
    fake_notebookutils.runtime = types.SimpleNamespace(
        context={
            "currentWorkspaceId": "notebook-ws",
            "defaultLakehouseWorkspaceId": "lakehouse-ws",
        }
    )
    monkeypatch.setitem(sys.modules, "notebookutils", fake_notebookutils)

    assert fabric_api.current_workspace_id() == "notebook-ws"
