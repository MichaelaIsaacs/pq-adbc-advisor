"""Discovery: enumerate connector calls in a Fabric workspace.

Optimized after David Coe's real-world 23-minute run on the MSIT test
workspace (81 artifacts, 228 connector calls). v0.2.4 architectural
changes on top of v0.2.3's parallelism + LRO tuning:

1. **sempy.fabric primary path.** When running inside a Fabric notebook,
   semantic-model M expressions come straight from ``sempy.fabric``
   (Pat's DFG2 accelerator pattern) — no LRO polling required.
   Falls back to REST getDefinition when sempy is unavailable.
2. **Rate-limit resilience.** All REST calls now route through
   ``fabric_api._request_with_retry`` which honors Retry-After and
   applies exponential backoff on 429/503/504. Throttled tenants no
   longer silently drop artifacts.
3. **Skipped-item transparency.** Skipped items are grouped by reason
   in the report so users see *why* their scan dropped N items instead
   of trusting an opaque count.
4. **Scope disclosure.** The report tracks which item types were
   inspected vs skipped (e.g. Report is not yet inspected) and
   surfaces that as a coverage score so customers don't take a clean
   report as a full-coverage guarantee.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import definitions, fabric_api, pipeline_scan, sempy_path, telemetry
from .constants import DEFAULT_MAX_PARALLEL
from .mcode import ConnectorCall, find_all_connectors
from .report import ImpactReport, ImpactedArtifact


# Item types we currently know how to inspect.
# SemanticModel / Dataset / Dataflow -> M expression scan.
# DataPipeline -> connection reference scan (no M code).
_INSPECTABLE_TYPES = {"SemanticModel", "Dataset", "Dataflow", "DataPipeline"}

# Item types the user is likely to have but that we do NOT yet parse
# for connector calls. Surfaced as "not inspected" in the report so
# customers know a clean scan is not a full-coverage guarantee.
_KNOWN_UNINSPECTED_TYPES = {
    "Notebook",           # M can be inline but rare
    "KQLQueryset",
    "Lakehouse",
    "Warehouse",
    "MirroredDatabase",
    "Report",
    "PaginatedReport",
    "MLModel",
    "MLExperiment",
    "Environment",
    "SparkJobDefinition",
}

# Print a status line every N artifacts processed
PROGRESS_EVERY = 10


def _log_progress(msg: str, verbose: bool) -> None:
    if verbose:
        print(f"[pq-adbc-advisor] {msg}", flush=True)


def _fetch_definition_and_scan(
    workspace_id: str,
    access_token: str,
    item: dict,
    use_sempy: bool,
) -> dict | None:
    """Fetch one item's definition and run the connector scan on it.

    Runs in a worker thread. Returns a dict with keys:
        {item, calls, skip_reason, source, pipeline_refs}
    where skip_reason is None on success and a string on skip, source
    records how the definition was obtained ("sempy" or "rest"), and
    pipeline_refs (list | None) is populated only for DataPipeline
    items — those are resolved into ConnectorCalls in the main phase
    once Fabric Connections have been fetched.
    """
    item_type = item.get("type", "")
    item_id = item.get("id", "")
    item_name = item.get("displayName", "")

    if item_type not in _INSPECTABLE_TYPES:
        return {
            "item": item, "calls": [], "skip_reason": "type_not_inspected",
            "source": None, "pipeline_refs": None,
        }

    # Data Pipeline: definition contains no M — it references connections
    # by ID. Return the raw refs; the caller resolves them after the
    # Fabric Connections listing lands.
    if item_type == "DataPipeline":
        try:
            definition = fabric_api.get_item_definition(
                workspace_id, item_id, access_token, item_type=item_type
            )
        except PermissionError:
            return {
                "item": item, "calls": [], "skip_reason": "permission_denied",
                "source": None, "pipeline_refs": None,
            }
        except FileNotFoundError:
            return {
                "item": item, "calls": [], "skip_reason": "deleted_during_scan",
                "source": None, "pipeline_refs": None,
            }
        if definition is None:
            return {
                "item": item, "calls": [], "skip_reason": "definition_unavailable",
                "source": None, "pipeline_refs": None,
            }
        refs = pipeline_scan.extract_connection_refs(definition)
        if not refs:
            return {
                "item": item, "calls": [], "skip_reason": "definition_parsed_but_no_expressions",
                "source": "rest", "pipeline_refs": None,
            }
        return {
            "item": item, "calls": [], "skip_reason": None,
            "source": "rest", "pipeline_refs": refs,
        }

    expressions: list[dict] = []
    source = "rest"

    # Fast path: sempy.fabric for semantic models when available.
    if use_sempy and item_type in ("SemanticModel", "Dataset"):
        sempy_result = sempy_path.extract_semantic_model_expressions_via_sempy(
            workspace_id, item_id, item_name
        )
        if sempy_result is not None:
            expressions = sempy_result
            source = "sempy"

    # Fallback: REST getDefinition + payload parse.
    if not expressions:
        try:
            definition = fabric_api.get_item_definition(
                workspace_id, item_id, access_token, item_type=item_type
            )
        except PermissionError:
            return {
                "item": item, "calls": [], "skip_reason": "permission_denied",
                "source": None, "pipeline_refs": None,
            }
        except FileNotFoundError:
            return {
                "item": item, "calls": [], "skip_reason": "deleted_during_scan",
                "source": None, "pipeline_refs": None,
            }
        if definition is None:
            return {
                "item": item, "calls": [], "skip_reason": "definition_unavailable",
                "source": None, "pipeline_refs": None,
            }
        expressions = definitions.extract_m_expressions(definition, item_type)
        source = "rest"

    if not expressions:
        return {
            "item": item, "calls": [],
            "skip_reason": "definition_parsed_but_no_expressions",
            "source": source, "pipeline_refs": None,
        }

    calls: list[ConnectorCall] = []
    malformed_expression_count = 0
    for e in expressions:
        expr = e.get("expression") if isinstance(e, dict) else None
        if not isinstance(expr, (str, list)):
            malformed_expression_count += 1
            continue
        calls.extend(find_all_connectors(expr))

    if malformed_expression_count and not calls:
        return {
            "item": item,
            "calls": [],
            "skip_reason": "malformed_expression_payload",
            "source": source,
            "pipeline_refs": None,
            "malformed_expression_count": malformed_expression_count,
        }

    return {
        "item": item, "calls": calls, "skip_reason": None,
        "source": source,
        "pipeline_refs": None,
        "malformed_expression_count": malformed_expression_count,
    }


def scan_workspace(
    workspace_id: str | None = None,
    access_token: str | None = None,
    include_fabric_connections: bool = True,
    include_non_migrating: bool = False,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    telemetry_enabled: bool = True,
    verbose: bool = True,
    use_sempy: bool | None = None,
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
        use_sempy: When None (default) auto-detect sempy.fabric and use
            it as the fast path for semantic models. Force True or
            False to override.
    """
    started = time.time()
    workspace_id = workspace_id or fabric_api.current_workspace_id()
    access_token = access_token or fabric_api.get_token()

    if use_sempy is None:
        use_sempy = sempy_path.sempy_available()

    report = ImpactReport(workspace_id=workspace_id, scope="workspace")
    report.used_sempy_path = use_sempy

    _log_progress(f"listing items in workspace {workspace_id}...", verbose)
    items = fabric_api.list_items(workspace_id, access_token)
    _log_progress(
        f"found {len(items)} items; scanning definitions in parallel "
        f"(max_parallel={max_parallel}, sempy={'on' if use_sempy else 'off'})...",
        verbose,
    )

    # Record which item types we saw so the report can compute a
    # coverage/trust score. A workspace with only Reports is not really
    # "clean" — we just didn't inspect anything.
    for item in items:
        report.observed_types.setdefault(item.get("type", "Unknown"), 0)
        report.observed_types[item.get("type", "Unknown")] += 1

    # Phase 1: parallel definition fetches + M scans
    completed = 0
    scan_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(_fetch_definition_and_scan, workspace_id, access_token, item, use_sempy): item
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
            except PermissionError:
                # v0.3.2 (gap 5): explicit label so users see "you didn't
                # have permission to read this item" instead of generic error.
                item = futures[fut]
                report.record_skipped(
                    item.get("id", ""), item.get("displayName", ""),
                    item.get("type", ""), reason="permission_denied",
                )
            except FileNotFoundError:
                # v0.3.3 (bug bash #8): artifact deleted between enumeration
                # and getDefinition. Distinct label from generic error.
                item = futures[fut]
                report.record_skipped(
                    item.get("id", ""), item.get("displayName", ""),
                    item.get("type", ""), reason="deleted_during_scan",
                )
            except Exception as e:
                item = futures[fut]
                report.record_skipped(
                    item.get("id", ""), item.get("displayName", ""),
                    item.get("type", ""), reason=f"error: {type(e).__name__}",
                )

    _log_progress(f"scans complete in {time.time() - started:.0f}s. Loading Fabric Connections + resolving...", verbose)

    # Track how many items came from sempy vs REST for telemetry.
    report.sempy_hits = sum(1 for r in scan_results if r.get("source") == "sempy")

    # Load Fabric Connections FIRST (v0.3.0). We need the connection ID
    # -> connector kind mapping to resolve DataPipeline references before
    # we can turn them into ConnectorCalls.
    connections_by_id: dict[str, dict] = {}
    if include_fabric_connections:
        connections, err = fabric_api.list_fabric_connections(access_token)
        report.fabric_connections = connections
        report.fabric_connections_error = err
        for c in connections or []:
            cid = c.get("id")
            if cid:
                connections_by_id[cid] = c

    # Phase 2: apply the include_non_migrating filter, collect artifacts
    # that still need a gateway lookup, and resolve pipeline references.
    needs_gateway: list[tuple[dict, list[ConnectorCall]]] = []
    pipeline_calls_count = 0
    for res in scan_results:
        item = res["item"]
        item_id = item.get("id", "")
        item_name = item.get("displayName", "")
        item_type = item.get("type", "")

        if res["skip_reason"] is not None:
            report.record_skipped(item_id, item_name, item_type, reason=res["skip_reason"])
            continue

        # DataPipeline: resolve refs against Fabric Connections now that
        # we have the ID -> kind mapping.
        if res.get("pipeline_refs"):
            calls = pipeline_scan.refs_to_connector_calls(
                res["pipeline_refs"], connections_by_id,
            )
            pipeline_calls_count += len(calls)
        else:
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

    report.pipeline_calls = pipeline_calls_count

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
    include_fabric_item_fallback: bool = True,
    telemetry_enabled: bool = True,
    verbose: bool = True,
) -> ImpactReport:
    """Tenant-wide scan via the Power BI admin Scanner API.

    Requires Fabric admin permission (or an SP in the correct security group).
    Emits one row per connector call across the tenant.

    ``include_non_migrating`` behaves the same as in ``scan_workspace``.
    ``include_fabric_item_fallback`` supplements Power BI Scanner API output
    with Fabric item definitions for Dataflows and Data Pipelines. This is
    necessary because tenant/workspace metadata is split across Power BI and
    Fabric APIs, and Scanner API payloads may omit Dataflow expressions or
    Copy/Data Pipeline JSON even when workspace enumeration succeeds.
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
    report.scanned_workspace_count = len(workspace_ids or [])
    report.used_fabric_item_fallback = include_fabric_item_fallback

    observed_keys: set[tuple[str, str, str]] = set()
    artifact_by_key: dict[tuple[str, str, str], ImpactedArtifact] = {}

    def _item_key(ws_id: str, item_id: str, item_type: str) -> tuple[str, str, str]:
        return (ws_id or "", item_id or "", item_type or "")

    def _observe(item_type: str, ws_id: str = "", item_id: str = "") -> None:
        key = _item_key(ws_id, item_id, item_type)
        if item_id and key in observed_keys:
            return
        if item_id:
            observed_keys.add(key)
        report.observed_types[item_type] = report.observed_types.get(item_type, 0) + 1

    def _scanner_id(obj: dict, *keys: str) -> str:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _remove_prior_skip(item_id: str, item_type: str) -> None:
        if not item_id:
            return
        report.skipped = [
            s for s in report.skipped
            if not (s.get("item_id") == item_id and s.get("item_type") == item_type)
        ]

    def _add_or_skip(
        *,
        ws_id: str,
        ws_name: str,
        item: dict,
        item_type: str,
        calls: list[ConnectorCall],
    ) -> None:
        item_id = _scanner_id(item, "id", "objectId", "datasetId", "dataflowId")
        item_name = _scanner_id(item, "name", "displayName") or item_id or item_type
        if not calls:
            if not any(
                s.get("item_id") == item_id and s.get("item_type") == item_type
                for s in report.skipped
            ):
                report.record_skipped(
                    item_id, item_name, item_type,
                    reason="definition_parsed_but_no_expressions",
                )
            return
        if not include_non_migrating:
            calls = [c for c in calls if c.is_migrating or c.custom_dsn]
            if not calls:
                if not any(
                    s.get("item_id") == item_id and s.get("item_type") == item_type
                    for s in report.skipped
                ):
                    report.record_skipped(
                        item_id, item_name, item_type, reason="no_migrating_connectors",
                    )
                return
        _remove_prior_skip(item_id, item_type)
        key = _item_key(ws_id, item_id, item_type)
        existing = artifact_by_key.get(key)
        if existing is not None:
            seen_calls = {
                    (c.connector_kind, c.m_function, c.migration, c.implementation, c.endpoint_hint, c.excerpt)
                    for c in existing.hits
            }
            for call in calls:
                    sig = (
                        call.connector_kind, call.m_function, call.migration,
                        call.implementation, call.endpoint_hint, call.excerpt,
                    )
                    if sig not in seen_calls:
                        existing.hits.append(call)
            return
        artifact = ImpactedArtifact(
            workspace_id=ws_id, workspace_name=ws_name,
            item_id=item_id, item_name=item_name,
            item_type=item_type, has_gateway=None, hits=calls,
        )
        artifact_by_key[key] = artifact
        report.add(artifact)

    for ws in scan.get("workspaces", []):
        ws_id = ws.get("id", "")
        ws_name = ws.get("name", "")
        for dataset in ws.get("datasets", []):
            if not isinstance(dataset, dict):
                continue
            _observe("SemanticModel", ws_id, _scanner_id(dataset, "id", "objectId", "datasetId"))
            calls: list[ConnectorCall] = []
            for e in definitions.expressions_from_scanner_dataset(dataset):
                expr = e.get("expression") if isinstance(e, dict) else None
                if isinstance(expr, (str, list)):
                    calls.extend(find_all_connectors(expr))
            _add_or_skip(
                ws_id=ws_id, ws_name=ws_name, item=dataset,
                item_type="SemanticModel", calls=calls,
            )
        for df in ws.get("dataflows", []):
            if not isinstance(df, dict):
                continue
            _observe("Dataflow", ws_id, _scanner_id(df, "id", "objectId", "dataflowId"))
            calls = []
            for e in definitions.expressions_from_scanner_dataflow(df):
                expr = e.get("expression") if isinstance(e, dict) else None
                if isinstance(expr, (str, list)):
                    calls.extend(find_all_connectors(expr))
            _add_or_skip(
                ws_id=ws_id, ws_name=ws_name, item=df,
                item_type="Dataflow", calls=calls,
            )

        for collection, item_type in (
            ("reports", "Report"),
            ("dashboards", "Dashboard"),
            ("notebooks", "Notebook"),
            ("lakehouses", "Lakehouse"),
            ("warehouses", "Warehouse"),
            ("datamarts", "Datamart"),
        ):
            for item in ws.get(collection, []) or []:
                if not isinstance(item, dict):
                    continue
                _observe(item_type, ws_id, _scanner_id(item, "id", "objectId"))
                report.record_skipped(
                    _scanner_id(item, "id", "objectId"),
                    _scanner_id(item, "name", "displayName") or item_type,
                    item_type,
                    reason="type_not_inspected",
                )

    connections_by_id: dict[str, dict] = {}
    if include_fabric_connections:
        connections, err = fabric_api.list_fabric_connections(access_token)
        report.fabric_connections = connections
        report.fabric_connections_error = err
        for c in connections or []:
            cid = c.get("id") if isinstance(c, dict) else None
            if cid:
                connections_by_id[cid] = c

    if include_fabric_item_fallback:
        _log_progress(
            "supplementing tenant scan with Fabric Dataflow/Data Pipeline definitions...",
            verbose,
        )

        def _process_fabric_item(ws_id: str, ws_name: str, item: dict) -> None:
            item_type = item.get("type", "")
            item_id = item.get("id", "")
            _observe(item_type, ws_id, item_id)
            try:
                res = _fetch_definition_and_scan(
                    ws_id, access_token, item, use_sempy=False,
                )
            except PermissionError:
                report.record_skipped(item_id, item.get("displayName", ""), item_type, "permission_denied")
                return
            except FileNotFoundError:
                report.record_skipped(item_id, item.get("displayName", ""), item_type, "deleted_during_scan")
                return
            except Exception as e:
                report.record_skipped(item_id, item.get("displayName", ""), item_type, f"error: {type(e).__name__}")
                return

            if res["skip_reason"] is not None:
                if not any(
                    s.get("item_id") == item_id and s.get("item_type") == item_type
                    for s in report.skipped
                ):
                    report.record_skipped(
                        item_id, item.get("displayName", ""), item_type, res["skip_reason"]
                    )
                return

            if res.get("pipeline_refs"):
                calls = pipeline_scan.refs_to_connector_calls(
                    res["pipeline_refs"], connections_by_id,
                )
                report.pipeline_calls += len(calls)
            else:
                calls = res["calls"]

            _add_or_skip(
                ws_id=ws_id,
                ws_name=ws_name,
                item=item,
                item_type=item_type,
                calls=calls,
            )

        for ws_id in workspace_ids or []:
            if not ws_id:
                continue
            try:
                fabric_items: list[dict] = []
                fabric_items.extend(fabric_api.list_items(ws_id, access_token, item_type="Dataflow"))
                fabric_items.extend(fabric_api.list_items(ws_id, access_token, item_type="DataPipeline"))
            except Exception:
                report.record_skipped(
                    ws_id,
                    f"Workspace {ws_id}",
                    "Workspace",
                    "fabric_items_unavailable",
                )
                continue

            ws_name = ""
            for item in fabric_items:
                if isinstance(item, dict) and item.get("type") in ("Dataflow", "DataPipeline"):
                    _process_fabric_item(ws_id, ws_name, item)

    if not report.artifacts and not report.skipped:
        report.observed_types["Workspace"] = len(workspace_ids or [])
        report.record_skipped(
            "(tenant)",
            f"{len(workspace_ids or [])} workspace(s)",
            "Tenant",
            reason="scanner_returned_no_supported_artifacts",
        )

    duration = time.time() - started
    _log_progress(f"tenant scan complete in {duration:.0f}s.", verbose)
    telemetry.emit_scan_summary(report, enabled=telemetry_enabled, duration_seconds=duration)
    return report
