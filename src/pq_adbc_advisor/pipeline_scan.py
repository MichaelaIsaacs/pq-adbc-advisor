"""Data Pipeline connector inspection.

Fabric Data Pipelines are the ADF-descendant orchestrator in Fabric.
They do NOT contain Power Query M expressions — they invoke Fabric
Connections (or, in some legacy shapes, inline linked-service configs)
via ``connectionReference.connectionId``.  For the ADBC campaign we
need to know whether any of those referenced connections are for
connectors that are migrating (Snowflake, BigQuery, Databricks, etc.)
because a data pipeline "silently succeeds" today can turn into a
failed run after the tenant flip.

Coverage strategy
-----------------
* Fetch the pipeline definition via ``getDefinition``. The response
  has one part named ``pipeline-content.json`` (base64 payload).
* Walk every activity in ``properties.activities`` — including nested
  ``ifTrue/ifFalse``, ``activities`` inside ``ForEach`` / ``Until`` /
  ``Switch``.  For each activity, inspect its ``typeProperties`` for
  the well-known source / sink / dataset connection reference shapes
  documented at
  https://learn.microsoft.com/fabric/data-factory/data-pipeline-json-definition.
* Collect the set of unique ``connectionId``s referenced.
* Cross-reference against the workspace's Fabric Connections listing
  (already fetched during ``scan_workspace``) to map each ID to a
  connector kind (Snowflake, Databricks, ...).

Skip signals
------------
* If the connection ID cannot be resolved (the caller doesn't have
  permission to see /v1/connections, or the connection lives outside
  the current workspace) we record a ``ConnectionRef`` with
  ``connector_kind=None`` so the report can show "N unresolved
  references" rather than silently dropping them.
* Legacy pipelines with inline ``linkedServiceName``/
  ``linkedService.properties.typeProperties`` are also walked; the
  ``typeProperties.type`` field is treated as the connector kind.

Design constraints
------------------
1. **No M-language parsing here.** We deliberately don't invoke
   ``find_all_connectors`` — a pipeline's connector risk is decided
   by the connection it references, not by any M in the pipeline.
2. **Pipeline scan produces ``ConnectorCall`` objects** so the
   existing report / risk / troubleshoot pipeline works unchanged.
   The ``m_function`` field is set to the activity's ``type`` (e.g.
   "Copy", "Lookup") and ``excerpt`` is a truncated JSON snippet.
3. **has_gateway is unknowable for pipeline connections** without
   another API round-trip (connection details endpoint), so it stays
   ``None`` — which the report already handles as "unknown" and
   surfaces as such in the risk classifier.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .constants import (
    IMPACTED_CONNECTORS,
    MIGRATION_NONE,
    friendly_name_for_prefix,
)
from .mcode import ConnectorCall


# Activity kinds we know how to walk into for nested activities.
_NESTED_ACTIVITY_CONTAINERS = ("IfCondition", "ForEach", "Until", "Switch")

# The 'typeProperties' keys we treat as connection-reference sources.
_CONN_REF_KEYS = (
    "connectionReference",
    "externalReferences",
    "linkedService",
    "dataset",
    "source",
    "sink",
)


def extract_connection_refs(definition: dict) -> list[dict[str, Any]]:
    """Return connection references pulled from a Data Pipeline definition.

    Returns a list of dicts:
        {
            "activity_name": str,
            "activity_type": str,
            "role": "source" | "sink" | "dataset" | "linkedService" | ...,
            "connection_id": str | None,
            "inline_type": str | None,   # legacy inline linkedService.type
            "excerpt": str,               # first 200 chars of the ref JSON
        }
    """
    if not definition or "definition" not in definition:
        return []
    parts = definition["definition"].get("parts", [])
    for part in parts:
        path = part.get("path", "").lower()
        if not path.endswith("pipeline-content.json"):
            continue
        payload = part.get("payload", "")
        try:
            import base64
            text = base64.b64decode(payload).decode("utf-8", errors="replace")
            doc = json.loads(text)
        except Exception:
            return []
        activities = _flatten_activities(doc.get("properties", {}).get("activities", []))
        refs: list[dict[str, Any]] = []
        for act in activities:
            refs.extend(_refs_from_activity(act))
        return refs
    return []


def _flatten_activities(activities: Iterable[dict]) -> list[dict]:
    """Recursively unwrap ForEach/If/Until/Switch containers."""
    out: list[dict] = []
    for act in activities or []:
        if not isinstance(act, dict):
            continue
        out.append(act)
        tp = act.get("typeProperties") or {}
        if act.get("type") in _NESTED_ACTIVITY_CONTAINERS:
            for nested_key in ("activities", "ifTrueActivities", "ifFalseActivities"):
                nested = tp.get(nested_key) or []
                out.extend(_flatten_activities(nested))
            for case in tp.get("cases", []) or []:
                out.extend(_flatten_activities(case.get("activities", []) or []))
            out.extend(_flatten_activities(tp.get("defaultActivities", []) or []))
    return out


def _refs_from_activity(activity: dict) -> list[dict[str, Any]]:
    name = activity.get("name", "?")
    a_type = activity.get("type", "?")
    tp = activity.get("typeProperties") or {}
    out: list[dict[str, Any]] = []

    # Copy activity: typeProperties.source / typeProperties.sink each
    # carry a datasetSettings/externalReferences/linkedService reference.
    for role in ("source", "sink"):
        node = tp.get(role)
        if not isinstance(node, dict):
            continue
        out.extend(_extract_refs(node, name, a_type, role))
        # Copy activities also nest a datasetSettings with its own ref.
        for key in ("datasetSettings",):
            nested = node.get(key)
            if isinstance(nested, dict):
                out.extend(_extract_refs(nested, name, a_type, f"{role}.{key}"))

    # Lookup / Script / Delete / Stored Procedure: single reference at
    # typeProperties root under 'dataset' or 'linkedService'.
    for role in ("dataset", "linkedService", "externalReferences", "connectionReference"):
        node = tp.get(role)
        if isinstance(node, dict):
            out.extend(_extract_refs(node, name, a_type, role))

    # Some activity kinds (WebHook, WebActivity) don't take connections;
    # nothing to emit.
    return out


def _extract_refs(node: dict, activity_name: str, activity_type: str, role: str) -> list[dict[str, Any]]:
    """Pull a connection ID + inline type from one reference node."""
    conn_id = None
    inline_type = None

    # Modern Fabric shape: {"externalReferences": {"connection": "<guid>"}}
    ext = node.get("externalReferences")
    if isinstance(ext, dict):
        conn_id = ext.get("connection") or conn_id

    # Or: {"connectionReference": {"connectionId": "<guid>"}}
    cref = node.get("connectionReference")
    if isinstance(cref, dict):
        conn_id = cref.get("connectionId") or conn_id

    # Direct fields at this level.
    conn_id = node.get("connectionId") or conn_id
    if not conn_id and isinstance(node.get("connection"), str):
        conn_id = node.get("connection")
    # When the node IS the linkedService reference itself (common for
    # Script/Delete/StoredProcedure activities), 'referenceName' is a
    # direct child, not nested under a 'linkedService' key.
    if not conn_id and isinstance(node.get("referenceName"), str):
        conn_id = node.get("referenceName")

    # Legacy inline linked service — 'type' is the connector kind.
    ls = node.get("linkedServiceName") or node.get("linkedService")
    if isinstance(ls, dict):
        tp = ls.get("properties", {}).get("typeProperties", {})
        inline_type = ls.get("properties", {}).get("type") or inline_type
        # Sometimes the wrapper carries a 'referenceName' pointing at
        # another linked service; that's still a name, not an ID.
        conn_id = conn_id or ls.get("referenceName")
        # Older shape: connection ID hides in typeProperties.connectionId
        conn_id = conn_id or tp.get("connectionId")

    # If nothing surfaced, don't emit a phantom row.
    if not (conn_id or inline_type):
        return []

    excerpt = json.dumps(node)[:200]
    return [{
        "activity_name": activity_name,
        "activity_type": activity_type,
        "role": role,
        "connection_id": conn_id,
        "inline_type": inline_type,
        "excerpt": excerpt,
    }]


def refs_to_connector_calls(
    refs: list[dict[str, Any]],
    connections_by_id: dict[str, dict],
) -> list[ConnectorCall]:
    """Turn connection references into ConnectorCall rows.

    ``connections_by_id`` maps ``connectionId`` -> the Fabric Connection
    metadata dict from ``fabric_api.list_fabric_connections``. We use it
    to derive the connector kind. For unresolved refs the connector kind
    stays "Unresolved connection" so the customer sees the coverage gap
    instead of a false all-clear.
    """
    calls: list[ConnectorCall] = []
    for ref in refs:
        cid = ref.get("connection_id")
        inline = ref.get("inline_type")

        # Resolve connector kind
        connector_prefix, friendly = _resolve_connector(cid, inline, connections_by_id)

        # Match against IMPACTED_CONNECTORS. The impacted catalog is keyed
        # by M function name, so we do a fuzzy family match on the
        # friendly name.
        migration = MIGRATION_NONE
        for rule in IMPACTED_CONNECTORS:
            if rule["kind"].lower() in (friendly or "").lower() or rule["family"] in (connector_prefix or "").lower():
                migration = rule["migration"]
                break

        excerpt = f"{ref.get('activity_type')} activity '{ref.get('activity_name')}' (role={ref.get('role')})"
        if cid:
            excerpt += f" -> connectionId={cid}"
        elif inline:
            excerpt += f" -> inline linkedService.type={inline}"

        calls.append(ConnectorCall(
            connector_kind=friendly or "Unresolved connection",
            m_function=ref.get("activity_type", "Pipeline"),
            migration=migration,
            implementation=None,
            endpoint_hint=cid,
            excerpt=excerpt,
            custom_dsn=False,
        ))
    return calls


def _resolve_connector(
    connection_id: str | None,
    inline_type: str | None,
    connections_by_id: dict[str, dict],
) -> tuple[str | None, str]:
    """Return (prefix, friendly_name) for a connection reference."""
    if connection_id and connection_id in connections_by_id:
        c = connections_by_id[connection_id]
        details = c.get("connectionDetails") or {}
        prefix = (details.get("type") or "").split(".")[0] if details.get("type") else ""
        friendly = friendly_name_for_prefix(prefix) if prefix else c.get("displayName", "Connection")
        return prefix.lower() or None, friendly
    if inline_type:
        prefix = inline_type.split(".")[0] if inline_type else ""
        return prefix.lower() or None, friendly_name_for_prefix(prefix)
    if connection_id:
        # Known ID but unresolved (permission or cross-workspace)
        return None, "Unresolved connection"
    return None, "Unknown"
