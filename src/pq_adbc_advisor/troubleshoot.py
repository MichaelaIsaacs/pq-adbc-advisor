"""Error classification + troubleshooting rules.

Rules are keyed to the failure catalog in the ODBC-to-ADBC External Guide
(aka.ms/adbcfaq).  Each rule maps a real-world error signature to:

  - issue        : short category
  - likely_cause : one-sentence explanation
  - suggested_actions : ordered list of things to try
  - docs         : link (defaults to the FAQ) 

Sources:
  1. ODBC to ADBC External Guide - the 17 documented failure modes.
  2. Real errors observed live against Fabric (regression-tested).
  3. Field patterns for common Power BI refresh failures.

The FAQ link is the default docs pointer for every rule so customers always
land on the authoritative reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


FAQ_URL = "https://learn.microsoft.com/power-query/transition-to-adbc"
LEARN_URL = "https://learn.microsoft.com/power-query/transition-to-adbc"
ESCALATION_ALIAS = "adbcmigration@microsoft.com"


@dataclass
class Diagnosis:
    issue: str
    likely_cause: str
    suggested_actions: list[str] = field(default_factory=list)
    docs: str = FAQ_URL

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue,
            "likely_cause": self.likely_cause,
            "suggested_actions": self.suggested_actions,
            "docs": self.docs,
        }


# Ordered: first match wins.  Patterns are case-insensitive regex.
# Rule numbers map to the "Gap analysis" table in the README so future
# reviewers can trace a rule back to a documented failure mode.
_RULES: list[dict[str, Any]] = [
    # ---- 1. Driver Not Found (External Guide #1) --------------------- #
    {
        "pattern": r"(DataSource\.Error:\s*ADBC:\s*driver\s*not\s*found|ADBC.*driver.*not.*(found|installed|available))",
        "issue": "ADBC driver not found",
        "likely_cause": "Power BI Desktop or the on-prem gateway is on a build older than the "
                        "release that ships the ADBC driver for this connector.",
        "suggested_actions": [
            "Update Power BI Desktop to the latest release.",
            "For gateway-backed refreshes: install the gateway release that contains ADBC support "
                "(check the gateway's release notes for the connector).",
            "As a temporary workaround, disable ADBC at the workspace level so this workload stays on ODBC.",
        ],
    },

    # ---- 2. Data source credentials not configured (real: newly-created models) ---- #
    {
        "pattern": r"(ModelRefreshFailed_CredentialsNotSpecified|credentials.*not.*(specified|configured|set)|missing.*credentials|no.*credentials.*(configured|for))",
        "issue": "Data source credentials not configured",
        "likely_cause": "The dataset has no stored credentials for the data source. Common right after "
                        "creating a model, or after switching auth types (e.g. ODBC username/password to ADBC OAuth2).",
        "suggested_actions": [
            "Open Dataset settings → Data source credentials and click 'Edit credentials' for each source.",
            "Choose the auth method the ADBC driver supports (OAuth2 for Snowflake/BigQuery, key-based for Redshift, etc.).",
            "Enter valid credentials and save. Then retry the refresh.",
        ],
        "docs": "https://learn.microsoft.com/power-bi/connect-data/refresh-data",
    },

    # ---- 3. Authentication Failures (External Guide #2) -------------- #
    # NOTE: we intentionally do NOT include the generic
    # `ModelRefresh_ShortMessage_ProcessingError` code here - that code
    # covers many non-auth failures. Instead we match on auth-specific
    # phrasing including "Access was denied" that Fabric returns from
    # inside a ProcessingError envelope.
    {
        "pattern": r"(DataSource\.Error:\s*ADBC:\s*authentication\s*failed|unauthorized|401|invalid.*credential|authentication.*failed|MSAL|OAuth2Failed|access.*was.*denied)",
        "issue": "ADBC authentication failed",
        "likely_cause": "Cached credentials were created against the ODBC-era shape and the metadata "
                        "no longer aligns with what ADBC expects (e.g. OAuth2 flow vs ODBC DSN username/password).",
        "suggested_actions": [
            "Clear permissions for this source: Dataset settings → Data source credentials → Edit → sign out.",
            "Re-authenticate using the auth method the ADBC driver supports.",
            "For Snowflake/BigQuery: verify the OAuth2 client ID / service account is still authorized and hasn't been revoked.",
            "For SP-based auth (Access Denied): confirm the SP still has read access on the data source (roles/shares can be revoked).",
        ],
    },

    # ---- 4. Type Mismatch / Schema Differences (External Guide #3) --- #
    {
        "pattern": r"(Expression\.Error:\s*Type\s*mismatch|schema.*(drift|mismatch|changed)|column.*not.*found|The key didn't match any rows|wide decimal|timestamp.*(different|coerced|type))",
        "issue": "Type mismatch or schema difference (ADBC vs ODBC)",
        "likely_cause": "ADBC exposes data types differently than ODBC. Wide decimals, timestamps, "
                        "and BigQuery record/geography columns are the common offenders. Downstream M "
                        "steps that assumed the ODBC-coerced type now fail.",
        "suggested_actions": [
            "Use Table.TransformColumnTypes(...) to convert types explicitly rather than relying on implicit coercion.",
            "For BigQuery: handle record/geography columns explicitly.",
            "For Snowflake: cast VARIANT/OBJECT columns before downstream steps.",
            "Re-detect the schema in Power Query Online: Home → Refresh preview.",
        ],
    },

    # ---- 5. Custom M builds DSN strings (External Guide #4) ---------- #
    # Runtime signature: often surfaces as generic connection or query errors
    # after ADBC flip; the tell is that scan already flagged this artifact
    # for custom Odbc.* usage.  We match plausible runtime signatures here.
    {
        "pattern": r"(DSN|Odbc\.DataSource|Odbc\.Query|connection.*string.*(invalid|malformed|unrecognized))",
        "issue": "Custom DSN-style M query incompatible with ADBC",
        "likely_cause": "The query builds a DSN-style ODBC connection string in M code. ADBC uses a "
                        "URI-style connection object instead. The standard connectors abstract this away "
                        "but custom M does not.",
        "suggested_actions": [
            "Rewrite the source step to use the standard connector (e.g. Snowflake.Databases, not Odbc.DataSource).",
            "If the query must stay on a raw DSN, keep the workspace-level ADBC override off for this workspace.",
            "Escalate to " + ESCALATION_ALIAS + " if the standard connector doesn't cover your scenario.",
        ],
    },

    # ---- 6. Gateway offline (Power BI general) ----------------------- #
    {
        "pattern": r"(gateway.*not.*(reach|online|available)|OnPremisesGatewayNotReachable|Cluster URI|gateway.*offline)",
        "issue": "Gateway offline or unreachable",
        "likely_cause": "The on-premises data gateway that this dataset depends on is not reachable.",
        "suggested_actions": [
            "Check the gateway service is running on the gateway machine.",
            "In the Fabric portal → Settings → Manage gateways, confirm the gateway shows 'Online'.",
            "If this dataset was moved to ADBC (cloud path), you may no longer need the gateway — remove the gateway binding.",
        ],
        "docs": "https://learn.microsoft.com/data-integration/gateway/service-gateway-manage",
    },

    # ---- 7. Gateway driver-path mismatch (External Guide #11) -------- #
    # Signature: service refresh works but gateway refresh fails with ODBC-style error
    {
        "pattern": r"(gateway.*ODBC|OnPremises.*driver|gateway.*driver.*(mismatch|not.*support))",
        "issue": "Service vs gateway driver mismatch",
        "likely_cause": "The service and the gateway can be on different driver stacks. The gateway "
                        "path may still be on ODBC while the service is on ADBC, and vice versa. This "
                        "is expected during rollout and does not invalidate the test.",
        "suggested_actions": [
            "Verify which driver each path uses: Fabric portal for service, gateway release notes for gateway build.",
            "If the gateway path is failing, update the gateway to a release that includes ADBC.",
            "Consult connector-specific gateway guidance in the FAQ.",
        ],
    },

    # ---- 8. Legacy ODBC path still in use (External Guide #14) ------- #
    {
        "pattern": r'(DataSource\.Error.*ODBC|ODBC.*driver.*not.*(found|installed)|Implementation\s*=\s*["\']1\.0)',
        "issue": "Legacy ODBC path still pinned in M",
        "likely_cause": "The M source step still references the legacy ODBC driver (usually a "
                        'connector pinned to Implementation="1.0").',
        "suggested_actions": [
            'Remove the [Implementation="1.0"] option from the M source step, or replace it with "2.0".',
            "If the query must stay on ODBC during validation, keep the workspace-level ADBC override off.",
            "Remember: at cutover the legacy ODBC path is removed. Implementation=\"1.0\" pins will stop working.",
        ],
    },

    # ---- 9. Proxy environment issues (External Guide #6) ------------- #
    {
        "pattern": r"(proxy|HTTP_PROXY|HTTPS_PROXY|407|proxy.*authentication|Unable to.*(proxy|tunnel))",
        "issue": "Proxy configuration issue",
        "likely_cause": "ADBC drivers may require additional proxy configuration beyond what ODBC needed.",
        "suggested_actions": [
            "Validate proxy behavior first: confirm the gateway or service can reach the data source through the proxy.",
            "For BigQuery specifically: proxy support for BigQuery ADBC is in progress. "
                "Contact " + ESCALATION_ALIAS + " for BigQuery proxy scenarios; custom binaries may be available.",
            "Check HTTP_PROXY / HTTPS_PROXY environment variables on the gateway machine.",
        ],
    },

    # ---- 10. Query timeout ------------------------------------------- #
    {
        "pattern": r"(timeout|timed out|Command timeout expired|took too long)",
        "issue": "Query timeout",
        "likely_cause": "The refresh exceeded the connection or command timeout. ADBC streams results "
                        "differently which can extend end-to-end refresh time in some workloads.",
        "suggested_actions": [
            "Increase CommandTimeout in the source step: e.g. Source = Snowflake.Databases(..., [CommandTimeout=#duration(0,1,0,0)]).",
            "Reduce the volume in the initial refresh (folding filters, incremental refresh).",
            "For very large fact tables, enable incremental refresh instead of full refresh.",
        ],
        "docs": "https://learn.microsoft.com/power-bi/connect-data/incremental-refresh-overview",
    },

    # ---- 11. TLS / certificate --------------------------------------- #
    {
        "pattern": r"(SSL|TLS|certificate|self.signed)",
        "issue": "TLS / certificate error",
        "likely_cause": "The ADBC driver's TLS validation is stricter than the legacy ODBC driver.",
        "suggested_actions": [
            "Confirm the server certificate is trusted by the machine running the gateway.",
            "If using a private endpoint / self-signed cert, install the CA into the trust store on the gateway.",
            "As a last resort, set TrustServerCertificate=true in the connection string (not recommended for production).",
        ],
    },

    # ---- 12. Rate limit ---------------------------------------------- #
    {
        "pattern": r"(rate.limit|429|too many requests|quota)",
        "issue": "Backend rate limit / quota",
        "likely_cause": "The data source rejected the refresh due to a quota or rate limit.",
        "suggested_actions": [
            "Stagger scheduled refreshes across the tenant.",
            "Reduce parallelism (Table.Buffer, disable parallel loading in dataset settings).",
            "Check your Snowflake warehouse / BigQuery slot quota.",
        ],
    },

    # ---- 14. Dremio port change (External Guide #15) ---------------- #
    # Placed BEFORE the generic network rule so port-specific failures
    # get the Dremio-specific advice.
    {
        "pattern": r"(Dremio.*(connection|port|refused|31010|32010)|31010|32010)",
        "issue": "Dremio port change (ODBC 31010 → ADBC 32010)",
        "likely_cause": "ODBC used Dremio port 31010; ADBC uses 32010. The connector switches the port "
                        "automatically, but firewall rules that only allowed 31010 will now block traffic.",
        "suggested_actions": [
            "Confirm firewall/network policy allows outbound to Dremio on port 32010.",
            "Verify the Dremio server exposes ADBC/Arrow Flight on 32010.",
            "No M edits are required - the port switch is automatic once ADBC is enabled.",
        ],
    },

    # ---- 15. Network / DNS ------------------------------------------- #
    {
        "pattern": r"(network|DNS|connection.*refused|Unable to connect)",
        "issue": "Network / connectivity failure",
        "likely_cause": "The refresh couldn't reach the data source endpoint.",
        "suggested_actions": [
            "Confirm the endpoint hostname and port are reachable from the gateway (or from the Fabric service in cloud-only setups).",
            "If using a private endpoint, verify the VNet gateway / private link is healthy.",
            "Check firewall allowlists include the ADBC driver's outbound IPs.",
        ],
    },
]


_COMPILED = [(re.compile(r["pattern"], re.IGNORECASE), r) for r in _RULES]


def diagnose(error_text: str | None) -> Diagnosis | None:
    """Classify a refresh error message.

    Returns a Diagnosis if any known pattern matches; falls back to a
    generic Diagnosis (with the escalation alias) so the report always
    has SOMETHING actionable.
    """
    if not error_text:
        return None
    for rx, rule in _COMPILED:
        if rx.search(error_text):
            return Diagnosis(
                issue=rule["issue"],
                likely_cause=rule["likely_cause"],
                suggested_actions=list(rule["suggested_actions"]),
                docs=rule.get("docs", FAQ_URL),
            )
    snippet = error_text[:200].replace("\n", " ")
    return Diagnosis(
        issue="Uncategorized refresh error",
        likely_cause=f"No known pattern matched. Raw error: {snippet}",
        suggested_actions=[
            f"Check the Power Query ADBC transition guide ({FAQ_URL}) for known issues.",
            f"If this occurs on ADBC-migrated datasets, forward the full error to {ESCALATION_ALIAS} "
                "with tenant ID, connector, query, and the error text.",
        ],
    )


# --------------------------------------------------------------------------- #
# Post-refresh regression detection - not error patterns, but delta-based
# --------------------------------------------------------------------------- #

def diagnose_performance_regression(duration_delta_pct: float | None) -> Diagnosis | None:
    """Return a Diagnosis when a refresh succeeded but got materially slower.

    External Guide #7 explicitly asks customers to compare refresh duration
    before and after the ADBC switch.
    """
    if duration_delta_pct is None:
        return None
    if duration_delta_pct <= 50:
        return None
    return Diagnosis(
        issue="Refresh regression - ADBC noticeably slower than ODBC baseline",
        likely_cause=(
            f"Refresh completed but took {duration_delta_pct:+.0f}% longer than the pre-migration "
            "baseline. Possible causes: query folding regression, DirectQuery translation difference, "
            "or streaming pattern change in the ADBC driver."
        ),
        suggested_actions=[
            "Capture a repro: baseline duration, ADBC duration, connector, and the M query.",
            "For DirectQuery: run View native query on a representative visual and compare folded SQL between ODBC and ADBC.",
            "Try Table.Buffer or explicit type conversion on hot-path columns to isolate the cause.",
            f"Escalate to {ESCALATION_ALIAS} with tenant ID and repro details.",
        ],
        docs=FAQ_URL,
    )


def diagnose_connector_call(
    is_pinned_odbc: bool,
    is_pinned_adbc: bool,
    is_migrating: bool,
    custom_dsn: bool,
    has_gateway: bool | None,
) -> Diagnosis | None:
    """Return a scan-time Diagnosis for a single connector call.

    Used by the impact report so each connection can carry its own
    fix hint before any refresh is triggered.  Returns None when the
    connection needs no action.
    """
    if custom_dsn:
        return Diagnosis(
            issue="Custom DSN-style M — needs review before ADBC flip",
            likely_cause=(
                "This query builds a DSN-style ODBC connection string in M. "
                "ADBC uses a URI-style connection object instead; the standard "
                "connectors abstract this away but custom M does not."
            ),
            suggested_actions=[
                "Rewrite the source step to use the standard connector (e.g. Snowflake.Databases, not Odbc.DataSource).",
                "If the query must stay on ODBC, keep the workspace-level ADBC override off.",
                f"Escalate to {ESCALATION_ALIAS} if the standard connector doesn't cover your scenario.",
            ],
            docs=LEARN_URL,
        )
    if is_pinned_odbc and is_migrating:
        if has_gateway is False:
            return Diagnosis(
                issue='Implementation="1.0" pinned, no gateway — will fail at cutover',
                likely_cause=(
                    "This connection explicitly pins the legacy ODBC driver "
                    'via Implementation="1.0". At cutover the ODBC path is removed; '
                    "without a gateway, this refresh will fail."
                ),
                suggested_actions=[
                    'Remove [Implementation="1.0"] from the source step (or replace with "2.0").',
                    "Validate the refresh works on ADBC before the cutover date.",
                    "Alternatively, bind a gateway to this dataset so the ODBC path is available via the gateway.",
                ],
                docs=LEARN_URL,
            )
        return Diagnosis(
            issue='Implementation="1.0" pinned — validate ADBC path',
            likely_cause=(
                "The connection pins the legacy ODBC driver. Refreshes continue to work "
                "through the gateway during the validation window, but at cutover the "
                "ODBC path is removed."
            ),
            suggested_actions=[
                'Test removing [Implementation="1.0"] in a copy of the dataset.',
                "Compare row counts, refresh duration, and query results between ODBC and ADBC.",
                "Roll the change out to production before the cutover date.",
            ],
            docs=LEARN_URL,
        )
    if is_migrating and not is_pinned_adbc:
        # Low risk but customer should still validate
        return Diagnosis(
            issue="Ready for ADBC — no action needed unless validation reveals issues",
            likely_cause=(
                "This connection uses the standard connector without an Implementation pin. "
                "The tenant/workspace ADBC switch will handle the migration automatically."
            ),
            suggested_actions=[
                "Enable the tenant setting 'Use ADBC drivers for supported connectors' in a test workspace.",
                "Refresh this dataset and compare row counts, duration, and results to baseline.",
                "If everything matches, roll the switch out to production workspaces.",
            ],
            docs=LEARN_URL,
        )
    return None
