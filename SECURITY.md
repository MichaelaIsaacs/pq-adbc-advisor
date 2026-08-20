# Security Policy

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security concerns.

Email `adbcmigration@microsoft.com` with the details. Include:

* A description of the issue and its potential impact.
* Steps to reproduce (a minimal notebook cell is ideal).
* Any known workarounds.

You should receive a response within two business days. If you need
to escalate, contact the Power Query PM team directly.

## What this tool touches

`pq-adbc-advisor` runs inside a Fabric notebook with the caller's
delegated user token. It:

* **Reads** semantic-model / dataflow definitions via
  `POST getDefinition` and `GET dataset/datasources`.
* **Reads** shared Fabric Connections via `GET /v1/connections`.
* Optionally **triggers refreshes** during `validate_migration`,
  scoped to datasets the caller explicitly opts in to validating.
* Emits anonymous scan metrics to a Microsoft-owned Application
  Insights resource. Tenant GUIDs are SHA-256 hashed by default;
  raw tenant GUID is only sent when the customer sets
  `PQ_ADBC_ADVISOR_TENANT_RAW=1`. See `telemetry.py` for the exact
  envelope shape.

## What it does not touch

* No credentials, secrets, or cookies.
* No workspace mutations outside the explicit `trigger_refresh` path.
* No files outside `/lakehouse/default/Files/pq_adbc_advisor_state.json`
  and `/tmp/`.
* No outbound network traffic other than Fabric / PBI Graph APIs and
  the App Insights ingestion endpoint.

## Auth model

The tool uses the delegated user's Power BI token acquired via
`notebookutils.credentials.getToken("pbi")`. Callers running outside
Fabric must supply a token explicitly. There is no service principal
path today — see the ARCHITECTURE.md ADR-list for the follow-up.
