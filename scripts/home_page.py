"""Home: the welcome, the three tiles and Needs you today.

Built from Home final (design/round-1 branch, design/home/final, 15 Nov 2026)
and the owner's decisions of that day in design/README.md, on the shared frame
in page_frame.py, dark only. At /, with every item at its own address,
/?item=<id>, and the items taken off Home at /?show=done.

What is on it, top to bottom (trimmed 17 Nov: no sentence under the greeting,
no As of time, no question under a widget's title, no footnotes, no keys):
- The welcome: the date, the greeting (by the first name on the optional
  first_name line of the owner's preferences file, never a name in the code)
  and five figures, each a link to where those items live and each equal to a
  group of the list below: urgent signals, new Top prospects, new One
  question away roles, company follow-ups due and application follow-ups due.
  A figure at 0 is dimmed. Company follow-ups due are read from
  watchlist.threads_due, the rule the Watchlist's tags use: a Watchlist
  company's next step due today or overdue, UK date; a next step with no date
  never counts.
- Radar's five jobs as lights, a mark in a ring, a colour and a word (On time,
  Late, Stale, Off, Not sending or, for the inbox, Quiet), from
  radar_common.job_health, the verdict /health gives, read by
  page_frame.job_light, as the top bar's light is. The phone push has no run
  of its own: it rides on the signal watcher, so its light is worked out from
  the watcher's runs and the ntfy address in radar.yaml (push_light). Under
  the lights, a panel, closed until asked for: Week by week, the flow of the
  last fourteen days and board freshness (as /radar draws them, without their
  captions), and a link to /radar.
- Applications: the board's six stages and what waits on the owner, read from
  the applications table (applications.load, board_page.board_model).
- Open roles: Top prospects, One question away and Reading, as the Jobs tabs
  count them, with today's new.
- Needs you today: Radar's jobs that need a look, urgent signals, new Top
  prospects, new One question away roles, company follow-ups due and
  application follow-ups due, every item shown in full, one under another. An
  urgent signal is drawn as Insights draws a story.

Done takes an item off Home and only off Home, in the home_done table; Undo
last puts it back. It never changes the item where it lives. Everything else
here reads: the page is drawn from one build_dashboard.collect() and a few
reads of its own on the same read-only connection, and its script writes only
through POST /home/done and /home/undone (and /watch, the signal watcher's
Check now, which the server already had).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import applications as ap
import build_dashboard as bd
import insights_page as ip
import jobs_page as jp
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
JOBS = (("inbox", "Inbox"), ("signals", "Signal watcher"),
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
SCHEDULE = {"inbox": "Each alert email, as n8n finds it in the inbox",
            "signals": "Every two hours", "check": "Once a day",
            "digest": "Mondays at 07:30", "push": "With the signal watcher"}
DOES = {"inbox": "Reads job alert emails, then stores and scores the roles.",
        "signals": "Reads the watched sources, then stores and scores signals.",
        "check": "Opens each advert's page to see whether the role is still open.",
        "digest": "Builds Monday's email of the week's roles and signals.",
        "push": "Sends a signal at 75 or more to the phone, as the watcher stores it."}
ENDED = {"ok": "Finished", "partial": "Finished, with failures", "failed": "Failed"}
GROUPS = (("radar", "Radar"), ("urgent", "Urgent signals"),
          ("top", "Top prospects"), ("question", "One question away"),
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
                "why": "The signal watcher has never run, so nothing has been pushed."}
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
    cls, sym = LIGHTS[light["word"]]
    return (f'<li class="ht-strip-job" data-job="{light["name"]}"><span class="ht-light {cls}">'
            f'<span class="ht-lamp">{_use(sym)}</span>{esc(light["word"])}</span>'
            f'<span class="ht-job-name">{esc(light["label"])}</span></li>')


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


def _gate_marks(o: dict) -> str:
    marks, words = [], []
    for gate, name, _letter in jp.GATES:
        cls, symbol, said = jp.gate_words(o, gate, name)
        marks.append(f'<svg class="{cls}" width="12" height="12" aria-hidden="true" '
                     f'focusable="false"><use href="#{symbol}"></use></svg>')
        words.append(said.split(",")[0])
    return (f'<span class="r-marks" aria-hidden="true">{"".join(marks)}</span>'
            f'<span class="visually-hidden">{esc(", ".join(words))}.</span>')


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
            "lead": jp.lead_line(o), "marks": _gate_marks(o), "role": o,
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

FIGURES = (("urgent", "urgent signal", "urgent signals", "#group-urgent"),
           ("top", "new Top prospect", "new Top prospects", "/jobs#group-top"),
           ("question", "new One question away", "new One question away", "/jobs#group-question"),
           # The Watchlist sorts its due follow-ups first.
           ("threads", "company follow-up due", "company follow-ups due", "/watchlist"),
           ("apps", "application follow-up due", "application follow-ups due",
            "/jobs?show=applied"))
JOB_PHRASES = {"inbox": "the inbox", "signals": "the signal watcher", "check": "the advert checker",
               "digest": "the Monday digest", "push": "the phone push"}


def greeting(now: datetime, name: str | None) -> str:
    hour = uk(now).hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return f"Good {part}, {name}" if name else f"Good {part}."


def apps_word(model: dict, count: int) -> str:
    base = "application follow-up" if count == 1 else "application follow-ups"
    return f"{base} due today" if not model["overdue"] else f"{base} due or overdue"


def figures_html(model: dict) -> str:
    """The five figures. One at 0 is dimmed, so the ones with a number stand out."""
    c = model["counts"]
    out = []
    for key, one, many, href in FIGURES:
        n = c[key]
        word = one if n == 1 else many
        if key == "apps":
            word = apps_word(model, n)
        out.append(f'<a class="ht-figlink{" is-zero" if not n else ""}" id="figure-{key}" '
                   f'href="{esc(href)}" data-group="{key}"><span class="ht-fignum">{n}</span>'
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
    return (
        '<section class="ht-welcome hf-hero" id="welcome" aria-labelledby="welcome-h">'
        '<div class="ht-welcome-in">'
        f'<p class="ht-date">{now:%A} {now.day} {now:%B}</p>'
        f'<h1 class="ht-greet" id="welcome-h"><span class="visually-hidden">Home. </span>'
        f'{esc(greeting(model["now"], model["name"]))}</h1>'
        f'{figures_html(model)}'
        '<div class="ht-strip"><ul class="ht-strip-list" aria-label="Radar\'s five jobs">'
        f'{strip}</ul><button type="button" class="btn btn-secondary ht-health-btn" id="health-btn" '
        'aria-expanded="false" aria-controls="health-panel">Radar\'s health'
        f'<span class="ht-chev">{_use("ic-down", 16)}</span></button></div>'
        f'{health_panel_html(model)}'
        '</div></section>')


# ================================================================ the tiles

def apps_tile(model: dict) -> str:
    b = model["bmodel"]
    now = model["now"]
    total = sum(b["totals"].values())
    head = ('<header class="ht-head"><h2 class="ht-title" id="tile-applications">Applications</h2>'
            '</header>')
    if not total:
        body = '<div class="apm"><div class="empty"><p>No applications yet.</p></div></div>'
        return (f'<section class="ht ht-apps-tile" aria-labelledby="tile-applications"><div class="ht-in">'
                f'{head}<div class="ht-body">{body}</div></div></section>')
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
        parts.append('<p class="apm-split">Nothing is waiting on you. A next step you set, or '
                     'Radar\'s Follow up, shows here.</p>')
    return (f'<section class="ht ht-apps-tile" aria-labelledby="tile-applications"><div class="ht-in">'
            f'{head}<div class="ht-body"><div class="apm">{"".join(parts)}</div></div>'
            '<p class="ht-foot"><a class="link link-block" href="/jobs?show=applied">Open the board</a>'
            '</p></div></section>')


def funnel_tile(model: dict) -> str:
    """Top prospects, One question away and Reading, as the Jobs tabs count
    them, each a link to its tab, with today's new beside it."""
    j = model["jmodel"]
    t = j["totals"]
    # New Top and One question away are the figures' counts, so a role Done
    # on Home is not new here either; Reading's new are Jobs' own.
    new = {"reading": sum(1 for o in j["tiers"]["reading"] if o.get("_unread")),
           "question": model["counts"]["question"], "top": model["counts"]["top"]}
    steps = (("top", "Top prospects", t["top"], new["top"], "/jobs#group-top"),
             ("question", "One question away", t["question"], new["question"], "/jobs#group-question"),
             ("reading", "Reading", t["reading"], new["reading"], "/jobs?reading=1#group-reading"))
    # Bar lengths on a square-root scale, the longest of the three full.
    top = math.sqrt(max([1] + [n for _k, _w, n, _f, _h in steps]))
    lis = []
    for key, name, n, fresh, href in steps:
        width = 100.0 * math.sqrt(n) / top if n else 0.0
        fresh_html = (f'<span class="ht-step-new"><span class="visually-hidden">, </span>{fresh} new</span>'
                      if fresh else '<span class="ht-step-new" aria-hidden="true"></span>')
        lis.append(f'<li><a class="ht-step" id="funnel-{key}" href="{esc(href)}">'
                   f'<span class="ht-step-name">{esc(name)}<span class="visually-hidden">:</span></span>'
                   f'<span class="ht-step-track" aria-hidden="true"><span class="ht-step-bar'
                   f'{" is-zero" if not n else ""}" style="--w:{width:.1f}%"></span></span>'
                   f'<span class="ht-step-fig">{n}<span class="visually-hidden"> '
                   f'{"role" if n == 1 else "roles"}</span></span>{fresh_html}'
                   f'<span class="ht-step-go">{_use("ic-right", 16)}</span></a></li>')
    return ('<section class="ht ht-funnel-tile" aria-labelledby="tile-open-roles"><div class="ht-in">'
            '<header class="ht-head"><h2 class="ht-title" id="tile-open-roles">Open roles</h2>'
            '</header><div class="ht-body">'
            f'<ol class="ht-funnel" aria-label="Open roles by tier, each a link to Jobs">{"".join(lis)}</ol>'
            '</div><p class="ht-foot"><a class="link link-block" href="/jobs">Open Jobs</a></p>'
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
        aria = (f"New Top prospects per week, weeks starting {said_top}. Signals at 40 or more "
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
                f'</span>Signals at 40 or more</p>{plot("signals", "ht-bar-2")}</div>'
                f'<div class="ht-weeks" style="--n:{n}" aria-hidden="true">{week_cells}</div></div>'
                f'<p class="ht-say">{esc(say)}</p>')
    return ('<section class="ht-week-block" aria-labelledby="tile-week-by-week">'
            f'{head}<div class="ht-body">{body}</div>'
            '<p class="ht-foot ht-links"><a class="link link-block" href="/jobs#group-top">Open Top prospects</a>'
            '<a class="link link-block" href="/insights">Open Insights</a></p></section>')


# ================================================================ Needs you today

def group_head(group: str, items: list, model: dict) -> str:
    n = len(items)
    title = dict(GROUPS)[group]
    if group == "radar":
        count = f"{n} of {len(JOBS)} jobs need a look"
    elif group == "urgent":
        count = str(n)
    elif group in ("top", "question"):
        count = f"{n} new"
    elif group == "threads":
        late = sum(1 for i in items if i["thread"]["state"] == "overdue")
        count = f"{n} due, {late} overdue" if late else f"{n} due today"
    else:
        late = sum(1 for i in items if ap.parse(i["app"].get("next_step_due")).date()
                   < uk(model["now"]).date())
        count = f"{n} due" + (f", {late} overdue" if late else " today")
    return (f'<h3 class="group-title" id="group-{group}">{esc(title)}'
            f'<span class="group-count">{esc(count)}</span></h3>')


# ---- one item, in full

def _facts(rows) -> str:
    return '<dl class="facts">' + "".join(
        f'<dt>{esc(k)}</dt><dd>{v}</dd>' for k, v in rows if v) + "</dl>"


def _sec(hid: str, title: str, body: str) -> str:
    return (f'<section class="sec" aria-labelledby="{hid}"><h5 class="sec-h" id="{hid}">{esc(title)}</h5>'
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
        out.append(f'<div class="ny-runs"><h5 class="sec-h" id="{uid}-runs">Last runs'
                   f'<span class="group-count"> {len(runs)}</span></h5>'
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


def _body_role(i: dict, uid: str) -> str:
    o = i["role"]
    tier = jp.TIER_WORDS.get(o.get("tier") or "", "Not scored")
    gates = []
    for gate, name, _l in jp.GATES:
        cls, symbol, said = jp.gate_words(o, gate, name)
        gates.append((name, f'<span class="status {"is-neg" if cls == "mk-fail" else "is-pos" if cls == "mk-pass" else "is-neu"}">'
                            f'{_use(symbol)}{esc(said[len(name) + 1:])}</span>'))
    cv = o.get("cv_match")
    score = (f'<p class="ny-para">{esc(i["lead"])}</p>' if i.get("lead") else "")
    score += (f'<p class="ny-fig"><span class="text-label ink-2">CV match</span>'
              f'<span class="text-figure-large">{int(cv)}%</span></p>' if cv is not None else "")
    basis = ("Scored on the full job spec." if jp.spec_tag(o) else
             "Scored on the advert card the alert email carried.")
    score += f'<p class="text-meta ny-basis">{esc(tier)}. {esc(basis)}</p>'
    out = [_sec(f"{uid}-why", "Why it is here", score),
           _sec(f"{uid}-gates", "The four gates", _facts(gates))]
    rows = [("Company", esc(o.get("company") or "Unknown company")),
            ("Where", esc(jp.location_text(o))), ("Pay", esc(o.get("salary_rate") or "Not stated")),
            ("Engagement", esc(jp.engagement_word(o))), ("Source", esc(jp.board(o.get("source")))),
            ("First seen", esc(day_time(jp.first_seen(o))))]
    out.append(_sec(f"{uid}-details", "Details", _facts(rows)))
    return "".join(out)


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
        parts.append(f'<div class="ny-ctx-part"><h6 class="sec-h">Thread</h6>'
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
            inner = (f'<span class="r1"><span class="r-title">{esc(rc.clean_headline(lead.get("headline") or ""))}</span>'
                     f'<span class="r-cv"><span class="visually-hidden">Relevance </span>{s["relevance"]}</span>'
                     f'<span class="r-seen"><span class="visually-hidden">Found </span>{esc(day(s["found"]))}</span></span>'
                     f'<span class="r2"><span class="r-meta">{esc(words)}</span></span>')
            if s["type"] in HIRING:
                rows.append(f'<div class="row row-2 is-static">{inner}</div>')
            else:
                rows.append(f'<a class="row row-2" href="/insights/story/{int(lead["id"])}">{inner}</a>')
        parts.append(f'<div class="ny-ctx-part"><h6 class="sec-h">Signals on file'
                     f'<span class="group-count"> {len(stories)}</span></h6>{"".join(rows)}</div>')
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
            rows.append(f'<a class="row row-2" href="/jobs/{int(o["id"])}"><span class="r1">'
                        f'<span class="r-title">{esc(o.get("title") or "Untitled role")}</span>'
                        f'<span class="r-cv"><span class="visually-hidden">CV match </span>'
                        f'{f"{int(cv)}%" if cv is not None else "–"}</span></span>'
                        f'<span class="r2"><span class="r-meta">{esc(", ".join(bits))}</span></span></a>')
        parts.append(f'<div class="ny-ctx-part"><h6 class="sec-h">Roles on Jobs'
                     f'<span class="group-count"> {len(roles)}</span></h6>{"".join(rows)}</div>')
    if not parts:
        return ""
    return (f'<section class="sec ny-context" aria-labelledby="{uid}-file">'
            f'<h5 class="sec-h" id="{uid}-file">On file for {esc(name)}</h5>{"".join(parts)}</section>')


def _on_file_link(i: dict, model: dict) -> str:
    cid = i.get("company_id")
    c = model["companies"].get(cid) if cid else None
    if not c:
        return ""
    return (f'<a class="link link-block ny-onfile" href="/company/{int(cid)}">'
            f'Everything on file<span class="visually-hidden"> for {esc(c["display_name"])}</span></a>')


def _actions(i: dict, model: dict) -> str:
    """Open where the item lives (a signal's card has its headline for that),
    Done or Put back, and Check now for the signal watcher. No Next: every
    item is on screen."""
    done_view = model["show"] == "done"
    acts = []
    if i["kind"] == "radar":
        n8n = bd.safe_url(model["config"].get("n8n_url"))
        open_ = ((n8n, "Open the workflow scheduler", True) if n8n
                 else ("/radar", "Open Radar's health", False))
    elif i["kind"] == "signal":
        open_ = None
    else:
        open_ = i.get("open")
    if open_:
        href, label, external = open_
        ext = (' target="_blank" rel="noopener noreferrer"' if external else "")
        tail = '<span class="visually-hidden"> (opens in a new tab)</span>' if external else ""
        acts.append(f'<a class="btn btn-primary" href="{esc(href)}" data-open{ext}>{esc(label)}{tail}</a>')
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
    announce = (f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{what}'
                f'<span class="visually-hidden"> on {esc(i["source"])} (opens in a new tab)</span>'
                f'{ip.ICON_OUT}</a>' if url else "")
    step = lead.get("playbook_step")
    move = (f'<section class="ny-move" aria-labelledby="{uid}-move"><h5 class="sec-h" id="{uid}-move">'
            f'Next move</h5><p class="ny-para">{esc(step)}</p></section>' if step else "")
    meta = f'<p class="story-meta">{announce}</p>' if announce else ""
    return (f'<article class="ny-item ny-story" id="{uid}" data-item="{uid}" data-key="{esc(i["key"])}" '
            f'aria-labelledby="{uid}-t" tabindex="-1">'
            f'<p class="story-line"><span class="co">{esc(i.get("company") or "Unknown company")}</span>, '
            f'via {esc(i["source"])}{esc(more)}</p>'
            f'<h4 class="story-h" id="{uid}-t">{headline}</h4>'
            f'<p class="story-sum">{esc(i.get("lead") or "")}</p>{move}{meta}'
            f'{_on_file_link(i, model)}{_actions(i, model)}</article>')


def item_html(i: dict, model: dict) -> str:
    """One item in full: its title, what it is, its tags, the body, what else
    is on file, and its controls."""
    if i["kind"] == "signal":
        return signal_card(i, model)
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
    elif i["kind"] == "role":
        body = _body_role(i, uid) + _context_company(i, model, uid)
    elif i["kind"] == "thread":
        body = _body_thread(i, uid, model["now"]) + _context_company(i, model, uid)
    else:
        body = _body_app(i, uid, model["now"]) + _context_company(i, model, uid)
    return (f'<article class="ny-item" id="{uid}" data-item="{uid}" data-key="{esc(i["key"])}" '
            f'aria-labelledby="{uid}-t" tabindex="-1">'
            f'<header class="ny-ih"><h4 class="ny-it" id="{uid}-t">{esc(i["title"])}</h4>'
            f'<p class="ny-im">{meta}{tags}</p></header>'
            f'<div class="ny-ib">{body}</div>{_on_file_link(i, model)}{_actions(i, model)}</article>')


def list_html(model: dict) -> str:
    items = model["items"]
    parts = []
    for group, _word in GROUPS:
        members = [i for i in items if i["group"] == group]
        if not members:
            continue
        body = "".join(item_html(i, model) for i in members)
        parts.append(f'<section class="ny-group" aria-labelledby="group-{group}">'
                     f'{group_head(group, members, model)}{body}</section>')
    return "".join(parts)


def empty_html(model: dict) -> str:
    if model["show"] == "done":
        return '<div class="empty"><p>Nothing done.</p></div>'
    return '<div class="empty"><p>Nothing today.</p></div>'


def needs_html(model: dict) -> str:
    items = model["items"]
    done_view = model["show"] == "done"
    title = "Done on Home" if done_view else "Needs you today"
    count = f'<span class="ny-count">{_n(len(items), "item")}</span>' if items else ""
    n_done = len(model["done"])
    seg = ('<nav class="segmented" aria-label="Show">'
           f'<a class="seg" href="/"{" aria-current=" + chr(34) + "page" + chr(34) if not done_view else ""}>To do</a>'
           f'<a class="seg" href="/?show=done"{" aria-current=" + chr(34) + "page" + chr(34) if done_view else ""}>'
           f'Done<span class="visually-hidden">, {n_done} {"item" if n_done == 1 else "items"}</span></a></nav>')
    head = (f'<header class="ny-head"><h2 class="ny-title" id="ny-title">{title}{count}</h2>'
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
    return (f'{welcome_html(model)}<div class="hf-bento">{apps_tile(model)}{funnel_tile(model)}'
            f'</div>{needs_html(model)}')


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
<style>{ip.FONT_CSS}{page_frame.FRAME_CSS}{HOME_CSS}</style>
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
:root{--font-serif:"IBM Plex Serif",Georgia,"Times New Roman",serif;--ht-surface:#1a1d1f;--ht-line:#2b3135;--ht-track:#252a2d;--ht-sunk:#15181a;--ht-series-1:#2aa1b3;--ht-series-2:#9a80ea;--hf-hero-a:#221e19;--hf-hero-b:#1a1d1f;--hf-hero-line:#3b342b}
.row:focus-visible{outline-offset:-7px;box-shadow:none}
/* The page scrolls as one document at every width: Needs you today shows every item in full. */
.app{container:app / inline-size;display:grid;grid-template-rows:48px auto;min-height:100vh;align-content:start;background:var(--canvas)}
.content{grid-row:2;min-width:0}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:32px;padding:0 12px;border:1px solid transparent;border-radius:var(--radius-2);font-size:14px;line-height:20px;font-weight:500;white-space:nowrap;text-decoration:none;cursor:pointer;transition:background-color var(--dur) ease-out,color var(--dur) ease-out,border-color var(--dur) ease-out}
.btn-primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn-secondary{background:var(--surface);border-color:var(--border-strong);color:var(--text-1)}
.btn-secondary:hover{background:var(--row-hover)}
.btn-ghost{background:transparent;color:var(--text-2)}
.btn-ghost:hover{color:var(--text-1);text-decoration:underline;text-underline-offset:2px}
.btn.is-disabled,.btn:disabled{color:var(--text-3);border-color:var(--border-subtle);cursor:default;text-decoration:none}
.btn[aria-busy="true"]{cursor:progress}
.link{color:var(--accent);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:2px;transition:color var(--dur) ease-out}
.link:hover{color:var(--accent-hover);text-decoration-thickness:2px}
.link-block{display:inline-flex;align-items:center;min-height:24px}
.tag{display:inline-flex;align-items:center;gap:4px;flex:none;height:20px;padding:0 8px;border-radius:var(--radius-2);font-size:12px;line-height:16px;font-weight:600;white-space:nowrap}
.tag-neutral{background:var(--neutral-tint);color:var(--neutral)}
.tag-caution{background:var(--caution-tint);color:var(--caution)}
.tag-negative{background:var(--negative-tint);color:var(--negative)}
.tag-positive{background:var(--positive-tint);color:var(--positive)}
.is-pos{color:var(--positive)}.is-neg{color:var(--negative)}.is-neu{color:var(--neutral)}.is-cau{color:var(--caution)}
.status{display:inline-flex;align-items:center;gap:4px;font-size:12px;line-height:16px;font-weight:600;white-space:nowrap}
.text-label{font-size:12px;line-height:16px;font-weight:600}
.text-meta{font-size:12px;line-height:16px;font-weight:400;color:var(--text-3)}
.text-figure-large{font-size:20px;line-height:28px;font-weight:600;font-variant-numeric:tabular-nums}
.ink-2{color:var(--text-2)}
.action-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.segmented{display:inline-flex;flex:none}
.seg{position:relative;display:inline-flex;align-items:center;justify-content:center;min-width:32px;height:32px;margin-left:-1px;padding:0 8px;border:1px solid var(--border-strong);border-radius:0;background:var(--surface);color:var(--text-2);font-size:12px;line-height:16px;font-weight:600;text-decoration:none}
.seg:first-child{margin-left:0;border-radius:var(--radius-2) 0 0 var(--radius-2)}
.seg:last-child{border-radius:0 var(--radius-2) var(--radius-2) 0}
.seg:hover{color:var(--text-1)}
.seg[aria-current]{background:var(--surface-sunken);color:var(--text-1)}
.seg[aria-current]::after{content:"";position:absolute;left:-1px;right:-1px;bottom:-1px;height:2px;background:var(--accent)}
.seg:focus-visible{z-index:1}
.pane-note{display:flex;align-items:center;min-height:32px;padding:8px 16px 7px;font-size:12px;line-height:16px;color:var(--text-2);border-bottom:1px solid var(--border-subtle)}
.group-title{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px;min-width:0;font-size:16px;line-height:24px;font-weight:600}
.group-count{font-size:12px;line-height:16px;font-weight:400;color:var(--text-3);font-variant-numeric:tabular-nums}
.row{position:relative;display:grid;grid-template-rows:20px 16px;row-gap:4px;height:56px;padding:8px 0 7px;border-bottom:1px solid var(--border-subtle);color:var(--text-1);text-decoration:none;transition:background-color var(--dur) ease-out}
.row:hover{background:var(--row-hover)}
.row.is-static:hover{background:transparent}
.r1,.r2{display:flex;min-width:0}
.r1{align-items:baseline;gap:8px}
.r-title{flex:1 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-weight:500}
.r-cv{flex:none;width:40px;text-align:right;font-weight:500;font-variant-numeric:tabular-nums}
.r-seen{flex:none;min-width:44px;text-align:right;font-size:12px;line-height:16px;color:var(--text-3);font-variant-numeric:tabular-nums;white-space:nowrap}
.r2{align-items:center;gap:8px}
.r-meta{flex:0 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:12px;line-height:16px;color:var(--text-3)}
.mk-pass{color:var(--text-3)}.mk-fail{color:var(--negative)}.mk-nj{color:var(--neutral)}.mk-ns{color:var(--neutral)}
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
/* ---- the tiles ---- */
.ht{container:ht / inline-size;min-width:0;background:var(--ht-surface);border:1px solid var(--ht-line);border-radius:var(--radius-4);color:var(--text-1);--focus-gap:var(--ht-surface)}
.ht-in{display:flex;flex-direction:column;gap:16px;height:100%;padding:16px}
.ht-head{display:flex;flex-direction:column;gap:2px}
.ht-title{font-size:16px;line-height:24px;font-weight:600;color:var(--text-1)}
.ht-body{display:flex;flex-direction:column;gap:12px;min-width:0}
.ht-say{font-size:14px;line-height:20px;color:var(--text-1);text-wrap:pretty;max-width:64ch}
.ht-foot{padding-top:4px;margin-top:auto}
.ht-links{display:flex;flex-wrap:wrap;gap:4px 24px}
.ht .empty{padding:0}
@container ht (min-width:480px){.ht-in{gap:20px;padding:20px 24px 20px}}
.ht-funnel{display:flex;flex-direction:column;gap:4px;margin:0 -8px}
.ht-step{display:grid;grid-template-columns:minmax(0,1fr) auto auto 16px;grid-template-areas:"name fig new go" "bar bar bar bar";align-items:center;column-gap:8px;row-gap:6px;min-height:24px;padding:6px 8px 8px;border-radius:var(--radius-2);color:var(--text-1);text-decoration:none}
.ht-step:hover{background:var(--row-hover)}
.ht-step:hover .ht-step-name{text-decoration:underline;text-underline-offset:2px}
.ht-step-name{grid-area:name;min-width:0;font-size:14px;line-height:20px;font-weight:500}
.ht-step-fig{grid-area:fig;font-size:14px;line-height:20px;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.ht-step-new{grid-area:new;min-width:5ch;font-size:12px;line-height:16px;color:var(--text-2);white-space:nowrap}
.ht-step-go{grid-area:go;display:inline-flex;color:var(--text-3)}
.ht-step-track{grid-area:bar;display:block;height:10px;background:var(--ht-track);border-radius:2px}
.ht-step-bar{display:block;width:var(--w);min-width:4px;height:100%;background:var(--ht-series-1);border-radius:2px}
.ht-step-bar.is-zero{min-width:0}
@container ht (min-width:480px){.ht-step{grid-template-columns:10rem minmax(0,1fr) 4ch 6ch 16px;grid-template-areas:"name bar fig new go";row-gap:0;padding-block:8px}.ht-step-track{height:14px}}
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
.apm-label{color:var(--text-1);text-decoration:underline;text-decoration-color:var(--border-strong);text-underline-offset:3px}
.apm-link:hover .apm-label{text-decoration-color:currentColor}
.apm-count{text-align:right;font-size:14px;line-height:20px;font-weight:600;color:var(--text-1);font-variant-numeric:tabular-nums}
.apm-pips{display:flex;flex-wrap:wrap;gap:2px;justify-content:flex-end}
.apm-pip{width:8px;height:12px;background:var(--text-3)}
.apm-pip.is-closed{background:transparent;box-shadow:inset 0 0 0 1px var(--text-3)}
.apm > .empty{padding:0}
.apm-split{font-size:12px;line-height:16px;color:var(--text-3)}
.apm-wait-head{font-size:12px;line-height:16px;font-weight:600;color:var(--text-1)}
.apm-wait-head .apm-wn{font-weight:400;color:var(--text-3)}
.apm-wait{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.apm-wait li{display:flex;flex-wrap:wrap;align-items:center;gap:0 8px;min-width:0;font-size:12px;line-height:16px}
.apm-wait .link{font-size:14px;line-height:20px}
.apm-wait .apm-more{color:var(--text-3)}
@container apm (min-width:520px){
  .apm-cols{grid-template-columns:repeat(6,minmax(0,1fr))}
  .apm-link{grid-template-columns:minmax(0,1fr);grid-template-rows:28px 32px auto;align-content:start;align-items:start;gap:4px;min-height:84px;height:100%;padding:0 8px 4px;border-bottom:0;border-left:1px solid var(--border-subtle)}
  .apm-col:first-child .apm-link{padding-left:0;border-left:0}
  .apm-count{grid-row:1;font-size:20px;line-height:28px;text-align:left}
  .apm-name{grid-row:2}
  .apm-pips{grid-row:3;justify-content:flex-start}
  .apm-wait{flex-direction:row;flex-wrap:wrap;gap:4px 24px}
}
/* ---- the welcome ---- */
.ht-welcome{container:htw / inline-size;min-width:0;color:var(--text-1)}
.ht-welcome-in{display:flex;flex-direction:column;gap:4px;padding:20px 16px}
.ht-date{font-size:14px;line-height:20px;color:var(--text-2)}
.ht-greet{font-size:24px;line-height:32px;font-weight:600;color:var(--text-1);text-wrap:balance}
.ht-figs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:16px}
.ht-figlink{display:flex;flex-direction:column;gap:2px;min-height:24px;padding:12px 14px;background:var(--ht-surface);border:1px solid var(--ht-line);border-radius:var(--radius-4);color:var(--text-1);text-decoration:none;--focus-gap:var(--canvas)}
.ht-figlink:hover{border-color:var(--border-strong)}
.ht-figlink:hover .ht-figword{text-decoration:underline;text-underline-offset:2px}
.ht-fignum{font-size:32px;line-height:40px;font-weight:600;font-variant-numeric:tabular-nums}
.ht-figword{font-size:14px;line-height:20px;color:var(--text-2)}
/* A figure at 0 steps back, so the ones with a number stand out. */
.ht-figlink.is-zero{background:transparent;border-color:var(--border-subtle)}
.ht-figlink.is-zero .ht-fignum,.ht-figlink.is-zero .ht-figword{color:var(--text-3);font-weight:400}
.ht-strip{display:flex;flex-wrap:wrap;align-items:center;gap:12px 20px;margin-top:12px;padding-top:12px;border-top:1px solid var(--ht-line)}
.ht-strip-list{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px}
.ht-strip-job{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:20px}
.ht-light{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:20px;font-weight:600;white-space:nowrap}
.ht-lamp{display:inline-flex;flex:none;align-items:center;justify-content:center;width:20px;height:20px;border:1.5px solid currentColor;border-radius:50%}
.ht-job-name{font-size:12px;line-height:16px;font-weight:500;color:var(--text-1)}
.ht-health-btn{margin-left:auto;--focus-gap:var(--hf-hero-a)}
.ht-chev{display:inline-flex;transition:transform var(--dur) ease-out}
.ht-health-btn[aria-expanded="true"] .ht-chev{transform:rotate(180deg)}
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
@container htw (min-width:720px){.ht-welcome-in{padding:24px 24px}.ht-greet{font-size:28px;line-height:36px}.ht-figs{grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}
/* ---- Needs you today: every item in full, one under another ---- */
.ny{container:ny / inline-size;min-width:0;background:var(--surface);border:1px solid var(--border-subtle);border-radius:var(--radius-4)}
.ny-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;min-height:56px;padding:12px 16px;border-bottom:1px solid var(--border-subtle)}
.ny-title{font-size:16px;line-height:24px;font-weight:600;color:var(--text-1)}
.ny-count{margin-left:8px;font-size:14px;font-weight:400;color:var(--text-3)}
.ny-tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-left:auto}
.ny-group > .group-title{padding:12px 16px;background:var(--surface-sunken);border-bottom:1px solid var(--border-subtle)}
.ny-item{display:flex;flex-direction:column;gap:16px;min-width:0;padding:20px 16px;border-bottom:1px solid var(--border-subtle);scroll-margin-top:8px}
.ny-group:last-child > .ny-item:last-child{border-bottom:0}
.ny-item:focus{outline:none}
.ny-item:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.ny-item:target{box-shadow:inset 3px 0 0 var(--accent)}
.ny-ih{display:flex;flex-direction:column;gap:6px}
.ny-it{max-width:80ch;font-size:18px;line-height:24px;font-weight:600;text-wrap:balance}
.ny-im{display:flex;flex-wrap:wrap;align-items:center;gap:4px 16px;font-size:12px;line-height:16px;color:var(--text-3)}
.ny-ib{display:flex;flex-direction:column;gap:20px;min-width:0}
.ny-para{max-width:80ch;text-wrap:pretty}
.ny-para + .ny-para{margin-top:8px}
.ny-fig{display:flex;align-items:baseline;gap:8px;margin-top:12px}
.ny-basis{max-width:80ch;margin-top:2px;text-wrap:pretty}
.ny-under{max-width:80ch;margin-top:8px;text-wrap:pretty}
.ny-context{display:flex;flex-direction:column;gap:12px}
.ny-ctx-part .sec-h{font-size:12px;line-height:16px;color:var(--text-2)}
.ny-ctx-part .row{max-width:80ch}
.ny-onfile{align-self:flex-start}
.ny-gone{color:var(--caution)}
/* An urgent signal, as Insights draws a story. */
.ny-story{gap:8px}
.ny-story .story-line{color:var(--text-3);font-size:13px;line-height:20px;overflow-wrap:anywhere}
.ny-story .story-line .co{color:var(--text-2);font-weight:600}
.ny-move{margin-top:4px}
.ny-story .story-h{max-width:60ch;font:500 22px/28px var(--font-serif);letter-spacing:-.005em;overflow-wrap:anywhere}
.ny-story .story-h a{color:var(--text-1);text-decoration:none}
.ny-story .story-h a:hover{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:4px}
.ny-story .story-sum{max-width:72ch;color:var(--text-2);font-size:15px;line-height:22px;text-wrap:pretty}
.ny-story .story-meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 16px;color:var(--text-3);font-size:13px;line-height:20px}
.ny-story .story-meta a{color:var(--accent);white-space:nowrap}
.ny-story .story-meta a:hover{color:var(--accent-hover)}
.ny-story .story-meta a svg{vertical-align:-1px;margin-left:3px}
.ny-story .ny-acts{margin-top:8px}
@container ny (max-width:599.98px){.ny-tools{margin-left:0}.facts{grid-template-columns:112px minmax(0,1fr)}}
/* ---- the page: the hero, the two tiles, the list ---- */
.hf{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-content:start;padding:12px 0 24px;background:var(--canvas)}
.hf > .ht-welcome,.hf > .hf-bento{margin:0 12px}
.hf > .ny{border-radius:0;border-left:0;border-right:0}
.hf-hero{background:linear-gradient(135deg,var(--hf-hero-a),var(--hf-hero-b) 70%);border:1px solid var(--hf-hero-line);border-radius:var(--radius-4);--focus-gap:var(--hf-hero-a)}
.hf-hero .ht-welcome-in{padding:20px 16px 16px}
.hf-hero .ht-greet{font-size:28px;line-height:36px}
.hf-hero .ht-strip{border-top-color:var(--hf-hero-line)}
.hf-bento{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:stretch}
/* Five figures: two, two, then the fifth across the width with its words beside the number;
   three and two from 720px of welcome; five across from 960px. */
@container htw (max-width:719.98px){.ht-figs-5 > .ht-figlink:last-child{grid-column:1 / -1;flex-direction:row;align-items:baseline;gap:12px}}
@container htw (min-width:720px) and (max-width:959.98px){.ht-figs.ht-figs-5{grid-template-columns:repeat(6,minmax(0,1fr))}.ht-figs-5 > .ht-figlink{grid-column:span 2}.ht-figs-5 > .ht-figlink:nth-child(n + 4){grid-column:span 3}}
@container htw (min-width:960px){.ht-figs.ht-figs-5{grid-template-columns:repeat(5,minmax(0,1fr))}}
@container htw (min-width:720px){.hf-hero .ht-welcome-in{padding:24px 24px 16px}.hf-hero .ht-figs{margin-top:12px}.hf-hero .ht-strip{margin-top:16px}}
@container htw (min-width:720px) and (max-width:1599.98px){.hf-hero .ht-figlink{flex-direction:row;align-items:baseline;gap:10px;padding:10px 16px}}
@container htw (min-width:960px) and (max-width:1599.98px){.hf-hero .ht-figs.ht-figs-5{grid-template-columns:repeat(5,minmax(max-content,1fr))}}
@container htw (min-width:1600px){
  .hf-hero .ht-welcome-in{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);grid-template-areas:"date figs" "greet figs" "strip strip" "panel panel";column-gap:64px;row-gap:6px;padding:40px 40px 20px}
  .hf-hero .ht-date{grid-area:date;align-self:end;font-size:16px;line-height:24px}
  .hf-hero .ht-greet{grid-area:greet;align-self:start;font-size:40px;line-height:52px}
  .hf-hero .ht-figs{grid-area:figs;align-self:center;margin-top:0;gap:16px}
  .hf-hero .ht-figs.ht-figs-5{grid-template-columns:repeat(5,minmax(max-content,1fr))}
  .hf-hero .ht-figlink{padding:20px 20px}
  .hf-hero .ht-fignum{font-size:48px;line-height:56px}
  .hf-hero .ht-figword{font-size:16px;line-height:24px}
  .hf-hero .ht-strip{grid-area:strip;margin-top:24px;padding-top:16px}
  .hf-hero .ht-health{grid-area:panel}
}
@container app (min-width:768px){
  .hf{gap:16px;padding:16px}
  .hf > .ht-welcome,.hf > .hf-bento{margin:0}
  .hf > .ny{border-radius:var(--radius-4);border-left:1px solid var(--border-subtle);border-right:1px solid var(--border-subtle)}
  .hf-bento{gap:16px}
}
@container app (min-width:1024px){.hf-bento{grid-template-columns:minmax(0,3fr) minmax(0,2fr)}}
/* Under 768 every target is 44px. */
@media (max-width:767.98px){
  html{scroll-padding-top:8px}
  .btn,.seg{height:44px}
  .undo-last{padding-inline:8px}
  .ht-step,.ht .link-block,.ht-figlink,.apm-link,.link-block,.row{min-height:44px}
  .apm-wait li .link{display:inline-flex;align-items:center;min-height:44px}
  .ny-story .story-meta a{display:inline-flex;align-items:center;min-height:44px}
  .ny-story .story-h{font-size:20px;line-height:26px}
  .table th:first-child,.table td:first-child{width:112px}
  .table th:nth-child(2),.table td:nth-child(2){width:80px}
}
@media (pointer:coarse){
  .btn,.seg{height:44px}
  .ht-step,.ht .link-block,.ht-figlink,.apm-link,.link-block{min-height:44px}
  .apm-wait li .link{display:inline-flex;align-items:center;min-height:44px}
  .ny-story .story-meta a{display:inline-flex;align-items:center;min-height:44px}
}
@media (forced-colors:active){.ny-item:target{border-left:3px solid Highlight}.tag,.ht-lamp{border:1px solid CanvasText}}
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
  function healthOpen() {
    var btn = el('health-btn');
    return !!btn && btn.getAttribute('aria-expanded') === 'true';
  }

  // ---- redrawing after a write: the main element, from the server
  function refresh(item, focusIt) {
    var open = healthOpen();
    return fetch('/' + (query(item) || '?') + (query(item) ? '&' : '') + 'part=main',
                 { headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) {
        main.innerHTML = j.html;
        document.title = j.title;
        setHealth(open);
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
    say('The signal watcher is running. This can take up to ten minutes.');
    fetch('/watch', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || j.ok === false) { say((j && j.note) || 'The run did not finish.', true); }
        else { say('The signal watcher ran.'); }
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
