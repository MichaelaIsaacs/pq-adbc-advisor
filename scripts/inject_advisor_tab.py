#!/usr/bin/env python3
"""Inject the 'Advisor Impact' tab into odbc-adbc-original.html.

The original ODBC → ADBC dashboard is served from SharePoint and lives
in aka.ms/adbcinsights. This script adds a third tab — 'Advisor Impact'
— that renders the pq-adbc-advisor telemetry (reach, resolution,
distribution, value, health) using the same snapshot the standalone
dashboard uses, but embedded inline so no cross-origin fetch is needed
inside the SharePoint iframe preview.

Reads:  dashboard/odbc-adbc-original.html + dashboard/data/snapshot.json
Writes: dashboard/odbc-adbc-dashboard.html   (the file to upload back
                                              to SharePoint)
"""

from __future__ import annotations

import json
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ORIGINAL = os.path.join(REPO, "dashboard", "odbc-adbc-original.html")
SNAPSHOT = os.path.join(REPO, "dashboard", "data", "snapshot.json")
OUT = os.path.join(REPO, "dashboard", "odbc-adbc-dashboard.html")


NAV_INJECT = '  <button class="tab-btn" data-tab="advisor">Advisor Impact</button>\n</nav>'
NAV_TARGET = '</nav>'


def render_advisor_section(snap: dict) -> str:
    """Return the <section> block for the Advisor Impact tab.

    Layout mirrors the existing tabs' style vocabulary (`.section-h`,
    `.desc`, `.kpi-block`, etc.) so it looks native to the dashboard
    rather than pasted in from somewhere else.
    """
    reach = snap.get("reach") or {}
    res = snap.get("resolution") or {}
    val = snap.get("value") or {}
    gh = snap.get("github") or {}
    trend = snap.get("trend") or []
    versions = snap.get("versions") or []
    per_ws = snap.get("per_workspace") or []
    skips = snap.get("skip_reasons") or []
    backends = snap.get("state_backends") or []
    first_vs_return = snap.get("first_vs_return") or []
    releases = snap.get("releases") or []
    generated_at = snap.get("generated_at", "")

    # -- inline the snapshot so no runtime fetch is needed --
    snapshot_json = json.dumps({
        "reach": reach, "resolution": res, "value": val,
        "github": gh, "trend": trend, "versions": versions,
        "per_workspace": per_ws, "skip_reasons": skips,
        "state_backends": backends, "first_vs_return": first_vs_return,
        "releases": releases, "generated_at": generated_at,
    })

    return f"""
<!-- ═══════════ TAB 3: ADVISOR IMPACT (pq-adbc-advisor telemetry) ═══════════ -->
<section class="tab" id="tab-advisor">

  <div class="section-h">
    <div>
      <h2>PQ ADBC Advisor — impact &amp; adoption</h2>
      <div class="desc">
        Live telemetry from workspaces running
        <a href="https://github.com/MichaelaIsaacs/pq-adbc-advisor" target="_blank"
           style="color:var(--brand)">pq-adbc-advisor</a> — the read-only Fabric
        notebook that scans a workspace, flags ODBC-pinned artifacts, and emits
        a per-workspace impact report. Every scan sends anonymous counts
        (no M code, no item names, no credentials). Refreshed daily.
      </div>
    </div>
    <div class="section-h-meta">
      Snapshot: <span class="mono">{generated_at or "—"}</span> ·
      <a href="https://portal.azure.com/#@microsoft.onmicrosoft.com/resource/subscriptions/40bf5434-ae5c-490d-a61b-de4c29313282/resourceGroups/rg-fabric-migration-scanner/providers/microsoft.insights/workbooks/ca79f051-259f-4dc5-a4e9-2970e8c0e660"
         target="_blank" style="color:var(--brand)">Azure Workbook (advanced)</a>
    </div>
  </div>

  <!-- Reach ------------------------------------------------------ -->
  <h3 class="adv-h">Reach</h3>
  <div class="desc" style="margin-bottom:12px">
    How many people are actually using this? Each unique user is counted
    once even across many scans, workspaces, or tenants (SHA-256 hashed).
  </div>
  <div class="adv-kpi-row">
    <div class="adv-kpi adv-kpi-good"><div class="adv-kpi-val">{int(reach.get("users", 0))}</div><div class="adv-kpi-lbl">Unique users</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(reach.get("scans", 0))}</div><div class="adv-kpi-lbl">Total scans</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(reach.get("workspaces", 0))}</div><div class="adv-kpi-lbl">Unique workspaces</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(reach.get("tenants", 0))}</div><div class="adv-kpi-lbl">Tenants (hashed)</div></div>
  </div>

  <div class="adv-2col">
    <div class="adv-card">
      <div class="adv-card-h">Scans per day <span class="adv-card-sub">(trailing 60 days)</span></div>
      <canvas id="advChartTrend"></canvas>
    </div>
    <div class="adv-card">
      <div class="adv-card-h">Version adoption <span class="adv-card-sub">(scans and workspaces per release)</span></div>
      <canvas id="advChartVersions"></canvas>
    </div>
  </div>

  <!-- Resolution ------------------------------------------------- -->
  <h3 class="adv-h">Migration progress</h3>
  <div class="desc" style="margin-bottom:12px">
    The tool showed customers a list of pinned ODBC drivers. These numbers show
    how many they've actually removed since we first saw them. Positive = cleanup.
    Negative = workspace grew.
  </div>
  <div class="adv-kpi-row">
    <div class="adv-kpi adv-kpi-good"><div class="adv-kpi-val">{int(res.get("resolved_pinned", 0)):+d}</div><div class="adv-kpi-lbl">ODBC pins cleaned up</div></div>
    <div class="adv-kpi adv-kpi-good"><div class="adv-kpi-val">{int(res.get("resolved_high", 0)):+d}</div><div class="adv-kpi-lbl">High-risk artifacts resolved</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(res.get("resolved_custom_dsn", 0)):+d}</div><div class="adv-kpi-lbl">Custom DSN M queries fixed</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(res.get("return_scans", 0))}</div><div class="adv-kpi-lbl">Return scans logged</div></div>
  </div>

  <div class="adv-card" style="margin-bottom:24px">
    <div class="adv-card-h">Per-workspace progress <span class="adv-card-sub">(latest scan per workspace, sorted by pins resolved)</span></div>
    <div id="advWsTable"></div>
  </div>

  <!-- Distribution ----------------------------------------------- -->
  <h3 class="adv-h">Distribution reach (GitHub)</h3>
  <div class="desc" style="margin-bottom:12px">
    How the repo is being pulled by other people. This is a public repo
    that installs via <code>git clone</code> / <code>pip install .</code> —
    every install counts as a clone. <b>Release "downloads" here means
    binary assets attached to a release tag</b>, which we don't publish,
    so that KPI is 0 by design.
  </div>
  <div class="adv-kpi-row">
    <div class="adv-kpi adv-kpi-good"><div class="adv-kpi-val">{int(gh.get("clones_14d", 0))}</div><div class="adv-kpi-lbl">Clones (14d) · {int(gh.get("unique_cloners_14d", 0))} unique people</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(gh.get("views_14d", 0))}</div><div class="adv-kpi-lbl">Repo page views (14d) · {int(gh.get("unique_viewers_14d", 0))} unique</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(gh.get("release_count", 0))}</div><div class="adv-kpi-lbl">Released tags · {int(gh.get("total_release_downloads", 0))} binary asset downloads</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(gh.get("open_issues", 0))}</div><div class="adv-kpi-lbl">Open issues · {int(gh.get("stargazers", 0))}★ {int(gh.get("forks", 0))} forks</div></div>
  </div>

  <div class="adv-2col">
    <div class="adv-card">
      <div class="adv-card-h">Clones per day <span class="adv-card-sub">(rolling 14-day window)</span></div>
      <canvas id="advChartClones"></canvas>
    </div>
    <div class="adv-card">
      <div class="adv-card-h">Top referrers <span class="adv-card-sub">(where people came from)</span></div>
      <div id="advReferrersTable"></div>
    </div>
  </div>

  <!-- Value ------------------------------------------------------ -->
  <h3 class="adv-h">Value narrative</h3>
  <div class="desc" style="margin-bottom:12px">
    Each artifact takes ~3 minutes to triage by hand. The tool does it in seconds.
    Only inspected items count — permission-denied or non-inspectable ones don't.
  </div>
  <div class="adv-kpi-row">
    <div class="adv-kpi adv-kpi-good"><div class="adv-kpi-val">{float(val.get("hours_saved", 0)):.1f}</div><div class="adv-kpi-lbl">Manual hours saved</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(val.get("inspected", 0))}</div><div class="adv-kpi-lbl">Artifacts inspected</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(val.get("median_coverage", 0))}%</div><div class="adv-kpi-lbl">Median coverage per scan</div></div>
    <div class="adv-kpi"><div class="adv-kpi-val">{int(float(val.get("sempy_used_pct", 0)))}%</div><div class="adv-kpi-lbl">Scans using sempy fast-path</div></div>
  </div>

  <!-- Health ----------------------------------------------------- -->
  <h3 class="adv-h">Reliability &amp; coverage</h3>
  <div class="desc" style="margin-bottom:12px">
    What blocks a clean scan? Are baselines persisting? Are users re-running the
    tool after fixing issues (the key iterative-use signal)?
  </div>
  <div class="adv-2col">
    <div class="adv-card">
      <div class="adv-card-h">Skip reasons <span class="adv-card-sub">(why we couldn't fully cover a workspace)</span></div>
      <canvas id="advChartSkip"></canvas>
    </div>
    <div class="adv-card">
      <div class="adv-card-h">First-run vs return-run <span class="adv-card-sub">(iterative use = value)</span></div>
      <canvas id="advChartFirstReturn"></canvas>
    </div>
  </div>

</section>
<!-- ═══════════ end Advisor Impact ═══════════ -->

<style>
  .section-h-meta {{ color:var(--muted); font-size:12px }}
  .adv-h {{ font-size:16px; font-weight:600; margin:28px 0 4px; color:var(--ink) }}
  .adv-kpi-row {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:12px; margin-bottom:20px }}
  .adv-kpi {{ background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:14px 16px; position:relative }}
  .adv-kpi::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background:var(--brand); border-radius:6px 0 0 6px }}
  .adv-kpi.adv-kpi-good::before {{ background:var(--ok) }}
  .adv-kpi-val {{ font-size:28px; font-weight:600; letter-spacing:-0.5px }}
  .adv-kpi-lbl {{ font-size:12px; color:var(--muted); margin-top:2px }}
  .adv-2col {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px }}
  @media(max-width:900px) {{ .adv-2col {{ grid-template-columns:1fr }} }}
  .adv-card {{ background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:16px }}
  .adv-card-h {{ font-size:13px; font-weight:600; margin-bottom:12px; color:var(--ink) }}
  .adv-card-sub {{ font-weight:400; color:var(--muted); font-size:11px; margin-left:4px }}
  .adv-card canvas {{ max-height:240px }}
  #tab-advisor table {{ width:100%; border-collapse:collapse; font-size:12px }}
  #tab-advisor th, #tab-advisor td {{ padding:8px 10px; text-align:left; border-bottom:1px solid var(--line) }}
  #tab-advisor th {{ font-size:10px; letter-spacing:1px; text-transform:uppercase; color:var(--muted); font-weight:600; background:#fafafa }}
  #tab-advisor td.num {{ text-align:right; font-variant-numeric:tabular-nums }}
  #tab-advisor td.pos {{ color:var(--ok); font-weight:600 }}
  #tab-advisor td.neg {{ color:var(--err); font-weight:600 }}
  #tab-advisor td.mono {{ font-family:'Cascadia Code', Menlo, Consolas, monospace; font-size:11px; color:var(--muted) }}
  .adv-pill {{ display:inline-block; padding:2px 6px; border-radius:8px; font-size:10px; font-weight:600 }}
  .adv-pill-first {{ background:#eff6fc; color:var(--brand) }}
  .adv-pill-return {{ background:#dff6dd; color:var(--ok) }}
</style>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script id="advisor-snapshot" type="application/json">{snapshot_json}</script>
<script>
(function () {{
  var __init = false;
  function initAdvisor() {{
    if (__init || !window.Chart) return;
    __init = true;
    var d = JSON.parse(document.getElementById('advisor-snapshot').textContent);
    var int = function (n) {{ return n === undefined || n === null ? 0 : parseInt(n, 10) || 0; }};

    // ---- Trend ----
    var trend = d.trend || [];
    new Chart(document.getElementById('advChartTrend'), {{
      type: 'line',
      data: {{
        labels: trend.map(function (x) {{ return (x.timestamp || '').slice(0, 10); }}),
        datasets: [
          {{label:'Scans', data:trend.map(function(x){{return int(x.scans);}}), borderColor:'#0078d4', backgroundColor:'rgba(0,120,212,0.12)', fill:true, tension:0.3, borderWidth:2}},
          {{label:'Workspaces', data:trend.map(function(x){{return int(x.workspaces);}}), borderColor:'#8378de', backgroundColor:'rgba(131,120,222,0.08)', fill:false, tension:0.3, borderWidth:2}}
        ]
      }},
      options: {{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{position:'bottom'}}}}, scales:{{y:{{beginAtZero:true, ticks:{{precision:0}}}}}}}}
    }});

    // ---- Versions ----
    var vers = (d.versions || []).slice().reverse();
    new Chart(document.getElementById('advChartVersions'), {{
      type: 'bar',
      data: {{
        labels: vers.map(function (v) {{ return v.version || ''; }}),
        datasets: [
          {{label:'Scans', data:vers.map(function(v){{return int(v.scans);}}), backgroundColor:'#0078d4'}},
          {{label:'Workspaces', data:vers.map(function(v){{return int(v.workspaces);}}), backgroundColor:'#8378de'}}
        ]
      }},
      options: {{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{position:'bottom'}}}}, scales:{{y:{{beginAtZero:true, ticks:{{precision:0}}}}}}}}
    }});

    // ---- Skip reasons ----
    var skips = d.skip_reasons || [];
    new Chart(document.getElementById('advChartSkip'), {{
      type: 'bar',
      data: {{
        labels: skips.map(function (x) {{ return x.reason; }}),
        datasets: [{{label:'Skips', data:skips.map(function(x){{return int(x.count);}}), backgroundColor:'#f7630c'}}]
      }},
      options: {{indexAxis:'y', responsive:true, maintainAspectRatio:false, plugins:{{legend:{{display:false}}}}, scales:{{x:{{beginAtZero:true, ticks:{{precision:0}}}}}}}}
    }});

    // ---- First-run vs return ----
    var fr = d.first_vs_return || [];
    new Chart(document.getElementById('advChartFirstReturn'), {{
      type: 'doughnut',
      data: {{
        labels: fr.map(function (x) {{ return x.is_first_run === 'True' ? 'First run' : 'Return run'; }}),
        datasets: [{{data: fr.map(function (x) {{ return int(x.scans); }}), backgroundColor:['#0078d4', '#107c10']}}]
      }},
      options: {{responsive:true, maintainAspectRatio:false, plugins:{{legend:{{position:'bottom'}}}}}}
    }});

    // ---- Per-workspace table ----
    var ws = d.per_workspace || [];
    var wsHtml;
    if (ws.length === 0) {{
      wsHtml = '<div style="padding:20px;text-align:center;color:var(--muted)">No workspace data yet</div>';
    }} else {{
      wsHtml = '<table><thead><tr>' +
        '<th>Workspace</th><th>State</th><th class="num">Runs</th>' +
        '<th class="num">Baseline pinned</th><th class="num">Current pinned</th>' +
        '<th class="num">Resolved</th><th class="num">Coverage</th><th>Ver</th>' +
        '</tr></thead><tbody>' +
        ws.map(function (w) {{
          var resolved = int(w.resolved_pinned);
          var state = w.is_first === 'True'
            ? '<span class="adv-pill adv-pill-first">first run</span>'
            : '<span class="adv-pill adv-pill-return">return</span>';
          var wsId = w.workspace || '';
          var trimWs = wsId.slice(0, 12) + (wsId.length > 12 ? '…' : '');
          return '<tr><td class="mono">' + trimWs + '</td><td>' + state + '</td>' +
            '<td class="num">' + int(w.run_count) + '</td>' +
            '<td class="num">' + int(w.first_pinned) + '</td>' +
            '<td class="num">' + int(w.current_pinned) + '</td>' +
            '<td class="num ' + (resolved > 0 ? 'pos' : resolved < 0 ? 'neg' : '') + '">' +
              (resolved > 0 ? '+' : '') + resolved + '</td>' +
            '<td class="num">' + int(w.coverage) + '%</td>' +
            '<td class="mono">' + (w.version || '—') + '</td></tr>';
        }}).join('') + '</tbody></table>';
    }}
    document.getElementById('advWsTable').innerHTML = wsHtml;

    // ---- Releases ----
    var rels = d.releases || [];
    var relsHtml;
    if (rels.length === 0) {{
      relsHtml = '<div style="padding:20px;text-align:center;color:var(--muted)">No release data</div>';
    }} else {{
      relsHtml = '<table><thead><tr><th>Tag</th><th class="num">Downloads</th><th>Published</th></tr></thead><tbody>' +
        rels.map(function (r) {{
          return '<tr><td class="mono">' + (r.tag || '—') + '</td>' +
            '<td class="num">' + int(r.downloads) + '</td>' +
            '<td class="mono">' + (r.published || '').slice(0, 10) + '</td></tr>';
        }}).join('') + '</tbody></table>';
    }}
    document.getElementById('advReleasesTable').innerHTML = relsHtml;

    // ---- Referrers ----
    var gh = d.github || {{}};
    var refs = [1, 2, 3].map(function (i) {{
      return {{name: gh['top_referrer_' + i], count: gh['top_referrer_' + i + '_count']}};
    }}).filter(function (x) {{ return x.name; }});
    var refsHtml;
    if (refs.length === 0) {{
      refsHtml = '<div style="padding:20px;text-align:center;color:var(--muted)">No referrer data yet</div>';
    }} else {{
      refsHtml = '<table><thead><tr><th>Source</th><th class="num">Views</th></tr></thead><tbody>' +
        refs.map(function (r) {{
          return '<tr><td class="mono">' + r.name + '</td><td class="num">' + int(r.count) + '</td></tr>';
        }}).join('') + '</tbody></table>';
    }}
    document.getElementById('advReferrersTable').innerHTML = refsHtml;
  }}

  // Chart.js loads async — retry until it's ready, then init once.
  var tries = 0;
  var tick = setInterval(function () {{
    tries += 1;
    if (window.Chart) {{
      clearInterval(tick);
      initAdvisor();
    }} else if (tries > 40) {{
      clearInterval(tick);
    }}
  }}, 100);

  // Also (re-)init when the advisor tab is clicked, in case charts didn't
  // render because their canvas was display:none at init time.
  document.querySelectorAll('.tab-btn[data-tab="advisor"]').forEach(function (b) {{
    b.addEventListener('click', function () {{
      setTimeout(function () {{ if (!__init) initAdvisor(); }}, 50);
    }});
  }});
}})();
</script>
"""


def build() -> None:
    if not os.path.exists(ORIGINAL):
        print(f"ERROR: {ORIGINAL} not found. Download from SharePoint first.")
        sys.exit(1)
    if not os.path.exists(SNAPSHOT):
        print(f"ERROR: {SNAPSHOT} not found. Run build_dashboard_snapshot.py first.")
        sys.exit(1)

    # v2 (Sept 2026): inline Chart.js because SharePoint's blob iframe CSP
    # blocks external <script src=…> loads (script-src 'unsafe-inline'
    # only). Cache the file next to the injector so we don't hit the CDN
    # on every rebuild.
    chartjs_path = os.path.join(HERE, "chartjs.umd.min.js")
    if not os.path.exists(chartjs_path):
        print(f"ERROR: {chartjs_path} missing. Copy chart.js UMD there once.")
        sys.exit(1)
    chartjs_source = open(chartjs_path).read()

    html = open(ORIGINAL).read()
    snap = json.load(open(SNAPSHOT))

    # 1) Add nav button
    if 'data-tab="advisor"' not in html:
        html = html.replace(NAV_TARGET, NAV_INJECT, 1)

    # 2) Add section AFTER the last existing section (before </main>)
    advisor_html = render_advisor_section(snap).replace(
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>',
        f'<script>{chartjs_source}</script>',
    )
    if 'id="tab-advisor"' in html:
        # Replace existing block on rebuilds. Find start/end sentinels.
        start_marker = "<!-- ═══════════ TAB 3: ADVISOR IMPACT"
        i = html.find(start_marker)
        j = html.find("</main>", i)
        if i != -1 and j != -1:
            html = html[:i] + advisor_html.lstrip() + "\n" + html[j:]
    else:
        html = html.replace("</main>", advisor_html + "\n</main>", 1)

    with open(OUT, "w") as f:
        f.write(html)
    print(f"wrote {OUT} ({len(html)} bytes)")


if __name__ == "__main__":
    build()
