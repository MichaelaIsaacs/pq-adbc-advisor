# pq-adbc-advisor telemetry queries

Ready-to-paste KQL against **`appi-fabric-migration-scanner`** in the
**PQ/M/Dataflows Adhoc Testing** subscription.

## Where to run them

Azure Portal → your App Insights resource → Logs → paste any query → Run.
Direct link: https://portal.azure.com/#@microsoft.onmicrosoft.com/resource/subscriptions/40bf5434-ae5c-490d-a61b-de4c29313282/resourceGroups/rg-fabric-migration-scanner/providers/Microsoft.Insights/components/appi-fabric-migration-scanner/logs

---

## 1. Adoption (your #1 ask)

```kusto
customEvents
| where name == "scan_complete"
| where timestamp > ago(30d)
| summarize
    total_scans = count(),
    unique_tenants = dcount(tostring(customDimensions.tenant_hash)),
    unique_workspaces = dcount(tostring(customDimensions.workspace_id))
```

## 2. Tenants using the tool (your #2 ask — join to MSSales for TPID)

```kusto
customEvents
| where name == "scan_complete"
| summarize
    scans = count(),
    workspaces = dcount(tostring(customDimensions.workspace_id)),
    first_seen = min(timestamp),
    last_seen = max(timestamp)
    by tenant_hash = tostring(customDimensions.tenant_hash),
       tenant_id = tostring(customDimensions.tenant_id)
| order by scans desc
```

`tenant_id` is only populated when `PQ_ADBC_ADVISOR_TENANT_RAW=1` is set.
When empty, hash your MSSales TPID list with SHA-256[:12] and join on
`tenant_hash`.

## 3. First-run impact (your #3 ask)

```kusto
customEvents
| where name == "scan_complete"
| where tostring(customDimensions.is_first_run) == "True"
| project
    timestamp,
    tenant_hash = tostring(customDimensions.tenant_hash),
    workspace_id = tostring(customDimensions.workspace_id),
    high_risk    = toint(customDimensions.first_risk_high),
    pinned_odbc  = toint(customDimensions.first_pinned_odbc),
    custom_dsn   = toint(customDimensions.first_custom_dsn),
    total_calls  = toint(customDimensions.first_total_calls)
| order by pinned_odbc desc
```

## 4. Improvement over time (your #4 ask — the money query)

```kusto
customEvents
| where name == "scan_complete"
| summarize arg_max(toint(customDimensions.run_count), *) by
    tenant_hash = tostring(customDimensions.tenant_hash),
    workspace_id = tostring(customDimensions.workspace_id)
| project
    tenant_hash,
    workspace_id,
    run_number      = toint(customDimensions.run_count),
    first_high      = toint(customDimensions.first_risk_high),
    current_high    = toint(customDimensions.current_risk_high),
    delta_high      = toint(customDimensions.first_risk_high) - toint(customDimensions.current_risk_high),
    first_pinned    = toint(customDimensions.first_pinned_odbc),
    current_pinned  = toint(customDimensions.current_pinned_odbc),
    delta_pinned    = toint(customDimensions.first_pinned_odbc) - toint(customDimensions.current_pinned_odbc),
    latest_scan     = timestamp
| where run_number >= 2
| order by delta_pinned desc, delta_high desc
```

## 5. Which connector shows up most?

```kusto
customEvents
| where name == "scan_complete"
| where timestamp > ago(30d)
| project cd = customDimensions
| mv-apply cd on (
    extend keys = bag_keys(cd)
    | mv-expand key = keys to typeof(string)
    | where key startswith "impacts_by_connector_"
    | extend connector = substring(key, strlen("impacts_by_connector_"))
    | extend count = toint(cd[key])
    | project connector, count
)
| summarize total_calls = sum(count) by connector
| order by total_calls desc
```

## 6. Stay-on-ODBC at risk

```kusto
customEvents
| where name == "scan_complete"
| where timestamp > ago(30d)
| summarize arg_max(timestamp, *) by workspace_id = tostring(customDimensions.workspace_id)
| project
    tenant_hash    = tostring(customDimensions.tenant_hash),
    workspace_id,
    pinned_at_risk = toint(customDimensions.odbc_pinned_at_risk_count)
| where pinned_at_risk > 0
| order by pinned_at_risk desc
```

## 7. Top diagnosed validation issues

```kusto
customEvents
| where name == "validation_complete"
| where timestamp > ago(30d)
| project cd = customDimensions
| mv-apply cd on (
    extend keys = bag_keys(cd)
    | mv-expand key = keys to typeof(string)
    | where key startswith "issue_"
    | extend issue = substring(key, strlen("issue_"))
    | extend count = toint(cd[key])
    | project issue, count
)
| summarize occurrences = sum(count) by issue
| order by occurrences desc
```
