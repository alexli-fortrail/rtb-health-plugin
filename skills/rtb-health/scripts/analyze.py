#!/usr/bin/env python3
"""
Parsing and analysis for weekly RTB campaign exports: column/metric-type
detection, z-score anomaly detection, chained goal-rule evaluation, WoW deltas,
and an All-Time / Latest-Week campaign health scan.

Modes:
  discover <file> [--cid FILE]
      Cheap first pass: column mapping, metric list + detected types, weeks,
      ad-group/SSP counts. Use it to ask an informed "what's your goal" question.

  estimate <file> [--cid FILE]
      Cheap pre-flight sizing: how big the detailed output will be, what the
      lite run would cost instead, and a light/medium/heavy band. Use it to warn
      the user before an expensive run.

  full <file> [--rules JSON] [--filters JSON] [--lite] [--max-weeks N]
       [--top-groups N] [--cid FILE]
      health + analyze in one pass, parsing the file only once. This is the
      normal entry point. --lite trims the per-group x week x metric matrix
      (the bulk of the output) without changing any surviving number.

  health <file> [--cid FILE]
      Health scan only: All Time + Latest Week, ROAS windows, CTCV/VTCV, CPI,
      per-SSP win rate/CPM, list-tag breakdown, WoW structure changes.

  analyze <file> --rules JSON [--filters JSON] [--cid FILE]
      Goal analysis only: z-score anomalies, chained goal-rule flags, WoW %,
      per-metric summary, SSP breakdown + SSP-level goal evaluation.

  export <file> --out CSV [--rules JSON] [--filters JSON] [--cid FILE]
      Writes the current (rules+filters scoped) view to a CSV file.

  allocation <file> --target JSON [--scope latest|all_time] [--cid FILE]
      Actual per-SSP cost share vs. a user-supplied target allocation.

Aggregation notes: CPA is volume-weighted (total cost / total valid action),
as are per-SSP win rate (total win / total bid) and CPM (total cost / total
impressions x 1000). Ratio metrics without separate numerator/denominator
columns in the export (ROAS, CTCV/VTCV rate) are averaged per row instead —
see the SKILL.md notes for why, and read those numbers with volume in mind.
"""
import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CID_MAP_PATH = SCRIPT_DIR.parent / "assets" / "cid_map_default.json"


# ---------------------------------------------------------------------------
# Metric type detection — drives formatting and avg-vs-total aggregation
# ---------------------------------------------------------------------------
def detect_metric_type(name):
    n = name.lower()
    if re.search(r"roas", n):
        return "roas"
    if re.search(r"rate|ctr|cvr|atr|mmp.ctr|purchase.uu|win.rate|show.rate|invalid.click.rate", n):
        return "pct"
    # ^cpi$ is included deliberately: in mobile-game verticals CPI is often the
    # primary bid lever alongside (or instead of) CPA, so it should format as a
    # dollar value everywhere rather than falling through to bare 'float'.
    if re.search(r"^cpa$|^cpm$|^cpc$|^cpi$|cost.per.install|cost|amount|aov|average.bid.price|average.win.price", n):
        return "dollar"
    if re.search(r"impression|click|action|^win$|^bid$|^bid\s", n):
        return "int"
    return "float"


# ---------------------------------------------------------------------------
# Value formatting — per-type display rules ($ / % / K,M / dashes for zero)
# ---------------------------------------------------------------------------
def format_val(v, metric_type):
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(n):
        return str(v)
    if n == 0:
        return "—"

    t = metric_type or "float"
    if t == "pct" or t == "roas":
        return f"{n * 100:.2f}%"
    if t == "dollar":
        if abs(n) >= 1000:
            return f"${n / 1000:.1f}K"
        elif abs(n) >= 1:
            return f"${n:.2f}"
        else:
            return f"${n:.4f}"
    if t == "int":
        if abs(n) >= 1e6:
            return f"{n / 1e6:.2f}M"
        elif abs(n) >= 1e3:
            return f"{n / 1e3:.1f}K"
        else:
            return f"{round(n):,}"
    # float
    if abs(n) >= 1e6:
        return f"{n / 1e6:.2f}M"
    elif abs(n) >= 1e3:
        return f"{n / 1e3:.1f}K"
    elif abs(n) < 0.001:
        mantissa, exp = f"{n:.2e}".split("e")
        exp_i = int(exp)
        return f"{mantissa}e{'+' if exp_i >= 0 else '-'}{abs(exp_i)}"
    else:
        text = f"{n:.4f}".rstrip("0").rstrip(".")
        return text if text else "0"


def to_float_or_none(v):
    try:
        n = float(v)
        if math.isnan(n):
            return None
        return n
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------
def load_raw_rows(file_path):
    path = Path(file_path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    elif path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")
    df = df.fillna("")
    all_cols = [str(c) for c in df.columns]
    df.columns = all_cols
    raw_rows = df.to_dict("records")
    return raw_rows, all_cols


def load_cid_map(cid_map_path, cid_file_path=None):
    cid_map = {}
    if cid_map_path and Path(cid_map_path).exists():
        with open(cid_map_path, encoding="utf-8") as f:
            cid_map = json.load(f)
    if cid_file_path:
        cid_raw, cid_cols = load_raw_rows(cid_file_path)
        id_col = next((c for c in cid_cols if re.search(r"ad.?group.?id", c, re.I)), None)
        name_col = next((c for c in cid_cols if re.search(r"ad.?group.?name", c, re.I)), None)
        if id_col and name_col:
            for r in cid_raw:
                gid = str(r.get(id_col, "")).strip()
                name = str(r.get(name_col, "")).strip()
                if not gid or not name:
                    continue
                existing = cid_map.setdefault(gid, [])
                if name not in existing:
                    existing.insert(0, name)
    return cid_map


# ---------------------------------------------------------------------------
# Column mapping + row parsing
# ---------------------------------------------------------------------------
def detect_col_map(all_cols):
    def find(pattern):
        for c in all_cols:
            if re.search(pattern, c, re.I):
                return c
        return None

    return {
        "date_range": find(r"date.?range") or (all_cols[0] if all_cols else None),
        "ad_group_name": find(r"ad.?group.?name"),
        "ad_group_id": find(r"ad.?group.?id"),
        "status": find(r"^status$|ad.?group.?status"),
        "ssp": find(r"^ssp$"),
    }


def parse_rows(raw_rows, all_cols, cid_map):
    col_map = detect_col_map(all_cols)
    fixed_cols = {v for v in col_map.values() if v}
    metric_cols = [c for c in all_cols if c not in fixed_cols and c != "" and not c.startswith("__") and not c.startswith("Unnamed:")]
    metric_types = {m: detect_metric_type(m) for m in metric_cols}
    va_col_name = next((c for c in metric_cols if re.fullmatch(r"valid.?action", c, re.I)), None) \
        or next((c for c in metric_cols if re.search(r"valid.?action", c, re.I)), None)

    date_col = col_map["date_range"]
    all_rows = []
    for r in raw_rows:
        date_val = str(r.get(date_col, "")).strip() if date_col else ""
        if not date_val or date_val == "date_range":
            continue
        metrics = {m: r.get(m, "") for m in metric_cols}
        row = {
            "date_range": date_val,
            "ad_group_name": str(r.get(col_map["ad_group_name"], "")).strip() if col_map["ad_group_name"] else "",
            "ad_group_id": str(r.get(col_map["ad_group_id"], "")).strip() if col_map["ad_group_id"] else "",
            "status": str(r.get(col_map["status"], "")).strip().lower() if col_map["status"] else "",
            "ssp": str(r.get(col_map["ssp"], "")).strip() if col_map["ssp"] else "",
            "metrics": metrics,
        }
        if not row["date_range"]:
            continue
        cid_entry = cid_map.get(row["ad_group_id"])
        if cid_entry:
            row["canonical_name"] = cid_entry[0]
            row["name_changed"] = cid_entry[0] != row["ad_group_name"]
        else:
            row["canonical_name"] = row["ad_group_name"]
            row["name_changed"] = False
        all_rows.append(row)

    weeks = sorted({r["date_range"] for r in all_rows})
    latest_week = weeks[-1] if weeks else None

    latest_status_map = {}
    latest_week_va_map = {}
    for r in all_rows:
        if r["date_range"] != latest_week:
            continue
        gk = r["ad_group_id"] + "||" + r["ssp"]
        latest_status_map[gk] = r["status"]
        if va_col_name:
            latest_week_va_map[gk] = to_float_or_none(r["metrics"].get(va_col_name)) or 0

    return {
        "all_rows": all_rows,
        "metric_cols": metric_cols,
        "metric_types": metric_types,
        "weeks": weeks,
        "latest_week": latest_week,
        "latest_status_map": latest_status_map,
        "latest_week_va_map": latest_week_va_map,
        "va_col_name": va_col_name,
        "col_map": col_map,
    }


# ---------------------------------------------------------------------------
# Anomaly detection — z-score,
# computed once over ALL rows, unaffected by filters/goal rules.
# ---------------------------------------------------------------------------
def compute_anomalies(all_rows, metric_cols):
    anomaly_map = {}
    groups = {}
    for r in all_rows:
        gk = r["ad_group_id"] + "||" + r["ssp"]
        groups.setdefault(gk, []).append(r)

    for rows in groups.values():
        if len(rows) < 3:
            continue
        for m in metric_cols:
            vals = [v for v in (to_float_or_none(r["metrics"].get(m)) for r in rows) if v is not None and v != 0]
            if len(vals) < 3:
                continue
            mean = sum(vals) / len(vals)
            variance = sum((v - mean) ** 2 for v in vals) / len(vals)
            std = math.sqrt(variance)
            if std == 0:
                continue
            for r in rows:
                v = to_float_or_none(r["metrics"].get(m))
                if v is None:
                    continue
                key = f"{r['date_range']}||{r['ad_group_id']}||{r['ssp']}||{m}"
                if v > mean + 2 * std:
                    anomaly_map[key] = "high"
                elif v < mean - 2 * std:
                    anomaly_map[key] = "low"
    return anomaly_map


def has_anomaly_in_row(r, metric_cols, anomaly_map):
    return any(
        anomaly_map.get(f"{r['date_range']}||{r['ad_group_id']}||{r['ssp']}||{m}")
        for m in metric_cols
    )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def get_filtered_rows(all_rows, filters, latest_status_map, latest_week_va_map, va_col_name, metric_cols, anomaly_map):
    filters = filters or {}
    search = (filters.get("search") or "").lower()
    ssp = filters.get("ssp") or ""
    status = filters.get("status") or ""
    va_min = filters.get("vaMin")
    anomaly_only = bool(filters.get("anomalyOnly"))

    valid_groups = None
    if va_min is not None:
        valid_groups = {gk for gk, va in latest_week_va_map.items() if va >= va_min}

    out = []
    for r in all_rows:
        if search:
            hay = (r["ad_group_name"].lower(), (r.get("canonical_name") or "").lower(),
                   r["ad_group_id"].lower(), r["ssp"].lower())
            if not any(search in h for h in hay):
                continue
        if ssp and r["ssp"] != ssp:
            continue
        if status:
            ls = latest_status_map.get(r["ad_group_id"] + "||" + r["ssp"])
            if not ls or ls != status:
                continue
        if va_min is not None and va_col_name:
            gk = r["ad_group_id"] + "||" + r["ssp"]
            if gk not in valid_groups:
                continue
        if anomaly_only and not has_anomaly_in_row(r, metric_cols, anomaly_map):
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Goal rule chain
# ---------------------------------------------------------------------------
def apply_goal_rules(groups, latest_week, goal_rules):
    """groups: dict gk -> {"meta":row, "byWeek": {week: row}}"""
    group_keys = list(groups.keys())
    survived_up_to = []
    if goal_rules:
        surviving = {gk for gk in group_keys if latest_week in groups[gk]["byWeek"]}
        for rule in goal_rules:
            nxt = set()
            for gk in surviving:
                row = groups[gk]["byWeek"].get(latest_week)
                if not row:
                    continue
                n = to_float_or_none(row["metrics"].get(rule["metric"]))
                if n is None:
                    continue
                fires = (rule["dir"] == "below" and n < rule["value"]) or (rule["dir"] == "above" and n > rule["value"])
                if fires:
                    nxt.add(gk)
            survived_up_to.append(nxt)
            surviving = nxt

    rules_fired_map = {}
    for gk in group_keys:
        latest_row = groups[gk]["byWeek"].get(latest_week)
        rf = 0
        if latest_row and goal_rules:
            for rule in goal_rules:
                if not rule.get("metric"):
                    break
                n = to_float_or_none(latest_row["metrics"].get(rule["metric"]))
                if n is None:
                    break
                fires = (rule["dir"] == "below" and n < rule["value"]) or (rule["dir"] == "above" and n > rule["value"])
                if fires:
                    rf += 1
                else:
                    break
        rules_fired_map[gk] = rf

    return survived_up_to, rules_fired_map


def cell_rule_flag(m, raw_val, gk, weekRowExists, goal_rules, survived_up_to):
    if not (weekRowExists and goal_rules):
        return False
    n = to_float_or_none(raw_val)
    if n is None:
        return False
    for ri, rule in enumerate(goal_rules):
        if rule["metric"] != m:
            continue
        in_scope = True if ri == 0 else (ri - 1 < len(survived_up_to) and gk in survived_up_to[ri - 1])
        if not in_scope:
            continue
        fires = (rule["dir"] == "below" and n < rule["value"]) or (rule["dir"] == "above" and n > rule["value"])
        if fires:
            return True
    return False


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------
def cmd_discover(args):
    raw_rows, all_cols = load_raw_rows(args.file)
    cid_map = load_cid_map(args.cid_map_default, args.cid)
    parsed = parse_rows(raw_rows, all_cols, cid_map)

    all_rows = parsed["all_rows"]
    ad_group_ids = {r["ad_group_id"] for r in all_rows}
    ssps = sorted({r["ssp"] for r in all_rows})

    out = {
        "importFile": str(Path(args.file).name),
        "weeks": parsed["weeks"],
        "latestWeek": parsed["latest_week"],
        "adGroupCount": len(ad_group_ids),
        "sspList": ssps,
        "vaColName": parsed["va_col_name"],
        "metrics": [
            {"name": m, "type": parsed["metric_types"][m]}
            for m in parsed["metric_cols"]
        ],
        "columnMapping": parsed["col_map"],
        "rowCount": len(all_rows),
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()


# ---------------------------------------------------------------------------
# health — All Time Scan + Latest Week Scan: aggregate (volume-weighted) CPA,
# SSP by cost/action share, creative breakdown (by leading [bracket] tag in
# ad_group_name), MMP CVR. Column detection is fuzzy so it tolerates the
# naming variations seen across different export configurations.
# ---------------------------------------------------------------------------
def find_metric_col(metric_cols, *patterns):
    for pat in patterns:
        for c in metric_cols:
            if re.fullmatch(pat, c, re.I):
                return c
    for pat in patterns:
        for c in metric_cols:
            if re.search(pat, c, re.I):
                return c
    return None


def creative_type(ad_group_name):
    m = re.match(r"^\[([^\]]+)\]", ad_group_name or "")
    return m.group(1) if m else "Other"


def list_tags(ad_group_name):
    """Best-effort extraction of list/targeting tags from ad_group_name text —
    SK tier, PM list / Fix PM list, IDFA, IDFV, whitelist — and their
    combinations (e.g. 'PM x SK 10' -> ['SK 10', 'PM list']).

    IMPORTANT — this is heuristic, not authoritative: every optimizer/AM names
    ad groups differently, so there is no single pattern that reliably reads
    off "what list is this ad group actually running." This only catches what
    happens to be spelled out in the name using patterns seen in real exports/
    real exports so far; an ad group named differently will fall into "No list tag
    detected" even if it's genuinely running one of these. Treat the resulting
    breakdown as a rough signal to sanity-check, not a ground-truth dimension —
    say so when presenting it, per the user's own caution about this (2026-07-
    ish): naming isn't standardized enough to fully rely on.

    Note the regex avoids `\b` before "SK"/"PM" — `\b` doesn't fire between an
    underscore and a letter (both count as word characters), and these tags
    are almost always underscore-prefixed in practice (e.g. '..._SK 9-10'),
    which silently broke an earlier version of this extraction.
    """
    name = ad_group_name or ""
    tags = []
    sk_m = re.search(r"(?<![A-Za-z])SK\s*(\d+(?:\s*-\s*\d+)?)", name, re.I)
    if sk_m:
        tags.append(f"SK {sk_m.group(1).replace(' ', '')}")
    if re.search(r"fix\s*pm(\s*list)?\b", name, re.I):
        tags.append("Fix PM list")
    elif re.search(r"(?<![A-Za-z])pm(\s*list)?(?![A-Za-z])", name, re.I):
        tags.append("PM list")
    if re.search(r"(?<![A-Za-z])idfa(?![A-Za-z])", name, re.I):
        tags.append("IDFA")
    if re.search(r"(?<![A-Za-z])idfv(?![A-Za-z])", name, re.I):
        tags.append("IDFV")
    if re.search(r"(?<![A-Za-z])wl(?![A-Za-z])|whitelist", name, re.I):
        tags.append("WL")
    return tags if tags else ["No list tag detected"]


def list_tag_combo(ad_group_name):
    """Single bucket key for group_breakdown() — the sorted combination of
    tags found, so 'SK 10 x PM list' and 'PM list x SK 10' land in the same
    bucket, and a genuine combination isn't split across two rows."""
    return " + ".join(sorted(list_tags(ad_group_name)))


def pct_change(latest, prior):
    if latest is None or prior is None or prior == 0:
        return None
    return round(((latest - prior) / abs(prior)) * 100, 1)


def sum_metric(rows, col):
    if not col:
        return None
    total = 0.0
    for r in rows:
        v = to_float_or_none(r["metrics"].get(col))
        if v is not None:
            total += v
    return total


def avg_metric(rows, col):
    if not col:
        return None
    vals = [v for v in (to_float_or_none(r["metrics"].get(col)) for r in rows) if v is not None and v != 0]
    if not vals:
        return None
    return sum(vals) / len(vals)


def metric_trend(all_rows, latest_rows, prior_rows, col, value_type):
    """All-Time avg + Latest-Week avg + WoW% for a single metric column."""
    if not col:
        return None
    all_avg = avg_metric(all_rows, col)
    latest_avg = avg_metric(latest_rows, col)
    prior_avg = avg_metric(prior_rows, col) if prior_rows else None
    return {
        "metric": col,
        "allTimeAvg": round(all_avg, 6) if all_avg is not None else None,
        "allTimeAvgText": format_val(all_avg, value_type) if all_avg is not None else "—",
        "latestAvg": round(latest_avg, 6) if latest_avg is not None else None,
        "latestAvgText": format_val(latest_avg, value_type) if latest_avg is not None else "—",
        "wowPct": pct_change(latest_avg, prior_avg),
    }


def aggregate_cpa(rows, cost_col, va_col):
    cost = sum_metric(rows, cost_col)
    va = sum_metric(rows, va_col)
    if cost is None or va is None or va == 0:
        return None
    return cost / va


def group_breakdown(rows, classify_fn, label_key, cost_col, va_col, invalid_col):
    """Shared by creative_breakdown() and list_breakdown() — bucket rows by
    whatever classify_fn extracts from ad_group_name, then report volume/cost/
    aggregate-CPA per bucket."""
    buckets = {}
    for r in rows:
        key = classify_fn(r["ad_group_name"])
        buckets.setdefault(key, []).append(r)
    out = []
    for key, rs in buckets.items():
        va = sum_metric(rs, va_col) or 0
        inv = sum_metric(rs, invalid_col) or 0
        cpa = aggregate_cpa(rs, cost_col, va_col)
        cost = sum_metric(rs, cost_col) or 0
        out.append({
            label_key: key,
            "validAction": va,
            "invalidAction": inv,
            "avgCPA": round(cpa, 4) if cpa is not None else None,
            "avgCPAText": format_val(cpa, "dollar") if cpa is not None else "—",
            "rawCost": round(cost, 4),
        })
    out.sort(key=lambda x: x["rawCost"], reverse=True)
    return out


def creative_breakdown(rows, cost_col, va_col, invalid_col):
    return group_breakdown(rows, creative_type, "creative", cost_col, va_col, invalid_col)


def list_breakdown(rows, cost_col, va_col, invalid_col):
    return group_breakdown(rows, list_tag_combo, "listTags", cost_col, va_col, invalid_col)


def ssp_by_metric(rows, col, denom_total, label):
    buckets = {}
    for r in rows:
        buckets.setdefault(r["ssp"], []).append(r)
    out = []
    for ssp, rs in buckets.items():
        val = sum_metric(rs, col) or 0
        rate = (val / denom_total * 100) if denom_total else None
        out.append({"ssp": ssp, label: round(val, 4), "ratePct": round(rate, 2) if rate is not None else None})
    out.sort(key=lambda x: x[label], reverse=True)
    return out


def ssp_efficiency_rows(rows, ssps, win_rate_col, cpm_col, bid_col, win_col, impression_col, cost_col):
    """Win Rate + CPM per SSP — lets you tell 'no supply anywhere' (both low)
    from 'being outbid' (low win rate, normal CPM) from 'structurally cheap/
    expensive inventory' (CPM itself is the outlier) — a distinction real AM/CM
    campaign managers make constantly, and plain cost/action share can't show it.

    Volume-weighted (total Win / total Bid, total Cost / total Impression*1000)
    when Bid/Win/Impression columns exist — NOT a naive average of each row's
    own Win Rate/CPM value. Confirmed against real data that the naive average
    can be badly skewed by a handful of low-volume rows with an extreme ratio
    (one SSP's naive-average win rate came out 3.5x its true volume-weighted
    rate). Falls back to averaging the ratio columns only if the raw counts
    aren't in the file."""
    out = []
    for ssp in ssps:
        ssp_rows = [r for r in rows if r["ssp"] == ssp]

        wr = None
        if bid_col and win_col:
            total_bid = sum_metric(ssp_rows, bid_col) or 0
            total_win = sum_metric(ssp_rows, win_col) or 0
            wr = (total_win / total_bid) if total_bid else None
        if wr is None:
            wr = avg_metric(ssp_rows, win_rate_col)

        cpm = None
        if impression_col and cost_col:
            total_impr = sum_metric(ssp_rows, impression_col) or 0
            total_cost = sum_metric(ssp_rows, cost_col) or 0
            cpm = (total_cost / total_impr * 1000) if total_impr else None
        if cpm is None:
            cpm = avg_metric(ssp_rows, cpm_col)

        out.append({
            "ssp": ssp,
            "winRate": round(wr, 4) if wr is not None else None,
            "winRateText": format_val(wr, "pct") if wr is not None else "—",
            "cpm": round(cpm, 4) if cpm is not None else None,
            "cpmText": format_val(cpm, "dollar") if cpm is not None else "—",
        })
    return out


def structure_changes(latest_rows, prior_rows, prior_week):
    """Which ad_group_id+SSP combos newly appeared or disappeared between the
    prior week and the latest week. Answers the first branch of a real
    recurring campaign-manager diagnosis question: 'is this WoW
    swing because our own CID/ad-group structure changed, because something
    changed on the client's side, or because we changed a policy/target?' —
    this only ever answers the first branch (it's the only one visible from
    the file itself); the other two require asking the user directly, they
    are not derivable from a performance export."""
    if not prior_week:
        return None
    latest_keys = {(r["ad_group_id"], r["ssp"]): r["ad_group_name"] for r in latest_rows}
    prior_keys = {(r["ad_group_id"], r["ssp"]): r["ad_group_name"] for r in prior_rows}
    added = [
        {"adGroupId": k[0], "ssp": k[1], "adGroupName": latest_keys[k]}
        for k in latest_keys.keys() - prior_keys.keys()
    ]
    removed = [
        {"adGroupId": k[0], "ssp": k[1], "adGroupName": prior_keys[k]}
        for k in prior_keys.keys() - latest_keys.keys()
    ]
    added.sort(key=lambda x: (x["ssp"], x["adGroupName"]))
    removed.sort(key=lambda x: (x["ssp"], x["adGroupName"]))
    return {"addedCount": len(added), "removedCount": len(removed), "added": added, "removed": removed}


def build_health_check(args_file, cid_map_default, cid_file, parsed=None):
    if parsed is None:
        raw_rows, all_cols = load_raw_rows(args_file)
        cid_map = load_cid_map(cid_map_default, cid_file)
        parsed = parse_rows(raw_rows, all_cols, cid_map)
    all_rows = parsed["all_rows"]
    metric_cols = parsed["metric_cols"]
    metric_types = parsed["metric_types"]
    weeks = parsed["weeks"]
    latest_week = parsed["latest_week"]
    prior_week = weeks[-2] if len(weeks) > 1 else None

    va_col = parsed["va_col_name"]
    cost_col = find_metric_col(metric_cols, r"^raw cost$", r"raw.?cost", r"cost")
    invalid_col = find_metric_col(metric_cols, r"^invalid action$", r"invalid.?action", r"invalid")
    mmp_click_col = find_metric_col(metric_cols, r"^mmp total click$", r"mmp.?total.?click", r"mmp.?click")
    # These reflect how campaign managers actually read this data in practice:
    # ROAS is tracked across
    # multiple attribution windows at once (not one column), CTCV/VTCV rate is
    # its own lens distinct from generic CVR, CPI (not just CPA) is the primary
    # bid lever in these mobile-game verticals, and win rate/CPM per SSP is how
    # AMs tell "no supply" from "being outbid" from "structurally cheap/pricey".
    win_rate_col = find_metric_col(metric_cols, r"^win rate$", r"win.?rate")
    cpm_col = find_metric_col(metric_cols, r"^raw cpm$", r"^cpm$", r"raw.?cpm", r"\bcpm\b")
    cpi_col = find_metric_col(metric_cols, r"^cpi$", r"cost.per.install")
    bid_col = find_metric_col(metric_cols, r"^bid$")
    win_col = find_metric_col(metric_cols, r"^win$")
    impression_col = find_metric_col(metric_cols, r"^impression$", r"^mmp impression$")
    roas_cols = [c for c in metric_cols if metric_types[c] == "roas"]
    ctcv_vtcv_cols = [c for c in metric_cols if metric_types[c] == "pct" and re.search(r"ctcv|vtcv", c, re.I)]

    columns_used = {
        "validAction": va_col, "rawCost": cost_col, "invalidAction": invalid_col, "mmpTotalClick": mmp_click_col,
        "winRate": win_rate_col, "cpm": cpm_col, "cpi": cpi_col, "roasWindows": roas_cols, "ctcvVtcv": ctcv_vtcv_cols,
        "bid": bid_col, "win": win_col, "impression": impression_col,
    }

    # ---- All Time Scan ----
    total_va = sum_metric(all_rows, va_col) or 0
    total_cost = sum_metric(all_rows, cost_col) or 0
    total_invalid = sum_metric(all_rows, invalid_col) or 0
    avg_cpa_all = aggregate_cpa(all_rows, cost_col, va_col)

    ssp_cost_all = ssp_by_metric(all_rows, cost_col, total_cost, "cost")
    ssp_action_all = ssp_by_metric(all_rows, va_col, total_va, "validAction")
    creative_all = creative_breakdown(all_rows, cost_col, va_col, invalid_col)
    list_all = list_breakdown(all_rows, cost_col, va_col, invalid_col)

    all_time_efficiency_ssps = {b["ssp"] for b in ssp_cost_all[:3]} | {b["ssp"] for b in ssp_action_all[:3]}
    ssp_efficiency_all = (
        ssp_efficiency_rows(all_rows, sorted(all_time_efficiency_ssps), win_rate_col, cpm_col,
                            bid_col, win_col, impression_col, cost_col)
        if (win_rate_col or cpm_col) else None
    )

    all_time = {
        "totalValidAction": total_va,
        "totalRawCost": round(total_cost, 4),
        "avgCPA": round(avg_cpa_all, 4) if avg_cpa_all is not None else None,
        "avgCPAText": format_val(avg_cpa_all, "dollar") if avg_cpa_all is not None else "—",
        "totalInvalidAction": total_invalid,
        "sspByCostTop3": ssp_cost_all[:3],
        "sspByActionTop3": ssp_action_all[:3],
        "sspEfficiency": ssp_efficiency_all,
        "creativeBreakdown": creative_all,
        "listBreakdown": list_all,
    }

    # ---- Latest Week Scan ----
    latest_rows = [r for r in all_rows if r["date_range"] == latest_week]
    prior_rows = [r for r in all_rows if r["date_range"] == prior_week] if prior_week else []

    va_latest = sum_metric(latest_rows, va_col) or 0
    va_prior = sum_metric(prior_rows, va_col) if prior_week else None
    cost_latest = sum_metric(latest_rows, cost_col) or 0
    invalid_latest = sum_metric(latest_rows, invalid_col) or 0
    invalid_prior = sum_metric(prior_rows, invalid_col) if prior_week else None

    cpa_latest = aggregate_cpa(latest_rows, cost_col, va_col)
    cpa_prior = aggregate_cpa(prior_rows, cost_col, va_col) if prior_week else None
    wow_cpa_abs = (round(cpa_latest - cpa_prior, 4) if (cpa_latest is not None and cpa_prior is not None) else None)

    ssp_cost_latest = ssp_by_metric(latest_rows, cost_col, cost_latest, "cost")
    ssp_action_latest = ssp_by_metric(latest_rows, va_col, va_latest, "validAction")
    # WoW per SSP (latest vs prior week, own value)
    prior_cost_by_ssp = {b["ssp"]: b["cost"] for b in ssp_by_metric(prior_rows, cost_col, None, "cost")} if prior_week else {}
    prior_va_by_ssp = {b["ssp"]: b["validAction"] for b in ssp_by_metric(prior_rows, va_col, None, "validAction")} if prior_week else {}
    for b in ssp_cost_latest:
        b["wowPct"] = pct_change(b["cost"], prior_cost_by_ssp.get(b["ssp"]))
    for b in ssp_action_latest:
        b["wowPct"] = pct_change(b["validAction"], prior_va_by_ssp.get(b["ssp"]))

    latest_efficiency_ssps = {b["ssp"] for b in ssp_cost_latest[:5]} | {b["ssp"] for b in ssp_action_latest[:5]}
    ssp_efficiency_latest = (
        ssp_efficiency_rows(latest_rows, sorted(latest_efficiency_ssps), win_rate_col, cpm_col,
                            bid_col, win_col, impression_col, cost_col)
        if (win_rate_col or cpm_col) else None
    )

    mmp_cvr = None
    if mmp_click_col:
        clicks_latest = sum_metric(latest_rows, mmp_click_col) or 0
        cvr_latest = (va_latest / clicks_latest) if clicks_latest else None
        cvr_prior = None
        if prior_week:
            clicks_prior = sum_metric(prior_rows, mmp_click_col) or 0
            cvr_prior = (va_prior / clicks_prior) if (va_prior is not None and clicks_prior) else None
        mmp_cvr = {
            "value": round(cvr_latest, 4) if cvr_latest is not None else None,
            "valueText": format_val(cvr_latest, "pct") if cvr_latest is not None else "—",
            "wowPct": pct_change(cvr_latest, cvr_prior),
        }

    latest_week_scan = {
        "week": latest_week,
        "priorWeek": prior_week,
        "totalValidAction": va_latest,
        "wowValidActionPct": pct_change(va_latest, va_prior),
        "avgCPA": round(cpa_latest, 4) if cpa_latest is not None else None,
        "avgCPAText": format_val(cpa_latest, "dollar") if cpa_latest is not None else "—",
        "wowCPAAbs": wow_cpa_abs,
        "totalInvalidAction": invalid_latest,
        "wowInvalidActionPct": pct_change(invalid_latest, invalid_prior),
        "creativeBreakdown": creative_breakdown(latest_rows, cost_col, va_col, invalid_col),
        "listBreakdown": list_breakdown(latest_rows, cost_col, va_col, invalid_col),
        "sspByCost": ssp_cost_latest[:5],
        "sspByAction": ssp_action_latest[:5],
        "sspEfficiency": ssp_efficiency_latest,
        "mmpCVR": mmp_cvr,
        "structureChanges": structure_changes(latest_rows, prior_rows, prior_week),
    }

    # ROAS windows (D0/D7/... — however many roas-typed columns this file has,
    # tracked side by side rather than picking just one), CTCV/VTCV rate, and
    # CPI trend — see the columns_used comment above for why these were added.
    roas_windows = [metric_trend(all_rows, latest_rows, prior_rows, c, "roas") for c in roas_cols]
    ctcv_vtcv = [metric_trend(all_rows, latest_rows, prior_rows, c, "pct") for c in ctcv_vtcv_cols]

    cpi_block = None
    if cpi_col:
        cpi_all = avg_metric(all_rows, cpi_col)
        cpi_latest = avg_metric(latest_rows, cpi_col)
        cpi_prior = avg_metric(prior_rows, cpi_col) if prior_week else None
        cpi_block = {
            "metric": cpi_col,
            "allTimeAvg": round(cpi_all, 4) if cpi_all is not None else None,
            "allTimeAvgText": format_val(cpi_all, "dollar") if cpi_all is not None else "—",
            "latestAvg": round(cpi_latest, 4) if cpi_latest is not None else None,
            "latestAvgText": format_val(cpi_latest, "dollar") if cpi_latest is not None else "—",
            "wowAbs": round(cpi_latest - cpi_prior, 4) if (cpi_latest is not None and cpi_prior is not None) else None,
        }

    return {
        "meta": {"importFile": str(Path(args_file).name), "weeks": weeks, "latestWeek": latest_week},
        "columnsUsed": columns_used,
        "allTime": all_time,
        "latestWeek": latest_week_scan,
        "roasWindows": roas_windows,
        "ctcvVtcv": ctcv_vtcv,
        "cpi": cpi_block,
    }


def cmd_health(args):
    result = build_health_check(args.file, args.cid_map_default, args.cid)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


# ---------------------------------------------------------------------------
# full — health + analyze in one process, parsing the file exactly once.
# Once the goal is known (asked upfront, per the current flow), health and
# analyze are always needed together — running them as two separate CLI
# invocations means parsing the same file twice and paying two Python
# process-startup costs for no reason. Added after the user flagged
# end-to-end wait time as too long during live testing.
# ---------------------------------------------------------------------------
def cmd_full(args):
    raw_rows, all_cols = load_raw_rows(args.file)
    cid_map = load_cid_map(args.cid_map_default, args.cid)
    parsed = parse_rows(raw_rows, all_cols, cid_map)
    health = build_health_check(args.file, args.cid_map_default, args.cid, parsed=parsed)
    analysis = build_analysis(
        args.file, args.cid_map_default, args.cid, args.rules, args.filters, parsed=parsed,
        lite=getattr(args, "lite", False),
        max_weeks=getattr(args, "max_weeks", None),
        top_groups=getattr(args, "top_groups", None),
    )
    json.dump({"health": health, "analysis": analysis}, sys.stdout, indent=2, ensure_ascii=False)
    print()


# ---------------------------------------------------------------------------
# estimate — cheap pre-flight sizing so the caller can warn the user (and offer
# a lite run) BEFORE doing the expensive work. Parses the file but emits only
# counts, so it costs almost nothing to run.
#
# The band thresholds and the bytes-per-cell constant below are calibrated from
# real files measured during development (~170 bytes of JSON per
# group x week x metric cell; e.g. 188 groups x 8 weeks x 35 metrics produced a
# ~4.8 MB group matrix). They are a rough workload signal, NOT a token count —
# actual token use depends on how much of the output the caller reads back.
# ---------------------------------------------------------------------------
BYTES_PER_CELL = 170


def cmd_estimate(args):
    raw_rows, all_cols = load_raw_rows(args.file)
    cid_map = load_cid_map(args.cid_map_default, args.cid)
    parsed = parse_rows(raw_rows, all_cols, cid_map)

    all_rows = parsed["all_rows"]
    n_metrics = len(parsed["metric_cols"])
    n_weeks = len(parsed["weeks"])
    n_groups = len({(r["ad_group_id"], r["ssp"]) for r in all_rows})
    cells = n_groups * n_weeks * n_metrics
    est_bytes = cells * BYTES_PER_CELL

    # Banded on estimated output size rather than raw cell count — size is what
    # actually costs the caller, and it's the number worth telling the user.
    est_mb = est_bytes / 1_048_576
    if est_mb < 0.5:
        band, advice = "light", "detailed run is fine, no need to warn the user"
    elif est_mb < 3:
        band, advice = "medium", "detailed run is usually fine; mention lite if the user is budget-conscious"
    else:
        band, advice = "heavy", "warn the user and offer the lite run before proceeding"

    # What the lite preset would reduce it to (mirrors build_analysis's defaults).
    lite_metrics = min(n_metrics, 8)
    lite_cells = min(n_groups, 15) * min(n_weeks, 2) * lite_metrics

    out = {
        "importFile": str(Path(args.file).name),
        "adGroupSspCombos": n_groups,
        "weeks": n_weeks,
        "metrics": n_metrics,
        "rows": len(all_rows),
        "detailed": {"cells": cells, "approxJsonBytes": est_bytes,
                     "approxJsonMB": round(est_mb, 1)},
        "lite": {"cells": lite_cells, "approxJsonBytes": lite_cells * BYTES_PER_CELL,
                 "approxJsonMB": round(lite_cells * BYTES_PER_CELL / 1_048_576, 2),
                 "smallerByFactor": (f"~{round(cells / lite_cells)}x" if lite_cells else "n/a")},
        "band": band,
        "advice": advice,
        "note": "Rough workload sizing from real-file calibration, not a token count.",
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()


# ---------------------------------------------------------------------------
# allocation — actual cost share per SSP vs. a user-supplied target %.
# Models the common "manual cost-rate allocation plan" pattern
# (e.g. "google 20%, smaato 30%, rubicon 10%..."), which the file
# itself has no notion of — the target has to come from the user every time,
# there's nothing to detect it from.
# ---------------------------------------------------------------------------
def build_allocation_check(args_file, cid_map_default, cid_file, target_json, scope):
    raw_rows, all_cols = load_raw_rows(args_file)
    cid_map = load_cid_map(cid_map_default, cid_file)
    parsed = parse_rows(raw_rows, all_cols, cid_map)
    all_rows = parsed["all_rows"]
    metric_cols = parsed["metric_cols"]
    latest_week = parsed["latest_week"]
    cost_col = find_metric_col(metric_cols, r"^raw cost$", r"raw.?cost", r"cost")

    raw_targets = json.loads(target_json)
    if not raw_targets:
        raise ValueError("--target must be a non-empty JSON object of {ssp: target_percent}")
    # Accept either fractions (0-1) or already-percentages (0-100).
    targets = {ssp: (v * 100 if 0 < v <= 1 else v) for ssp, v in raw_targets.items()}

    if scope == "latest":
        rows = [r for r in all_rows if r["date_range"] == latest_week]
        scope_label = latest_week
    elif scope == "all_time":
        rows = all_rows
        scope_label = "all_time"
    else:
        raise ValueError(f"--scope must be 'latest' or 'all_time', got {scope!r}")

    total_cost = sum_metric(rows, cost_col) or 0
    actual = ssp_by_metric(rows, cost_col, total_cost, "cost")
    actual_by_ssp = {b["ssp"]: b["ratePct"] for b in actual}

    all_ssps = set(actual_by_ssp) | set(targets)
    out = []
    for ssp in all_ssps:
        act = actual_by_ssp.get(ssp, 0.0)
        tgt = targets.get(ssp)
        out.append({
            "ssp": ssp,
            "actualPct": round(act, 2),
            "targetPct": round(tgt, 2) if tgt is not None else None,
            "deviationPct": round(act - tgt, 2) if tgt is not None else None,
        })
    out.sort(key=lambda x: abs(x["deviationPct"]) if x["deviationPct"] is not None else -1, reverse=True)

    return {
        "meta": {"importFile": str(Path(args_file).name), "scope": scope_label, "costCol": cost_col},
        "allocation": out,
    }


def cmd_allocation(args):
    result = build_allocation_check(args.file, args.cid_map_default, args.cid, args.target, args.scope)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def build_analysis(args_file, cid_map_default, cid_file, rules_json, filters_json, parsed=None,
                   lite=False, max_weeks=None, top_groups=None):
    """`lite=True` trims the per-group×week×metric matrix — by far the biggest
    part of this output (measured: ~10 KB for the health scan vs ~4.8 MB for
    the group matrix on a 188-group × 8-week × 35-metric file). It keeps the
    goal metrics plus a few core ones, the last 2 weeks, and the most relevant
    groups, so a caller working under a tight token budget can still get a
    correct answer to the stated goal. Nothing is approximated — the numbers
    that survive the trim are computed identically; there is simply less of
    them. max_weeks/top_groups override the lite defaults when given."""
    if parsed is None:
        raw_rows, all_cols = load_raw_rows(args_file)
        cid_map = load_cid_map(cid_map_default, cid_file)
        parsed = parse_rows(raw_rows, all_cols, cid_map)

    all_rows = parsed["all_rows"]
    metric_cols = parsed["metric_cols"]
    metric_types = parsed["metric_types"]
    latest_week = parsed["latest_week"]

    goal_rules = json.loads(rules_json) if rules_json else []
    for rule in goal_rules:
        if rule.get("metric") not in metric_cols:
            raise ValueError(f"Unknown metric in rule: {rule.get('metric')!r}. Valid metrics: {metric_cols}")
        if rule.get("dir") not in ("below", "above"):
            raise ValueError(f"Rule dir must be 'below' or 'above', got {rule.get('dir')!r}")
    goal_rules = goal_rules[:3]

    filters = json.loads(filters_json) if filters_json else {}

    cost_col = find_metric_col(metric_cols, r"^raw cost$", r"raw.?cost", r"cost")

    # Anomalies are a property of the data, so both the map AND the reported
    # counts are computed over the untrimmed metric set / week range. Keeping
    # copies here means anomalyCounts reads identically on a lite and a detailed
    # run — otherwise a lite run silently under-reports them, which would make
    # "no anomalies" indistinguishable from "we didn't look".
    anomaly_map = compute_anomalies(all_rows, metric_cols)
    filtered = get_filtered_rows(
        all_rows, filters, parsed["latest_status_map"], parsed["latest_week_va_map"],
        parsed["va_col_name"], metric_cols, anomaly_map,
    )
    filtered_untrimmed = filtered
    metric_cols_untrimmed = list(metric_cols)

    if lite:
        # Keep the goal metrics plus the handful needed to judge whether a
        # result is trustworthy (volume, spend, efficiency) — dropping the rest
        # is where nearly all the size saving comes from.
        keep = []
        for rule in goal_rules:
            if rule["metric"] not in keep:
                keep.append(rule["metric"])
        for pat in (r"^valid action$", r"valid.?action", r"^raw cost$", r"raw.?cost",
                    r"^raw cpa$", r"^cpa$", r"^cpi$", r"^win rate$", r"^raw cpm$", r"^cpm$"):
            c = find_metric_col(metric_cols, pat)
            if c and c not in keep:
                keep.append(c)
        metric_cols = [m for m in metric_cols if m in keep]
        if max_weeks is None:
            max_weeks = 2
        if top_groups is None:
            top_groups = 15

    sorted_weeks = sorted({r["date_range"] for r in filtered}, reverse=True)
    if max_weeks:
        sorted_weeks = sorted_weeks[:max_weeks]
        kept_weeks = set(sorted_weeks)
        filtered = [r for r in filtered if r["date_range"] in kept_weeks]

    # Grouped by ad_group_id (not name) — an ad group's display name can change
    # mid-campaign in the platform (same ID, retargeted/renamed) while its ID
    # stays stable, so identity/history must key on ad_group_id. `meta` is kept
    # as whichever row has the latest date_range seen so far, so the displayed
    # name always reflects current targeting, not whatever name happened to be
    # in the first row parsed (which — since files are typically chronological
    # — would otherwise be the OLDEST, possibly stale, name).
    groups = {}
    for r in filtered:
        gk = r["ad_group_id"] + "||" + r["ssp"]
        g = groups.setdefault(gk, {"meta": r, "byWeek": {}})
        if r["date_range"] >= g["meta"]["date_range"]:
            g["meta"] = r
        g["byWeek"][r["date_range"]] = r

    survived_up_to, rules_fired_map = apply_goal_rules(groups, latest_week, goal_rules)

    # anomaly counts over the untrimmed view (see note above), so lite and
    # detailed runs report the same figure
    anomaly_counts = {"high": 0, "low": 0}
    for r in filtered_untrimmed:
        for m in metric_cols_untrimmed:
            a = anomaly_map.get(f"{r['date_range']}||{r['ad_group_id']}||{r['ssp']}||{m}")
            if a == "high":
                anomaly_counts["high"] += 1
            elif a == "low":
                anomaly_counts["low"] += 1

    # per-group output rows
    out_groups = []
    summary_vals = {m: {w: {"sum": 0.0, "count": 0} for w in sorted_weeks} for m in metric_cols}
    rule_flag_cell_count = 0

    group_items = sorted(groups.items(), key=lambda kv: (-rules_fired_map.get(kv[0], 0), kv[0]))
    groups_omitted = 0
    if top_groups and len(group_items) > top_groups:
        # Never drop a group that fired a rule — those are the answer to the
        # user's goal. Fill the remaining slots with the biggest spenders, so
        # what's dropped is genuinely low-signal (tiny/idle groups).
        latest_cost = {}
        for gk, grp in group_items:
            row = grp["byWeek"].get(sorted_weeks[0]) if sorted_weeks else None
            latest_cost[gk] = (to_float_or_none(row["metrics"].get(cost_col)) or 0) if (row and cost_col) else 0
        fired = [it for it in group_items if rules_fired_map.get(it[0], 0) > 0]
        rest = [it for it in group_items if rules_fired_map.get(it[0], 0) == 0]
        rest.sort(key=lambda kv: -latest_cost.get(kv[0], 0))
        kept = fired + rest[:max(0, top_groups - len(fired))]
        groups_omitted = len(group_items) - len(kept)
        kept_keys = {gk for gk, _ in kept}
        group_items = [it for it in group_items if it[0] in kept_keys]

    for gk, grp in group_items:
        r = grp["meta"]
        weeks_out = {}
        wow_out = {}
        for wi, w in enumerate(sorted_weeks):
            week_row = grp["byWeek"].get(w)
            raw_val_by_metric = {}
            week_metrics = {}
            for m in metric_cols:
                raw_val = week_row["metrics"].get(m) if week_row else ""
                raw_val_by_metric[m] = raw_val
                mtype = metric_types[m]
                anom = anomaly_map.get(f"{w}||{r['ad_group_id']}||{r['ssp']}||{m}") if week_row else None
                rflag = cell_rule_flag(m, raw_val, gk, bool(week_row), goal_rules, survived_up_to)
                if rflag:
                    rule_flag_cell_count += 1
                n = to_float_or_none(raw_val)
                if week_row and n is not None and n != 0:
                    summary_vals[m][w]["sum"] += n
                    summary_vals[m][w]["count"] += 1
                week_metrics[m] = {
                    "raw": raw_val if week_row else None,
                    "text": format_val(raw_val, mtype) if week_row else "—",
                    "anomaly": anom,
                    "ruleFlag": rflag,
                }
            weeks_out[w] = week_metrics

            if wi == 0 and len(sorted_weeks) > 1:
                prev_row = grp["byWeek"].get(sorted_weeks[1])
                for m in metric_cols:
                    cur_n = to_float_or_none(raw_val_by_metric[m])
                    prev_n = to_float_or_none(prev_row["metrics"].get(m)) if prev_row else None
                    if cur_n is not None and prev_n is not None and prev_n != 0:
                        chg = ((cur_n - prev_n) / abs(prev_n)) * 100
                        direction = "flat" if abs(chg) < 1 else ("up" if chg > 0 else "down")
                        wow_out[m] = {"pctChange": round(chg, 1), "direction": direction}

        latest_status = parsed["latest_status_map"].get(gk)
        # Names seen for this ad_group_id across the weeks in view — if more
        # than one, the ad group was renamed/retargeted mid-period in the
        # platform. Same ID, so history/anomalies are still correctly grouped
        # together; this just flags that a WoW comparison may be crossing a
        # targeting change, not a pure performance shift.
        names_in_period = sorted({wr["ad_group_name"] for wr in grp["byWeek"].values() if wr["ad_group_name"]})
        out_groups.append({
            "adGroupId": r["ad_group_id"],
            "adGroupName": r["ad_group_name"],
            "canonicalName": r.get("canonical_name"),
            "nameChanged": r.get("name_changed", False),
            "nameHistoryChanged": len(names_in_period) > 1,
            "namesInPeriod": names_in_period if len(names_in_period) > 1 else None,
            "ssp": r["ssp"],
            "latestStatus": latest_status,
            "rulesFired": rules_fired_map.get(gk, 0),
            "weeks": weeks_out,
            "wow": wow_out,
        })

    # summary row: avg for pct/roas/dollar/float, total for int
    summary_out = {}
    for m in metric_cols:
        mtype = metric_types[m]
        use_avg = mtype in ("pct", "roas", "dollar", "float")
        per_week = {}
        for w in sorted_weeks:
            s = summary_vals[m][w]
            if s["count"] == 0:
                per_week[w] = {"value": None, "text": "—"}
            else:
                val = s["sum"] / s["count"] if use_avg else s["sum"]
                per_week[w] = {"value": round(val, 6), "text": format_val(val, mtype), "mode": "avg" if use_avg else "total"}
        summary_out[m] = per_week

    # SSP breakdown at latest week (over filtered rows)
    ssp_breakdown = {}
    latest_filtered_rows = [r for r in filtered if r["date_range"] == (sorted_weeks[0] if sorted_weeks else None)]
    for r in latest_filtered_rows:
        ssp = r["ssp"]
        bucket = ssp_breakdown.setdefault(ssp, {"count": 0, "metrics": {}})
        bucket["count"] += 1
        for m in metric_cols:
            n = to_float_or_none(r["metrics"].get(m))
            if n is not None and n != 0:
                mb = bucket["metrics"].setdefault(m, {"sum": 0.0, "count": 0})
                mb["sum"] += n
                mb["count"] += 1
    for ssp, bucket in ssp_breakdown.items():
        for m, mb in bucket["metrics"].items():
            avg = mb["sum"] / mb["count"] if mb["count"] else 0
            mb["avg"] = round(avg, 6)
            mb["avgText"] = format_val(avg, metric_types[m])

    # SSP-level goal evaluation: aggregate each rule's metric per SSP at latest
    # week, then apply the same sequential rule-chain firing used in
    # apply_goal_rules(), but at SSP granularity. Lets the caller show which
    # SSPs meet/near the goal overall before drilling into ad groups within them.
    #
    # A rule metric that's a cost-per-X ratio (name contains "cpa"/"cpi", and
    # Raw Cost + Valid Action columns exist) is reconstructed as ΣCost/ΣVA for
    # that SSP — NOT averaged from each row's own pre-computed ratio value.
    # Confirmed on a real file this matters: gadex_hk's naive per-row average
    # came out $15.02 (driven by a couple of low-volume/high-CPA ad groups),
    # while the true volume-weighted rate (its actual total cost over total
    # valid action) was $6.32 — a >2x distortion on the SSP carrying 65% of
    # all spend. Same trap as the Win Rate/CPM fix in ssp_efficiency_rows,
    # just discovered later because sspGoalEval takes an arbitrary user metric
    # rather than a fixed one. Any other metric (ROAS, CVR, etc.) still uses
    # the plain per-row average — there's no raw numerator/denominator to
    # reconstruct those from (see the Notes section below).
    ssp_goal_eval = []
    if goal_rules:
        is_cpa_like = lambda m: cost_col and parsed["va_col_name"] and m != cost_col and re.search(r"cpa|cpi", m, re.I)
        ssp_latest_vals = {}
        ssp_ad_group_ids = {}
        for r in latest_filtered_rows:
            ssp = r["ssp"]
            ssp_ad_group_ids.setdefault(ssp, set()).add(r["ad_group_id"])
            bucket = ssp_latest_vals.setdefault(ssp, {})
            cv = bucket.setdefault("__cost_va__", {"cost": 0.0, "va": 0.0})
            cost_v = to_float_or_none(r["metrics"].get(cost_col)) if cost_col else None
            va_v = to_float_or_none(r["metrics"].get(parsed["va_col_name"])) if parsed["va_col_name"] else None
            if cost_v is not None:
                cv["cost"] += cost_v
            if va_v is not None:
                cv["va"] += va_v
            for rule in goal_rules:
                m = rule["metric"]
                n = to_float_or_none(r["metrics"].get(m))
                if n is not None and n != 0:
                    mb = bucket.setdefault(m, {"sum": 0.0, "count": 0})
                    mb["sum"] += n
                    mb["count"] += 1
        for ssp, bucket in ssp_latest_vals.items():
            rf = 0
            rule_values = {}
            for rule in goal_rules:
                m = rule["metric"]
                if is_cpa_like(m):
                    cv = bucket["__cost_va__"]
                    val = (cv["cost"] / cv["va"]) if cv["va"] else None
                else:
                    mb = bucket.get(m)
                    if not mb or mb["count"] == 0:
                        val = None
                    else:
                        use_avg = metric_types[m] in ("pct", "roas", "dollar", "float")
                        val = mb["sum"] / mb["count"] if use_avg else mb["sum"]
                if val is None:
                    rule_values[m] = None
                    break
                rule_values[m] = round(val, 6)
                fires = (rule["dir"] == "below" and val < rule["value"]) or (rule["dir"] == "above" and val > rule["value"])
                if fires:
                    rf += 1
                else:
                    break
            ssp_goal_eval.append({
                "ssp": ssp,
                "rulesFired": rf,
                "meetsGoal": rf == len(goal_rules),
                "ruleMetricValues": {
                    m: {"value": v, "text": format_val(v, metric_types[m]) if v is not None else "—"}
                    for m, v in rule_values.items()
                },
                "adGroupCount": len(ssp_ad_group_ids.get(ssp, set())),
            })
        ssp_goal_eval.sort(key=lambda x: (-x["rulesFired"], x["ssp"]))

    return {
        "meta": {
            "importFile": str(Path(args_file).name),
            "latestWeek": latest_week,
            "weeksInView": sorted_weeks,
            "goalRules": goal_rules,
            "filters": filters,
            "adGroupCount": len(groups),
            "sspCount": len({r["ssp"] for r in filtered}),
            # Trim disclosure — always present so a lite run can never be
            # mistaken for full coverage. Report these to the user verbatim.
            "lite": bool(lite),
            "metricsShown": len(metric_cols),
            "metricsTotal": len(parsed["metric_cols"]),
            "weeksShown": len(sorted_weeks),
            "weeksTotal": len(parsed["weeks"]),
            "groupsShown": len(out_groups),
            "groupsOmitted": groups_omitted,
        },
        "groups": out_groups,
        "summary": summary_out,
        "sspBreakdown": ssp_breakdown,
        "sspGoalEval": ssp_goal_eval,
        "anomalyCounts": anomaly_counts,
        "ruleFlagCellCount": rule_flag_cell_count,
    }


def cmd_analyze(args):
    result = build_analysis(args.file, args.cid_map_default, args.cid, args.rules, args.filters)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def cmd_export(args):
    result = build_analysis(args.file, args.cid_map_default, args.cid, args.rules, args.filters)
    weeks = result["meta"]["weeksInView"]
    metric_cols = sorted({m for g in result["groups"] for m in g["weeks"].get(weeks[0], {})}) if weeks else []
    # preserve metric order as returned by summary (insertion order == metric_cols order)
    metric_cols = list(result["summary"].keys())

    hdr1 = ["Ad Group ID", "Ad Group", "SSP", "Status"]
    hdr2 = ["", "", "", ""]
    for m in metric_cols:
        for wi, w in enumerate(weeks):
            hdr1.append(m if wi == 0 else "")
            hdr2.append(w)

    rows = [hdr1, hdr2]
    for g in sorted(result["groups"], key=lambda g: g["adGroupId"]):
        row = [g["adGroupId"], g["adGroupName"], g["ssp"], (g["latestStatus"] or "—").upper()]
        for m in metric_cols:
            for w in weeks:
                cell = g["weeks"].get(w, {}).get(m, {})
                row.append(cell.get("raw") if cell.get("raw") is not None else "")
        rows.append(row)

    import csv
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    print(json.dumps({"written": args.out, "rows": len(rows) - 2}))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cid-map-default", dest="cid_map_default", default=str(DEFAULT_CID_MAP_PATH))
    sub = parser.add_subparsers(dest="mode", required=True)

    p_discover = sub.add_parser("discover")
    p_discover.add_argument("file")
    p_discover.add_argument("--cid")
    p_discover.set_defaults(func=cmd_discover)

    p_health = sub.add_parser("health")
    p_health.add_argument("file")
    p_health.add_argument("--cid")
    p_health.set_defaults(func=cmd_health)

    p_full = sub.add_parser("full")
    p_full.add_argument("file")
    p_full.add_argument("--rules", default=None, help="JSON list of {metric,dir,value}, max 3")
    p_full.add_argument("--filters", default=None)
    p_full.add_argument("--cid")
    p_full.add_argument("--lite", action="store_true",
                       help="Trim the group matrix: goal + core metrics, last 2 weeks, ~15 most relevant groups")
    p_full.add_argument("--max-weeks", dest="max_weeks", type=int, default=None,
                        help="Keep only the N most recent weeks (overrides the lite default)")
    p_full.add_argument("--top-groups", dest="top_groups", type=int, default=None,
                        help="Keep only N groups: all rule-firing ones first, then biggest spenders")
    p_full.set_defaults(func=cmd_full)

    p_estimate = sub.add_parser("estimate")
    p_estimate.add_argument("file")
    p_estimate.add_argument("--cid")
    p_estimate.set_defaults(func=cmd_estimate)

    p_allocation = sub.add_parser("allocation")
    p_allocation.add_argument("file")
    p_allocation.add_argument("--target", required=True, help='JSON {"ssp": target_percent_or_fraction, ...}')
    p_allocation.add_argument("--scope", default="latest", choices=["latest", "all_time"])
    p_allocation.add_argument("--cid")
    p_allocation.set_defaults(func=cmd_allocation)

    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("file")
    p_analyze.add_argument("--rules", default=None, help="JSON list of {metric,dir,value}, max 3")
    p_analyze.add_argument("--cid")
    p_analyze.add_argument("--filters", default=None, help='JSON: {"search","ssp","status","vaMin","anomalyOnly"}')
    p_analyze.set_defaults(func=cmd_analyze)

    p_export = sub.add_parser("export")
    p_export.add_argument("file")
    p_export.add_argument("--out", required=True)
    p_export.add_argument("--rules", default=None)
    p_export.add_argument("--cid")
    p_export.add_argument("--filters", default=None)
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        json.dump({"error": str(e)}, sys.stdout)
        print()
        sys.exit(1)
