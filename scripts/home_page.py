"""Home: the welcome, the three tiles and Needs you today.

Built from Home final (design/round-1 branch, design/home/final, 15 Nov 2026)
and the owner's decisions of that day in design/README.md, on the shared frame
in page_frame.py, dark only. Restyled 17 Nov ("Option B"): colour groups
things. Stories are amber, jobs purple, applications and follow-ups teal
(page_frame's --group-* colours); each group has an icon chip and a soft glow
from the top left of its cards. At /, with every item at its own address,
/?item=<id>, and the items taken off Home at /?show=done. On Home a signal is
a story, and the signal watcher is the News watcher.

What is on it, top to bottom (trimmed 17 Nov: no sentence under the greeting,
no As of time, no question under a widget's title, no footnotes, no keys):
- The welcome, with no card: the date, the greeting (by the first name on the
  optional first_name line of the owner's preferences file, never a name in
  the code) and, at the right, Radar's lights and Radar's health.
- Five tiles, each a link to where those items live: urgent stories, Top
  prospects, One question away, company follow-ups due and application
  follow-ups due. Urgent stories and the follow-ups each equal a group of the
  list below; Top prospects and One question away are every open role in the
  tier, jobs_page.tier_totals, the number on the Jobs page's tier bands. A
  tile at 0 keeps its colour, softer. Company follow-ups due are read from
  watchlist.threads_due, the rule the Watchlist's tags use: a Watchlist
  company's next step due today or overdue, UK date; a next step with no date
  never counts.
- Radar's five jobs as lights, each a chip with a dot, the job's name and a
  word (On time, Late, Stale, Off, Not sending or, for the inbox, Quiet), from
  radar_common.job_health, the verdict /health gives, read by
  page_frame.job_light, as the top bar's light is. The phone push has no run
  of its own: it rides on the signal watcher, so its light is worked out from
  the watcher's runs and the ntfy address in radar.yaml (push_light). Under
  the lights, a panel, closed until asked for: Week by week, the flow of the
  last fourteen days and board freshness (as /radar draws them, without their
  captions), and a link to /radar.
- Under the tiles, two columns (stacked on a phone, in this order):
- Open actions (until 17 Nov, Needs you today): Radar's jobs that need a
  look, urgent stories, new Top prospects, new One question away roles,
  company follow-ups due and application follow-ups due, in that order, every
  item its own card in full, one under another, with no subheadings. An
  urgent story is an amber card: its kind and source, the headline, the
  summary, its Next move, its links and Done. A role is a short purple card:
  the Jobs row (jobs_page.row_html, with Jobs' own row rules), its tier's
  pill and Done, nothing else. A follow-up is a teal card; a job of Radar's
  that needs a look takes a neutral chip and no glow.
- Jobs (until 17 Nov, Open roles): Top prospects, One question away and
  Reading, each its tier's pill and its total (jobs_page.tier_totals), each a
  link to its tier on Jobs, and See all.
- Applications: the board's six stages and what waits on the owner, read from
  the applications table (applications.load, board_page.board_model).
Every section's title sits over its card. Links are the text colour,
underlined in grey; teal on Home means Applications, never a link.

Done takes an item off Home and only off Home, in the home_done table; Undo
last puts it back. It never changes the item where it lives. Everything else
here reads: the page is drawn from one build_dashboard.collect() and a few
reads of its own on the same read-only connection, and its script writes only
through POST /home/done and /home/undone (and /watch, the signal watcher's
Check now, which the server already had).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import applications as ap
import build_dashboard as bd
import insights_page as ip
import jobs_page as jp
import tiers
import page_frame
import radar_common as rc
import watchlist

esc = bd.esc

WEEKS = 4                 # the weeks the trend draws, this one included
SIGNAL_BAR = 40           # the mark of a buying window (insights_page.BUYING)
URGENT_DAYS = 7           # an urgent signal is on Home for the week it was found
WAIT_SHOWN = 3            # Waiting on you items the tile lists
KEY_MAX = 300             # the longest item key the routes take
KEY_RE = re.compile(r"^(signal|role|thread|app|radar):.+")

HIRING = rc.HIRING_TYPES
CODE_TYPES = {"job-advert": "buying-window", "qa-tripwire": "first-hire"}
TYPE_WORDS = {"buying-window": "Buying window", "first-hire": "First hire",
              "funding": "Funding round", "spin-off": "Spin-off launch",
              "accelerator": "Accelerator entry", "regulation": "Regulation",
              "market": "Market news", "other": "Other news"}
CHANNEL_WORDS = {"comment": "comment", "connection-note": "connection note",
                 "engagement": "engagement", "artefact": "article sent",
                 "other": "other"}

# Radar's five jobs, as the strip names them, in order.
JOBS = (("inbox", "Inbox"), ("signals", "News watcher"),
        ("check", "Advert checker"), ("digest", "Monday digest"),
        ("push", "Phone push"))
# The words each light can say, with its colour class and its mark, each word
# its own mark, so neither the colour nor the shape stands alone. Quiet is the
# inbox's alone: alert mail arrives unevenly, so a gap under its stale limit is
# normal, not a fault. Not sending is grey, never amber (17 Nov): a job in dry
# run is the box as it is set up.
LIGHTS = {"On time": ("is-pos", "st-pos"), "Late": ("is-cau", "st-late"),
          "Not sending": ("is-neu", "st-paused"), "Stale": ("is-neg", "st-neg"),
          "Off": ("is-neu", "st-neu"), "Quiet": ("is-neu", "st-quiet")}
# A light that is a fault, and so an item under Radar in Needs you today. The
# rules are page_frame's, so the top bar's light reads the same.
FAULTS = page_frame.FAULTS
DRY_JOBS = page_frame.DRY_JOBS
# The marks the lights add to the page's symbols.
SYMBOLS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
<symbol id="st-late" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 4.8V8l2.2 1.5" fill="none" stroke="currentColor" stroke-width="1.6"/></symbol>
<symbol id="st-quiet" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="2.2 2.2"/></symbol>
<symbol id="st-paused" viewBox="0 0 16 16"><path d="M6 4.5v7M10 4.5v7" fill="none" stroke="currentColor" stroke-width="1.8"/></symbol>
<symbol id="ic-down" viewBox="0 0 16 16"><path d="M3.5 6l4.5 4.5L12.5 6" fill="none" stroke="currentColor" stroke-width="1.8"/></symbol>
</svg>"""
# The groups' line icons (owner, 17 Nov): drawn on a 20 by 20 grid, no fill,
# in the colour of their chip. Each group has one colour, page_frame's
# --group-story, --group-jobs and --group-apps; Radar's own items and the
# Open actions title take a neutral chip.
ICONS = {
    "story": '<rect x="3.5" y="3.5" width="13" height="13" rx="2"/><line x1="6.5" y1="7.5" x2="13.5" y2="7.5"/>'
             '<line x1="6.5" y1="10.5" x2="13.5" y2="10.5"/><line x1="6.5" y1="13.5" x2="10.5" y2="13.5"/>',
    "top": '<circle cx="10" cy="10" r="6.5"/><circle cx="10" cy="10" r="2.5"/>',
    "question": '<circle cx="10" cy="10" r="6.5"/><path d="M8 8.3a2 2 0 1 1 2.9 1.8c-.6.3-.9.7-.9 1.3"/>'
                '<circle cx="10" cy="13.7" r="0.5" fill="currentColor"/>',
    # A company: a building, so it never reads as the story's page. Two rows
    # of windows; three ran together at 18px.
    "threads": '<path d="M4.5 16.5V5.5A1.5 1.5 0 0 1 6 4h5a1.5 1.5 0 0 1 1.5 1.5v11"/>'
               '<path d="M12.5 9H14a1.5 1.5 0 0 1 1.5 1.5v6"/><line x1="3" y1="16.5" x2="17" y2="16.5"/>'
               '<line x1="7.2" y1="8" x2="9.8" y2="8"/><line x1="7.2" y1="12" x2="9.8" y2="12"/>',
    "apps": '<rect x="3" y="5" width="14" height="10" rx="2"/><polyline points="3.5,6.5 10,11 16.5,6.5"/>',
    "actions": '<rect x="3.5" y="3.5" width="13" height="13" rx="3"/><polyline points="6.8,10.2 9,12.4 13.4,7.8"/>',
    "jobs": '<rect x="3" y="6.5" width="14" height="9.5" rx="2"/>'
            '<path d="M7.5 6.5V5A1.5 1.5 0 0 1 9 3.5h2A1.5 1.5 0 0 1 12.5 5v1.5"/>',
    "applications": '<path d="M17 3 3 9l5.5 2.5L11 17z"/><line x1="8.5" y1="11.5" x2="17" y2="3"/>',
    # Radar's own item, a job that needs a look: a clock, as the Late mark is.
    "radar": '<circle cx="10" cy="10" r="6.5"/><polyline points="10,6.5 10,10 12.5,11.5"/>',
}


def chip(icon: str, group: str) -> str:
    """A group's icon chip: 32px square, the icon in the group's colour on a
    tint of it. group is story, jobs, apps or neutral."""
    return (f'<span class="gchip g-{group}" aria-hidden="true"><svg viewBox="0 0 20 20" '
            'width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
            f'{ICONS[icon]}</svg></span>')


SCHEDULE = {"inbox": "Each alert email, as n8n finds it in the inbox",
            "signals": "Every two hours", "check": "Once a day",
            "digest": "Mondays at 07:30", "push": "With the signal watcher"}
DOES = {"inbox": "Reads job alert emails, then stores and scores the roles.",
        "signals": "Reads the watched sources, then stores and scores stories.",
        "check": "Opens each advert's page to see whether the role is still open.",
        "digest": "Builds Monday's email of the week's roles and stories.",
        "push": "Sends a story at 75 or more to the phone, as the watcher stores it."}
ENDED = {"ok": "Finished", "partial": "Finished, with failures", "failed": "Failed"}
GROUPS = (("radar", "Radar"), ("urgent", "Urgent stories"),
          ("top", tiers.NAMES["top"]), ("question", tiers.NAMES["question"]),
          ("threads", "Company follow-ups due"), ("apps", "Applications"))


# ================================================================ time

def _aware(moment) -> datetime:
    if isinstance(moment, datetime):
        return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return ap.parse(moment) or datetime.now(timezone.utc)


def uk(moment) -> datetime:
    """A moment in UK time, which is what every time on the page is. UTC where
    the machine has no time zone database, right all winter."""
    moment = _aware(moment)
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/London"))
    except Exception:  # noqa: BLE001
        return moment


def _when(iso) -> datetime | None:
    return uk(ap.parse(iso)) if ap.parse(iso) else None


def hm(iso) -> str:
    when = _when(iso)
    return f"{when:%H:%M}" if when else ""


def day(iso) -> str:
    when = _when(iso)
    return f"{when.day} {when:%b}" if when else ""


def day_time(iso) -> str:
    when = _when(iso)
    return f"{when.day} {when:%b} {when:%H:%M}" if when else ""


def seen_words(iso, now: datetime) -> str:
    """Today's time, or the day for anything older."""
    when = _when(iso)
    if when is None:
        return ""
    return f"{when:%H:%M}" if when.date() == uk(now).date() else f"{when.day} {when:%b}"


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _hours(h) -> str:
    if h is None:
        return ""
    whole = int(round(h))
    if whole < 1:
        return "under an hour"
    if whole < 48:
        return _n(whole, "hour")
    return _n(round(h / 24), "day")


def _use(symbol: str, size: int = 12) -> str:
    return jp._use(symbol, size)


# ================================================================ the lights

# One job's light (never Off, stale Stale, late, failing or partial Late, the
# inbox's late Quiet, the digest in dry run Not sending): page_frame's rules,
# shared with the top bar's light.
_dry = page_frame._dry
job_light = page_frame.job_light


def push_light(jobs: dict, config: dict, last_push) -> dict:
    """The phone push has no run of its own, so its light is worked out from
    what is stored: Off when radar.yaml names no ntfy address or the watcher
    has never run, then the watcher's own Stale or Late, since a push goes
    only when the watcher runs, then Not sending while the watcher runs in dry
    run (it writes its pushes to a file), and otherwise On time."""
    watcher = jobs.get("signals") or {"state": "never"}
    v = {"state": watcher.get("state"), "mode": watcher.get("mode"),
         "last_run": watcher.get("last_run"), "hours_ago": watcher.get("hours_ago"),
         "late_hours": watcher.get("late_hours"), "stale_hours": watcher.get("stale_hours"),
         "last_push": last_push, "watched": watcher.get("watched", True)}
    if not (str(config.get("ntfy_url") or "").strip()
            and str(config.get("ntfy_topic_radar") or "").strip()):
        return {"name": "push", "word": "Off", "verdict": v, "state": "never",
                "why": "radar.yaml names no ntfy address, so nothing can be pushed."}
    if (watcher.get("state") or "never") == "never":
        return {"name": "push", "word": "Off", "verdict": v, "state": "never",
                "why": "The news watcher has never run, so nothing has been pushed."}
    # The watcher's state first: a push goes only when the watcher runs.
    light = job_light("signals", watcher)
    light.update(name="push", verdict=v)
    if light["word"] == "On time" and _dry("signals", watcher):
        light["word"] = "Not sending"
    return light


def lights(data: dict, config: dict, last_push) -> list:
    jobs = bd.job_verdicts(data)
    out = []
    for name, label in JOBS:
        light = (push_light(jobs, config, last_push) if name == "push"
                 else job_light(name, jobs.get(name)))
        light["label"] = label
        out.append(light)
    return out


def light_html(light: dict) -> str:
    """One light as a chip: a dot, the job's name, then its word. The word is
    always there, so the dot's colour never stands alone."""
    cls, _sym = LIGHTS[light["word"]]
    return (f'<li class="ht-strip-job" data-job="{light["name"]}">'
            f'<span class="ht-dot {cls}" aria-hidden="true"></span>'
            f'<span class="ht-job-name">{esc(light["label"])}</span>'
            '<span class="visually-hidden">, </span>'
            f'<span class="ht-light {cls}">{esc(light["word"])}</span></li>')


def lights_summary(lights: list) -> tuple[str, str]:
    """The phone's one chip for the five lights: their words counted, in the
    order the lights' words are listed ("3 on time, 2 not sending"), and the
    dot's class, the positive one only when every light is on time."""
    counts: dict = {}
    for light in lights:
        counts[light["word"]] = counts.get(light["word"], 0) + 1
    words = ", ".join(f"{counts[w]} {w.lower()}" for w in LIGHTS if w in counts)
    every = bool(lights) and all(l["word"] == "On time" for l in lights)
    return words, ("is-pos" if every else "is-neu")


# ================================================================ reads of its own

def _table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def done_map(conn) -> dict:
    """{item_key: done_at} for every item taken off Home. Empty before the
    table exists (a database no writer has opened since the deploy)."""
    if not _table(conn, "home_done"):
        return {}
    return {r[0]: r[1] for r in conn.execute("SELECT item_key, done_at FROM home_done")}


def last_runs(conn, workflow: str, limit: int = 3) -> list:
    have = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    outcome = "outcome" if "outcome" in have else "NULL AS outcome"
    return [dict(zip(("ts", "mode", "outcome", "note"), r)) for r in conn.execute(
        f"SELECT ts, mode, {outcome}, note FROM runs WHERE workflow = ?"
        " ORDER BY ts DESC, id DESC LIMIT ?", (workflow, limit))]


def companies(conn, ids) -> dict:
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r[0]: dict(zip(("id", "display_name", "what_they_build", "software_content",
                            "stage", "city", "country", "people"), r))
            for r in conn.execute(
                "SELECT id, display_name, what_they_build, software_content, stage, city,"
                f" country, people FROM companies WHERE id IN ({marks})", ids)}


def top_history(conn) -> list:
    """(opportunity id, when the score was made, when it was recorded) for every
    kept score that put a role in Top prospects. The role's own scores only:
    a tailored CV's score is not the role's."""
    if not _table(conn, "score_history"):
        return []
    return [tuple(r) for r in conn.execute(
        "SELECT opportunity_id, scored_at, recorded_at FROM score_history"
        " WHERE tier = 'top' AND event <> ?", (rc.TAILORED_EVENT,))]


# ================================================================ week by week

def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_model(data: dict, history: list, now: datetime) -> dict:
    """New Top prospects and signals at 40 or more, a Monday week at a time.

    Rebuilt from what is stored, so it is full on the first day:
    - A Top prospect is a role (its repost chain), counted once, in the week it
      was first scored Top: the earliest of every kept score in score_history
      that put it there (dated by the score's own scored_at; a score kept
      without one is dated by the advert's first sighting) and of the Top
      score its adverts hold now (their scored_at, else their first sighting).
      Every advert on file counts, acknowledged, closed and expired ones too,
      because the chart measures what arrived; a role applied for is left
      out, as the digest leaves it out.
    - A signal at 40 or more is a story (the reports sharing an event_key),
      counted once, in the week it was first found, when its best report
      scored 40 or more. Hidden stories count too; a hiring story (a buying
      window or a first hire) does not, as Insights leaves hiring out.
    Weeks are UK calendar weeks from Monday; this week runs Monday to now. The
    chart starts on the week Radar's first advert or signal arrived, so a week
    before Radar was listening is never drawn as a quiet one.
    """
    today = uk(now).date()
    this_monday = _monday(today)
    rows = list(data.get("arrivals") or []) + list(data.get("retired_roles") or [])
    applied = {jp.role_key(r) for r in rows if r.get("applied_at")}
    by_id = {r.get("id"): r for r in rows}
    first_top: dict = {}

    def note(key, iso) -> None:
        when = _when(iso)
        if when is not None and (key not in first_top or when < first_top[key]):
            first_top[key] = when

    for r in rows:
        if r.get("tier") == "top":
            note(jp.role_key(r), r.get("scored_at") or r.get("first_seen"))
    for oid, scored_at, _recorded in history:
        r = by_id.get(oid)
        if r is not None:
            note(jp.role_key(r), scored_at or r.get("first_seen"))
    tops: dict = {}
    for key, when in first_top.items():
        if key not in applied:
            wk = _monday(when.date())
            tops[wk] = tops.get(wk, 0) + 1

    stories: dict = {}
    for s in list(data.get("signals") or []) + list(data.get("dismissed_signals") or []):
        key = s.get("event_key") or f"e{s.get('id')}"
        found = _when(s.get("first_seen"))
        best = s.get("relevance") or 0
        kind = s.get("story_type") or CODE_TYPES.get(s.get("source_id")) or "other"
        have = stories.get(key)
        if have is None:
            stories[key] = [found, best, kind]
        else:
            if found and (have[0] is None or found < have[0]):
                have[0] = found
            if best > have[1]:
                have[1], have[2] = best, kind   # the best report says what the story is
    sigs: dict = {}
    for found, best, kind in stories.values():
        if found is not None and best >= SIGNAL_BAR and kind not in HIRING:
            wk = _monday(found.date())
            sigs[wk] = sigs.get(wk, 0) + 1

    starts = [d for d in (_when(r.get("first_seen")) for r in rows) if d]
    starts += [v[0] for v in stories.values() if v[0]]
    begun = _monday(min(starts).date()) if starts else this_monday
    first_week = begun >= this_monday
    weeks = []
    wk = max(begun, this_monday - timedelta(weeks=WEEKS - 1))
    while wk <= this_monday:
        weeks.append(wk)
        wk += timedelta(weeks=1)
    series = [{"week": w, "top": tops.get(w, 0), "signals": sigs.get(w, 0)} for w in weeks]
    last = series[-2] if len(series) > 1 else {"top": 0, "signals": 0}
    return {"weeks": series, "first_week": first_week,
            "starts": this_monday + timedelta(weeks=1),
            "this": series[-1], "last": last}


# ================================================================ the items

def _story_groups(data: dict) -> list:
    """The open stories: reports sharing an event_key, led by the best."""
    events: dict = {}
    for s in data.get("signals") or []:
        if s.get("relevance") is None:
            continue
        events.setdefault(s.get("event_key") or f"e{s.get('id')}", []).append(s)
    out = []
    for key, reports in events.items():
        ranked = sorted(reports, key=lambda r: (-(r.get("relevance") or 0),
                                                r.get("first_seen") or "", r.get("id") or 0))
        lead = ranked[0]
        kind = lead.get("story_type") or CODE_TYPES.get(lead.get("source_id")) or "other"
        out.append({"key": key, "lead": lead, "reports": reports, "type": kind,
                    "relevance": lead.get("relevance") or 0,
                    "found": min(r.get("first_seen") or "" for r in reports),
                    "closed": any(r.get("status") in ("actioned", "dead") for r in reports)})
    return out


def _source_name(source_id) -> str:
    if source_id in ("job-advert", "qa-tripwire"):
        return "a job advert"
    try:
        names = {s.get("id"): s.get("name") or s.get("id") for s in bd.load_watchlist()}
    except Exception:  # noqa: BLE001  a missing watchlist is a name in words
        names = {}
    if source_id in names:
        return str(names[source_id])
    words = str(source_id or "a source").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "A source"


def _role_meta(o: dict) -> str:
    bits = [o.get("company") or "Unknown company"]
    place = jp.location_text(o)
    if place:
        bits.append(place)
    if o.get("salary_rate"):
        bits.append(o["salary_rate"])
    word = jp.engagement_word(o)
    if word:
        bits.append(word)
    bits.append(jp.board(o.get("source")))
    return ", ".join(bits)


def _due_tag(due, now: datetime) -> tuple[str, str]:
    """(the tag's words, its class and mark) for a next step's due date."""
    d = ap.parse(due)
    if d is None:
        return "", ""
    gap = (d.date() - uk(now).date()).days
    if gap < 0:
        return f"Overdue by {_n(-gap, 'day')}", "caution"
    if gap == 0:
        return "Due today", "caution"
    return f"Due {day(due)}", "neutral"


def step_title(a: dict) -> str:
    """What waits, in words: Radar's Follow up names the company. An
    application with an interview and no step is on the tile's Waiting on you,
    as on the board, and never an item or a figure."""
    company = a.get("company") or "the company"
    step = a.get("next_step")
    if step == ap.FOLLOW_UP:
        return f"Follow up with {company}"
    if step:
        return step
    return f"Interview with {company}"


def build_items(data: dict, config: dict, conn, now: datetime, light_list: list,
                jmodel: dict, bmodel: dict, push_on: bool = False) -> list:
    """Every item Home would list, Done or not, most urgent group first."""
    items = []
    # ---- Radar: a job whose light is a fault (Late, Stale or Off).
    for light in light_list:
        if light["word"] in FAULTS:
            items.append(_radar_item(light, conn, config, now, push_on))
    # ---- Urgent signals: a story at the fast bar or over, found this week.
    fast = int(data.get("fast") or config.get("fast_signal_threshold", 75))
    since = uk(now) - timedelta(days=URGENT_DAYS)
    arrivals = {r.get("url_hash"): r for r in (data.get("arrivals") or []) if r.get("url_hash")}
    stories = [s for s in _story_groups(data)
               if s["relevance"] >= fast and not s["closed"]
               and (_when(s["found"]) or since) > since]
    stories.sort(key=lambda s: (-s["relevance"], "".join(chr(0x7e - ord(c)) for c in s["found"])))
    new_roles = {jp.role_key(o) for tier in ("top", "question")
                 for o in jmodel["tiers"][tier] if o.get("_unread")}
    for s in stories:
        item = _signal_item(s, arrivals, now)
        # A buying window read from an advert that is listed already, as a new
        # Top prospect or One question away role, is not listed twice.
        if item.get("role") is not None and jp.role_key(item["role"]) in new_roles:
            continue
        items.append(item)
    # ---- New Top prospects and One question away roles: read nowhere yet.
    for tier, group in (("top", "top"), ("question", "question")):
        for o in jmodel["tiers"][tier]:
            if o.get("_unread"):
                items.append(_role_item(o, group, now))
    # ---- Threads due: a Watchlist company's next step due today or overdue,
    # by the Watchlist's own rule, most overdue first.
    for t in threads_due(conn, now):
        items.append(_thread_item(t, now))
    # ---- Application follow-ups due today or overdue.
    today = uk(now).date()
    due = [a for a in bmodel["waiting"] if a.get("id") is not None and a.get("next_step")
           and ap.parse(a.get("next_step_due")) is not None
           and ap.parse(a["next_step_due"]).date() <= today]
    due.sort(key=lambda a: (str(a.get("next_step_due")), a.get("id") or 0))
    for a in due:
        items.append(_app_item(a, now))
    return items


def _radar_item(light: dict, conn, config: dict, now: datetime, push_on: bool) -> dict:
    name, word, v = light["name"], light["word"], light["verdict"]
    label = light["label"]
    hours = v.get("hours_ago")
    if word == "Stale":
        title = f"{label} is stale, {_hours(hours)} since its last run" if hours is not None \
            else f"{label} is stale: no run has finished"
        lead = (f"Last finished {_hours(hours)} ago. Stale after {_n(v.get('stale_hours') or 0, 'hour')}."
                if hours is not None else "Its runs have all failed.")
        tag = f"Stale, {_hours(hours)}" if hours is not None else "Stale"
    elif word == "Late":
        if light["state"] == "failing":
            title = f"{label} is late: its last run failed"
            lead = (f"The last run that finished was {_hours(hours)} ago. "
                    f"{_n(v.get('failed_runs') or 1, 'run')} failed since.")
        elif light["state"] == "partial":
            title = f"{label} is late: its last run finished with failures"
            lead = (f"Its last run, {_hours(hours)} ago, finished with failures"
                    + (f": {str(v.get('note'))[:200]}" if v.get("note") else ".")
                    if hours is not None else "Its last run finished with failures.")
        else:
            title = f"{label} is late, {_hours(hours)} since its last run"
            lead = (f"Last finished {_hours(hours)} ago. Late after "
                    f"{_n(v.get('late_hours') or 0, 'hour')}, stale after {_n(v.get('stale_hours') or 0, 'hour')}.")
        tag = "Late"
    else:
        title = f"{label} is off"
        lead = light.get("why") or f"The {label.lower()} has never run."
        tag = "Off"
    cls, sym = LIGHTS[word]
    tag_cls = {"is-neg": "negative", "is-cau": "caution", "is-neu": "neutral",
               "is-pos": "positive"}[cls]
    runs = last_runs(conn, "signals" if name == "push" else name)
    return {"kind": "radar", "group": "radar", "id": f"radar-{name}",
            # The episode is in the key: the last finished run, which a fault
            # keeps until a new run finishes. Done holds for this episode, and
            # the next fault is a new item.
            "key": f"radar:{name}:{word.lower()}:{v.get('last_run') or 'never'}",
            "title": title, "fig": "", "seen": seen_words(v.get("last_run"), now),
            "seen_label": "Last ran", "meta": SCHEDULE[name],
            "tag": f'<span class="tag tag-{tag_cls}">{_use(sym)}{esc(tag)}</span>',
            "lead": lead, "light": light, "runs": runs, "push_on": push_on,
            "company_id": None}


def _signal_item(s: dict, arrivals: dict, now: datetime) -> dict:
    lead = s["lead"]
    hiring = s["type"] in HIRING
    role = arrivals.get(lead.get("url_hash")) if lead.get("source_id") == "job-advert" else None
    company = lead.get("company") or "Unknown company"
    source = _source_name(lead.get("source_id"))
    if not hiring:
        open_ = (f"/insights/story/{int(lead['id'])}", "Open the story on Insights", False)
        stays = "The story stays as it is on Insights."
    elif role is not None:
        open_ = (f"/jobs/{int(role['id'])}", "Open the role on Jobs", False)
        stays = "The role stays as it is on Jobs, and hiring stories are not on Insights."
    elif lead.get("company_id"):
        open_ = (f"/company/{int(lead['company_id'])}", "Open the company's record", False)
        stays = "The company's record stays as it is."
    else:
        url = bd.safe_url(lead.get("source_url"))
        open_ = (url, "Open the source", True) if url else None
        stays = "Nothing else changes."
    return {"kind": "signal", "group": "urgent", "id": f"signal-{int(lead['id'])}",
            "key": f"signal:{s['key']}", "title": rc.clean_headline(lead.get("headline") or "Untitled"),
            "fig": str(s["relevance"]), "fig_label": "Relevance ",
            "seen": seen_words(s["found"], now), "seen_label": "Found ",
            "meta": f"{company}, read from {source}", "tag": "",
            "lead": lead.get("card_summary") or lead.get("why") or "",
            "story": s, "role": role, "open": open_, "stays": stays, "source": source,
            "company_id": lead.get("company_id"), "company": company}


def _role_item(o: dict, group: str, now: datetime) -> dict:
    cv = o.get("cv_match")
    return {"kind": "role", "group": group, "id": f"role-{int(o['id'])}",
            # The group is in the key, so a role Done as One question away
            # comes back when it becomes a Top prospect.
            "key": f"role:{group}:{jp.role_key(o)}", "title": o.get("title") or "Untitled role",
            "fig": f"{int(cv)}%" if cv is not None else "–", "fig_label": "CV match ",
            "seen": seen_words(jp.first_seen(o), now), "seen_label": "First seen ",
            "meta": _role_meta(o), "tag": jp.spec_tag(o) + jp.lifecycle_tag(o, rc.now_iso()),
            "lead": jp.lead_line(o), "role": o,
            "open": (f"/jobs/{int(o['id'])}", "Open the role on Jobs", False),
            "company_id": o.get("company_id"), "company": o.get("company")}


def threads_due(conn, now: datetime) -> list:
    """watchlist.threads_due on Home's read-only connection. A database the
    Watchlist's columns have not reached yet has no threads due."""
    try:
        return watchlist.threads_due(conn, now)
    except sqlite3.OperationalError:
        return []


def _thread_item(t: dict, now: datetime) -> dict:
    th = t["thread"]
    last = th.get("last") or {}
    cid = int(t["company_id"])
    words, cls = _due_tag(th["due_date"], now)
    channel = CHANNEL_WORDS.get(last.get("channel") or "other", "other")
    contact = f"Last contact {day(last.get('touched_at'))}, {channel}" if last.get("touched_at") else ""
    return {"kind": "thread", "group": "threads", "id": f"thread-{cid}",
            # The due date is in the key, so a thread Done today comes back
            # when its date is moved and it falls due again.
            "key": f"thread:{cid}:{th['due_date']}", "title": t["name"],
            "fig": "", "seen": day(th["due_date"]), "seen_label": "Due ",
            "meta": contact,
            "tag": (f'<span class="tag tag-{cls}">{_use("st-cau" if cls == "caution" else "st-neu")}'
                    f'{esc(words)}</span>') if words else "",
            "lead": th.get("next_step") or "", "thread": th, "place": t.get("place") or "",
            "open": (f"/watchlist?company={cid}", "Open it on the Watchlist", False),
            "company_id": cid, "company": t["name"]}


def _app_item(a: dict, now: datetime) -> dict:
    words, cls = _due_tag(a.get("next_step_due"), now)
    stage = a.get("stage") or "applied"
    applied_on = a.get("applied_on") or a.get("stage_at")
    stay = ap.days_between(a.get("stage_at"), now)
    lead = (f"Applied {day(applied_on)}. " if applied_on and stage != "preparing" else "") + \
        f"{_n(stay, 'day')} in {ap.STAGE_IN[stage]}."
    return {"kind": "app", "group": "apps", "id": f"app-{int(a['id'])}",
            "key": f"app:{int(a['id'])}:{a.get('next_step_due')}", "title": step_title(a),
            "fig": "", "seen": day(a.get("next_step_due")), "seen_label": "Due ",
            "meta": f"{a.get('title') or 'Untitled role'}, {ap.STAGE_WORDS[stage]}",
            "tag": (f'<span class="tag tag-{cls}">{_use("st-cau" if cls == "caution" else "st-neu")}'
                    f'{esc(words)}</span>') if words else "",
            "lead": lead, "app": a,
            "open": (f"/jobs?show=applied#app-{int(a['id'])}", "Open it on the board", False),
            "company_id": a.get("company_id"), "company": a.get("company")}


# ================================================================ the model

def build_model(conn, data: dict, config: dict, now=None, item: str | None = None,
                show: str = "todo", push_on: bool = False) -> dict:
    moment = _aware(now) if now else datetime.now(timezone.utc)
    show = "done" if show == "done" else "todo"
    last_push = None
    pushed = [s.get("pushed_at") for s in (data.get("signals") or []) + (data.get("dismissed_signals") or [])
              if s.get("pushed_at")]
    if pushed:
        last_push = max(pushed)
    light_list = lights(data, config, last_push)
    jmodel = jp.build_model(data, now=ap.iso(moment))
    bmodel = __import__("board_page").board_model(data, moment)
    items = build_items(data, config, conn, moment, light_list, jmodel, bmodel, push_on)
    done = done_map(conn)
    todo = [i for i in items if i["key"] not in done]
    gone = sorted((i for i in items if i["key"] in done), key=lambda i: done[i["key"]], reverse=True)
    # Done items are listed in the same groups as To do, so the list reads the
    # same way (sorted is stable, so each group keeps its own order).
    order = {g: n for n, (g, _w) in enumerate(GROUPS)}
    shown = sorted(gone if show == "done" else todo, key=lambda i: order[i["group"]])
    selected = next((i for i in shown if i["id"] == item), None)
    missing = item is not None and selected is None
    if selected is None and shown:
        selected = shown[0]
    counts = {g: sum(1 for i in todo if i["group"] == g) for g, _w in GROUPS}
    overdue = sum(1 for i in todo if i["group"] == "apps"
                  and ap.parse(i["app"].get("next_step_due")).date() < uk(moment).date())
    threads_overdue = sum(1 for i in todo if i["group"] == "threads"
                          and i["thread"]["state"] == "overdue")
    company_ids = {i.get("company_id") for i in shown if i.get("company_id")}
    return {
        "now": moment, "show": show, "items": shown, "todo": todo, "done": gone,
        "selected": selected, "missing": missing, "counts": counts, "overdue": overdue,
        "threads_overdue": threads_overdue,
        "lights": light_list, "jobs": bd.job_verdicts(data), "jmodel": jmodel, "bmodel": bmodel,
        "week": week_model(data, top_history(conn), moment),
        "name": rc.read_first_name(config), "data": data, "config": config,
        "companies": companies(conn, company_ids),
    }


# ================================================================ the welcome

# The five tiles: (group, one, many, where it links, icon, colour group).
FIGURES = (("urgent", "urgent story", "urgent stories", "#group-urgent", "story", "story"),
           ("top", tiers.NAMES["top"], tiers.NAMES["top"], "/jobs#group-top", "top", "jobs"),
           ("question", tiers.NAMES["question"], tiers.NAMES["question"], "/jobs#group-question",
            "question", "jobs"),
           # The Watchlist sorts its due follow-ups first.
           ("threads", "company follow-up due", "company follow-ups due", "/watchlist",
            "threads", "apps"),
           ("apps", "application follow-up due", "application follow-ups due",
            "/jobs?show=applied", "apps", "apps"))
JOB_PHRASES = {"inbox": "the inbox", "signals": "the news watcher", "check": "the advert checker",
               "digest": "the Monday digest", "push": "the phone push"}


def greeting(now: datetime, name: str | None) -> str:
    hour = uk(now).hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return f"Good {part}, {name}" if name else f"Good {part}."


def apps_word(model: dict, count: int) -> str:
    base = "application follow-up" if count == 1 else "application follow-ups"
    return f"{base} due today" if not model["overdue"] else f"{base} due or overdue"


def figure_counts(model: dict) -> dict:
    """The five tiles' numbers. Urgent stories and the two kinds of follow-up
    are their groups in Open actions; Top prospects and One question away are
    every open role in the tier, jobs_page.tier_totals, the same count the
    Jobs page puts on its tier bands, so the two pages cannot disagree."""
    c = model["counts"]
    totals = jp.tier_totals(model["jmodel"])
    return {"urgent": c["urgent"], "top": totals["top"], "question": totals["question"],
            "threads": c["threads"], "apps": c["apps"]}


def figures_html(model: dict) -> str:
    """The five tiles, each in its group's colour. One at 0 keeps the colour
    with a softer glow and its number muted."""
    c = figure_counts(model)
    out = []
    for key, one, many, href, icon, group in FIGURES:
        n = c[key]
        word = one if n == 1 else many
        if key == "apps":
            word = apps_word(model, n)
        out.append(f'<a class="ht-figlink glow-tile g-{group}{" is-zero" if not n else ""}" '
                   f'id="figure-{key}" href="{esc(href)}" data-group="{key}">{chip(icon, group)}'
                   f'<span class="ht-fignum">{n}</span>'
                   f'<span class="ht-figword">{esc(word)}</span></a>')
    return (f'<nav class="ht-figs ht-figs-5" aria-label="What needs you, by kind">'
            f'{"".join(out)}</nav>')


def health_panel_html(model: dict) -> str:
    """What opens under the lights: Week by week, the flow of the last
    fourteen days and board freshness, as /radar draws the last two but
    without their captions, then the way to the whole of Radar's health.
    Hidden until its button is pressed; the script opens and closes it."""
    data = model["data"]
    try:
        flow = bd.flow_widget(data, captions=False, tag="h3")
        boards = bd.freshness_widget(data, captions=False, tag="h3")
    except Exception:  # noqa: BLE001  a broken widget never costs Home
        flow = boards = ""
    return (f'<div class="ht-health" id="health-panel" hidden>{week_html(model)}'
            f'<div class="ht-health-widgets">{flow}{boards}</div>'
            '<p class="ht-health-foot"><a class="link link-block" href="/radar">'
            'Open the full health page</a></p></div>')


def welcome_html(model: dict) -> str:
    now = uk(model["now"])
    strip = "".join(light_html(l) for l in model["lights"])
    said, dot = lights_summary(model["lights"])
    # Under 700px the five chips fold behind one that counts them.
    summary = (f'<button type="button" class="ht-lights-sum" id="lights-btn" aria-expanded="false" '
               f'aria-controls="lights-list"><span class="ht-dot {dot}" aria-hidden="true"></span>'
               f'<span class="visually-hidden">Radar\'s five jobs: </span>{esc(said)}</button>')
    return (
        '<section class="ht-welcome" id="welcome" aria-labelledby="welcome-h">'
        '<div class="ht-welcome-in"><div class="ht-hello">'
        f'<p class="ht-date">{now:%A} {now.day} {now:%B}</p>'
        f'<h1 class="ht-greet" id="welcome-h"><span class="visually-hidden">Home. </span>'
        f'{esc(greeting(model["now"], model["name"]))}</h1></div>'
        f'<div class="ht-strip">{summary}<ul class="ht-strip-list" id="lights-list" '
        'aria-label="Radar\'s five jobs">'
        f'{strip}</ul><button type="button" class="ht-health-btn" id="health-btn" '
        'aria-expanded="false" aria-controls="health-panel">Radar\'s health</button></div>'
        f'</div>{health_panel_html(model)}</section>'
        f'{figures_html(model)}')


# ================================================================ the tiles

def apps_tile(model: dict) -> str:
    b = model["bmodel"]
    now = model["now"]
    total = sum(b["totals"].values())
    # The title sits over the card, as every section title on Home does.
    head = (f'<header class="sec-head">{chip("applications", "apps")}'
            '<h2 class="sec-title" id="tile-applications">Applications</h2></header>')
    if not total:
        body = '<div class="apm"><div class="empty"><p>No applications yet.</p></div></div>'
        return (f'<section class="ht-side ht-apps-tile" aria-labelledby="tile-applications">{head}'
                f'<div class="side-card glow-wide g-apps"><div class="ht-body">{body}</div></div></section>')
    cols = []
    said = []
    for stage in ap.STAGES:
        n = b["totals"][stage]
        said.append(f"{n} {ap.STAGE_WORDS[stage]}")
        pips = "".join(f'<span class="apm-pip{" is-closed" if stage == "closed" else ""}"></span>'
                       for _ in range(min(n, 24)))
        cols.append(f'<li class="apm-col"><a class="apm-link" id="mini-{stage}" '
                    f'href="/jobs?show=applied#col-{stage}"><span class="apm-name">'
                    f'<span class="apm-label">{ap.STAGE_WORDS[stage]}</span></span>'
                    f'<span class="apm-pips" aria-hidden="true">{pips}</span>'
                    f'<span class="apm-count">{n}</span></a></li>')
    label = (f"Applications by column. {_n(total, 'application')}: "
             + ", ".join(said[:-1]) + " and " + said[-1] + ".")
    parts = [f'<ol class="apm-cols" aria-label="{esc(label)}">{"".join(cols)}</ol>']
    closed = b["totals"]["closed"]
    if closed:
        apps = model["data"].get("applications") or []
        by_radar = sum(1 for a in apps if a.get("stage") == "closed"
                       and a.get("closed_reason") == "no-reply" and a.get("stage_by") == ap.RADAR)
        bits = []
        for reason, word in (("no-reply", "no reply"), ("turned-down", "turned down"),
                             ("withdrawn", "withdrawn")):
            n = b["reasons"][reason]
            if n:
                extra = f" ({by_radar} closed by Radar)" if reason == "no-reply" and by_radar else ""
                bits.append(f"{n} {word}{extra}")
        if bits:
            words = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
            parts.append(f'<p class="apm-split">Closed: {esc(words)}.</p>')
    waiting = b["waiting"]
    if waiting:
        lis = []
        for a in waiting[:WAIT_SHOWN]:
            href = f"/jobs?show=applied#app-{a['id']}" if a.get("id") is not None else "/jobs?show=applied"
            if a.get("next_step"):
                d = ap.parse(a.get("next_step_due"))
                gap = (d.date() - uk(now).date()).days if d else 99
                words = __import__("board_page").due_words(a.get("next_step_due"), uk(now))
            else:
                at = ap.parse(a.get("interview_at"))
                gap = (at.date() - uk(now).date()).days if at else 99
                words = __import__("board_page").interview_words(a.get("interview_at"))
            cls, sym = ("is-cau", "st-cau") if gap <= 1 else ("is-neu", "st-neu")
            status = (f'<span class="status {cls}">{_use(sym)}{esc(words)}</span>'
                      if words else "")
            lis.append(f'<li><a class="link" href="{esc(href)}">{esc(step_title(a))}</a>'
                       f'{status}</li>')
        more = len(waiting) - WAIT_SHOWN
        if more > 0:
            lis.append(f'<li class="apm-more">and {more} more on the board</li>')
        parts.append(f'<div><h3 class="apm-wait-head" id="mini-waiting">Waiting on you '
                     f'<span class="apm-wn">{len(waiting)}</span></h3>'
                     f'<ul class="apm-wait" aria-labelledby="mini-waiting">{"".join(lis)}</ul></div>')
    else:
        parts.append('<p class="apm-split">Nothing is waiting on you.</p>')
    return (f'<section class="ht-side ht-apps-tile" aria-labelledby="tile-applications">{head}'
            f'<div class="side-card glow-wide g-apps"><div class="ht-body"><div class="apm">{"".join(parts)}</div></div>'
            '<p class="ht-foot"><a class="link link-block" href="/jobs?show=applied">Open the board</a>'
            '</p></div></section>')


def jobs_tile(model: dict) -> str:
    """Jobs (until 17 Nov, Open roles): Top prospects, One question away and
    Reading, each its tier's pill and its total, jobs_page.tier_totals, the
    number on the Jobs page's tier bands. Each row links to its tier on Jobs,
    and See all to the Jobs page."""
    totals = jp.tier_totals(model["jmodel"])
    hrefs = {"top": "/jobs#group-top", "question": "/jobs#group-question",
             "reading": "/jobs?reading=1#group-reading"}
    lis = "".join(
        f'<li><a class="hj-row" id="funnel-{key}" href="{esc(hrefs[key])}">{tiers.pill_html(key)}'
        f'<span class="visually-hidden">, </span><span class="hj-fig">{totals[key]}</span></a></li>'
        for key in tiers.OPEN_TIERS)
    return ('<section class="ht-side ht-jobs-tile" aria-labelledby="tile-jobs">'
            f'<header class="sec-head">{chip("jobs", "jobs")}<h2 class="sec-title" id="tile-jobs">Jobs</h2>'
            '<a class="link sec-link" href="/jobs">See all<span class="visually-hidden"> on Jobs</span></a>'
            '</header><div class="side-card glow-wide g-jobs">'
            f'<ol class="hj-list" aria-label="Every open role by tier, each a link to its tier on Jobs">{lis}</ol>'
            '</div></section>')


def week_html(model: dict) -> str:
    """Week by week, in the health panel: new Top prospects and signals at 40
    or more a week (week_model says exactly what each bar counts), the This
    week line, and the way to each."""
    w = model["week"]
    head = '<h2 class="ht-title" id="tile-week-by-week">Week by week</h2>'
    if w["first_week"]:
        this = w["this"]
        body = ('<div class="ht-firsts">'
                f'<p class="ht-first"><span class="ht-first-fig"><span class="ht-swatch ht-swatch-1" '
                f'aria-hidden="true"></span>{this["top"]}</span><span class="ht-first-say">'
                f'{"Top prospect" if this["top"] == 1 else "Top prospects"} this week so far</span></p>'
                f'<p class="ht-first"><span class="ht-first-fig"><span class="ht-swatch ht-swatch-2" '
                f'aria-hidden="true"></span>{this["signals"]}</span><span class="ht-first-say">'
                f'{"signal" if this["signals"] == 1 else "signals"} at 40 or more this week so far</span></p>'
                '</div>')
    else:
        weeks = w["weeks"]
        n = len(weeks)

        def plot(series: str, cls: str) -> str:
            top = max(x[series] for x in weeks) or 1
            cols = "".join(
                f'<span class="ht-col"><span class="ht-val">{x[series]}</span>'
                f'<span class="ht-bar {cls}{" is-zero" if not x[series] else ""}" '
                f'style="--f:{x[series] / top:.3f}"></span></span>' for x in weeks)
            return f'<div class="ht-plot" style="--n:{n}">{cols}</div>'

        def label(x, i):
            wk = x["week"]
            return f"{wk.day} {wk:%b}" + (" so far" if i == n - 1 else "")

        said_top = ", ".join(f"{label(x, i)} {x['top']}" for i, x in enumerate(weeks))
        said_sig = ", ".join(f"{label(x, i)} {x['signals']}" for i, x in enumerate(weeks))
        aria = (f"New Top prospects per week, weeks starting {said_top}. Stories at 40 or more "
                f"per week, {said_sig}. Each row has its own scale.")
        week_cells = "".join(
            f'<span class="ht-week{" is-now" if i == n - 1 else ""}">{x["week"].day} {x["week"]:%b}'
            + ('<small>so far</small>' if i == n - 1 else "") + '</span>'
            for i, x in enumerate(weeks))
        this, last = w["this"], w["last"]
        say = (f"This week {_n(this['top'], 'Top prospect')} (last week {last['top']}) and "
               f"{_n(this['signals'], 'signal')} at 40 or more (last week {last['signals']}).")
        body = (f'<div class="ht-trend" role="img" aria-label="{esc(aria)}">'
                '<div class="ht-series"><p class="ht-series-name"><span class="ht-swatch ht-swatch-1">'
                f'</span>Top prospects</p>{plot("top", "ht-bar-1")}</div>'
                '<div class="ht-series"><p class="ht-series-name"><span class="ht-swatch ht-swatch-2">'
                f'</span>Stories at 40 or more</p>{plot("signals", "ht-bar-2")}</div>'
                f'<div class="ht-weeks" style="--n:{n}" aria-hidden="true">{week_cells}</div></div>'
                f'<p class="ht-say">{esc(say)}</p>')
    return ('<section class="ht-week-block" aria-labelledby="tile-week-by-week">'
            f'{head}<div class="ht-body">{body}</div>'
            '<p class="ht-foot ht-links"><a class="link link-block" href="/jobs#group-top">Open Top prospects</a>'
            '<a class="link link-block" href="/insights">Open Insights</a></p></section>')


# ================================================================ Open actions

# ---- one item, in full

def _facts(rows) -> str:
    return '<dl class="facts">' + "".join(
        f'<dt>{esc(k)}</dt><dd>{v}</dd>' for k, v in rows if v) + "</dl>"


def _sec(hid: str, title: str, body: str) -> str:
    return (f'<section class="sec" aria-labelledby="{hid}"><h4 class="sec-h" id="{hid}">{esc(title)}</h4>'
            f'{body}</section>')


def _body_radar(i: dict, uid: str) -> str:
    light = i["light"]
    v = light["verdict"]
    paras = [f'<p class="ny-para">{esc(i["lead"])}</p>']
    if v.get("failed_note"):
        paras.append(f'<p class="ny-para">The last failed run said: {esc(str(v["failed_note"])[:300])}</p>')
    out = [_sec(f"{uid}-wrong", "What is wrong", "".join(paras))]
    runs = i["runs"]
    if runs:
        trs = "".join(
            f'<tr><td>{esc(day_time(r["ts"]))}</td><td>{esc((r["mode"] or "").replace("-", " ").capitalize())}</td>'
            f'<td>{esc(ENDED.get(r["outcome"] or "ok", "Finished"))}{": " + esc(str(r["note"])[:160]) if r.get("note") else ""}</td></tr>'
            for r in runs)
        name = JOB_PHRASES["signals" if i["light"]["name"] == "push" else i["light"]["name"]]
        out.append(f'<div class="ny-runs"><h4 class="sec-h" id="{uid}-runs">Last runs'
                   f'<span class="group-count"> {len(runs)}</span></h4>'
                   f'<div class="table-wrap" role="region" aria-labelledby="{uid}-runs" tabindex="0">'
                   f'<table class="table"><caption class="visually-hidden">The last runs of {esc(name)}</caption>'
                   '<thead><tr><th scope="col">Ran</th><th scope="col">Mode</th>'
                   f'<th scope="col">How it ended</th></tr></thead><tbody>{trs}</tbody></table></div></div>')
    return "".join(out)


def _context_radar(i: dict, uid: str) -> str:
    light = i["light"]
    v = light["verdict"]
    name = light["name"]
    limits = (f"Late after {_n(v.get('late_hours') or 0, 'hour')} without a run, stale after "
              f"{_n(v.get('stale_hours') or 0, 'hour')}." if v.get("late_hours") else "")
    rows = [("What it does", esc(DOES[name])), ("Runs", esc(SCHEDULE[name])),
            ("Limit", esc(limits)),
            ("Last ran", esc(day_time(v.get("last_run")) or "Never")),
            ("Mode", esc((v.get("mode") or "").replace("-", " ").capitalize()))]
    if name == "push":
        rows.append(("Last push", esc(day_time(v.get("last_push")) or "None on file")))
    if v.get("watched") is False:
        rows.append(("Radar's health", "Not watched by /health, so it never makes it degraded."))
    return _sec(f"{uid}-schedule", "Schedule", _facts(rows))


def role_row(i: dict, model: dict) -> str:
    """A role on Home is one short card in the Jobs purple: its chip, the Jobs
    row (jobs_page.row_html, the same function the Jobs queue draws with), its
    title the link to the role on Jobs, then its tier's pill and Done, and
    nothing else (owner, 17 Nov)."""
    uid = esc(i["id"])
    row = jp.row_html(i["role"], "open", None, ap.iso(model["now"]))
    tier = i.get("group") or i["role"].get("tier") or ""
    pill = tiers.pill_html(tier) if tier in tiers.OPEN_TIERS else ""
    return (f'<article class="ny-item ny-role glow-short g-jobs" id="{uid}" data-item="{uid}" '
            f'data-key="{esc(i["key"])}" aria-labelledby="rt-{int(i["role"]["id"])}" tabindex="-1">'
            f'{chip("jobs", "jobs")}<div class="ny-role-row">{row}</div>{_actions(i, model, pill)}</article>')


def _body_app(i: dict, uid: str, now: datetime) -> str:
    a = i["app"]
    board = __import__("board_page")
    who = ("Radar added this Follow up 7 days after you marked the role applied."
           if a.get("next_step_by") == ap.RADAR else "You set this step on the board.")
    paras = [f'<p class="ny-para">{esc(a.get("next_step") or "")}, '
             f'{esc(board.due_words(a.get("next_step_due"), uk(now)))}.</p>',
             f'<p class="text-meta ny-under">{esc(who)}</p>']
    if a.get("interview_at"):
        paras.append(f'<p class="ny-para">{esc(board.interview_words(a["interview_at"]))}.</p>')
    if ap.warned(a, now):
        on = ap.close_on(a)
        paras.append(f'<p class="ny-para">No movement for {_n(ap.still_days(a, now), "day")}. '
                     f'Radar closes it as no reply on {on.day} {on:%b} unless it moves.</p>'
                     if on else "")
    out = [_sec(f"{uid}-next", "Next step", "".join(paras))]
    stage = a.get("stage") or "applied"
    rows = [("Role", esc(a.get("title") or "Untitled role")),
            ("Company", esc(a.get("company") or "Unknown company")),
            ("Stage", esc(ap.STAGE_WORDS[stage])),
            ("Applied", esc(day(a.get("applied_on"))) if stage != "preparing" else ""),
            ("CV sent", esc(a.get("applied_cv") or "") if stage != "preparing" else "")]
    out.append(_sec(f"{uid}-details", "Details", _facts(rows)))
    return "".join(out)


def _body_thread(i: dict, uid: str, now: datetime) -> str:
    th = i["thread"]
    last = th.get("last") or {}
    words, _cls = _due_tag(th["due_date"], now)
    due = f"Due {day(th['due_date'])}"
    if words.startswith("Overdue"):
        due += ", o" + words[1:]
    elif words == "Due today":
        due += ", today"
    paras = [f'<p class="ny-para">{esc(th.get("next_step") or "")}</p>',
             f'<p class="text-meta ny-under">{esc(due)}. You set this step on the Watchlist.</p>']
    out = [_sec(f"{uid}-next", "Next step", "".join(paras))]
    channel = CHANNEL_WORDS.get(last.get("channel") or "other", "other")
    rows = [("Company", esc(i.get("company") or "")),
            ("Where", esc(i.get("place") or "")),
            ("Last contact", esc(f"{day(last.get('touched_at'))}, {channel}") if last.get("touched_at") else ""),
            ("Note", esc(last.get("note") or ""))]
    out.append(_sec(f"{uid}-details", "Details", _facts(rows)))
    return "".join(out)


def _context_company(i: dict, model: dict, uid: str) -> str:
    """What else is on file for the item's company: its thread, its signals and
    its roles, then the way to its record. The fixed fields (builds, software,
    stage, where, people) are on the company's own page, not here (17 Nov)."""
    cid = i.get("company_id")
    c = model["companies"].get(cid) if cid else None
    if not c:
        return ""
    name = c["display_name"]
    data = model["data"]
    parts = []
    thread = next((t for t in data.get("threads") or []
                   if (t.get("company_id") == cid) or str(t.get("company") or "").lower() == name.lower()),
                  None)
    if thread:
        when = []
        if thread.get("next_action_date"):
            when.append(f"Due {day(thread['next_action_date'])}")
        when.append(f"Last contact {day(thread.get('touched_at'))}, "
                    f"{CHANNEL_WORDS.get(thread.get('channel') or 'other', 'other')}")
        parts.append(f'<div class="ny-ctx-part"><h5 class="sec-h">Thread</h5>'
                     f'<p class="ny-para">{esc(thread.get("next_action"))}</p>'
                     f'<p class="text-meta ny-under">{esc(", ".join(when))}.</p></div>')
    stories = [s for s in _story_groups({"signals": (data.get("signals") or [])})
               if s["lead"].get("company_id") == cid]
    stories.sort(key=lambda s: s["found"], reverse=True)
    if stories:
        rows = []
        for s in stories[:3]:
            lead = s["lead"]
            words = TYPE_WORDS.get(s["type"], "Other news")
            inner = (f'<span class="c1"><span class="c-title">{esc(rc.clean_headline(lead.get("headline") or ""))}</span>'
                     f'<span class="c-fig"><span class="visually-hidden">Relevance </span>{s["relevance"]}</span>'
                     f'<span class="c-seen"><span class="visually-hidden">Found </span>{esc(day(s["found"]))}</span></span>'
                     f'<span class="c2"><span class="c-meta">{esc(words)}</span></span>')
            if s["type"] in HIRING:
                rows.append(f'<div class="ctx-row is-static">{inner}</div>')
            else:
                rows.append(f'<a class="ctx-row" href="/insights/story/{int(lead["id"])}">{inner}</a>')
        parts.append(f'<div class="ny-ctx-part"><h5 class="sec-h">Stories on file'
                     f'<span class="group-count"> {len(stories)}</span></h5>{"".join(rows)}</div>')
    roles = [o for o in (model["jmodel"]["all_lists"]["open"] + model["jmodel"]["all_lists"]["acknowledged"])
             if o.get("company_id") == cid]
    if roles:
        rows = []
        for o in roles[:3]:
            cv = o.get("cv_match")
            bits = [jp.TIER_WORDS.get(o.get("tier") or "", "Not scored")]
            if jp.location_text(o):
                bits.append(jp.location_text(o))
            if o.get("salary_rate"):
                bits.append(o["salary_rate"])
            rows.append(f'<a class="ctx-row" href="/jobs/{int(o["id"])}"><span class="c1">'
                        f'<span class="c-title">{esc(o.get("title") or "Untitled role")}</span>'
                        f'<span class="c-fig"><span class="visually-hidden">CV match </span>'
                        f'{f"{int(cv)}%" if cv is not None else "–"}</span></span>'
                        f'<span class="c2"><span class="c-meta">{esc(", ".join(bits))}</span></span></a>')
        parts.append(f'<div class="ny-ctx-part"><h5 class="sec-h">Roles on Jobs'
                     f'<span class="group-count"> {len(roles)}</span></h5>{"".join(rows)}</div>')
    if not parts:
        return ""
    return (f'<section class="sec ny-context" aria-labelledby="{uid}-file">'
            f'<h4 class="sec-h" id="{uid}-file">On file for {esc(name)}</h4>{"".join(parts)}</section>')


def _on_file_link(i: dict, model: dict) -> str:
    cid = i.get("company_id")
    c = model["companies"].get(cid) if cid else None
    if not c:
        return ""
    return (f'<a class="link link-block ny-onfile" href="/company/{int(cid)}">'
            f'Everything on file<span class="visually-hidden"> for {esc(c["display_name"])}</span></a>')


def _actions(i: dict, model: dict, lead: str = "") -> str:
    """Open where the item lives (a story's card has its headline for that,
    a role's row its title), Done or Put back, and Check now for the news
    watcher, after lead (a story's links, a role's tier pill). No Next: every
    item is on screen."""
    done_view = model["show"] == "done"
    acts = [lead] if lead else []
    if i["kind"] == "radar":
        n8n = bd.safe_url(model["config"].get("n8n_url"))
        open_ = ((n8n, "Open the workflow scheduler", True) if n8n
                 else ("/radar", "Open Radar's health", False))
    elif i["kind"] in ("signal", "role"):
        open_ = None
    else:
        open_ = i.get("open")
    if open_:
        href, label, external = open_
        ext = (' target="_blank" rel="noopener noreferrer"' if external else "")
        tail = '<span class="visually-hidden"> (opens in a new tab)</span>' if external else ""
        # An underlined link, as a story's Read the announcement is: teal
        # means Applications on Home now, never a link (owner, 17 Nov).
        acts.append(f'<a class="link" href="{esc(href)}" data-open{ext}>{esc(label)}{tail}</a>')
    if done_view:
        acts.append(f'<button type="button" class="btn btn-secondary" data-back="{esc(i["key"])}" '
                    f'data-title="{esc(i["title"])}">Put back on Home</button>')
    else:
        acts.append(f'<button type="button" class="btn btn-secondary" data-done="{esc(i["key"])}" '
                    f'data-title="{esc(i["title"])}">Done</button>')
    if i["kind"] == "radar" and i["light"]["name"] == "signals" and not done_view:
        acts.append('<button type="button" class="btn btn-secondary" data-check>Check now</button>')
    return f'<div class="action-bar ny-acts">{"".join(acts)}</div>'


def _signal_href(i: dict) -> tuple[str, bool]:
    """Where a signal's headline goes: the story on Insights, or for a hiring
    story (not on Insights) its role on Jobs, its company, or its source."""
    open_ = i.get("open")
    if not open_:
        return "", False
    return open_[0], open_[2]


def signal_card(i: dict, model: dict) -> str:
    """An urgent signal as Insights draws a story (insights_page.card):
    company and source, the headline, the line on why it matters, its next
    move (the playbook step), the announcement, and everything on file. Laid
    out without separators, with Done in place of Insights' own actions, and
    without the relevance and found date (owner, 17 Nov)."""
    s = i["story"]
    lead = s["lead"]
    uid = esc(i["id"])
    href, external = _signal_href(i)
    ext = ' target="_blank" rel="noopener noreferrer"' if external else ""
    tail = '<span class="visually-hidden"> (opens in a new tab)</span>' if external else ""
    title = esc(i["title"])
    headline = f'<a href="{esc(href)}"{ext}>{title}{tail}</a>' if href else title
    more = ""
    if len(s["reports"]) > 1:
        n = len(s["reports"]) - 1
        more = f' and {n} more source{"s" if n > 1 else ""}'
    url = bd.safe_url(lead.get("source_url"))
    what = "Read the advert" if lead.get("source_id") in ("job-advert", "qa-tripwire") else "Read the announcement"
    announce = (f'<a class="link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">{what}'
                f'<span class="visually-hidden"> on {esc(i["source"])} (opens in a new tab)</span>'
                f'{ip.ICON_OUT}</a>' if url else "")
    step = lead.get("playbook_step")
    move = (f'<section class="ny-move" aria-labelledby="{uid}-move"><h4 class="sec-h" id="{uid}-move">'
            f'Next move</h4><p class="ny-para">{esc(step)}</p></section>' if step else "")
    return (f'<article class="ny-item ny-story glow-wide g-story" id="{uid}" data-item="{uid}" '
            f'data-key="{esc(i["key"])}" aria-labelledby="{uid}-t" tabindex="-1">'
            f'<p class="story-line">{chip("story", "story")}<span class="story-kind">Urgent story</span>'
            '<span class="fact-rule" aria-hidden="true"></span><span class="visually-hidden">, </span>'
            f'<span class="story-src"><span class="co">{esc(i.get("company") or "Unknown company")}</span>, '
            f'via {esc(i["source"])}{esc(more)}</span></p>'
            f'<h3 class="story-h" id="{uid}-t">{headline}</h3>'
            f'<p class="story-sum">{esc(i.get("lead") or "")}</p>{move}'
            f'{_actions(i, model, announce + _on_file_link(i, model))}</article>')


def item_html(i: dict, model: dict) -> str:
    """One item in full: its title, what it is, its tags, the body, what else
    is on file, and its controls."""
    if i["kind"] == "signal":
        return signal_card(i, model)
    if i["kind"] == "role":
        return role_row(i, model)
    uid = esc(i["id"])
    meta_bits = {"radar": ["Radar", SCHEDULE[i["light"]["name"]] if i["kind"] == "radar" else ""],
                 "role": [i.get("company") or "",
                          f"First seen {day_time(jp.first_seen(i['role']))}" if i.get("role") else ""],
                 "thread": [i.get("place") or ""],
                 "app": [i.get("company") or "",
                         ap.STAGE_WORDS.get((i.get("app") or {}).get("stage") or "applied", "")]}[i["kind"]]
    meta = "".join(f"<span>{esc(b)}</span>" for b in meta_bits if b)
    tags = i.get("tag") or ""
    if i["kind"] == "radar":
        body = _body_radar(i, uid) + _context_radar(i, uid)
    elif i["kind"] == "thread":
        body = _body_thread(i, uid, model["now"]) + _context_company(i, model, uid)
    else:
        body = _body_app(i, uid, model["now"]) + _context_company(i, model, uid)
    # A follow-up is teal, with a glow; a job of Radar's that needs a look
    # takes a neutral chip and no glow, as it belongs to no group.
    look = {"thread": ("threads", "apps", " ny-follow glow-wide g-apps"),
            "app": ("apps", "apps", " ny-follow glow-wide g-apps"),
            "radar": ("radar", "neutral", " ny-radar")}[i["kind"]]
    return (f'<article class="ny-item{look[2]}" id="{uid}" data-item="{uid}" data-key="{esc(i["key"])}" '
            f'aria-labelledby="{uid}-t" tabindex="-1">'
            f'<header class="ny-ih">{chip(look[0], look[1])}<div class="ny-ih-text">'
            f'<h3 class="ny-it" id="{uid}-t">{esc(i["title"])}</h3>'
            f'<p class="ny-im">{meta}{tags}</p></div></header>'
            f'<div class="ny-ib">{body}</div>{_on_file_link(i, model)}{_actions(i, model)}</article>')


def list_html(model: dict) -> str:
    items = model["items"]
    parts = []
    for group, _word in GROUPS:
        members = [i for i in items if i["group"] == group]
        if not members:
            continue
        body = "".join(item_html(i, model) for i in members)
        # No subheading (owner, 17 Nov): each card carries its kind. The
        # block keeps the group's address, which the tiles link to.
        parts.append(f'<div class="ny-group" id="group-{group}">{body}</div>')
    return "".join(parts)


def empty_html(model: dict) -> str:
    if model["show"] == "done":
        return '<div class="empty"><p>Nothing done.</p></div>'
    return '<div class="empty"><p>Nothing today.</p></div>'


def sec_count(n: int, one: str, many: str) -> str:
    """A section title's count pill, read as ", 3 items"."""
    return (f'<span class="sec-count"><span class="visually-hidden">, </span>{n}'
            f'<span class="visually-hidden"> {one if n == 1 else many}</span></span>')


def needs_html(model: dict) -> str:
    """Open actions: the title row (its chip, the title, the count of items to
    do, then To do, Done and Undo last), then every item as its own card."""
    items = model["items"]
    done_view = model["show"] == "done"
    title = "Done on Home" if done_view else "Open actions"
    count = sec_count(len(items), "item", "items") if items else ""
    n_done = len(model["done"])
    seg = ('<nav class="segmented" aria-label="Show">'
           f'<a class="seg" href="/"{" aria-current=" + chr(34) + "page" + chr(34) if not done_view else ""}>To do</a>'
           f'<a class="seg" href="/?show=done"{" aria-current=" + chr(34) + "page" + chr(34) if done_view else ""}>'
           f'Done<span class="visually-hidden">, {n_done} {"item" if n_done == 1 else "items"}</span></a></nav>')
    head = (f'<header class="sec-head ny-head">{chip("actions", "neutral")}'
            f'<h2 class="sec-title ny-title" id="ny-title">{title}{count}</h2>'
            f'<div class="ny-tools">{seg}<button type="button" class="btn btn-ghost undo-last" '
            'id="undo-last" disabled>Undo last</button></div></header>')
    # An address for an item that has gone says so, on an empty Home as well.
    notice = ('<p class="pane-note ny-gone" role="status">That item is no longer on Home: it was '
              'done, or it no longer needs you.</p>' if model["missing"] else "")
    if not items:
        return (f'<section class="ny ny-is-empty" id="ny" aria-labelledby="ny-title">{head}'
                f'<div class="ny-body">{notice}{empty_html(model)}</div></section>')
    return (f'<section class="ny" id="ny" aria-labelledby="ny-title">{head}'
            f'<div class="ny-body" id="ny-list">{notice}{list_html(model)}</div></section>')


def main_html(model: dict) -> str:
    # Two columns under the tiles, Open actions and then Jobs and
    # Applications; on a phone they stack in that order.
    return (f'{welcome_html(model)}<div class="hf-cols"><div class="hf-main">{needs_html(model)}</div>'
            f'<div class="hf-side">{jobs_tile(model)}{apps_tile(model)}</div></div>')


def page_title(model: dict) -> str:
    return "Home, MedTech Radar" if model["show"] == "todo" else "Done on Home, MedTech Radar"


def render(conn, data: dict, config: dict, item: str | None = None, show: str = "todo",
           now=None, push_on: bool = False) -> str:
    """The whole page, from one collect() and the reads above."""
    model = build_model(conn, data, config, now, item, show, push_on)
    state = {"show": model["show"], "item": (model["selected"] or {}).get("id"),
             "target": item if item and not model["missing"] else None}
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(page_title(model))}</title>
<link rel="icon" href="{bd.FAVICON}">
<style>{ip.FONT_CSS}{page_frame.FRAME_CSS}{jp.ROW_CSS}{jp.ROW_NARROW_CSS}{tiers.PILL_CSS}{HOME_CSS}</style>
</head>
<body>
<div class="app" data-page="home" data-state="{esc(json.dumps(state))}">
{jp.SYMBOLS}{SYMBOLS}
{page_frame.appbar_html("home", model["jobs"], "Home")}
<main class="content hf" id="main">{main_html(model)}</main>
<p class="toast" id="toast" role="status" aria-live="polite"></p>
</div>
<script>{bd.SCRIPT}{page_frame.FRAME_SCRIPT}{HOME_SCRIPT}</script>
</body>
</html>"""


def main_payload(conn, data: dict, config: dict, item: str | None = None, show: str = "todo",
                 now=None, push_on: bool = False) -> dict:
    """The page's main element alone, for the script to put in place after Done
    or Undo last, with the selection and the title it now has."""
    model = build_model(conn, data, config, now, item, show, push_on)
    # After a write the script asks for the item next to the one it changed,
    # so an item that is not there is not news.
    model["missing"] = False
    return {"html": main_html(model), "title": page_title(model),
            "item": (model["selected"] or {}).get("id"), "count": len(model["items"])}


# ================================================================ Done

def clean_key(value) -> str:
    """An item key as the routes take it, or raises ValueError: one printable
    line, at most KEY_MAX characters, naming one of the five kinds."""
    if not isinstance(value, str):
        raise ValueError("key must be text")
    key = value.strip()
    if not key or len(key) > KEY_MAX or not key.isprintable() or not KEY_RE.match(key):
        raise ValueError("key must name a Home item, like role:..., signal:..., thread:..., app:... "
                         "or radar:...")
    return key


def mark_done(conn, key: str, title=None, now: str | None = None) -> dict:
    """Take one item off Home. Commits. Refused when it is off already."""
    title = " ".join(str(title or "").split())[:KEY_MAX] or None
    cur = conn.execute("INSERT OR IGNORE INTO home_done (item_key, done_at, title) VALUES (?,?,?)",
                       (key, now or rc.now_iso(), title))
    conn.commit()
    if not cur.rowcount:
        return {"ok": False, "note": "That item is off Home already. Reload to see where it is."}
    return {"ok": True, "key": key, "done": True}


def mark_undone(conn, key: str) -> dict:
    """Put one item back on Home. Commits. Refused when it is not off Home."""
    cur = conn.execute("DELETE FROM home_done WHERE item_key = ?", (key,))
    conn.commit()
    if not cur.rowcount:
        return {"ok": False, "note": "That item is on Home already. Reload to see it."}
    return {"ok": True, "key": key, "done": False}


# ================================================================ styles

HOME_CSS = """
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--canvas)}
html body{margin:0;background:var(--canvas);color:var(--text-1);font-family:var(--font-sans);font-size:14px;line-height:20px;font-weight:400;letter-spacing:0;-webkit-font-smoothing:antialiased}
h1,h2,h3,h4,h5,h6,p,ul,ol,dl,dd{margin:0}
ul,ol{list-style:none;padding:0}
button,input{font:inherit;color:inherit;letter-spacing:0}
a{color:inherit}
:root{--font-serif:"IBM Plex Serif",Georgia,"Times New Roman",serif;--ht-surface:#1a1d1f;--ht-line:#2b3135;--ht-track:#252a2d;--ht-sunk:#15181a;--ht-series-1:#2aa1b3;--ht-series-2:#9a80ea;--link-line:#5b6770}
.ctx-row:focus-visible{outline-offset:-7px;box-shadow:none}
/* The page scrolls as one document at every width: Needs you today shows every item in full. */
.app{container:app / inline-size;display:grid;grid-template-rows:var(--bar-h) auto;min-height:100vh;align-content:start;background:var(--canvas)}
.content{grid-row:2;min-width:0}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:32px;padding:0 12px;border:1px solid transparent;border-radius:var(--radius-2);font-size:14px;line-height:20px;font-weight:500;white-space:nowrap;text-decoration:none;cursor:pointer;transition:background-color var(--dur) ease-out,color var(--dur) ease-out,border-color var(--dur) ease-out}
.btn-secondary{background:var(--surface);border-color:var(--border-strong);color:var(--text-1)}
.btn-secondary:hover{background:var(--row-hover)}
.btn-ghost{background:transparent;color:var(--text-2)}
.btn-ghost:hover{color:var(--text-1);text-decoration:underline;text-underline-offset:2px}
.btn.is-disabled,.btn:disabled{color:var(--text-3);border-color:var(--border-subtle);cursor:default;text-decoration:none}
.btn[aria-busy="true"]{cursor:progress}
/* Links on Home (owner, 17 Nov): the main text colour, underlined in a
   quiet grey that turns to the text colour on hover. Never teal, which
   means Applications here. */
.link{color:var(--text-1);text-decoration:underline;text-decoration-color:var(--link-line);text-decoration-thickness:1px;text-underline-offset:3px;transition:text-decoration-color var(--dur) ease-out}
.link:hover{color:var(--text-1);text-decoration-color:var(--text-1)}
.link-block{display:inline-flex;align-items:center;min-height:24px}
.tag{display:inline-flex;align-items:center;gap:4px;flex:none;height:20px;padding:0 8px;border-radius:var(--radius-2);font-size:12px;line-height:16px;font-weight:600;white-space:nowrap}
.tag-neutral{background:var(--neutral-tint);color:var(--neutral)}
.tag-caution{background:var(--caution-tint);color:var(--caution)}
.tag-negative{background:var(--negative-tint);color:var(--negative)}
.tag-positive{background:var(--positive-tint);color:var(--positive)}
.is-pos{color:var(--positive)}.is-neg{color:var(--negative)}.is-neu{color:var(--neutral)}.is-cau{color:var(--caution)}
.status{display:inline-flex;align-items:flex-start;gap:4px;max-width:100%;font-size:12px;line-height:16px;font-weight:600;white-space:normal;overflow-wrap:anywhere}
.status > svg{flex:none;margin-top:2px}
.text-label{font-size:12px;line-height:16px;font-weight:600}
.text-meta{font-size:12px;line-height:16px;font-weight:400;color:var(--text-3)}
.text-figure-large{font-size:20px;line-height:28px;font-weight:600;font-variant-numeric:tabular-nums}
.ink-2{color:var(--text-2)}
.action-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.segmented{display:inline-flex;flex:none;gap:4px}
.seg{display:inline-flex;align-items:center;justify-content:center;min-width:32px;height:32px;padding:0 12px;border:1px solid transparent;border-radius:16px;background:transparent;color:var(--text-2);font-size:13px;line-height:20px;font-weight:500;text-decoration:none}
.seg:hover{color:var(--text-1)}
.seg[aria-current]{background:#1a2025;border-color:#2a3238;color:var(--text-1)}
.pane-note{display:flex;align-items:center;min-height:32px;padding:8px 16px 7px;font-size:12px;line-height:16px;color:var(--text-2);border-bottom:1px solid var(--border-subtle)}
.group-title{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;min-width:0;font-size:16px;line-height:24px;font-weight:600}
.group-count{font-size:12px;line-height:16px;font-weight:400;color:var(--text-3);font-variant-numeric:tabular-nums}
.ctx-row{position:relative;display:grid;grid-template-rows:20px 16px;row-gap:4px;height:56px;padding:8px 0 7px;border-bottom:1px solid var(--border-subtle);color:var(--text-1);text-decoration:none;transition:background-color var(--dur) ease-out}
.ctx-row:hover{background:var(--row-hover)}
.ctx-row.is-static:hover{background:transparent}
.c1,.c2{display:flex;min-width:0}
.c1{align-items:baseline;gap:8px}
.c-title{flex:1 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-weight:500}
.c-fig{flex:none;width:40px;text-align:right;font-weight:500;font-variant-numeric:tabular-nums}
.c-seen{flex:none;min-width:44px;text-align:right;font-size:12px;line-height:16px;color:var(--text-3);font-variant-numeric:tabular-nums;white-space:nowrap}
.c2{align-items:center;gap:8px}
.c-meta{flex:0 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:12px;line-height:16px;color:var(--text-3)}
.sec-h{margin-bottom:8px;font-size:14px;line-height:20px;font-weight:600;color:var(--text-1)}
.sec-h .group-count{margin-left:4px}
.facts{display:grid;grid-template-columns:160px minmax(0,1fr);gap:8px 16px;align-items:baseline}
.facts dt{font-size:12px;line-height:16px;font-weight:600;color:var(--text-2)}
.facts dd{max-width:80ch;overflow-wrap:anywhere}
.table-wrap{min-width:0;overflow-x:auto;overscroll-behavior-x:contain}
.table{width:100%;border-collapse:separate;border-spacing:0}
.table th,.table td{height:40px;padding:10px 12px;text-align:left;vertical-align:middle;border-bottom:1px solid var(--border-subtle)}
.table th:first-child,.table td:first-child{padding-left:8px;white-space:nowrap;width:136px}
.table th:nth-child(2),.table td:nth-child(2){white-space:nowrap;width:96px}
.table thead th{height:40px;padding-top:0;padding-bottom:0;background:var(--surface-sunken);border-bottom-color:var(--border-strong);font-size:12px;line-height:16px;font-weight:600;color:var(--text-3)}
.empty{padding:16px}
.empty > p{max-width:72ch}
.toast{position:fixed;left:50%;bottom:16px;z-index:30;transform:translateX(-50%);display:flex;align-items:center;gap:12px;max-width:min(560px,calc(100vw - 32px));padding:8px 8px 8px 16px;background:var(--surface-sunken);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay)}
.toast:empty{display:none}
.toast.is-bad{border-color:var(--negative)}
.toast.is-bad::before{content:"!";display:inline-flex;align-items:center;justify-content:center;flex:none;width:16px;height:16px;border-radius:50%;background:var(--negative);color:var(--on-accent);font-weight:700;font-size:12px}
/* ---- the groups (owner, 17 Nov): each has one colour, page_frame's
   --group-*, for its icon chip and a soft glow from the top left of its
   cards. The glow is background only, over the card's own ground. ---- */
.g-story{--g:var(--group-story-rgb);--gc:var(--group-story)}
.g-jobs{--g:var(--group-jobs-rgb);--gc:var(--group-jobs)}
.g-apps{--g:var(--group-apps-rgb);--gc:var(--group-apps)}
.g-neutral{--g:232 237 240;--gc:var(--text-1)}
.gchip{display:inline-flex;align-items:center;justify-content:center;flex:none;width:32px;height:32px;border-radius:8px;background:rgb(var(--g) / .16);color:var(--gc)}
.gchip.g-neutral{background:rgb(var(--g) / .10)}
.gchip svg{display:block}
.glow-tile{background-image:radial-gradient(150% 170% at 0% 0%,rgb(var(--g) / .30) 0%,rgb(var(--g) / .11) 45%,rgb(var(--g) / 0) 100%)}
.glow-tile.is-zero{background-image:radial-gradient(150% 170% at 0% 0%,rgb(var(--g) / .16) 0%,rgb(var(--g) / .06) 45%,rgb(var(--g) / 0) 100%)}
.glow-wide{background-image:radial-gradient(115% 160% at 0% 0%,rgb(var(--g) / .20) 0%,rgb(var(--g) / .08) 45%,rgb(var(--g) / 0) 100%)}
.glow-short{background-image:radial-gradient(80% 260% at 0% 0%,rgb(var(--g) / .22) 0%,rgb(var(--g) / .08) 45%,rgb(var(--g) / 0) 100%)}
@media (forced-colors:active){.gchip{border:1px solid CanvasText}}
/* ---- the tiles ---- */
/* The right column: Jobs, then Applications, each a card under its title. */
.ht-side{min-width:0}
.side-card{container:side / inline-size;display:flex;flex-direction:column;gap:16px;min-width:0;padding:20px;background-color:var(--surface);border:1px solid var(--border-subtle);border-radius:14px;color:var(--text-1);--focus-gap:var(--surface)}
.ht-title{font-size:16px;line-height:24px;font-weight:600;color:var(--text-1)}
.ht-body{display:flex;flex-direction:column;gap:12px;min-width:0}
.ht-say{font-size:14px;line-height:20px;color:var(--text-1);text-wrap:pretty;max-width:64ch}
.ht-foot{padding-top:4px;margin-top:auto}
.ht-links{display:flex;flex-wrap:wrap;gap:4px 24px}
.side-card .empty{padding:0;color:var(--text-2)}
.hj-list{display:flex;flex-direction:column;margin:-8px 0}
.hj-list li + li{border-top:1px solid var(--border-subtle)}
.hj-row{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:56px;padding:8px 0;color:var(--text-1);text-decoration:none}
.hj-row .tier-pill{cursor:inherit}
.hj-fig{font-size:18px;line-height:24px;font-weight:600;font-variant-numeric:tabular-nums}
.hj-row:hover .hj-fig{text-decoration:underline;text-decoration-color:var(--link-line);text-underline-offset:3px}
.sec-link{display:inline-flex;align-items:center;min-height:32px;font-size:14px;line-height:20px}
.ht-trend{--ht-plot-h:44px;display:flex;flex-direction:column;gap:12px}
.ht-series{display:grid;grid-template-columns:minmax(0,1fr);row-gap:6px}
.ht-series-name{display:flex;align-items:center;gap:8px;font-size:12px;line-height:16px;font-weight:600;color:var(--text-2)}
.ht-swatch{flex:none;width:12px;height:12px;border-radius:2px}
.ht-swatch-1,.ht-bar-1{background:var(--ht-series-1)}
.ht-swatch-2,.ht-bar-2{border:1.5px solid var(--ht-series-2);background:repeating-linear-gradient(135deg,var(--ht-series-2) 0 1.5px,transparent 1.5px 5px)}
.ht-plot{display:grid;grid-template-columns:repeat(var(--n,4),minmax(0,1fr));column-gap:8px;align-items:end;height:calc(var(--ht-plot-h) + 20px);border-bottom:1px solid var(--border-strong)}
.ht-col{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:2px;height:100%}
.ht-val{font-size:12px;line-height:16px;font-weight:600;color:var(--text-1);font-variant-numeric:tabular-nums}
.ht-bar{display:block;width:min(32px,64%);height:calc(var(--f) * var(--ht-plot-h));border-radius:2px 2px 0 0}
.ht-bar-2{border-bottom:0}
.ht-bar.is-zero{height:0;border:0}
.ht-weeks{display:grid;grid-template-columns:repeat(var(--n,4),minmax(0,1fr));column-gap:8px;margin-top:-6px}
.ht-week{font-size:12px;line-height:16px;color:var(--text-3);text-align:center}
.ht-week.is-now{color:var(--text-2);font-weight:600}
.ht-week small{display:block;font-size:12px;font-weight:400;color:var(--text-3)}
.ht-firsts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.ht-first{display:flex;flex-direction:column;gap:4px;padding:12px;background:var(--ht-sunk);border-radius:var(--radius-2)}
.ht-first-fig{display:flex;align-items:center;gap:8px;font-size:28px;line-height:36px;font-weight:600;font-variant-numeric:tabular-nums}
.ht-first-say{font-size:12px;line-height:16px;color:var(--text-2)}
@container ht (min-width:480px){.ht-trend{--ht-plot-h:56px}.ht-series{grid-template-columns:9.5rem minmax(0,1fr);column-gap:16px;align-items:end}.ht-series-name{align-self:center}.ht-weeks{margin-left:calc(9.5rem + 16px)}}
.apm{container:apm / inline-size;display:flex;flex-direction:column;gap:12px;min-width:0}
.apm-cols{display:grid;grid-template-columns:minmax(0,1fr)}
.apm-col{min-width:0}
.apm-link{display:grid;grid-template-columns:minmax(0,1fr) auto 3ch;align-items:center;gap:8px;min-height:28px;padding:2px 0;border-bottom:1px solid var(--border-subtle);color:var(--text-2);text-decoration:none;font-size:12px;line-height:16px}
.apm-name{min-width:0}
.apm-label{color:var(--text-1);text-decoration:underline;text-decoration-color:var(--link-line);text-underline-offset:3px}
.apm-link:hover .apm-label{text-decoration-color:var(--text-1)}
.apm-count{text-align:right;font-size:14px;line-height:20px;font-weight:600;color:var(--text-1);font-variant-numeric:tabular-nums}
.apm-pips{display:flex;flex-wrap:wrap;gap:2px;justify-content:flex-end}
.apm-pip{width:8px;height:12px;background:var(--text-3)}
.apm-pip.is-closed{background:transparent;box-shadow:inset 0 0 0 1px var(--text-3)}
.apm > .empty{padding:0}
.apm-split{font-size:12px;line-height:16px;color:var(--text-2)}
.apm-wait-head{font-size:12px;line-height:16px;font-weight:600;color:var(--text-1)}
.apm-wait-head .apm-wn{font-weight:400;color:var(--text-2)}
.apm-wait{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.apm-wait li{display:flex;flex-wrap:wrap;align-items:center;gap:0 8px;min-width:0;font-size:12px;line-height:16px}
.apm-wait .link{font-size:14px;line-height:20px}
.apm-wait .apm-more{color:var(--text-2)}
@container apm (min-width:520px){
  .apm-cols{grid-template-columns:repeat(6,minmax(0,1fr))}
  .apm-link{grid-template-columns:minmax(0,1fr);grid-template-rows:28px 32px auto;align-content:start;align-items:start;gap:4px;min-height:84px;height:100%;padding:0 8px 4px;border-bottom:0;border-left:1px solid var(--border-subtle)}
  .apm-col:first-child .apm-link{padding-left:0;border-left:0}
  .apm-count{grid-row:1;font-size:20px;line-height:28px;text-align:left}
  .apm-name{grid-row:2}
  .apm-pips{grid-row:3;justify-content:flex-start}
  .apm-wait{flex-direction:row;flex-wrap:wrap;gap:4px 24px}
}
/* ---- the welcome: no card; the date and the greeting, and Radar's lights
   as chips, each a dot, the job's name and its word ---- */
.ht-welcome{container:htw / inline-size;min-width:0;color:var(--text-1)}
.ht-welcome-in{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px 32px}
.ht-hello{display:flex;flex-direction:column;gap:4px;min-width:0}
.ht-date{font-size:14px;line-height:20px;color:var(--text-3)}
.ht-greet{font-size:34px;line-height:42px;font-weight:600;letter-spacing:-.01em;color:var(--text-1);text-wrap:balance}
.ht-strip{display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:8px 16px;min-width:0}
.ht-strip-list{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px}
.ht-strip-job{display:inline-flex;align-items:center;gap:8px;height:36px;padding:0 14px;background:var(--surface);border:1px solid var(--border-subtle);border-radius:18px;font-size:13px;line-height:20px;white-space:nowrap}
.ht-dot{flex:none;width:8px;height:8px;border-radius:50%;background:var(--text-3)}
.ht-dot.is-pos{background:var(--positive)}
.ht-dot.is-cau{background:var(--caution)}
.ht-dot.is-neg{background:var(--negative)}
.ht-job-name{font-weight:500;color:var(--text-1)}
.ht-light.is-neu{color:var(--text-3)}
.ht-health-btn{display:inline-flex;align-items:center;min-height:36px;padding:0;border:0;background:none;color:var(--text-1);font-size:13px;line-height:20px;font-weight:500;text-decoration:underline;text-decoration-color:var(--link-line);text-decoration-thickness:1px;text-underline-offset:3px;cursor:pointer;white-space:nowrap}
.ht-health-btn:hover{text-decoration-color:var(--text-1)}
@container htw (max-width:719.98px){.ht-strip,.ht-strip-list{justify-content:flex-start}}
.ht-lights-sum{display:none}
@media (forced-colors:active){.ht-dot{border:1px solid CanvasText}}
/* ---- the five tiles, each in its group's colour: two a row on a phone with
   the first across the width, three and two from 600px, five across from
   1024px ---- */
/* Every tile the same height: the rows share the tallest. */
.ht-figs{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));grid-auto-rows:1fr;gap:12px}
.ht-figlink{display:flex;flex-direction:column;align-items:flex-start;min-width:0;min-height:44px;padding:16px;background-color:var(--surface);border:1px solid var(--border-subtle);border-radius:12px;color:var(--text-1);text-decoration:none;--focus-gap:var(--canvas);transition:border-color var(--dur) ease-out}
.ht-figlink:hover{border-color:var(--border-strong)}
.ht-figlink:hover .ht-figword{text-decoration:underline;text-decoration-color:var(--link-line);text-underline-offset:3px}
.ht-figlink > .gchip{margin-bottom:16px}
.ht-fignum{font-size:40px;line-height:48px;font-weight:600;font-variant-numeric:tabular-nums}
.ht-figword{margin-top:2px;font-size:16px;line-height:24px;font-weight:400;color:var(--text-2)}
.ht-figlink.is-zero .ht-fignum{color:var(--text-3)}
@container app (max-width:599.98px){.ht-figs{grid-template-columns:repeat(2,minmax(0,1fr))}.ht-figs > .ht-figlink:first-child{grid-column:1 / -1}}
@container app (min-width:600px) and (max-width:1023.98px){.ht-figs{grid-template-columns:repeat(6,minmax(0,1fr))}.ht-figs > .ht-figlink{grid-column:span 2}.ht-figs > .ht-figlink:nth-child(n + 4){grid-column:span 3}}
/* ---- the health panel, under the lights ---- */
.ht-health{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;margin-top:16px}
.ht-health-widgets{display:grid;grid-template-columns:minmax(0,1fr);gap:12px}
.ht-week-block{container:ht / inline-size;display:flex;flex-direction:column;gap:16px;min-width:0;padding:16px;background:var(--ht-surface);border:1px solid var(--ht-line);border-radius:var(--radius-4);--focus-gap:var(--ht-surface)}
.ht-health .widget{min-width:0;padding:16px;background:var(--ht-surface);border:1px solid var(--ht-line);border-radius:var(--radius-4);--focus-gap:var(--ht-surface)}
.ht-health .widget h3{margin-bottom:8px;font-size:16px;line-height:24px;font-weight:600;color:var(--text-1)}
.ht-health .widget ul{display:flex;flex-direction:column;gap:4px;font-size:14px;line-height:20px;color:var(--text-2)}
.ht-health .widget li{display:flex;align-items:baseline;gap:2px;min-width:0}
.ht-health .widget .num{margin-right:6px;font-size:16px;line-height:20px;font-weight:600;color:var(--text-1);font-variant-numeric:tabular-nums}
.ht-health .widget .li-age{margin-left:auto;padding-left:10px;font-size:12px;color:var(--text-3);font-variant-numeric:tabular-nums;white-space:nowrap}
.ht-health .bars{display:flex;align-items:flex-end;gap:3px;height:46px;margin:8px 0 6px}
.ht-health .bars i{display:block;flex:1;min-width:4px;background:var(--accent-quiet);border-radius:1px 1px 0 0}
.ht-health .bars i.today{background:var(--accent)}
.ht-health .bars i.none{height:2px!important;background:var(--hairline)}
.ht-health .bars-label{display:flex;justify-content:space-between;font-size:12px;line-height:16px;color:var(--text-3)}
.ht-health .dot{display:inline-block;flex:none;width:9px;height:9px;margin-right:9px;border-radius:50%}
.ht-health .dot-green{background:var(--settled)}
.ht-health .dot-amber{background:#C08A1D}
.ht-health .dot-red{background:var(--fail)}
.ht-health .dot-grey{background:transparent;border:1.5px solid var(--hairline-strong)}
.ht-health-foot{padding-top:4px}
@container htw (min-width:600px){.ht-health-widgets{grid-template-columns:repeat(2,minmax(0,1fr))}}
@container htw (min-width:1100px){.ht-health{grid-template-columns:minmax(0,2fr) minmax(0,1fr) minmax(0,1fr)}.ht-health-widgets{display:contents}.ht-health-foot{grid-column:1 / -1}}
/* ---- a section's title, outside its card: the chip, the title, a count
   pill, and a link or the tools at the right; 12px to the card ---- */
.sec-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;min-width:0;margin-bottom:12px}
.sec-title{display:flex;align-items:center;gap:10px;min-width:0;font-size:17px;line-height:24px;font-weight:600;color:var(--text-1)}
.sec-count{display:inline-flex;align-items:center;height:22px;padding:0 8px;border:1px solid #2a3238;border-radius:11px;background:#1a2025;font-size:12px;line-height:16px;font-weight:500;color:var(--text-2);font-variant-numeric:tabular-nums}
.sec-link{margin-left:auto}
.sec-head > .gchip{margin-right:2px}
/* ---- Open actions: every item in full, each its own card in its group's
   colour, one under another ---- */
.ny{container:ny / inline-size;min-width:0}
.ny-tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-left:auto}
.ny-tools .undo-last,.ny-tools .undo-last:disabled{border-color:transparent;font-size:13px}
.ny-body,.ny-group{display:flex;flex-direction:column;gap:16px;min-width:0}
.ny-body > .empty{padding:20px;background:var(--surface);border:1px solid var(--border-subtle);border-radius:14px;color:var(--text-2)}
.ny-body > .pane-note{min-height:0;padding:0;border:0}
.ny-item{position:relative;display:flex;flex-direction:column;gap:16px;min-width:0;padding:20px;background-color:var(--surface);border:1px solid var(--border-subtle);border-radius:14px;scroll-margin-top:16px;--focus-gap:var(--surface)}
.ny-item:focus{outline:none}
.ny-item:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.ny-item:target{border-color:var(--border-strong)}
.ny-ih{display:flex;align-items:flex-start;gap:14px}
.ny-ih-text{display:flex;flex-direction:column;gap:6px;min-width:0;padding-top:4px}
.ny-it{max-width:80ch;font-size:18px;line-height:24px;font-weight:600;text-wrap:balance}
.ny-im{display:flex;flex-wrap:wrap;align-items:center;gap:4px 16px;font-size:12px;line-height:16px;color:var(--text-2)}
.ny-ib{display:flex;flex-direction:column;gap:20px;min-width:0}
.ny-para{max-width:80ch;text-wrap:pretty}
.ny-para + .ny-para{margin-top:8px}
.ny-fig{display:flex;align-items:baseline;gap:8px;margin-top:12px}
.ny-basis{max-width:80ch;margin-top:2px;text-wrap:pretty}
.ny-under{max-width:80ch;margin-top:8px;text-wrap:pretty}
.ny-follow .text-meta,.ny-follow .c-meta,.ny-follow .c-seen,.ny-follow .group-count{color:var(--text-2)}
.ny-context{display:flex;flex-direction:column;gap:12px}
.ny-ctx-part .sec-h{font-size:12px;line-height:16px;color:var(--text-2)}
.ny-ctx-part .ctx-row{max-width:80ch}
.ny-onfile{align-self:flex-start}
.ny-gone{color:var(--caution)}
/* Every item's buttons: 44px, Done quiet. */
.ny-acts{gap:12px}
.ny-acts .btn{height:44px;padding:0 16px;border-radius:8px}
.ny-acts .btn-secondary{background:var(--surface-sunken);border-color:#39424a;color:var(--text-1)}
.ny-acts .btn-secondary:hover{background:var(--row-hover)}
/* A role: one short card, its chip, the Jobs row, its tier's pill and Done;
   Done goes under the row in a narrow card. The row's own rules are
   jobs_page.ROW_CSS; it is a "queue" container, as on Jobs, so the row's
   narrow rules reach it. */
.ny-role{container:nyrole / inline-size;flex-direction:row;flex-wrap:wrap;align-items:center;gap:12px 16px;padding:16px 20px}
.ny-role-row{container:queue / inline-size;flex:1 1 0;min-width:0}
.ny-role .row{min-height:0;padding:0;border-bottom:0;background:transparent}
.ny-role .row:hover{background:transparent}
.ny-role .r-title{font-size:16px;line-height:24px;font-weight:600}
.ny-role .r-meta,.ny-role .r2{color:var(--text-2)}
.ny-role .r-meta{font-size:13px;line-height:20px}
.ny-role .r2{min-height:20px}
.ny-role .r-bit ~ .r-bit::before{border-left-color:var(--text-3)}
.ny-role > .ny-acts{flex:none;margin-left:auto}
@container nyrole (max-width:479.98px){.ny-role > .ny-acts{flex-basis:100%;justify-content:flex-end}}
/* An urgent story: its kind and source, the headline, the summary, its Next
   move in an inset panel, then its links and Done. */
.ny-story{gap:14px}
.ny-story .story-line{display:flex;align-items:center;gap:12px;min-width:0;color:var(--text-2);font-size:13px;line-height:20px;overflow-wrap:anywhere}
.ny-story .story-kind{font-weight:600;color:#ecc27a}
.ny-story .story-kind{flex:none}
.ny-story .story-src{flex:0 1 auto;min-width:0}
.ny-story .story-src .co{color:var(--text-2)}
.fact-rule{flex:none;display:inline-block;width:0;height:12px;border-left:1px solid var(--text-3)}
.ny-story .story-h{max-width:60ch;margin-top:2px;font-size:21px;line-height:1.3;font-weight:600;overflow-wrap:anywhere;text-wrap:balance}
.ny-story .story-h a{color:var(--text-1);text-decoration:none}
.ny-story .story-h a:hover{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:4px}
.ny-story .story-sum{max-width:72ch;color:var(--text-2);font-size:15px;line-height:24px;text-wrap:pretty}
.ny-move{padding:14px 16px;background:#101417;border:1px solid #1f262b;border-radius:10px}
.ny-move .sec-h{margin-bottom:4px;font-size:14px;line-height:20px;font-weight:600;color:var(--text-1)}
.ny-move .ny-para{color:var(--text-2);font-size:15px;line-height:24px}
.ny-story .ny-acts{gap:12px 24px;margin-top:2px}
.ny-story .ny-acts .link{display:inline-flex;align-items:center;min-height:44px;font-size:15px;line-height:24px}
.ny-story .ny-acts .link svg{margin-left:4px}
.ny-story .ny-acts .ny-onfile{align-self:auto}
@container ny (max-width:599.98px){.ny-tools{margin-left:0;flex-basis:100%}.facts{grid-template-columns:112px minmax(0,1fr)}.ny-item{padding:16px}.ny-role{padding:14px 16px}}
/* ---- the page: a dotted ground, then the welcome, the tiles and the rest ---- */
.app{background-color:var(--canvas);background-image:radial-gradient(rgba(232,237,240,.06) 1px,transparent 1px);background-size:22px 22px}
/* The content box, set once and read by the page and by the top bar's
   contents (page_frame's .appbar-in), so the two share their edges at every
   width: capped at 1440px, 16px in, 24px from 768px and 32px from 1024px. */
.app > *{--frame-max:1440px;--frame-pad:16px}
.hf{display:grid;grid-template-columns:minmax(0,1fr);gap:20px;align-content:start;width:100%;max-width:var(--frame-max);margin:0 auto;padding:20px var(--frame-pad) 32px}
/* Under the tiles, two columns, 2fr and 1fr, 20px apart and aligned at the
   top; under 1024px they stack: Open actions, then Jobs, then Applications. */
.hf-cols{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;align-items:start;min-width:0}
.hf-main,.hf-side{min-width:0}
.hf-side{display:flex;flex-direction:column;gap:24px}
@container app (min-width:768px){.app > *{--frame-pad:24px}.hf{gap:24px;padding-block:32px 48px}}
@container app (min-width:1024px){.app > *{--frame-pad:32px}.hf-cols{grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:20px}}
/* Under 768 every target is 44px. */
@media (max-width:767.98px){
  html{scroll-padding-top:8px}
  .btn,.seg{height:44px}
  .undo-last{padding-inline:8px}
  .ht-figlink,.apm-link,.link-block,.ctx-row,.ht-health-btn,.sec-link,.ny-acts .link{min-height:44px}
  .apm-wait li .link{display:inline-flex;align-items:center;min-height:44px}
  .table th:first-child,.table td:first-child{width:112px}
  .table th:nth-child(2),.table td:nth-child(2){width:80px}
}
/* Under 700px (owner, 17 Nov): Open actions comes up the page. The five
   lights fold behind one chip that counts them, Radar's health beside it; the
   tiles are shorter, the chip and the number on one line and the word under
   them; the greeting and the gaps are a little tighter. */
@media (max-width:699.98px){
  .hf{gap:16px;padding-top:12px}
  .ht-welcome-in{row-gap:12px}
  .ht-greet{font-size:30px;line-height:36px}
  .ht-strip{flex-wrap:wrap;gap:8px 16px}
  .ht-lights-sum{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:0 16px;background:var(--surface);border:1px solid var(--border-subtle);border-radius:22px;color:var(--text-1);font:500 13px/20px var(--font-sans);white-space:nowrap;cursor:pointer}
  .ht-lights-sum:hover{border-color:var(--border-strong)}
  .ht-strip-list{display:none;order:3;flex-basis:100%}
  .ht-lights-sum[aria-expanded="true"] ~ .ht-strip-list{display:flex}
  .ht-figs{grid-auto-rows:auto;gap:8px}
  .ht-figlink{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;column-gap:12px;row-gap:0;padding:12px}
  .ht-figlink > .gchip{margin-bottom:0}
  .ht-fignum{font-size:28px;line-height:32px}
  .ht-figword{grid-column:1 / -1;margin-top:0;font-size:14px;line-height:18px}
}
@media (pointer:coarse){
  .btn,.seg{height:44px}
  .ht-figlink,.apm-link,.link-block,.ht-health-btn,.sec-link,.ny-acts .link{min-height:44px}
  .apm-wait li .link{display:inline-flex;align-items:center;min-height:44px}
}
@media (forced-colors:active){.ny-item:target{outline:2px solid Highlight}.tag{border:1px solid CanvasText}}
"""


# ================================================================ the script

HOME_SCRIPT = r"""
(function () {
  'use strict';
  var app = document.querySelector('.app[data-page="home"]');
  if (!app) return;
  var state = JSON.parse(app.dataset.state);
  var main = document.getElementById('main');
  var toast = document.getElementById('toast');
  var DONE = { done: 'Done', back: 'Put back' };
  // No single-key shortcuts on Home (17 Nov): nothing here listens for keys.
  // Enter on a link or a button does what it always does.

  function el(id) { return document.getElementById(id); }
  function query(item) {
    var p = [];
    if (item) p.push('item=' + encodeURIComponent(item));
    if (state.show === 'done') p.push('show=done');
    return p.length ? '?' + p.join('&') : '';
  }
  function postJson(path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () {
        return { ok: false, note: 'No usable answer came back, so that may not have saved. Reload to check.' };
      }); });
  }
  // The toast stays about eight seconds, its Undo with it, or until the next
  // click on the page.
  var toastTimer = null;
  function hush() {
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
    toast.textContent = '';
    toast.classList.remove('is-bad');
  }
  function say(text, bad, undo) {
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(hush, 8000);
    toast.textContent = '';
    toast.classList.toggle('is-bad', !!bad);
    var span = document.createElement('span');
    span.textContent = text;
    toast.appendChild(span);
    if (undo) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-secondary';
      b.textContent = 'Undo';
      b.addEventListener('click', undoLast);
      toast.appendChild(b);
    }
  }

  // ---- the undo stack, kept for the tab so a reload keeps it
  function stack() {
    try { return JSON.parse(sessionStorage.getItem('radar-home-undo') || '[]'); } catch (e) { return []; }
  }
  function setStack(s) {
    try { sessionStorage.setItem('radar-home-undo', JSON.stringify(s.slice(-20))); } catch (e) {}
    var b = el('undo-last');
    if (!b) return;
    b.disabled = !s.length;
    var last = s[s.length - 1];
    b.setAttribute('aria-label', last ? 'Undo last: ' + DONE[last.kind] + ', ' + last.title
                   : 'Undo last, nothing to undo');
  }

  // ---- the items: every one is on the page, so going to one is a scroll
  function items() { return Array.prototype.slice.call(main.querySelectorAll('.ny-item[data-item]')); }
  function goTo(id) {
    var it = id && el(id);
    if (!it || !it.classList.contains('ny-item')) return false;
    it.scrollIntoView({ block: 'start' });
    it.focus({ preventScroll: true });
    return true;
  }
  function neighbour(id) {
    var r = items();
    var i = r.findIndex(function (x) { return x.dataset.item === id; });
    if (i < 0) return null;
    var other = r[i + 1] || r[i - 1];
    return other ? other.dataset.item : null;
  }

  // ---- Radar's health: the panel under the lights, closed until asked for
  function setHealth(open) {
    var btn = el('health-btn'), panel = el('health-panel');
    if (!btn || !panel) return;
    btn.setAttribute('aria-expanded', String(open));
    panel.hidden = !open;
  }
  // ---- the lights on a phone: one chip that shows the five, or hides them
  function setLights(open) {
    var btn = el('lights-btn');
    if (btn) btn.setAttribute('aria-expanded', String(open));
  }
  function lightsOpen() {
    var btn = el('lights-btn');
    return !!btn && btn.getAttribute('aria-expanded') === 'true';
  }
  function healthOpen() {
    var btn = el('health-btn');
    return !!btn && btn.getAttribute('aria-expanded') === 'true';
  }

  // ---- redrawing after a write: the main element, from the server
  function refresh(item, focusIt) {
    var open = healthOpen(), lit = lightsOpen();
    return fetch('/' + (query(item) || '?') + (query(item) ? '&' : '') + 'part=main',
                 { headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) {
        main.innerHTML = j.html;
        document.title = j.title;
        setHealth(open);
        setLights(lit);
        setStack(stack());
        try { history.replaceState(null, '', '/' + query(null)); } catch (e) {}
        if (!focusIt) return;
        if (!(item && goTo(item))) {
          var h = el('ny-title');
          if (h) { h.tabIndex = -1; h.focus(); }
        }
      }).catch(function () { location.reload(); });
  }

  // ---- Done, Put back and Undo last
  var busy = false;
  function act(kind, key, title, id, undoing) {
    if (busy) { say('Still saving the last change.'); return Promise.resolve('busy'); }
    busy = true;
    var path = (kind === 'done') !== !!undoing ? '/home/done' : '/home/undone';
    return postJson(path, { key: key, title: title }).then(function (j) {
      busy = false;
      if (!j || j.ok === false) { say((j && j.note) || 'That did not stick. Try again.', true); return 'refused'; }
      if (!undoing) {
        var s = stack();
        s.push({ kind: kind, key: key, title: title, id: id });
        setStack(s);
        say((kind === 'done' ? 'Done: “' + title + '” is off Home.'
             : 'Put back on Home: “' + title + '”.'), false, true);
        return refresh(neighbour(id), true).then(function () { return 'ok'; });
      }
      say('Undone: “' + title + '” is ' + (kind === 'done' ? 'back on Home.' : 'off Home again.'));
      var back = (kind === 'done') === (state.show !== 'done');
      return refresh(back ? id : null, true).then(function () { return 'ok'; });
    }, function () {
      busy = false;
      say('No usable answer came back, so that may not have saved. Reload to check.', true);
      return 'error';
    });
  }
  var undoing = false;
  function undoLast() {
    var s = stack();
    if (!s.length || undoing) return;
    var last = s.pop();
    undoing = true;
    act(last.kind, last.key, last.title, last.id, true).then(function (outcome) {
      undoing = false;
      if (outcome === 'ok' || outcome === 'refused') setStack(s); else setStack(stack());
    });
  }
  function checkNow(btn) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    var at = btn.closest('[data-item]');
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = 'Checking';
    say('The news watcher is running. This can take up to ten minutes.');
    fetch('/watch', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || j.ok === false) { say((j && j.note) || 'The run did not finish.', true); }
        else { say('The news watcher ran.'); }
        refresh(at ? at.dataset.item : null, false);
      }, function () {
        say('No usable answer came back. Reload to check.', true);
        refresh(at ? at.dataset.item : null, false);
      });
  }

  // ---- clicks
  document.addEventListener('click', function (e) {
    if (toast.textContent && !(e.target.closest && e.target.closest('#toast'))) hush();
  }, true);
  main.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    if (e.target.closest('#health-btn')) { setHealth(!healthOpen()); return; }
    if (e.target.closest('#lights-btn')) { setLights(!lightsOpen()); return; }
    var at = e.target.closest('[data-item]');
    var id = at ? at.dataset.item : null;
    var done = e.target.closest('[data-done]');
    if (done) { act('done', done.dataset.done, done.dataset.title, id, false); return; }
    var back = e.target.closest('[data-back]');
    if (back) { act('back', back.dataset.back, back.dataset.title, id, false); return; }
    var check = e.target.closest('[data-check]');
    if (check) { checkNow(check); return; }
    if (e.target.closest('#undo-last')) { undoLast(); }
  });

  // ---- on load: an item's own address, /?item=<id>, brings it into view
  setStack(stack());
  if (state.target) goTo(state.target);
})();
"""
