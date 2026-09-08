# Telemetry — how it works, what we collect, how to audit

**Applies to:** `pq-adbc-advisor` v0.3.7+
**Owner:** Michaela Isaacs (misaacs@microsoft.com)
**Last updated:** 2026-09-08

This doc describes the telemetry pipeline top-to-bottom so any PM, engineer, CSA, or customer can audit exactly what is collected, where it goes, and how to disable it.

---

## TL;DR

- **What we send:** anonymous, hashed counts. No M code, no item names, no endpoint URLs, no credentials, no refresh error message bodies.
- **Where it goes:** a single App Insights resource owned by Michaela (`appi-fabric-migration-scanner` in `rg-fabric-migration-scanner`).
- **How to opt out:** call `disable_telemetry()` once. Persists across kernel restarts and workspace re-mounts.
- **How to audit:** call `telemetry_health()` to see the pipeline state; the results are also written to a persistent log file in your workspace lakehouse.

---

## What we send

The tool emits three event names to Application Insights.

| Event | When | Contents |
|---|---|---|
| `scan_complete` | End of every `scan_workspace()` / `scan_tenant()` call | Aggregate counts per risk bucket, per connector, per migration; workspace-scoped baseline for improvement-over-time |
| `validation_complete` | End of every `validate_migration()` call | Aggregate refresh-outcome counts by issue class |
| `canary_ping` | Health-check pings from an automation, CI, or `send_canary()` | Version + source label; used only to keep an eye on the pipeline itself, never mixed with real adoption counts |

### The exact fields in `scan_complete`

Everything on the wire is either an integer, a short enum, or a hex hash. There are no free-text fields (no M excerpts, no error strings) at any point.

| Field | Type | What it is | PII? |
|---|---|---|---|
| `version` | str | Tool version (e.g. `0.3.7`) | No |
| `surface` | enum | `notebook / cli_fabric / cli_pbix / external_tool` | No |
| `mode` | enum | `diagnose / remediate` (v0 is diagnose-only) | No |
| `tenant_hash` | str | **SHA-256(tenant_id)[:12]** — one-way, no reverse lookup possible | No |
| `user_hash` | str | **SHA-256(user_upn)[:12]** — one-way | No |
| `user_hash_source` | enum | `aad / env / ""` — provenance of the hashed id | No |
| `workspace_id` | str | Fabric workspace GUID | No — workspace IDs are internal identifiers, not PII |
| `run_id` | str | UUID for this scan | No |
| `duration_seconds` | float | How long the scan took | No |
| `artifacts_scanned` | int | Total items in workspace | No |
| `impacted_count` | int | Items with a migrating connector | No |
| `odbc_pinned_count` | int | Items pinning `Implementation="1.0"` | No |
| `odbc_pinned_at_risk_count` | int | Pinned AND no gateway | No |
| `scope` | enum | `workspace / tenant` | No |
| `risk_high / risk_medium / risk_low / risk_unknown / risk_na` | int | Item counts per risk bucket | No |
| `custom_dsn` | int | Count of custom-DSN M queries | No |
| `total_calls` | int | Total connector calls found | No |
| `impacts_by_connector_<name>` | int | One field per connector family (Snowflake, Databricks, BigQuery, etc.) | No |
| `first_run_* / run_count / first_run_at` | int / str | Locked-in baseline from the first scan of this workspace | No — the tool's own bookkeeping |
| `platform / python` | str | Client-side runtime info (OS name, Python version) | No |

### What we deliberately do NOT send

- M code, source strings, or any excerpt of the customer's Power Query expressions
- Item names (e.g. semantic model names, dataflow names)
- Endpoint URLs, hostnames, ports, database or catalog names
- Credentials of any kind
- Refresh error message bodies
- Raw tenant ID, raw user UPN (only hashes)
- Anything from a custom-DSN M query
- IP addresses (Application Insights masks them by default)

---

## How to opt out

The Python API works from any Fabric notebook and is persistent across kernel restarts:

```python
from pq_adbc_advisor import disable_telemetry
disable_telemetry()
```

The opt-out flag is written to `/lakehouse/default/Files/pq_adbc_advisor_opt_out.json` on Fabric or `~/.pq-adbc-advisor/opt_out.json` locally. It stays there until you explicitly call `enable_telemetry()`.

You can also opt out via environment variable if you're running in CI or from a non-Fabric context:

```bash
export PQ_ADBC_ADVISOR_TELEMETRY=off
```

---

## How to audit the pipeline

Call `telemetry_health()` to see live state:

```python
from pq_adbc_advisor import telemetry_health
health = telemetry_health()
```

Returns:

```
{
  "connection": {"ikey_prefix": "82635ec0...", "endpoint": "https://westus2-2..."},
  "recent_attempts": [...],           # last 20 send attempts (in-memory ring buffer)
  "persistent_log_count": 47,          # attempts logged on disk (survives restart)
  "buffered_envelopes": 0,             # events waiting to flush on next send
  "last_ok_at": "2026-09-08T19:11:35Z",
  "last_failure": null,
  "opt_out_active": false
}
```

Field semantics:

- **`recent_attempts`** — the last 20 outcomes recorded in this process. Each entry is `{at, status, detail}` where `status` is one of `ok`, `retried_ok`, `http_429`, `http_5xx`, `captive_portal`, `ingest_drop`, `network`, `disabled`, `unparseable body`.
- **`persistent_log_count`** — the same information as `recent_attempts` but persisted to disk in `/lakehouse/default/Files/pq_adbc_advisor_telemetry_log.json`. Survives kernel restart so you can debug post-hoc.
- **`buffered_envelopes`** — the number of events that failed to send (proxy blocked, offline, 5xx) and are queued to retry on the next successful `_post()`. Buffered events carry the same hashed/anonymized fields as the wire event; they never hold PII.

---

## What's hardened as of v0.3.7

The v0.3.7 release fixed several silent-failure modes that could cause events to be dropped without anyone noticing:

1. **HTTP 200 alone is not proof of ingestion.** Prior versions treated any 2xx as success. v0.3.7 parses the Application Insights ingestion response body and requires `itemsAccepted == itemsReceived` with no ingest errors.
2. **Corporate proxy captive portals** returning an HTML sign-in page at HTTP 200 are detected and rejected.
3. **Timeout raised from 5s to 15s** to survive slow enterprise proxies.
4. **Automatic buffer and flush.** Failed events (proxy blocked, offline, 5xx) are persisted to the lakehouse and re-sent on the next successful scan. Prevents proxy-blocked / offline runs from being silent losses.
5. **Persistent send log** written to disk on every attempt so PM team can debug post-hoc.
6. **`send_canary()` public helper** for scheduled health checks.
7. **10 new tests** in `tests/test_v037_telemetry_hardening.py`.

---

## Where the data lands

- **App Insights resource:** `appi-fabric-migration-scanner`
- **Resource group:** `rg-fabric-migration-scanner`
- **Subscription:** `40bf5434-ae5c-490d-a61b-de4c29313282`
- **Ingestion endpoint:** `https://westus2-2.in.applicationinsights.azure.com/v2/track`
- **Data retention:** 90 days (Application Insights default)
- **Owner:** misaacs@microsoft.com

For long-term retention past the 90-day window, we're wiring a nightly export from App Insights → OneLake (see the campaign dashboard's Advisor Impact tab). This is scoped for v0.3.8 and lives in a separate PR.

---

## Reproducing an ingest test

Any user can prove the pipeline works from their own notebook:

```python
from pq_adbc_advisor import send_canary
outcome = send_canary(source="manual-audit")
print(outcome)
# {'accepted': True, 'status': 'ok', 'detail': 'received=1 accepted=1 errors=0'}
```

If `accepted` is `False`, the returned `status`/`detail` explain why (network, captive portal, ingest drop, etc.) — that's your starting point to debug your environment's proxy or network policy.

---

## Questions

- **Am I affected if my customer uses a corporate proxy?** Only if the proxy blocks outbound traffic to `westus2-2.in.applicationinsights.azure.com` on port 443. If it does, events are automatically buffered locally and retried on the next successful send from that workspace. If the proxy is a permanent block, events accumulate in the local buffer (capped at 500 entries) — no data loss until they exceed the cap, at which point the oldest entries are silently dropped.
- **How do I check if events are being received?** `telemetry_health()` locally, plus the Advisor Impact tab on the `aka.ms/adbcinsights` dashboard for aggregate counts.
- **Can I run the tool with telemetry off?** Yes. Everything works identically — the only difference is nothing is sent upstream. The impact report renders exactly the same.
