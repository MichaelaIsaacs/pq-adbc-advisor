"""Parse Power Query M expressions to detect connector calls.

Two entry points:

* ``find_all_connectors(m)`` returns a record for EVERY external connector
  call found (SQL Server, Salesforce, Snowflake, ...).  Each record carries
  a ``migration`` bucket identifying whether the connector is affected by
  a current migration effort (e.g. ``odbc_to_adbc:snowflake``) or ``none``.

* ``find_hits(m)`` returns only records for connectors in the current
  IMPACTED_CONNECTORS list.  Backward-compat wrapper.

Correctness notes:

* We STRIP comments (``//...`` line and ``/* ... */`` block) and STRING
  literals BEFORE scanning so commented-out sources and connector-shaped
  text values don't produce false hits.
* When looking for an ``Implementation="..."`` option we bound the search
  to the CURRENT connector call's argument list (matched parens), so an
  Implementation from a later call cannot bleed into an earlier one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .constants import (
    MIGRATION_DEPRECATION,
    MIGRATION_NONE,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_NA,
    RISK_UNKNOWN,
    friendly_name_for_prefix,
    is_external_connector,
    rule_for_function,
)

# Match:  Ident.Ident(  e.g.  Snowflake.Databases(   Salesforce.Reports(
_CALL_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)+)\s*\(")

# Match Implementation="X.Y" (single or double quotes, whitespace tolerant).
_IMPL_RE = re.compile(r"""Implementation\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)

# Try to pull out an endpoint / server name from a call's argument list.
_FIRST_STRING_RE = re.compile(r'"([^"]+)"')

# --------------------------------------------------------------------------- #
# Secret redaction (v0.3.3 bug bash #3)
# --------------------------------------------------------------------------- #
#
# Excerpts and endpoint hints can contain literal secrets in two forms:
#   1) DSN-style connection strings: "Password=hunter2;PWD=abc;Token=eyJ...".
#   2) M shared-parameter default values that get sliced into an excerpt
#      when the parameter definition sits near the Odbc.Query(...) call:
#      shared ApiKey = "sk-live-..." meta [IsParameterQuery=true, ...];
#
# Both forms leak into HTML today. This scrubber runs on every excerpt and
# endpoint_hint before they are stored on ConnectorCall.
#
# We are intentionally conservative: we only redact clearly sensitive keys
# (password, pwd, token, secret, apikey, api_key, accountkey, sas, authorization).
# User/UID stay visible because they're often needed for triage.

_SECRET_KEYS = (
    r"password|pwd|pass|token|secret|apikey|api[_\- ]?key|accountkey|"
    r"account[_\- ]?key|shared[_\- ]?access[_\- ]?signature|sas|authorization|bearer"
)

# Connection-string style: Key=value; or Key="value" until ; or end.
_SECRET_KV_RE = re.compile(
    rf"\b({_SECRET_KEYS})\s*=\s*(\"[^\"]*\"|'[^']*'|[^;\s]+)",
    re.IGNORECASE,
)

# M shared-parameter form: shared Name = "..." meta [IsParameterQuery=true, ...];
# When a sensitive param default (name matches one of the secret keys) sits
# adjacent to a call we caught, its string value can end up in the excerpt.
_M_PARAM_SECRET_RE = re.compile(
    rf"\b(shared\s+({_SECRET_KEYS})[A-Za-z0-9_]*)\s*=\s*\"([^\"]+)\"",
    re.IGNORECASE,
)

# Also scrub any JWT-shaped tokens we see in free text.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b")


def redact_secrets(text: str) -> str:
    """Replace secret values in a string with ``***REDACTED***``.

    Handles: DSN-style ``Key=value``, M shared-parameter defaults with
    sensitive names, and JWT-shaped tokens in free text. Safe to call on
    already-redacted text (idempotent).
    """
    if not text:
        return text

    def _kv_sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        return f"{key}=***REDACTED***"

    def _param_sub(m: "re.Match[str]") -> str:
        lhs = m.group(1)  # "shared ApiKey"
        return f"{lhs} = \"***REDACTED***\""

    out = _SECRET_KV_RE.sub(_kv_sub, text)
    out = _M_PARAM_SECRET_RE.sub(_param_sub, out)
    out = _JWT_RE.sub("***REDACTED***", out)
    return out


# Hard cap on excerpt / endpoint_hint length. Anything past this is truncated
# with a "(truncated)" marker so the HTML table can never blow up because a
# customer stored a 40KB base64 blob in an M query.
_EXCERPT_MAX_LEN = 500
_ENDPOINT_MAX_LEN = 300


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + " ...(truncated)"


# Scan-time patterns that indicate a custom DSN-style ODBC path in M.
_CUSTOM_DSN_PATTERNS = [
    re.compile(r"\bOdbc\.DataSource\s*\(", re.IGNORECASE),
    re.compile(r"\bOdbc\.Query\s*\(", re.IGNORECASE),
    re.compile(r'\bDSN\s*=\s*["\']', re.IGNORECASE),
    re.compile(r'\bDriver\s*=\s*\{', re.IGNORECASE),
]

# --------------------------------------------------------------------------- #
# Pre-processing: strip comments + string literals
# --------------------------------------------------------------------------- #

# M syntax:
#   line comment:  // ... to end of line
#   block comment: /* ... */  (does not nest per the M spec)
#   string literal: "..."  with "" as an escape for a literal double quote
# We replace strings with a same-length blob of a sentinel char so byte offsets
# and regex behavior remain consistent while removing false matches.

def _strip_noise(m: str) -> str:
    """Remove comments from an M expression, respecting string literals.

    We do NOT touch string literal contents (they are needed for
    Implementation="1.0" and for endpoint hints). We only replace line-
    and block-comment characters with spaces (preserving offsets so
    downstream ranges still align).
    """
    out: list[str] = []
    i = 0
    n = len(m)
    in_str = False
    while i < n:
        ch = m[i]
        if in_str:
            out.append(ch)
            if ch == '"':
                # Doubled "" is an escape inside a string
                if i + 1 < n and m[i + 1] == '"':
                    out.append(m[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        # Not in a string
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        # Line comment
        if ch == '/' and i + 1 < n and m[i + 1] == '/':
            j = m.find("\n", i)
            if j == -1:
                out.append(" " * (n - i))
                i = n
                continue
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment
        if ch == '/' and i + 1 < n and m[i + 1] == '*':
            j = m.find("*/", i + 2)
            if j == -1:
                out.append(" " * (n - i))
                i = n
                continue
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _string_ranges(m: str) -> list[tuple[int, int]]:
    """Return [start, end) byte ranges of every string literal in M.

    Handles the M convention that a literal double-quote inside a string is
    written as two adjacent double quotes ("").
    """
    ranges: list[tuple[int, int]] = []
    i = 0
    n = len(m)
    while i < n:
        if m[i] != '"':
            i += 1
            continue
        start = i
        i += 1
        while i < n:
            if m[i] == '"':
                # Look for doubled quote (escape)
                if i + 1 < n and m[i + 1] == '"':
                    i += 2
                    continue
                # End of string
                i += 1
                break
            i += 1
        ranges.append((start, i))
    return ranges


def _in_range(pos: int, ranges: list[tuple[int, int]]) -> bool:
    for a, b in ranges:
        if a <= pos < b:
            return True
        if a > pos:
            return False
    return False


def _find_call_end(m: str, open_paren: int) -> int:
    """Return the index of the ``)`` matching the ``(`` at ``open_paren``.

    Respects string literals so a ")" inside a string doesn't close the call.
    Returns len(m) if the call is unterminated.
    """
    depth = 0
    i = open_paren
    n = len(m)
    in_str = False
    while i < n:
        ch = m[i]
        if in_str:
            if ch == '"':
                if i + 1 < n and m[i + 1] == '"':
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


# --------------------------------------------------------------------------- #
# ConnectorCall dataclass
# --------------------------------------------------------------------------- #

@dataclass
class ConnectorCall:
    """One M connector invocation detected inside a Power Query expression."""

    connector_kind: str          # friendly name, e.g. "Snowflake" or "SQL Server"
    m_function: str              # exact M identifier, e.g. "Snowflake.Databases"
    migration: str               # "odbc_to_adbc:snowflake" | "none"
    implementation: str | None   # "1.0", "2.0", or None if not pinned
    endpoint_hint: str | None    # first string literal (server / URL) if we could find one
    excerpt: str                 # short snippet of surrounding M
    custom_dsn: bool = False     # True when the surrounding M builds a raw ODBC DSN/driver string

    @property
    def is_pinned_odbc(self) -> bool:
        return self.implementation is not None and self.implementation.startswith("1")

    @property
    def is_pinned_adbc(self) -> bool:
        return self.implementation is not None and self.implementation.startswith("2")

    @property
    def is_migrating(self) -> bool:
        return self.migration != MIGRATION_NONE

    def risk(self, has_gateway: bool | None) -> str:
        """Compute risk for this hit given gateway presence.

        Migration-family semantics:

        * ``deprecation:*`` — no ADBC replacement. Any use = medium risk
          (needs a real migration plan). Any pinned = high risk (already
          brittle, no gateway safe-fallback).
        * ``odbc_to_adbc:*`` — ADBC replacement exists. Risk depends on
          pinning + gateway.
        * ``none`` — not migrating; custom DSN is still medium risk
          because ADBC changes the shape ODBC-shaped M code depends on.
        """
        # Non-migrating connectors
        if not self.is_migrating:
            return RISK_MEDIUM if self.custom_dsn else RISK_NA

        # Deprecation: the connector is going away entirely
        if self.migration.startswith(f"{MIGRATION_DEPRECATION}:"):
            return RISK_HIGH if self.is_pinned_odbc else RISK_MEDIUM

        # ODBC -> ADBC migration
        if self.is_pinned_adbc:
            return RISK_LOW
        if self.is_pinned_odbc:
            if has_gateway is True:
                return RISK_MEDIUM
            if has_gateway is False:
                return RISK_HIGH
            return RISK_UNKNOWN
        if self.custom_dsn:
            return RISK_MEDIUM
        return RISK_LOW


# Backwards-compat name
Hit = ConnectorCall


# --------------------------------------------------------------------------- #
# Scanning entry points
# --------------------------------------------------------------------------- #

def find_all_connectors(m_expression: str) -> list[ConnectorCall]:
    """Return every external-connector call found in a single M expression."""
    if not m_expression:
        return []

    # 1) Strip comments so commented-out sources don't produce false hits.
    stripped = _strip_noise(m_expression)

    # 2) Compute string-literal ranges (in the comment-stripped text). We
    #    use these to skip regex matches that fall inside a string literal
    #    (e.g. the text "Snowflake.Databases(...)" appearing inside quotes).
    ranges = _string_ranges(stripped)

    # 3) Detect any custom DSN pattern anywhere in the expression - once.
    has_custom_dsn = any(rx.search(stripped) for rx in _CUSTOM_DSN_PATTERNS)

    out: list[ConnectorCall] = []
    for match in _CALL_RE.finditer(stripped):
        # Skip matches that begin inside a string literal.
        if _in_range(match.start(), ranges):
            continue

        fn = match.group(1)
        prefix = fn.split(".", 1)[0]
        if not is_external_connector(prefix):
            continue

        rule = rule_for_function(fn)
        if rule is not None:
            kind = rule["kind"]
            migration = rule["migration"]
        else:
            kind = friendly_name_for_prefix(prefix)
            migration = MIGRATION_NONE

        # BOUNDED argument-list window: from ( to matching ), respecting strings.
        # This prevents a later call's Implementation from leaking into an
        # earlier call's classification.
        open_paren = match.end() - 1  # position of '('
        close_paren = _find_call_end(stripped, open_paren)
        arg_window = stripped[open_paren : close_paren + 1]

        impl_match = _IMPL_RE.search(arg_window)
        implementation = impl_match.group(1) if impl_match else None

        endpoint_hint: str | None = None
        ep_match = _FIRST_STRING_RE.search(arg_window)
        if ep_match:
            endpoint_hint = _truncate(redact_secrets(ep_match.group(1)), _ENDPOINT_MAX_LEN)

        # Custom-DSN scoping is also bounded to this call's args, PLUS the
        # global flag - so a call to Snowflake.Databases sitting in a query
        # that ALSO uses Odbc.DataSource still gets flagged.
        call_is_custom_dsn = (
            prefix.lower() in ("odbc", "oledb") or
            any(rx.search(arg_window) for rx in _CUSTOM_DSN_PATTERNS) or
            (has_custom_dsn and prefix.lower() in ("odbc", "oledb"))
        )

        start = max(0, match.start() - 40)
        end = min(len(stripped), match.end() + 160)
        excerpt = stripped[start:end].replace("\n", " ").strip()
        excerpt = _truncate(redact_secrets(excerpt), _EXCERPT_MAX_LEN)

        out.append(
            ConnectorCall(
                connector_kind=kind,
                m_function=fn,
                migration=migration,
                implementation=implementation,
                endpoint_hint=endpoint_hint,
                excerpt=excerpt,
                custom_dsn=call_is_custom_dsn,
            )
        )
    return out


def find_hits(m_expression: str) -> list[ConnectorCall]:
    """Backward-compat: return only calls that belong to a migration bucket."""
    return [c for c in find_all_connectors(m_expression) if c.is_migrating]


def find_all_in_many(expressions: Iterable[str]) -> list[ConnectorCall]:
    out: list[ConnectorCall] = []
    for expr in expressions:
        out.extend(find_all_connectors(expr))
    return out


def summarize_kinds(calls: Iterable[ConnectorCall]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in calls:
        counts[c.connector_kind] = counts.get(c.connector_kind, 0) + 1
    return counts
