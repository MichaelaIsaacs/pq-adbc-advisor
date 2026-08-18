"""Discovery: enumerate every connector call in every artifact in a workspace.

Unlike the previous impacted-only mode, ``scan_workspace`` now emits a row
for EVERY external connector it finds - migrating or not - with a
``migration`` bucket telling the customer which effort each falls under.

We also enumerate shared Fabric Connections via /v1/connections so the
customer sees the workspace's inventory of first-class connections too,
not only the M-embedded ones.
"""

from __future__ import annotations

from typing import Any

from . import definitions, fabric_api, telemetry
from .mcode import ConnectorCall, find_all_connectors
from .report import ImpactReport, ImpactedArtifact


# Item types we currently know how to inspect for M expressions.
_INSPECTABLE_TYPES = {"SemanticModel", "Dataset", "Dataflow"}


def scan_workspace(
    workspace_id: str | None = None,
    access_token: str | None = None,
    include_fabric_connections: bool = True,
    telemetry_enabled: bool = True,
) -> ImpactReport:
    """Scan every artifact in a workspace and enumerate all connector calls.

    Args:
        workspace_id: Fabric workspace GUID. Defaults to the current
            notebook's workspace when running inside Fabric.
        access_token: PBI/Fabric bearer token. Defaults to the notebook
            user's token via notebookutils.
        include_fabric_connections: When True (default) we also list the
            caller's shared Fabric Connections via /v1/connections. NOTE:
            /v1/connections is caller-wide (across all workspaces the
            caller can see), not workspace-scoped, and is often blocked
            for non-admins. When the API is blocked we surface the reason
            in ``report.fabric_connections_error`` rather than silently
            reporting zero connections.
        telemetry_enabled: When True (default) we emit anonymous scan
            metrics to Application Insights.
    """
    workspace_id = workspace_id or fabric_api.current_workspace_id()
    access_token = access_token or fabric_api.get_token()

    report = ImpactReport(workspace_id=workspace_id, scope="workspace")
    items = fabric_api.list_items(workspace_id, access_token)

    for item in items:
        item_type = item.get("type", "")
        item_id = item.get("id", "")
        item_name = item.get("displayName", "")

        if item_type not in _INSPECTABLE_TYPES:
            report.record_skipped(item_id, item_name, item_type, reason="type_not_inspected")
            continue

        definition = fabric_api.get_item_definition(
            workspace_id, item_id, access_token, item_type=item_type
        )
        if definition is None:
            report.record_skipped(item_id, item_name, item_type, reason="definition_unavailable")
            continue

        expressions = definitions.extract_m_expressions(definition, item_type)
        if not expressions:
            report.record_skipped(
                item_id, item_name, item_type, reason="definition_parsed_but_no_expressions"
            )
            continue
        calls: list[ConnectorCall] = []
        for e in expressions:
            calls.extend(find_all_connectors(e["expression"]))

        if not calls:
            # Artifact has expressions but none reference an external connector.
            report.record_skipped(item_id, item_name, item_type, reason="no_external_connectors")
            continue

        has_gateway = None
        if item_type in ("SemanticModel", "Dataset"):
            gateway_result = fabric_api.get_dataset_gateway(workspace_id, item_id, access_token)
            if gateway_result == "unknown":
                has_gateway = None
            else:
                has_gateway = gateway_result is not None

        report.add(
            ImpactedArtifact(
                workspace_id=workspace_id,
                item_id=item_id,
                item_name=item_name,
                item_type=item_type,
                has_gateway=has_gateway,
                hits=calls,
            )
        )

    if include_fabric_connections:
        connections, err = fabric_api.list_fabric_connections(access_token)
        report.fabric_connections = connections
        report.fabric_connections_error = err

    telemetry.emit_scan_summary(report, enabled=telemetry_enabled)
    return report


def scan_tenant(
    access_token: str | None = None,
    workspace_ids: list[str] | None = None,
    include_fabric_connections: bool = True,
    telemetry_enabled: bool = True,
) -> ImpactReport:
    """Tenant-wide scan via the Power BI admin Scanner API.

    Requires Fabric admin permission (or an SP in the correct security group).
    Emits one row per connector call across the tenant.
    """
    access_token = access_token or fabric_api.get_token()
    if workspace_ids is None:
        workspaces = fabric_api.scan_workspaces_modified(access_token)
        workspace_ids = [
            (ws.get("id") if isinstance(ws, dict) else ws) for ws in workspaces
        ]

    scan = fabric_api.scan_tenant_workspaces(access_token, workspace_ids)
    report = ImpactReport(workspace_id="(tenant)", scope="tenant")

    for ws in scan.get("workspaces", []):
        ws_id = ws.get("id", "")
        ws_name = ws.get("name", "")
        for dataset in ws.get("datasets", []):
            calls: list[ConnectorCall] = []
            for e in definitions.expressions_from_scanner_dataset(dataset):
                calls.extend(find_all_connectors(e["expression"]))
            if not calls:
                continue
            report.add(
                ImpactedArtifact(
                    workspace_id=ws_id,
                    workspace_name=ws_name,
                    item_id=dataset.get("id", ""),
                    item_name=dataset.get("name", ""),
                    item_type="SemanticModel",
                    has_gateway=None,
                    hits=calls,
                )
            )
        for df in ws.get("dataflows", []):
            calls = []
            for e in definitions.expressions_from_scanner_dataflow(df):
                calls.extend(find_all_connectors(e["expression"]))
            if not calls:
                continue
            report.add(
                ImpactedArtifact(
                    workspace_id=ws_id,
                    workspace_name=ws_name,
                    item_id=df.get("objectId", df.get("id", "")),
                    item_name=df.get("name", ""),
                    item_type="Dataflow",
                    has_gateway=None,
                    hits=calls,
                )
            )

    if include_fabric_connections:
        connections, err = fabric_api.list_fabric_connections(access_token)
        report.fabric_connections = connections
        report.fabric_connections_error = err

    telemetry.emit_scan_summary(report, enabled=telemetry_enabled)
    return report
