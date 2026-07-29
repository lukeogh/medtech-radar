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
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    age_cutoff = (now - timedelta(days=AGEING_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    opportunities = [dict(r) for r in conn.execute(
        "SELECT * FROM opportunities"
        " ORDER BY (combined IS NULL), combined DESC, first_seen DESC")]
    signals = [dict(r) for r in conn.execute(
        "SELECT * FROM signals"
        " ORDER BY (relevance IS NULL), relevance DESC, first_seen DESC")]
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
        return (o["status"] in ("new", "digested")
                and (o["combined"] or 0) >= threshold
                and (o["status_changed_at"] or o["first_seen"] or "9999") <= age_cutoff)

    def is_ageing_sig(s) -> bool:
        return (s["status"] in ("new", "digested")
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

    fresh = (run_row["any_runs"] == 0 and not opportunities and not signals
             and not threads)

    return {
        "threshold": threshold, "fast": fast, "fresh": fresh,
        "opportunities": opportunities, "signals": signals,
        "threads": threads, "ageing": ageing,
        "ageing_opp_ids": {a["id"] for a in ageing if a["kind"] == "opportunity"},
        "watchlist": watchlist, "sources_state": sources_state,
        "inbox_ts": run_row["inbox_ts"], "signals_ts": run_row["signals_ts"],
        "week_runs": week["runs"], "week_seen": week["seen"],
        "week_new": week["new"], "failed_emails": failed_emails,
        "tokens": tokens, "cost": cost, "spark": spark,
        "checked": len(checked), "answering": len(answering),
    }


# ------------------------------------------------------------------- pieces

def status_chip(status) -> str:
    cls = {"new": " chip-new", "actioned": " chip-actioned",
           "dead": " chip-dead"}.get(status, "")
    return f'<span class="chip{cls}">{esc(status or "new")}</span>'


def row_open(detail_id: str) -> str:
    return (f'<div class="row"><button type="button" class="row-summary"'
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


def render_opportunity(o: dict, data: dict, i: int) -> str:
    did = f"d-o{i}"
    review = o["combined"] is None
    meta_bits = [b for b in (o.get("company"), o.get("location"),
                             o.get("salary_rate")) if b]
    meta = ". ".join(str(b) for b in meta_bits)
    why = o.get("one_line_why") or (
        "The scorer could not score this one, so a human look is due."
        if review else "")
    out = [row_open(did), '<span class="row-main"><span class="row-head">',
           f'<span class="row-title">{esc(o.get("title") or "Untitled role")}</span>']
    if meta:
        out.append(f'<span class="row-meta">{esc(meta)}</span>')
    out.append("</span>")
    if why:
        out.append(f'<span class="row-why">{esc(sentence(why))}</span>')
    out.append("</span>")

    if review:
        out.append('<span class="col-score">&ndash;</span>')
        out.append('<span class="chip chip-fail">review</span>')
    else:
        hot = " col-score-hot" if (o["combined"] or 0) >= data["threshold"] else ""
        out.append(f'<span class="col-cv">{o["cv_match"]}</span>'
                   f'<span class="col-want">{o["want_match"]}</span>'
                   f'<span class="col-score{hot}">{o["combined"]}</span>')
        out.append(status_chip(o["status"]))
    out.append(f'<span class="col-when">{fmt_day(o.get("first_seen"))}</span>')

    parts = []
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
        if o["id"] in data["ageing_opp_ids"] and o.get("company"):
            parts.append('<code>python scripts/touch.py mark '
                         f'"{esc(o["company"])}" --as dead</code>')
    parts.append(f'<div class="row-sub">Via {esc(via(o.get("source")))}.</div>')
    link = source_link(o.get("source_url"), "View the advert")
    if link:
        parts.append(link)
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
  --page-max:1040px; --measure-why:52ch; --measure-read:66ch;
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
.panel-head{display:grid;grid-template-columns:12px minmax(0,1fr) repeat(5, 88px);column-gap:var(--space-10);align-items:baseline;padding:var(--space-12) var(--space-4) var(--space-8) var(--space-8);border-bottom:1px solid var(--panel-rule);font:var(--weight-strong) var(--text-2xs)/1.55 var(--font-sans);letter-spacing:var(--tracking-label);text-transform:uppercase;color:var(--ink-3)}
.row-summary{display:grid;grid-template-columns:12px minmax(0,1fr) repeat(5, 88px);column-gap:var(--space-10);align-items:baseline;width:100%;background:none;border:none;border-radius:var(--radius-sm);padding:var(--space-12) var(--space-4) var(--space-12) var(--space-8);margin:0;text-align:left;font:inherit;color:inherit;cursor:pointer}
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
.col-when{grid-column:7;font:400 var(--text-xs)/1.55 var(--font-sans);color:var(--ink-3);font-variant-numeric:tabular-nums;white-space:nowrap;text-align:center}
.col-cv,.col-want,.col-score{font:var(--weight-medium) var(--text-sm)/1.55 var(--font-sans);font-variant-numeric:tabular-nums;color:var(--ink-2);white-space:nowrap;text-align:center}
.col-cv{grid-column:3}
.col-want{grid-column:4}
.col-score{grid-column:5}
.col-score-hot{color:var(--ink-1);font-weight:var(--weight-strong)}
.row-summary > .chip{grid-column:6;justify-self:center}
.panel-head .h-cv{grid-column:3;text-align:center}
.panel-head .h-want{grid-column:4;text-align:center}
.panel-head .h-score{grid-column:5;text-align:center}
.panel-head .h-chip{grid-column:6;text-align:center}
.panel-head .h-when{grid-column:7;text-align:center}
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
  .col-cv,.col-want{display:none}
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


SERVE_SCRIPT = """
(function () {
  var refresh = document.getElementById('btn-refresh');
  if (refresh) refresh.addEventListener('click', function () { location.reload(); });
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


def section(title: str, count, note: str, panel_class: str, body: str) -> str:
    return f"""<section class="section">
<div class="section-head"><h2>{esc(title)}</h2>
<span class="section-count">{count}</span></div>
<p class="section-note">{esc(note)}</p>
<div class="panel {panel_class}">{body}</div>
</section>"""


def render_page(data: dict, config: dict, db_label: str, out: Path,
                serve: bool = False) -> str:
    now = radar_common.now_iso()
    today = now[:10]
    fresh = data["fresh"]

    opp_head = ('<div class="panel-head"><span></span><span>Role</span>'
                '<span class="h-cv">Capability</span>'
                '<span class="h-want">Desire</span>'
                '<span class="h-score">Combined</span>'
                '<span class="h-chip">Status</span>'
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

    if fresh or not data["opportunities"]:
        opp_body = ('<p class="empty">Nothing scored yet. The inbox workflow '
                    'fills this section once radar-inbox is armed.</p>')
    else:
        opp_body = opp_head + "".join(
            render_opportunity(o, data, i)
            for i, o in enumerate(data["opportunities"], 1))
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
         f"{data['threshold']}.", "panel-blue", opp_body)}
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
{section("Watchlist health", len(data["watchlist"]),
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
