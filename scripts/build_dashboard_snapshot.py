#!/usr/bin/env python3
"""Rebuild the ADBC Insights dashboard data snapshot.

Runs on a schedule (or one-shot locally). Queries Application Insights +
GitHub, writes `dashboard/data/snapshot.json` for the static HTML to fetch.
Also self-heals if a metric is missing so a bad query never bricks the dash.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


REPO = os.environ.get("REPO", "MichaelaIsaacs/pq-adbc-advisor")
APP_ID = os.environ.get("AI_APP_ID", "9d5ab587-21ab-4b01-ad3a-e43e2fccd8dc")
OUT_PATH = os.environ.get("OUT_PATH", "dashboard/data/snapshot.json")


def _ai_token() -> str | None:
    """Try az CLI first (for local runs), fall back to None so caller
    can use an API key instead (for GitHub Actions)."""
    try:
        return subprocess.check_output(
            ["az", "account", "get-access-token",
             "--resource", "https://api.applicationinsights.io",
             "--query", "accessToken", "-o", "tsv"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def kql(query: str, token: str | None) -> dict:
    url = (f"https://api.applicationinsights.io/v1/apps/{APP_ID}/query"
           f"?query={urllib.parse.quote(query)}")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api_key = os.environ.get("AI_API_KEY", "")
    if api_key and not token:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"{e.code} {e.reason}"}
    tables = data.get("tables", [])
    if not tables:
        return {"rows": [], "columns": []}
    t = tables[0]
    cols = [c["name"] for c in t["columns"]]
    return {"columns": cols, "rows": [dict(zip(cols, r)) for r in t["rows"]]}


def _gh(path: str, token: str) -> dict | list:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"{e.code} {e.reason}"}


def build_snapshot() -> dict:
    token = _ai_token()
    gh_token = os.environ.get("GITHUB_TOKEN", "")

    snap: dict = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "repo": REPO,
    }

    # ── Reach ─────────────────────────────────────────────────────────
    reach = kql(
        'customEvents | where name == "scan_complete" '
        '| summarize scans=count(), '
        'workspaces=dcount(tostring(customDimensions.workspace_id)), '
        'tenants=dcount(tostring(customDimensions.tenant_hash))',
        token,
    )
    snap["reach"] = reach["rows"][0] if reach.get("rows") else {}

    # ── Resolution rollup ─────────────────────────────────────────────
    res = kql(
        'customEvents | where name == "scan_complete" '
        'and tostring(customDimensions.is_first_run) == "False" '
        '| summarize '
        'resolved_pinned=sum(toint(customDimensions.resolved_pinned_odbc)), '
        'resolved_high=sum(toint(customDimensions.resolved_risk_high)), '
        'resolved_medium=sum(toint(customDimensions.resolved_risk_medium)), '
        'resolved_custom_dsn=sum(toint(customDimensions.resolved_custom_dsn)), '
        'return_scans=count()',
        token,
    )
    snap["resolution"] = res["rows"][0] if res.get("rows") else {}

    # ── Value narrative ───────────────────────────────────────────────
    val = kql(
        'customEvents | where name == "scan_complete" '
        '| summarize '
        'hours_saved=sum(toreal(customDimensions.estimated_manual_hours_saved)), '
        'inspected=sum(toint(customDimensions.inspected_artifacts)), '
        'median_coverage=percentile(toint(customDimensions.coverage_score_pct), 50), '
        'sempy_used_pct=100.0 * countif(tostring(customDimensions.sempy_used) == "True") / count()',
        token,
    )
    snap["value"] = val["rows"][0] if val.get("rows") else {}

    # ── Daily scan trend ──────────────────────────────────────────────
    trend = kql(
        'customEvents | where name == "scan_complete" and timestamp > ago(60d) '
        '| summarize scans=count(), workspaces=dcount(tostring(customDimensions.workspace_id)) '
        'by bin(timestamp, 1d) | order by timestamp asc',
        token,
    )
    snap["trend"] = trend.get("rows", [])

    # ── Version adoption ──────────────────────────────────────────────
    ver = kql(
        'customEvents | where name == "scan_complete" '
        '| summarize scans=count(), workspaces=dcount(tostring(customDimensions.workspace_id)) '
        'by version=tostring(customDimensions.version) | order by version desc',
        token,
    )
    snap["versions"] = ver.get("rows", [])

    # ── Per-workspace progress ────────────────────────────────────────
    perws = kql(
        'customEvents | where name == "scan_complete" '
        '| project timestamp, '
        'workspace=tostring(customDimensions.workspace_id), '
        'is_first=tostring(customDimensions.is_first_run), '
        'run_count=toint(customDimensions.run_count), '
        'first_pinned=toint(customDimensions.first_pinned_odbc), '
        'current_pinned=toint(customDimensions.current_pinned_odbc), '
        'resolved_pinned=toint(customDimensions.resolved_pinned_odbc), '
        'coverage=toint(customDimensions.coverage_score_pct), '
        'version=tostring(customDimensions.version) '
        '| summarize arg_max(timestamp, *) by workspace | order by resolved_pinned desc',
        token,
    )
    snap["per_workspace"] = perws.get("rows", [])

    # ── Skip reasons ──────────────────────────────────────────────────
    skip = kql(
        'customEvents | where name == "scan_complete" '
        '| project cds=customDimensions '
        '| mv-expand cd = bag_keys(cds) '
        '| where tostring(cd) startswith "skipped_by_reason_" '
        '| extend reason=replace_string(tostring(cd), "skipped_by_reason_", ""), '
        'n=toint(cds[tostring(cd)]) '
        '| summarize count=sum(n) by reason | order by count desc',
        token,
    )
    snap["skip_reasons"] = skip.get("rows", [])

    # ── State backend split ───────────────────────────────────────────
    backends = kql(
        'customEvents | where name == "scan_complete" '
        '| summarize scans=count() by backend=tostring(customDimensions.state_backend)',
        token,
    )
    snap["state_backends"] = backends.get("rows", [])

    # ── First-run vs return ───────────────────────────────────────────
    fr = kql(
        'customEvents | where name == "scan_complete" '
        '| summarize scans=count() by is_first_run=tostring(customDimensions.is_first_run)',
        token,
    )
    snap["first_vs_return"] = fr.get("rows", [])

    # ── GitHub reach ──────────────────────────────────────────────────
    gh = kql(
        'customEvents | where name == "github_traffic" | top 1 by timestamp desc '
        '| project customDimensions',
        token,
    )
    if gh.get("rows"):
        raw = gh["rows"][0].get("customDimensions")
        try:
            snap["github"] = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            snap["github"] = {}
    else:
        snap["github"] = {}

    # ── Release list (fresh) ──────────────────────────────────────────
    if gh_token:
        rels = _gh(f"/repos/{REPO}/releases", gh_token)
        if isinstance(rels, list):
            snap["releases"] = [
                {
                    "tag": r.get("tag_name"),
                    "name": r.get("name"),
                    "published": r.get("published_at"),
                    "downloads": sum(a.get("download_count", 0) for a in r.get("assets", [])),
                    "url": r.get("html_url"),
                }
                for r in rels
            ]

    return snap


def main() -> int:
    snap = build_snapshot()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(snap, f, indent=2)
    print(f"wrote {OUT_PATH}")
    print(f"  reach: {snap.get('reach')}")
    print(f"  resolution: {snap.get('resolution')}")
    print(f"  value: {snap.get('value')}")
    print(f"  github clones_14d: {snap.get('github', {}).get('clones_14d')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
