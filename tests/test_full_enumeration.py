"""Tests for full-connector enumeration and non-migrating connector handling."""

from pq_adbc_advisor.constants import MIGRATION_NONE
from pq_adbc_advisor.mcode import find_all_connectors, find_hits


MIXED = '''
let
    A = Snowflake.Databases("acct.snowflake.com", "WH", [Implementation="1.0"]),
    B = Sql.Database("myserver", "mydb"),
    C = Web.Contents("https://api.example.com/data"),
    D = Salesforce.Reports([]),
    E = AmazonRedshift.Database("h:5439","prod")
in A
'''


def test_find_all_returns_every_connector():
    calls = find_all_connectors(MIXED)
    kinds = sorted(c.connector_kind for c in calls)
    assert kinds == [
        "Amazon Redshift",
        "SQL Server",
        "Salesforce",
        "Snowflake",
        "Web",
    ]


def test_find_all_flags_migration_bucket():
    calls = find_all_connectors(MIXED)
    by_kind = {c.connector_kind: c for c in calls}
    assert by_kind["Snowflake"].migration == "odbc_to_adbc:snowflake"
    assert by_kind["Amazon Redshift"].migration == "odbc_to_adbc:redshift"
    assert by_kind["SQL Server"].migration == MIGRATION_NONE
    assert by_kind["Salesforce"].migration == MIGRATION_NONE
    assert by_kind["Web"].migration == MIGRATION_NONE


def test_non_migrating_connector_risk_is_na():
    calls = find_all_connectors(MIXED)
    sql = next(c for c in calls if c.connector_kind == "SQL Server")
    assert sql.risk(has_gateway=True) == "na"
    assert sql.risk(has_gateway=False) == "na"
    assert sql.risk(has_gateway=None) == "na"


def test_endpoint_hint_extracted():
    calls = find_all_connectors(MIXED)
    snow = next(c for c in calls if c.connector_kind == "Snowflake")
    sql = next(c for c in calls if c.connector_kind == "SQL Server")
    web = next(c for c in calls if c.connector_kind == "Web")
    assert snow.endpoint_hint == "acct.snowflake.com"
    assert sql.endpoint_hint == "myserver"
    assert web.endpoint_hint == "https://api.example.com/data"


def test_find_hits_only_returns_migrating():
    hits = find_hits(MIXED)
    kinds = sorted(h.connector_kind for h in hits)
    assert kinds == ["Amazon Redshift", "Snowflake"]


def test_builtins_are_ignored():
    m = 'let x = Table.FromRows({{1}}), y = Text.Upper("hi") in x'
    assert find_all_connectors(m) == []
