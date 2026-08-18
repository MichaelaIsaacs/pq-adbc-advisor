# pq-adbc-advisor

Preview release of the Power Query Connector Upgrade Advisor for the ODBC → ADBC migration.

The initial code is on the **v0.2.1-initial-review** branch (open PR).

- Read-only Fabric notebook tool
- Scans every workspace connector, buckets by migration effort
- Validates each refresh after the ADBC switch, classifies failures
- Renders inline in Fabric notebooks with Fabric-branded HTML
- Anonymous telemetry to `appi-fabric-migration-scanner` for adoption tracking

**Reviewers:** see [`CONTRIBUTING.md`](https://github.com/MichaelaIsaacs/pq-adbc-advisor/blob/v0.2.1-initial-review/CONTRIBUTING.md) on the review branch for a 15-minute read order.
