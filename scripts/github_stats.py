#!/usr/bin/env python3
"""Report distribution reach for pq-adbc-advisor.

Combines three data sources that together answer "how many people are
picking this up":

  1. GitHub Traffic API — 14-day rolling window of clones and views.
     Requires a token with ``repo`` scope (or Fine-grained "Read
     Administration" on the repo). Fields returned:
       * clones.count / clones.uniques
       * views.count / views.uniques
     See https://docs.github.com/rest/metrics/traffic

  2. GitHub Releases API — per-asset download counts (cumulative
     since the release was published, not windowed).

  3. Application Insights — count of unique workspace_ids that ran
     ``scan_complete``, per version. This is the closest signal we have
     to "actual users" because a clone / release download can be a bot
     or a mirror, but a scan_complete only lands after a real Fabric
     tenant has run the tool at least once.

Usage:

    export GITHUB_TOKEN=ghp_...
    export AI_APP_ID=9d5ab587-21ab-4b01-ad3a-e43e2fccd8dc
    python scripts/github_stats.py

    # Or just the GitHub half (no Azure CLI required):
    python scripts/github_stats.py --skip-ai

The script fails soft: any of the three sources can be missing / 401
and the others still report. Nothing is written anywhere; output is
printed as three tables.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any


REPO = "MichaelaIsaacs/pq-adbc-advisor"


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #

def _gh_get(path: str, token: str) -> Any:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def gh_traffic(token: str) -> dict[str, Any]:
    clones = _gh_get(f"/repos/{REPO}/traffic/clones", token)
    views = _gh_get(f"/repos/{REPO}/traffic/views", token)
    referrers = _gh_get(f"/repos/{REPO}/traffic/popular/referrers", token)
    paths = _gh_get(f"/repos/{REPO}/traffic/popular/paths", token)
    return {"clones": clones, "views": views, "referrers": referrers, "paths": paths}


def gh_releases(token: str) -> list[dict[str, Any]]:
    return _gh_get(f"/repos/{REPO}/releases", token)


def print_gh_summary(traffic: dict[str, Any], releases: list[dict[str, Any]]) -> None:
    print("== GitHub traffic (rolling 14 days) ==")
    c = traffic["clones"]
    v = traffic["views"]
    print(f"  clones : {c['count']:>4}  unique: {c['uniques']:>4}")
    print(f"  views  : {v['count']:>4}  unique: {v['uniques']:>4}")

    print("\n  top referrers:")
    if not traffic["referrers"]:
        print("    (none)")
    for r in traffic["referrers"][:10]:
        print(f"    {r['referrer']:<32} views={r['count']:>3}  unique={r['uniques']:>3}")

    print("\n  top paths:")
    if not traffic["paths"]:
        print("    (none)")
    for p in traffic["paths"][:10]:
        title = (p.get("title") or "")[:60]
        print(f"    {p['path']:<40} views={p['count']:>3}  unique={p['uniques']:>3}  {title}")

    print("\n== GitHub release downloads (cumulative) ==")
    if not releases:
        print("  (no releases yet)")
    total = 0
    for rel in releases:
        rel_total = sum(a.get("download_count", 0) for a in rel.get("assets", []))
        total += rel_total
        print(f"  {rel['tag_name']:<12} {rel_total:>5}  {rel['name']}")
    print(f"  ---------- ------")
    print(f"  {'TOTAL':<12} {total:>5}")


# --------------------------------------------------------------------------- #
# Application Insights
# --------------------------------------------------------------------------- #

def _ai_query(app_id: str, kql: str) -> dict[str, Any]:
    """Query Application Insights via GET (POST silently returns {} for us)."""
    from urllib.parse import quote

    token = subprocess.check_output(
        [
            "az", "account", "get-access-token",
            "--resource", "https://api.applicationinsights.io",
            "--query", "accessToken", "-o", "tsv",
        ],
        text=True,
    ).strip()
    url = f"https://api.applicationinsights.io/v1/apps/{app_id}/query?query={quote(kql)}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _print_table(rows: dict[str, Any], header: str) -> None:
    print(f"\n== {header} ==")
    tables = rows.get("tables", [])
    if not tables or not tables[0]["rows"]:
        print("  (no data)")
        return
    t = tables[0]
    cols = [c["name"] for c in t["columns"]]
    widths = [max(len(str(r[i])) for r in t["rows"] + [cols]) for i in range(len(cols))]
    print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in t["rows"]:
        print("  " + "  ".join(str(x).ljust(widths[i]) for i, x in enumerate(r)))


def print_ai_summary(app_id: str) -> None:
    _print_table(
        _ai_query(app_id, (
            'customEvents | where name == "scan_complete" '
            '| summarize scans=count(), '
            'distinct_workspaces=dcount(tostring(customDimensions.workspace_id)), '
            'distinct_tenants=dcount(tostring(customDimensions.tenant_hash)) '
            'by tostring(customDimensions.version) | order by scans desc'
        )),
        "App Insights: scans / distinct workspaces / distinct tenants by version",
    )
    _print_table(
        _ai_query(app_id, (
            'customEvents | where name == "scan_complete" and timestamp > ago(30d) '
            '| summarize scans=count() by bin(timestamp, 1d) | order by timestamp asc'
        )),
        "App Insights: scan volume, last 30 days",
    )
    _print_table(
        _ai_query(app_id, (
            'customEvents | where name == "scan_complete" '
            '| summarize count() by state=tostring(customDimensions.state_backend)'
        )),
        "App Insights: state backend distribution (v0.3.4+)",
    )
    _print_table(
        _ai_query(app_id, (
            'customEvents | where name == "scan_complete" '
            '| summarize scans=count(), '
            'workspaces=dcount(tostring(customDimensions.workspace_id)), '
            'total_hours_saved=sum(toreal(customDimensions.estimated_manual_hours_saved)) '
            'by bin(timestamp, 30d) | order by timestamp desc'
        )),
        "App Insights: value narrative — hours saved per 30-day window (v0.3.4+)",
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--skip-gh", action="store_true")
    ap.add_argument("--skip-ai", action="store_true")
    ap.add_argument("--app-id", default=os.environ.get("AI_APP_ID", "9d5ab587-21ab-4b01-ad3a-e43e2fccd8dc"))
    args = ap.parse_args()

    if not args.skip_gh:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            print("[warn] GITHUB_TOKEN not set — skipping GitHub section.")
        else:
            try:
                traffic = gh_traffic(token)
                releases = gh_releases(token)
                print_gh_summary(traffic, releases)
            except urllib.error.HTTPError as e:
                print(f"[warn] GitHub API failed: {e.code} {e.reason}")

    if not args.skip_ai:
        try:
            print_ai_summary(args.app_id)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print(f"[warn] az CLI not available / not signed in — skipping App Insights: {e}")
        except urllib.error.HTTPError as e:
            print(f"[warn] App Insights query failed: {e.code} {e.reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
