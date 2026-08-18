"""Report objects returned to the customer.

The report objects render as rich, styled HTML when displayed in a Jupyter or
Fabric notebook (via ``_repr_html_``), so a customer can just type::

    baseline = scan_workspace()
    baseline

and get a colour-coded dashboard inline.  The plain-text ``summary()`` is
still available for terminal / log output, and ``to_html(path)`` writes a
self-contained file for sharing with CSAs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constants import (
    MIGRATION_NONE,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_NA,
    RISK_UNKNOWN,
    TOOL_VERSION,
)
from .mcode import ConnectorCall
from .troubleshoot import Diagnosis


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class ImpactedArtifact:
    workspace_id: str
    item_id: str
    item_name: str
    item_type: str
    hits: list[ConnectorCall]           # ALL connector calls (migrating + not)
    has_gateway: bool | None = None
    workspace_name: str = ""

    @property
    def worst_risk(self) -> str:
        rank = {RISK_HIGH: 4, RISK_MEDIUM: 3, RISK_UNKNOWN: 2, RISK_LOW: 1, RISK_NA: 0}
        risks = [h.risk(self.has_gateway) for h in self.hits]
        return max(risks, key=lambda r: rank.get(r, 0)) if risks else RISK_NA

    @property
    def connectors(self) -> list[str]:
        seen: list[str] = []
        for h in self.hits:
            if h.connector_kind not in seen:
                seen.append(h.connector_kind)
        return seen

    @property
    def has_migrating_connector(self) -> bool:
        return any(h.is_migrating for h in self.hits)


@dataclass
class ValidationResult:
    artifact: ImpactedArtifact
    status: str  # passed | passed_with_regression | failed | skipped | no_new_refresh | refresh_not_triggered
    reason: str = ""
    pre_refresh: dict | None = None
    post_refresh: dict | None = None
    duration_delta_pct: float | None = None
    diagnosis: Diagnosis | None = None


# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #

_RISK_COLOR = {
    RISK_HIGH: "#c62828",       # deep red
    RISK_MEDIUM: "#ef6c00",     # amber
    RISK_LOW: "#2e7d32",        # deep green
    RISK_UNKNOWN: "#607d8b",    # blue-grey
    RISK_NA: "#9e9e9e",         # grey
}

_STATUS_COLOR = {
    "passed": "#2e7d32",
    "passed_with_regression": "#ef6c00",
    "failed": "#c62828",
    "no_new_refresh": "#607d8b",
    "refresh_not_triggered": "#607d8b",
    "skipped": "#9e9e9e",
}

_STYLE = """
<style>
  /* Fabric-inspired styling: Segoe UI, primary #117865 (Fabric brand teal), accent #742774 */
  .pqa-root {
    font-family: "Segoe UI", "Segoe UI Web (West European)", -apple-system, BlinkMacSystemFont, Roboto, "Helvetica Neue", sans-serif;
    color: #242424; padding: 4px 0;
    -webkit-font-smoothing: antialiased;
  }
  .pqa-banner {
    background: linear-gradient(135deg, #117865 0%, #0e6e5e 40%, #742774 100%);
    color: white; padding: 22px 28px; border-radius: 8px; margin-bottom: 20px;
    box-shadow: 0 4px 8px rgba(0,0,0,0.08);
    position: relative; overflow: hidden;
  }
  .pqa-banner::before {
    content: ""; position: absolute; top: -40px; right: -40px; width: 180px; height: 180px;
    background: radial-gradient(circle, rgba(255,255,255,0.12) 0%, transparent 70%);
    border-radius: 50%;
  }
  .pqa-banner-eyebrow {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 11px; font-weight: 600; letter-spacing: 0.8px; text-transform: uppercase;
    opacity: 0.85; margin-bottom: 6px;
  }
  .pqa-banner-eyebrow::before {
    content: ""; display: inline-block; width: 14px; height: 14px;
    background: white; border-radius: 3px;
    -webkit-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M4 4h8v8H4V4zm10 0h6v6h-6V4zm0 8h6v8h-6v-8zM4 14h8v6H4v-6z' fill='black'/></svg>") center / contain no-repeat;
            mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M4 4h8v8H4V4zm10 0h6v6h-6V4zm0 8h6v8h-6v-8zM4 14h8v6H4v-6z' fill='black'/></svg>") center / contain no-repeat;
  }
  .pqa-banner h1 { margin: 0 0 4px 0; font-size: 22px; font-weight: 600; letter-spacing: -0.2px; }
  .pqa-banner .pqa-sub { opacity: 0.85; font-size: 13px; font-family: "Cascadia Code", "Consolas", monospace; }

  .pqa-kpis { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
  .pqa-kpi {
    flex: 1 1 170px; min-width: 170px;
    background: #ffffff; border: 1px solid #edebe9; border-radius: 6px;
    padding: 14px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    transition: box-shadow 0.15s ease;
  }
  .pqa-kpi:hover { box-shadow: 0 2px 6px rgba(0,0,0,0.08); }
  .pqa-kpi .pqa-kpi-label {
    font-size: 11px; text-transform: uppercase; color: #616161;
    letter-spacing: 0.6px; font-weight: 600;
  }
  .pqa-kpi .pqa-kpi-value {
    font-size: 28px; font-weight: 600; margin-top: 4px; color: #242424;
    line-height: 1.1;
  }
  .pqa-kpi .pqa-kpi-sub { font-size: 12px; color: #616161; margin-top: 2px; }
  .pqa-kpi.pqa-high    { border-top: 3px solid #c50f1f; }
  .pqa-kpi.pqa-high    .pqa-kpi-value { color: #c50f1f; }
  .pqa-kpi.pqa-medium  { border-top: 3px solid #d83b01; }
  .pqa-kpi.pqa-medium  .pqa-kpi-value { color: #d83b01; }
  .pqa-kpi.pqa-low     { border-top: 3px solid #117865; }
  .pqa-kpi.pqa-low     .pqa-kpi-value { color: #117865; }
  .pqa-kpi.pqa-neutral { border-top: 3px solid #742774; }
  .pqa-kpi.pqa-neutral .pqa-kpi-value { color: #742774; }

  .pqa-section-title {
    margin: 24px 0 8px 0; font-size: 15px; font-weight: 600; color: #242424;
    display: flex; align-items: center; gap: 8px;
  }
  .pqa-section-title::before {
    content: ""; display: inline-block; width: 3px; height: 16px;
    background: linear-gradient(180deg, #117865 0%, #742774 100%);
    border-radius: 2px;
  }
  .pqa-section-desc  { font-size: 12.5px; color: #616161; margin-bottom: 12px; }
  .pqa-section-desc code { background: #f3f2f1; padding: 1px 5px; border-radius: 3px; font-size: 11.5px; }

  .pqa-pill {
    display: inline-block; padding: 2px 9px; border-radius: 11px; font-size: 11px; font-weight: 600;
    color: white; letter-spacing: 0.2px;
  }
  .pqa-badge-migrating  { background: #742774; }
  .pqa-badge-none       { background: #8a8886; }
  .pqa-badge-custom-dsn { background: #d83b01; }

  .pqa-table {
    border-collapse: collapse; width: 100%; font-size: 12.5px;
    background: white; border: 1px solid #edebe9; border-radius: 6px; overflow: hidden;
  }
  .pqa-table th {
    background: #faf9f8; text-align: left; padding: 9px 12px; font-weight: 600;
    border-bottom: 1px solid #edebe9; color: #323130; font-size: 12px;
    text-transform: uppercase; letter-spacing: 0.4px;
  }
  .pqa-table td { padding: 9px 12px; border-bottom: 1px solid #f3f2f1; vertical-align: top; }
  .pqa-table tr:last-child td { border-bottom: none; }
  .pqa-table tr:hover td { background: #f9f8f7; }
  .pqa-table code {
    background: #f3f2f1; padding: 1px 6px; border-radius: 3px;
    font-family: "Cascadia Code", "Consolas", monospace; font-size: 11.5px; color: #323130;
  }

  .pqa-diag {
    background: #fff4ce; border-left: 4px solid #d83b01;
    padding: 14px 18px; margin: 10px 0; border-radius: 4px;
  }
  .pqa-diag.pqa-failed  { background: #fde7e9; border-color: #c50f1f; }
  .pqa-diag.pqa-warn    { background: #fff4ce; border-color: #d83b01; }
  .pqa-diag h4 { margin: 0 0 6px 0; font-size: 14px; color: #242424; font-weight: 600; }
  .pqa-diag .pqa-cause { font-size: 12.5px; color: #323130; margin: 4px 0 10px 0; line-height: 1.5; }
  .pqa-diag ol { margin: 6px 0 6px 20px; padding: 0; font-size: 12.5px; color: #323130; line-height: 1.6; }
  .pqa-diag ol li { margin-bottom: 3px; }

  .pqa-conn-card {
    background: #ffffff; border: 1px solid #edebe9; border-radius: 6px;
    margin-bottom: 12px; overflow: hidden;
    box-shadow: 0 1px 2px rgba(0,0,0,0.03);
  }
  .pqa-conn-header {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 18px; border-bottom: 1px solid #f3f2f1;
  }
  .pqa-conn-header.pqa-status-ok    { background: linear-gradient(90deg, rgba(17,120,101,0.06) 0%, transparent 30%); border-left: 4px solid #117865; }
  .pqa-conn-header.pqa-status-fail  { background: linear-gradient(90deg, rgba(197,15,31,0.06) 0%, transparent 30%); border-left: 4px solid #c50f1f; }
  .pqa-conn-header.pqa-status-warn  { background: linear-gradient(90deg, rgba(216,59,1,0.06) 0%, transparent 30%); border-left: 4px solid #d83b01; }
  .pqa-conn-header.pqa-status-info  { background: linear-gradient(90deg, rgba(116,39,116,0.05) 0%, transparent 30%); border-left: 4px solid #742774; }
  .pqa-conn-header.pqa-status-none  { border-left: 4px solid #d2d0ce; }

  .pqa-status-icon {
    flex: 0 0 32px; width: 32px; height: 32px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 18px; font-weight: 700;
  }
  .pqa-status-icon.pqa-status-ok    { background: #117865; }
  .pqa-status-icon.pqa-status-fail  { background: #c50f1f; }
  .pqa-status-icon.pqa-status-warn  { background: #d83b01; }
  .pqa-status-icon.pqa-status-info  { background: #742774; }
  .pqa-status-icon.pqa-status-none  { background: #d2d0ce; color: #605e5c; }

  .pqa-conn-head-body { flex: 1; }
  .pqa-conn-name { font-size: 14.5px; font-weight: 600; color: #242424; }
  .pqa-conn-meta {
    font-size: 12px; color: #605e5c; margin-top: 3px;
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  }
  .pqa-conn-meta code {
    background: #f3f2f1; padding: 1px 6px; border-radius: 3px;
    font-family: "Cascadia Code", "Consolas", monospace; font-size: 11.5px; color: #323130;
  }
  .pqa-status-label {
    font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 12px;
    letter-spacing: 0.3px;
  }
  .pqa-status-label.pqa-status-ok    { background: #dff6dd; color: #0e6e5e; }
  .pqa-status-label.pqa-status-fail  { background: #fde7e9; color: #a80000; }
  .pqa-status-label.pqa-status-warn  { background: #fff4ce; color: #a4571e; }
  .pqa-status-label.pqa-status-info  { background: #f5eaf5; color: #5c1e5c; }
  .pqa-status-label.pqa-status-none  { background: #f3f2f1; color: #605e5c; }

  .pqa-conn-body { padding: 12px 18px 16px 18px; }
  .pqa-conn-body .pqa-artifact {
    font-size: 12px; color: #605e5c; margin-bottom: 6px;
  }
  .pqa-conn-body .pqa-artifact b { color: #323130; font-weight: 600; }
  .pqa-conn-body .pqa-excerpt {
    font-family: "Cascadia Code", "Consolas", monospace; font-size: 11.5px;
    background: #faf9f8; color: #323130; padding: 8px 10px;
    border-radius: 4px; border: 1px solid #f3f2f1; margin-top: 6px;
    white-space: pre-wrap; word-break: break-word; max-height: 60px; overflow-y: auto;
  }

  .pqa-diag-inline {
    margin-top: 12px; padding: 12px 14px; border-radius: 4px;
    background: #faf9f8; border-left: 3px solid;
  }
  .pqa-diag-inline.pqa-status-fail { border-color: #c50f1f; background: #fef7f8; }
  .pqa-diag-inline.pqa-status-warn { border-color: #d83b01; background: #fffbf0; }
  .pqa-diag-inline .pqa-diag-title {
    font-size: 13px; font-weight: 600; color: #242424; margin-bottom: 4px;
  }
  .pqa-diag-inline .pqa-diag-cause {
    font-size: 12px; color: #605e5c; margin-bottom: 8px; line-height: 1.5;
  }
  .pqa-diag-inline .pqa-diag-actions-label {
    font-size: 11px; font-weight: 600; color: #323130; text-transform: uppercase;
    letter-spacing: 0.4px; margin-bottom: 4px;
  }
  .pqa-diag-inline ol {
    margin: 0 0 0 20px; padding: 0; font-size: 12px; color: #323130; line-height: 1.6;
  }
  .pqa-diag-inline ol li { margin-bottom: 2px; }
  .pqa-diag-inline .pqa-diag-docs {
    margin-top: 8px; font-size: 11.5px;
  }
  .pqa-diag-inline .pqa-diag-docs a { color: #117865; text-decoration: none; font-weight: 600; }
  .pqa-diag-inline .pqa-diag-docs a:hover { text-decoration: underline; }

  /* Connector group (parent) + connection (child) layout */
  .pqa-connector-group {
    margin-bottom: 16px; background: #ffffff; border: 1px solid #edebe9;
    border-radius: 6px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.03);
  }
  .pqa-connector-head {
    padding: 12px 18px; background: #faf9f8; border-bottom: 1px solid #edebe9;
    display: flex; align-items: center; gap: 12px;
  }
  .pqa-connector-title {
    font-size: 15px; font-weight: 600; color: #242424; flex: 1;
  }
  .pqa-connector-count {
    font-size: 12px; color: #605e5c; background: #ffffff;
    padding: 2px 10px; border-radius: 12px; border: 1px solid #edebe9;
    font-weight: 500;
  }
  .pqa-connector-summary {
    font-size: 12px; color: #605e5c; display: flex; gap: 6px;
  }
  .pqa-summary-chip {
    padding: 2px 8px; border-radius: 10px; font-weight: 600; font-size: 11px;
  }
  .pqa-summary-chip.ok   { background: #dff6dd; color: #0e6e5e; }
  .pqa-summary-chip.fail { background: #fde7e9; color: #a80000; }
  .pqa-summary-chip.warn { background: #fff4ce; color: #a4571e; }

  .pqa-connection {
    padding: 14px 18px; border-bottom: 1px solid #f3f2f1;
    display: flex; gap: 14px; align-items: flex-start;
  }
  .pqa-connection:last-child { border-bottom: none; }
  .pqa-connection-icon {
    flex: 0 0 28px; width: 28px; height: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 15px; font-weight: 700; margin-top: 2px;
  }
  .pqa-connection-icon.ok   { background: #117865; }
  .pqa-connection-icon.fail { background: #c50f1f; }
  .pqa-connection-icon.warn { background: #d83b01; }
  .pqa-connection-icon.info { background: #742774; }
  .pqa-connection-icon.none { background: #d2d0ce; color: #605e5c; }

  .pqa-connection-body { flex: 1; min-width: 0; }
  .pqa-connection-title {
    font-size: 13.5px; font-weight: 600; color: #242424; margin-bottom: 3px;
    word-break: break-word;
  }
  .pqa-connection-title code {
    background: transparent; padding: 0; font-family: "Cascadia Code","Consolas",monospace;
    font-size: 13px; color: #242424;
  }
  .pqa-connection-meta {
    font-size: 12px; color: #605e5c; display: flex; flex-wrap: wrap;
    gap: 10px; align-items: center; margin-bottom: 6px;
  }
  .pqa-connection-meta code {
    background: #f3f2f1; padding: 1px 6px; border-radius: 3px;
    font-family: "Cascadia Code","Consolas",monospace; font-size: 11.5px; color: #323130;
  }
  .pqa-connection-artifact { font-size: 12px; color: #605e5c; }
  .pqa-connection-artifact b { color: #323130; font-weight: 600; }

  .pqa-fix-block {
    margin-top: 10px; padding: 10px 14px; border-radius: 4px;
    background: #faf9f8; border-left: 3px solid #742774;
  }
  .pqa-fix-block.fail { border-color: #c50f1f; background: #fef7f8; }
  .pqa-fix-block.warn { border-color: #d83b01; background: #fffbf0; }
  .pqa-fix-block.ok   { border-color: #117865; background: #f3fbf7; }
  .pqa-fix-title {
    font-size: 12.5px; font-weight: 600; color: #242424; margin-bottom: 4px;
  }
  .pqa-fix-cause {
    font-size: 12px; color: #605e5c; margin-bottom: 8px; line-height: 1.5;
  }
  .pqa-fix-actions-label {
    font-size: 11px; font-weight: 600; color: #323130; text-transform: uppercase;
    letter-spacing: 0.4px; margin-bottom: 4px;
  }
  .pqa-fix-block ol {
    margin: 0 0 0 20px; padding: 0; font-size: 12px; color: #323130; line-height: 1.6;
  }
  .pqa-fix-block ol li { margin-bottom: 2px; }
  .pqa-fix-docs { margin-top: 6px; font-size: 11.5px; }
  .pqa-fix-docs a { color: #117865; text-decoration: none; font-weight: 600; }
  .pqa-fix-docs a:hover { text-decoration: underline; }

  .pqa-footer {
    margin-top: 24px; padding-top: 14px; border-top: 1px solid #edebe9;
    font-size: 11px; color: #8a8886;
    display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  }
  .pqa-footer a { color: #117865; text-decoration: none; font-weight: 600; }
  .pqa-footer a:hover { text-decoration: underline; }

  .pqa-preview-banner {
    background: #fff8e1; border-left: 4px solid #d29200;
    padding: 12px 16px 12px 18px; border-radius: 4px;
    margin-bottom: 20px; font-size: 12.5px; color: #3b3a39; line-height: 1.55;
  }
  .pqa-preview-banner b { color: #242424; font-weight: 600; }
  .pqa-preview-banner a { color: #117865; font-weight: 600; text-decoration: none; }
  .pqa-preview-banner a:hover { text-decoration: underline; }
</style>
"""


_PREVIEW_BANNER = (
    '<div class="pqa-preview-banner">'
    '<b>Preview release.</b> This is an early tool built to accelerate ADBC migration testing. '
    'Results reflect what we can see from item definitions and refresh history — some connectors, '
    'custom M patterns, and error types may not be recognized yet. Treat this as a starting point '
    'for triage, not a final verdict. For authoritative guidance, refer to the '
    '<a href="https://learn.microsoft.com/power-query/transition-to-adbc" target="_blank">'
    'Transition to ADBC docs</a>.'
    '</div>'
)


def _icon(kind: str) -> str:
    return {"ok": "&#10003;", "fail": "&#10007;", "warn": "!", "info": "i", "none": "&#183;"}.get(kind, "&#183;")


def _label(kind: str) -> str:
    return {"ok": "Ready", "fail": "Will fail", "warn": "Needs review", "info": "Info", "none": "Not in migration"}.get(kind, "")


def _render_connection_row(
    *,
    status_kind: str,
    status_label: str,
    connection_title: str,       # main line, usually the endpoint
    connection_title_is_code: bool,
    meta_chips: list[str],       # small chips like "Implementation=1.0", "custom DSN", "gateway"
    artifact_line: str,          # "In: Sales Model (SemanticModel)"
    diagnosis: Diagnosis | None,
) -> str:
    title_html = (
        f"<code>{_escape(connection_title)}</code>"
        if connection_title_is_code
        else _escape(connection_title)
    )
    chips_html = " ".join(
        f'<span class="pqa-summary-chip warn">{_escape(c)}</span>' for c in meta_chips
    )
    fix_html = ""
    if diagnosis is not None:
        fix_class = "fail" if status_kind == "fail" else ("warn" if status_kind == "warn" else "ok")
        actions = "".join(f"<li>{_escape(a)}</li>" for a in diagnosis.suggested_actions)
        docs = (
            f'<div class="pqa-fix-docs">&#8594; <a href="{diagnosis.docs}" target="_blank">Learn more</a></div>'
            if diagnosis.docs else ""
        )
        fix_html = (
            f'<div class="pqa-fix-block {fix_class}">'
            f'  <div class="pqa-fix-title">{_escape(diagnosis.issue)}</div>'
            f'  <div class="pqa-fix-cause">{_escape(diagnosis.likely_cause)}</div>'
            f'  <div class="pqa-fix-actions-label">Recommended fix</div>'
            f'  <ol>{actions}</ol>'
            f'  {docs}'
            f'</div>'
        )

    label_pill = (
        f'<span class="pqa-summary-chip {"ok" if status_kind=="ok" else "fail" if status_kind=="fail" else "warn"}">'
        f'{_escape(status_label)}</span>'
    )

    return (
        f'<div class="pqa-connection">'
        f'  <div class="pqa-connection-icon {status_kind}">{_icon(status_kind)}</div>'
        f'  <div class="pqa-connection-body">'
        f'    <div class="pqa-connection-title">{title_html}</div>'
        f'    <div class="pqa-connection-meta">{label_pill} {chips_html}</div>'
        f'    <div class="pqa-connection-artifact">{artifact_line}</div>'
        f'    {fix_html}'
        f'  </div>'
        f'</div>'
    )


def _render_connector_group(
    connector_kind: str,
    connections_html: list[str],
    ok_count: int,
    warn_count: int,
    fail_count: int,
    skipped_count: int = 0,
) -> str:
    total = ok_count + warn_count + fail_count + skipped_count
    chips = []
    if ok_count:      chips.append(f'<span class="pqa-summary-chip ok">{ok_count} ok</span>')
    if warn_count:    chips.append(f'<span class="pqa-summary-chip warn">{warn_count} review</span>')
    if fail_count:    chips.append(f'<span class="pqa-summary-chip fail">{fail_count} failed</span>')
    if skipped_count: chips.append(f'<span class="pqa-summary-chip">{skipped_count} n/a</span>')
    return (
        f'<div class="pqa-connector-group">'
        f'  <div class="pqa-connector-head">'
        f'    <div class="pqa-connector-title">{_escape(connector_kind)}</div>'
        f'    <div class="pqa-connector-summary">{" ".join(chips)}</div>'
        f'    <div class="pqa-connector-count">{total} connection{"s" if total != 1 else ""}</div>'
        f'  </div>'
        f'  {"".join(connections_html)}'
        f'</div>'
    )


def _fmt_pill(text: str, css_class: str) -> str:
    return f'<span class="pqa-pill {css_class}">{text}</span>'


def _fmt_risk_cell(risk: str) -> str:
    color = _RISK_COLOR.get(risk, "#9e9e9e")
    return f'<span style="color:{color};font-weight:600;">{risk}</span>'


def _fmt_migration_cell(mig: str, is_migrating: bool) -> str:
    if is_migrating:
        # odbc_to_adbc:snowflake -> "ADBC · snowflake"
        parts = mig.split(":", 1)
        label = parts[1] if len(parts) == 2 else mig
        return _fmt_pill(f"ADBC · {label}", "pqa-badge-migrating")
    return _fmt_pill("not in migration", "pqa-badge-none")


def _kpi_card(label: str, value: str, sub: str = "", tone: str = "neutral") -> str:
    return (
        f'<div class="pqa-kpi pqa-{tone}">'
        f'<div class="pqa-kpi-label">{label}</div>'
        f'<div class="pqa-kpi-value">{value}</div>'
        f'<div class="pqa-kpi-sub">{sub}</div>'
        f"</div>"
    )


# --------------------------------------------------------------------------- #
# ImpactReport
# --------------------------------------------------------------------------- #

@dataclass
class ImpactReport:
    workspace_id: str
    scope: str = "workspace"  # or "tenant"
    tool_version: str = TOOL_VERSION
    artifacts: list[ImpactedArtifact] = field(default_factory=list)
    fabric_connections: list[dict[str, Any]] = field(default_factory=list)
    fabric_connections_error: str | None = None
    skipped: list[dict[str, str]] = field(default_factory=list)

    def add(self, artifact: ImpactedArtifact) -> None:
        self.artifacts.append(artifact)

    def record_skipped(self, item_id: str, name: str, item_type: str, reason: str) -> None:
        self.skipped.append(
            {"item_id": item_id, "item_name": name, "item_type": item_type, "reason": reason}
        )

    # ---- Aggregates ---------------------------------------------------- #

    def _counts(self) -> dict[str, Any]:
        counts_by_risk = {RISK_HIGH: 0, RISK_MEDIUM: 0, RISK_LOW: 0, RISK_UNKNOWN: 0, RISK_NA: 0}
        counts_by_connector: dict[str, int] = {}
        counts_by_migration: dict[str, int] = {}
        pinned_odbc = 0
        migrating_artifacts = 0
        total_calls = 0
        custom_dsn = 0
        for a in self.artifacts:
            counts_by_risk[a.worst_risk] = counts_by_risk.get(a.worst_risk, 0) + 1
            if a.has_migrating_connector:
                migrating_artifacts += 1
            for h in a.hits:
                total_calls += 1
                counts_by_connector[h.connector_kind] = counts_by_connector.get(h.connector_kind, 0) + 1
                counts_by_migration[h.migration] = counts_by_migration.get(h.migration, 0) + 1
                if h.is_pinned_odbc:
                    pinned_odbc += 1
                if h.custom_dsn:
                    custom_dsn += 1
        return {
            "counts_by_risk": counts_by_risk,
            "counts_by_connector": counts_by_connector,
            "counts_by_migration": counts_by_migration,
            "pinned_odbc": pinned_odbc,
            "migrating_artifacts": migrating_artifacts,
            "total_calls": total_calls,
            "custom_dsn": custom_dsn,
        }

    # ---- Views --------------------------------------------------------- #

    def to_dataframe(self):
        """One row per connector call across the workspace."""
        import pandas as pd

        rows: list[dict[str, Any]] = []
        for a in self.artifacts:
            for h in a.hits:
                rows.append(
                    {
                        "workspace_id": a.workspace_id,
                        "workspace_name": a.workspace_name,
                        "item_id": a.item_id,
                        "item_name": a.item_name,
                        "item_type": a.item_type,
                        "connector_kind": h.connector_kind,
                        "m_function": h.m_function,
                        "migration": h.migration,
                        "is_migrating": h.is_migrating,
                        "implementation": h.implementation or "",
                        "custom_dsn": h.custom_dsn,
                        "endpoint_hint": h.endpoint_hint or "",
                        "has_gateway": a.has_gateway,
                        "risk": h.risk(a.has_gateway),
                        "excerpt": h.excerpt,
                    }
                )
        return pd.DataFrame(rows)

    def fabric_connections_dataframe(self):
        import pandas as pd

        rows = []
        for c in self.fabric_connections:
            details = c.get("connectionDetails", {}) or {}
            rows.append(
                {
                    "connection_id": c.get("id", ""),
                    "display_name": c.get("displayName", ""),
                    "connectivity_type": c.get("connectivityType", ""),
                    "connector_type": details.get("type", ""),
                    "gateway_id": c.get("gatewayId", ""),
                    "path": details.get("path", ""),
                }
            )
        return pd.DataFrame(rows)

    def summary(self) -> dict[str, Any]:
        c = self._counts()
        result = {
            "scope": self.scope,
            "workspace_id": self.workspace_id,
            "artifact_count": len(self.artifacts),
            "artifacts_with_migrating_connector": c["migrating_artifacts"],
            "total_connector_calls": c["total_calls"],
            "connector_calls_pinned_to_odbc": c["pinned_odbc"],
            "custom_dsn_calls": c["custom_dsn"],
            "counts_by_risk": c["counts_by_risk"],
            "counts_by_migration": c["counts_by_migration"],
            "counts_by_connector": c["counts_by_connector"],
            "fabric_connection_count": len(self.fabric_connections),
            "skipped_count": len(self.skipped),
            "tool_version": self.tool_version,
        }
        _pretty_print(result)
        return result

    # ---- Notebook rendering ------------------------------------------- #

    def _repr_html_(self) -> str:
        c = self._counts()
        counts_risk = c["counts_by_risk"]
        n_high = counts_risk[RISK_HIGH]
        n_medium = counts_risk[RISK_MEDIUM]

        # KPI cards
        kpis = [
            _kpi_card("Artifacts scanned", str(len(self.artifacts)),
                      f"{c['migrating_artifacts']} touch a migrating connector"),
            _kpi_card("Connections", str(c["total_calls"]),
                      f"{c['pinned_odbc']} pinned to ODBC · {c['custom_dsn']} custom DSN"),
            _kpi_card(
                "Will fail", str(n_high),
                "at cutover" if n_high else "no immediate breaks",
                tone="high" if n_high else "low",
            ),
            _kpi_card(
                "Needs review", str(n_medium),
                "gateway-backed or DSN-shaped" if n_medium else "clean",
                tone="medium" if n_medium else "low",
            ),
            _kpi_card("Fabric Connections", str(len(self.fabric_connections)),
                      "shared cloud connections", tone="neutral"),
        ]

        # Group connections by connector kind
        from .troubleshoot import diagnose_connector_call
        groups: dict[str, list[tuple[ImpactedArtifact, ConnectorCall, str, Diagnosis | None]]] = {}
        for a in self.artifacts:
            for h in a.hits:
                risk = h.risk(a.has_gateway)
                if risk == RISK_HIGH:
                    status_kind = "fail"
                elif risk == RISK_MEDIUM:
                    status_kind = "warn"
                elif risk == RISK_UNKNOWN:
                    status_kind = "warn"
                elif risk == RISK_NA:
                    status_kind = "none"
                else:
                    status_kind = "ok"
                diag = diagnose_connector_call(
                    is_pinned_odbc=h.is_pinned_odbc,
                    is_pinned_adbc=h.is_pinned_adbc,
                    is_migrating=h.is_migrating,
                    custom_dsn=h.custom_dsn,
                    has_gateway=a.has_gateway,
                )
                groups.setdefault(h.connector_kind, []).append((a, h, status_kind, diag))

        # Sort connectors: migrating first (by count desc), then non-migrating
        def _sort_key(kv):
            kind, rows = kv
            any_migrating = any(row[1].is_migrating for row in rows)
            return (0 if any_migrating else 1, -len(rows), kind)

        groups_html = []
        for kind, rows in sorted(groups.items(), key=_sort_key):
            ok = warn = fail = na = 0
            conn_htmls = []
            for artifact, call, status_kind, diag in rows:
                if status_kind == "fail": fail += 1
                elif status_kind == "warn": warn += 1
                elif status_kind == "ok":   ok += 1
                elif status_kind == "none": na += 1

                # Title = endpoint if we have one, else the m_function
                if call.endpoint_hint:
                    title = call.endpoint_hint
                    title_is_code = True
                else:
                    title = call.m_function + "(...)"
                    title_is_code = True

                chips = []
                if call.implementation:
                    chips.append(f'Implementation="{call.implementation}"')
                if call.custom_dsn:
                    chips.append("custom DSN")
                if artifact.has_gateway is True:
                    chips.append("via gateway")
                elif artifact.has_gateway is False and call.is_migrating:
                    chips.append("no gateway")

                artifact_line = (
                    f'In: <b>{_escape(artifact.item_name)}</b> '
                    f'<span style="color:#a19f9d;font-size:11px;">({_escape(artifact.item_type)})</span>'
                )

                conn_htmls.append(_render_connection_row(
                    status_kind=status_kind,
                    status_label=_label(status_kind),
                    connection_title=title,
                    connection_title_is_code=title_is_code,
                    meta_chips=chips,
                    artifact_line=artifact_line,
                    diagnosis=diag,
                ))

            groups_html.append(_render_connector_group(kind, conn_htmls, ok, warn, fail, na))

        if not groups_html:
            groups_html = ['<div class="pqa-section-desc">No external connector calls found in this workspace.</div>']

        # Fabric connections table (top 10) - kept as a compact table
        conn_html = ""
        if self.fabric_connections_error:
            conn_html = (
                '<div class="pqa-section-title">Shared Fabric Connections</div>'
                f'<div class="pqa-section-desc" style="color:#a4571e;">'
                f'Could not enumerate Fabric Connections '
                f'(<code>{_escape(self.fabric_connections_error)}</code>). '
                'This API is often blocked for non-admins; ask a Fabric admin to run the scan if you need the shared-connection inventory.'
                '</div>'
            )
        elif self.fabric_connections:
            top = self.fabric_connections[:10]
            conn_rows = ""
            for cn in top:
                details = cn.get("connectionDetails", {}) or {}
                gw_raw = cn.get("gatewayId")
                gw = _escape(gw_raw) if gw_raw else '<span style="color:#a19f9d;">—</span>'
                conn_rows += (
                    f"<tr><td>{_escape(cn.get('displayName',''))}</td>"
                    f"<td>{_escape(details.get('type',''))}</td>"
                    f"<td>{_escape(cn.get('connectivityType',''))}</td>"
                    f"<td style='font-size:11px;color:#605e5c;'>{gw}</td></tr>"
                )
            more = ""
            if len(self.fabric_connections) > 10:
                more = f'<div class="pqa-section-desc">…and {len(self.fabric_connections) - 10} more.</div>'
            conn_html = (
                '<div class="pqa-section-title">Shared Fabric Connections</div>'
                '<table class="pqa-table">'
                "<thead><tr><th>Name</th><th>Type</th><th>Connectivity</th><th>Gateway</th></tr></thead>"
                f"<tbody>{conn_rows}</tbody></table>{more}"
            )

        scope_label = "tenant scan" if self.scope == "tenant" else "workspace scan"
        return (
            _STYLE +
            '<div class="pqa-root">'
            '<div class="pqa-banner">'
            '<div class="pqa-banner-eyebrow">Microsoft Fabric &middot; Power Query</div>'
            "<h1>Connector Upgrade Advisor</h1>"
            f'<div class="pqa-sub">{scope_label} &middot; workspace {_escape(self.workspace_id)}</div>'
            "</div>"
            f'{_PREVIEW_BANNER}'
            f'<div class="pqa-kpis">{"".join(kpis)}</div>'
            '<div class="pqa-section-title">Connections by connector</div>'
            '<div class="pqa-section-desc">'
            "Each connector groups its individual connections. "
            "<b>&#10003; Ready</b> = tenant switch will handle it. "
            "<b>! Needs review</b> = custom DSN or gateway-backed ODBC pin. "
            "<b>&#10007; Will fail</b> = ODBC-pinned without a gateway; breaks at cutover."
            "</div>"
            f'{"".join(groups_html)}'
            f"{conn_html}"
            '<div class="pqa-footer">'
            f'<span>Generated by pq-adbc-advisor {TOOL_VERSION}</span>'
            '<span><a href="https://learn.microsoft.com/power-query/transition-to-adbc">Transition to ADBC (Microsoft Learn)</a></span>'
            "</div>"
            "</div>"
        )

    def display(self) -> None:
        """Show the rendered HTML in the current notebook (works in Fabric/Jupyter)."""
        try:
            from IPython.display import HTML, display
            display(HTML(self._repr_html_()))
        except Exception:
            _pretty_print(self.summary())

    def to_html(self, path: str, title: str = "PQ Connector Inventory & ADBC Impact") -> str:
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title></head><body>"
            f"{self._repr_html_()}"
            "</body></html>"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path


# --------------------------------------------------------------------------- #
# ValidationReport
# --------------------------------------------------------------------------- #

@dataclass
class ValidationReport:
    baseline_workspace_id: str
    tool_version: str = TOOL_VERSION
    results: list[ValidationResult] = field(default_factory=list)

    def add(self, result: ValidationResult) -> None:
        self.results.append(result)

    def _counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    def to_dataframe(self):
        import pandas as pd

        rows = []
        for r in self.results:
            diag = r.diagnosis
            rows.append(
                {
                    "workspace_id": r.artifact.workspace_id,
                    "item_id": r.artifact.item_id,
                    "item_name": r.artifact.item_name,
                    "item_type": r.artifact.item_type,
                    "connectors": ", ".join(r.artifact.connectors),
                    "status": r.status,
                    "reason": (r.reason or "")[:400],
                    "duration_delta_pct": r.duration_delta_pct,
                    "pre_refresh_end": (r.pre_refresh or {}).get("endTime"),
                    "post_refresh_end": (r.post_refresh or {}).get("endTime"),
                    "diagnosis_issue": diag.issue if diag else "",
                    "diagnosis_likely_cause": diag.likely_cause if diag else "",
                    "diagnosis_suggested_actions": "; ".join(diag.suggested_actions) if diag else "",
                    "diagnosis_docs": diag.docs if diag else "",
                }
            )
        return pd.DataFrame(rows)

    def summary(self) -> dict[str, Any]:
        counts = self._counts()
        issues: dict[str, int] = {}
        for r in self.results:
            if r.diagnosis:
                issues[r.diagnosis.issue] = issues.get(r.diagnosis.issue, 0) + 1
        result = {
            "baseline_workspace_id": self.baseline_workspace_id,
            "validated_count": len(self.results),
            "counts_by_status": counts,
            "counts_by_diagnosed_issue": issues,
            "tool_version": self.tool_version,
        }
        _pretty_print(result)
        return result

    # ---- Notebook rendering ------------------------------------------- #

    def _repr_html_(self) -> str:
        counts = self._counts()
        n_pass = counts.get("passed", 0)
        n_regress = counts.get("passed_with_regression", 0)
        n_fail = counts.get("failed", 0)
        n_skip = sum(counts.get(s, 0) for s in ("skipped", "no_new_refresh", "refresh_not_triggered"))

        kpis = [
            _kpi_card("Passed", str(n_pass), "clean refresh", tone="low"),
            _kpi_card("With regression", str(n_regress),
                      "slower than baseline" if n_regress else "no regressions",
                      tone="medium" if n_regress else "low"),
            _kpi_card("Failed", str(n_fail), "needs attention",
                      tone="high" if n_fail else "low"),
            _kpi_card("Skipped", str(n_skip),
                      "not validated", tone="neutral"),
        ]

        # Group by connector kind. Each artifact contributes AT MOST ONE
        # row per unique connector_kind - the refresh outcome is at the
        # artifact level, not per M call, so duplicating it across every
        # call would falsely paint unrelated connectors with the artifact
        # status (e.g. a Snowflake failure showing red on a SQL Server
        # side-connection in the same model).
        groups: dict[str, list[tuple[ValidationResult, ConnectorCall, str]]] = {}
        for r in self.results:
            if r.status == "passed":
                status_kind = "ok"
            elif r.status == "passed_with_regression":
                status_kind = "warn"
            elif r.status == "failed":
                status_kind = "fail"
            elif r.status in ("no_new_refresh", "refresh_not_triggered"):
                status_kind = "warn"
            else:
                status_kind = "none"

            # Dedupe: prefer the first migrating call per connector kind,
            # falling back to the first call.
            seen_kinds: set[str] = set()
            preferred_call_by_kind: dict[str, ConnectorCall] = {}
            for call in r.artifact.hits:
                kind = call.connector_kind
                if kind in seen_kinds and not call.is_migrating:
                    continue
                if kind not in preferred_call_by_kind or (
                    call.is_migrating and not preferred_call_by_kind[kind].is_migrating
                ):
                    preferred_call_by_kind[kind] = call
                seen_kinds.add(kind)

            for kind, call in preferred_call_by_kind.items():
                groups.setdefault(kind, []).append((r, call, status_kind))

        # sort: any-failed first, then any-warn, then all-ok
        def _sort_key(kv):
            kind, rows = kv
            has_fail = any(row[2] == "fail" for row in rows)
            has_warn = any(row[2] == "warn" for row in rows)
            return (0 if has_fail else 1 if has_warn else 2, -len(rows), kind)

        groups_html = []
        for kind, rows in sorted(groups.items(), key=_sort_key):
            ok = warn = fail = skipped = 0
            conn_htmls = []
            for r, call, status_kind in rows:
                if status_kind == "fail": fail += 1
                elif status_kind == "warn": warn += 1
                elif status_kind == "ok": ok += 1
                else: skipped += 1

                title = call.endpoint_hint or (call.m_function + "(...)")
                chips = []
                if call.implementation:
                    chips.append(f'Implementation="{call.implementation}"')
                if call.custom_dsn:
                    chips.append("custom DSN")
                if r.duration_delta_pct is not None:
                    sign = "+" if r.duration_delta_pct >= 0 else ""
                    chips.append(f"{sign}{r.duration_delta_pct:.1f}% vs baseline")

                # The refresh outcome is at the model level. For non-migrating
                # connectors we surface the outcome but NOT the ADBC-specific
                # diagnosis - a fix pointing at OAuth2 rebinding doesn't
                # apply to a SQL Server side-source in the same model.
                show_diagnosis = call.is_migrating

                artifact_line = (
                    f'In: <b>{_escape(r.artifact.item_name)}</b> '
                    f'<span style="color:#a19f9d;font-size:11px;">({_escape(r.artifact.item_type)})</span> &middot; '
                    f'refresh <b>{_escape(r.status)}</b>'
                )
                if not show_diagnosis and status_kind == "fail":
                    artifact_line += (
                        ' <span style="color:#a19f9d;">'
                        '(not part of the ADBC migration &mdash; fix guidance not shown)'
                        '</span>'
                    )

                label = {
                    "ok": "Refresh passed",
                    "warn": "Regression" if r.status == "passed_with_regression" else "No refresh",
                    "fail": "Refresh failed",
                    "none": "Skipped",
                }.get(status_kind, r.status)

                conn_htmls.append(_render_connection_row(
                    status_kind=status_kind,
                    status_label=label,
                    connection_title=title,
                    connection_title_is_code=True,
                    meta_chips=chips,
                    artifact_line=artifact_line,
                    diagnosis=r.diagnosis if show_diagnosis else None,
                ))

            groups_html.append(_render_connector_group(kind, conn_htmls, ok, warn, fail, skipped))

        if not groups_html:
            groups_html = ['<div class="pqa-section-desc">No validation results.</div>']

        return (
            _STYLE +
            '<div class="pqa-root">'
            '<div class="pqa-banner">'
            '<div class="pqa-banner-eyebrow">Microsoft Fabric &middot; Power Query</div>'
            "<h1>ADBC Migration Validation</h1>"
            f'<div class="pqa-sub">workspace {_escape(self.baseline_workspace_id)}</div>'
            "</div>"
            f'{_PREVIEW_BANNER}'
            f'<div class="pqa-kpis">{"".join(kpis)}</div>'
            '<div class="pqa-section-title">Refresh outcome by connection</div>'
            '<div class="pqa-section-desc">'
            "Each connection shows its post-migration refresh outcome. "
            "<b>&#10003; Refresh passed</b> = clean refresh. "
            "<b>! Regression</b> = passed but noticeably slower. "
            "<b>&#10007; Refresh failed</b> = needs the fix listed below."
            "</div>"
            f'{"".join(groups_html)}'
            '<div class="pqa-footer">'
            f'<span>Generated by pq-adbc-advisor {TOOL_VERSION}</span>'
            '<span><a href="https://learn.microsoft.com/power-query/transition-to-adbc">Transition to ADBC (Microsoft Learn)</a></span>'
            "</div>"
            "</div>"
        )

    def display(self) -> None:
        try:
            from IPython.display import HTML, display
            display(HTML(self._repr_html_()))
        except Exception:
            _pretty_print(self.summary())

    def to_html(self, path: str, title: str = "PQ ADBC Validation Report") -> str:
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title></head><body>"
            f"{self._repr_html_()}"
            "</body></html>"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _pretty_print(summary: dict[str, Any]) -> None:
    print("=" * 68)
    for k, v in summary.items():
        print(f"{k:>34}: {v}")
    print("=" * 68)


def _escape(text: str | None) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
