"""Discovery: enumerate connector calls in a Fabric workspace.

Optimized after David Coe's real-world 23-minute run on the MSIT test
workspace (81 artifacts, 228 connector calls). Three changes drive the
speedup:

1. **Skip non-migrating connectors by default.** ~45% of David's calls
   were to SQL / Web / Excel / etc. that we cannot help with. The default
   is now ``include_non_migrating=False``; pass ``True`` for a full
   inventory.
2. **Parallelize per-item REST calls.** ``get_item_definition`` and
   ``get_dataset_gateway`` are I/O bound; a ``ThreadPoolExecutor`` with
   ``DEFAULT_MAX_PARALLEL`` workers runs them concurrently.
3. **Faster LRO first poll.** ``fabric_api._await_lro`` now starts at 1s
   and backs off, instead of respecting the server's ``Retry-After: 20``
   before the very first poll.

Progress is printed every ``PROGRESS_EVERY`` artifacts so a long run
does not look hung.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import definitions, fabric_api, telemetry
from .constants import DEFAULT_MAX_PARALLEL
from .mcode import ConnectorCall, find_all_connectors
from .report import ImpactReport, ImpactedArtifact


# Item types we currently know how to inspect for M expressions.
_INSPECTABLE_TYPES = {"SemanticModel", "Dataset", "Dataflow"}

# Print a status line every N artifacts processed
PROGRESS_EVERY = 10


def _log_progress(msg: str, verbose: bool) -> None:
    if verbose:
        print(f"[pq-adbc-advisor] {msg}", flush=True)


def _fetch_definition_and_scan(
    workspace_id: str,
    access_token: str,
    item: dict,
) -> dict | None:
    """Fetch one item's definition and run the M scan on it.

    Runs in a worker thread. Returns a dict with keys:
        {item, calls, skip_reason}
    where skip_reason is None on success and a string on skip.
    """
    item_type = item.get("type", "")
    item_id = item.get("id", "")

    if item_type not in _INSPECTABLE_TYPES:
        return {"item": item, "calls": [], "skip_reason": "type_not_inspected"}

    definition = fabric_api.get_item_definition(
        workspace_id, item_id, access_token, item_type=item_type
    )
    if definition is None:
        return {"item": item, "calls": [], "skip_reason": "definition_unavailable"}

    expressions = definitions.extract_m_expressions(definition, item_type)
    if not expressions:
        return {"item": item, "calls": [], "skip_reason": "definition_parsed_but_no_expressions"}

    calls: list[ConnectorCall] = []
    for e in expressions:
        calls.extend(find_all_connectors(e["expression"]))

    return {"item": item, "calls": calls, "skip_reason": None}


def scan_workspace(
    workspace_id: str | None = None,
    access_token: str | None = None,
    include_fabric_connections: bool = True,
    include_non_migrating: bool = False,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    telemetry_enabled: bool = True,
    verbose: bool = True,
) -> ImpactReport:
    """Scan a workspace for connector calls affected by the ADBC migration.

    Args:
        workspace_id: Fabric workspace GUID. Defaults to the current
            notebook's workspace when running inside Fabric.
        access_token: PBI/Fabric bearer token. Defaults to the notebook
            user's token via notebookutils.
        include_fabric_connections: When True (default) also list the
            caller's shared Fabric Connections via /v1/connections.
        include_non_migrating: When False (default) the report only shows
            connectors that belong to a migration effort (Snowflake,
            BigQuery, Databricks, etc.) plus any custom-DSN M queries.
            When True the report also lists every other external
            connector (SQL Server, Excel, Web, etc.) with a
            ``migration=none`` tag. Turning this off makes the scan
            substantially faster on large workspaces.
        max_parallel: Cap on simultaneous per-item REST calls. Default
            10 stays within Fabric's typical per-identity rate limits.
        telemetry_enabled: When True (default) emit anonymous scan
            metrics to Application Insights.
        verbose: When True (default) print a progress line to stdout
            every 10 artifacts. Set False for quiet operation (CI).
    """
    started = time.time()
    workspace_id = workspace_id or fabric_api.current_workspace_id()
    access_token = access_token or fabric_api.get_token()

    report = ImpactReport(workspace_id=workspace_id, scope="workspace")
    _log_progress(f"listing items in workspace {workspace_id}...", verbose)
    items = fabric_api.list_items(workspace_id, access_token)
    _log_progress(f"found {len(items)} items, scanning definitions in parallel (max_parallel={max_parallel})...", verbose)

    # Phase 1: parallel definition fetches + M scans
    completed = 0
    scan_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(_fetch_definition_and_scan, workspace_id, access_token, item): item
            for item in items
        }
        for fut in as_completed(futures):
            completed += 1
            if completed % PROGRESS_EVERY == 0:
                elapsed = time.time() - started
                _log_progress(
                    f"processed {completed}/{len(items)} items ({elapsed:.0f}s elapsed)",
                    verbose,
                )
            try:
                scan_results.append(fut.result())
            except Exception as e:
                item = futures[fut]
                report.record_skipped(
                    item.get("id", ""), item.get("displayName", ""),
                    item.get("type", ""), reason=f"error: {type(e).__name__}",
                )

    _log_progress(f"scans complete in {time.time() - started:.0f}s. Filtering + fetching gateways...", verbose)

    # Phase 2: apply the include_non_migrating filter, collect artifacts
    # that still need a gateway lookup
    needs_gateway: list[tuple[dict, list[ConnectorCall]]] = []
    for res in scan_results:
        item = res["item"]
        item_id = item.get("id", "")
        item_name = item.get("displayName", "")
        item_type = item.get("type", "")

        if res["skip_reason"] is not None:
            report.record_skipped(item_id, item_name, item_type, reason=res["skip_reason"])
            continue

        calls = res["calls"]
        if not calls:
            report.record_skipped(item_id, item_name, item_type, reason="no_external_connectors")
            continue

        if not include_non_migrating:
            # Fast-path filter: drop non-migrating calls unless they're
            # custom-DSN (which are still relevant per External Guide #4).
            filtered = [c for c in calls if c.is_migrating or c.custom_dsn]
            if not filtered:
                report.record_skipped(
                    item_id, item_name, item_type, reason="no_migrating_connectors",
                )
                continue
            calls = filtered

        if item_type in ("SemanticModel", "Dataset"):
            needs_gateway.append((item, calls))
        else:
            report.add(
                ImpactedArtifact(
                    workspace_id=workspace_id, item_id=item_id, item_name=item_name,
                    item_type=item_type, has_gateway=None, hits=calls,
                )
            )

    # Phase 3: parallel gateway lookups
    if needs_gateway:
        _log_progress(f"looking up gateway for {len(needs_gateway)} datasets...", verbose)

        def _gateway_for(item_id: str) -> Any:
            return fabric_api.get_dataset_gateway(workspace_id, item_id, access_token)

        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            gw_futures = {
                pool.submit(_gateway_for, item.get("id", "")): (item, calls)
                for item, calls in needs_gateway
            }
            for fut in as_completed(gw_futures):
                item, calls = gw_futures[fut]
                try:
                    gateway_result = fut.result()
                except Exception:
                    gateway_result = "unknown"
                if gateway_result == "unknown":
                    has_gateway = None
                else:
                    has_gateway = gateway_result is not None
                report.add(
                    ImpactedArtifact(
                        workspace_id=workspace_id,
                        item_id=item.get("id", ""),
                        item_name=item.get("displayName", ""),
                        item_type=item.get("type", ""),
                        has_gateway=has_gateway, hits=calls,
                    )
                )

    if include_fabric_connections:
        _log_progress("listing Fabric shared connections...", verbose)
        connections, err = fabric_api.list_fabric_connections(access_token)
        report.fabric_connections = connections
        report.fabric_connections_error = err

    duration = time.time() - started
    _log_progress(
        f"scan complete in {duration:.0f}s: "
        f"{len(report.artifacts)} impacted artifact(s), {len(report.skipped)} skipped.",
        verbose,
    )
    telemetry.emit_scan_summary(report, enabled=telemetry_enabled, duration_seconds=duration)
    return report


def scan_tenant(
    access_token: str | None = None,
    workspace_ids: list[str] | None = None,
    include_fabric_connections: bool = True,
    include_non_migrating: bool = False,
    telemetry_enabled: bool = True,
    verbose: bool = True,
) -> ImpactReport:
    """Tenant-wide scan via the Power BI admin Scanner API.

    Requires Fabric admin permission (or an SP in the correct security group).
    Emits one row per connector call across the tenant.

    ``include_non_migrating`` behaves the same as in ``scan_workspace``.
    """
    started = time.time()
    access_token = access_token or fabric_api.get_token()
    if workspace_ids is None:
        _log_progress("listing modified workspaces...", verbose)
        workspaces = fabric_api.scan_workspaces_modified(access_token)
        workspace_ids = [
            (ws.get("id") if isinstance(ws, dict) else ws) for ws in workspaces
        ]
        _log_progress(f"scanning {len(workspace_ids)} workspaces...", verbose)

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
            if not include_non_migrating:
                calls = [c for c in calls if c.is_migrating or c.custom_dsn]
                if not calls:
                    continue
            report.add(
                ImpactedArtifact(
                    workspace_id=ws_id, workspace_name=ws_name,
                    item_id=dataset.get("id", ""), item_name=dataset.get("name", ""),
                    item_type="SemanticModel", has_gateway=None, hits=calls,
                )
            )
        for df in ws.get("dataflows", []):
            calls = []
            for e in definitions.expressions_from_scanner_dataflow(df):
                calls.extend(find_all_connectors(e["expression"]))
            if not calls:
                continue
            if not include_non_migrating:
                calls = [c for c in calls if c.is_migrating or c.custom_dsn]
                if not calls:
                    continue
            report.add(
                ImpactedArtifact(
                    workspace_id=ws_id, workspace_name=ws_name,
                    item_id=df.get("objectId", df.get("id", "")),
                    item_name=df.get("name", ""),
                    item_type="Dataflow", has_gateway=None, hits=calls,
                )
            )

    if include_fabric_connections:
        connections, err = fabric_api.list_fabric_connections(access_token)
        report.fabric_connections = connections
        report.fabric_connections_error = err

    duration = time.time() - started
    _log_progress(f"tenant scan complete in {duration:.0f}s.", verbose)
    telemetry.emit_scan_summary(report, enabled=telemetry_enabled, duration_seconds=duration)
    return report
