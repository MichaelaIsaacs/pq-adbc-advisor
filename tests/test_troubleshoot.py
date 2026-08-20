"""Tests for the error-classification / troubleshoot module.

Rules are keyed to the ODBC-to-ADBC External Guide catalog.  Where a real
error text was observed live against Fabric, we keep it as a regression test.
"""

from pq_adbc_advisor.troubleshoot import diagnose, diagnose_performance_regression


# ---- External Guide #1: Driver Not Found ---------------------------- #

def test_driver_not_found_exact_signature():
    d = diagnose("DataSource.Error: ADBC: driver not found")
    assert d is not None
    assert d.issue == "ADBC driver not found"
    assert any("Update Power BI Desktop" in a for a in d.suggested_actions)


def test_driver_not_found_variant():
    d = diagnose("The ADBC driver for Snowflake is not installed.")
    assert d is not None
    assert d.issue == "ADBC driver not found"


# ---- Real: credentials not specified (regression) ------------------- #

def test_real_credentials_not_specified_from_fabric():
    real = '{"errorCode":"ModelRefreshFailed_CredentialsNotSpecified"}'
    d = diagnose(real)
    assert d is not None
    assert d.issue == "Data source credentials not configured"


# ---- External Guide #2: Authentication Failures --------------------- #

def test_adbc_auth_exact_signature():
    d = diagnose("DataSource.Error: ADBC: authentication failed")
    assert d is not None
    assert d.issue == "ADBC authentication failed"


def test_real_access_denied_from_fabric():
    real = '{"errorCode":"ModelRefresh_ShortMessage_ProcessingError","errorDescription":"Access was denied."}'
    d = diagnose(real)
    assert d is not None
    assert d.issue == "ADBC authentication failed"


def test_401_unauthorized():
    d = diagnose("HTTP 401 Unauthorized")
    assert d is not None
    assert d.issue == "ADBC authentication failed"


# ---- External Guide #3: Type Mismatch ------------------------------- #

def test_expression_error_type_mismatch():
    d = diagnose("Expression.Error: Type mismatch on column Amount")
    assert d is not None
    assert d.issue == "Type mismatch or schema difference (ADBC vs ODBC)"


def test_schema_drift_key_didnt_match():
    d = diagnose("The key didn't match any rows in the table.")
    assert d is not None
    assert d.issue == "Type mismatch or schema difference (ADBC vs ODBC)"


# ---- External Guide #4: Custom DSN in M ----------------------------- #

def test_custom_dsn_detected():
    d = diagnose("Connection string is malformed: DSN not recognized")
    assert d is not None
    assert "DSN" in d.issue or "custom" in d.issue.lower()


# ---- External Guide #6: Proxy --------------------------------------- #

def test_proxy_407():
    d = diagnose("HTTP 407 Proxy Authentication Required")
    assert d is not None
    assert d.issue == "Proxy configuration issue"
    # BigQuery-specific escalation guidance should be present
    assert any("BigQuery" in a for a in d.suggested_actions)


# ---- Gateway offline ------------------------------------------------ #

def test_gateway_offline():
    d = diagnose("OnPremisesGatewayNotReachable: could not reach cluster URI.")
    assert d is not None
    assert d.issue == "Gateway offline or unreachable"


# ---- External Guide #14: Implementation pinning legacy path --------- #

def test_odbc_leftover_implementation_1():
    d = diagnose('DataSource.Error: ODBC connection uses Implementation="1.0"')
    assert d is not None
    # Either legacy pinning OR gateway driver-mismatch, but should say something
    assert d.issue in (
        "Legacy ODBC path still pinned in M",
        "Service vs gateway driver mismatch",
    )


# ---- Timeout, TLS, rate limit, network ----------------------------- #

def test_timeout():
    d = diagnose("Command timeout expired while executing the query.")
    assert d is not None
    assert d.issue == "Query timeout"


def test_tls():
    d = diagnose("SSL certificate verify failed: self signed certificate.")
    assert d is not None
    assert d.issue == "TLS / certificate error"


def test_rate_limit():
    d = diagnose("HTTP 429 Too Many Requests: quota exceeded.")
    assert d is not None
    assert d.issue == "Backend rate limit / quota"


def test_network():
    d = diagnose("Unable to connect: DNS resolution failed for host.")
    assert d is not None
    assert d.issue == "Network / connectivity failure"


# ---- External Guide #15: Dremio port change ------------------------ #

def test_dremio_port_change():
    d = diagnose("Connection to Dremio on port 32010 refused")
    assert d is not None
    assert "Dremio" in d.issue


# ---- Fallback ------------------------------------------------------- #

def test_unknown_returns_generic_fallback():
    d = diagnose("Some totally unfamiliar error text that matches no rule.")
    assert d is not None
    assert d.issue == "Uncategorized refresh error"
    assert any("adbcmigration@microsoft.com" in a for a in d.suggested_actions)


def test_empty_returns_none():
    assert diagnose("") is None
    assert diagnose(None) is None


# ---- External Guide #7: performance regression --------------------- #

def test_performance_regression_below_threshold_returns_none():
    assert diagnose_performance_regression(0) is None
    assert diagnose_performance_regression(20) is None
    assert diagnose_performance_regression(50) is None


def test_performance_regression_above_threshold_returns_diagnosis():
    d = diagnose_performance_regression(120.5)
    assert d is not None
    assert "regression" in d.issue.lower()
    assert any("Table.Buffer" in a or "native query" in a for a in d.suggested_actions)


def test_performance_regression_none_input():
    assert diagnose_performance_regression(None) is None
