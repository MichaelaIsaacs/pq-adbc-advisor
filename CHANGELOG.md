# Changelog

All notable changes to `pq-adbc-advisor` are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
