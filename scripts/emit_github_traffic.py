#!/usr/bin/env python3
"""Emit a `github_traffic` customEvent to App Insights so the workbook can
chart distribution reach alongside runtime telemetry.

Fields emitted:
  * clones_14d, unique_cloners_14d
  * views_14d, unique_viewers_14d
  * top_referrer_1, top_referrer_1_count
  * total_release_downloads
  * per-release download counts as `release_downloads_v0_3_3`, etc.
  * stargazers, forks, open_issues, open_pulls

Configuration (all env vars):
  * GITHUB_TOKEN      — needs `repo` scope for Traffic API
  * REPO              — defaults to MichaelaIsaacs/pq-adbc-advisor
  * AI_CONNECTION_STRING — Application Insights connection string
                           (must contain InstrumentationKey= and IngestionEndpoint=)

Meant to be run once a day from GitHub Actions (see
.github/workflows/github-traffic-to-ai.yml). Fails soft on any 4xx from
either side so a missing token / rate limit doesn't stop the pipeline.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


REPO = os.environ.get("REPO", "MichaelaIsaacs/pq-adbc-advisor")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
AI_CONN = os.environ.get("AI_CONNECTION_STRING", "")


def _gh(path: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def collect_github_metrics() -> dict:
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN is required")
    metrics: dict = {}

    # Traffic — clones + views (14-day rolling)
    try:
        clones = _gh(f"/repos/{REPO}/traffic/clones")
        metrics["clones_14d"] = clones.get("count", 0)
        metrics["unique_cloners_14d"] = clones.get("uniques", 0)
    except Exception as e:
        metrics["clones_error"] = str(e)[:200]

    try:
        views = _gh(f"/repos/{REPO}/traffic/views")
        metrics["views_14d"] = views.get("count", 0)
        metrics["unique_viewers_14d"] = views.get("uniques", 0)
    except Exception as e:
        metrics["views_error"] = str(e)[:200]

    try:
        refs = _gh(f"/repos/{REPO}/traffic/popular/referrers")
        for i, r in enumerate(refs[:3], start=1):
            metrics[f"top_referrer_{i}"] = r.get("referrer", "")
            metrics[f"top_referrer_{i}_count"] = r.get("count", 0)
    except Exception as e:
        metrics["referrers_error"] = str(e)[:200]

    # Releases — cumulative per-tag download counts
    try:
        releases = _gh(f"/repos/{REPO}/releases")
        total = 0
        for rel in releases:
            tag = (rel.get("tag_name") or "").replace(".", "_").replace("-", "_")
            n = sum(a.get("download_count", 0) for a in rel.get("assets", []))
            if tag:
                metrics[f"release_downloads_{tag}"] = n
            total += n
        metrics["total_release_downloads"] = total
        metrics["release_count"] = len(releases)
    except Exception as e:
        metrics["releases_error"] = str(e)[:200]

    # Repo-level signals
    try:
        repo = _gh(f"/repos/{REPO}")
        metrics["stargazers"] = repo.get("stargazers_count", 0)
        metrics["forks"] = repo.get("forks_count", 0)
        metrics["open_issues"] = repo.get("open_issues_count", 0)
        metrics["subscribers"] = repo.get("subscribers_count", 0)
    except Exception as e:
        metrics["repo_error"] = str(e)[:200]

    return metrics


def _resolve_ai() -> tuple[str, str]:
    if not AI_CONN:
        raise RuntimeError("AI_CONNECTION_STRING is required")
    ikey = ""
    endpoint = "https://dc.services.visualstudio.com/v2/track"
    for chunk in AI_CONN.split(";"):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k == "instrumentationkey":
            ikey = v
        elif k == "ingestionendpoint":
            endpoint = v.rstrip("/") + "/v2/track"
    if not ikey:
        raise RuntimeError("AI_CONNECTION_STRING must contain InstrumentationKey=")
    return ikey, endpoint


def emit(name: str, props: dict) -> None:
    ikey, endpoint = _resolve_ai()
    envelope = {
        "name": "Microsoft.ApplicationInsights.Event",
        "time": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "iKey": ikey,
        "data": {
            "baseType": "EventData",
            "baseData": {
                "ver": 2,
                "name": name,
                "properties": {k: str(v) for k, v in props.items()},
            },
        },
    }
    body = json.dumps(envelope).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"[emit] {name} -> {r.status} {r.read()[:200].decode(errors='replace')}")


def main() -> int:
    props = collect_github_metrics()
    props["repo"] = REPO
    props["source"] = "github_traffic_daily"
    props["run_id"] = uuid.uuid4().hex
    print("[collected]")
    for k, v in sorted(props.items()):
        print(f"  {k}: {v}")
    emit("github_traffic", props)
    return 0


if __name__ == "__main__":
    sys.exit(main())
