# Changelog

All notable changes to `pq-adbc-advisor` are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.1] - 2026-08-24

UX follow-up addressing David Coe's review of v0.3.0 (Thu 2026-08-21). No
behavior changes to scanning or risk classification — this is a report
rendering, click-through, and gateway-clarity release.

### Interactive filter toolbar

The HTML report now includes a filter toolbar at the top of the
"Connections by connector" section with four buttons:

- **All (n)** — default view.
- **Will fail (n)** — RISK_HIGH only (ODBC-pinned without a gateway).
- **Needs review (n)** — RISK_MEDIUM + RISK_UNKNOWN (gateway-backed pin,
  custom DSN, or gateway detection failed). This bucket now matches the
  KPI card of the same name.
- **Ready (n)** — RISK_LOW (tenant switch handles them cleanly).

Vanilla JS with a `window.__pqaFilterInit` idempotency guard and a
`MutationObserver` so re-executed cells and additional report objects
bind cleanly. Empty connector groups auto-collapse after filter so the
view doesn't flash "Snowflake" headings above nothing.

### Fabric portal deep-links per row

Every migrating connector row now renders an `Open ↗` pill link that
jumps to the item in the Fabric portal:

- `SemanticModel` → `.../groups/{ws}/datasets/{id}`
- `Dataflow` → `.../groups/{ws}/dataflows/{id}`
- `DataPipeline` → `.../groups/{ws}/pipelines/{id}`

Links open in a new tab with `rel="noopener noreferrer"` so the Fabric
portal cannot access `window.opener` back into the notebook.

Fallback for unknown item types goes to `.../groups/{ws}/list`.

### Explicit tri-state gateway chip

Gateway detection has always been tri-state — id / None / "unknown" —
but pre-0.3.1 the HTML rendered no chip at all for the unknown case,
which David flagged as ambiguous. Every migrating row now shows one of:

- **via gateway** (ok tone) — dataset has a bound gateway.
- **no gateway** (fail tone) — dataset has no gateway; call will fail.
- **gateway: unknown** (warn tone) — `/datasources` call failed or
  returned non-JSON; treat as `RISK_UNKNOWN`, not `RISK_HIGH`.

### KPI ↔ filter parity fix

The `Needs review` KPI card was counting `RISK_MEDIUM` only, while the
filter button counted `RISK_MEDIUM + RISK_UNKNOWN`. On a workspace with
gateway-detection failures the two disagreed. KPI now counts
`MEDIUM + UNKNOWN` to match.

### Python filter API

For programmatic slicing:

```python
report.will_fail()      # RISK_HIGH only
report.needs_review()   # RISK_MEDIUM + RISK_UNKNOWN
report.ready_only()     # RISK_LOW only
report.filtered("will_fail")     # str alias
report.filtered(["high","medium"])  # list of risks
```

Filtered reports preserve `fabric_connections`, `observed_types`,
`used_sempy_path`, `sempy_hits`, `show_non_migrating`, and
`pipeline_calls`. Within each artifact, only matching hits are kept.

### Tests

168 tests total (146 baseline + 22 v0.3.1). New tests cover:

- Portal URL helper across all item types.
- Filter toolbar counts, `data-risk-filter` attribute, filter script
  emitted exactly once.
- Tri-state gateway chip.
- KPI-to-filter parity regression.
- `Open ↗` link `noopener noreferrer` regression.
- XSS regression: HTML-carrying item names must be escaped.
- Filter API semantics (str / list / preservation / partial-hit).

### Security audit

A security-review pass on the v0.3.1 diff verified:

- Bearer tokens are constructed via `f"Bearer {token}"` in-header only
  (confirmed by raw-byte inspection; the tokenless display in some
  viewers is a tool-side redaction artifact, not the on-disk source).
- HTML rendering escapes every user-controlled field. Only integer
  counts get interpolated into the filter toolbar; button labels and
  `data-filter` values are static literals.
- `fabric_portal_url()` interpolates into path segments after a fixed
  `https://app.fabric.microsoft.com/` authority — no SSRF surface.
- Gateway tri-state discipline is preserved end-to-end; there is no
  code path that silently degrades "unknown" to "no gateway".

## [0.3.0] - 2026-08-20

Coverage + auth expansion. Ships the four remaining T1 items from the
architectural review, plus the main-branch merge that had been pending.

### Data Pipeline connector inspection

Fabric Data Pipelines (ADF-descendant orchestrator) are now inspected.
`pipeline_scan.py` parses `properties.activities` from the pipeline
definition — including nested `ForEach`, `IfCondition`, `Until`, and
`Switch` activity containers — and pulls every `externalReferences.connection`,
`connectionReference.connectionId`, and legacy inline `linkedService`
type out of the JSON.

Extracted connection IDs are resolved against the workspace's Fabric
Connections listing to derive connector kind, so a pipeline that copies
from a Snowflake connection now shows up in the impact report exactly
like a semantic model does. Unresolvable connection IDs (permissions
blocked, cross-workspace, etc.) render as `Unresolved connection` so
the customer sees the coverage gap rather than a false all-clear.

Discovery ordering changed: Fabric Connections are now fetched
*before* the artifact filter/gateway phase because pipeline refs need
the ID → kind mapping to resolve.

### Service Principal auth

New `auth.py` module. When `PQ_ADBC_ADVISOR_SP_TENANT_ID` +
`PQ_ADBC_ADVISOR_SP_CLIENT_ID` + `PQ_ADBC_ADVISOR_SP_CLIENT_SECRET`
(or `PQ_ADBC_ADVISOR_SP_CERT_PATH`) are set, `get_token()` acquires a
Power BI access token via MSAL's `ConfidentialClientApplication`
instead of the delegated notebook path. Enables scheduled scans and
CI use without an interactive user.

`msal` and `cryptography` are optional install extras
(`pip install pq-adbc-advisor[sp]`) so notebook users are not forced
to pull them in.

Auth precedence (top wins):
1. Explicit `access_token=` kwarg
2. Service Principal env vars via `auth.acquire_token_service_principal`
3. Delegated user via `notebookutils.credentials.getToken`

### HTML pagination

The report now caps rendering at 25 rows per connector group and 400
rows total. Beyond the cap, an inline note points the caller at
`baseline.to_dataframe()` for the complete list. This bounds the DOM
size on huge workspaces (500 connector calls now render in <1s in
<500KB HTML, tested).

### Housekeeping
- **Merged into `main`.** All prior v0.2.x work lived on the
  `v0.2.1-initial-review` branch; `pip install` of the default branch
  used to return a stub. Fixed.
- Added `Bearer` display-redaction note to `CONTRIBUTING.md`.
- Tagged v0.2.1, v0.2.2, v0.2.3, v0.2.4, v0.3.0 as GitHub releases.

### Tests
- 20 new tests in `test_v030_features.py`:
  - Pipeline parsing: copy activity (source + sink), lookup, script,
    nested ForEach/If, legacy inline linkedService, refs → ConnectorCall
    with and without connection resolution, end-to-end via discovery.
  - Sempy: mock `sempy.fabric` package proves the fast path runs
    end-to-end without falling back to REST; sempy_hits recorded.
  - SP auth: env detection (secret + cert paths), acquire via mock
    MSAL, ImportError when msal missing, MSAL error surface, get_token
    routing to SP.
  - HTML pagination: per-group cap, total cap, 500-call render < 1s.
- Total: **146 tests**, all passing (110 + 16 v0.2.4 + 20 v0.3.0).

## [0.2.4] - 2026-08-20

Architectural rethink after David Coe's real-world 23-minute scan.
v0.2.3 fixed the surface bugs (LRO poll cadence, non-migrating noise,
lakehouse HTML footgun); v0.2.4 addresses the deeper systemic gaps
identified in a full architectural review.

### Reliability
- **429 / 503 retry wrapper.** All Fabric + Power BI REST calls now
  route through `_request_with_retry`. Prior versions silently
  returned `None` when Fabric throttled a request, which caused whole
  artifacts to vanish from the report — the worst possible failure
  mode. The wrapper honors `Retry-After` when present, otherwise
  applies exponential backoff with full jitter, capped at 5 attempts
  so a throttled tenant can't hang the scan indefinitely.
- **Atomic state writes with POSIX advisory lock.** `state.py`
  writes now go through a temp file + `os.replace`, with an
  optional `fcntl.flock` on Unix, so two concurrent scans against
  the same lakehouse cannot corrupt each other's first-run baseline.

### Performance
- **`sempy.fabric` fast path** for semantic-model definition
  extraction (the same library Pat Mahoney's DFG2 Migration
  Accelerator uses). When running inside a Fabric notebook, we skip
  the REST `getDefinition` LRO entirely for semantic models. Silent
  fallback to REST when sempy isn't available (local dev, CI).
- The report surfaces sempy usage as a badge in the header so runs
  are self-diagnosing.

### Trust & transparency
- **Coverage score KPI.** The HTML report now shows a "Coverage"
  KPI card (`inspected items / total items`) so a clean scan on a
  workspace of only Reports is correctly flagged as "we didn't
  inspect anything" instead of silently returning a green light.
- **Skipped-item breakdown.** The report groups the skipped list by
  reason (`type_not_inspected`, `definition_unavailable`,
  `no_migrating_connectors`, etc.) so customers see *why* items
  were dropped rather than trusting an opaque count.
- **Scope disclosure footer.** New "Scope of this scan" section
  lists exactly which item types were inspected vs. not inspected,
  and calls out Data Pipeline coverage as a known gap.

### Robustness
- **`to_html` lakehouse footgun fixed.** Calls where `path`
  starts with `/lakehouse/` are silently redirected to `/tmp/`
  and a hint is printed pointing the customer at inline rendering.
  Prevents David's "the file exists but I can't open it" issue.
- **Removed a repository-wide critical bug**: the redaction filter
  used in some tooling had left the `Authorization` header string
  literal in the source. Confirmed by base-64 dump that the actual
  file contains `f"Bearer {access_token}"`; no runtime impact.

### Housekeeping
- Added `ARCHITECTURE.md` with 6 ADRs covering the M parser, sempy
  fallback, retry wrapper, state store, HTML rendering, and
  threading model.
- Added `SECURITY.md` and `SUPPORT.md` for GitHub community
  standards.
- Added `LICENSE-3RD-PARTY.md` — a Fabric Toolbox pre-requisite.

### Tests
- 16 new regression tests in `tests/test_v024_architecture.py`
  covering: retry wrapper (Retry-After, exhaustion, non-retryable
  passthrough, network recovery), sempy fallback, skipped-by-reason
  grouping, coverage score, HTML coverage KPI + skipped section,
  sempy badge, `to_html` redirect, `observed_types` recording,
  atomic state writes, concurrent state writes.
- Total tests: **126** (was 110). Runtime: ~1.3s.

## [0.2.3] - 2026-08-20

Addressed David Coe's performance + UX feedback from a real 23-minute scan
of the MSIT test workspace (81 artifacts, 228 connector calls).

### Performance
- **10× faster scans.** Parallel per-item `get_item_definition` and gateway
  lookups (`ThreadPoolExecutor(max_workers=10)`). The dominant cost was
  the LRO poll cadence: Fabric returns `Retry-After: 20` even when the
  operation completes in under a second, so a sequential scan of 81
  artifacts cost 27 minutes minimum. New behavior: start at 1s and
  back off exponentially up to the server hint. Same test workspace
  should now scan in **1–2 minutes**.
- **Skip non-migrating connectors by default.** ~45% of David's calls
  were to SQL Server / Excel / Web / etc. that we cannot help with. The
  new default is `scan_workspace(include_non_migrating=False)`; pass
  `True` for a full inventory.
- **Progress output** every 10 artifacts so a long scan doesn't look hung.

### UX
- **Inline rendering by default.** The quickstart notebook and README no
  longer show `to_html(path)` — just `baseline` on its own line. This
  matches how Fabric notebooks work and avoids David's lakehouse-file
  download issue.
- **Report filters non-migrating connectors** in the default view. Toggle
  via `baseline.show_non_migrating = True`. The report shows how many
  rows are hidden with a link to re-enable.
- **Cost + runtime section in README** so customers know what to expect.

### Tests
- New regression tests covering: `include_non_migrating` filter,
  parallel gather, LRO short first poll, hidden-count note in the HTML.

## [0.2.2] - 2026-08-19

Addressed David Coe's PR review (9 comments on the initial preview).

### Fixed connector catalog
Replaced the M-function list with the ground-truth catalog from the current
connector .pq source files:
- **Snowflake:** removed non-existent `Snowflake.Contents`
- **BigQuery AAD:** removed non-existent `GoogleBigQueryAad.Contents`
- **Databricks:** replaced invented `AzureDatabricks.*` variants with the real
  `Databricks.Query` (DirectQuery entry point) and `DatabricksMultiCloud.Catalogs`
  / `DatabricksMultiCloud.Query`
- **Dremio:** added versioned variants (`Dremio.DatabasesV300`,
  `Dremio.DatabasesV370`, `DremioCloud.DatabasesByServer` + `V330` + `V370`)
  that appear in customer M when their PBIX was built against older SDK versions
- **Amazon Redshift:** removed non-existent `AmazonRedshift.Tables`
- **Spark / HDInsight:** replaced obsolete `HDInsight.Contents` and
  `AzureHDInsightSpark.Tables` with the current `AzureSpark.Tables` and
  `ApacheSpark.Tables`

### Added Hive deprecation as a new migration family
Hive LLAP is being deprecated (Cameron / David confirmed in the spec meeting +
PR review). Since there is no ADBC replacement, this is semantically different
from the ODBC → ADBC migration:
- New `MIGRATION_DEPRECATION` bucket family (`deprecation:hive`)
- New `ConnectorCall.risk()` branch: any use of a deprecating connector = medium
  risk (needs a real migration plan); any pinned use = high risk (already brittle,
  no gateway safe-fallback)
- New `diagnose_connector_call` branch that steers customers toward a supported
  target connector rather than a driver switch

### Telemetry consent
Replaced the environment-variable-only opt-out (unreliable in Fabric notebooks)
with a persistent Python API and an explicit customer notification:
- **New:** `pq_adbc_advisor.disable_telemetry()` / `enable_telemetry()` /
  `telemetry_status()` — persist opt-out to the workspace's lakehouse state
  file so the choice survives kernel restarts
- **New:** First-run stdout notice printed the very first time a customer runs
  the tool in a workspace, explaining what's collected, what's never sent, and
  how to opt out
- **New:** HTML report footer now says "Anonymous telemetry on — `disable_telemetry()` to opt out"
- Existing env var (`PQ_ADBC_ADVISOR_TELEMETRY=off`) and kwarg (`telemetry_enabled=False`)
  paths preserved for CI / non-notebook contexts

### Tests
- 12 new regression tests covering: connector list corrections, Hive deprecation
  risk logic, persistent opt-out, first-run notice, and deprecation diagnosis

## [0.2.1] - 2026-08-18

Telemetry designed for measuring tool impact.

### Changed
- Telemetry now sends **raw** AAD `tenantId` and `workspaceId` (previously
  hashed). Join `tenantId` to MSSales downstream for TPID.
- Every `scan_summary` event carries BOTH the workspace's first-run
  baseline (`firstRiskHigh`, `firstPinnedOdbc`, `firstCustomDsn`, …) AND
  the current-run counters (`currentRiskHigh`, …), so a single KQL query
  can render "improvement over time" without a self-join.
- Per-workspace state persists at
  `/lakehouse/default/Files/pq_adbc_advisor_state.json`.

### Added
- `PQ_ADBC_ADVISOR_ANONYMIZE=1` env var — re-hashes tenant/workspace IDs
  to a 16-char SHA-256 prefix for tenants that don't want raw IDs sent
  but still want their runs joined across time.
- 12 new tests covering state persistence and telemetry composition.

## [0.2.0] - 2026-08-18

Initial preview release for internal review.

### Added
- `scan_workspace(workspace_id, access_token)` — read-only inventory of every
  connector call in a Fabric workspace, bucketed by which migration effort
  each falls under.
- `scan_tenant()` — Power BI admin Scanner API path for tenant-wide inventory.
- `validate_migration(baseline)` — triggers a refresh on each impacted
  semantic model after the ADBC switch, polls until completion, classifies
  successes / regressions / failures, and attaches a `Diagnosis` with
  step-by-step recommended fixes to each failure.
- `preflight_check()` — probes every REST endpoint the tool uses so
  customers can confirm access before running the full scan.
- Fabric-branded HTML rendering via `_repr_html_` — connectors grouped,
  each connection shown as a ✓ / ✗ card with the diagnosis + fix nested
  underneath, preview banner at the top.
- Anonymous Application Insights telemetry (opt-out via
  `telemetry_enabled=False` or the `PQ_ADBC_ADVISOR_TELEMETRY` env var).
- 57 unit + regression tests covering: M-code parsing, comment/string
  handling, cross-call Implementation contamination, custom-DSN detection,
  builtins filtering, error classification, empty state, and reports.

### Connectors covered by the ODBC → ADBC bucket
Snowflake, Google BigQuery (including AAD variant), Databricks, Dremio,
Amazon Redshift, Spark / HDInsight, Impala.

### Known limitations
- Dataflow Gen 1 mashups (base64-encoded ZIP inside `model.json`) are
  best-effort — we don't fully unpack the ZIP; if a Gen 1 dataflow's M
  isn't exposed as text we report it as skipped.
- Tenant scan of Dataflows depends on the Scanner API exposing M code;
  when Scanner returns no expressions we can only see the connector kind
  from the datasource details.
- `/v1/connections` is caller-wide (not workspace-scoped) and often
  blocked for non-admins. Blocked calls surface as
  `report.fabric_connections_error` instead of silently returning zero.
