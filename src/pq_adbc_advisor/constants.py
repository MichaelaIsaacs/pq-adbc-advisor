"""Connector rules, migration buckets, and tool metadata."""

from __future__ import annotations

TOOL_VERSION = "0.2.1"

MIGRATION_ODBC_TO_ADBC = "odbc_to_adbc"
MIGRATION_NONE = "none"

IMPACTED_CONNECTORS = [
    {
        "kind": "Snowflake",
        "m_functions": ["Snowflake.Databases", "Snowflake.Contents"],
        "family": "snowflake",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:snowflake",
        "notes": "Snowflake migrating from ODBC driver to ADBC. Default flip ~autumn 2026, cutover ~early 2027.",
    },
    {
        "kind": "Google BigQuery",
        "m_functions": [
            "GoogleBigQuery.Database",
            "GoogleBigQueryAad.Database",
            "GoogleBigQueryAad.Contents",
        ],
        "family": "bigquery",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:bigquery",
        "notes": "BigQuery (service-account/OAuth and AAD variants) migrating to ADBC. Proxy support in progress; contact adbcmigration@microsoft.com for proxy scenarios.",
    },
    {
        "kind": "Databricks",
        "m_functions": ["Databricks.Catalogs", "Databricks.Contents", "AzureDatabricks.Catalogs", "AzureDatabricks.Contents"],
        "family": "databricks",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:databricks",
        "notes": "Databricks (Azure + non-Azure) migrating from ODBC to ADBC.",
    },
    {
        "kind": "Dremio",
        "m_functions": ["Dremio.Databases", "Dremio.Contents"],
        "family": "dremio",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:dremio",
        "notes": "Dremio migrating from ODBC (port 31010) to ADBC (port 32010). Port switch is automatic; no M edits needed.",
    },
    {
        "kind": "Amazon Redshift",
        "m_functions": ["AmazonRedshift.Database", "AmazonRedshift.Tables"],
        "family": "redshift",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:redshift",
        "notes": "Amazon Redshift migrating from ODBC to ADBC.",
    },
    {
        "kind": "Spark / HDInsight",
        "m_functions": ["Spark.Tables", "HDInsight.Contents", "AzureHDInsightSpark.Tables"],
        "family": "spark",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:spark",
        "notes": "Spark / HDInsight Spark connector migrating to ADBC.",
    },
    {
        "kind": "Impala",
        "m_functions": ["Impala.Database"],
        "family": "impala",
        "migration": f"{MIGRATION_ODBC_TO_ADBC}:impala",
        "notes": "Impala connector migrating from ODBC to ADBC.",
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
