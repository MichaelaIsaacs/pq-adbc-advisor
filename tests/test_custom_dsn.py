"""Tests for scan-time detection of custom DSN-style M."""

from pq_adbc_advisor.mcode import find_all_connectors


CUSTOM_ODBC_DSN = '''
let
    ConnStr = "Driver={Snowflake ODBC};Server=x.snowflakecomputing.com;DSN=SNOW_PROD",
    Source = Odbc.DataSource(ConnStr, [HierarchicalNavigation=true])
in
    Source
'''

STANDARD_SNOWFLAKE = '''
let
    Source = Snowflake.Databases("myacct.snowflakecomputing.com", "COMPUTE_WH")
in
    Source
'''


def test_custom_dsn_detected_on_odbc_datasource():
    calls = find_all_connectors(CUSTOM_ODBC_DSN)
    assert calls
    odbc_call = next((c for c in calls if c.m_function.lower().startswith("odbc.")), None)
    assert odbc_call is not None
    assert odbc_call.custom_dsn is True
    # Even non-migrating generic ODBC becomes risk=medium when it's a custom DSN
    assert odbc_call.risk(has_gateway=None) == "medium"


def test_standard_connector_not_flagged_as_custom_dsn():
    calls = find_all_connectors(STANDARD_SNOWFLAKE)
    assert len(calls) == 1
    assert calls[0].custom_dsn is False


def test_databricks_detected():
    m = 'let Source = Databricks.Catalogs("host.databricks.com", "warehouse") in Source'
    calls = find_all_connectors(m)
    assert len(calls) == 1
    assert calls[0].connector_kind == "Databricks"
    assert calls[0].migration == "odbc_to_adbc:databricks"


def test_dremio_detected():
    m = 'let Source = Dremio.Databases("host.dremio.com:31010") in Source'
    calls = find_all_connectors(m)
    assert len(calls) == 1
    assert calls[0].connector_kind == "Dremio"
    assert calls[0].migration == "odbc_to_adbc:dremio"


def test_bigquery_aad_variant_bucketed_with_bigquery():
    m = 'let Source = GoogleBigQueryAad.Database([BillingProject="p"]) in Source'
    calls = find_all_connectors(m)
    assert len(calls) == 1
    assert calls[0].connector_kind == "Google BigQuery"
    assert calls[0].migration == "odbc_to_adbc:bigquery"
