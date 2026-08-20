# Contributing to pq-adbc-advisor

Thank you for reviewing / contributing. This document exists so you can
get productive in a few minutes.

## Repo layout

```
pq-adbc-advisor/
├── src/pq_adbc_advisor/
│   ├── __init__.py         # public API surface — everything a customer imports
│   ├── constants.py        # connector rules, migration buckets, risk levels
│   ├── mcode.py            # M-expression parser (regex, no full grammar)
│   ├── definitions.py      # extract M from TMSL/TMDL/Dataflow definitions
│   ├── fabric_api.py       # thin Fabric / Power BI REST wrappers (read-only + one refresh POST)
│   ├── discovery.py        # scan_workspace / scan_tenant
│   ├── validation.py       # validate_migration + refresh polling
│   ├── troubleshoot.py     # error-classification rule table + scan-time diagnoses
│   ├── report.py           # ImpactReport / ValidationReport with _repr_html_
│   ├── preflight.py        # preflight_check() API self-test
│   └── telemetry.py        # anonymous App Insights emission
├── tests/                  # 57 pytest tests, no live-Fabric dependency
├── examples/               # quickstart.ipynb
└── pyproject.toml
```

## Reviewer's guide

If you're doing a first-pass code review, we recommend this order:

1. **`README.md`** — customer-facing pitch (~5 min).
2. **`examples/quickstart.ipynb`** — the notebook cells a customer will run.
3. **`src/pq_adbc_advisor/constants.py`** — connector migration rules and risk
   levels. Everything downstream is data-driven off this file.
4. **`src/pq_adbc_advisor/mcode.py`** — the M-code parser. Notes on why it's
   regex-based (not a full M grammar) are in the module docstring.
5. **`src/pq_adbc_advisor/troubleshoot.py`** — the error-classification rules
   and the scan-time diagnosis logic.
6. **`src/pq_adbc_advisor/report.py`** — how results render in the notebook.
7. **`tests/test_review_regressions.py`** — regression tests for the bugs
   an earlier adversarial review found, so you can see what "we already
   fixed" looks like.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

All 57 tests should pass in under a second. Tests never hit the network.

## Design principles

1. **Diagnostic, not remediation.** The scan phase is 100 % read-only. The
   validation phase POSTs `/refreshes` because that is the only way to
   observe post-ADBC behavior — never a `PATCH` or `updateDefinition`.
2. **Data-driven rules.** Every connector, migration bucket, and error
   pattern is a data entry in `constants.py` or `troubleshoot.py`. Adding
   a new deprecation (e.g. Exchange, Hive) is a single dict.
3. **Bounded parsing.** `mcode.py` never tries to be a full M grammar. It
   strips comments and string literals, then scans call-by-call with
   arg-window bounds so Implementation from one call cannot leak into
   another.
4. **Graceful degradation.** Every REST call has a fallback — a blocked
   `/v1/connections` surfaces as `fabric_connections_error`, not a
   silent empty list.
5. **Small public API.** The customer imports one of:
   `scan_workspace`, `scan_tenant`, `validate_migration`, `preflight_check`.

## Adding a new migration campaign

Suppose Microsoft announces an Oracle ODBC → ADBC campaign. Only two files
change:

1. Add a rule to `IMPACTED_CONNECTORS` in `constants.py`:
   ```python
   {
       "kind": "Oracle",
       "m_functions": ["Oracle.Database"],
       "family": "oracle",
       "migration": f"{MIGRATION_ODBC_TO_ADBC}:oracle",
       "notes": "Oracle connector migrating from ODBC to ADBC.",
   },
   ```
2. Optionally add a new error-classification rule to `_RULES` in
   `troubleshoot.py` if Oracle has a distinctive failure signature.

## Adding a new error-classification rule

Append a dict to `_RULES` in `troubleshoot.py`. Rules are ordered — first
match wins. When you add a rule, also add a regression test in
`tests/test_troubleshoot.py` (real error text preferred).

## Commit style

Small, descriptive. When the change fixes a bug the adversarial reviewer
called out, reference the finding ID in the commit body (e.g. `r05:
bound Implementation window to current call args`).

## Reporting bugs

Open an issue or ping `adbcmigration@microsoft.com` with:
- workspace ID (redacted if sensitive),
- one M expression that repros the bug,
- expected vs actual output.


## Heads-up: token display redaction in some editors

Several editor / assistant tools (including a few internal ones) apply a
display-time redaction filter that replaces the substring `Bearer `
in **every** file view with literal asterisks. The source code on disk is
fine — this is purely a display artifact. If you see something like

```python
"Authorization": f"******",
```

in a file view, do NOT edit it — you'd be replacing correct code with
literal asterisks. Instead verify the raw bytes:

```bash
python3 -c "
with open('src/pq_adbc_advisor/fabric_api.py','rb') as f:
    for line in f.read().decode().splitlines():
        if 'uthoriz' in line:
            import base64; print(base64.b64encode(line.encode()).decode())
"
```

Decode the base64 output to confirm the file really has
`f"Bearer {access_token}"`. If you have already accidentally
written the literal `"******"` back, `git checkout` the file to restore
it.
