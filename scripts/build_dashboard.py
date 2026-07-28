"""Render a read-only HTML dashboard from the Radar database.

One self-contained file, no server, no login. Every opportunity and signal
links out to its source. Regenerate whenever you want a fresh view:

    python scripts/build_dashboard.py

Writes dashboard.html at the repo root by default. The script only ever
SELECTs. It never changes a row, so it is safe to run at any time.

Flags:
    --db PATH    read a different database (tests use this)
    --out PATH   write somewhere else
    --quiet      print nothing on success
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar_common

OUT_DEFAULT = radar_common.REPO_ROOT / "dashboard.html"
WATCHLIST_PATH = radar_common.REPO_ROOT / "config" / "watchlist.yaml"


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def safe_url(url) -> str | None:
    """Only http(s) URLs become links. Anything else renders as plain text."""
    if not url:
        return None
    url = str(url).strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return None


def link(url, text, fallback_plain=True) -> str:
    ok = safe_url(url)
    if ok:
        return (f'<a href="{esc(ok)}" target="_blank" '
                f'rel="noopener noreferrer">{esc(text)}</a>')
    return esc(text) if fallback_plain else ""


def fmt_when(iso) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        return dt.strftime("%d %b %Y")
    except ValueError:
        return esc(iso)


def status_chip(status) -> str:
    cls = {"new": "chip-new", "digested": "chip-digested",
           "actioned": "chip-good", "dead": "chip-dead"}.get(status, "")
    return f'<span class="chip {cls}">{esc(status or "new")}</span>'


def meter(score, threshold) -> str:
    try:
        v = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        v = 0
    hot = " hot" if v >= threshold else ""
    return (f'<div class="scorecell"><span class="scoreno{hot}">{v}</span>'
            f'<span class="meter"><span class="meter-fill{hot}" '
            f'style="width:{v}%"></span></span></div>')


def load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    import yaml
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    sources = data.get("sources") or []
    if not isinstance(sources, list):
        return []
    return [s for s in sources if isinstance(s, dict)]


def hot_row(score, threshold, status) -> bool:
    """One rule for both the stat tiles and the row tinting."""
    return (score or 0) >= threshold and status in ("new", "digested")


# Dollars per million tokens (input, output), matching docs/cost-note.md.
# Unknown or missing models price at the dearest rate so the tile can only
# ever overstate, never flatter. Cache reads bill at a tenth of input.
PRICES_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}
PRICE_FALLBACK = (3.00, 15.00)


def week_cost_usd(conn, since_iso) -> float:
    total = 0.0
    for r in conn.execute(
            """SELECT model, SUM(input_tokens) AS tin, SUM(output_tokens) AS tout,
                      SUM(cache_read_tokens) AS tcache
               FROM runs WHERE ts >= ? GROUP BY model""", (since_iso,)):
        p_in, p_out = PRICES_PER_MTOK.get(r["model"] or "", PRICE_FALLBACK)
        total += ((r["tin"] or 0) * p_in
                  + (r["tout"] or 0) * p_out
                  + (r["tcache"] or 0) * p_in * 0.1) / 1_000_000
    return total


# ------------------------------------------------------------------ sections

def collect(conn, config) -> dict:
    threshold = int(config.get("score_threshold", 70))
    fast = int(config.get("fast_signal_threshold", 75))
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")

    opportunities = conn.execute(
        "SELECT * FROM opportunities ORDER BY combined DESC, first_seen DESC"
    ).fetchall()
    signals = conn.execute(
        "SELECT * FROM signals ORDER BY relevance DESC, first_seen DESC"
    ).fetchall()
    threads = conn.execute(
        """SELECT t.* FROM touches t
           JOIN (SELECT company, MAX(touched_at) AS mt FROM touches
                 GROUP BY company) l
             ON t.company = l.company AND t.touched_at = l.mt
           JOIN (SELECT company, touched_at, MAX(id) AS mid FROM touches
                 GROUP BY company, touched_at) tie
             ON t.id = tie.mid
           WHERE t.next_action IS NOT NULL AND t.next_action != ''
           ORDER BY COALESCE(t.next_action_date, '9999') ASC""").fetchall()
    sources_state = {r["source_id"]: r for r in conn.execute(
        "SELECT * FROM source_state").fetchall()}
    runs = conn.execute(
        """SELECT workflow, mode, COUNT(*) AS n,
                  SUM(items_in) AS items_in, SUM(items_new) AS items_new,
                  SUM(input_tokens) AS tin, SUM(output_tokens) AS tout,
                  SUM(cache_read_tokens) AS tcache, MAX(ts) AS last_ts
           FROM runs WHERE ts >= ? GROUP BY workflow, mode
           ORDER BY workflow, mode""", (week_ago,)).fetchall()

    needs_review = [
        {"kind": "opportunity", "label": r["title"] or "Untitled role",
         "company": r["company"] or "", "url": r["source_url"],
         "note": r["notes"] or "scoring failed", "first_seen": r["first_seen"]}
        for r in conn.execute(
            "SELECT title, company, source_url, notes, first_seen"
            " FROM opportunities WHERE combined IS NULL ORDER BY first_seen")
    ] + [
        {"kind": "signal", "label": r["headline"] or "Signal",
         "company": r["company"] or "", "url": r["source_url"],
         "note": r["why"] or "scoring failed", "first_seen": r["first_seen"]}
        for r in conn.execute(
            "SELECT headline, company, source_url, why, first_seen"
            " FROM signals WHERE relevance IS NULL ORDER BY first_seen")
    ]

    return {
        "threshold": threshold,
        "fast": fast,
        "opportunities": opportunities,
        "signals": signals,
        "needs_review": needs_review,
        "threads": threads,
        "sources_state": sources_state,
        "watchlist": load_watchlist(),
        "runs": runs,
        "hot_inbound": sum(1 for o in opportunities if hot_row(
            o["combined"], threshold, o["status"])),
        "hot_signals": sum(1 for s in signals if hot_row(
            s["relevance"], fast, s["status"])),
        "week_cost": week_cost_usd(conn, week_ago),
    }


def render_opportunities(data) -> str:
    rows = []
    for o in data["opportunities"]:
        rows.append(f"""<tr class="{'rowhot' if hot_row(o['combined'], data['threshold'], o['status']) else ''}">
<td>{meter(o['combined'], data['threshold'])}</td>
<td class="cell-main">{link(o['source_url'], o['title'] or 'Untitled role')}
  <div class="why">{esc(o['one_line_why'])}</div></td>
<td>{esc(o['company'])}</td>
<td>{esc(o['location'])}</td>
<td>{esc(o['salary_rate'])}</td>
<td>{status_chip(o['status'])}</td>
<td class="num">{fmt_when(o['first_seen'])}</td>
<td class="num">{fmt_when(o['act_by'])}</td>
</tr>""")
    if not rows:
        return '<p class="empty">Nothing scored yet. The inbox workflow fills this table.</p>'
    return f"""<div class="tablewrap"><table>
<thead><tr><th>Score</th><th>Role</th><th>Company</th><th>Location</th>
<th>Rate</th><th>Status</th><th class="num">First seen</th><th class="num">Act by</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def render_signals(data) -> str:
    rows = []
    for s in data["signals"]:
        pushed = ('<span class="chip chip-good">pushed</span>'
                  if s["pushed"] else '<span class="chip chip-quiet">held</span>')
        rows.append(f"""<tr class="{'rowhot' if hot_row(s['relevance'], data['fast'], s['status']) else ''}">
<td>{meter(s['relevance'], data['fast'])}</td>
<td class="cell-main">{link(s['source_url'], s['headline'] or s['company'] or 'Signal')}
  <div class="why">{esc(s['why'])}</div></td>
<td>{esc(s['company'])}</td>
<td>{esc(s['playbook_step'])}</td>
<td>{pushed}</td>
<td>{status_chip(s['status'])}</td>
<td class="num">{fmt_when(s['first_seen'])}</td>
</tr>""")
    if not rows:
        return '<p class="empty">No signals yet. The watcher fills this table.</p>'
    return f"""<div class="tablewrap"><table>
<thead><tr><th>Relevance</th><th>What happened</th><th>Company</th>
<th>Playbook step</th><th>Push</th><th>Status</th><th class="num">First seen</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def render_needs_review(data) -> str:
    rows = []
    for n in data["needs_review"]:
        rows.append(f"""<tr>
<td>{esc(n['kind'])}</td>
<td class="cell-main">{link(n['url'], n['label'])}</td>
<td>{esc(n['company'])}</td>
<td>{esc(n['note'])}</td>
<td class="num">{fmt_when(n['first_seen'])}</td>
</tr>""")
    if not rows:
        return '<p class="empty">Nothing here. The scorer parsed everything it was given.</p>'
    return f"""<div class="tablewrap"><table>
<thead><tr><th>Kind</th><th>Item</th><th>Company</th><th>Stored note</th>
<th class="num">First seen</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def render_threads(data) -> str:
    rows = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for t in data["threads"]:
        due = t["next_action_date"] or ""
        overdue = due and due < today
        due_cell = (f'<span class="chip chip-serious">due {fmt_when(due)}</span>'
                    if overdue else fmt_when(due))
        rows.append(f"""<tr>
<td class="cell-main">{esc(t['company'])}</td>
<td class="num">{fmt_when(t['touched_at'])}</td>
<td>{esc(t['channel'])}</td>
<td>{esc(t['note'])}</td>
<td>{esc(t['next_action'])}</td>
<td class="num">{due_cell}</td>
</tr>""")
    if not rows:
        return ('<p class="empty">Nothing waiting. Log touches with '
                '<code>python scripts/touch.py add</code> and pending ones appear here.</p>')
    return f"""<div class="tablewrap"><table>
<thead><tr><th>Company</th><th class="num">Last touch</th><th>Channel</th>
<th>Note</th><th>Next action</th><th class="num">When</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def render_watchlist(data) -> str:
    rows = []
    for src in data["watchlist"]:
        sid = src.get("id", "")
        state = data["sources_state"].get(sid)
        last = fmt_when(state["last_checked"]) if state else "never"
        result = esc(state["last_status"]) if state and state["last_status"] else "no check yet"
        unverified = src.get("status") == "unverified"
        badge = ('<span class="chip chip-quiet">unverified</span>'
                 if unverified else "")
        interval = ("skipped" if unverified
                    else f"{esc(src.get('check_interval_hours', ''))}h")
        rows.append(f"""<tr>
<td class="cell-main">{link(src.get('url'), src.get('name', sid))} {badge}</td>
<td class="num">{esc(src.get('tier', ''))}</td>
<td>{esc(src.get('method', ''))}</td>
<td class="num">{interval}</td>
<td class="num">{last}</td>
<td>{result}</td>
</tr>""")
    if not rows:
        return '<p class="empty">config/watchlist.yaml is missing or empty.</p>'
    return f"""<div class="tablewrap"><table>
<thead><tr><th>Source</th><th class="num">Tier</th><th>Method</th>
<th class="num">Interval</th><th class="num">Last checked</th><th>Last result</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def render_runs(data) -> str:
    rows = []
    for r in data["runs"]:
        rows.append(f"""<tr>
<td class="cell-main">{esc(r['workflow'])}</td>
<td>{esc(r['mode'])}</td>
<td class="num">{r['n'] or 0}</td>
<td class="num">{r['items_in'] or 0}</td>
<td class="num">{r['items_new'] or 0}</td>
<td class="num">{(r['tin'] or 0):,}</td>
<td class="num">{(r['tout'] or 0):,}</td>
<td class="num">{(r['tcache'] or 0):,}</td>
<td class="num">{fmt_when(r['last_ts'])}</td>
</tr>""")
    if not rows:
        return '<p class="empty">No runs in the last seven days.</p>'
    return f"""<div class="tablewrap"><table>
<thead><tr><th>Workflow</th><th>Mode</th><th class="num">Runs</th>
<th class="num">Items</th><th class="num">New</th><th class="num">Tokens in</th>
<th class="num">Tokens out</th><th class="num">Cached</th><th class="num">Last run</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


# ---------------------------------------------------------------------- page

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #6e6c66;
  --hairline: #e1e0d9; --border: rgba(11,11,11,0.10);
  --accent: #2a78d6; --accent-soft: #9ec5f4; --accent-ink: #2266bd;
  --good: #006300; --serious: #b34a1f; --link: #256abf;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --ink: #ececea; --ink-2: #c3c2b7; --muted: #898781;
    --hairline: #2c2c2a; --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --accent-soft: #1c5cab; --accent-ink: #6da7ec;
    --good: #0ca30c; --serious: #ec835a; --link: #6da7ec;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 20px 60px; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
td a { text-decoration: underline; text-decoration-color: var(--accent-soft);
  text-underline-offset: 2px; }
td a:hover { text-decoration-color: var(--link); }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px;
  margin-bottom: 6px; }
header h1 { font-size: 20px; margin: 0; }
.gen { color: var(--muted); font-size: 12px; }
.quicklinks { margin: 0 0 18px; color: var(--muted); font-size: 13px; }
.quicklinks a { margin-right: 14px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin-bottom: 26px; }
.tile { background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; }
.tile .v { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
.tile .l { color: var(--ink-2); font-size: 12.5px; margin-top: 2px; }
section { margin-bottom: 30px; }
h2 { font-size: 15px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 12.5px; margin: 0 0 10px; }
.tablewrap { overflow-x: auto; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; min-width: 760px; }
th { text-align: left; font-size: 11.5px; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--muted); font-weight: 600;
  padding: 10px 12px; border-bottom: 1px solid var(--hairline); }
td { padding: 10px 12px; border-bottom: 1px solid var(--hairline);
  vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums;
  white-space: nowrap; }
.cell-main { font-weight: 550; }
.why { color: var(--ink-2); font-weight: 400; font-size: 12.5px; margin-top: 2px;
  max-width: 46ch; }
.rowhot td { background: color-mix(in srgb, var(--accent) 5%, transparent); }
.scorecell { display: flex; align-items: center; gap: 8px; min-width: 96px; }
.scoreno { font-variant-numeric: tabular-nums; width: 26px; text-align: right;
  color: var(--ink-2); }
.scoreno.hot { color: var(--ink); font-weight: 650; }
.meter { flex: 1; height: 4px; border-radius: 2px; background: var(--hairline);
  overflow: hidden; min-width: 48px; }
.meter-fill { display: block; height: 100%; border-radius: 2px;
  background: var(--accent-soft); }
.meter-fill.hot { background: var(--accent); }
.chip { display: inline-block; font-size: 11px; font-weight: 600;
  border-radius: 999px; padding: 1px 8px; border: 1px solid var(--border);
  color: var(--ink-2); white-space: nowrap; }
.chip-good { color: var(--good); border-color: var(--good); }
.chip-serious { color: var(--serious); border-color: var(--serious); }
.chip-new { color: var(--accent-ink); border-color: var(--accent-ink); }
.chip-digested, .chip-quiet, .chip-dead { }
.chip-dead { text-decoration: line-through; }
.empty { color: var(--muted); background: var(--surface);
  border: 1px dashed var(--hairline); border-radius: 10px; padding: 16px; }
footer { color: var(--muted); font-size: 12px; border-top: 1px solid var(--hairline);
  padding-top: 14px; }
code { background: var(--surface); border: 1px solid var(--hairline);
  border-radius: 4px; padding: 0 4px; font-size: 12px; }
"""


def render_page(data, config, db_label, out) -> str:
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    n8n = safe_url(config.get("n8n_url"))
    quick = []
    if n8n:
        quick.append(f'<a href="{esc(n8n)}" target="_blank" rel="noopener noreferrer">n8n workflows</a>')
    for target, label in (
            (radar_common.REPO_ROOT / "test" / "last_digest.html", "last dry-run digest"),
            (radar_common.REPO_ROOT / "test" / "last_signal.txt", "last dry-run payloads"),
            (radar_common.REPO_ROOT / "docs" / "cost-note.md", "cost note")):
        if target.exists():
            href = Path(os.path.relpath(target, out.parent)).as_posix()
            quick.append(f'<a href="{esc(href)}">{esc(label)}</a>')

    tiles = f"""
<div class="tiles">
  <div class="tile"><div class="v">{data['hot_inbound']}</div>
    <div class="l">inbound at or above {data['threshold']}</div></div>
  <div class="tile"><div class="v">{data['hot_signals']}</div>
    <div class="l">signals at or above {data['fast']}</div></div>
  <div class="tile"><div class="v">{len(data['threads'])}</div>
    <div class="l">threads awaiting action</div></div>
  <div class="tile"><div class="v">${data['week_cost']:,.2f}</div>
    <div class="l">api spend, last seven days</div></div>
</div>"""

    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MedTech Radar</title>
<style>{CSS}</style>
</head>
<body>
<main>
<header><h1>MedTech Radar</h1>
  <span class="gen">read only, generated {esc(now)} from {esc(db_label)}</span></header>
<p class="quicklinks">{' '.join(quick)}</p>
{tiles}
<section>
  <h2>Inbound</h2>
  <p class="sub">Job adverts and approaches, scored against the CV and preferences. Rows tinted blue sit at or above the digest threshold.</p>
  {render_opportunities(data)}
</section>
<section>
  <h2>Signals</h2>
  <p class="sub">Funding rounds and spin-off launches from the watchlist. At or above {data['fast']} the same-day push fires once the signals workflow is armed.</p>
  {render_signals(data)}
</section>
<section>
  <h2>Needs review</h2>
  <p class="sub">Rows the scorer could not score. Clear them with python scripts/rescore.py once the cause is fixed.</p>
  {render_needs_review(data)}
</section>
<section>
  <h2>Threads awaiting action</h2>
  <p class="sub">The latest touch per company with a next step still open, from the tracker.</p>
  {render_threads(data)}
</section>
<section>
  <h2>Watchlist health</h2>
  <p class="sub">Every fetched source, when it was last checked and what came back. Unverified sources are skipped on scheduled runs. Email-borne alerts arrive through the inbox workflow instead.</p>
  {render_watchlist(data)}
</section>
<section>
  <h2>Pipeline, last seven days</h2>
  <p class="sub">Runs and token spend from the runs table. docs/cost-note.md explains the arithmetic.</p>
  {render_runs(data)}
</section>
<footer>Read only. Nothing on this page changes the database. Regenerate with
<code>python scripts/build_dashboard.py</code>.</footer>
</main>
</body>
</html>"""


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Render the read-only Radar dashboard.")
    parser.add_argument("--db", help="database path override")
    parser.add_argument("--out", help="output path, default dashboard.html at repo root")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    radar_common.load_env()
    config = radar_common.load_config()
    db_path = Path(args.db).resolve() if args.db else radar_common.DB_PATH

    if not db_path.exists():
        if args.db:
            print(f"No database at {db_path}. Check the --db path.", file=sys.stderr)
            return 2
        # First run on a fresh clone. Create the empty schema once, then read it.
        radar_common.get_db(db_path).close()

    # Open read only so this script can never change a row, whatever happens.
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    data = collect(conn, config)
    conn.close()

    try:
        db_label = db_path.relative_to(radar_common.REPO_ROOT).as_posix()
    except ValueError:
        db_label = str(db_path)

    out = Path(args.out).resolve() if args.out else OUT_DEFAULT
    out.write_text(render_page(data, config, db_label, out), encoding="utf-8")
    if not args.quiet:
        print(json.dumps({"out": str(out),
                          "opportunities": len(data["opportunities"]),
                          "signals": len(data["signals"]),
                          "threads": len(data["threads"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
