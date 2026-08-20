"""Unit tests for the M-code parser.

These tests do not require Fabric or any network access.  Run with::

    pip install -e ".[dev]"
    pytest
"""

from pq_adbc_advisor.mcode import find_hits


SNOWFLAKE_ODBC_PINNED = '''
let
    Source = Snowflake.Databases("myacct.snowflakecomputing.com", "COMPUTE_WH", [Implementation="1.0", Role="ANALYST"]),
    DB = Source{[Name="SALES"]}[Data]
in
    DB
'''

SNOWFLAKE_ADBC_PINNED = '''
let
    Source = Snowflake.Databases("myacct.snowflakecomputing.com", "COMPUTE_WH", [Implementation="2.0"])
in
    Source
'''

SNOWFLAKE_UNPINNED = '''
let
    Source = Snowflake.Databases("myacct.snowflakecomputing.com", "COMPUTE_WH")
in
    Source
'''

REDSHIFT_QUERY = '''
let
    Source = AmazonRedshift.Database("host:5439", "prod", [Implementation = "1.0"])
in
    Source
'''

BIGQUERY_AAD = '''
let
    Source = GoogleBigQueryAad.Database([BillingProject="my-project"])
in
    Source
'''

SPARK_QUERY = '''
let
    Source = Spark.Tables("https://ws.azurehdinsight.net/", 1, [Implementation="1.0"])
in
    Source
'''

MIXED_UNSUPPORTED = '''
let
    Source = Sql.Database("server", "db"),
    Web = Web.Contents("https://api.example.com")
in
    Source
'''


def test_detects_snowflake_odbc_pinning():
    hits = find_hits(SNOWFLAKE_ODBC_PINNED)
    assert len(hits) == 1
    assert hits[0].connector_kind == "Snowflake"
    assert hits[0].is_pinned_odbc is True
    assert hits[0].is_pinned_adbc is False
    assert hits[0].risk(has_gateway=False) == "high"
    assert hits[0].risk(has_gateway=True) == "medium"


def test_detects_snowflake_adbc_pinning():
    hits = find_hits(SNOWFLAKE_ADBC_PINNED)
    assert len(hits) == 1
    assert hits[0].is_pinned_adbc is True
    assert hits[0].risk(has_gateway=False) == "low"


def test_unpinned_snowflake_is_low_risk():
    hits = find_hits(SNOWFLAKE_UNPINNED)
    assert len(hits) == 1
    assert hits[0].implementation is None
    assert hits[0].risk(has_gateway=None) == "low"


def test_detects_redshift():
    hits = find_hits(REDSHIFT_QUERY)
    assert len(hits) == 1
    assert hits[0].connector_kind == "Amazon Redshift"
    assert hits[0].is_pinned_odbc is True


def test_detects_bigquery_aad():
    hits = find_hits(BIGQUERY_AAD)
    assert len(hits) == 1
    # AAD variant is now folded into the base BigQuery migration bucket
    assert hits[0].connector_kind == "Google BigQuery"
    assert hits[0].migration == "odbc_to_adbc:bigquery"


def test_detects_spark():
    hits = find_hits(SPARK_QUERY)
    assert len(hits) == 1
    assert hits[0].connector_kind == "Spark / HDInsight"
    assert hits[0].is_pinned_odbc is True


def test_ignores_unrelated_connectors():
    hits = find_hits(MIXED_UNSUPPORTED)
    assert hits == []


def test_multiple_hits_in_one_expression():
    m = SNOWFLAKE_ODBC_PINNED + "\n" + REDSHIFT_QUERY
    hits = find_hits(m)
    kinds = sorted(h.connector_kind for h in hits)
    assert kinds == ["Amazon Redshift", "Snowflake"]


def test_empty_input():
    assert find_hits("") == []
    assert find_hits(None) == []  # type: ignore[arg-type]
