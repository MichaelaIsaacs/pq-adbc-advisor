# Architecture

This document captures the shape of the system and the ADRs
(architecture decision records) behind the non-obvious choices. Read
this before proposing large changes.

## Runtime shape

```
┌────────────────────────────┐
│  Fabric notebook (customer) │
└──────────────┬─────────────┘
               │
               │  scan_workspace() / validate_migration()
               ▼
┌──────────────────────────────────────────────────────────┐
│  pq_adbc_advisor.discovery                               │
│    ├── Phase 1: parallel definition fetch + M scan       │
│    ├── Phase 2: filter + gateway lookups                 │
│    └── Phase 3: Fabric Connections enumeration           │
└──────┬──────────────────────────────┬────────────────────┘
       │                              │
       │ sempy fast path              │ REST fallback
       ▼                              ▼
┌──────────────────┐        ┌────────────────────────┐
│  sempy.fabric    │        │  fabric_api            │
│  list_partitions │        │  _request_with_retry   │
│  list_expressions│        │  _await_lro            │
└──────────────────┘        └───────────┬────────────┘
                                        │
                                        ▼
                            ┌───────────────────────┐
                            │  Fabric + PBI REST    │
                            │  api.fabric.microsoft │
                            │  api.powerbi.com      │
                            └───────────────────────┘
```

Emitted results:

* `ImpactReport` — HTML-rendered inline; pandas DataFrame; JSON summary.
* `ValidationReport` — one row per validated dataset with pass/fail and
  a `Diagnosis` from `troubleshoot.py`.
* Anonymous telemetry envelope → `appi-fabric-migration-scanner` in the
  PQ/M/Dataflows Adhoc Testing subscription.

## ADRs

### ADR-001: Regex M parser, not a real grammar

**Context.** M is a first-class language with a public grammar, but a
grammar-driven parser adds a ~500KB dependency (or a pure-Python
implementation whose CPU cost dominates the scan on large workspaces)
and doesn't buy us signal we can't already get from bounded, string-
aware regex.

**Decision.** Ship a bounded regex parser (`mcode.py`) that:

* strips comments while respecting `#""` string literals;
* walks arg windows with string-aware paren matching so a URL
  containing `(` doesn't derail argument extraction;
* stops at reasonable arg-length ceilings so a pathological M script
  cannot blow up scan time.

**Consequences.** We deliberately do not detect:

* String-concatenated function calls (`ident & "(""p"")"`);
* Wrapper functions that shell out to a connector at call time;
* Dynamic reflection via `Value.NativeQuery`.

These blind spots are documented in `mcode.py`; the `troubleshoot.py`
guide points customers to raise them if they hit an unclassified
failure.

### ADR-002: REST fallback preserved even after sempy adoption

**Context.** `sempy.fabric` is the natural fast path in Fabric (Pat
Mahoney's DFG2 accelerator uses it) but it is:

1. Only present inside Fabric notebooks (not local dev, not CI);
2. Versioned independently of the Fabric portal (columns rename
   between minor sempy releases);
3. Currently only covers semantic models — dataflows still need REST.

**Decision.** Try `sempy.fabric` first for semantic models; fall
through to REST `getDefinition` on any failure. The report carries
`used_sempy_path` and `sempy_hits` for post-hoc measurement, and
telemetry surfaces both.

**Consequences.** Two codepaths must stay in sync. `discovery.py`
funnels both through the same `find_all_connectors` reducer to
minimize drift.

### ADR-003: 429 / 503 retry wrapper, not per-call custom logic

**Context.** Fabric and Power BI both throttle 429 with `Retry-After`
hints, and both occasionally return 503 during regional rollouts.
Prior versions silently returned None on throttle, which caused whole
artifacts to disappear from the report — worst possible failure mode.

**Decision.** All REST calls go through `_request_with_retry`. It
honors `Retry-After` when present, otherwise applies exponential
backoff with full jitter, and caps attempts at `RETRY_MAX_ATTEMPTS = 5`
so a throttled tenant can't make the scan hang indefinitely.

**Consequences.** Worst-case scan time on a heavily throttled tenant
is bounded but slower. Callers should still watch `verbose=True`
output for elevated retry counts.

### ADR-004: State in the default lakehouse, not `~/.config`

**Context.** Fabric notebook filesystems are ephemeral. The only
persistent, per-workspace location we can write from a notebook is
`/lakehouse/default/Files/`.

**Decision.** Persist opt-out flag + first-run baseline snapshot to
`/lakehouse/default/Files/pq_adbc_advisor_state.json` using an atomic
temp+rename with an optional POSIX advisory lock.

**Consequences.**

* If the workspace has no default lakehouse attached, every run
  reports as a "first run" (documented behavior).
* Concurrent notebooks against the same lakehouse cannot corrupt the
  state file — but they can trigger a benign duplicate first-run
  snapshot if two scans race the initial write.

### ADR-005: Inline HTML rendering, not saved files

**Context.** David Coe's real-world test: writing the report HTML to
`/lakehouse/default/Files/adbc_impact.html` succeeds but the customer
can't open the file through the Fabric Files pane.

**Decision.** The primary rendering path is `_repr_html_`, invoked by
just typing `baseline` in a notebook cell. `to_html(path)` still
exists for CSA hand-offs but silently redirects `/lakehouse/` paths to
`/tmp/` and prints a hint pointing the customer at inline rendering.

**Consequences.** Customers who genuinely want the file in the
lakehouse must copy it themselves via `notebookutils.fs.cp`. This is
a deliberate friction point; the inline path is the recommended one.

### ADR-006: Threading model — one ThreadPoolExecutor per phase

**Context.** Fabric REST calls are I/O bound. Parallelism gives us
close to `max_parallel`× speedup until we hit the tenant's 429 ceiling.

**Decision.** Use `concurrent.futures.ThreadPoolExecutor` with
`DEFAULT_MAX_PARALLEL = 10` (empirically well under Fabric's per-
identity limit). One pool per phase (definition fetch, gateway
lookup) so a slow gateway lookup can't starve definition workers.

**Consequences.** GIL is fine — every worker is blocked on a socket.
Higher `max_parallel` values are supported but hit throttle sooner;
we let the caller override.

## Coverage disclosure

`ImpactReport.coverage()` computes a score from `observed_types` —
the set of item types seen during discovery. Item types we do not yet
parse (Data Pipeline, Notebook, MLModel, etc.) count against the
coverage score so a clean report on a workspace of only Reports is
correctly flagged as "we didn't inspect anything" rather than
silently returning a green light.

The scan's HTML render surfaces both the coverage percentage as a KPI
and the exact list of inspected vs not-inspected types in the "Scope
of this scan" section at the bottom.

## Not in scope (yet)

* Data Pipeline connector call parsing (tracked as a follow-up).
* KQL Queryset scanning.
* Fabric Warehouse T-SQL parsing.
* Any write path beyond `trigger_refresh` in `validation.py`.

## ADRs (v0.3.0)

### ADR-007: Data Pipeline inspection via JSON activity walk, not M

**Context.** Fabric Data Pipelines are the ADF descendant. Their
definitions are JSON, not M. Nothing in the M parser is useful here.

**Decision.** Add a separate `pipeline_scan.py` that walks
`properties.activities` recursively (including `ForEach`,
`IfCondition`, `Until`, `Switch` containers) and emits raw
`ConnectionRef` records. Resolve those refs against the Fabric
Connections listing to derive connector kind, then convert to
`ConnectorCall` so the existing report/risk/troubleshoot pipeline
works unchanged.

**Consequences.**
* Discovery ordering changes — Fabric Connections must be fetched
  BEFORE the artifact filter phase, since pipeline refs need the ID
  → kind mapping to resolve.
* Unresolvable connection IDs (permissions blocked, cross-workspace)
  render as `Unresolved connection` — a coverage gap the customer
  can see, not a silent green light.
* Gateway status is unknowable for pipeline refs without an extra
  API call per connection; `has_gateway=None` until we add that.

### ADR-008: Service Principal auth via MSAL, optional install

**Context.** `notebookutils.credentials.getToken` is delegated-user
only. A customer running a scheduled scan (or CI test against a lab
workspace) has no way to authenticate.

**Decision.** New `auth.py` module. Detects
`PQ_ADBC_ADVISOR_SP_*` env vars; when present, uses MSAL's
`ConfidentialClientApplication` with either a client secret or a PEM
certificate. `msal` and `cryptography` are optional pip extras
(`pip install pq-adbc-advisor[sp]`) so notebook users don't pay for
them.

**Consequences.**
* Auth precedence is now three levels: explicit kwarg > SP env > notebook.
* Documented in README and SECURITY.md. The SP still needs the same
  Fabric permissions the delegated user would.

### ADR-009: HTML pagination via row caps, not lazy loading

**Context.** A workspace with 500 connector calls used to render a
700KB+ DOM. That's still tolerable in a Fabric notebook but not
paginated, and there was no ceiling — a pathological workspace could
have hung the kernel.

**Decision.** Hard cap: 25 rows per connector group, 400 rows total.
Beyond either cap, an inline note points the customer at
`baseline.to_dataframe()` for the complete list. No lazy loading /
JavaScript — the report has to render statically since it's a
`_repr_html_` snapshot.

**Consequences.** Stress test confirms 500 calls render in <1s under
500KB. Customers who want the full inventory drop to the DataFrame
view. That's the same tradeoff a spreadsheet takes.
