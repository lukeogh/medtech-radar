"""Render the read-only Radar dashboard from the database.

Implements the First Light design system's dashboard kit
(ui_kits/medtech-radar-dashboard in the design project). The archive.
Everything the machine has on file, read on purpose rather than out of
habit. One self-contained file, no server, no external requests.

    python scripts/build_dashboard.py

Design decisions carried over from the kit, in brief. No score meters, no
stat tiles, no pipeline table, no tinted hot rows. A standing line orients,
a heartbeat carries the machinery facts at footer weight, and each section
sits on its own soft-tinted paper. Rows share one grammar, one line at
rest, detail on expand. A fresh database renders the honest as-delivered
state rather than pretending to be a quiet week. Unscored rows sit in
their home sections wearing a review chip instead of hiding in a separate
list.

The script only ever SELECTs. Flags: --db PATH, --out PATH, --quiet.
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
AGEING_DAYS = 14

_UNITS = ("no", "one", "two", "three", "four", "five", "six", "seven",
          "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
          "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
         60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety"}


def words(n: int) -> str:
    """Small numbers in words, the way the page speaks."""
    if 0 <= n < 20:
        return _UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        base = _TENS[tens * 10]
        return base if unit == 0 else f"{base}-{_UNITS[unit]}"
    return str(n)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def safe_url(url) -> str | None:
    if not url:
        return None
    url = str(url).strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return None


def fmt_day(iso) -> str:
    """27 Jul, the column date."""
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%d %b").lstrip("0")
    except ValueError:
        return esc(iso)


def fmt_day_time(iso) -> str:
    """29 Jul 05:30, the watchlist date."""
    try:
        parsed = datetime.strptime(str(iso)[:16], "%Y-%m-%dT%H:%M")
        return parsed.strftime("%d %b %H:%M").lstrip("0")
    except ValueError:
        return fmt_day(iso)


def fmt_long(iso) -> str:
    """29 July 2026 at 06:12, the masthead date."""
    try:
        parsed = datetime.strptime(str(iso)[:16], "%Y-%m-%dT%H:%M")
        return f"{parsed.day} {parsed.strftime('%B %Y at %H:%M')}"
    except ValueError:
        return esc(iso)


def sentence(text: str) -> str:
    text = (text or "").strip()
    if text and text[-1] not in ".?":
        text += "."
    return text


SOURCE_NAMES = {
    "linkedin-alert": "LinkedIn alert", "reed-alert": "Reed alert",
    "indeed-alert": "Indeed alert", "cvlibrary-alert": "CV-Library alert",
    "email-other": "email alert",
}


def via(source) -> str:
    return SOURCE_NAMES.get(source or "", source or "unknown source")


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


# Dollars per million tokens (input, output), matching docs/cost-note.md.
PRICES_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}
PRICE_FALLBACK = (3.00, 15.00)


def week_usage(conn, since_iso) -> tuple[int, float]:
    """(total tokens, dollars) across the last week, priced per run model."""
    tokens = 0
    cost = 0.0
    for r in conn.execute(
            """SELECT model, SUM(input_tokens) AS tin, SUM(output_tokens) AS tout,
                      SUM(cache_read_tokens) AS tcache
               FROM runs WHERE ts >= ? GROUP BY model""", (since_iso,)):
        p_in, p_out = PRICES_PER_MTOK.get(r["model"] or "", PRICE_FALLBACK)
        tin, tout, tcache = (r["tin"] or 0), (r["tout"] or 0), (r["tcache"] or 0)
        tokens += tin + tout + tcache
        cost += (tin * p_in + tout * p_out + tcache * p_in * 0.1) / 1_000_000
    return tokens, cost


# ------------------------------------------------------------------ collect

def collect(conn, config) -> dict:
    threshold = int(config.get("score_threshold", 70))
    fast = int(config.get("fast_signal_threshold", 75))
    # The floor drives the rate legend. A dashboard is a reader, so a
    # missing floor line degrades to an honest legend note here while the
    # pipeline itself fails loudly at the same gap.
    try:
        floor = radar_common.read_rate_floor(config)
    except RuntimeError:
        floor = None
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    age_cutoff = (now - timedelta(days=AGEING_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_opportunities = [dict(r) for r in conn.execute(
        "SELECT * FROM opportunities"
        " ORDER BY (combined IS NULL), combined DESC, first_seen DESC")]
    # A human said "seen it". Acknowledged rows leave the default view but
    # stay on the page behind the toggle, with an undo, and stay in the
    # database so dedupe keeps rejecting their URLs forever.
    opportunities = [o for o in all_opportunities
                     if not o.get("acknowledged_at")]
    acknowledged = [o for o in all_opportunities if o.get("acknowledged_at")]
    all_signals = [dict(r) for r in conn.execute(
        "SELECT * FROM signals"
        " ORDER BY (relevance IS NULL), relevance DESC, first_seen DESC")]
    # Dismissed insights follow the jobs rule. Out of sight, never out of
    # the database, so the URL-hash dedupe keeps rejecting them.
    signals = [s for s in all_signals if not s.get("acknowledged_at")]
    dismissed_signals = [s for s in all_signals if s.get("acknowledged_at")]
    # The latest touch per company, whatever the channel, so a story about
    # a company Luke already spoke to says so on its card.
    touch_map = {r["company"].lower(): dict(r) for r in conn.execute(
        """SELECT t.company, t.touched_at, t.channel, t.next_action,
                  t.next_action_date FROM touches t
           JOIN (SELECT company, MAX(id) AS mid FROM touches
                 GROUP BY company) l ON t.id = l.mid""")}
    threads = [dict(r) for r in conn.execute(
        """SELECT t.* FROM touches t
           JOIN (SELECT company, MAX(touched_at) AS mt FROM touches
                 GROUP BY company) l
             ON t.company = l.company AND t.touched_at = l.mt
           JOIN (SELECT company, touched_at, MAX(id) AS mid FROM touches
                 GROUP BY company, touched_at) tie
             ON t.id = tie.mid
           WHERE t.next_action IS NOT NULL AND TRIM(t.next_action) <> ''
           ORDER BY (t.next_action_date IS NULL), t.next_action_date""")]

    def is_ageing_opp(o) -> bool:
        return (not o.get("acknowledged_at")
                and o["status"] in ("new", "digested")
                and (o["combined"] or 0) >= threshold
                and (o["status_changed_at"] or o["first_seen"] or "9999") <= age_cutoff)

    def is_ageing_sig(s) -> bool:
        return (not s.get("acknowledged_at")
                and s["status"] in ("new", "digested")
                and (s["relevance"] or 0) >= threshold
                and (s["first_seen"] or "9999") <= age_cutoff)

    ageing = ([{**o, "kind": "opportunity"} for o in opportunities if is_ageing_opp(o)]
              + [{**s, "kind": "signal"} for s in signals if is_ageing_sig(s)])
    ageing.sort(key=lambda a: a.get("status_changed_at") or a.get("first_seen") or "")

    sources_state = {r["source_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM source_state")}
    watchlist = load_watchlist()

    run_row = conn.execute(
        "SELECT MAX(CASE WHEN workflow = 'inbox' THEN ts END) AS inbox_ts,"
        " MAX(CASE WHEN workflow = 'signals' THEN ts END) AS signals_ts,"
        " COUNT(*) AS any_runs FROM runs").fetchone()
    week = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN workflow = 'inbox' THEN 1 END), 0) AS runs,"
        " COALESCE(SUM(CASE WHEN workflow = 'inbox' THEN items_in END), 0) AS seen,"
        " COALESCE(SUM(CASE WHEN workflow = 'inbox' THEN items_new END), 0) AS new"
        " FROM runs WHERE ts >= ?", (week_ago,)).fetchone()
    failed_emails = conn.execute(
        "SELECT COUNT(*) FROM email_attempts WHERE attempts >= 3"
        " AND last_attempt >= ?", (week_ago,)).fetchone()[0]
    tokens, cost = week_usage(conn, week_ago)

    # Arrivals per day across a fortnight, the Jobs page's flow chart.
    # Acknowledged rows still count, an arrival is an arrival.
    per_day = {}
    for o in all_opportunities:
        day = (o.get("first_seen") or "")[:10]
        if day:
            per_day[day] = per_day.get(day, 0) + 1
    daily_arrivals = []
    for back in range(13, -1, -1):
        day = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        daily_arrivals.append((day, per_day.get(day, 0)))

    # Week buckets for the Home trends. Top prospects meet the digest bar,
    # the market metric Luke reads week to week. Signal activity charts the
    # year's rhythm, dismissed included, activity is activity.
    def week_bucket(iso: str) -> str | None:
        try:
            d = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
        monday = d - timedelta(days=d.weekday())
        return monday.strftime("%Y-%m-%d")

    prospects_by_week: dict[str, int] = {}
    for o in all_opportunities:
        if (o.get("combined") or 0) >= threshold:
            wk = week_bucket(o.get("first_seen"))
            if wk:
                prospects_by_week[wk] = prospects_by_week.get(wk, 0) + 1
    # A rolling year of insight activity, months as buckets, and only the
    # signals that cleared the digest bar. Relevance is the rubric's own
    # judgement of "a medtech event that could need Luke's input", so the
    # trend charts the market that matters, not ecosystem noise. Months
    # before the earliest signal on file are None, not zero, the radar was
    # not listening yet and a gap must never read as a quiet market.
    signals_by_month: dict[str, int] = {}
    earliest_signal = None
    for s in all_signals:
        seen = (s.get("first_seen") or "")[:7]
        if not seen:
            continue
        if earliest_signal is None or seen < earliest_signal:
            earliest_signal = seen
        if (s.get("relevance") or 0) >= threshold:
            signals_by_month[seen] = signals_by_month.get(seen, 0) + 1

    monthly_signals: list[tuple[str, int | None]] = []
    cursor = now.replace(day=1)
    months = []
    for _ in range(12):
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    for month in reversed(months):
        if earliest_signal is None or month < earliest_signal:
            monthly_signals.append((month, None))
        else:
            monthly_signals.append((month, signals_by_month.get(month, 0)))

    this_monday = now - timedelta(days=now.weekday())
    weekly_prospects = []
    for back in range(7, -1, -1):
        wk = (this_monday - timedelta(weeks=back)).strftime("%Y-%m-%d")
        weekly_prospects.append((wk, prospects_by_week.get(wk, 0)))

    month_now = now.strftime("%Y-%m")
    month_prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    month_prospects = {"this": 0, "last": 0}
    for o in all_opportunities:
        if (o.get("combined") or 0) >= threshold:
            month = (o.get("first_seen") or "")[:7]
            if month == month_now:
                month_prospects["this"] += 1
            elif month == month_prev:
                month_prospects["last"] += 1

    spark = []
    for back in (3, 2, 1, 0):
        start = (now - timedelta(days=7 * (back + 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now - timedelta(days=7 * back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = conn.execute(
            "SELECT COALESCE(SUM(items_new), 0) FROM runs"
            " WHERE ts >= ? AND ts < ?", (start, end)).fetchone()[0]
        spark.append(int(count))

    checked = [s for s in watchlist if sources_state.get(s.get("id"))
               and sources_state[s.get("id")].get("last_checked")]
    answering = [s for s in checked
                 if (sources_state[s["id"]].get("last_status") or "")
                 in ("200", "304")]

    fresh = (run_row["any_runs"] == 0 and not all_opportunities
             and not signals and not threads)

    return {
        "threshold": threshold, "fast": fast, "floor": floor, "fresh": fresh,
        "opportunities": opportunities, "acknowledged": acknowledged,
        "signals": signals, "dismissed_signals": dismissed_signals,
        "touch_map": touch_map,
        "threads": threads, "ageing": ageing,
        "ageing_opp_ids": {a["id"] for a in ageing if a["kind"] == "opportunity"},
        "watchlist": watchlist, "sources_state": sources_state,
        "inbox_ts": run_row["inbox_ts"], "signals_ts": run_row["signals_ts"],
        "week_runs": week["runs"], "week_seen": week["seen"],
        "week_new": week["new"], "failed_emails": failed_emails,
        "daily_arrivals": daily_arrivals,
        "weekly_prospects": weekly_prospects,
        "monthly_signals": monthly_signals,
        "month_prospects": month_prospects,
        "tokens": tokens, "cost": cost, "spark": spark,
        "checked": len(checked), "answering": len(answering),
    }


# ------------------------------------------------------------------- pieces

def status_chip(status) -> str:
    cls = {"new": " chip-new", "actioned": " chip-actioned",
           "dead": " chip-dead"}.get(status, "")
    return f'<span class="chip{cls}">{esc(status or "new")}</span>'


def row_open(detail_id: str, extra_class: str = "") -> str:
    cls = f"row{' ' + extra_class if extra_class else ''}"
    return (f'<div class="{cls}"><button type="button" class="row-summary"'
            f' aria-expanded="false" aria-controls="{detail_id}">'
            f'<span class="caret" aria-hidden="true"></span>')


def detail(parts: list[str], detail_id: str) -> str:
    inner = "".join(f"<div>{p}</div>" for p in parts if p)
    return f'</button><div class="row-detail" id="{detail_id}" hidden>{inner}</div></div>'


def labelled(label: str, body: str) -> str:
    return (f'<span class="detail-label">{esc(label)}</span>'
            f'<span class="detail-body">{esc(body)}</span>')


def source_link(url, text: str) -> str:
    ok = safe_url(url)
    if not ok:
        return ""
    return (f'<a href="{esc(ok)}" target="_blank" rel="noopener noreferrer">'
            f'{esc(text)}</a>')


RATE_WORDS = {"above": "Above", "close": "Close", "below": "Below",
              "unstated": "Unstated"}


def rate_cell(o: dict) -> str:
    band = (o.get("rate_band") or "unstated").lower()
    word = RATE_WORDS.get(band, "Unstated")
    return f'<span class="col-rate rate-{esc(band)}">{esc(word)}</span>'


def rate_detail(o: dict) -> str:
    """The arithmetic behind the band, spelled out in the row detail.

    Code-computed pay display lives here on purpose. The ban on pay words
    covers model-written free text, the column and this line are exactly
    where pay now belongs.
    """
    verbatim = (o.get("salary_rate") or "").strip()
    rate = o.get("day_rate")
    if rate is not None:
        converted = f"About £{round(rate):,} a day converted."
        return f"{verbatim} {converted}".strip() if verbatim else converted
    return verbatim or "No usable figure stated."


def render_opportunity(o: dict, data: dict, i: int, acked: bool = False) -> str:
    did = f"d-o{i}"
    review = o["combined"] is None
    meta_bits = [b for b in (o.get("company"), o.get("location"),
                             o.get("salary_rate")) if b]
    meta = ". ".join(str(b) for b in meta_bits)
    why = o.get("one_line_why") or (
        "The scorer could not score this one, so a human look is due."
        if review else "")
    out = [row_open(did, "row-acked" if acked else ""),
           '<span class="row-main"><span class="row-head">',
           f'<span class="row-title">{esc(o.get("title") or "Untitled role")}</span>']
    if meta:
        out.append(f'<span class="row-meta">{esc(meta)}</span>')
    out.append("</span>")
    if why:
        out.append(f'<span class="row-why">{esc(sentence(why))}</span>')
    out.append("</span>")

    # The buying window is a fact about our own history, so it shows on both
    # branches. An advert that could not be scored still tells us the company
    # is hiring, and that is the whole of the signal.
    window_chip = ('<span class="chip">buying window</span>'
                   if o.get("buying_window") else "")
    if review:
        out.append('<span class="col-score">&ndash;</span>')
        out.append(rate_cell(o))
        out.append(window_chip)
        if not acked:
            out.append('<span class="chip chip-fail">review</span>')
    else:
        hot = " col-score-hot" if (o["combined"] or 0) >= data["threshold"] else ""
        out.append(f'<span class="col-cv">{o["cv_match"]}</span>'
                   f'<span class="col-want">{o["want_match"]}</span>'
                   f'<span class="col-score{hot}">{o["combined"]}</span>')
        out.append(rate_cell(o))
        out.append(window_chip)
    if acked:
        out.append(f'<span class="chip">seen {fmt_day(o.get("acknowledged_at"))}</span>')
    out.append(f'<span class="col-when">{fmt_day(o.get("first_seen"))}</span>')

    parts = [labelled("Rate", rate_detail(o))]
    if o.get("buying_window"):
        parts.append(labelled(
            "Buying window",
            "This company is already in the touch log, so the advert is the "
            "playbook's buying-window moment whatever the job score says. "
            "The insight, and the move to make, wait on the Insights page."))
    flags = []
    try:
        flags = [str(f) for f in json.loads(o.get("red_flags") or "[]")]
    except (ValueError, TypeError):
        pass
    if flags:
        items = "".join(f"<li>{esc(f)}</li>" for f in flags)
        parts.append(f'<span class="detail-label">Red flags</span>'
                     f'<ul class="flags">{items}</ul>')
    if review:
        parts.append(labelled(
            "Next step",
            "Re-score it once the cause is fixed. It stays out of the digest "
            "until then."))
        parts.append("<code>python scripts/rescore.py</code>")
    else:
        action = sentence(o.get("suggested_action") or "No action suggested.")
        if o.get("act_by"):
            action += f" Act by {fmt_day(o['act_by'])}."
        parts.append(labelled("Next step", action))
    if o.get("cv_version"):
        parts.append(f'<div class="row-sub">Scored against {esc(o["cv_version"])}.</div>')
    parts.append(f'<div class="row-sub">Via {esc(via(o.get("source")))}.</div>')
    link = source_link(o.get("source_url"), "View the advert")
    if link:
        parts.append(link)
    if data.get("serve"):
        if acked:
            parts.append(f'<button type="button" class="act-btn" '
                         f'data-unack="{o["id"]}">Undo, back to the list</button>')
        else:
            parts.append(f'<button type="button" class="act-btn" '
                         f'data-ack="{o["id"]}">Acknowledge, seen it</button>')
    elif not acked:
        parts.append('<div class="row-sub">Acknowledge it from the served '
                     'page, or with <code>python scripts/touch.py mark '
                     f'"{esc(o.get("company") or "")}" --as actioned</code></div>')
    out.append(detail(parts, did))
    return "".join(out)


def render_signal(s: dict, data: dict, i: int) -> str:
    did = f"d-s{i}"
    review = s["relevance"] is None
    out = [row_open(did), '<span class="row-main"><span class="row-head">',
           f'<span class="row-title">{esc(s.get("headline") or s.get("company") or "Signal")}</span>',
           "</span>"]
    why = s.get("why") or ""
    if why:
        out.append(f'<span class="row-why">{esc(sentence(why))}</span>')
    sub_bits = [b for b in (s.get("company"),) if b]
    sub = " &middot; ".join(esc(b) for b in sub_bits)
    if s.get("source_id"):
        sub = (sub + " &middot; " if sub else "") + f"via {esc(s['source_id'])}"
    if sub:
        out.append(f'<span class="row-sub">{sub}</span>')
    out.append("</span>")

    if review:
        out.append('<span class="col-score">&ndash;</span>')
        out.append('<span class="chip chip-fail">review</span>')
    else:
        hot = " col-score-hot" if (s["relevance"] or 0) >= data["fast"] else ""
        out.append(f'<span class="col-score{hot}">{s["relevance"]}</span>')
        if s.get("pushed"):
            out.append(f'<span class="chip chip-actioned">pushed '
                       f'{fmt_day(s.get("pushed_at"))}</span>')
        elif s["status"] in ("actioned", "dead"):
            out.append(status_chip(s["status"]))
        else:
            out.append('<span class="chip">held</span>')
    out.append(f'<span class="col-when">{fmt_day(s.get("first_seen"))}</span>')

    parts = []
    if review:
        parts.append(labelled(
            "Next step",
            "Re-score it once the cause is fixed. It stays out of the digest "
            "until then."))
        parts.append("<code>python scripts/rescore.py</code>")
    elif s.get("playbook_step"):
        parts.append(labelled("Playbook step", sentence(s["playbook_step"])))
    link = source_link(s.get("source_url"), "Read the announcement")
    if link:
        parts.append(link)
    out.append(detail(parts, did))
    return "".join(out)


def render_thread(t: dict, i: int, today: str) -> str:
    did = f"d-t{i}"
    due = t.get("next_action_date") or ""
    out = [row_open(did), '<span class="row-main"><span class="row-head">',
           f'<span class="row-title">{esc(t.get("company"))}</span>']
    if due and due == today:
        out.append('<span class="chip">due today</span>')
    out.append("</span>")
    out.append(f'<span class="row-why">{esc(sentence(t.get("next_action")))}</span>')
    out.append(f'<span class="row-sub">Last touch {esc(fmt_day(t.get("touched_at")))} '
               f'via {esc(t.get("channel") or "other")}.</span>')
    out.append("</span>")
    when = f"due {fmt_day(due)}" if due else "no date"
    out.append(f'<span class="col-when">{esc(when)}</span>')
    parts = []
    if t.get("note"):
        parts.append(labelled("Note", sentence(t["note"])))
    out.append(detail(parts, did))
    return "".join(out)


def render_ageing_row(a: dict, i: int) -> str:
    did = f"d-a{i}"
    if a["kind"] == "opportunity":
        title, meta = a.get("title") or "Untitled role", a.get("company") or ""
        score = a.get("combined")
    else:
        title, meta = a.get("headline") or a.get("company") or "Signal", ""
        score = a.get("relevance")
    since = a.get("status_changed_at") or a.get("first_seen") or ""
    try:
        days = (datetime.now(timezone.utc)
                - datetime.strptime(since[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)).days
        why = f"{words(days).capitalize()} days, no movement."
    except ValueError:
        why = "Sitting still, no movement."
    out = [row_open(did), '<span class="row-main"><span class="row-head">',
           f'<span class="row-title">{esc(title)}</span>']
    if meta:
        out.append(f'<span class="row-meta">{esc(meta)}</span>')
    out.append("</span>")
    out.append(f'<span class="row-why">{esc(why)}</span></span>')
    out.append(f'<span class="col-score">{score}</span>')
    out.append(status_chip(a.get("status")))
    out.append(f'<span class="col-when">{fmt_day(a.get("first_seen"))}</span>')
    parts = [labelled("Next step",
                      "Either reply or retire it. Retiring takes one command.")]
    company = a.get("company")
    if company:
        parts.append(f'<code>python scripts/touch.py mark "{esc(company)}"'
                     ' --as dead</code>')
    out.append(detail(parts, did))
    return "".join(out)


RESULT_WORDS = {
    "200": ("answered", False), "304": ("not modified", False),
    "999": ("blocked by robots.txt", True),
    "robots-blocked": ("blocked by robots.txt", True),
    "0": ("unreachable", True), "403": ("blocks plain fetches", True),
}


def render_watchlist(data: dict) -> str:
    rows = ['<div class="src-head"><span>Source</span>'
            '<span class="h-method">Method</span>'
            '<span class="h-when">Last checked</span>'
            '<span class="h-result">Result</span></div>']
    by_tier: dict[int, list[dict]] = {}
    for src in data["watchlist"]:
        by_tier.setdefault(int(src.get("tier", 2)), []).append(src)
    tier_words = {1: "Tier 1. Origin sources, checked every six hours",
                  2: "Tier 2. Trade press, checked daily"}
    for tier in sorted(by_tier):
        rows.append(f'<p class="tier">{esc(tier_words.get(tier, f"Tier {tier}"))}</p>')
        for src in by_tier[tier]:
            state = data["sources_state"].get(src.get("id"))
            unverified = src.get("status") == "unverified"
            name = source_link(src.get("url"), src.get("name", src.get("id"))) \
                or esc(src.get("name", src.get("id")))
            if unverified:
                name += ' <span class="chip">unverified</span>'
            if unverified:
                when = "skipped"
            elif state and state.get("last_checked"):
                when = fmt_day_time(state["last_checked"])
            else:
                when = "never"
            if unverified:
                result, bad = "blocks plain fetches", False
            elif state and state.get("last_status"):
                result, bad = RESULT_WORDS.get(
                    str(state["last_status"]),
                    (f"error {state['last_status']}", True))
            else:
                result, bad = "no check yet", False
            bad_cls = " bad" if bad else ""
            rows.append(
                f'<div class="src"><span class="src-name">{name}</span>'
                f'<span class="src-method">{esc(src.get("method", ""))}</span>'
                f'<span class="src-when">{esc(when)}</span>'
                f'<span class="src-result{bad_cls}">{esc(result)}</span></div>')
    return "".join(rows)


def standing_line(data: dict) -> str:
    if data["fresh"]:
        loaded = words(len(data["watchlist"])).capitalize()
        return ("<p>Nothing on file yet, and that is correct.</p>"
                f'<p class="note">{loaded} sources are loaded and the watcher '
                "hasn't run. Arm the workflows and this page fills itself.</p>")
    n_roles = len(data["opportunities"])
    n_sigs = len(data["signals"])
    n_threads = len(data["threads"])
    n_age = len(data["ageing"])
    first = (f"{words(n_roles).capitalize()} "
             f"{'role' if n_roles == 1 else 'roles'} and {words(n_sigs)} "
             f"{'signal' if n_sigs == 1 else 'signals'} on file.")
    if n_threads and n_age:
        second = (f" {words(n_threads).capitalize()} "
                  f"{'thread is' if n_threads == 1 else 'threads are'} open "
                  f"and {words(n_age)} "
                  f"{'thing has' if n_age == 1 else 'things have'} been "
                  "sitting still for over a fortnight.")
    elif n_threads:
        second = (f" {words(n_threads).capitalize()} "
                  f"{'thread is' if n_threads == 1 else 'threads are'} open "
                  "and nothing is sitting still.")
    elif n_age:
        second = (f" {words(n_age).capitalize()} "
                  f"{'thing has' if n_age == 1 else 'things have'} been "
                  "sitting still for over a fortnight.")
    else:
        second = " No threads are waiting and nothing is sitting still."
    return f"<p>{first}{second}</p>"


def heartbeat_facts(data: dict) -> str:
    if data["fresh"]:
        live = sum(1 for s in data["watchlist"] if s.get("status") == "live")
        facts = ["Inbox has never run", "Signals have never been checked",
                 f"{len(data['watchlist'])} sources loaded, {live} verified",
                 "0 items seen", "No failed emails", "No tokens spent"]
    else:
        facts = []
        facts.append(f"Inbox last ran {fmt_day_time(data['inbox_ts'])}"
                     if data["inbox_ts"] else "Inbox has never run")
        facts.append(f"Signals last checked {fmt_day_time(data['signals_ts'])}"
                     if data["signals_ts"] else "Signals have never been checked")
        facts.append(f"{len(data['watchlist'])} sources watched, "
                     f"{data['answering']} answering"
                     if data["checked"]
                     else f"{len(data['watchlist'])} sources loaded, "
                          "none checked yet")
        facts.append(f"{data['week_runs']} inbox "
                     f"{'run' if data['week_runs'] == 1 else 'runs'} this week")
        dups = max(0, data["week_seen"] - data["week_new"])
        facts.append(f"{data['week_seen']} seen, {data['week_new']} new, "
                     f"{dups} duplicates skipped")
        facts.append("No failed emails" if not data["failed_emails"]
                     else f"{data['failed_emails']} "
                          f"{'email' if data['failed_emails'] == 1 else 'emails'}"
                          " failed extraction")
        facts.append(f"{data['tokens']:,} tokens, about ${data['cost']:,.2f}")
    return "".join(f"<li>{esc(f)}</li>" for f in facts)


def sparkline(data: dict) -> str:
    if data["fresh"] or not any(data["spark"]):
        return ""
    top = max(data["spark"])
    bars = "".join(
        f'<i style="height:{max(2, round(18 * n / top))}px"></i>'
        for n in data["spark"])
    said = ", ".join(words(n).capitalize() if i == 0 else words(n)
                     for i, n in enumerate(data["spark"]))
    return (f'<p class="spark-row"><span class="spark" role="img" aria-label='
            f'"New items per week over the last four weeks. {esc(said)}.">'
            f'{bars}</span><span>New items per week, last four weeks</span></p>')


# --------------------------------------------------------------------- page

CSS = """
:root{
  color-scheme:light;
  --paper:#f9f8f4; --surface:#fdfcfa; --surface-sunk:#f3f1ea;
  --ink-1:#14130f; --ink-2:#4e4b44; --ink-3:#726e64;
  --hairline:#e5e2d8; --hairline-strong:#d3cfc2;
  --accent:#1f5f9e; --accent-quiet:#a8c6e2; --accent-wash:#eef3f8;
  --fail:#a3341c; --settled:#2f6a45;
  --tint-blue:#f1f4f7; --tint-sand:#f7f3ea; --tint-sage:#f0f4ef;
  --tint-clay:#f7f0ec; --tint-stone:#f4f3ef;
  --panel-rule:rgba(20,19,15,.09);
  --font-serif:Georgia,serif;
  --font-sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --font-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --text-2xs:11px; --text-xs:12px; --text-sm:13px; --text-base:15px;
  --text-lg:20px; --text-xl:26px; --text-2xl:34px;
  --weight-medium:550; --weight-strong:650; --tracking-label:.04em;
  --space-4:4px; --space-6:6px; --space-8:8px; --space-10:10px;
  --space-12:12px; --space-16:16px; --space-20:20px; --space-24:24px;
  --space-32:32px; --space-40:40px; --space-56:56px; --space-72:72px;
  --page-max:1400px; --measure-why:74ch; --measure-read:66ch;
  --radius-sm:4px; --radius:10px; --radius-pill:999px;
  --ease:cubic-bezier(.2,0,.1,1); --dur-instant:90ms; --dur-fast:140ms; --dur:200ms;
}
@media (prefers-color-scheme:dark){:root{
  color-scheme:dark;
  --paper:#14161a; --surface:#1b1e23; --surface-sunk:#101216;
  --ink-1:#e8e6e0; --ink-2:#b4b0a6; --ink-3:#8a857a;
  --hairline:#282c33; --hairline-strong:#3a3f47;
  --accent:#7db4e8; --accent-quiet:#35536f; --accent-wash:#1a222b;
  --fail:#e0906e; --settled:#7fb894;
  --tint-blue:#171b21; --tint-sand:#1c1a16; --tint-sage:#161a17;
  --tint-clay:#1e1917; --tint-stone:#191a1c;
  --panel-rule:rgba(232,230,224,.10);
}}
html[data-appearance="light"]{
  color-scheme:light;
  --paper:#f9f8f4; --surface:#fdfcfa; --surface-sunk:#f3f1ea;
  --ink-1:#14130f; --ink-2:#4e4b44; --ink-3:#726e64;
  --hairline:#e5e2d8; --hairline-strong:#d3cfc2;
  --accent:#1f5f9e; --accent-quiet:#a8c6e2; --accent-wash:#eef3f8;
  --fail:#a3341c; --settled:#2f6a45;
  --tint-blue:#f1f4f7; --tint-sand:#f7f3ea; --tint-sage:#f0f4ef;
  --tint-clay:#f7f0ec; --tint-stone:#f4f3ef;
  --panel-rule:rgba(20,19,15,.09);
}
html[data-appearance="dark"]{
  color-scheme:dark;
  --paper:#14161a; --surface:#1b1e23; --surface-sunk:#101216;
  --ink-1:#e8e6e0; --ink-2:#b4b0a6; --ink-3:#8a857a;
  --hairline:#282c33; --hairline-strong:#3a3f47;
  --accent:#7db4e8; --accent-quiet:#35536f; --accent-wash:#1a222b;
  --fail:#e0906e; --settled:#7fb894;
  --tint-blue:#171b21; --tint-sand:#1c1a16; --tint-sage:#161a17;
  --tint-clay:#1e1917; --tint-stone:#191a1c;
  --panel-rule:rgba(232,230,224,.10);
}
@media (prefers-reduced-motion:reduce){:root{--dur-instant:0ms;--dur-fast:0ms;--dur:0ms}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--paper);color:var(--ink-1);font:400 var(--text-base)/1.55 var(--font-sans);-webkit-font-smoothing:antialiased}
.page{max-width:var(--page-max);margin:0 auto;padding:var(--space-40) var(--space-24) var(--space-72)}
a{color:var(--accent);text-decoration:underline;text-decoration-color:var(--accent-quiet);text-underline-offset:2px;transition:text-decoration-color var(--dur-instant) var(--ease)}
a:hover{text-decoration-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:var(--radius-sm)}
.masthead{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:var(--space-12);padding-bottom:var(--space-16)}
.controls{display:flex;flex-wrap:wrap;gap:var(--space-8)}
.segmented button:disabled{color:var(--ink-3);cursor:wait}
.masthead h1{font:400 var(--text-2xl)/1.2 var(--font-serif);letter-spacing:-.01em;margin:0}
.mark{flex:0 0 auto;display:block}
.brand{display:flex;align-items:baseline;gap:var(--space-12)}
.masthead-sub{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);margin:var(--space-6) 0 0}
.segmented{display:inline-flex;gap:1px;padding:1px;border-radius:var(--radius-pill);border:1px solid var(--hairline);background:var(--surface)}
.segmented button{font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;white-space:nowrap;border:none;border-radius:var(--radius-pill);padding:5px 11px;cursor:pointer;background:transparent;color:var(--ink-3);transition:color var(--dur-instant) var(--ease),background var(--dur-instant) var(--ease)}
.segmented button:hover{color:var(--ink-2)}
.segmented button[aria-pressed="true"]{background:var(--surface-sunk);color:var(--ink-1)}
.standing{border-top:1px solid var(--hairline-strong);border-bottom:1px solid var(--hairline);padding:var(--space-24) 0 var(--space-20)}
.standing p{font:400 var(--text-xl)/1.35 var(--font-serif);margin:0;max-width:var(--measure-read);text-wrap:pretty}
.standing .note{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2);margin:var(--space-8) 0 0;max-width:var(--measure-read);text-wrap:pretty}
.section{margin-top:var(--space-56)}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:var(--space-12);padding-bottom:var(--space-8);border-bottom:1px solid var(--hairline-strong)}
.section-head h2{font:var(--weight-strong) var(--text-sm)/1.35 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;margin:0}
.section-count{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);font-variant-numeric:tabular-nums}
.section-note{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-3);margin:var(--space-10) 0 var(--space-12);max-width:none;text-wrap:pretty}
.panel{background:var(--tint-stone);border:1px solid var(--panel-rule);border-radius:var(--radius);padding:0 var(--space-16)}
.panel-blue{background:var(--tint-blue)}
.panel-sand{background:var(--tint-sand)}
.panel-sage{background:var(--tint-sage)}
.panel-clay{background:var(--tint-clay)}
.row{border-top:1px solid var(--panel-rule);border-left:2px solid transparent}
.panel-head + .row{border-top:none}
.panel-head{display:grid;grid-template-columns:12px minmax(0,1fr) repeat(6, 88px);column-gap:var(--space-10);align-items:baseline;padding:var(--space-12) var(--space-4) var(--space-8) var(--space-8);border-bottom:1px solid var(--panel-rule);font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3)}
.row-summary{display:grid;grid-template-columns:12px minmax(0,1fr) repeat(6, 88px);column-gap:var(--space-10);align-items:baseline;width:100%;background:none;border:none;border-radius:var(--radius-sm);padding:var(--space-12) var(--space-4) var(--space-12) var(--space-8);margin:0;text-align:left;font:inherit;color:inherit;cursor:pointer}
.row-summary[aria-expanded]:hover .row-title{color:var(--accent)}
.row-summary:not([aria-expanded]){cursor:default}
.caret{width:6px;height:6px;border-right:1.5px solid var(--ink-3);border-bottom:1.5px solid var(--ink-3);transform:rotate(-45deg) translateX(-1px);transform-origin:60% 60%;transition:transform var(--dur-fast) var(--ease)}
.row-summary:not([aria-expanded]) .caret{opacity:0}
.row-summary[aria-expanded="true"] .caret{transform:rotate(45deg) translateY(-1px)}
.row-main{min-width:0}
.row-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--space-8)}
.row-title{font:var(--weight-medium) var(--text-base)/1.35 var(--font-sans);color:var(--ink-1);transition:color var(--dur-instant) var(--ease)}
.row-meta{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3)}
.row-why{display:block;font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2);max-width:var(--measure-why);margin-top:var(--space-4);text-wrap:pretty}
.row-sub{display:block;font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);margin-top:var(--space-4)}
.row-detail{display:grid;gap:var(--space-12);padding:0 var(--space-8) var(--space-20) var(--space-32);max-width:var(--measure-read)}
.row-detail[hidden]{display:none}
.col-when{grid-column:8;font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);font-variant-numeric:tabular-nums;white-space:nowrap;text-align:center}
.col-cv,.col-want,.col-score{font:var(--weight-medium) var(--text-sm)/1.55 var(--font-sans);font-variant-numeric:tabular-nums;color:var(--ink-2);white-space:nowrap;text-align:center}
.col-cv{grid-column:3}
.col-want{grid-column:4}
.col-score{grid-column:5}
.col-score-hot{color:var(--ink-1);font-weight:var(--weight-strong)}
.col-rate{grid-column:6;font:var(--weight-medium) var(--text-xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;white-space:nowrap;text-align:center;color:var(--ink-3)}
.rate-above{color:var(--settled);font-weight:var(--weight-strong)}
.rate-close{color:var(--ink-2)}
.panel-head .h-rate{grid-column:6;text-align:center}
.legend{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);margin:var(--space-8) 0 0;max-width:none;text-wrap:pretty}
.tabs{display:inline-flex;gap:var(--space-16);align-items:baseline;margin-right:var(--space-16)}
.tabs a{font:var(--weight-strong) var(--text-sm)/1.35 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;text-decoration:none;color:var(--ink-3);padding-bottom:2px;border-bottom:2px solid transparent}
.tabs a:hover{color:var(--ink-2)}
.tabs a[aria-current="page"]{color:var(--ink-1);border-bottom-color:var(--accent)}
.front{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(0,1fr);gap:var(--space-24);margin-top:var(--space-24)}
.lead{background:var(--tint-sand);border:1px solid var(--panel-rule);border-radius:var(--radius);padding:var(--space-24)}
.lead .kicker,.story .kicker{font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--accent);margin:0 0 var(--space-8)}
.lead h3{font:400 var(--text-2xl)/1.15 var(--font-serif);letter-spacing:-.01em;margin:0 0 var(--space-12);text-wrap:balance}
.lead .standfirst{font:400 var(--text-lg)/1.45 var(--font-serif);color:var(--ink-2);margin:0 0 var(--space-12);text-wrap:pretty}
.story-meta{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);font-variant-numeric:tabular-nums}
.playbook{background:var(--accent-wash);border-left:2px solid var(--accent-quiet);border-radius:var(--radius-sm);padding:var(--space-10) var(--space-12);font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2);margin:var(--space-12) 0 0}
.playbook b{font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3);display:block;margin-bottom:2px}
.widgets{display:grid;gap:var(--space-16);align-content:start}
.widget{background:var(--tint-stone);border:1px solid var(--panel-rule);border-radius:var(--radius);padding:var(--space-16)}
.widget h4{font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3);margin:0 0 var(--space-10)}
.widget ul{list-style:none;margin:0;padding:0;font:400 var(--text-sm)/1.7 var(--font-sans);color:var(--ink-2)}
.widget .num{font:var(--weight-strong) var(--text-lg)/1.2 var(--font-sans);color:var(--ink-1);font-variant-numeric:tabular-nums;margin-right:6px}
.stories{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:var(--space-16);margin-top:var(--space-24)}
.story{background:var(--surface);border:1px solid var(--panel-rule);border-radius:var(--radius);padding:var(--space-16)}
.story h3{font:400 var(--text-lg)/1.3 var(--font-serif);margin:0 0 var(--space-8);text-wrap:balance}
.story p{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2);margin:0 0 var(--space-8);text-wrap:pretty}
.section-rule{font:var(--weight-strong) var(--text-sm)/1.35 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;margin:var(--space-40) 0 0;padding-bottom:var(--space-8);border-bottom:1px solid var(--hairline-strong)}
.sources-fold{margin-top:var(--space-56)}
.sources-fold summary{font:var(--weight-strong) var(--text-sm)/1.35 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3);cursor:pointer;padding-bottom:var(--space-8);border-bottom:1px solid var(--hairline-strong)}
.sources-fold summary:hover{color:var(--ink-1)}
.sources-fold .panel{margin-top:var(--space-12)}
.story-actions{display:flex;gap:var(--space-10);margin:var(--space-12) 0 0}
.touch-note{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--settled);margin:var(--space-8) 0 0}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:var(--space-16);margin-top:var(--space-24)}
.board-note{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);margin:var(--space-6) 0 0}
.add-source{display:flex;flex-wrap:wrap;gap:var(--space-10);align-items:center;padding:var(--space-12) 0 var(--space-4)}
.add-source-strip{margin-top:var(--space-40)}
.add-source input{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-1);background:var(--surface);border:1px solid var(--hairline-strong);border-radius:var(--radius-sm);padding:6px 10px;min-width:200px}
.add-source input::placeholder{color:var(--ink-3)}
.board-badge{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:6px;margin-right:10px;color:#fff;font:650 10px/1 var(--font-sans);letter-spacing:0;text-transform:none;vertical-align:-4px;flex:none}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:9px;flex:none;vertical-align:baseline}
.dot-green{background:var(--settled);animation:dot-pulse 2.4s ease-in-out infinite}
.dot-amber{background:#C08A1D}
.dot-red{background:var(--fail)}
.dot-grey{background:transparent;border:1.5px solid var(--hairline-strong)}
@keyframes dot-pulse{0%,100%{box-shadow:0 0 0 0 color-mix(in srgb, var(--settled) 40%, transparent)}55%{box-shadow:0 0 0 5px transparent}}
@media (prefers-reduced-motion:reduce){.dot-green{animation:none}}
.widget li{display:flex;align-items:baseline;gap:2px}
.widget .li-age{margin-left:auto;color:var(--ink-3);font-variant-numeric:tabular-nums;font-size:var(--text-xs);padding-left:10px;white-space:nowrap}
.bars{display:flex;align-items:flex-end;gap:3px;height:46px;margin:var(--space-8) 0 var(--space-6)}
.bars i{display:block;flex:1;min-width:4px;background:var(--accent-quiet);border-radius:1px 1px 0 0}
.bars i.today{background:var(--accent)}
.bars i.none{height:2px !important;background:var(--hairline)}
.bars-label{font:400 var(--text-2xs)/1.55 var(--font-sans);color:var(--ink-3);display:flex;justify-content:space-between}
.entrance{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--space-16);margin-top:var(--space-24)}
.card{display:flex;flex-direction:column;gap:var(--space-8);border:1px solid var(--panel-rule);border-radius:var(--radius);padding:var(--space-24);text-decoration:none;color:var(--ink-1);transition:border-color var(--dur-fast) var(--ease),transform var(--dur-fast) var(--ease)}
.card:hover{border-color:var(--accent-quiet);transform:translateY(-1px)}
.card svg{color:var(--accent)}
.card-jobs{background:var(--tint-blue)}
.card-insights{background:var(--tint-sand)}
.card-cv{background:var(--tint-sage)}
.card-title{font:400 var(--text-xl)/1.2 var(--font-serif)}
.card-sub{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2)}
.trend-line{display:block;width:100%;height:96px;margin:var(--space-8) 0 var(--space-4)}
.pref-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:var(--space-16)}
.pref-grid label{display:flex;flex-direction:column;gap:6px;font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3)}
.pref-grid input{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-1);background:var(--surface);border:1px solid var(--hairline-strong);border-radius:var(--radius-sm);padding:7px 10px}
.pref-grid .act-btn{align-self:flex-start}
.info{position:relative;display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border:1px solid var(--hairline-strong);border-radius:50%;color:var(--ink-3);font:650 9px/1 var(--font-serif);font-style:italic;cursor:help;margin-left:7px;vertical-align:-2px;text-transform:none;letter-spacing:0}
.info:hover,.info:focus-visible{color:var(--ink-1);border-color:var(--accent-quiet)}
.info::after{content:attr(data-tip);position:absolute;left:50%;bottom:calc(100% + 8px);transform:translateX(-50%);width:250px;background:var(--surface);color:var(--ink-2);border:1px solid var(--hairline-strong);border-radius:var(--radius);padding:10px 12px;font:400 var(--text-xs)/1.55 var(--font-sans);letter-spacing:0;text-transform:none;text-align:left;white-space:normal;box-shadow:0 4px 14px rgba(0,0,0,.08);opacity:0;pointer-events:none;transition:opacity var(--dur-fast) var(--ease);z-index:5}
.info:hover::after,.info:focus-visible::after{opacity:1}
.section-head h2{display:inline-flex;align-items:center}
.ext{display:inline-flex;color:var(--ink-3);margin-left:8px;vertical-align:-1px;transition:color var(--dur-instant) var(--ease)}
.ext:hover{color:var(--accent)}
@media (max-width:900px){.front{grid-template-columns:minmax(0,1fr)}}
.row-acked{display:none;opacity:.6}
.panel.show-acked .row-acked{display:block}
.ack-toggle{font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;white-space:nowrap;border:1px solid var(--hairline);border-radius:var(--radius-pill);padding:3px 10px;margin-left:auto;cursor:pointer;background:var(--surface);color:var(--ink-3);transition:color var(--dur-instant) var(--ease)}
.ack-toggle:hover{color:var(--ink-2)}
.ack-toggle[aria-pressed="true"]{background:var(--surface-sunk);color:var(--ink-1)}
.act-btn{font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;white-space:nowrap;border:1px solid var(--hairline-strong);border-radius:var(--radius-pill);padding:4px 12px;cursor:pointer;background:var(--surface);color:var(--ink-2);justify-self:start;transition:color var(--dur-instant) var(--ease),border-color var(--dur-instant) var(--ease)}
.act-btn:hover{color:var(--accent);border-color:var(--accent-quiet)}
.act-btn:disabled{color:var(--ink-3);cursor:wait}
.row-summary > .chip{grid-column:7;justify-self:center}
.panel-head .h-cv{grid-column:3;text-align:center}
.panel-head .h-want{grid-column:4;text-align:center}
.panel-head .h-score{grid-column:5;text-align:center}
.panel-head .h-chip{grid-column:7;text-align:center}
.panel-head .h-when{grid-column:8;text-align:center}
.chip{display:inline-block;font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);border-radius:var(--radius-pill);padding:1px 8px;border:1px solid var(--hairline-strong);color:var(--ink-3);white-space:nowrap}
.chip-new{color:var(--accent);border-color:var(--accent-quiet)}
.chip-actioned{color:var(--settled);border-color:var(--settled)}
.chip-dead{text-decoration:line-through}
.chip-fail{color:var(--fail);border-color:var(--fail)}
.detail-label{display:block;font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3);margin-bottom:var(--space-4)}
.detail-body{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2)}
.flags{display:flex;flex-wrap:wrap;gap:var(--space-4) var(--space-10);margin:0;padding:0;list-style:none;font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2)}
.flags li+li::before{content:"\\00b7";color:var(--ink-3);margin-right:var(--space-6)}
.empty{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-3);margin:0;padding:var(--space-16) var(--space-8);max-width:var(--measure-read);text-wrap:pretty}
code{font:400 var(--text-xs)/1.55 var(--font-mono);background:var(--surface);border:1px solid var(--panel-rule);border-radius:var(--radius-sm);padding:2px 6px;color:var(--ink-2)}
.tier{font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3);padding:var(--space-16) var(--space-8) var(--space-6);border-top:1px solid var(--panel-rule)}
.panel > .tier:first-child{border-top:none;padding-top:var(--space-12)}
.src,.src-head{display:grid;grid-template-columns:minmax(0,1fr) 88px 88px 200px;column-gap:var(--space-10);align-items:baseline;padding:var(--space-8);font:400 var(--text-sm)/1.55 var(--font-sans)}
.src{border-top:1px solid var(--panel-rule)}
.src-head{padding:var(--space-12) var(--space-8) var(--space-8);border-bottom:1px solid var(--panel-rule);font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3)}
.src-head .h-method,.src-head .h-when{text-align:center}
.src-head + .tier{border-top:none}
.src-name{min-width:0;overflow-wrap:anywhere}
.src-method,.src-when{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);font-variant-numeric:tabular-nums;text-align:center}
.src-result{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);text-align:left;white-space:nowrap}
.src-result.bad{color:var(--fail)}
.heartbeat{margin-top:var(--space-56);padding-top:var(--space-16);border-top:1px solid var(--hairline)}
.heartbeat-facts{display:flex;flex-wrap:wrap;gap:var(--space-4) var(--space-16);list-style:none;margin:0;padding:0;font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);font-variant-numeric:tabular-nums}
.heartbeat-facts li+li::before{content:"\\00b7";color:var(--hairline-strong);margin-right:var(--space-16)}
.spark-row{display:flex;align-items:center;gap:var(--space-10);font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);margin-top:var(--space-12)}
.spark{display:inline-flex;align-items:flex-end;gap:3px;height:18px}
.spark i{display:block;width:5px;background:var(--hairline-strong);border-radius:1px}
.spark i:last-child{background:var(--ink-3)}
.outlinks{display:flex;flex-wrap:wrap;gap:var(--space-16);margin:var(--space-16) 0 0;padding:0;list-style:none;font:400 var(--text-xs)/1.55 var(--font-sans)}
.end{font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);margin:var(--space-12) 0 0}
@media (max-width:820px){
  .panel-head{display:none}
  .row-summary{grid-template-columns:12px minmax(0,1fr) auto;column-gap:var(--space-12);row-gap:var(--space-6)}
  .col-cv,.col-want,.col-rate{display:none}
  .col-score{grid-column:3;text-align:right}
  .col-score::before{content:"combined ";font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3)}
  .row-summary > .chip{grid-column:2;justify-self:start}
  .col-when{grid-column:3;text-align:right}
  .src-head{display:none}
  .src{grid-template-columns:minmax(0,1fr) auto;row-gap:var(--space-4)}
  .src-method,.src-when{grid-column:2;text-align:right}
  .src-result{grid-column:1/-1;text-align:left;white-space:normal}
}
@media (max-width:600px){
  .page{padding:var(--space-24) var(--space-16) var(--space-56)}
  .masthead h1{font-size:var(--text-xl)}
  .standing p{font-size:var(--text-lg)}
  .section{margin-top:var(--space-40)}
  .row-detail{padding-left:var(--space-20)}
}
"""

FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
           "viewBox='0 0 32 32'><rect width='32' height='32' rx='7' "
           "fill='%2314161a'/><circle cx='16' cy='21' r='7' fill='%23f2efe8'/>"
           "<rect x='0' y='21' width='32' height='11' fill='%2314161a'/>"
           "<rect x='4' y='21' width='24' height='1.6' rx='0.8' "
           "fill='%237db4e8'/></svg>")

MARK_SVG = ('<svg class="mark" viewBox="0 0 36 17" width="48" height="23" '
            'fill="none" aria-hidden="true" focusable="false">'
            '<path d="M4 15a14 14 0 0 1 28 0" stroke="var(--ink-3)" stroke-width="1.2"/>'
            '<path d="M8.5 15a9.5 9.5 0 0 1 19 0" stroke="var(--ink-2)" stroke-width="1.2"/>'
            '<path d="M13 15a5 5 0 0 1 10 0Z" fill="var(--ink-1)"/>'
            '<path d="M0 15h36" stroke="var(--ink-1)" stroke-width="1.5"/></svg>')

SCRIPT = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.row-summary[aria-expanded]');
    if (!btn) return;
    var open = btn.getAttribute('aria-expanded') === 'true';
    btn.setAttribute('aria-expanded', open ? 'false' : 'true');
    var panel = document.getElementById(btn.getAttribute('aria-controls'));
    if (panel) panel.hidden = open;
  });
  var ackToggle = document.getElementById('ack-toggle');
  if (ackToggle) ackToggle.addEventListener('click', function () {
    var panel = ackToggle.closest('.section').querySelector('.panel');
    var on = panel.classList.toggle('show-acked');
    ackToggle.setAttribute('aria-pressed', String(on));
  });
  var appearance = document.querySelectorAll('[data-appearance-set]');
  appearance.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var next = btn.dataset.appearanceSet;
      document.documentElement.setAttribute('data-appearance', next);
      appearance.forEach(function (b) { b.setAttribute('aria-pressed', String(b === btn)); });
      try { localStorage.setItem('radar-appearance', next); } catch (err) {}
    });
  });
  try {
    var saved = localStorage.getItem('radar-appearance');
    if (saved) {
      document.documentElement.setAttribute('data-appearance', saved);
      appearance.forEach(function (b) {
        b.setAttribute('aria-pressed', String(b.dataset.appearanceSet === saved));
      });
    }
  } catch (err) {}
})();
"""


INSIGHTS_SCRIPT = """
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-sig-done],[data-sig-ack],[data-sig-unack]');
    if (!btn) return;
    var url, id;
    if (btn.hasAttribute('data-sig-done')) { url = '/sig/done'; id = btn.dataset.sigDone; }
    else if (btn.hasAttribute('data-sig-ack')) { url = '/sig/ack'; id = btn.dataset.sigAck; }
    else { url = '/sig/unack'; id = btn.dataset.sigUnack; }
    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Working';
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(id) })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok === false) {
          btn.disabled = false;
          btn.textContent = label;
          window.alert(j.note || 'That did not stick. Try again.');
          return;
        }
        location.reload();
      })
      .catch(function () { location.reload(); });
  });
})();
"""


SERVE_SCRIPT = """
(function () {
  var refresh = document.getElementById('btn-refresh');
  if (refresh) refresh.addEventListener('click', function () { location.reload(); });
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-ack],[data-unack]');
    if (!btn) return;
    var ack = btn.hasAttribute('data-ack');
    btn.disabled = true;
    btn.textContent = ack ? 'Acknowledging' : 'Restoring';
    fetch(ack ? '/ack' : '/unack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(btn.dataset.ack || btn.dataset.unack) })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok === false) {
          btn.disabled = false;
          btn.textContent = (ack ? 'Acknowledge, seen it' : 'Undo, back to the list');
          window.alert(j.note || 'That did not stick. Try again.');
          return;
        }
        location.reload();
      })
      .catch(function () { location.reload(); });
  });
  var watch = document.getElementById('btn-watch');
  if (watch) watch.addEventListener('click', function () {
    watch.disabled = true;
    watch.textContent = 'Checking';
    fetch('/watch', { method: 'POST' })
      .then(function () { location.reload(); })
      .catch(function () { location.reload(); });
  });
  setTimeout(function () { location.reload(); }, 900000);
})();
"""


def section(title: str, count, note: str, panel_class: str, body: str,
            legend: str = "", head_extra: str = "",
            title_html: str | None = None) -> str:
    legend_html = f'\n<p class="legend">{esc(legend)}</p>' if legend else ""
    return f"""<section class="section">
<div class="section-head"><h2>{title_html or esc(title)}</h2>{head_extra}
<span class="section-count">{count}</span></div>
<p class="section-note">{esc(note)}</p>
<div class="panel {panel_class}">{body}</div>{legend_html}
</section>"""


# The box-with-arrow glyph, the universal "this opens the website".
EXT_SVG = ('<svg viewBox="0 0 12 12" width="11" height="11" fill="none" '
           'stroke="currentColor" stroke-width="1.3" stroke-linecap="round" '
           'stroke-linejoin="round" aria-hidden="true">'
           '<path d="M5 2H2.2A1.2 1.2 0 0 0 1 3.2v6.6A1.2 1.2 0 0 0 2.2 11'
           'h6.6A1.2 1.2 0 0 0 10 9.8V7"/>'
           '<path d="M7.5 1H11v3.5M11 1 5.8 6.2"/></svg>')


def info_tip(text: str) -> str:
    """The small circled i that carries a widget's rule on hover."""
    return (f'<span class="info" tabindex="0" data-tip="{esc(text)}">i</span>')


def board_title_html(s: dict) -> str:
    """Badge tile, board name, and the open-the-site arrow when known."""
    badge = s.get("badge") or (s.get("name") or "?")[:1].upper()
    colour = s.get("colour") or ("var(--ink-3)" if s["id"] == "email-other"
                                 else "var(--accent)")
    html = (f'<span class="board-badge" style="background:{esc(colour)}">'
            f'{esc(badge)}</span><span>{esc(s.get("name") or s["id"])}</span>')
    site = safe_url(s.get("url"))
    if site:
        html += (f'<a class="ext" href="{esc(site)}" target="_blank" '
                 f'rel="noopener noreferrer" aria-label="Open '
                 f'{esc(s.get("name") or s["id"])} in a new tab">{EXT_SVG}</a>')
    return html


def rate_legend(floor) -> str:
    if floor is None:
        return ("Rate bands need the day_rate_floor_gbp line in the "
                "preferences file, and it is missing, so everything reads "
                "Unstated until it returns.")
    f = f"£{round(floor):,}"
    lo = f"£{round(floor) - 50:,}"
    return (f"Rate bands sit against the {f} a day floor from the "
            f"preferences file. Above meets it, Close is under by up to "
            f"£50, so {lo} and up, Below is under by more, and a stated "
            "range is banded on its top, the best case. Salaries convert "
            "at 220 working days a year, hours at eight a day, euros at "
            "the static rate in radar.yaml.")


def render_page(data: dict, config: dict, db_label: str, out: Path,
                serve: bool = False) -> str:
    now = radar_common.now_iso()
    today = now[:10]
    fresh = data["fresh"]
    data["serve"] = serve

    opp_head = ('<div class="panel-head"><span></span><span>Role</span>'
                '<span class="h-cv">Capability</span>'
                '<span class="h-want">Desire</span>'
                '<span class="h-score">Combined</span>'
                '<span class="h-rate">Rate</span>'
                '<span class="h-chip"></span>'
                '<span class="h-when">Seen</span></div>')
    sig_head = ('<div class="panel-head"><span></span><span>What happened</span>'
                '<span class="h-score">Relevance</span>'
                '<span class="h-chip">Push</span>'
                '<span class="h-when">Seen</span></div>')
    thr_head = ('<div class="panel-head"><span></span><span>Company</span>'
                '<span class="h-when">Due</span></div>')
    age_head = ('<div class="panel-head"><span></span><span>Item</span>'
                '<span class="h-score">Score</span>'
                '<span class="h-chip">Status</span>'
                '<span class="h-when">Seen</span></div>')

    acked = data.get("acknowledged") or []
    if fresh or (not data["opportunities"] and not acked):
        opp_body = ('<p class="empty">Nothing scored yet. The inbox workflow '
                    'fills this section once radar-inbox is armed.</p>')
    else:
        opp_body = opp_head + "".join(
            render_opportunity(o, data, i)
            for i, o in enumerate(data["opportunities"], 1))
        if not data["opportunities"]:
            opp_body += ('<p class="empty">Everything in sight is '
                         'acknowledged. The toggle above shows the pile.</p>')
        opp_body += "".join(
            render_opportunity(o, data, i, acked=True)
            for i, o in enumerate(acked, 1 + len(data["opportunities"])))
    ack_toggle = ""
    if acked and not fresh:
        ack_toggle = (f'<button type="button" class="ack-toggle" '
                      f'id="ack-toggle" aria-pressed="false">Show '
                      f'acknowledged ({len(acked)})</button>')
    if fresh or not data["signals"]:
        sig_body = ('<p class="empty">No signals yet. The watcher fills this '
                    'section once radar-signals is armed and switched from '
                    'dry run to push.</p>')
    else:
        sig_body = sig_head + "".join(
            render_signal(s, data, i) for i, s in enumerate(data["signals"], 1))
    if fresh or not data["threads"]:
        thr_body = ('<p class="empty">Nothing waiting. Log touches with '
                    '<code>python scripts/touch.py add</code> and open ones '
                    'appear here.</p>')
    else:
        thr_body = thr_head + "".join(
            render_thread(t, i, today) for i, t in enumerate(data["threads"], 1))
    if fresh:
        age_body = ('<p class="empty">Nothing sitting idle, because nothing '
                    'has been seen yet.</p>')
    elif not data["ageing"]:
        age_body = '<p class="empty">Nothing sitting idle.</p>'
    else:
        age_body = age_head + "".join(
            render_ageing_row(a, i) for i, a in enumerate(data["ageing"], 1))

    outlinks = []
    if serve:
        outlinks.append('<li><a href="/cv">Update the CV the scorer reads</a></li>')
    n8n = safe_url(config.get("n8n_url"))
    if n8n:
        outlinks.append(f'<li><a href="{esc(n8n)}" target="_blank" '
                        'rel="noopener noreferrer">Open the n8n workflows</a></li>')
    for target, label in (
            (radar_common.REPO_ROOT / "test" / "last_digest.html",
             "Read the last dry-run digest"),
            (radar_common.REPO_ROOT / "test" / "last_signal.txt",
             "Read the last dry-run push payloads"),
            (radar_common.REPO_ROOT / "docs" / "cost-note.md",
             "Read the cost note")):
        if target.exists():
            href = Path(os.path.relpath(target, out.parent)).as_posix()
            outlinks.append(f'<li><a href="{esc(href)}">{esc(label)}</a></li>')

    return f"""<!doctype html>
<html lang="en-GB" data-appearance="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MedTech Radar</title>
<link rel="icon" href="{FAVICON}">
<style>{CSS}</style>
</head>
<body>
<main class="page">
<header class="masthead">
<div>
<div class="brand">{MARK_SVG}<h1>MedTech Radar</h1></div>
<p class="masthead-sub">Everything on file. Read only, generated {esc(fmt_long(now))} from {esc(db_label)}.{" The page re-renders on every load and reloads itself every fifteen minutes." if serve else ""}</p>
</div>
<div class="controls">
{'''<nav class="tabs" aria-label="Pages"><a href="/" aria-current="page">Home</a><a href="/jobs">Jobs</a><a href="/insights">Insights</a></nav>''' if serve else ""}
{'''<span class="segmented" role="group" aria-label="Data">
<button type="button" id="btn-refresh">Refresh</button>
<button type="button" id="btn-watch">Check now</button>
</span>''' if serve else ""}
<span class="segmented" role="group" aria-label="Appearance">
<button type="button" data-appearance-set="light" aria-pressed="false">Light</button>
<button type="button" data-appearance-set="auto" aria-pressed="true">Auto</button>
<button type="button" data-appearance-set="dark" aria-pressed="false">Dark</button>
</span>
</div>
</header>
<section class="standing">{standing_line(data)}</section>
{section("Inbound", 0 if fresh else len(data["opportunities"]),
         "Job adverts and approaches. Capability is judged on the CV alone, "
         "desire on the preferences file alone, and the two are never "
         f"blurred. The combined figure decides the digest, and the bar is "
         f"{data['threshold']}. Acknowledged rows leave this view and the "
         "digest but never the database, so a seen advert can never "
         "resurface.", "panel-blue", opp_body,
         legend=rate_legend(data["floor"]), head_extra=ack_toggle)}
{section("Signals", 0 if fresh else len(data["signals"]),
         "Funding, spin-offs, accelerator entries and first quality hires. "
         f"At {data['fast']} the phone gets a push once the workflow is "
         "armed.", "panel-sand", sig_body)}
{section("Threads awaiting action", 0 if fresh else len(data["threads"]),
         "The latest touch per company with a next step still open, from "
         "the tracker.", "panel-sage", thr_body)}
{section("Ageing", 0 if fresh else len(data["ageing"]),
         "Above the bar, older than two weeks, hasn't moved. Not a problem, "
         "just unfinished.", "panel-clay", age_body)}
{"" if serve else section("Watchlist health", len(data["watchlist"]),
         "Every fetched source and what came back. Email-borne alerts "
         "arrive through the inbox workflow instead and are not listed "
         "here.", "", render_watchlist(data))}
<footer class="heartbeat">
<ul class="heartbeat-facts">{heartbeat_facts(data)}</ul>
{sparkline(data)}
<ul class="outlinks">{"".join(outlinks)}</ul>
<p class="end">Read only. Nothing on this page changes the database. Regenerate with <code>python scripts/build_dashboard.py</code>.</p>
</footer>
</main>
<script>{SCRIPT}{SERVE_SCRIPT if serve else ""}</script>
</body>
</html>"""


def _story_card(s: dict, data: dict, lead: bool = False,
                state: str = "active") -> str:
    """One signal as a newspaper story. The lead gets the big type.

    state chooses the action row. "active" offers I did it and Dismiss,
    "followed" shows when it was done, "dismissed" offers the undo.
    """
    kicker_bits = [b for b in (s.get("company"),) if b]
    if s.get("source_id"):
        kicker_bits.append(f"via {s['source_id']}")
    kicker = " &middot; ".join(esc(b) for b in kicker_bits)
    relevance = s.get("relevance")
    meta_bits = [f"Relevance {relevance}" if relevance is not None
                 else "Awaiting a score"]
    if s.get("pushed"):
        meta_bits.append(f"pushed to the phone {fmt_day(s.get('pushed_at'))}")
    meta_bits.append(f"seen {fmt_day(s.get('first_seen'))}")
    meta = " &middot; ".join(meta_bits)
    link = source_link(s.get("source_url"), "Read the announcement")
    playbook = ""
    if s.get("playbook_step"):
        playbook = (f'<div class="playbook"><b>Suggestion</b>'
                    f'{esc(sentence(s["playbook_step"]))}</div>')
    # Relationship context. A story about a company already touched says
    # so, because the second contact should know about the first.
    touch = (data.get("touch_map") or {}).get(
        (s.get("company") or "").lower())
    touch_line = ""
    if touch:
        touch_line = (f'<p class="touch-note">You last touched this company '
                      f'{esc(fmt_day(touch.get("touched_at")))} via '
                      f'{esc(touch.get("channel") or "other")}.</p>')
        # A booked next move belongs beside the last one, so the card
        # answers "what now" without a trip to the tracker.
        if touch.get("next_action"):
            due = touch.get("next_action_date")
            when = f" Due {esc(fmt_day(due))}." if due else ""
            touch_line += (f'<p class="touch-note">Booked. '
                           f'{esc(sentence(touch["next_action"]))}{when}</p>')
    actions = ""
    if state == "active":
        actions = (f'<p class="story-actions">'
                   f'<button type="button" class="act-btn" '
                   f'data-sig-done="{s["id"]}">I did it</button>'
                   f'<button type="button" class="act-btn" '
                   f'data-sig-ack="{s["id"]}">Dismiss</button></p>')
    elif state == "dismissed":
        actions = (f'<p class="story-actions">'
                   f'<button type="button" class="act-btn" '
                   f'data-sig-unack="{s["id"]}">Undo, back on the page'
                   f'</button></p>')
    why = esc(sentence(s.get("why") or ""))
    headline = esc(s.get("headline") or s.get("company") or "Signal")
    if lead:
        return (f'<article class="lead"><p class="kicker">{kicker}</p>'
                f"<h3>{headline}</h3>"
                + (f'<p class="standfirst">{why}</p>' if why else "")
                + playbook + touch_line
                + f'<p class="story-meta" style="margin-top:12px">{meta}.'
                + (f" {link}" if link else "") + "</p>" + actions
                + "</article>")
    return (f'<article class="story"><p class="kicker">{kicker}</p>'
            f"<h3>{headline}</h3>"
            + (f"<p>{why}</p>" if why else "")
            + playbook + touch_line
            + f'<p class="story-meta" style="margin-top:10px">{meta}.'
            + (f" {link}" if link else "") + "</p>" + actions
            + "</article>")


def render_insights_page(data: dict, config: dict, db_label: str) -> str:
    """The Insights front page. Signals as news, sources folded below.

    The archive page is for working the pipeline. This page is for
    reading what the watcher found, the way a front page reads, lead
    story first, the rest in columns, the machinery in widgets, and the
    provenance behind a fold for the day it is wanted.
    """
    now = radar_common.now_iso()
    week_ago = (datetime.now(timezone.utc)
                - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    live = [s for s in data["signals"] if s.get("status") != "dead"]
    followed = [s for s in live if s.get("status") == "actioned"]
    active = [s for s in live if s.get("status") != "actioned"]
    dismissed = data.get("dismissed_signals") or []
    scored = [s for s in active if s.get("relevance") is not None]
    unscored = [s for s in active if s.get("relevance") is None]
    fresh = [s for s in scored if (s.get("first_seen") or "") >= week_ago]
    earlier = [s for s in scored if (s.get("first_seen") or "") < week_ago]
    lead = fresh[0] if fresh else (scored[0] if scored else None)
    fresh_rest = [s for s in fresh if s is not lead]
    earlier_rest = [s for s in earlier if s is not lead]

    pushed = [s for s in scored + followed if s.get("pushed")]
    answering = data["answering"]
    watched = len(data["watchlist"])
    quiet = []
    for src in data["watchlist"]:
        state = data["sources_state"].get(src.get("id"))
        status = str(state.get("last_status")) if state else None
        if status and RESULT_WORDS.get(status, ("", True))[1]:
            word = RESULT_WORDS.get(status, (f"error {status}", True))[0]
            quiet.append(f"{src.get('name', src.get('id'))}, {word}")

    widgets = ['<div class="widgets">']
    widgets.append(
        '<div class="widget"><h4>The week in numbers'
        + info_tip("Fresh means scored insights first seen in the last "
                   "seven days, dismissed and followed-up ones excluded.")
        + "</h4><ul>"
        f'<li><span class="num">{len(fresh)}</span>fresh '
        f'{"insight" if len(fresh) == 1 else "insights"}</li>'
        f'<li><span class="num">{data["week_new"]}</span>new items through '
        'the pipeline</li>'
        f'<li><span class="num">{len(pushed)}</span>pushed to the phone'
        '</li>'
        + (f'<li><span class="num">{len(followed)}</span>'
           f'{"suggestion" if len(followed) == 1 else "suggestions"} done'
           '</li>' if followed else "")
        + (f'<li><span class="num">{len(unscored)}</span>awaiting a score, '
           'rescore.py clears them</li>' if unscored else "")
        + "</ul></div>")
    if pushed:
        items = "".join(
            f"<li>{esc(s.get('headline') or s.get('company'))} "
            f"({fmt_day(s.get('pushed_at'))})</li>" for s in pushed[:5])
        widgets.append('<div class="widget"><h4>Reached the phone'
                       + info_tip(f"Signals at {data['fast']} or higher "
                                  "push to the phone the moment they "
                                  "score. This lists what actually "
                                  "arrived, with the date it did.")
                       + f"</h4><ul>{items}</ul></div>")
    coverage_note = (f'<li><span class="num">{answering}</span>of {watched} '
                     "sources answering</li>")
    quiet_items = "".join(f"<li>{esc(q)}</li>" for q in quiet[:5])
    if not quiet_items:
        # Honest wording either way. A source that has never been checked
        # is not "responsive", it is simply still in the queue.
        if data["checked"] >= watched:
            quiet_items = "<li>Every source is answering.</li>"
        else:
            quiet_items = ("<li>Every checked source is answering, "
                           f"{watched - data['checked']} await their first "
                           "check.</li>")
    widgets.append(
        '<div class="widget"><h4>Coverage'
        + info_tip("Answering means the last fetch came back with content "
                   "or not-modified. Sources never checked are queued, "
                   "not failing, and anything gone dark is named here.")
        + '</h4><ul>' + coverage_note
        + quiet_items + "</ul></div>")
    widgets.append("</div>")

    if lead:
        front = (f'<div class="front">{_story_card(lead, data, lead=True)}'
                 + "".join(widgets) + "</div>")
        body = front
        if fresh_rest:
            body += ('<p class="section-rule">Also fresh this week</p>'
                     '<div class="stories">'
                     + "".join(_story_card(s, data) for s in fresh_rest)
                     + "</div>")
        if earlier_rest:
            body += ('<p class="section-rule">Earlier, still on file</p>'
                     '<div class="stories">'
                     + "".join(_story_card(s, data) for s in earlier_rest)
                     + "</div>")
    else:
        body = ('<div class="front"><div class="lead"><h3>Nothing checked '
                "and relevant yet.</h3><p class=\"standfirst\">The watcher "
                "fills this page as the watchlist turns up funding rounds, "
                "spin-offs and first hires. The sources below say whether "
                "it is looking.</p></div>" + "".join(widgets) + "</div>")

    if followed:
        body += ('<p class="section-rule">Followed up. The suggestion is '
                 'done and the touch is logged.</p>'
                 '<div class="stories">'
                 + "".join(_story_card(s, data, state="followed")
                           for s in followed)
                 + "</div>")
    if dismissed:
        body += (f'<details class="sources-fold"><summary>Dismissed '
                 f'({len(dismissed)}). Out of sight, still deduped, undo '
                 'lives here.</summary><div class="stories" '
                 'style="margin-top:16px">'
                 + "".join(_story_card(s, data, state="dismissed")
                           for s in dismissed)
                 + "</div></details>")

    sources_fold = (
        f'<details class="sources-fold"><summary>Sources. {watched} '
        f"watched, {answering} answering. Where this page's information "
        "comes from.</summary>"
        f'<div class="panel">{render_watchlist(data)}</div>'
        '<p class="legend">Email-borne job alerts arrive through the inbox '
        "workflow and are not fetched sources, so they are not listed "
        "here.</p></details>")

    return f"""<!doctype html>
<html lang="en-GB" data-appearance="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MedTech Radar. Insights.</title>
<link rel="icon" href="{FAVICON}">
<style>{CSS}</style>
</head>
<body>
<main class="page">
<header class="masthead">
<div>
<div class="brand">{MARK_SVG}<h1>Insights</h1></div>
<p class="masthead-sub">What the watcher found, checked and relevant, read
like a front page. Generated {esc(fmt_long(now))} from {esc(db_label)}.</p>
</div>
<div class="controls">
<nav class="tabs" aria-label="Pages"><a href="/">Home</a><a href="/jobs">Jobs</a><a href="/insights" aria-current="page">Insights</a></nav>
<span class="segmented" role="group" aria-label="Appearance">
<button type="button" data-appearance-set="light" aria-pressed="false">Light</button>
<button type="button" data-appearance-set="auto" aria-pressed="true">Auto</button>
<button type="button" data-appearance-set="dark" aria-pressed="false">Dark</button>
</span>
</div>
</header>
{body}
{sources_fold}
</main>
<script>{SCRIPT}{INSIGHTS_SCRIPT}</script>
</body>
</html>"""


ICON_JOBS = ('<svg viewBox="0 0 24 24" width="26" height="26" fill="none" '
             'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true">'
             '<rect x="3" y="7.5" width="18" height="12" rx="2"/>'
             '<path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5"/>'
             '<path d="M3 12.5h18"/></svg>')
ICON_INSIGHTS = ('<svg viewBox="0 0 24 24" width="26" height="26" fill="none" '
                 'stroke="currentColor" stroke-width="1.5" '
                 'stroke-linecap="round" stroke-linejoin="round" '
                 'aria-hidden="true">'
                 '<path d="M4 5h13a0 0 0 0 1 0 0v13a2 2 0 0 1-2 2H6a2 2 0 0 '
                 '1-2-2Z"/><path d="M17 8h2a1 1 0 0 1 1 1v9a2 2 0 0 1-2 2"/>'
                 '<path d="M7 9h7M7 12.5h7M7 16h4"/></svg>')
ICON_CV = ('<svg viewBox="0 0 24 24" width="26" height="26" fill="none" '
           'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
           'stroke-linejoin="round" aria-hidden="true">'
           '<path d="M6 3h8l4 4v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 '
           '1 2-2Z"/><path d="M14 3v4h4"/><circle cx="10" cy="11" r="1.8"/>'
           '<path d="M7 17.5c.6-1.8 1.8-2.7 3-2.7s2.4.9 3 2.7"/></svg>')


def _month_label(month: str) -> str:
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%b %y")
    except (ValueError, TypeError):
        return month


def _line_chart(series: list[tuple[str, int | None]], label: str) -> str:
    """A quiet SVG line across month buckets, peaks and troughs visible.

    A None count means the radar was not listening that month, so the
    line simply does not exist there. Zero is drawn, silence is not.
    """
    if not series:
        return ""
    counted = [c for _, c in series if c is not None]
    if not counted:
        return ('<p class="board-note">The year starts this month. The '
                "line draws itself as the radar listens.</p>")
    top = max(counted) or 1
    w, h, pad = 560, 96, 8
    step = (w - 2 * pad) / max(1, len(series) - 1)
    points = []
    dots = []
    for i, (month, c) in enumerate(series):
        if c is None:
            continue
        x = pad + i * step
        y = h - pad - (h - 2 * pad) * c / top
        points.append(f"{x:.1f},{y:.1f}")
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" '
                    f'fill="var(--accent)"><title>{c} in '
                    f'{esc(_month_label(month))}</title></circle>')
    first_label = _month_label(series[0][0])
    last_label = _month_label(series[-1][0])
    data_start = next(m for m, c in series if c is not None)
    started_late = any(c is None for _, c in series)
    poly = (f'<polyline points="{" ".join(points)}" fill="none" '
            'stroke="var(--accent)" stroke-width="1.8" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
            if len(points) > 1 else "")
    note = (f'<p class="board-note">Recording began {esc(_month_label(data_start))}. '
            "The empty months are the radar's age, not a quiet market.</p>"
            if started_late else "")
    return (f'<svg class="trend-line" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="none" role="img" aria-label="{esc(label)}">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" '
            'stroke="var(--hairline-strong)" stroke-width="1"/>'
            + poly + "".join(dots) + "</svg>"
            f'<p class="bars-label"><span>{esc(first_label)}</span>'
            f'<span>peak {top}</span><span>{esc(last_label)}</span></p>'
            + note)


HOME_SCRIPT = """
(function () {
  document.querySelectorAll('[data-pref-save]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var key = btn.dataset.prefSave;
      var input = document.getElementById('pref-' + key);
      var note = document.getElementById('pref-note');
      btn.disabled = true;
      fetch('/profile/setting', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: key, value: input.value })
      }).then(function (r) { return r.json(); })
        .then(function (j) {
          if (j && j.ok === false) {
            btn.disabled = false;
            note.textContent = j.note || 'That did not stick.';
            return;
          }
          location.reload();
        })
        .catch(function () { btn.disabled = false;
                             note.textContent = 'The server did not answer.'; });
    });
  });
})();
"""


def render_home_page(data: dict, config: dict, db_label: str) -> str:
    """Home as the entrance. Cards to the pages, trends, the scoring panel.

    The working tables live on their own pages now, Jobs, Insights and
    the full archive at /archive. Home answers the walking-past
    questions: is it running, what came in, how is the market moving,
    and what is the scorer actually judging me against.
    """
    data["serve"] = True
    now = radar_common.now_iso()

    active = data["opportunities"]
    week_ago = (datetime.now(timezone.utc)
                - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_this_week = [o for o in active
                     if (o.get("first_seen") or "") >= week_ago]
    live_signals = [s for s in data["signals"] if s.get("status") == "new"
                    and s.get("relevance") is not None]
    fresh_signals = [s for s in live_signals
                     if (s.get("first_seen") or "") >= week_ago]
    cv_label = "the built-in cv.txt"
    try:
        import cv_store
        import score_item
        cv_label = score_item.get_cv_version(config)
    except Exception:  # noqa: BLE001  the card degrades, never breaks Home
        pass

    cards = f"""<div class="entrance">
<a class="card card-jobs" href="/jobs">{ICON_JOBS}<span class="card-title">Jobs</span>
<span class="card-sub">{len(active)} {"role" if len(active) == 1 else "roles"} in play, {len(new_this_week)} new this week</span></a>
<a class="card card-insights" href="/insights">{ICON_INSIGHTS}<span class="card-title">Insights</span>
<span class="card-sub">{len(live_signals)} live, {len(fresh_signals)} fresh this week</span></a>
<a class="card card-cv" href="/cv">{ICON_CV}<span class="card-title">CV</span>
<span class="card-sub">Scoring against {esc(cv_label)}</span></a>
</div>"""

    def hours_since(iso):
        try:
            then = datetime.strptime(str(iso)[:16], "%Y-%m-%dT%H:%M")
        except (ValueError, TypeError):
            return None
        return max(0.0, (datetime.now(timezone.utc)
                         - then.replace(tzinfo=timezone.utc)
                         ).total_seconds() / 3600)

    inbox_hours = hours_since(data["inbox_ts"])
    if inbox_hours is None:
        sync_dot, sync_word = "red", "never run"
    elif inbox_hours <= 2:
        sync_dot, sync_word = "green", "on schedule"
    elif inbox_hours <= 6:
        sync_dot, sync_word = "amber", "running late"
    else:
        sync_dot, sync_word = "red", "stalled"
    sync_when = (f", last sync {fmt_day_time(data['inbox_ts'])}"
                 if data["inbox_ts"] else "")

    wp = data["weekly_prospects"]
    this_wk = wp[-1][1] if wp else 0
    last_wk = wp[-2][1] if len(wp) > 1 else 0
    if this_wk > last_wk:
        wk_verdict = "more than last week, the market is giving"
    elif this_wk < last_wk:
        wk_verdict = "down on last week"
    else:
        wk_verdict = "level with last week"
    top_w = max((c for _, c in wp), default=0) or 1
    wp_bars = "".join(
        ('<i class="none" title="none in the week of '
         f'{esc(fmt_day(wk))}"></i>') if c == 0 else
        (f'<i class="{"today" if i == len(wp) - 1 else ""}" '
         f'style="height:{max(4, round(46 * c / top_w))}px" '
         f'title="{c} in the week of {esc(fmt_day(wk))}"></i>')
        for i, (wk, c) in enumerate(wp))
    mp = data["month_prospects"]

    overview = f"""<div class="metrics">
<div class="widget"><h4>Right now{info_tip("The inbox light judges its hourly schedule. Green ran within two hours, amber within six, red stalled or never run. The counts are the last seven days.")}</h4><ul>
<li><span class="dot dot-{sync_dot}" title="{esc(sync_word)}"></span>Inbox {esc(sync_word)}{esc(sync_when)}</li>
<li><span class="num">{len(new_this_week)}</span>new {"job" if len(new_this_week) == 1 else "jobs"} this week</li>
<li><span class="num">{data["week_new"]}</span>through the pipeline, {max(0, data["week_seen"] - data["week_new"])} duplicates skipped</li>
</ul></div>
<div class="widget"><h4>Top prospects, week on week{info_tip(f"A top prospect scores {data['threshold']} or higher combined, the digest bar. Bars are Monday-to-Sunday weeks, and the month figures sit beneath for the slower trend.")}</h4>
<div class="bars">{wp_bars}</div>
<p class="bars-label"><span>eight weeks ago</span><span>this week</span></p>
<ul style="margin-top:8px">
<li><span class="num">{this_wk}</span>this week, {last_wk} last week, {esc(wk_verdict)}</li>
<li><span class="num">{mp["this"]}</span>this month, {mp["last"]} last month</li>
</ul></div>
<div class="widget"><h4>Insight activity, the year{info_tip(f"Only medtech events scoring {data['threshold']} or higher count, the ones that could need your input. Peaks say the ecosystem is announcing, troughs say go quiet and build.")}</h4>
{_line_chart(data["monthly_signals"], "Relevant medtech signals per month over twelve months")}
</div>
</div>"""

    floor_val = data["floor"]
    title_val = radar_common.read_pref_line("target_title", config) or ""
    keywords_val = radar_common.read_pref_line("keywords", config) or ""
    keyword_chips = "".join(
        f'<span class="chip">{esc(k.strip())}</span>'
        for k in keywords_val.split(",") if k.strip())
    profile = f"""<section class="section">
<div class="section-head"><h2>What the scorer judges against</h2></div>
<p class="section-note">Capability comes from the CV, desire from the
preferences file, and these are the file's machine-readable lines, edited
in place. Everything saved here is in the scorer's next system prompt.</p>
<div class="panel panel-sage" style="padding:16px">
<div class="pref-grid">
<label>Title<input type="text" id="pref-target_title"
 value="{esc(title_val)}" placeholder="e.g. Fractional software director, medtech and IVD">
<button type="button" class="act-btn" data-pref-save="target_title">Save</button></label>
<label>Day-rate floor, GBP<input type="text" id="pref-day_rate_floor_gbp"
 value="{esc('' if floor_val is None else f'{floor_val:g}')}" placeholder="650">
<button type="button" class="act-btn" data-pref-save="day_rate_floor_gbp">Save</button></label>
<label>Keywords, comma separated<input type="text" id="pref-keywords"
 value="{esc(keywords_val)}" placeholder="IEC 62304, ISO 13485, IVD, fractional">
<button type="button" class="act-btn" data-pref-save="keywords">Save</button></label>
</div>
<p class="board-note" id="pref-note"></p>
<div style="margin-top:10px">{keyword_chips}</div>
<p class="legend">Also in the judging set. The active CV,
{esc(cv_label)}, changed at <a href="/cv">the CV page</a>. The engagement
types, location and sector rules live as prose in
config/profile/preferences.md, edited by hand, this panel never touches
them. The rate floor drives the Rate bands everywhere at once.</p>
</div>
</section>"""

    n_threads = len(data["threads"])
    n_age = len(data["ageing"])
    n_review = sum(1 for o in active if o.get("combined") is None)
    # A booked date that has arrived is the difference between a thread
    # waiting and a thread late. Today counts as due, not overdue.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n_due = sum(1 for t in data["threads"]
                if (t.get("next_action_date") or "") and
                str(t["next_action_date"])[:10] <= today)
    attention_bits = []
    if n_threads:
        due_note = f", {n_due} due or overdue" if n_due else ""
        attention_bits.append(
            f'<li><span class="num">{n_threads}</span>'
            f'{"thread" if n_threads == 1 else "threads"} awaiting your '
            f'next move{due_note}</li>')
    if n_age:
        attention_bits.append(
            f'<li><span class="num">{n_age}</span>above the bar and '
            'sitting still over a fortnight</li>')
    if n_review:
        attention_bits.append(
            f'<li><span class="num">{n_review}</span>awaiting a score, '
            'rescore.py clears them</li>')
    attention = ""
    if attention_bits:
        attention = ('<section class="section">'
                     '<div class="section-head"><h2>Needs you</h2></div>'
                     '<div class="widget" style="margin-top:12px"><ul>'
                     + "".join(attention_bits)
                     + '</ul><p class="board-note">The detail lives in '
                     '<a href="/archive">the full archive</a>.</p>'
                     "</div></section>")

    return f"""<!doctype html>
<html lang="en-GB" data-appearance="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MedTech Radar</title>
<link rel="icon" href="{FAVICON}">
<style>{CSS}</style>
</head>
<body>
<main class="page">
<header class="masthead">
<div>
<div class="brand">{MARK_SVG}<h1>MedTech Radar</h1></div>
<p class="masthead-sub">Generated {esc(fmt_long(now))} from {esc(db_label)}. The page re-renders on every load.</p>
</div>
<div class="controls">
<nav class="tabs" aria-label="Pages"><a href="/" aria-current="page">Home</a><a href="/jobs">Jobs</a><a href="/insights">Insights</a></nav>
<span class="segmented" role="group" aria-label="Appearance">
<button type="button" data-appearance-set="light" aria-pressed="false">Light</button>
<button type="button" data-appearance-set="auto" aria-pressed="true">Auto</button>
<button type="button" data-appearance-set="dark" aria-pressed="false">Dark</button>
</span>
</div>
</header>
<section class="standing">{standing_line(data)}</section>
{cards}
{overview}
{profile}
{attention}
<footer class="heartbeat">
<ul class="heartbeat-facts">{heartbeat_facts(data)}</ul>
{sparkline(data)}
<ul class="outlinks"><li><a href="/archive">The full archive, every row on file</a></li><li><a href="/cv">Update the CV the scorer reads</a></li></ul>
<p class="end">Buttons write, everything else reads. Regenerate the static fallback with <code>python scripts/build_dashboard.py</code>.</p>
</footer>
</main>
<script>{SCRIPT}{HOME_SCRIPT}</script>
</body>
</html>"""


JOBS_SCRIPT = """
(function () {
  var addBtn = document.getElementById('src-add');
  var nameIn = document.getElementById('src-name');
  var senderIn = document.getElementById('src-sender');
  var urlIn = document.getElementById('src-url');
  var note = document.getElementById('src-note');
  if (!addBtn) return;
  addBtn.addEventListener('click', function () {
    addBtn.disabled = true;
    fetch('/jobs/source', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: nameIn.value, sender: senderIn.value,
                            url: urlIn ? urlIn.value : '' })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok === false) {
          addBtn.disabled = false;
          note.textContent = j.note || 'That did not stick. Try again.';
          return;
        }
        location.reload();
      })
      .catch(function () { addBtn.disabled = false;
                           note.textContent = 'The server did not answer.'; });
  });
})();
"""


def render_jobs_page(data: dict, config: dict, db_label: str) -> str:
    """The Jobs page. Top prospects first, then one section per board.

    Every board gets its section whether or not it has sent anything,
    because an empty section under a board's own name says "subscribe the
    inbox there" louder than absence ever could. The metrics strip at the
    top exists to build trust that the machinery is alive, last run, this
    week's flow, and the freshness of each board's most recent email.
    """
    data["serve"] = True
    now = radar_common.now_iso()
    active = data["opportunities"]
    everything = active + (data.get("acknowledged") or [])

    last_by_source: dict[str, str] = {}
    for o in everything:
        src = o.get("source") or "email-other"
        seen = o.get("first_seen") or ""
        if seen > last_by_source.get(src, ""):
            last_by_source[src] = seen

    registry = [dict(s) for s in radar_common.load_job_sources()]
    known_ids = {s["id"] for s in registry}
    extras = sorted({(o.get("source") or "email-other") for o in everything
                     if (o.get("source") or "email-other") not in known_ids})
    for extra in extras:
        registry.append({"id": extra,
                         "name": ("Other email alerts" if extra == "email-other"
                                  else extra),
                         "sender_contains": None})

    builtin_ids = {s["id"] for s in radar_common.BUILTIN_JOB_SOURCES}
    customs = [s for s in registry
               if s["id"] not in builtin_ids and s.get("sender_contains")]

    def hours_since(iso) -> float | None:
        try:
            then = datetime.strptime(str(iso)[:16], "%Y-%m-%dT%H:%M")
        except (ValueError, TypeError):
            return None
        return max(0.0, (datetime.now(timezone.utc)
                         - then.replace(tzinfo=timezone.utc)
                         ).total_seconds() / 3600)

    def ago(hours: float | None) -> str:
        if hours is None:
            return ""
        if hours < 1:
            return "under an hour ago"
        if hours < 48:
            n = round(hours)
            return f"{n} {'hour' if n == 1 else 'hours'} ago"
        days = round(hours / 24)
        return f"{days} days ago"

    def dot(cls: str, word: str) -> str:
        return f'<span class="dot dot-{cls}" title="{esc(word)}"></span>'

    # Traffic lights. Boards send daily-ish, so two days is flowing,
    # a week is quiet, longer means a subscription died. Never-heard-from
    # is a hollow waiting dot, not an alarm, subscribing is a human step.
    def board_light(hours: float | None) -> tuple[str, str]:
        if hours is None:
            return "grey", "waiting for the first email"
        if hours <= 48:
            return "green", "flowing"
        if hours <= 24 * 7:
            return "amber", "quiet lately"
        return "red", "gone silent"

    # The inbox runs hourly when armed, so its light judges the schedule.
    inbox_hours = hours_since(data["inbox_ts"])
    if inbox_hours is None:
        inbox_light, inbox_word = "red", "never run"
    elif inbox_hours <= 2:
        inbox_light, inbox_word = "green", "on schedule"
    elif inbox_hours <= 6:
        inbox_light, inbox_word = "amber", "running late"
    else:
        inbox_light, inbox_word = "red", "stalled"

    dups = max(0, data["week_seen"] - data["week_new"])
    metrics = ['<div class="metrics">']
    metrics.append(
        '<div class="widget"><h4>The machine'
        + info_tip("Green ran within two hours of the hourly schedule, "
                   "amber within six, red stalled or never run. Failed "
                   "emails are poison messages that hit the three-strike "
                   "cap, the digest names them.")
        + "</h4><ul>"
        + f"<li>{dot(inbox_light, inbox_word)}Inbox {inbox_word}"
        + (f'<span class="li-age">{ago(inbox_hours)}</span>'
           if inbox_hours is not None else "")
        + "</li>"
        + f'<li><span class="num">{data["week_runs"]}</span>inbox '
        f'{"run" if data["week_runs"] == 1 else "runs"} this week</li>'
        + (f"<li>{dot('green', 'no failed emails')}No failed emails</li>"
           if not data["failed_emails"]
           else f"<li>{dot('red', 'failed emails')}"
                f'<span class="num">{data["failed_emails"]}</span>'
                'failed emails, see the digest</li>')
        + ('<li class="board-note">First move. Open n8n and read Radar '
           "Inbox's last execution. Active, failed, or absent tells you "
           "which fix it is.</li>"
           if inbox_light in ("amber", "red") else "")
        + "</ul></div>")

    counts = [c for _, c in data["daily_arrivals"]]
    top = max(counts) if any(counts) else 0
    bars = []
    for i, (day, c) in enumerate(data["daily_arrivals"]):
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%d %b").lstrip("0")
        if c == 0:
            bars.append(f'<i class="none" title="none on {esc(label)}"></i>')
        else:
            h = max(4, round(46 * c / top))
            today_cls = " today" if i == len(data["daily_arrivals"]) - 1 else ""
            bars.append(f'<i class="{today_cls.strip()}" '
                        f'style="height:{h}px" '
                        f'title="{c} on {esc(label)}"></i>')
    fortnight_total = sum(counts)
    metrics.append(
        '<div class="widget"><h4>The flow, last fourteen days'
        + info_tip("Arrivals per day, acknowledged ones included, an "
                   "arrival is an arrival. Hover a bar for its exact "
                   "count and date.")
        + "</h4>"
        f'<div class="bars">{"".join(bars)}</div>'
        '<p class="bars-label"><span>two weeks ago</span><span>today</span></p>'
        '<ul style="margin-top:8px">'
        f'<li><span class="num">{fortnight_total}</span>'
        f'{"role" if fortnight_total == 1 else "roles"} arrived</li>'
        f'<li><span class="num">{data["week_new"]}</span>new this week, '
        f'{dups} {"duplicate" if dups == 1 else "duplicates"} skipped</li>'
        "</ul></div>")

    fresh_items = []
    for s in registry:
        seen = last_by_source.get(s["id"])
        h = hours_since(seen)
        light, word = board_light(h)
        fresh_items.append(
            f"<li>{dot(light, word)}{esc(s['name'])}"
            f'<span class="li-age">'
            + (esc(ago(h)) if h is not None else "waiting")
            + "</span></li>")
    any_red_board = any(
        board_light(hours_since(last_by_source.get(s["id"])))[0] == "red"
        for s in registry)
    metrics.append('<div class="widget"><h4>Board freshness'
                   + info_tip("Green emailed within two days, amber within "
                              "a week, red was flowing and went silent, a "
                              "hollow dot has never been heard from and is "
                              "waiting on a subscription.")
                   + "</h4><ul>"
                   + "".join(fresh_items)
                   + ('<li class="board-note">A silent board stopped '
                      "sending email. Check its alert settings on the "
                      "board, then the aggregator's spam folder. LinkedIn "
                      "also rides the personal-Gmail forward filter.</li>"
                      if any_red_board else "")
                   + "</ul></div>")
    metrics.append("</div>")

    counter = 0
    scored = [o for o in active if o.get("combined") is not None]
    prospects = sorted(scored, key=lambda o: -(o["combined"] or 0))[:5]
    if prospects:
        rows = [opp_head_html()]
        for o in prospects:
            counter += 1
            rows.append(render_opportunity(o, data, counter))
        prospects_body = "".join(rows)
    else:
        prospects_body = ('<p class="empty">Nothing scored yet. The top of '
                          'the pile appears here as roles land.</p>')
    body = "".join(metrics)
    body += section(
        "Top prospects", len(prospects),
        "The highest combined scores still in play, whatever the board. "
        "Acknowledged roles live behind the toggle on Home.",
        "panel-blue", prospects_body, legend=rate_legend(data["floor"]))

    def board_section(s) -> str:
        nonlocal counter
        rows_for = [o for o in active
                    if (o.get("source") or "email-other") == s["id"]]
        rows_for.sort(key=lambda o: (o.get("combined") is None,
                                     -(o.get("combined") or 0)))
        seen = last_by_source.get(s["id"])
        note = (f"Last email {fmt_day_time(seen)}." if seen else
                (f"No roles yet from {esc(s['name'])}. Subscribe "
                 f"{esc(config.get('aggregator_email', 'the aggregator inbox'))} "
                 "to its alerts and they land here."
                 if s.get("sender_contains")
                 else "Alerts from senders no board claims land here, "
                      "still extracted, still scored."))
        if rows_for:
            rows = [opp_head_html()]
            for o in rows_for:
                counter += 1
                rows.append(render_opportunity(o, data, counter))
            body_html = "".join(rows)
        else:
            body_html = f'<p class="empty">{note}</p>'
        return section(s["name"], len(rows_for), note if rows_for else "",
                       "panel-stone", body_html,
                       title_html=board_title_html(s))

    known = [s for s in registry if s["id"] in known_ids]
    catch_all = [s for s in registry if s["id"] not in known_ids]
    for s in known:
        body += board_section(s)

    # The add-source strip. It sits where the next board's section would
    # appear, quiet, footer weight, no tinted panel. The receipt line only
    # earns ink once a custom source exists, there is no delete button on
    # purpose, removal is a hand-edit of config/job_sources.yaml.
    customs_line = ""
    if customs:
        named = ", ".join(
            f"{esc(s['name'])} (matches {esc(s['sender_contains'])})"
            for s in customs)
        customs_line = (f'<p class="legend">Added so far: {named}. Edit or '
                        "remove them by hand in config/job_sources.yaml.</p>")
    body += f"""<div class="add-source-strip">
<p class="section-rule">Add a job source</p>
<div class="add-source">
<input type="text" id="src-name" placeholder="Board name, e.g. Technojobs">
<input type="text" id="src-sender" placeholder="Sender contains, e.g. technojobs">
<input type="text" id="src-url" placeholder="Site, optional, e.g. technojobs.co.uk">
<button type="button" class="act-btn" id="src-add">Add the source</button>
<span class="board-note" id="src-note"></span>
</div>
<p class="legend">A new board files its emails under its own name here.
Subscribing {esc(config.get('aggregator_email', 'the aggregator inbox'))}
to its alerts on the board itself is still yours to do, and unrecognised
senders are scored anyway under Other email alerts.</p>
{customs_line}
</div>
"""
    for s in catch_all:
        body += board_section(s)

    return f"""<!doctype html>
<html lang="en-GB" data-appearance="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MedTech Radar. Jobs.</title>
<link rel="icon" href="{FAVICON}">
<style>{CSS}</style>
</head>
<body>
<main class="page">
<header class="masthead">
<div>
<div class="brand">{MARK_SVG}<h1>Jobs</h1></div>
<p class="masthead-sub">Every board in its own section, best prospects
first. Generated {esc(fmt_long(now))} from {esc(db_label)}.</p>
</div>
<div class="controls">
<nav class="tabs" aria-label="Pages"><a href="/">Home</a><a href="/jobs" aria-current="page">Jobs</a><a href="/insights">Insights</a></nav>
<span class="segmented" role="group" aria-label="Appearance">
<button type="button" data-appearance-set="light" aria-pressed="false">Light</button>
<button type="button" data-appearance-set="auto" aria-pressed="true">Auto</button>
<button type="button" data-appearance-set="dark" aria-pressed="false">Dark</button>
</span>
</div>
</header>
{body}
</main>
<script>{SCRIPT}{SERVE_SCRIPT}{JOBS_SCRIPT}</script>
</body>
</html>"""


def opp_head_html() -> str:
    return ('<div class="panel-head"><span></span><span>Role</span>'
            '<span class="h-cv">Capability</span>'
            '<span class="h-want">Desire</span>'
            '<span class="h-score">Combined</span>'
            '<span class="h-rate">Rate</span>'
            '<span class="h-chip"></span>'
            '<span class="h-when">Seen</span></div>')


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
        print(json.dumps({"out": str(out), "fresh": data["fresh"],
                          "opportunities": len(data["opportunities"]),
                          "signals": len(data["signals"]),
                          "threads": len(data["threads"]),
                          "ageing": len(data["ageing"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
