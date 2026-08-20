"""Regression tests for bugs surfaced by the initial adversarial review.

Each test is tagged with the review-finding ID so future reviewers can
trace a rule back to its origin.
"""

from pq_adbc_advisor.mcode import find_all_connectors, find_hits
from pq_adbc_advisor.troubleshoot import diagnose


# -------- r05: Implementation cross-contamination between calls -------- #

def test_r05_implementation_does_not_leak_from_later_call():
    """An unpinned Snowflake call followed by a pinned Redshift call must
    NOT report Snowflake as pinned to ODBC."""
    m = '''
    let
        S = Snowflake.Databases("snow.example.com", "WH"),
        R = AmazonRedshift.Database("redshift.example.com:5439", "prod", [Implementation="1.0"])
    in
        S
    '''
    calls = find_all_connectors(m)
    snow = next(c for c in calls if c.connector_kind == "Snowflake")
    rs = next(c for c in calls if c.connector_kind == "Amazon Redshift")
    assert snow.implementation is None, "Snowflake must not inherit Redshift's Implementation"
    assert snow.is_pinned_odbc is False
    assert rs.is_pinned_odbc is True


def test_r05_deeply_nested_call_arguments_do_not_cross_contaminate():
    m = '''
    let
        Outer = Snowflake.Databases(SomeHelper.Get(), "WH"),
        Other = Snowflake.Databases("x", "y", [Implementation="1.0"])
    in Outer
    '''
    calls = find_all_connectors(m)
    kinds = [c.implementation for c in calls if c.connector_kind == "Snowflake"]
    # First Snowflake call is unpinned; second is pinned. Order preserved.
    assert kinds[0] is None
    assert kinds[1] == "1.0"


# -------- r06: Comments and string literals must not create hits ------- #

def test_r06_line_comment_does_not_create_false_hit():
    m = '''
    let
        // Old code: Source = Snowflake.Databases("legacy.snowflake.com","WH",[Implementation="1.0"])
        Source = Sql.Database("prod", "db")
    in Source
    '''
    calls = find_all_connectors(m)
    kinds = [c.connector_kind for c in calls]
    assert kinds == ["SQL Server"], f"Expected only SQL Server, got {kinds}"


def test_r06_block_comment_does_not_create_false_hit():
    m = '''
    /*
      Do not use: Source = Snowflake.Databases("x","y",[Implementation="1.0"])
    */
    let Source = Sql.Database("prod", "db") in Source
    '''
    calls = find_all_connectors(m)
    assert [c.connector_kind for c in calls] == ["SQL Server"]


def test_r06_string_literal_containing_connector_call_is_ignored():
    m = '''
    let
        Note = "See Snowflake.Databases(...) example in docs",
        Source = Sql.Database("prod", "db")
    in Source
    '''
    calls = find_all_connectors(m)
    assert [c.connector_kind for c in calls] == ["SQL Server"]


def test_r06_url_with_double_slash_is_not_treated_as_comment():
    """A URL like https://ws.azurehdinsight.net/ contains // but is inside a string."""
    m = 'let Source = Spark.Tables("https://ws.azurehdinsight.net/", 1, [Implementation="1.0"]) in Source'
    calls = find_all_connectors(m)
    assert len(calls) == 1
    assert calls[0].connector_kind == "Spark / HDInsight"
    assert calls[0].is_pinned_odbc is True
    assert calls[0].endpoint_hint == "https://ws.azurehdinsight.net/"


# -------- r07: M library functions must not be reported as connectors -- #

def test_r07_json_document_is_not_a_connector():
    m = 'let raw = Web.Contents("https://api.example.com"), parsed = Json.Document(raw) in parsed'
    calls = find_all_connectors(m)
    kinds = sorted(c.connector_kind for c in calls)
    assert "Web" in kinds  # Web.Contents is a real connector
    assert not any(c.m_function.startswith("Json.") for c in calls)


def test_r07_uri_parts_is_not_a_connector():
    m = 'let u = Uri.Parts("https://example.com/path") in u'
    calls = find_all_connectors(m)
    assert calls == []


def test_r07_xml_document_is_not_a_connector():
    m = 'let raw = Web.Contents("http://x"), doc = Xml.Document(raw) in doc'
    calls = find_all_connectors(m)
    assert not any(c.m_function.startswith("Xml.") for c in calls)


# -------- r09: ModelRefresh ProcessingError is not always auth -------- #

def test_r09_generic_processing_error_is_not_forced_to_auth():
    """A processing error with no auth-related text must NOT be classified as auth."""
    generic = '{"errorCode":"ModelRefresh_ShortMessage_ProcessingError","errorDescription":"Out of memory during model refresh."}'
    d = diagnose(generic)
    assert d is not None
    assert d.issue != "ADBC authentication failed"


def test_r09_access_denied_still_matches_auth():
    """The exact real error we observed live must still classify as auth."""
    real = '{"errorCode":"ModelRefresh_ShortMessage_ProcessingError","errorDescription":"Access was denied."}'
    d = diagnose(real)
    assert d is not None
    assert d.issue == "ADBC authentication failed"


# -------- r02: tenant scan dataflow shape variations ------------------ #

def test_r02_scanner_dataflow_with_mcode_field():
    from pq_adbc_advisor.definitions import expressions_from_scanner_dataflow
    df = {"queries": [{"name": "Q1", "mCode": 'let Source = Snowflake.Databases("h","w") in Source'}]}
    exprs = list(expressions_from_scanner_dataflow(df))
    assert len(exprs) == 1
    assert "Snowflake" in exprs[0]["expression"]


def test_r02_scanner_dataflow_with_entities():
    from pq_adbc_advisor.definitions import expressions_from_scanner_dataflow
    df = {"entities": [{"name": "E1", "partitions": [{"expression": 'let x = Snowflake.Databases("h","w") in x'}]}]}
    exprs = list(expressions_from_scanner_dataflow(df))
    assert len(exprs) == 1
