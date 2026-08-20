"""Connector rules, migration buckets, and tool metadata."""

from __future__ import annotations

TOOL_VERSION = "0.2.3"

# Migration bucket families.
#   odbc_to_adbc  - connector is moving from an embedded ODBC driver to the ADBC path
#   deprecation   - connector is being retired entirely, no ADBC replacement
#   none          - not part of any current migration
MIGRATION_ODBC_TO_ADBC = "odbc_to_adbc"
MIGRATION_DEPRECATION = "deprecation"
MIGRATION_NONE = "none"

# Ground-truth M function catalog per David Coe review (2026-08-19).
# The m_functions lists match the ``shared X.Y = ...`` entries in the
# actual connector .pq source files.  Do not add functions here unless
# they are exported by the current connector - false positives shift
# customer risk classifications.
IMPACTED_CONNECTORS = [
    {
        "kind": "Snowflake",
        "m_functions": ["Snowflake.Databases"],
        "family": "snowflake",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:snowflake",
        "notes": "Snowflake migrating from ODBC driver to ADBC. Default flip ~autumn 2026, cutover ~early 2027.",
    },
    {
        "kind": "Google BigQuery",
        "m_functions": [
            "GoogleBigQuery.Database",
            "GoogleBigQueryAad.Database",
        ],
        "family": "bigquery",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:bigquery",
        "notes": "BigQuery (service-account/OAuth and AAD variants) migrating to ADBC. Proxy support in progress; contact adbcmigration@microsoft.com for proxy scenarios.",
    },
    {
        "kind": "Databricks",
        "m_functions": [
            # Databricks connector on Azure Databricks
            "Databricks.Catalogs",
            "Databricks.Contents",
            "Databricks.Query",  # DirectQuery entry point
            # DatabricksMultiCloud: Databricks on AWS / GCP (different Kind, different auth)
            "DatabricksMultiCloud.Catalogs",
            "DatabricksMultiCloud.Query",
        ],
        "family": "databricks",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:databricks",
        "notes": "Databricks (Azure Databricks + DatabricksMultiCloud) migrating from ODBC to ADBC. Query entries are DirectQuery-only.",
    },
    {
        "kind": "Dremio",
        "m_functions": [
            # Self-hosted Dremio, versioned by connector SDK
            "Dremio.Databases",
            "Dremio.DatabasesV300",
            "Dremio.DatabasesV370",
            # Dremio Cloud (hosted), also versioned
            "DremioCloud.DatabasesByServer",
            "DremioCloud.DatabasesByServerV330",
            "DremioCloud.DatabasesByServerV370",
        ],
        "family": "dremio",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:dremio",
        "notes": "Dremio (self-hosted + DremioCloud, multiple SDK versions) migrating from ODBC to ADBC. Port 31010 → 32010 switches automatically.",
    },
    {
        "kind": "Amazon Redshift",
        "m_functions": ["AmazonRedshift.Database"],
        "family": "redshift",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:redshift",
        "notes": "Amazon Redshift migrating from ODBC to ADBC.",
    },
    {
        "kind": "Spark / HDInsight",
        "m_functions": [
            "Spark.Tables",         # managed Spark
            "AzureSpark.Tables",    # HDInsight Spark (formerly AzureHDInsightSpark.*)
            "ApacheSpark.Tables",   # on-prem Apache Spark
        ],
        "family": "spark",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:spark",
        "notes": "Spark family (managed Spark, HDInsight Spark, on-prem Apache Spark) migrating to ADBC.",
    },
    {
        "kind": "Impala",
        "m_functions": ["Impala.Database"],
        "family": "impala",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:impala",
        "notes": "Impala connector migrating from ODBC to ADBC.",
    },
    # Hive is a DEPRECATION, not a migration.  There is no ADBC replacement;
    # the connector is being retired.  Risk logic in mcode.py treats
    # `deprecation:*` buckets differently than `odbc_to_adbc:*` because a
    # gateway is not a safe fallback here.
    {
        "kind": "Hive LLAP",
        "m_functions": [
            "AzureHiveLLAP.Database",
            "ApacheHiveLLAP.Database",
        ],
        "family": "hive",
        "migration": f"{MIGRATION_DEPRECATION}:hive",
        "notes": "Hive LLAP is being deprecated; there is no ADBC replacement. Plan migration to Databricks or Fabric SQL Endpoint before end-of-life.",
    },
]

_M_FN_INDEX = {
    fn.lower(): rule for rule in IMPACTED_CONNECTORS for fn in rule["m_functions"]
}


def rule_for_function(fn_name: str) -> dict | None:
    """Return the connector rule for an M function name, or None."""
    return _M_FN_INDEX.get(fn_name.lower())


# Friendly display names for every M connector prefix we know about.
# Keys are matched case-insensitively against the FIRST identifier
# before the dot in the M function name.  Extend freely.
KNOWN_CONNECTOR_PREFIXES: dict[str, str] = {
    "sql": "SQL Server",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "oracle": "Oracle",
    "teradata": "Teradata",
    "sybase": "Sybase",
    "db2": "IBM Db2",
    "informix": "IBM Informix",
    "sqlite": "SQLite",
    "access": "Microsoft Access",
    "excel": "Excel",
    "csv": "CSV",
    "text": "Text / CSV",
    "json": "JSON",
    "xml": "XML",
    "html": "HTML",
    "web": "Web",
    "odata": "OData",
    "sharepoint": "SharePoint",
    "onedrive": "OneDrive",
    "azurestorage": "Azure Storage",
    "azuredatalake": "Azure Data Lake",
    "azureblob": "Azure Blob Storage",
    "azuretables": "Azure Tables",
    "azurequeues": "Azure Queues",
    "azurecosmosdb": "Azure Cosmos DB",
    "azuredatabricks": "Azure Databricks",
    "databricks": "Databricks",
    "azuresynapse": "Azure Synapse",
    "azureml": "Azure ML",
    "kusto": "Azure Data Explorer (Kusto)",
    "azuredataexplorer": "Azure Data Explorer",
    "azurepostgresql": "Azure PostgreSQL",
    "azuremysql": "Azure MySQL",
    "amazons3": "Amazon S3",
    "amazonathena": "Amazon Athena",
    "amazonredshift": "Amazon Redshift",
    "googleanalytics": "Google Analytics",
    "googlesheets": "Google Sheets",
    "googlebigquery": "Google BigQuery",
    "googlebigqueryaad": "Google BigQuery (AAD)",
    "snowflake": "Snowflake",
    "salesforce": "Salesforce",
    "sap": "SAP",
    "saphana": "SAP HANA",
    "sapbw": "SAP BW",
    "dynamics": "Dynamics 365",
    "commondataservice": "Dataverse (CDS)",
    "dataverse": "Dataverse",
    "exchange": "Exchange",
    "hive": "Hive",
    "impala": "Impala",
    "vertica": "Vertica",
    "spark": "Spark",
    "hdinsight": "HDInsight",
    "azurehdinsightspark": "Azure HDInsight Spark",
    "odbc": "Generic ODBC",
    "oledb": "Generic OLE DB",
    "folder": "Folder",
    "file": "File",
}

# M function prefixes that are NOT external data sources - they're
# in-memory M builtins.  We exclude them from connector reporting.
# These are M library functions that transform or shape data but do NOT
# establish an external connection.
_M_BUILTIN_PREFIXES = {
    # Type constructors / data shapers
    "table", "record", "list", "text", "number", "datetime", "date",
    "duration", "time", "value", "type", "function", "expression",
    "diagnostics", "binary", "logical", "any", "iterator", "combiner",
    "splitter", "replacer", "comparer", "geometry",
    # Parsers / decoders (not connectors)
    "json", "xml", "csv", "html", "uri",
    # Character / cryptography helpers
    "character", "guid", "byteorder", "hash", "cryptography", "base64",
    # Section / metadata (M language plumbing)
    "section",
}


def is_external_connector(prefix: str) -> bool:
    """Return True if the M function prefix looks like an external data source."""
    return prefix.lower() not in _M_BUILTIN_PREFIXES


def friendly_name_for_prefix(prefix: str) -> str:
    """Return a friendly display name for an M connector prefix."""
    return KNOWN_CONNECTOR_PREFIXES.get(prefix.lower(), prefix)


# Risk levels attached to each hit.
RISK_HIGH = "high"      # Implementation="1.0" pinned AND no gateway -> breaks at cutover
RISK_MEDIUM = "medium"  # Implementation="1.0" pinned WITH gateway -> works via gateway
RISK_LOW = "low"        # Implementation unspecified -> tenant switch handles migration
RISK_UNKNOWN = "unknown"
RISK_NA = "na"          # Connector is not part of any current migration

# Power BI Scanner API defaults
SCANNER_CHUNK_SIZE = 100
SCANNER_POLL_INTERVAL_SEC = 5
SCANNER_MAX_POLLS = 60

# Fabric getDefinition LRO tuning (David Coe review, 2026-08-20).
# The Fabric API commonly returns Retry-After: 20 even when the underlying
# operation completes in <1s. Starting with a short first sleep and backing
# off exponentially up to the server's hint drops the p50 wait per artifact
# from ~20s to ~1s.
LRO_FIRST_POLL_SEC = 1

# Parallelism for per-item Fabric REST calls (getDefinition, gateway lookup).
# Fabric APIs generally tolerate ~10 in flight per identity. Higher values
# hit rate limits.
DEFAULT_MAX_PARALLEL = 10
