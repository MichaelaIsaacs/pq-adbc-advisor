# Power Query Connector Upgrade Advisor

Read-only Python library that a customer can pull from GitHub and run inside a
Fabric notebook to answer two questions:

1. **Every connector in my workspace — which are affected by the ODBC → ADBC
   migration, and which aren't?**
2. **After I flip the ADBC switch, did every connector still refresh
   successfully? If any failed, what's the likely cause and fix?**

Covers the seven connectors in the ODBC → ADBC migration: **Snowflake,
Google BigQuery (including AAD variant), Databricks, Dremio, Amazon Redshift,
Spark / HDInsight, Impala** — and lists every other connector in the workspace with
a `migration = none` marker so nothing is invisible.

The tool is **diagnostic**. It never rewrites M code, changes connections, or
triggers migrations. The `scan_workspace` / `scan_tenant` phase is fully
read-only. The `validate_migration` phase, by default, POSTs a refresh
request to each impacted semantic model so it can compare pre- and
post-migration behavior — pass `trigger_refresh=False` to run refreshes
yourself in the Fabric portal and have the tool just observe.

Built on top of the same Fabric REST patterns as the excellent
[DFG2 Migration Accelerator](https://github.com/microsoft/fabric-toolbox/tree/main/accelerators/DFG2-migration-accelerator)
in the Fabric Toolbox.

---

## Quickstart

Inside a Fabric notebook cell:

```python
%pip install git+https://github.com/MichaelaIsaacs/pq-adbc-advisor.git
from pq_adbc_advisor import scan_workspace, validate_migration

# Phase 1 — BEFORE flipping the ADBC switch
baseline = scan_workspace()          # default: skip non-migrating connectors
baseline                             # renders inline in the notebook

# ... flip the tenant / workspace ADBC switch ...

# Phase 2 — AFTER flipping the switch
result = validate_migration(baseline)
result                               # inline results with per-failure fix
```

You do **not** need to call `to_html(path)` — the report renders inline as
soon as you evaluate the variable in a notebook cell. `to_html()` is
optional for CSAs who want to share the report offline.

For a full inventory (including SQL Server / Excel / Web / other
non-migrating connectors):

```python
baseline = scan_workspace(include_non_migrating=True)
```

Or toggle after the fact:

```python
baseline.show_non_migrating = True
baseline
```

For a tenant-wide scan (requires Fabric admin):

```python
from pq_adbc_advisor import scan_tenant
report = scan_tenant()
report
```

## Cost + runtime

The scan phase is **read-only** and does not execute customer queries. It
reads item definitions via the Fabric REST API and scans them for M
patterns. Nothing is charged to Fabric capacity for the scan.

Typical runtimes with the default `max_parallel=10`:

| Workspace size | Runtime (v0.2.3, REST only) | Runtime (v0.2.4, sempy on) |
|---|---|---|
| Small (< 20 artifacts) | < 15 seconds | < 10 seconds |
| Medium (20–80 artifacts) | 30–90 seconds | 15–45 seconds |
| Large (100+ artifacts, tenant-wide scan) | 2–5 minutes | 1–3 minutes |

The **sempy fast path** shipped in v0.2.4 skips the Fabric REST
long-running-operation entirely for semantic models when running inside
a Fabric notebook (it uses the same `sempy.fabric` library that powers
Pat Mahoney's DFG2 Migration Accelerator). Look for the "sempy fast
path · N hits" badge in the report header to confirm it engaged.

**Rate limits.** All Fabric + Power BI REST calls retry on 429/503,
honoring the server's `Retry-After` hint and capping at 5 attempts.
If a scan is unusually slow, `verbose=True` (default) surfaces retry
activity. To back off further, drop `max_parallel` (e.g.
`scan_workspace(max_parallel=5)`).

**Coverage disclosure.** The report shows a "Coverage" KPI card and a
"Scope of this scan" footer listing which item types were inspected
(SemanticModel, Dataset, Dataflow) vs. not inspected (Data Pipeline,
Notebook, Report, etc.). A clean scan on a workspace that only
contains Reports is honestly reported as low coverage, not a green
light.

The **validation** phase triggers a real refresh on every impacted
semantic model, so it does consume Fabric capacity — one refresh per
model, run in parallel up to `max_parallel`.

---

## What the impact report tells you

**One row per connector call** in the workspace:

| column | meaning |
|--------|---------|
| `item_type` | SemanticModel, Dataset, Dataflow |
| `connector_kind` | Friendly name (Snowflake, SQL Server, Salesforce, Web, ...) |
| `m_function` | Exact M identifier (e.g. `Snowflake.Databases`) |
| `migration` | `odbc_to_adbc:snowflake`, `odbc_to_adbc:redshift`, ..., or `none` |
| `is_migrating` | Boolean shortcut |
| `implementation` | `1.0` (ODBC pinned), `2.0` (ADBC pinned), or blank |
| `endpoint_hint` | First string literal (server / URL) if we could find one |
| `has_gateway` | Whether the dataset is bound to a gateway |
| `risk` | high / medium / low / unknown / **na** |
| `excerpt` | Short slice of the M expression for eyeballing |

Plus a second table listing every shared **Fabric Connection** (`/v1/connections`)
so customers see the workspace's first-class connection inventory too.

### Risk logic

- **high** — `Implementation="1.0"` pinned **and** no gateway → will silently
  break when ODBC is disabled in the service.
- **medium** — `Implementation="1.0"` pinned **with** a gateway → will keep
  working via the gateway, but you may want to migrate anyway.
- **low** — `Implementation` not pinned → the tenant / workspace ADBC switch
  handles the migration for you.
- **unknown** — pinning found but gateway state couldn't be determined.
- **na** — connector isn't part of any current migration effort.

---

## Validation + troubleshooting

`validate_migration(baseline)` does the following for every semantic model
in the baseline (skips dataflows for now):

1. Reads its last refresh time and duration.
2. POSTs a new refresh (skip with `trigger_refresh=False` to run
   the refresh yourself).
3. Polls `/refreshes` until the new refresh completes or fails.
4. Compares status + duration → `passed`, `failed`, `no_new_refresh`,
   `refresh_not_triggered`, `skipped`.
5. **For every failure, classifies the error text against a rules table**
   and attaches a `Diagnosis` with:
   - `issue` — short category (e.g. "Authentication failed")
   - `likely_cause` — one-sentence explanation
   - `suggested_actions` — ordered list of things to try
   - `docs` — link to the relevant Learn page (when available)

Known failure classes today:

- ADBC driver missing (gateway version too old)
- Authentication failed (creds, OAuth2 flow, MSAL)
- Gateway offline or unreachable
- Legacy ODBC path still in use (`Implementation="1.0"` leftover)
- Schema drift between ODBC and ADBC (columns, types, VARIANT, timestamps)
- Query timeout
- TLS / certificate error
- Backend rate limit (Snowflake warehouse quota, BQ slots)
- Network / DNS failure
- Uncategorized fallback with the raw error surfaced

Add new rules by extending `troubleshoot.py`'s `_RULES` list — no engine
changes needed.

By default we validate **every** artifact (not just migrating ones) so
side effects of the tenant switch surface. Pass `only_migrating=True` to
narrow the scope.

---

## Telemetry

The advisor emits **two events** to Application Insights per run: one on
scan, one on validation. The events are designed so the Power Query PM
team can see (1) adoption, (2) which tenant is running the tool, and
(3) whether the customer's exposure is going down over time.

### What's sent

**Every event:**
- `toolVersion`, `python`, `platform`
- `tenantId` — raw AAD tenant GUID (used to join to MSSales for TPID
  in downstream analysis)
- `workspaceId` — raw Fabric workspace GUID
- `runId` — per-run UUID for deduplication

**`pq_adbc_advisor_scan_summary` also carries:**
- `runCount` (1 for the first scan of this workspace, 2 for the second …)
- `isFirstRun` (true / false)
- `firstRunAt` (ISO timestamp of this workspace's first scan)
- **First-run baseline** locked in on the very first scan:
  `firstRiskHigh`, `firstRiskMedium`, `firstRiskLow`, `firstRiskNa`,
  `firstCustomDsn`, `firstTotalCalls`, `firstMigratingArtifacts`,
  `firstPinnedOdbc`, and one `firstConnector_<Kind>` per connector.
- **Current-run counters** from this exact scan:
  `currentRiskHigh`, `currentRiskMedium`, …, `currentPinnedOdbc`,
  plus one `counter_<Kind>` per connector.

Because both baselines are in every event, a single KQL query renders
"improvement":

```kusto
customEvents
| where name == "pq_adbc_advisor_scan_summary"
| project tenantId=tostring(customDimensions.tenantId),
          workspaceId=tostring(customDimensions.workspaceId),
          runNumber=toint(customDimensions.runCount),
          firstHigh=toint(customDimensions.firstRiskHigh),
          currentHigh=toint(customDimensions.currentRiskHigh),
          deltaHigh=toint(customDimensions.firstRiskHigh) - toint(customDimensions.currentRiskHigh),
          firstPinnedOdbc=toint(customDimensions.firstPinnedOdbc),
          currentPinnedOdbc=toint(customDimensions.currentPinnedOdbc)
| where tenantId != ""
| summarize arg_max(runNumber, *) by tenantId, workspaceId
| order by deltaHigh desc
```

State that makes `runCount` work is persisted at
`/lakehouse/default/Files/pq_adbc_advisor_state.json` in the workspace's
default lakehouse. If no default lakehouse is attached, every run
reports `runCount=1` and the first-run counters equal the current-run
counters.

### What's never sent

- workspace names, item / model names, endpoint hints (server URLs)
- M code, credentials, connection strings, gateway names
- refresh error message bodies (only the count per diagnosed issue kind)
- user identity, OneLake paths, dataset IDs

### Opting out

The first time you run the tool in a workspace you'll see a one-time notice
in the notebook output describing what's collected. To opt out at any time
(the choice persists across kernel restarts):

```python
from pq_adbc_advisor import disable_telemetry
disable_telemetry()
```

To re-enable later:

```python
from pq_adbc_advisor import enable_telemetry
enable_telemetry()
```

To check the current state:

```python
from pq_adbc_advisor import telemetry_status
telemetry_status()
# -> {'endpoint_configured': True, 'resource': 'appi-fabric-migration-scanner',
#     'env_var_off': False, 'persistent_opt_out': False, 'effectively_enabled': True}
```

Two additional opt-out paths are supported for CI and non-notebook contexts:

```python
baseline = scan_workspace(telemetry_enabled=False)  # per-call
```

```bash
export PQ_ADBC_ADVISOR_TELEMETRY=off                # env var
```

### Anonymizing tenant/workspace IDs

If a customer wants adoption to still be tracked but doesn't want the
raw AAD guid sent:

```bash
export PQ_ADBC_ADVISOR_ANONYMIZE=1
```

This substitutes a 16-character SHA-256 prefix for `tenantId` and
`workspaceId` so runs from the same tenant can still be joined without
revealing the tenant.

---

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The unit tests do not require a Fabric tenant; they exercise the M-code
parser, report objects, and troubleshoot rules against fixture data.

---

## License

MIT. See `LICENSE`.
