"""Telemetry emission to Application Insights.

This module implements the telemetry contract defined in the
PQ-Connector-Upgrade-Advisor-Options spec: event names, field names,
and Kusto-query compatibility.  We deliberately mirror the spec so the
Kusto queries already in the spec work against the events we send.

Two things worth calling out vs. the spec:

  1. The spec defines an OPTIONAL ``tenant_hash`` field (SHA-256 of
     the tenant ID, first 12 hex chars). We honor that by default. If
     you set ``PQ_ADBC_ADVISOR_TENANT_RAW=1`` we send the raw AAD
     tenant guid too as ``tenant_id`` so you can join to MSSales for
     TPID. Turning that flag on is a Michaela decision and defaults
     to OFF to match the spec's "one-way hash" statement.

  2. The spec's remediation events (remediate_previewed / committed /
     rolled_back / failed) are NOT emitted by v0 of the tool because
     it only diagnoses - it doesn't rewrite M code. Those events are
     reserved for Level 2. We keep them as reserved names so the
     Kusto queries in the spec remain valid.

Every ``scan_complete`` event ALSO carries the "first-run" snapshot
locked in on the first scan of each workspace, plus the current-run
counters, so improvement over time is a single-row KQL query without
a self-join.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import time
import uuid
from typing import Any

import requests

from .constants import (
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_NA,
    RISK_UNKNOWN,
    TOOL_VERSION,
)
from . import state as _state

# Application Insights ingestion.
#
# Ordering:
#   1. Environment variable PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING (preferred)
#   2. Environment variable PQ_ADBC_ADVISOR_APPINSIGHTS_KEY (ikey only)
#   3. The BAKED_IN_CONNECTION_STRING below (set by Michaela before publishing)
#   4. Nothing -> telemetry is a no-op
#
# For the appi-fabric-migration-scanner resource, replace the placeholder
# below with the connection string from Azure Portal -> Overview.
BAKED_IN_CONNECTION_STRING = (
    "InstrumentationKey=82635ec0-984b-46e2-9abd-2a6c3d371af1;"
    "IngestionEndpoint=https://westus2-2.in.applicationinsights.azure.com/;"
    "LiveEndpoint=https://westus2.livediagnostics.monitor.azure.com/;"
    "ApplicationId=9d5ab587-21ab-4b01-ad3a-e43e2fccd8dc"
)


def _resolve_connection() -> tuple[str, str] | None:
    """Return (ikey, ingestion_endpoint) or None when telemetry is disabled."""
    conn = (
        os.environ.get("PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING")
        or BAKED_IN_CONNECTION_STRING
    )
    ikey = ""
    endpoint = "https://dc.services.visualstudio.com/v2/track"
    if conn and "InstrumentationKey=" in conn:
        for chunk in conn.split(";"):
            if "=" not in chunk:
                continue
            k, v = chunk.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k.lower() == "instrumentationkey":
                ikey = v
            elif k.lower() == "ingestionendpoint":
                endpoint = v.rstrip("/") + "/v2/track"
    # If the connection string only carried the placeholder, fall back to
    # the ikey env var so tests / migrations still work.
    if (not ikey) or ikey.startswith("00000000"):
        env_ikey = os.environ.get("PQ_ADBC_ADVISOR_APPINSIGHTS_KEY", "").strip()
        if env_ikey and not env_ikey.startswith("00000000"):
            ikey = env_ikey
    if not ikey or ikey.startswith("00000000"):
        return None
    return ikey, endpoint


def _telemetry_enabled(explicit: bool) -> bool:
    if not explicit:
        return False
    # Environment variable opt-out (works for CI / local dev but not always
    # discoverable from a Fabric notebook - see set_telemetry_opt_out below
    # for the persistent notebook-friendly path).
    if os.environ.get("PQ_ADBC_ADVISOR_TELEMETRY", "").lower() in ("off", "0", "false"):
        return False
    # Persistent opt-out written to the lakehouse state file. Survives
    # kernel restarts and is set via the disable_telemetry() Python API.
    if _state.is_telemetry_opted_out():
        return False
    return _resolve_connection() is not None


# --------------------------------------------------------------------------- #
# Public opt-out / opt-in API (persistent across kernel sessions)
# --------------------------------------------------------------------------- #
# Env vars are unreliable in Fabric notebooks (David Coe review 2026-08-19).
# These functions write the opt-out flag to the same lakehouse state file
# that carries the first-run baseline, so the choice survives kernel restarts.

def disable_telemetry() -> bool:
    """Persistently disable anonymous telemetry for this workspace.

    Writes an opt-out flag to /lakehouse/default/Files/pq_adbc_advisor_state.json
    that survives kernel restarts. Prefer this over environment variables in
    Fabric notebooks.

    Returns True when the opt-out was successfully persisted, False when we
    couldn't reach the state file (e.g. no default lakehouse attached, in
    which case the opt-out is still honored for the current process).
    """
    saved = _state.set_telemetry_opt_out(True)
    if saved:
        print("[pq-adbc-advisor] Anonymous telemetry disabled for this workspace.")
        print("[pq-adbc-advisor] Run enable_telemetry() to re-enable.")
    else:
        print("[pq-adbc-advisor] Telemetry opt-out set for this session.")
        print("[pq-adbc-advisor] (Could not persist to lakehouse - the opt-out")
        print("[pq-adbc-advisor]  applies to this kernel session only.)")
    return saved


def enable_telemetry() -> bool:
    """Re-enable anonymous telemetry after a previous disable_telemetry() call.

    Returns True when the opt-in was persisted successfully.
    """
    saved = _state.set_telemetry_opt_out(False)
    if saved:
        print("[pq-adbc-advisor] Anonymous telemetry re-enabled.")
    return saved


def telemetry_status() -> dict[str, Any]:
    """Return a dict describing the current telemetry configuration.

    Useful for the customer to check what the tool is doing before they
    trust the opt-out choice. v0.3.5 also exposes the last-send status
    so corporate-proxy / throttled-ikey issues surface without a debug
    session.
    """
    resolved = _resolve_connection()
    history = get_send_history()
    return {
        "endpoint_configured": resolved is not None,
        "resource": "appi-fabric-migration-scanner" if resolved else None,
        "env_var_off": os.environ.get("PQ_ADBC_ADVISOR_TELEMETRY", "").lower() in ("off", "0", "false"),
        "persistent_opt_out": _state.is_telemetry_opted_out(),
        "effectively_enabled": _telemetry_enabled(True),
        "last_send": history[-1] if history else None,
        "recent_send_statuses": [h["status"] for h in history[-5:]],
    }


# --------------------------------------------------------------------------- #
# First-run stdout notice
# --------------------------------------------------------------------------- #

_FIRST_RUN_NOTICE = (
    "\n" + "─" * 68 + "\n"
    "  pq-adbc-advisor — anonymous telemetry\n"
    + "─" * 68 + "\n"
    "  This tool sends anonymous counts (per-connector, per-risk) to the\n"
    "  Power Query PM team so we can measure adoption and prioritize\n"
    "  building these diagnostics into the product itself.\n"
    "\n"
    "  Never sent: M code, item/workspace names, endpoint URLs, credentials,\n"
    "  refresh error message bodies, gateway names, or dataset IDs.\n"
    "  Tenant ID is SHA-256 hashed by default (12 hex chars).\n"
    "\n"
    "  To disable at any time (persists across kernel restarts):\n"
    "    from pq_adbc_advisor import disable_telemetry\n"
    "    disable_telemetry()\n"
    "\n"
    "  Details + Kusto queries the PM team runs:\n"
    "    https://github.com/MichaelaIsaacs/pq-adbc-advisor#telemetry\n"
    + "─" * 68 + "\n"
)


def _maybe_print_first_run_notice(is_first_run: bool) -> None:
    """Print the notice once per (workspace × process) on first-run only."""
    if not is_first_run:
        return
    if getattr(_maybe_print_first_run_notice, "_printed", False):
        return
    _maybe_print_first_run_notice._printed = True  # type: ignore[attr-defined]
    try:
        print(_FIRST_RUN_NOTICE)
    except Exception:
        pass


def _send_raw_tenant() -> bool:
    """True when we should send the raw AAD tenant guid alongside tenant_hash."""
    return os.environ.get("PQ_ADBC_ADVISOR_TENANT_RAW", "").lower() in ("1", "on", "true")


def _tenant_id() -> str:
    """Best-effort raw AAD tenant guid from the Fabric notebook runtime."""
    try:
        import notebookutils  # type: ignore
        ctx = getattr(notebookutils.runtime, "context", None)
        if ctx is None:
            return ""
        get = ctx.get if callable(getattr(ctx, "get", None)) else lambda *_: ""
        return get("tenantId", "") or get("TenantId", "") or ""
    except Exception:
        return ""


def _tenant_hash(tenant_id: str) -> str:
    """SHA-256 first 12 hex chars, matches the spec's tenant_hash field."""
    if not tenant_id:
        return ""
    return hashlib.sha256(tenant_id.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Session identity (v0.3.4)
# --------------------------------------------------------------------------- #
# One notebook can run scan_workspace(), then validate_migration(), then
# scan_workspace() again. All three should share a session_id so we can
# tell "unique scans" apart from "one session, three phases" in KQL.
_SESSION_ID = uuid.uuid4().hex


def session_id() -> str:
    return _SESSION_ID


# --------------------------------------------------------------------------- #
# Value-story helpers (v0.3.4)
# --------------------------------------------------------------------------- #
# Every field below answers the question "why is this tool worth the
# session cost?". We only send derived numbers, never M code or names.
#
# _MANUAL_TRIAGE_MIN_PER_ARTIFACT reflects the PM team's assumption for
# per-artifact manual triage (open item, inspect definition, cross-check
# gateway binding). It's conservative on purpose; the KQL narrative is
# "we scanned N artifacts in D seconds vs an estimated N * this-many
# minutes if the customer had triaged by hand".
_MANUAL_TRIAGE_MIN_PER_ARTIFACT = 3


def _estimated_manual_hours_saved(artifacts_scanned: int) -> float:
    return round((artifacts_scanned * _MANUAL_TRIAGE_MIN_PER_ARTIFACT) / 60.0, 2)


def _base_properties(tenant_id: str, workspace_id: str, run_id: str, duration: float) -> dict[str, Any]:
    props: dict[str, Any] = {
        "version": TOOL_VERSION,
        "surface": "notebook",  # spec-defined values: notebook | cli_fabric | cli_pbix | external_tool
        "mode": "diagnose",     # v0 is diagnose-only
        "tenant_hash": _tenant_hash(tenant_id),
        "workspace_id": workspace_id or "",
        "run_id": run_id,
        "session_id": _SESSION_ID,  # v0.3.4 — same across scan+validate in one kernel
        "state_backend": _state.state_backend(),  # v0.3.4 — lakehouse | home | memory
        "duration_seconds": round(duration, 2),
        "python": platform.python_version(),
        "platform": platform.system(),
    }
    if _send_raw_tenant() and tenant_id:
        # Opt-in field so Michaela can join to MSSales for TPID.
        props["tenant_id"] = tenant_id
    return props


# --------------------------------------------------------------------------- #
# scan_complete
# --------------------------------------------------------------------------- #

def emit_scan_summary(report, enabled: bool, duration_seconds: float = 0.0) -> None:
    if not _telemetry_enabled(enabled):
        return

    # Derive current-run counters from the report.
    counts_by_risk = {RISK_HIGH: 0, RISK_MEDIUM: 0, RISK_LOW: 0, RISK_UNKNOWN: 0, RISK_NA: 0}
    counts_by_connector: dict[str, int] = {}
    counts_by_migration: dict[str, int] = {}
    pinned_odbc = 0
    pinned_at_risk = 0  # spec's odbc_pinned_at_risk_count = pinned AND no gateway
    migrating_artifacts = 0
    total_calls = 0
    custom_dsn = 0
    for a in report.artifacts:
        counts_by_risk[a.worst_risk] = counts_by_risk.get(a.worst_risk, 0) + 1
        if a.has_migrating_connector:
            migrating_artifacts += 1
        for h in a.hits:
            total_calls += 1
            counts_by_connector[h.connector_kind] = counts_by_connector.get(h.connector_kind, 0) + 1
            counts_by_migration[h.migration] = counts_by_migration.get(h.migration, 0) + 1
            if h.is_pinned_odbc:
                pinned_odbc += 1
                if a.has_gateway is False:
                    pinned_at_risk += 1
            if h.custom_dsn:
                custom_dsn += 1

    current = {
        "risk_high": counts_by_risk[RISK_HIGH],
        "risk_medium": counts_by_risk[RISK_MEDIUM],
        "risk_low": counts_by_risk[RISK_LOW],
        "risk_unknown": counts_by_risk[RISK_UNKNOWN],
        "risk_na": counts_by_risk[RISK_NA],
        "custom_dsn": custom_dsn,
        "total_calls": total_calls,
        "migrating_artifacts": migrating_artifacts,
        "pinned_odbc": pinned_odbc,
        "counts_by_connector": counts_by_connector,
    }

    run_id = uuid.uuid4().hex
    tenant_id = _tenant_id()
    workspace_id = report.workspace_id or ""
    # v0.3.5 Bug 2 fix — pass workspace/tenant identity to compute_run_state
    # so the home-fallback baseline is stored per-workspace instead of a
    # single machine-wide file that shadows every other workspace scan.
    combined = _state.compute_run_state(
        current, run_id,
        workspace_id=workspace_id,
        tenant_hash=_tenant_hash(tenant_id),
    )

    # Notify the customer on their FIRST scan of a given workspace.
    # This is our privacy-notification hook (David Coe review): before the
    # first event lands upstream the customer sees what's being collected
    # and how to opt out.
    _maybe_print_first_run_notice(bool(combined.get("is_first_run", False)))

    # Spec-compatible fields (event name + field names from the spec) FIRST.
    props = _base_properties(tenant_id, workspace_id, run_id, duration_seconds)
    inspected_artifacts = len(report.artifacts)
    artifacts_scanned = inspected_artifacts + len(report.skipped)
    props.update({
        "artifacts_scanned": artifacts_scanned,
        "inspected_artifacts": inspected_artifacts,  # v0.3.5 Bug 7 fix
        "impacted_count": migrating_artifacts,
        "odbc_pinned_count": pinned_odbc,
        "odbc_pinned_at_risk_count": pinned_at_risk,
        "scope": report.scope,
    })
    # v0.3.4 value-story fields:
    #   coverage_score_pct   — how much of the workspace we could inspect
    #   sempy_hits           — how many items used the fast path
    #   sempy_used           — whether the sempy fast path was on
    #   skipped_by_reason_*  — one field per skip reason so KQL can chart
    #                          "what actually blocks a clean scan"
    #   estimated_manual_hours_saved — value narrative for leadership
    try:
        coverage = report.coverage()
        props["coverage_score_pct"] = coverage.get("score_pct", 0)
    except Exception:
        props["coverage_score_pct"] = 0
    props["sempy_hits"] = getattr(report, "sempy_hits", 0)
    props["sempy_used"] = bool(getattr(report, "used_sempy_path", False))
    # v0.3.5 Bug 7 fix: hours-saved credits inspected items ONLY. Skipped
    # items (permission_denied, deleted, non-inspectable types) produced
    # no diagnostic value, so the tool cannot claim to have saved manual
    # triage time on them.
    props["estimated_manual_hours_saved"] = _estimated_manual_hours_saved(inspected_artifacts)
    for reason, count in report.skipped_by_reason().items():
        props[f"skipped_by_reason_{_safe_key(reason)}"] = count
    # Spec's ``impacts_by_connector`` field, flattened as one property per
    # connector so the KQL ``mv-expand impacts_by_connector`` query works
    # against ``customDimensions``.
    for kind, count in counts_by_connector.items():
        props[f"impacts_by_connector_{_safe_key(kind)}"] = count

    # Extra improvement-over-time fields (not in the spec's original
    # contract but strictly additive so old queries still work).
    is_first_run = bool(combined.get("is_first_run", True))
    first_run_at = combined.get("first_run_at", "") or ""
    props.update({
        "run_count": combined.get("run_count", 1),
        "is_first_run": is_first_run,
        "first_run_at": first_run_at,
        "first_risk_high": combined.get("first_risk_high", 0),
        "first_risk_medium": combined.get("first_risk_medium", 0),
        "first_pinned_odbc": combined.get("first_pinned_odbc", 0),
        "first_custom_dsn": combined.get("first_custom_dsn", 0),
        "first_migrating_artifacts": combined.get("first_migrating_artifacts", 0),
        "first_total_calls": combined.get("first_total_calls", 0),
        "current_risk_high": current["risk_high"],
        "current_risk_medium": current["risk_medium"],
        "current_pinned_odbc": current["pinned_odbc"],
        "current_custom_dsn": current["custom_dsn"],
        "fabric_connection_count": len(report.fabric_connections),
        "fabric_connections_error": report.fabric_connections_error or "",
        "skipped_count": len(report.skipped),
    })
    for kind, count in (combined.get("first_counts_by_connector", {}) or {}).items():
        props[f"first_impacts_by_connector_{_safe_key(kind)}"] = count

    # v0.3.5 — leadership resolution metrics
    #
    # These are the KQL money-shot fields: one-line "how much have they
    # cleaned up since scan #1?" charts, computed here so nobody has to
    # subtract in Kusto and nobody has to unpack per-connector fields
    # side by side. Values can go NEGATIVE when a workspace grows
    # (new pinned models added AFTER the baseline) — we send the signed
    # delta so leadership charts distinguish "progress" from "regress".
    props["resolved_risk_high"] = (
        int(combined.get("first_risk_high", 0)) - current["risk_high"]
    )
    props["resolved_risk_medium"] = (
        int(combined.get("first_risk_medium", 0)) - current["risk_medium"]
    )
    props["resolved_pinned_odbc"] = (
        int(combined.get("first_pinned_odbc", 0)) - current["pinned_odbc"]
    )
    props["resolved_custom_dsn"] = (
        int(combined.get("first_custom_dsn", 0)) - current["custom_dsn"]
    )
    props["resolved_migrating_artifacts"] = (
        int(combined.get("first_migrating_artifacts", 0)) - current["migrating_artifacts"]
    )
    # Per-connector deltas so KQL can chart "Snowflake resolutions per week"
    # in one line without unpacking baseline + current side by side.
    first_by_kind = dict(combined.get("first_counts_by_connector", {}) or {})
    all_kinds = set(first_by_kind) | set(counts_by_connector)
    for kind in all_kinds:
        first_n = int(first_by_kind.get(kind, 0))
        curr_n = int(counts_by_connector.get(kind, 0))
        props[f"resolved_by_connector_{_safe_key(kind)}"] = first_n - curr_n

    # v0.3.5 — time_to_second_scan_days
    #
    # Proves iterative use. On first run this is empty. On subsequent
    # runs it's the days between now and the persisted first_run_at.
    # We only compute this once (on run #2) so leadership charts have
    # a clean cohort metric; later runs still send it so a query can
    # bucket by "second-scan latency" without joins.
    if not is_first_run and first_run_at:
        try:
            from datetime import datetime as _dt
            first_dt = _dt.fromisoformat(first_run_at.replace("Z", "+00:00"))
            now_dt = _dt.fromisoformat(_state._now_iso().replace("Z", "+00:00"))
            days = (now_dt - first_dt).total_seconds() / 86400.0
            # Guard against clock skew producing negatives.
            props["time_since_first_scan_days"] = round(max(days, 0.0), 2)
        except (ValueError, TypeError):
            props["time_since_first_scan_days"] = ""
    else:
        props["time_since_first_scan_days"] = ""

    _post("scan_complete", props)


# --------------------------------------------------------------------------- #
# validation_complete (NOT in the original spec's event names — we send this
# with the "validation" prefix so the spec's queries against
# scan_complete are unaffected).
# --------------------------------------------------------------------------- #

def emit_validation_summary(report, enabled: bool, duration_seconds: float = 0.0) -> None:
    if not _telemetry_enabled(enabled):
        return
    counts: dict[str, int] = {}
    issues: dict[str, int] = {}
    for r in report.results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.diagnosis:
            issues[r.diagnosis.issue] = issues.get(r.diagnosis.issue, 0) + 1

    tenant_id = _tenant_id()
    workspace_id = report.baseline_workspace_id or ""

    props = _base_properties(tenant_id, workspace_id, uuid.uuid4().hex, duration_seconds)
    props.update({
        "validated_count": len(report.results),
        "passed": counts.get("passed", 0),
        "passed_with_regression": counts.get("passed_with_regression", 0),
        "failed": counts.get("failed", 0),
        "no_new_refresh": counts.get("no_new_refresh", 0),
        "refresh_not_triggered": counts.get("refresh_not_triggered", 0),
        "skipped": counts.get("skipped", 0),
    })
    for issue, count in issues.items():
        props[f"issue_{_safe_key(issue)}"] = count

    _post("validation_complete", props)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _safe_key(name: str) -> str:
    """Application Insights property keys should be alphanumeric + underscore."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


# --------------------------------------------------------------------------- #
# _post observability (v0.3.5 Bug 9 fix)
# --------------------------------------------------------------------------- #
# A silent _post is worse than no _post: the PM team can't tell a
# throttled ikey, a captive-portal proxy, or a regional 5xx from a happy
# send. We now:
#   * Inspect status_code and reject captive-portal HTML bodies.
#   * Retry ONCE on 429 / 503.
#   * Record every attempt's outcome in a bounded ring buffer that
#     telemetry_status() exposes so users can debug corporate proxies.

_SEND_HISTORY_CAP = 20
_SEND_HISTORY: list[dict[str, Any]] = []
_SEND_HISTORY_LOCK = __import__("threading").Lock()


def _record_send(status: str, detail: str = "") -> None:
    entry = {"at": _state._now_iso(), "status": status, "detail": detail[:200]}
    with _SEND_HISTORY_LOCK:
        _SEND_HISTORY.append(entry)
        if len(_SEND_HISTORY) > _SEND_HISTORY_CAP:
            del _SEND_HISTORY[: len(_SEND_HISTORY) - _SEND_HISTORY_CAP]


def get_send_history() -> list[dict[str, Any]]:
    """Snapshot of the last ~20 send attempts (v0.3.5).

    Each entry: ``{"at": iso_utc, "status": str, "detail": str}``.
    Status values: ``"ok"``, ``"retried_ok"``, ``"http_<code>"``,
    ``"captive_portal"``, ``"network"``, ``"disabled"``.
    """
    with _SEND_HISTORY_LOCK:
        return list(_SEND_HISTORY)


def _looks_like_captive_portal(body: str) -> bool:
    """Corporate proxies love returning a 200 + HTML login page."""
    if not body:
        return False
    head = body[:2048].lower()
    return "<html" in head or "<!doctype html" in head


def _post(event_name: str, properties: dict[str, Any]) -> None:
    """Send one event to Application Insights with retry + observability."""
    resolved = _resolve_connection()
    if resolved is None:
        _record_send("disabled", "no ikey resolved")
        return
    ikey, endpoint = resolved

    envelope = {
        "name": "Microsoft.ApplicationInsights.Event",
        "time": _state._now_iso(),
        "iKey": ikey,
        "data": {
            "baseType": "EventData",
            "baseData": {
                "ver": 2,
                "name": event_name,
                "properties": {k: str(v) for k, v in properties.items()},
            },
        },
    }
    body = json.dumps(envelope)
    headers = {"Content-Type": "application/json"}

    # First attempt.
    r = _try_send(endpoint, body, headers)
    if r is None:
        # Network failure — retry once to absorb transient DNS blips.
        r = _try_send(endpoint, body, headers)
        if r is None:
            _record_send("network", "requests raised twice in a row")
            return
        _record_send("retried_ok" if 200 <= r.status_code < 300 else f"http_{r.status_code}")
        return

    # HTTP response — classify.
    if r.status_code in (429, 503):
        r2 = _try_send(endpoint, body, headers)
        if r2 is not None and 200 <= r2.status_code < 300:
            _record_send("retried_ok", f"first={r.status_code}")
            return
        _record_send(
            f"http_{r.status_code}",
            f"retry {'also failed' if r2 is None else 'returned ' + str(r2.status_code)}",
        )
        return

    if 200 <= r.status_code < 300:
        # 200 from a captive portal is a hidden failure.
        try:
            text_head = (r.text or "")[:2048]
        except Exception:
            text_head = ""
        if _looks_like_captive_portal(text_head):
            _record_send("captive_portal", "HTML body in a 2xx response")
            return
        _record_send("ok")
        return

    _record_send(f"http_{r.status_code}", (getattr(r, "text", "") or "")[:200])


def _try_send(endpoint: str, body: str, headers: dict[str, str]):
    try:
        return requests.post(endpoint, data=body, headers=headers, timeout=5)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Public helper for smoke-testing the pipeline
# --------------------------------------------------------------------------- #

def send_test_event(note: str = "") -> dict[str, Any]:
    """Send a single synthetic event to prove the pipeline works.

    Returns a dict describing what was attempted so a caller can print
    it and eyeball whether the resource actually got the event.
    """
    resolved = _resolve_connection()
    if resolved is None:
        return {"sent": False, "reason": "telemetry not configured (no ikey resolved)"}
    ikey, endpoint = resolved
    ev = {
        "surface": "notebook",
        "mode": "diagnose",
        "version": TOOL_VERSION,
        "test": True,
        "note": note,
        "sent_at": _state._now_iso(),
    }
    start = time.time()
    envelope = {
        "name": "Microsoft.ApplicationInsights.Event",
        "time": _state._now_iso(),
        "iKey": ikey,
        "data": {
            "baseType": "EventData",
            "baseData": {"ver": 2, "name": "scan_complete_test", "properties": {k: str(v) for k, v in ev.items()}},
        },
    }
    try:
        r = requests.post(endpoint, data=json.dumps(envelope), headers={"Content-Type": "application/json"}, timeout=10)
        return {
            "sent": True,
            "http_status": r.status_code,
            "ingestion_endpoint": endpoint,
            "ikey_prefix": ikey[:8] + "...",
            "response_ms": round((time.time() - start) * 1000, 1),
            "response_body": r.text[:200],
        }
    except Exception as e:
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}
