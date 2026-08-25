#!/usr/bin/env python3
"""
Renders the JSON output of `analyze.py full` (health + analysis) into a
self-contained HTML dashboard report — the standard presentation layer for
/rtb-health, not a one-off hand-built page.

Everything mechanical (KPIs, ROAS windows, list/SK breakdown, structure
changes, goal table, ad-group drill-down with sparklines) is derived directly
from the JSON. The one thing this script can't produce is the qualitative
diagnosis — that still has to come from Claude's own investigation (multi-week
digging, cross-referencing structure changes, sanity-checking suspicious
results), passed in as --diagnosis.

Usage:
  python3 render_report.py --data full_output.json --diagnosis findings.json \
    --out report.html [--title "Campaign Name"]

--diagnosis is a JSON list of {"severity": "critical"|"warn"|"good"|"neutral",
"label": str, "text": str} — text may contain simple <b>...</b> for emphasis,
nothing else. If omitted, the diagnosis panel is skipped.
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path


def esc(s):
    return html.escape(str(s), quote=True)


def guess_campaign_name(groups, filename):
    for g in groups[:50]:
        name = g.get("adGroupName", "")
        m = re.search(r"\]?\s*\[?([^\[\]_]{3,40}?)_AIBID_", name)
        if m:
            candidate = m.group(1).strip(" :-")
            if candidate:
                return candidate
    stem = Path(filename).stem
    stem = re.sub(r"^\d{4}_\d{2}_\d{2}_\d{4}_\d{2}_\d{2}_", "", stem)
    stem = re.sub(r"_\d+WEEK$", "", stem, flags=re.I)
    return stem or filename


def wow_chip(pct, invert=False, none_label="—"):
    if pct is None:
        return f'<span class="chip neutral">{none_label}</span>'
    good = (pct >= 0) if not invert else (pct <= 0)
    cls = "good" if good else "critical"
    if abs(pct) < 1:
        cls = "neutral"
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "→")
    return f'<span class="chip {cls}">{arrow} {abs(pct):.1f}%</span>'


def abs_chip(delta, unit="$", invert=False, none_label="—"):
    if delta is None:
        return f'<span class="chip neutral">{none_label}</span>'
    good = (delta <= 0) if not invert else (delta >= 0)
    cls = "good" if good else "critical"
    if abs(delta) < 0.005:
        cls = "neutral"
    arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "→")
    return f'<span class="chip {cls}">{arrow} {unit}{abs(delta):.2f}</span>'


def bar_row(label, all_time_pct, latest_pct, wow_pct, max_scale=None):
    scale = max_scale or max(all_time_pct or 0, latest_pct or 0, 1)
    at_w = min(100, (all_time_pct or 0) / scale * 100)
    lt_w = min(100, (latest_pct or 0) / scale * 100)
    wow_html = wow_chip(wow_pct) if wow_pct is not None else '<span class="chip neutral">—</span>'
    return f"""
      <div class="roas-row">
        <div class="rw-label">{esc(label)}</div>
        <div class="roas-bars"><div class="bar-all" style="width:{at_w:.1f}%"></div><div class="bar-latest" style="width:{lt_w:.1f}%"></div></div>
        <div class="rw-wow">{wow_html}</div>
      </div>"""


def build_html(data, diagnosis, title_override, data_filename):
    health = data["health"]
    analysis = data.get("analysis")
    all_time = health["allTime"]
    latest = health["latestWeek"]
    meta = health["meta"]
    weeks = meta["weeks"]
    latest_week_label = meta["latestWeek"]
    prior_week_label = latest.get("priorWeek")

    groups = analysis["groups"] if analysis else []
    title = title_override or guess_campaign_name(groups, data_filename)

    # ---- KPI row ----
    va_kpi = f"""
    <div class="kpi">
      <div class="kpi-label">Valid Action · Latest Wk</div>
      <div class="kpi-value">{latest['totalValidAction']:g}</div>
      <div class="kpi-sub">{wow_chip(latest.get('wowValidActionPct'))} vs. prior week</div>
    </div>"""
    cpa_kpi = f"""
    <div class="kpi">
      <div class="kpi-label">Aggregate CPA · Latest Wk</div>
      <div class="kpi-value">{esc(latest.get('avgCPAText','—'))}</div>
      <div class="kpi-sub">{abs_chip(latest.get('wowCPAAbs'))} vs. prior week</div>
    </div>"""
    invalid_kpi = f"""
    <div class="kpi">
      <div class="kpi-label">Invalid Action · Latest Wk</div>
      <div class="kpi-value">{latest['totalInvalidAction']:g}</div>
      <div class="kpi-sub">{wow_chip(latest.get('wowInvalidActionPct'), invert=True)} vs. prior week</div>
    </div>"""
    roas_windows = data["health"].get("roasWindows") or []
    real_roas = [r for r in roas_windows if r and r.get("latestAvg") is not None]
    if real_roas:
        headline_roas = real_roas[0]
        fourth_kpi = f"""
    <div class="kpi">
      <div class="kpi-label">{esc(headline_roas['metric'])} · Latest Wk</div>
      <div class="kpi-value">{esc(headline_roas['latestAvgText'])}</div>
      <div class="kpi-sub">{wow_chip(headline_roas.get('wowPct'))} vs. prior week</div>
    </div>"""
    else:
        mmp = latest.get("mmpCVR")
        if mmp and mmp.get("value") is not None:
            fourth_kpi = f"""
    <div class="kpi">
      <div class="kpi-label">MMP CVR · Latest Wk</div>
      <div class="kpi-value">{esc(mmp['valueText'])}</div>
      <div class="kpi-sub">{wow_chip(mmp.get('wowPct'))} vs. prior week</div>
    </div>"""
        else:
            at = all_time.get("avgCPAText", "—")
            fourth_kpi = f"""
    <div class="kpi">
      <div class="kpi-label">Aggregate CPA · All Time</div>
      <div class="kpi-value">{esc(at)}</div>
      <div class="kpi-sub"><span class="chip neutral">{all_time['totalValidAction']:g} VA over {len(weeks)}w</span></div>
    </div>"""

    # ---- ROAS windows panel ----
    if real_roas:
        max_scale = max((r["allTimeAvg"] or 0) for r in real_roas + [{"allTimeAvg": 0}]) or 1
        rows_html = "".join(
            bar_row(r["metric"], (r["allTimeAvg"] or 0) * 100, (r["latestAvg"] or 0) * 100, r.get("wowPct"), max_scale=max_scale * 100)
            for r in real_roas
        )
        roas_panel = f"""
  <div class="panel">
    <p class="panel-eyebrow">Return on Ad Spend</p>
    <h2>ROAS across every tracked window</h2>
    <p class="panel-sub">All windows shown side by side — a single-window read can miss a broader trend.</p>
    <div class="roas-windows">{rows_html}
    </div>
    <div class="roas-legend">
      <span><i class="swatch" style="background:var(--text-faint);opacity:.35"></i>{len(weeks)}-week average</span>
      <span><i class="swatch" style="background:var(--accent)"></i>Latest week</span>
    </div>
  </div>"""
    else:
        roas_panel = """
  <div class="panel">
    <p class="panel-eyebrow">Return on Ad Spend</p>
    <h2>No ROAS data in this file</h2>
    <p class="panel-sub">ROAS-typed columns exist in the export but have no populated values this period — revenue tracking gap, not a parsing issue.</p>
  </div>"""

    # ---- list/SK breakdown ----
    list_bd = (latest.get("listBreakdown") or all_time.get("listBreakdown") or [])[:8]
    list_source = "Latest week" if latest.get("listBreakdown") else "All time"
    if list_bd:
        max_cost = max((row["rawCost"] for row in list_bd), default=1) or 1
        list_rows = "".join(f"""
        <div class="bar-list-row"><div class="bl-label" title="{esc(row['listTags'])}">{esc(row['listTags'])}</div><div class="bl-track"><div class="bl-fill" style="width:{row['rawCost']/max_cost*100:.1f}%"></div></div><div class="bl-value">${row['rawCost']:,.0f}</div></div>"""
            for row in list_bd)
        list_panel = f"""
    <div class="panel">
      <p class="panel-eyebrow">Targeting Mix · {esc(list_source)}</p>
      <h2>Spend by list / SK tag</h2>
      <p class="panel-sub">Extracted from ad group naming — heuristic, not a guaranteed-complete taxonomy; naming varies by optimizer.</p>
      <div class="bar-list">{list_rows}
      </div>
    </div>"""
    else:
        list_panel = """
    <div class="panel">
      <p class="panel-eyebrow">Targeting Mix</p>
      <h2>No list/SK tags detected</h2>
      <p class="panel-sub">Ad group naming in this file didn't match the known SK/PM/IDFA/IDFV/WL patterns.</p>
    </div>"""

    # ---- structure changes ----
    sc = latest.get("structureChanges")
    if sc:
        struct_panel = f"""
    <div class="panel">
      <p class="panel-eyebrow">Week-over-Week</p>
      <h2>Structure {"shifted" if (sc['addedCount']+sc['removedCount']) > 0 else "held steady"}</h2>
      <p class="panel-sub">Ad group × SSP combinations that appeared or disappeared vs. the prior week.</p>
      <div class="stat-pair">
        <div class="stat-box added"><div class="n">+{sc['addedCount']}</div><div class="l">newly added</div></div>
        <div class="stat-box removed"><div class="n">−{sc['removedCount']}</div><div class="l">paused / removed</div></div>
      </div>
    </div>"""
    else:
        struct_panel = """
    <div class="panel">
      <p class="panel-eyebrow">Week-over-Week</p>
      <h2>No prior week to compare</h2>
      <p class="panel-sub">Only one week of data in this file.</p>
    </div>"""

    # ---- goal analysis ----
    goal_rules = analysis["meta"]["goalRules"] if analysis else []
    goal_section = ""
    drilldown_js = "[]"
    if analysis and goal_rules:
        goal_desc = " → ".join(f"{r['metric']} {'<' if r['dir']=='below' else '>'} {r['value']}" for r in goal_rules)
        metric_names = [r["metric"] for r in goal_rules]

        def closeness(s):
            # Passers already win on rulesFired alone. Among non-passers, rank
            # by how close the first UNMET rule's value is to its threshold —
            # not alphabetically — so a near-miss (32% vs. a 40% bar) outranks
            # an SSP with zero data at all, instead of losing to it on ties.
            idx = s["rulesFired"]
            if idx >= len(goal_rules):
                return (0.0,)
            rule = goal_rules[idx]
            v = s["ruleMetricValues"].get(rule["metric"], {}).get("value")
            if v is None:
                return (float("inf"),)
            dist = (v - rule["value"]) if rule["dir"] == "below" else (rule["value"] - v)
            return (dist,)

        ssp_eval = sorted(analysis["sspGoalEval"], key=lambda s: (-s["rulesFired"], closeness(s)))[:12]
        header_cols = "".join(f"<th>{esc(m)}</th>" for m in metric_names)
        ssp_rows = ""
        for s in ssp_eval:
            if s["rulesFired"] == len(goal_rules):
                pill = '<span class="pill pass">Meets goal</span>'
            elif s["rulesFired"] > 0:
                pill = '<span class="pill near">Near</span>'
            else:
                pill = '<span class="pill miss">Below</span>'
            val_cols = "".join(f"<td>{esc(s['ruleMetricValues'].get(m, {}).get('text', '—'))}</td>" for m in metric_names)
            row_cls = ' class="highlight"' if s["rulesFired"] == len(goal_rules) else ""
            ssp_rows += f"<tr{row_cls}><td class=\"name\">{esc(s['ssp'])}</td>{val_cols}<td>{s['adGroupCount']}</td><td>{pill}</td></tr>"

        relevant_ssps = {s["ssp"] for s in ssp_eval if s["rulesFired"] > 0}
        drill_groups = [g for g in groups if g["ssp"] in relevant_ssps]
        drill_groups.sort(key=lambda g: -g["rulesFired"])
        drill_groups = drill_groups[:20]
        primary_metric = metric_names[0]
        weeks_oldest_first = list(reversed(analysis["meta"]["weeksInView"]))
        drill_rows = ""
        js_series = []
        for idx, g in enumerate(drill_groups):
            name = g.get("canonicalName") if g.get("nameChanged") else g["adGroupName"]
            latest_w = analysis["meta"]["weeksInView"][0]
            cell = g["weeks"].get(latest_w, {}).get(primary_metric, {})
            va_cell = g["weeks"].get(latest_w, {}).get(analysis["meta"].get("vaColName", ""), {})
            series = []
            for w in weeks_oldest_first:
                c = g["weeks"].get(w, {}).get(primary_metric, {})
                raw = c.get("raw")
                try:
                    series.append(float(raw) if raw not in (None, "") else None)
                except (TypeError, ValueError):
                    series.append(None)
            js_series.append(series)
            row_cls = ' class="highlight"' if g["rulesFired"] == len(goal_rules) else ""
            drill_rows += f"""<tr{row_cls}>
        <td class="name">{esc(name)}</td>
        <td>{esc(g['ssp'])}</td>
        <td>{esc(cell.get('text','—'))}</td>
        <td style="min-width:120px;"><canvas class="spark" data-idx="{idx}"></canvas></td>
      </tr>"""
        drilldown_js = json.dumps([{"series": s, "highlight": drill_groups[i]["rulesFired"] == len(goal_rules)} for i, s in enumerate(js_series)])

        goal_section = f"""
  <div class="panel">
    <p class="panel-eyebrow">Goal Analysis</p>
    <h2>Target: {esc(goal_desc)}</h2>
    <p class="panel-sub">Step 1 — which SSPs clear the bar in aggregate (volume-weighted for cost-per-action metrics), before drilling into individual ad groups.</p>
    <div class="table-scroll">
      <table>
        <thead><tr><th>SSP</th>{header_cols}<th>Ad groups</th><th>Status</th></tr></thead>
        <tbody>{ssp_rows}</tbody>
      </table>
    </div>
    <p class="panel-sub" style="margin-top:22px;">Step 2 — ad groups behind the SSPs above that fired at least one rule, with {esc(primary_metric)} trend.</p>
    <div class="table-scroll">
      <table>
        <thead><tr><th>Ad Group</th><th>SSP</th><th>{esc(primary_metric)}</th><th>Trend</th></tr></thead>
        <tbody id="drilldown-body">{drill_rows}</tbody>
      </table>
    </div>
  </div>"""

    # ---- diagnosis ----
    diag_html = ""
    if diagnosis:
        cards = ""
        for f in diagnosis:
            sev = f.get("severity", "neutral")
            cards += f"""
      <div class="diag-card {esc(sev)}">
        <div class="stripe"></div>
        <div class="body">
          <div class="head"><span class="label">{esc(f.get('label',''))}</span></div>
          <p>{f.get('text','')}</p>
        </div>
      </div>"""
        diag_html = f"""
  <div class="panel">
    <p class="panel-eyebrow">Diagnosis</p>
    <h2>What the numbers mean</h2>
    <div class="diag-list">{cards}
    </div>
  </div>"""

    top_ssps_cost = ", ".join(f"{s['ssp']} {s['ratePct']:.1f}%" for s in all_time["sspByCostTop3"])
    top_ssps_action = ", ".join(f"{s['ssp']} {s['ratePct']:.1f}%" for s in all_time["sspByActionTop3"])

    return f"""<title>Campaign Health — {esc(title)}</title>
<style>
{CSS}
</style>

<div class="page">
  <div class="masthead">
    <div>
      <p class="masthead-eyebrow">Campaign Health Report</p>
      <h1>{esc(title)}</h1>
      <p class="masthead-meta">{len(weeks)}-week window <b>{esc(weeks[0])}</b> – <b>{esc(weeks[-1])}</b> · Latest week <b>{esc(latest_week_label)}</b></p>
    </div>
    <div class="masthead-right">
      Generated by <span class="tag">/rtb-health</span><br>
      Source: {esc(Path(data_filename).name)}
    </div>
  </div>

  <div class="kpi-row">{va_kpi}{cpa_kpi}{fourth_kpi}{invalid_kpi}</div>

  <p class="panel-sub" style="margin:0;">All time — top SSP by cost: {esc(top_ssps_cost)} · by action: {esc(top_ssps_action)}</p>

  {roas_panel}

  <div class="panel-grid-2">
    {list_panel}
    {struct_panel}
  </div>
  {goal_section}
  {diag_html}

  <div class="footer">
    <span>Methodology: aggregate metrics are volume-weighted (Σcost/ΣVA, ΣWin/ΣBid) where source columns allow, not row-averages.</span>
    <span>rtb-health · generated for internal review</span>
  </div>
</div>

<script>
  const drilldownSeries = {drilldown_js};
  function drawSpark(canvas, series, color) {{
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const vals = series.filter(v => v !== null);
    if (!vals.length) return;
    const max = Math.max(...vals, 1), min = Math.min(...vals, 0);
    const pad = 4;
    const pts = series.map((v, i) => {{
      const x = pad + (i / (series.length - 1)) * (w - pad * 2);
      if (v === null) return null;
      const y = h - pad - ((v - min) / (max - min || 1)) * (h - pad * 2);
      return [x, y];
    }});
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = color;
    ctx.beginPath();
    let started = false;
    pts.forEach(p => {{
      if (!p) {{ started = false; return; }}
      if (!started) {{ ctx.moveTo(p[0], p[1]); started = true; }}
      else ctx.lineTo(p[0], p[1]);
    }});
    ctx.stroke();
    const last = pts.filter(Boolean).pop();
    if (last) {{
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(last[0], last[1], 2.4, 0, Math.PI * 2);
      ctx.fill();
    }}
  }}
  const styles = getComputedStyle(document.documentElement);
  document.querySelectorAll("canvas.spark").forEach((c, i) => {{
    const d = drilldownSeries[i];
    if (!d) return;
    const color = d.highlight ? styles.getPropertyValue("--good").trim() : styles.getPropertyValue("--accent").trim();
    drawSpark(c, d.series, color);
  }});
</script>
"""


CSS = r"""
:root {
  --bg: #f5f6f8; --surface: #ffffff; --surface-2: #eef0f3; --border: #dde1e6;
  --text: #171b1e; --text-dim: #5b666b; --text-faint: #93a0a5;
  --accent: #5b6bd6; --accent-soft: rgba(91, 107, 214, 0.1);
  --good: #1f9d73; --good-soft: rgba(31, 157, 115, 0.12);
  --warn: #b97a1f; --warn-soft: rgba(185, 122, 31, 0.12);
  --critical: #d1453d; --critical-soft: rgba(209, 69, 61, 0.1);
  --shadow: 0 1px 2px rgba(20, 24, 28, 0.04), 0 8px 24px rgba(20, 24, 28, 0.05);
  --font-display: ui-serif, "New York", "Iowan Old Style", Georgia, "Times New Roman", serif;
  --font-body: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b0f13; --surface: #131a20; --surface-2: #1b242c; --border: #263139;
    --text: #eef2f1; --text-dim: #94a4a9; --text-faint: #566267;
    --accent: #8b98f9; --accent-soft: rgba(139, 152, 249, 0.14);
    --good: #3fd0a0; --good-soft: rgba(63, 208, 160, 0.14);
    --warn: #e8b04e; --warn-soft: rgba(232, 176, 78, 0.14);
    --critical: #ef6f68; --critical-soft: rgba(239, 111, 104, 0.14);
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 12px 32px rgba(0,0,0,.35);
  }
}
:root[data-theme="dark"] {
  --bg: #0b0f13; --surface: #131a20; --surface-2: #1b242c; --border: #263139;
  --text: #eef2f1; --text-dim: #94a4a9; --text-faint: #566267;
  --accent: #8b98f9; --accent-soft: rgba(139, 152, 249, 0.14);
  --good: #3fd0a0; --good-soft: rgba(63, 208, 160, 0.14);
  --warn: #e8b04e; --warn-soft: rgba(232, 176, 78, 0.14);
  --critical: #ef6f68; --critical-soft: rgba(239, 111, 104, 0.14);
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 12px 32px rgba(0,0,0,.35);
}
:root[data-theme="light"] {
  --bg: #f5f6f8; --surface: #ffffff; --surface-2: #eef0f3; --border: #dde1e6;
  --text: #171b1e; --text-dim: #5b666b; --text-faint: #93a0a5;
  --accent: #5b6bd6; --accent-soft: rgba(91, 107, 214, 0.1);
  --good: #1f9d73; --good-soft: rgba(31, 157, 115, 0.12);
  --warn: #b97a1f; --warn-soft: rgba(185, 122, 31, 0.12);
  --critical: #d1453d; --critical-soft: rgba(209, 69, 61, 0.1);
  --shadow: 0 1px 2px rgba(20,24,28,.04), 0 8px 24px rgba(20,24,28,.05);
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:var(--font-body); font-size:14px; line-height:1.55; -webkit-font-smoothing:antialiased; }
::selection { background: var(--accent-soft); }
.page { max-width:980px; margin:0 auto; padding:40px 28px 80px; display:flex; flex-direction:column; gap:28px; }
.masthead { display:flex; justify-content:space-between; align-items:flex-end; gap:24px; flex-wrap:wrap; padding-bottom:20px; border-bottom:1px solid var(--border); }
.masthead-eyebrow { font-family:var(--font-mono); font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--accent); margin:0 0 10px; }
.masthead h1 { font-family:var(--font-display); font-weight:500; font-size:30px; letter-spacing:-.01em; margin:0 0 8px; text-wrap:balance; }
.masthead-meta { color:var(--text-dim); font-size:13px; }
.masthead-meta b { color:var(--text); font-weight:600; }
.masthead-right { text-align:right; font-family:var(--font-mono); font-size:11.5px; color:var(--text-faint); line-height:1.7; }
.masthead-right .tag { display:inline-block; margin-top:6px; padding:3px 9px; border-radius:20px; background:var(--accent-soft); color:var(--accent); font-size:10.5px; letter-spacing:.04em; }
.kpi-row { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
.kpi { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px 18px; box-shadow:var(--shadow); }
.kpi-label { font-family:var(--font-mono); font-size:10.5px; letter-spacing:.08em; text-transform:uppercase; color:var(--text-faint); margin-bottom:8px; }
.kpi-value { font-family:var(--font-mono); font-size:24px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-.01em; }
.kpi-sub { margin-top:8px; font-size:12px; color:var(--text-dim); }
.chip { display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:20px; font-family:var(--font-mono); font-size:11px; font-weight:600; font-variant-numeric:tabular-nums; }
.chip.good { background:var(--good-soft); color:var(--good); }
.chip.warn { background:var(--warn-soft); color:var(--warn); }
.chip.critical { background:var(--critical-soft); color:var(--critical); }
.chip.neutral { background:var(--surface-2); color:var(--text-dim); }
.panel { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:24px 26px; box-shadow:var(--shadow); }
.panel-eyebrow { font-family:var(--font-mono); font-size:10.5px; letter-spacing:.12em; text-transform:uppercase; color:var(--accent); margin:0 0 6px; }
.panel h2 { font-family:var(--font-display); font-weight:500; font-size:21px; margin:0 0 4px; text-wrap:balance; }
.panel-sub { color:var(--text-dim); font-size:12.5px; margin:0 0 18px; }
.panel-grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:24px; }
@media (max-width:720px) { .kpi-row{grid-template-columns:repeat(2,1fr);} .panel-grid-2{grid-template-columns:1fr;} }
.table-scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:12.5px; }
th { text-align:right; font-family:var(--font-mono); font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; color:var(--text-faint); font-weight:600; padding:0 10px 8px; border-bottom:1px solid var(--border); white-space:nowrap; }
th:first-child, td:first-child { text-align:left; }
td { padding:9px 10px; border-bottom:1px solid var(--border); text-align:right; font-family:var(--font-mono); font-variant-numeric:tabular-nums; white-space:nowrap; }
td.name { font-family:var(--font-body); text-align:left; white-space:normal; max-width:320px; }
tr:last-child td { border-bottom:none; }
tr.highlight td { background:var(--good-soft); }
.pill { display:inline-block; padding:2px 9px; border-radius:20px; font-size:11px; font-weight:600; font-family:var(--font-mono); }
.pill.pass { background:var(--good-soft); color:var(--good); }
.pill.near { background:var(--warn-soft); color:var(--warn); }
.pill.miss { background:var(--surface-2); color:var(--text-faint); }
.roas-windows { display:flex; flex-direction:column; gap:14px; }
.roas-row { display:grid; grid-template-columns:150px 1fr 90px; align-items:center; gap:12px; }
.roas-row .rw-label { font-size:12.5px; color:var(--text-dim); }
.roas-bars { position:relative; height:20px; background:var(--surface-2); border-radius:4px; overflow:hidden; }
.roas-bars .bar-all { position:absolute; top:0; left:0; bottom:0; background:var(--text-faint); opacity:.35; border-radius:4px; }
.roas-bars .bar-latest { position:absolute; top:3px; bottom:3px; left:0; background:var(--accent); border-radius:3px; }
.roas-row .rw-wow { text-align:right; }
.roas-legend { display:flex; gap:18px; margin-top:4px; font-size:11px; color:var(--text-faint); }
.roas-legend span { display:inline-flex; align-items:center; gap:5px; }
.swatch { width:9px; height:9px; border-radius:2px; display:inline-block; }
.bar-list { display:flex; flex-direction:column; gap:10px; }
.bar-list-row { display:grid; grid-template-columns:120px 1fr 70px; align-items:center; gap:10px; }
.bar-list-row .bl-label { font-family:var(--font-mono); font-size:11.5px; color:var(--text-dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.bl-track { height:14px; background:var(--surface-2); border-radius:3px; overflow:hidden; }
.bl-fill { height:100%; background:var(--accent); border-radius:3px; }
.bl-value { font-family:var(--font-mono); font-size:11.5px; text-align:right; color:var(--text-dim); font-variant-numeric:tabular-nums; }
canvas.spark { display:block; width:100%; height:34px; }
.stat-pair { display:flex; gap:14px; }
.stat-box { flex:1; border:1px solid var(--border); border-radius:8px; padding:14px 16px; }
.stat-box .n { font-family:var(--font-mono); font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }
.stat-box .l { font-size:11.5px; color:var(--text-dim); margin-top:2px; }
.stat-box.added .n { color:var(--good); }
.stat-box.removed .n { color:var(--critical); }
.diag-list { display:flex; flex-direction:column; gap:12px; }
.diag-card { display:grid; grid-template-columns:4px 1fr; gap:14px; border:1px solid var(--border); border-radius:8px; overflow:hidden; }
.diag-card .stripe { background:var(--text-faint); }
.diag-card.critical .stripe { background:var(--critical); }
.diag-card.warn .stripe { background:var(--warn); }
.diag-card.good .stripe { background:var(--good); }
.diag-card .body { padding:14px 18px 14px 0; }
.diag-card .head { display:flex; align-items:center; gap:8px; margin-bottom:5px; }
.diag-card .head .label { font-family:var(--font-mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; font-weight:700; }
.diag-card.critical .head .label { color:var(--critical); }
.diag-card.warn .head .label { color:var(--warn); }
.diag-card.good .head .label { color:var(--good); }
.diag-card.neutral .head .label { color:var(--text-dim); }
.diag-card p { margin:0; font-size:13px; color:var(--text); }
.diag-card p b { font-family:var(--font-mono); font-variant-numeric:tabular-nums; }
.footer { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; padding-top:18px; border-top:1px solid var(--border); font-size:11.5px; color:var(--text-faint); font-family:var(--font-mono); }
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="Path to `analyze.py full` JSON output")
    ap.add_argument("--diagnosis", default=None, help="Path to a JSON list of {severity,label,text} findings")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default=None)
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))
    diagnosis = json.load(open(args.diagnosis, encoding="utf-8")) if args.diagnosis else None
    data_filename = data.get("health", {}).get("meta", {}).get("importFile", args.data)

    html_out = build_html(data, diagnosis, args.title, data_filename)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(json.dumps({"written": args.out}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
