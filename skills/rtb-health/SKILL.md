---
name: rtb-health
description: Analyze a weekly RTB campaign export (xlsx/csv) — ask the user's optimization goal upfront alongside the file, then produce a shareable HTML campaign-health report (goal-oriented health check, by-SSP then by-SSP+Ad-Group goal analysis, diagnosis covering win rate/cost/actions/SSP/anomalies). Use when the user attaches an RTB weekly export file, asks to check "campaign health", or invokes /rtb-health. Distinct from /rtb (which fetches live data from the Appier dashboard via API token).
---

# RTB Health — campaign health check + goal-driven analysis from a weekly export

Combines two things in one interactive skill: z-score anomaly detection,
chained goal-rule flagging, WoW deltas and per-metric summaries, plus an
All-Time / Latest-Week campaign health scan. You do the parsing (via
`scripts/analyze.py`), write the qualitative diagnosis yourself (no pasted API
key, no external call — you're already the model doing the analysis), and
render the result as a shareable HTML report (via `scripts/render_report.py`)
rather than typing tables into chat — this is the standard output, not a
one-off demo format.

**About `$SKILL_DIR` in the commands below**: substitute this skill's own base
directory — Claude Code reports it when the skill is invoked ("Base directory
for this skill: …"). It is NOT an exported shell variable, so don't rely on
the shell to expand it; paste the real path into the command. This indirection
exists so the skill works both from `~/.claude/skills/rtb-health/` and from
wherever a plugin install puts it — never hardcode either location.

## Flow

### 1. Get the file and the goal — together, upfront
Ask for both in the same message, not as two separate back-and-forths:

> "把这周的 RTB 导出文件传给我(xlsx/csv,可选附带 CID Overview),同时告诉我这次想看的目标——比如 CPA < $5、ROAS 7D > 40%、Valid Action >= 5。最多可以设 3 条链式条件(第二条只在第一条命中的组里继续筛)。"

If the user already gave you one half (file or goal) before you asked, don't
re-ask for it — just fill in whichever's missing.

Once the file is in hand, run `discover` to get this file's real metric column
names and reconcile them against whatever the user said their goal is:
```bash
python3 "$SKILL_DIR"/scripts/analyze.py discover <file> [--cid <cid_file>]
```
If their phrasing is ambiguous about which exact column they mean (e.g. this
file has both `ROAS 0D / $3` and `ROAS 7D / $3`), ask which one(s) rather than
guessing — getting the metric wrong wastes the whole downstream analysis.

Rules format: up to 3, each `{"metric": <exact column name>, "dir": "below"|"above", "value": <number>}`.
Chain semantics (ported exactly from the html, do not simplify):
- Rule 1 flags matching cells across **all weeks** for **all groups**.
- Rule 2+ only applies to groups whose **latest week** value already satisfied
  all prior rules.
- `rulesFired` counts how many rules fire *consecutively from rule 1* (stops
  at the first non-firing rule).

Also ask, but don't force, optional narrowing filters (SSP, status, VA floor,
anomalies-only) — map straight to `--filters`.

### 2. Health check + analyze — one combined call
Now that you know both the file and the goal, run **`full`**, not `health` and
`analyze` separately — it parses the file once and returns both, which is
noticeably faster than two round trips (each separate CLI call re-parses the
whole file and pays its own Python startup cost):
```bash
python3 "$SKILL_DIR"/scripts/analyze.py full <file> \
  --rules '[{"metric":"CPA","dir":"below","value":5}]' \
  --filters '{}' \
  [--cid <cid_file>]
```
Returns `{"health": {...}, "analysis": {...}}`. This step covers the health
check (`result.health`); step 3 below uses `result.analysis` from this same
call — don't re-run `analyze` separately.

`result.health` carries two scans:
- **All Time**: total valid action, aggregate CPA (`total cost / total valid
  action` — volume-weighted, not an average of per-row CPAs), total invalid
  action, top-3 SSPs by cost share, top-3 SSPs by action share, and a
  breakdown by the ad group name's leading `[bracket]` tag (e.g.
  `[D][CCPA]...` → `D`). That leading tag reflects whatever convention this
  advertiser's ad group names use (targeting/compliance flags, creative
  format, etc.) — if it doesn't read as meaningful creative-type grouping for
  this file, say so plainly rather than forcing an interpretation.
- **Latest Week** (vs. prior week): total valid action + WoW%, aggregate CPA +
  WoW **absolute dollar delta** (not %, per the original formula), invalid
  action + WoW%, creative breakdown, top-5 SSPs by cost/action with their own
  WoW%, and MMP CVR (`valid action / MMP total click`) + WoW% if that column
  exists in this file.

The response also carries a few top-level sections beyond All Time/Latest
Week, added because real AM/CM Slack conversations across 5 campaign channels
consistently centered on these — include whichever are non-null for this
file, and say plainly when one is missing (don't fabricate it):
- **`roasWindows`** — every ROAS-typed column in the file (D0/D7/whatever
  windows exist) shown side by side with its own WoW%, not just one. AMs
  track these simultaneously, not as a single number.
- **`ctcvVtcv`** — CTCV rate / VTCV rate columns, distinct from generic CVR —
  these are a separate optimization lens in practice (click-through vs.
  view-through attributed conversions).
- **`cpi`** — if the file has a distinct CPI column (cost-per-install, not
  CPA), show it with the same avg + WoW-dollar-delta treatment as aggregate
  CPA. In mobile-game verticals this is often the *primary* bid lever, ahead
  of CPA.
- **`allTime.sspEfficiency` / `latestWeek.sspEfficiency`** — Win Rate + CPM
  per SSP (for the same top SSPs already listed by cost/action), **volume-
  weighted** (total Win / total Bid, total Cost / total Impression×1000) when
  `Bid`/`Win`/`Impression` columns exist — NOT a naive average of each row's
  own ratio value. A naive average of ratios is a real trap here: verified on
  a real file where it overstated one SSP's win rate by >3x versus the true
  weighted rate, enough to flip which SSP looked best. Only falls back to
  averaging the ratio column directly if the file lacks raw Bid/Win/Impression
  counts — say so if that fallback is in effect, since it's the less reliable
  path. Use this to distinguish "no supply anywhere" (both low) from "being
  outbid" (win rate low, CPM normal) from "structurally cheap/expensive
  inventory" (CPM itself is the outlier) — plain cost/action share can't tell
  these apart, but AMs make exactly this distinction constantly.
- **`allTime.listBreakdown` / `latestWeek.listBreakdown`** — best-effort
  extraction of SK tier / PM list / Fix PM list / IDFA / IDFV / whitelist tags
  (and combinations, e.g. `SK 6-8 + WL`) from `ad_group_name` text.
  **Explicitly heuristic, say so when presenting it**: every optimizer/AM
  names ad groups differently, so this only catches what happens to be
  spelled out in the name — an ad group using a different naming convention
  silently lands in `"No list tag detected"` even if it's genuinely running
  one of these lists. Treat it as a rough signal worth a sanity-check
  mention, never as a confirmed ground-truth breakdown.
- **`latestWeek.structureChanges`** — ad_group_id+SSP combos that newly
  appeared (`added`) or disappeared (`removed`) vs. the prior week. This
  answers one specific branch of a common AM/CM question — "is this WoW swing
  because our own structure changed?" — automatically; it can't answer the
  other two real branches (a client-side/external shift, or an internal
  policy/target change), those aren't in the file and have to be asked
  directly if a swing doesn't line up with a structure change.

This is working data for the report generated in step 5 — don't dump it into
chat. Standard output is the HTML report; chat gets a short summary (step 6).

### 3. Analyze
No new command here — use `result.analysis` from the `full` call in step 2.
It carries `sspGoalEval` (each SSP's own aggregate value for the rule
metric(s) — avg for pct/roas/dollar/float, sum for int, same convention as
the summary row, **except CPA/CPI-like metrics which are volume-weighted**,
see Notes — and whether that SSP-level aggregate meets the chained goal),
plus the existing per-group (`groups`), per-metric `summary`, `sspBreakdown`,
and `anomalyCounts`/`ruleFlagCellCount`. Don't print this raw JSON — it's
working data for the next two steps.

(If the user changes the goal/filters later in step 7 and asks to rerun, that
rerun uses `full` again, same as step 2 — only the very first pass benefits
from being described as its own numbered step here.)

### 4. Investigate and write the diagnosis
The HTML report (step 5) mechanically generates the health-check panels and
the by-SSP → by-SSP+AdGroup goal tables straight from the JSON —
`render_report.py` already ranks `sspGoalEval` by `rulesFired` then by
closeness to the goal, then drills into ad groups belonging to any SSP that
fired at least one rule, identified by full `adGroupName`/`canonicalName`
(never a truncated ID). **You don't need to build those tables yourself
anymore.** What the script *can't* do is the qualitative diagnosis — that's
still your job, same investigative bar as before, just a different output
shape: instead of markdown prose in chat, write a JSON list of findings for
the report, each `{"severity": "critical"|"warn"|"good"|"neutral", "label": "<short label>", "text": "<one dense sentence, plain text or simple <b> for emphasis>"}`.

Using `sspGoalEval`, `summary`, `sspBreakdown`, `anomalyCounts`, and the
health-check output from step 2 (including `roasWindows`, `ctcvVtcv`, `cpi`,
`sspEfficiency`), scan across these angles, but **only write down the ones
with an actual finding**:
- **Overall health** vs. the All Time / Latest Week baseline. If there's a big
  WoW swing, check `structureChanges` first — if a chunk of `added`/`removed`
  ad groups lines up with the swing, that's a structural cause you can state
  outright; if it doesn't, say the swing isn't explained by structure and is
  either an external (client-side) shift or an internal policy/target change
  — ask the user which, don't guess.
- **Win rate & bid efficiency**, if a relevant metric exists — use
  `sspEfficiency` to say *why* an SSP is underperforming (no supply vs.
  outbid vs. structurally priced), not just that it is.
- **Cost & CPM trends** — spend concentration, CPA/CPI movement.
- **Valid actions & CPA/ROAS** — volume trend, goal hit rate; if
  `roasWindows` has multiple windows, note when they disagree (e.g. D0 up but
  D7 down) rather than only quoting whichever window the user's goal used.
- **SSP breakdown** — SSPs carrying the goal vs. riding thin samples
  (cross-reference `sspGoalEval`'s `adGroupCount`).
- **Anomalies & risks** — anything z-score-flagged worth a second look, and
  any group with `nameHistoryChanged: true` in `groups` — its `namesInPeriod`
  means the ad group was renamed/retargeted mid-window in the platform (same
  `adGroupId`, so history is still correctly grouped), so a WoW/trend read on
  it may be crossing a targeting change, not a pure performance shift.

Hard rules, this is the part most likely to go wrong:
- **One line per finding card, not a paragraph.** State the number and the
  implication in the same short sentence. No throat-clearing ("从大盘看…",
  "值得注意的是…", "需要留意的是…") — just say the thing.
- **Never restate a number that's already visible in a table/panel in the
  report.** Only write a finding if it adds a NEW conclusion from the data.
- **Skip angles with nothing real to say — silently, no "本次没有指定X" filler
  card.** A 2-card diagnosis is fine if that's all the data supports; there's
  no minimum card count to hit.
- Small sample sizes (low Valid Action driving a flattering ratio) are a
  recurring failure mode here — flag it in one card when it applies, don't
  build a whole paragraph around it.
- `severity` drives the card's color stripe in the report — `critical` for
  things actively going wrong, `warn` for caution/needs-a-look, `good` for a
  genuine opportunity or a confirmed-benign explanation, `neutral` for
  data-gap/FYI notes that aren't good or bad.

**Terse ≠ shallow.** Cutting filler words is not the same as cutting
investigation — don't let brevity become an excuse to stop at the first
number you see. Before writing each finding, actually dig one level deeper:
- Pull the **multi-week history** for anything you're about to call out (not
  just latest-week vs. prior-week) — a metric that's been swinging wildly for
  8 weeks is a different finding than one that just moved once. `summary` and
  a group's `weeks` map both have the full week range, not just the latest.
- For SSPs that **almost** met the goal, actually look at why (which specific
  ad group is dragging the aggregate down, is it one bad week or consistent)
  instead of just listing the near-miss number.
- If a matched result looks suspicious (thin sample, one-off spike), say
  *what you checked* to confirm it, not just the conclusion — e.g. "过去8周
  这个组的 ROAS7D 在 2%–123% 之间跳,这周 88% 只是又一次波动" is a finding;
  "样本量小,数据可能不稳定" alone is filler dressed as insight.
Each finding should be one dense, specific sentence — not a padded paragraph,
but not a bare number either. Length is not the target; investigation depth
is. A short diagnosis is fine ONLY if that depth genuinely turned up little.

### 5. Render and publish the report
Write your findings list to a small JSON file, then generate the report:
```bash
python3 "$SKILL_DIR"/scripts/render_report.py \
  --data <full_output.json> --diagnosis <findings.json> \
  --out <report.html> [--title "<campaign name>"]
```
`--data` is the raw JSON you got from the `full` call in step 2 — save its
stdout to a file first if you haven't already, this command reads it back
from disk. `--title` is optional; without it the script guesses the campaign
name from ad group naming (text before `_AIBID_`) or the filename — override
it if that guess looks wrong. Then publish the resulting HTML with the
Artifact tool so the user gets a shareable link.

### 6. Chat summary — short, not a re-statement
The report carries the full detail now — don't re-type its tables into chat.
Give the user a few sentences: the single most important finding (usually
whatever's `critical` or the top `good` opportunity), then the report link.
This replaces what used to be a long markdown write-up in chat.

### 7. Offer to refine or export
Invite the user to adjust the goal/filters and rerun steps 2-6 (re-running
`full` and `render_report.py` regenerates the report with the new cut). For a
CSV of the current view instead:
```bash
python3 "$SKILL_DIR"/scripts/analyze.py export <file> --out <path>.csv \
  --rules '<same rules json>' --filters '<same filters json>' [--cid <cid_file>]
```

### 8. Optional: target-allocation check
Only if the user gives you a target cost-share plan (e.g. "google 20%, smaato
30%, rubicon 10%..." — this is never derivable from the file, it has to come
from them):
```bash
python3 "$SKILL_DIR"/scripts/analyze.py allocation <file> \
  --target '{"google":20,"smaato":30,"rubicon":10}' --scope latest [--cid <cid_file>]
```
`--scope` is `latest` (default) or `all_time`. Values can be given as
percentages (20) or fractions (0.2) — either works. The output lists **every**
SSP with any spend, including near-zero ones with no target — **filter this
down yourself before presenting**: show the SSPs the user gave a target for
(sorted by `deviationPct` magnitude, biggest miss first) plus any SSP that's
soaking up real spend (say, >2-3% of cost) without a target at all, since
that's untracked leakage worth flagging. Don't dump all 50+ rows.

## Notes

- **CID mapping is optional and not bundled.** If the user attaches a "CID
  Overview" file (any sheet with an Ad Group ID column and an Ad Group Name
  column), pass it via `--cid` and the skill will use it to show canonical ad
  group names. Without it, ad group names come straight from the export
  itself — everything still works, `canonicalName` just equals `adGroupName`.
  You can also keep a local default at `assets/cid_map_default.json` (a JSON
  object of `{"<ad_group_id>": ["<canonical name>"]}`); it's picked up
  automatically when present, and its absence is not an error.
- Anomaly detection needs ≥3 weeks of history per ad-group+SSP group and is
  computed over the **entire file**, unaffected by filters/goal rules — a
  property of the data, not the current view.
- Column detection for the health check (`Valid Action`/`Raw Cost`/`Invalid
  Action`/`MMP Total Click`/`Win Rate`/`CPM`/`CPI`) is fuzzy-matched; if a
  column isn't found, that metric/section is simply omitted — say so, don't
  fabricate a number. `CPI` was also added to `detect_metric_type`'s dollar
  bucket (the original html/formulas doc didn't have it) so it formats as `$`
  everywhere, not just in the health check.
- **By design, not volume-weighted**: `summary`, `sspBreakdown`, `roasWindows`,
  and `ctcvVtcv` average each row's own ratio value directly (matches the
  original html's convention) rather than reconstructing a weighted average
  from raw numerator/denominator columns. This was deliberately left as-is
  (confirmed with the user 2026-07-29) — unlike Win Rate (Bid/Win), CPM
  (Cost/Impression), and CPA (Cost/Valid Action), metrics like ROAS, CTCV
  rate, and VTCV rate don't have separate raw numerator/denominator columns in
  typical exports (no separate "revenue" or "conversion count" column feeding
  ROAS, for instance) — there's nothing to reconstruct a weighted average
  from. If the ad groups/weeks behind one of these numbers have wildly
  different volume, still worth saying so in the diagnosis, but don't attempt
  to "fix" the aggregation without an actual raw column to weight by.
- **`sspGoalEval` IS volume-weighted for CPA/CPI-like goal metrics** (any rule
  metric name containing "cpa"/"cpi", reconstructed as ΣCost/ΣVA), even though
  the sections above aren't — found on a real file where the naive per-row
  average overstated the dominant SSP's CPA by >2x ($15.02 naive vs $6.32
  true) purely because a couple of its low-volume ad groups had extreme
  individual ratios. Any other goal metric (ROAS, CVR, a custom rate column)
  still uses the plain per-row average for the same no-raw-columns-to-
  reconstruct-from reason as above.
- Ad groups are identified by `ad_group_id` (stable) for all history/anomaly
  grouping; `adGroupName`/`canonicalName` always reflect the **latest** week's
  name for display. If the name changed mid-period, `nameHistoryChanged` is
  `true` and `namesInPeriod` lists what was seen — this doesn't break the
  grouping (ID never changes), it's just a flag that a trend read may be
  crossing a targeting/rename event.
- This skill only ever reads the export file it's given. If your org also runs
  a separate alerting/dashboard system with its own KPI thresholds, the goal
  rules here are independent and session-defined — don't assume the two agree,
  and don't present this skill's verdicts as those of another system.
- The HTML report (`render_report.py`) derives everything mechanical straight
  from the `full` JSON — KPIs, ROAS windows, list/SK breakdown, structure
  changes, the goal table, and the ad-group drill-down with sparklines. It
  cannot write the diagnosis cards itself; those always come from you, passed
  in via `--diagnosis`. If `--diagnosis` is omitted the report simply skips
  that panel — don't treat a missing diagnosis panel as a script bug.
- `render_report.py`'s campaign-name guess (from ad group naming or the
  filename) is a heuristic — pass `--title` explicitly if it guesses wrong.
