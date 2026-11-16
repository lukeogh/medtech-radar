"""Home: the welcome, the three tiles and Needs you today.

Built from Home final (design/round-1 branch, design/home/final, 15 Nov 2026)
and the owner's decisions of that day in design/README.md, on the shared frame
in page_frame.py, dark only. At /, with every item at its own address,
/?item=<id>, and the items taken off Home at /?show=done.

What is on it, top to bottom:
- The welcome: the date, the greeting (by the first name on the optional
  first_name line of the owner's preferences file, never a name in the code),
  one sentence saying what needs the owner, and four figures, each a link to
  where those items live and each equal to a group of the list below: urgent
  signals, new Top prospects, new One question away roles and application
  follow-ups due. Threads due joins when the Watchlist page is built.
- Radar's five jobs as lights, a mark in a ring, a colour and a word (On time,
  Late, Stale, Off, Dry run or, for the inbox, Quiet), from
  radar_common.job_health, the verdict
  /health gives. The phone push has no run of its own: it rides on the signal
  watcher, so its light is worked out from the watcher's runs and the ntfy
  address in radar.yaml (push_light).
- Applications: the board's six stages and what waits on the owner, read from
  the applications table (applications.load, board_page.board_model).
- Open roles: the four tiers as the Jobs tabs count them, with today's new.
- Week by week: new Top prospects and signals at 40 or more over the last
  four weeks, rebuilt from what is stored (week_model says exactly how).
- Needs you today: Radar's jobs that need a look, urgent signals, new Top
  prospects, new One question away roles and application follow-ups due, with
  the selected item's detail beside the list, or under its row on a phone.

Done (key E) takes an item off Home and only off Home, in the home_done table;
Undo last puts it back. It never changes the item where it lives. Everything
else here reads: the page is drawn from one build_dashboard.collect() and a few
reads of its own on the same read-only connection, and its script writes only
through POST /home/done and /home/undone (and /watch, the signal watcher's
Check now, which the server already had).
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone

import applications as ap
import build_dashboard as bd
import jobs_page as jp
import page_frame
import radar_common as rc

esc = bd.esc

WEEKS = 4                 # the weeks the trend draws, this one included
SIGNAL_BAR = 40           # the mark of a buying window (insights_page.BUYING)
URGENT_DAYS = 7           # an urgent signal is on Home for the week it was found
WAIT_SHOWN = 3            # Waiting on you items the tile lists
KEY_MAX = 300             # the longest item key the routes take
KEY_RE = re.compile(r"^(signal|role|app|radar):.+")

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
# normal, not a fault.
LIGHTS = {"On time": ("is-pos", "st-pos"), "Late": ("is-cau", "st-late"),
          "Dry run": ("is-cau", "st-cau"), "Stale": ("is-neg", "st-neg"),
          "Off": ("is-neu", "st-neu"), "Quiet": ("is-neu", "st-quiet")}
# A light that is a fault, and so an item under Radar in Needs you today and a
# part of the sentence. Dry run is the box as it is set up, and Quiet is normal.
FAULTS = ("Late", "Stale", "Off")
# Only these say Dry run (decision 5, 15 Nov): the digest is built and not
# sent, the push is written to a file. The signal watcher and the advert
# checker in dry run still read and store, so their lights are their state.
DRY_JOBS = ("digest", "push")
# The marks the lights add to the page's symbols.
SYMBOLS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
<symbol id="st-late" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 4.8V8l2.2 1.5" fill="none" stroke="currentColor" stroke-width="1.6"/></symbol>
<symbol id="st-quiet" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="2.2 2.2"/></symbol>
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
          ("apps", "Applications"))


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

def _dry(name: str, verdict: dict) -> bool:
    mode = str(verdict.get("mode") or "")
    if name == "digest":
        return mode not in ("", "build", "commit")
    return mode in ("dry-run", "mock")


def job_light(name: str, verdict: dict | None) -> dict:
    """One job's light from its job_health verdict: never is Off, stale is
    Stale, late, failing or partial (a run that finished with failures) is
    Late, except the inbox's late, which is Quiet; and the digest on time
    whose last build was a dry run says Dry run."""
    v = dict(verdict or {"state": "never"})
    state = v.get("state") or "never"
    if state == "never":
        word = "Off"
    elif state == "stale":
        word = "Stale"
    elif state == "late" and name == "inbox":
        word = "Quiet"
    elif state in ("late", "failing", "partial"):
        word = "Late"
    else:
        word = "Dry run" if name in DRY_JOBS and _dry(name, v) else "On time"
    return {"name": name, "word": word, "verdict": v, "state": state}


def push_light(jobs: dict, config: dict, last_push) -> dict:
    """The phone push has no run of its own, so its light is worked out from
    what is stored: Off when radar.yaml names no ntfy address or the watcher
    has never run, then the watcher's own Stale or Late, since a push goes
    only when the watcher runs, then Dry run while the watcher runs in dry run
    (it writes its pushes to a file), and otherwise On time."""
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
        light["word"] = "Dry run"
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
    return " · ".join(bits)


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
        tag = f"Stale · {_hours(hours)}" if hours is not None else "Stale"
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
            "meta": f"{company} · read from {source}", "tag": "",
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
            "meta": f"{a.get('title') or 'Untitled role'} · {ap.STAGE_WORDS[stage]}",
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
    company_ids = {i.get("company_id") for i in shown if i.get("company_id")}
    return {
        "now": moment, "show": show, "items": shown, "todo": todo, "done": gone,
        "selected": selected, "missing": missing, "counts": counts, "overdue": overdue,
        "lights": light_list, "jobs": bd.job_verdicts(data), "jmodel": jmodel, "bmodel": bmodel,
        "week": week_model(data, top_history(conn), moment),
        "name": rc.read_first_name(config), "data": data, "config": config,
        "companies": companies(conn, company_ids),
    }


# ================================================================ the welcome

FIGURES = (("urgent", "urgent signal", "urgent signals", "#group-urgent"),
           ("top", "new Top prospect", "new Top prospects", "/jobs#group-top"),
           ("question", "new One question away", "new One question away", "/jobs#group-question"),
           ("apps", "application follow-up due", "application follow-ups due",
            "/jobs?show=applied"))
JOB_PHRASES = {"inbox": "the inbox", "signals": "the signal watcher", "check": "the advert checker",
               "digest": "the Monday digest", "push": "the phone push"}
FAULT_WORDS = {"Stale": "is stale", "Late": "is late", "Off": "is off"}


def greeting(now: datetime, name: str | None) -> str:
    hour = uk(now).hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    return f"Good {part}, {name}" if name else f"Good {part}."


def apps_word(model: dict, count: int) -> str:
    base = "application follow-up" if count == 1 else "application follow-ups"
    return f"{base} due today" if not model["overdue"] else f"{base} due or overdue"


def status_sentence(model: dict) -> str:
    c = model["counts"]
    parts = []
    if c["urgent"]:
        parts.append(_n(c["urgent"], "urgent signal"))
    if c["top"]:
        parts.append(_n(c["top"], "new Top prospect"))
    if c["question"]:
        parts.append(f"{c['question']} new One question away {'role' if c['question'] == 1 else 'roles'}")
    if c["apps"]:
        parts.append(f"{c['apps']} {apps_word(model, c['apps'])}")
    for i in model["todo"]:
        if i["group"] == "radar":
            light = i["light"]
            parts.append(f"{JOB_PHRASES[light['name']]} {FAULT_WORDS[light['word']]}")
    if not parts:
        return "Nothing needs you today."
    n = len(parts)
    said = parts[0] if n == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    lead = f"{bd.words(n).capitalize()} {'thing needs' if n == 1 else 'things need'} you"
    return f"{lead}: {said}."


def quiet_line(model: dict) -> str:
    """On a quiet day, what Radar read and when, as it is stored."""
    jobs = model["jobs"]
    data = model["data"]
    bits = []
    sig = jobs.get("signals") or {}
    if sig.get("last_run"):
        answering = data.get("answering") or 0
        watched = len(data.get("watchlist") or [])
        # Only once the sources have been checked is there a count to give.
        reading = (f", with {answering} of {watched} sources answering"
                   if watched and data.get("checked") else "")
        bits.append(f"The signal watcher last ran at {hm(sig['last_run'])}{reading}")
    inbox = jobs.get("inbox") or {}
    if inbox.get("last_run"):
        bits.append(f"the inbox at {hm(inbox['last_run'])}")
    if not bits:
        return "Radar has not run yet."
    said = bits[0] + (" and " + bits[1] if len(bits) > 1 else "")
    return said[:1].upper() + said[1:] + "."


def figures_html(model: dict) -> str:
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
    return (f'<nav class="ht-figs ht-figs-4" aria-label="What needs you, by kind">'
            f'{"".join(out)}</nav>')


def welcome_html(model: dict) -> str:
    now = uk(model["now"])
    sentence = status_sentence(model)
    quiet = (f'<p class="ht-quiet">{esc(quiet_line(model))}</p>'
             if sentence == "Nothing needs you today." else "")
    strip = "".join(light_html(l) for l in model["lights"])
    return (
        '<section class="ht-welcome hf-hero" id="welcome" aria-labelledby="welcome-h">'
        '<div class="ht-welcome-in">'
        f'<p class="ht-date">{now:%A} {now.day} {now:%B}</p>'
        f'<h1 class="ht-greet" id="welcome-h"><span class="visually-hidden">Home. </span>'
        f'{esc(greeting(model["now"], model["name"]))}</h1>'
        f'<p class="ht-status" id="status">{esc(sentence)}</p>{quiet}'
        f'<p class="ht-asof"><span>As of {now:%H:%M}. Times are UK time.</span>'
        '<a class="link ht-settings" href="/cv#settings">Settings, on the CV page</a></p>'
        f'{figures_html(model)}'
        '<div class="ht-strip"><ul class="ht-strip-list" aria-label="Radar\'s five jobs">'
        f'{strip}</ul><a class="link link-block" href="/radar">Radar\'s health</a></div>'
        '</div></section>')


# ================================================================ the tiles

def apps_tile(model: dict) -> str:
    b = model["bmodel"]
    now = model["now"]
    total = sum(b["totals"].values())
    head = ('<header class="ht-head"><h2 class="ht-title" id="tile-applications">Applications</h2>'
            '<p class="ht-q">Where do my applications stand?</p></header>')
    if not total:
        body = ('<div class="apm"><div class="empty"><p>No applications yet. Radar adds an item '
                'when you save a tailored CV or mark a role applied.</p>'
                '<a class="link link-block" href="/jobs#group-top">Open Top prospects on Jobs</a>'
                '</div></div>')
        return (f'<section class="ht ht-apps-tile" aria-labelledby="tile-applications"><div class="ht-in">'
                f'{head}<div class="ht-body">{body}</div></div></section>')
    cols = []
    said = []
    for stage in ap.STAGES:
        n = b["totals"][stage]
        said.append(f"{n} {ap.STAGE_WORDS[stage]}")
        pips = "".join(f'<span class="apm-pip{" is-closed" if stage == "closed" else ""}"></span>'
                       for _ in range(min(n, 24)))
        by = '<small>Filled by Radar</small>' if stage in ("preparing", "applied") else ""
        cols.append(f'<li class="apm-col"><a class="apm-link" id="mini-{stage}" '
                    f'href="/jobs?show=applied#col-{stage}"><span class="apm-name">'
                    f'<span class="apm-label">{ap.STAGE_WORDS[stage]}</span>{by}</span>'
                    f'<span class="apm-pips" aria-hidden="true">{pips}</span>'
                    f'<span class="apm-count">{n}</span></a></li>')
    label = (f"Applications by column. {_n(total, 'application')}: "
             + ", ".join(said[:-1]) + " and " + said[-1] + ".")
    parts = [f'<ol class="apm-cols" aria-label="{esc(label)}">{"".join(cols)}</ol>',
             '<p class="apm-radar">Radar fills Preparing and Applied by itself.</p>']
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
    j = model["jmodel"]
    t = j["totals"]
    # New Top and One question away are the figures' counts, so a role Done
    # on Home is not new here either; Reading's new are Jobs' own.
    new = {"reading": sum(1 for o in j["tiers"]["reading"] if o.get("_unread")),
           "question": model["counts"]["question"], "top": model["counts"]["top"]}
    steps = (("all", "All open", j["open_total"], 0, "/jobs"),
             ("reading", "Reading", t["reading"], new["reading"], "/jobs?reading=1#group-reading"),
             ("question", "One question away", t["question"], new["question"], "/jobs#group-question"),
             ("top", "Top prospects", t["top"], new["top"], "/jobs#group-top"))
    top = math.sqrt(max(1, j["open_total"]))
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
    notes = [f"{t['filtered']} filtered out."]
    unscored = len(j.get("unscored") or [])
    if unscored:
        notes.append(f"{_n(unscored, 'role')} not scored yet.")
    notes.append("Each step counts its own tier, as the Jobs tabs do. Bar lengths on a "
                 "square-root scale. A role you have acknowledged or applied for is not counted.")
    return ('<section class="ht ht-funnel-tile" aria-labelledby="tile-open-roles"><div class="ht-in">'
            '<header class="ht-head"><h2 class="ht-title" id="tile-open-roles">Open roles</h2>'
            '<p class="ht-q">Where do the open roles narrow?</p></header><div class="ht-body">'
            f'<ol class="ht-funnel" aria-label="Open roles by tier, each a link to Jobs">{"".join(lis)}</ol>'
            f'<p class="ht-note">{esc(" ".join(notes))}</p></div>'
            '<p class="ht-foot"><a class="link link-block" href="/jobs">Open Jobs</a></p>'
            '</div></section>')


# The tile's small print: exactly what each bar counts (week_model has the rest).
WEEK_NOTE = ("Weeks start on Monday; this week runs Monday to now. A bar counts the roles first "
             "scored Top that week, from the score record, a role you applied for left out, and "
             "the stories found that week that scored 40 or more, hiring stories left out as on "
             "Insights. Each row has its own scale, so compare the figures, not the bar heights "
             "across rows.")


def trend_tile(model: dict) -> str:
    w = model["week"]
    head = ('<header class="ht-head"><h2 class="ht-title" id="tile-week-by-week">Week by week</h2>'
            '<p class="ht-q">Is the flow picking up?</p></header>')
    stale = next((l for l in model["lights"] if l["name"] == "signals" and l["word"] == "Stale"), None)
    stale_note = ""
    if stale and stale["verdict"].get("last_run"):
        stale_note = (f" Signals as of {day_time(stale['verdict']['last_run'])}, "
                      f"{_hours(stale['verdict'].get('hours_ago'))} old.")
    if w["first_week"]:
        this = w["this"]
        starts = w["starts"]
        body = ('<div class="ht-firsts">'
                f'<p class="ht-first"><span class="ht-first-fig"><span class="ht-swatch ht-swatch-1" '
                f'aria-hidden="true"></span>{this["top"]}</span><span class="ht-first-say">'
                f'{"Top prospect" if this["top"] == 1 else "Top prospects"} this week so far</span></p>'
                f'<p class="ht-first"><span class="ht-first-fig"><span class="ht-swatch ht-swatch-2" '
                f'aria-hidden="true"></span>{this["signals"]}</span><span class="ht-first-say">'
                f'{"signal" if this["signals"] == 1 else "signals"} at 40 or more this week so far</span></p>'
                '</div>'
                f'<p class="ht-say">Trends start after your first full week, on Monday {starts.day} '
                f'{starts:%b}. Until then this tile counts the week so far.</p>'
                f'<p class="ht-note">{esc(WEEK_NOTE + stale_note)}</p>')
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
                f'<p class="ht-say">{esc(say)}</p>'
                f'<p class="ht-note">{esc(WEEK_NOTE + stale_note)}</p>')
    return ('<section class="ht ht-trend-tile" aria-labelledby="tile-week-by-week"><div class="ht-in">'
            f'{head}<div class="ht-body">{body}</div>'
            '<p class="ht-foot"><a class="link link-block" href="/insights">Open Insights</a></p>'
            '</div></section>')


# ================================================================ Needs you today

def row_html(i: dict, selected: bool, done_view: bool = False) -> str:
    fig = (f'<span class="r-cv"><span class="visually-hidden">{esc(i.get("fig_label", ""))}</span>'
           f'{esc(i["fig"])}</span>' if i.get("fig") else "")
    seen = (f'<span class="r-seen"><span class="visually-hidden">{esc(i.get("seen_label", ""))}</span>'
            f'{esc(i["seen"])}</span>' if i.get("seen") else "")
    current = ' aria-current="true"' if selected else ""
    return (f'<a class="row row-3{" is-unread" if i["kind"] == "role" else ""}" id="row-{esc(i["id"])}" '
            f'href="/?item={esc(i["id"])}{"&amp;show=done" if done_view else ""}" '
            f'data-item="{esc(i["id"])}" data-key="{esc(i["key"])}"'
            f'{current}><span class="r1"><span class="r-title">{esc(i["title"])}</span>{fig}{seen}</span>'
            f'<span class="r2"><span class="r-meta">{esc(i["meta"])}</span>{i.get("tag", "")}</span>'
            f'<span class="r3"><span class="r-lead">{esc(i.get("lead") or "")}</span>'
            f'{i.get("marks", "")}</span></a>')


def group_head(group: str, items: list, model: dict) -> str:
    n = len(items)
    title = dict(GROUPS)[group]
    meta = ""
    if group == "radar":
        count = f"{n} of {len(JOBS)} jobs need a look"
    elif group == "urgent":
        count = str(n)
        fast = int(model["data"].get("fast") or 75)
        stale = next((l for l in model["lights"] if l["name"] == "signals" and l["word"] == "Stale"), None)
        if stale and stale["verdict"].get("last_run"):
            meta = (f'<span class="head-meta">As of {esc(day_time(stale["verdict"]["last_run"]))}, '
                    f'{esc(_hours(stale["verdict"].get("hours_ago")))} old</span>')
        else:
            meta = f'<span class="head-meta">Relevance {fast} and over</span>'
    elif group in ("top", "question"):
        count = f"{n} new"
    else:
        late = sum(1 for i in items if ap.parse(i["app"].get("next_step_due")).date()
                   < uk(model["now"]).date())
        count = f"{n} due" + (f' <span class="group-new">· {late} overdue</span>' if late else " today")
    return (f'<div class="group-head"><h3 class="group-title" id="group-{group}">{esc(title)}'
            f'<span class="group-count">{count}</span></h3>{meta}</div>')


def list_html(model: dict) -> str:
    items = model["items"]
    sel = model["selected"]
    parts = []
    if any(i["kind"] == "role" for i in items):
        parts.append('<p class="pane-note">Scores are from the advert card unless tagged Full job spec.</p>')
    for group, _word in GROUPS:
        members = [i for i in items if i["group"] == group]
        if not members:
            continue
        rows = "".join(row_html(i, sel is not None and i["id"] == sel["id"], model["show"] == "done")
                       for i in members)
        parts.append(f'<section class="ny-group" aria-labelledby="group-{group}">'
                     f'{group_head(group, members, model)}{rows}</section>')
    return "".join(parts)


# ---- the detail of one item

def _facts(rows) -> str:
    return '<dl class="facts">' + "".join(
        f'<dt>{esc(k)}</dt><dd>{v}</dd>' for k, v in rows if v) + "</dl>"


def _sec(hid: str, title: str, body: str) -> str:
    return (f'<section class="sec" aria-labelledby="{hid}"><h4 class="sec-h" id="{hid}">{esc(title)}</h4>'
            f'{body}</section>')


def _people(raw) -> str:
    try:
        people = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return ""
    out = []
    for p in people if isinstance(people, list) else []:
        if isinstance(p, dict):
            name = p.get("name") or ""
            role = p.get("role") or p.get("title") or ""
            out.append(f"{name} ({role})" if role else name)
        elif p:
            out.append(str(p))
    return ", ".join(x for x in out if x)


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


def _context_radar(i: dict) -> str:
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
    return ('<div class="ny-context"><div class="group-head"><h4 class="group-title">Schedule</h4>'
            f'<span class="head-meta">{esc(SCHEDULE[name])}</span></div>'
            f'<div class="pane-body">{_facts(rows)}</div></div>')


def _body_signal(i: dict, uid: str) -> str:
    s = i["story"]
    lead = s["lead"]
    found = day_time(s["found"])
    if lead.get("source_id") == "job-advert":
        basis = f"Set by rule, not judged: every buying window is 90. Read from a job advert, found {found}."
    else:
        basis = f"Scored by the signal rubric. Read from {i['source']}, found {found}."
    out = [_sec(f"{uid}-why", "Why it matters",
                f'<p class="ny-para">{esc(lead.get("why") or i["lead"] or "No reason on file.")}</p>'
                f'<p class="ny-fig"><span class="text-label ink-2">Relevance</span>'
                f'<span class="text-figure-large">{s["relevance"]}</span></p>'
                f'<p class="text-meta ny-basis">{esc(basis)}</p>')]
    if lead.get("playbook_step"):
        under = ("No drafts. A buying window's next move is to send an article."
                 if s["type"] == "buying-window" else
                 "Drafts, where there are any, are on the story's page on Insights."
                 if s["type"] not in HIRING else "")
        out.append(_sec(f"{uid}-next", "Next move",
                        f'<p class="ny-para">{esc(lead["playbook_step"])}</p>'
                        + (f'<p class="text-meta ny-under">{esc(under)}</p>' if under else "")))
    role = i.get("role")
    if role is not None:
        read_from = (f'A job advert: <a class="link" href="/jobs/{int(role["id"])}">'
                     f'{esc(role.get("title") or "the role")}, on Jobs</a>')
    else:
        url = bd.safe_url(lead.get("source_url"))
        read_from = (f'<a class="link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                     f'{esc(i["source"])}<span class="visually-hidden"> (opens in a new tab)</span></a>'
                     if url else esc(i["source"]))
    phone = (f"Pushed to the phone {day(lead.get('pushed_at'))}." if lead.get("pushed_at")
             else "Not pushed to the phone.")
    rows = [("Type", esc(TYPE_WORDS.get(s["type"], "Other news"))), ("Read from", read_from),
            ("Found", esc(found)), ("Phone", esc(phone))]
    if len(s["reports"]) > 1:
        rows.append(("Reports", esc(f"{len(s['reports'])} sources told this story")))
    out.append(_sec(f"{uid}-details", "Details", _facts(rows)))
    return "".join(out)


def _body_role(i: dict, uid: str) -> str:
    o = i["role"]
    tier = jp.TIER_WORDS.get(o.get("tier") or "", "Not scored")
    gates = []
    for gate, name, _l in jp.GATES:
        cls, symbol, said = jp.gate_words(o, gate, name)
        gates.append((name, f'<span class="status {"is-neg" if cls == "mk-fail" else "is-neu" if cls == "mk-nj" else "is-pos"}">'
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
             f'<p class="text-meta ny-under">{esc(who)} Change it or clear it on the board.</p>']
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


def _context_company(i: dict, model: dict) -> str:
    cid = i.get("company_id")
    c = model["companies"].get(cid) if cid else None
    if not c:
        return ""
    name = c["display_name"]
    where = ", ".join(x for x in (c.get("city"), c.get("country")) if x)
    stage = str(c.get("stage") or "").replace("-", " ").strip()
    if stage.lower().startswith("series "):
        stage = "Series " + stage.split(" ", 1)[1].upper()
    facts = [("Builds", esc(c.get("what_they_build") or "Not known")),
             ("Software", esc(c.get("software_content") or "Not known")),
             ("Stage", esc(stage[:1].upper() + stage[1:] if stage else "Not known")),
             ("Where", esc(where or "Not known")),
             ("People", esc(_people(c.get("people")) or "Not known"))]
    data = model["data"]
    parts = [f'<div class="pane-body">{_facts(facts)}']
    thread = next((t for t in data.get("threads") or []
                   if (t.get("company_id") == cid) or str(t.get("company") or "").lower() == name.lower()),
                  None)
    if thread:
        when = []
        if thread.get("next_action_date"):
            when.append(f"Due {day(thread['next_action_date'])}")
        when.append(f"Last contact {day(thread.get('touched_at'))}, "
                    f"{CHANNEL_WORDS.get(thread.get('channel') or 'other', 'other')}.")
        parts.append(f'<section class="sec"><h5 class="sec-h">Thread</h5>'
                     f'<p class="ny-para">{esc(thread.get("next_action"))}</p>'
                     f'<p class="text-meta ny-under">{esc(" · ".join(when))}</p></section>')
    parts.append("</div>")
    stories = [s for s in _story_groups({"signals": (data.get("signals") or [])})
               if s["lead"].get("company_id") == cid]
    stories.sort(key=lambda s: s["found"], reverse=True)
    if stories:
        rows = []
        for s in stories[:3]:
            lead = s["lead"]
            this = i.get("story") is not None and i["story"]["key"] == s["key"]
            words = TYPE_WORDS.get(s["type"], "Other news") + (" · the signal shown here" if this else "")
            inner = (f'<span class="r1"><span class="r-title">{esc(rc.clean_headline(lead.get("headline") or ""))}</span>'
                     f'<span class="r-cv"><span class="visually-hidden">Relevance </span>{s["relevance"]}</span>'
                     f'<span class="r-seen"><span class="visually-hidden">Found </span>{esc(day(s["found"]))}</span></span>'
                     f'<span class="r2"><span class="r-meta">{esc(words)}</span></span>')
            if s["type"] in HIRING:
                rows.append(f'<div class="row row-2 is-static">{inner}</div>')
            else:
                rows.append(f'<a class="row row-2" href="/insights/story/{int(lead["id"])}">{inner}</a>')
        parts.append(f'<div class="group-head"><h5 class="group-title">Signals on file'
                     f'<span class="group-count">{len(stories)}</span></h5></div>{"".join(rows)}')
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
                        f'<span class="r2"><span class="r-meta">{esc(" · ".join(bits))}</span></span></a>')
        parts.append(f'<div class="group-head"><h5 class="group-title">Roles on Jobs'
                     f'<span class="group-count">{len(roles)}</span></h5></div>{"".join(rows)}')
    parts.append(f'<div class="pane-body"><a class="link link-block" href="/company/{int(cid)}">'
                 f'Everything on file for {esc(name)}</a></div>')
    return (f'<div class="ny-context"><div class="group-head"><h4 class="group-title">On file for '
            f'{esc(name)}</h4></div>{"".join(parts)}</div>')


def detail_html(i: dict, model: dict, prev_id, next_id) -> str:
    uid = f"d-{i['id']}"
    done_view = model["show"] == "done"
    q = "&show=done" if done_view else ""
    meta_bits = {"radar": ["Radar", SCHEDULE[i["light"]["name"]] if i["kind"] == "radar" else ""],
                 "signal": ["Urgent signal", i.get("company") or "",
                            TYPE_WORDS.get((i.get("story") or {}).get("type"), ""),
                            f"found {day_time((i.get('story') or {}).get('found'))}"],
                 "role": [dict(GROUPS).get(i["group"], ""), i.get("company") or "",
                          f"first seen {day_time(jp.first_seen(i['role']))}" if i.get("role") else ""],
                 "app": ["Application follow-up", i.get("company") or "",
                         ap.STAGE_WORDS.get((i.get("app") or {}).get("stage") or "applied", "")]}[i["kind"]]
    meta = '<span aria-hidden="true">·</span>'.join(f"<span>{esc(b)}</span>" for b in meta_bits if b)
    if i["kind"] == "radar" and i.get("tag"):
        meta += i["tag"]
    prev = (f'<a class="btn btn-secondary" href="/?item={esc(prev_id)}{q}" data-step="-1" rel="prev" '
            'aria-keyshortcuts="K">Previous<kbd class="kbd" aria-hidden="true">K</kbd></a>'
            if prev_id else '<span class="btn btn-secondary is-disabled" aria-disabled="true">Previous'
            '<kbd class="kbd" aria-hidden="true">K</kbd></span>')
    nxt = (f'<a class="btn btn-secondary" href="/?item={esc(next_id)}{q}" data-step="1" rel="next" '
           'aria-keyshortcuts="J">Next<kbd class="kbd" aria-hidden="true">J</kbd></a>'
           if next_id else '<span class="btn btn-secondary is-disabled" aria-disabled="true">Next'
           '<kbd class="kbd" aria-hidden="true">J</kbd></span>')
    # Open goes where the item lives.
    if i["kind"] == "radar":
        n8n = bd.safe_url(model["config"].get("n8n_url"))
        open_ = ((n8n, "Open the workflow scheduler", True) if n8n
                 else ("/radar", "Open Radar's health", False))
    else:
        open_ = i.get("open")
    acts = []
    if open_:
        href, label, external = open_
        ext = (' target="_blank" rel="noopener noreferrer"' if external else "")
        tail = '<span class="visually-hidden"> (opens in a new tab)</span>' if external else ""
        acts.append(f'<a class="btn btn-primary" href="{esc(href)}" data-open{ext} '
                    f'aria-keyshortcuts="Enter">{esc(label)}{tail}<kbd class="kbd" aria-hidden="true">Enter</kbd></a>')
    if done_view:
        acts.append(f'<button type="button" class="btn btn-secondary" data-back="{esc(i["key"])}" '
                    f'data-title="{esc(i["title"])}" aria-keyshortcuts="E">Put back on Home'
                    '<kbd class="kbd" aria-hidden="true">E</kbd></button>')
        note = "Put back returns it to To do. It was only ever off Home."
    else:
        acts.append(f'<button type="button" class="btn btn-secondary" data-done="{esc(i["key"])}" '
                    f'data-title="{esc(i["title"])}" aria-keyshortcuts="E">Done'
                    '<kbd class="kbd" aria-hidden="true">E</kbd></button>')
        if i["kind"] == "radar":
            note = ("Done takes this off Home until this fault ends, and Undo last puts it back. "
                    "Radar's health still reports it.")
        elif i["kind"] == "role":
            note = "Done takes this off Home, and Undo last puts it back. The role stays open and new on Jobs."
        elif i["kind"] == "app":
            note = ("Done takes this off Home, and Undo last puts it back. The step stays on the board "
                    "until you change or clear it there.")
        else:
            note = f"Done takes this off Home, and Undo last puts it back. {i.get('stays', '')}".strip()
    check = ""
    if i["kind"] == "radar" and i["light"]["name"] == "signals" and not done_view:
        pushes = ("The dashboard was started with the phone push on, so a signal at 75 or more is "
                  "sent to the phone." if i.get("push_on") else
                  "The dashboard runs it in dry run, so nothing is sent to the phone.")
        check = ('<div class="ny-check"><button type="button" class="btn btn-secondary" data-check>'
                 f'Check now</button><p>Runs the signal watcher once, for up to ten minutes. {pushes}</p></div>')
    if i["kind"] == "radar":
        body = _body_radar(i, uid)
        context = _context_radar(i)
    elif i["kind"] == "signal":
        body = _body_signal(i, uid)
        context = _context_company(i, model)
    elif i["kind"] == "role":
        body = _body_role(i, uid)
        context = _context_company(i, model)
    else:
        body = _body_app(i, uid, model["now"])
        context = _context_company(i, model)
    more = ""
    if context and i.get("company_id") and i["kind"] != "radar":
        c = model["companies"].get(i["company_id"])
        if c:
            more = (f'<p class="ny-inline-more"><a class="link link-block" href="/company/{int(i["company_id"])}">'
                    f'Everything on file for {esc(c["display_name"])}</a></p>')
    return (f'<article class="ny-detail-item" id="{uid}" data-for="{esc(i["id"])}" aria-labelledby="{uid}-t">'
            '<header class="ny-dh"><div class="ny-dh-grid">'
            f'<h3 class="text-title ny-dh-title" id="{uid}-t" tabindex="-1">{esc(i["title"])}</h3>'
            f'<div class="ny-dh-nav">{prev}{nxt}</div><p class="ny-dh-meta">{meta}</p></div>'
            f'<div class="action-bar ny-acts">{"".join(acts)}</div>{check}'
            f'<p class="ny-act-note">{esc(note)}</p></header>'
            f'<div class="ny-scroll"><div class="ny-item"><div class="pane-body">{body}</div></div>'
            f'{context}</div>{more}</article>')


def empty_html(model: dict) -> str:
    if model["show"] == "done":
        return ('<div class="empty"><p>Nothing is done on Home. Done, key E, takes an item off Home '
                'and lists it here; Put back or Undo last returns it.</p>'
                '<a class="link link-block" href="/">Back to To do</a></div>')
    q = model["jmodel"]["totals"]["question"]
    link = (f'<a class="link link-block" href="/jobs#group-question">Look through the '
            f'{q} One question away {"role" if q == 1 else "roles"} on Jobs</a>' if q else
            '<a class="link link-block" href="/jobs">Open Jobs</a>')
    return ('<div class="empty"><p>Nothing needs you today. An item arrives here when a role is '
            'scored Top prospect or One question away, a signal reaches '
            f'{int(model["data"].get("fast") or 75)}, an application step falls due or a Radar job '
            f'needs a look.</p>{link}</div>')


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
    ids = [i["id"] for i in items]
    details = []
    for n, i in enumerate(items):
        details.append((i["id"], detail_html(i, model, ids[n - 1] if n else None,
                                             ids[n + 1] if n + 1 < len(ids) else None)))
    sel = model["selected"]["id"]
    pane = next(d for k, d in details if k == sel)
    store = "".join(d for k, d in details if k != sel)
    return (f'<section class="ny" id="ny" aria-labelledby="ny-title">{head}<div class="ny-body">'
            f'<section class="pane ny-list" id="ny-list" aria-label="Items">{notice}{list_html(model)}</section>'
            f'<section class="ny-detail" id="ny-pane" aria-label="Selected item">{pane}</section>'
            f'</div><div class="ny-store" id="ny-store" hidden>{store}</div></section>')


def main_html(model: dict) -> str:
    return (f'{welcome_html(model)}<div class="hf-bento">{apps_tile(model)}{funnel_tile(model)}'
            f'{trend_tile(model)}</div>{needs_html(model)}')


KEYMAP = (("J", "Next item"), ("K", "Previous item"),
          ("Enter", "Open the selected item where it lives"),
          ("E", "Done: take it off Home (in Done, put it back)"),
          ("Z", "Undo last"), ("?", "Show this list"))


def keymap_html() -> str:
    return ('<dl class="keymap">' + "".join(
        f'<div><dt><kbd class="kbd">{esc(k)}</kbd></dt><dd>{esc(v)}</dd></div>'
        for k, v in KEYMAP) + '</dl>')


def page_title(model: dict) -> str:
    sel = model["selected"]
    if sel is not None and model["show"] == "todo" and model["selected"] is not model["items"][0]:
        return f"{sel['title']} · Home · MedTech Radar"
    return "Home · MedTech Radar" if model["show"] == "todo" else "Done on Home · MedTech Radar"


def render(conn, data: dict, config: dict, item: str | None = None, show: str = "todo",
           now=None, push_on: bool = False) -> str:
    """The whole page, from one collect() and the reads above."""
    model = build_model(conn, data, config, now, item, show, push_on)
    state = {"show": model["show"], "item": (model["selected"] or {}).get("id")}
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(page_title(model))}</title>
<link rel="icon" href="{bd.FAVICON}">
<style>{page_frame.FRAME_CSS}{HOME_CSS}</style>
</head>
<body>
<div class="app" data-page="home" data-state="{esc(json.dumps(state))}">
{jp.SYMBOLS}{SYMBOLS}
{page_frame.appbar_html("home", model["jobs"], "Home", help_button=True)}
<main class="content hf" id="main">{main_html(model)}</main>
<p class="toast" id="toast" role="status" aria-live="polite"></p>
<dialog class="keys" id="keys" aria-labelledby="keys-title">
<h2 id="keys-title">Keyboard shortcuts</h2>
<p class="keys-note">The keys work anywhere on Home except while you type in a field.</p>
<p class="keys-toggle"><label><input type="checkbox" id="keys-on" checked> Single-key shortcuts on</label></p>
{keymap_html()}
<form method="dialog"><button type="submit" class="btn btn-secondary">Close</button></form>
</dialog>
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
    line, at most KEY_MAX characters, naming one of the four kinds."""
    if not isinstance(value, str):
        raise ValueError("key must be text")
    key = value.strip()
    if not key or len(key) > KEY_MAX or not key.isprintable() or not KEY_RE.match(key):
        raise ValueError("key must name a Home item, like role:..., signal:..., app:... or radar:...")
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
html{background:var(--canvas)}
html body{margin:0;background:var(--canvas);color:var(--text-1);font-family:var(--font-sans);font-size:14px;line-height:20px;font-weight:400;letter-spacing:0;-webkit-font-smoothing:antialiased}
h1,h2,h3,h4,h5,p,ul,ol,dl,dd{margin:0}
ul,ol{list-style:none;padding:0}
button,input{font:inherit;color:inherit;letter-spacing:0}
a{color:inherit}
:root{--ht-surface:#1a1d1f;--ht-line:#2b3135;--ht-track:#252a2d;--ht-sunk:#15181a;--ht-series-1:#2aa1b3;--ht-series-2:#9a80ea;--hf-hero-a:#221e19;--hf-hero-b:#1a1d1f;--hf-hero-line:#3b342b}
.row:focus-visible{outline-offset:-7px;box-shadow:none}
.app{container:app / inline-size;display:grid;grid-template-rows:48px minmax(0,1fr);height:100vh;height:100dvh;overflow:hidden;background:var(--canvas)}
.content{grid-row:2;min-width:0;min-height:0}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:32px;padding:0 12px;border:1px solid transparent;border-radius:var(--radius-2);font-size:14px;line-height:20px;font-weight:500;white-space:nowrap;text-decoration:none;cursor:pointer;transition:background-color var(--dur) ease-out,color var(--dur) ease-out,border-color var(--dur) ease-out}
.btn-primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn-primary .kbd{background:var(--on-accent);color:var(--accent-hover);border-color:var(--on-accent)}
.btn-secondary{background:var(--surface);border-color:var(--border-strong);color:var(--text-1)}
.btn-secondary:hover{background:var(--row-hover)}
.btn-ghost{background:transparent;color:var(--text-2)}
.btn-ghost:hover{color:var(--text-1);text-decoration:underline;text-underline-offset:2px}
.btn.is-disabled,.btn:disabled{color:var(--text-3);border-color:var(--border-subtle);cursor:default;text-decoration:none}
.btn.is-disabled .kbd{color:var(--text-3);border-color:var(--border-subtle)}
.btn[aria-busy="true"]{cursor:progress}
.kbd{display:inline-flex;align-items:center;justify-content:center;flex:none;min-width:18px;height:18px;padding:0 4px;border:1px solid var(--border-strong);border-radius:var(--radius-2);background:var(--surface-sunken);color:var(--text-2);font-family:var(--font-mono);font-size:11px;line-height:16px;font-weight:500;white-space:nowrap}
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
.text-title{font-size:18px;line-height:24px;font-weight:600}
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
.pane{min-width:0;min-height:0;overflow:auto;overscroll-behavior:contain;scroll-padding-top:40px;background:var(--surface)}
.pane-body{display:flex;flex-direction:column;gap:24px;padding:16px 16px 24px}
.pane-note{display:flex;align-items:center;min-height:32px;padding:8px 16px 7px;font-size:12px;line-height:16px;color:var(--text-2);border-bottom:1px solid var(--border-subtle)}
.head-meta{min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:12px;line-height:16px;color:var(--text-3)}
.group-head{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:8px;height:40px;padding:0 16px;background:var(--surface);border-bottom:1px solid var(--border-subtle)}
.group-title{display:flex;align-items:baseline;gap:8px;min-width:0;font-size:16px;line-height:24px;font-weight:600;white-space:nowrap}
.group-count{font-size:12px;line-height:16px;font-weight:400;color:var(--text-3);font-variant-numeric:tabular-nums}
.group-new{color:var(--text-2)}
.row{position:relative;display:grid;grid-template-rows:20px 16px 20px;row-gap:2px;height:76px;padding:8px 16px 7px;border-bottom:1px solid var(--border-subtle);background:var(--surface);color:var(--text-1);text-decoration:none;transition:background-color var(--dur) ease-out}
.row:hover{background:var(--row-hover)}
.row[aria-current="true"]{background:var(--row-selected);border-left:3px solid var(--accent);padding-left:13px}
.row-2{grid-template-rows:20px 16px;row-gap:4px;height:56px}
.row.is-static:hover{background:var(--surface)}
.r1,.r2,.r3{display:flex;min-width:0}
.r1{align-items:baseline;gap:8px}
.r-title{flex:1 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-weight:500}
.is-unread .r-title{font-weight:600}
.r-cv{flex:none;width:40px;text-align:right;font-weight:500;font-variant-numeric:tabular-nums}
.r-seen{flex:none;min-width:44px;text-align:right;font-size:12px;line-height:16px;color:var(--text-3);font-variant-numeric:tabular-nums;white-space:nowrap}
.r2{align-items:center;gap:8px}
.r-meta{flex:0 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:12px;line-height:16px;color:var(--text-3)}
.r2 .tag{margin:-2px 0}
.r3{align-items:center;gap:12px}
.r-lead{flex:1 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:var(--text-2)}
.r-marks{display:inline-flex;align-items:center;gap:4px;flex:none}
.mk-pass{color:var(--text-3)}.mk-fail{color:var(--negative)}.mk-nj{color:var(--neutral)}
.sec-h{margin-bottom:8px;font-size:16px;line-height:24px;font-weight:600}
.sec-h .group-count{margin-left:4px}
.facts{display:grid;grid-template-columns:160px minmax(0,1fr);gap:8px 16px;align-items:baseline}
.facts dt{font-size:12px;line-height:16px;font-weight:600;color:var(--text-2)}
.facts dd{max-width:80ch;overflow-wrap:anywhere}
.table-wrap{min-width:0;overflow-x:auto;overscroll-behavior-x:contain;background:var(--surface)}
.table{width:100%;border-collapse:separate;border-spacing:0;background:var(--surface)}
.table th,.table td{height:40px;padding:0 12px;text-align:left;vertical-align:middle;border-bottom:1px solid var(--border-subtle)}
.table th:first-child,.table td:first-child{padding-left:0;white-space:nowrap;width:136px}
.table th:nth-child(2),.table td:nth-child(2){white-space:nowrap;width:96px}
.table thead th{background:var(--surface-sunken);border-bottom-color:var(--border-strong);font-size:12px;line-height:16px;font-weight:600;color:var(--text-3)}
.table thead th:first-child{padding-left:8px}
.table td:first-child{padding-left:8px}
.table td{padding-top:10px;padding-bottom:10px;height:auto}
.empty{padding:12px 16px 24px}
.empty > p{max-width:72ch}
.empty > .link-block{margin-top:12px}
.keymap{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 16px;align-items:center;margin-top:16px}
.keymap div{display:contents}
.keymap dt{display:flex;align-items:center;justify-content:flex-end;gap:4px}
.keymap dd{color:var(--text-2)}
.toast{position:fixed;left:50%;bottom:16px;z-index:30;transform:translateX(-50%);display:flex;align-items:center;gap:12px;max-width:min(560px,calc(100vw - 32px));padding:8px 8px 8px 16px;background:var(--surface-sunken);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay)}
.toast:empty{display:none}
.toast.is-bad{border-color:var(--negative)}
.toast.is-bad::before{content:"!";display:inline-flex;align-items:center;justify-content:center;flex:none;width:16px;height:16px;border-radius:50%;background:var(--negative);color:var(--on-accent);font-weight:700;font-size:12px}
.keys{max-width:min(480px,calc(100vw - 32px));padding:24px;background:var(--surface);color:var(--text-1);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay)}
.keys::backdrop{background:rgba(0,0,0,.6)}
.keys h2{font-size:18px;line-height:24px;font-weight:600}
.keys-note{margin-top:4px;color:var(--text-2)}
.keys-toggle{margin-top:12px}
.keys-toggle label{display:inline-flex;align-items:center;gap:8px;min-height:32px;cursor:pointer}
.keys-toggle input{width:18px;height:18px;accent-color:var(--accent)}
.keys form{margin-top:16px}
/* ---- the tiles (home2_tiles) ---- */
.ht{container:ht / inline-size;min-width:0;background:var(--ht-surface);border:1px solid var(--ht-line);border-radius:var(--radius-4);color:var(--text-1);--focus-gap:var(--ht-surface)}
.ht-in{display:flex;flex-direction:column;gap:16px;height:100%;padding:16px}
.ht-head{display:flex;flex-direction:column;gap:2px}
.ht-title{font-size:16px;line-height:24px;font-weight:600;color:var(--text-1)}
.ht-q{font-size:14px;line-height:20px;color:var(--text-2);text-wrap:pretty}
.ht-body{display:flex;flex-direction:column;gap:12px;min-width:0}
.ht-say{font-size:14px;line-height:20px;color:var(--text-1);text-wrap:pretty;max-width:64ch}
.ht-note{font-size:12px;line-height:16px;color:var(--text-3);text-wrap:pretty;max-width:72ch}
.ht-foot{padding-top:4px;margin-top:auto}
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
.apm-name small{display:block;font-size:12px;color:var(--text-3)}
.apm-radar{display:none;font-size:12px;line-height:16px;color:var(--text-3)}
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
  .apm-name small{display:none}
  .apm-pips{grid-row:3;justify-content:flex-start}
  .apm-radar{display:block}
  .apm-wait{flex-direction:row;flex-wrap:wrap;gap:4px 24px}
}
/* ---- the welcome ---- */
.ht-welcome{container:htw / inline-size;min-width:0;color:var(--text-1)}
.ht-welcome-in{display:flex;flex-direction:column;gap:4px;padding:20px 16px}
.ht-date{font-size:14px;line-height:20px;color:var(--text-2)}
.ht-greet{font-size:24px;line-height:32px;font-weight:600;color:var(--text-1);text-wrap:balance}
.ht-status{max-width:80ch;margin-top:4px;font-size:16px;line-height:24px;color:var(--text-1);text-wrap:pretty}
.ht-quiet{max-width:80ch;font-size:14px;line-height:20px;color:var(--text-2);text-wrap:pretty}
.ht-asof{display:flex;flex-wrap:wrap;align-items:center;gap:4px 16px;margin-top:4px;font-size:12px;line-height:16px;color:var(--text-3)}
.ht-settings{display:inline-flex;align-items:center;min-height:24px;font-size:12px}
.ht-figs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:16px}
.ht-figlink{display:flex;flex-direction:column;gap:2px;min-height:24px;padding:12px 14px;background:var(--ht-surface);border:1px solid var(--ht-line);border-radius:var(--radius-4);color:var(--text-1);text-decoration:none;--focus-gap:var(--canvas)}
.ht-figlink:hover{border-color:var(--border-strong)}
.ht-figlink:hover .ht-figword{text-decoration:underline;text-underline-offset:2px}
.ht-fignum{font-size:32px;line-height:40px;font-weight:600;font-variant-numeric:tabular-nums}
.ht-figword{font-size:14px;line-height:20px;color:var(--text-2)}
.ht-figlink.is-zero .ht-fignum{color:var(--text-3)}
.ht-strip{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px;margin-top:12px;padding-top:12px;border-top:1px solid var(--ht-line)}
.ht-strip-list{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px}
.ht-strip-job{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:20px}
.ht-light{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:20px;font-weight:600;white-space:nowrap}
.ht-lamp{display:inline-flex;flex:none;align-items:center;justify-content:center;width:20px;height:20px;border:1.5px solid currentColor;border-radius:50%}
.ht-job-name{font-size:12px;line-height:16px;font-weight:500;color:var(--text-1)}
@container htw (min-width:720px){.ht-welcome-in{padding:24px 24px}.ht-greet{font-size:28px;line-height:36px}.ht-figs{grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}
/* ---- Needs you today ---- */
.ny{container:ny / inline-size;display:grid;grid-template-rows:auto minmax(0,1fr);height:100%;min-width:0;min-height:0;background:var(--surface);border:1px solid var(--border-subtle);border-radius:var(--radius-4);overflow:hidden}
.ny-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;min-height:56px;padding:12px 16px;border-bottom:1px solid var(--border-subtle)}
.ny-title{font-size:16px;line-height:24px;font-weight:600;color:var(--text-1)}
.ny-count{margin-left:8px;font-size:14px;font-weight:400;color:var(--text-3)}
.ny-tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-left:auto}
.ny-body{display:grid;grid-template-columns:minmax(0,1fr);min-width:0;min-height:0}
.ny-list{border-bottom:1px solid var(--border-subtle)}
.ny-detail{min-width:0;min-height:0;background:var(--surface)}
.ny-detail-item{display:block;min-width:0}
.ny-dh{container:nydh / inline-size;padding:12px 16px;background:var(--surface);border-bottom:1px solid var(--border-subtle)}
.ny-dh-grid{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"title nav" "meta meta";column-gap:16px}
.ny-dh-title{grid-area:title;align-self:center;text-wrap:balance}
.ny-dh-title:focus{outline:none}
.ny-dh-nav{grid-area:nav;display:flex;align-items:center;gap:8px}
.ny-dh-meta{grid-area:meta;display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;min-height:20px;margin-top:4px;font-size:12px;line-height:16px;color:var(--text-3)}
@container nydh (max-width:599.98px){.ny-dh-grid{grid-template-columns:minmax(0,1fr);grid-template-areas:"title" "meta" "nav"}.ny-dh-nav{margin-top:8px}}
.ny-acts{margin-top:12px}
.ny-check{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-top:8px}
.ny-check > p{flex:1 1 32ch;min-width:0;max-width:80ch;font-size:12px;line-height:16px;color:var(--text-2);text-wrap:pretty}
.ny-act-note{max-width:96ch;margin-top:8px;font-size:12px;line-height:16px;color:var(--text-2);text-wrap:pretty}
.ny-para{max-width:80ch;text-wrap:pretty}
.ny-para + .ny-para{margin-top:8px}
.ny-fig{display:flex;align-items:baseline;gap:8px;margin-top:12px}
.ny-basis{max-width:80ch;margin-top:2px;text-wrap:pretty}
.ny-under{max-width:80ch;margin-top:8px;text-wrap:pretty}
.ny-context{border-top:1px solid var(--border-subtle)}
.ny-context .group-head{position:static}
.ny-runs .sec-h{margin-bottom:8px}
.ny-inline-more{display:none;padding:0 16px 16px}
.ny-gone{color:var(--caution)}
/* A phone, or a block under 720px: the selected item's detail opens under its row. */
.ny-detail-item.is-inline{border-bottom:1px solid var(--border-subtle);background:var(--surface)}
.ny-detail-item.is-inline .ny-dh{border-bottom:0;padding-bottom:0}
.ny-detail-item.is-inline .ny-dh-grid{display:none}
.ny-detail-item.is-inline .ny-acts{margin-top:0}
.ny-detail-item.is-inline .ny-context{display:none}
.ny-detail-item.is-inline .ny-inline-more{display:block}
@container ny (max-width:599.98px){.ny-acts > .btn-primary{flex:1 1 100%}.ny-tools{margin-left:0}}
@container ny (min-width:720px){
  .ny-body{grid-template-columns:minmax(320px,40%) minmax(0,1fr)}
  .ny-list{border-right:1px solid var(--border-subtle);border-bottom:0}
  .ny-detail{overflow:hidden}
  .ny-detail-item{display:grid;grid-template-rows:auto minmax(0,1fr);height:100%;min-height:0}
  .ny-scroll{min-width:0;min-height:0;overflow:auto;overscroll-behavior:contain}
}
@container ny (min-width:1100px){.ny-body{grid-template-columns:440px minmax(0,1fr)}}
@container ny (min-width:1500px){
  .ny-body{grid-template-columns:clamp(480px,30cqw,680px) minmax(0,1fr)}
  .ny-detail-item{grid-template-columns:minmax(0,1fr) clamp(440px,28cqw,640px);grid-template-rows:auto minmax(0,1fr)}
  .ny-dh{grid-column:1;grid-row:1}
  .ny-scroll{display:contents}
  .ny-item{grid-column:1;grid-row:2;min-width:0;min-height:0;overflow:auto;overscroll-behavior:contain}
  .ny-context{grid-column:2;grid-row:1 / span 2;min-width:0;min-height:0;overflow:auto;overscroll-behavior:contain;border-top:0;border-left:1px solid var(--border-subtle)}
}
.ny-is-empty .ny-body{grid-template-columns:minmax(0,1fr)}
/* ---- Home final: the hero, the bento, the list block ---- */
.hf{display:grid;grid-template-columns:minmax(0,1fr);grid-template-rows:auto auto auto;gap:12px;align-content:start;padding:12px 0 24px;background:var(--canvas)}
.hf > .ht-welcome,.hf > .hf-bento{margin:0 12px}
.hf > .ny{border-radius:0;border-left:0;border-right:0}
.hf-hero{background:linear-gradient(135deg,var(--hf-hero-a),var(--hf-hero-b) 70%);border:1px solid var(--hf-hero-line);border-radius:var(--radius-4);--focus-gap:var(--hf-hero-a)}
.hf-hero .ht-welcome-in{padding:20px 16px 16px}
.hf-hero .ht-greet{font-size:28px;line-height:36px}
.hf-hero .ht-strip{border-top-color:var(--hf-hero-line)}
.hf-bento{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;align-items:stretch}
@container htw (max-width:719.98px){.ht-figs-4 > .ht-figlink:last-child{grid-column:auto}}
@container htw (min-width:720px){.hf-hero .ht-welcome-in{padding:24px 24px 16px}.hf-hero .ht-figs{margin-top:12px}.hf-hero .ht-strip{margin-top:16px}}
@container htw (min-width:720px) and (max-width:1599.98px){.hf-hero .ht-status{max-width:none}.hf-hero .ht-figlink{flex-direction:row;align-items:baseline;gap:10px;padding:10px 16px}}
@container htw (min-width:960px) and (max-width:1599.98px){
  .hf-hero .ht-welcome-in{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"date asof" "greet greet" "status status" "quiet quiet" "figs figs" "strip strip";row-gap:4px;padding-top:20px}
  .hf-hero .ht-date{grid-area:date;align-self:center}
  .hf-hero .ht-greet{grid-area:greet}
  .hf-hero .ht-status{grid-area:status}
  .hf-hero .ht-quiet{grid-area:quiet}
  .hf-hero .ht-asof{grid-area:asof;align-self:center;margin-top:0}
  .hf-hero .ht-figs{grid-area:figs}
  .hf-hero .ht-figs.ht-figs-4{grid-template-columns:repeat(4,minmax(max-content,1fr))}
  .hf-hero .ht-strip{grid-area:strip}
}
@container htw (min-width:1600px){
  .hf-hero .ht-welcome-in{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);grid-template-areas:"date figs" "greet figs" "status figs" "quiet figs" "asof figs" "strip strip";column-gap:64px;row-gap:6px;padding:40px 40px 20px}
  .hf-hero .ht-date{grid-area:date;font-size:16px;line-height:24px}
  .hf-hero .ht-greet{grid-area:greet;font-size:40px;line-height:52px}
  .hf-hero .ht-status{grid-area:status;max-width:64ch;font-size:20px;line-height:30px}
  .hf-hero .ht-quiet{grid-area:quiet;max-width:64ch;font-size:16px;line-height:24px}
  .hf-hero .ht-asof{grid-area:asof}
  .hf-hero .ht-figs{grid-area:figs;align-self:center;margin-top:0;gap:16px}
  .hf-hero .ht-figs.ht-figs-4{grid-template-columns:repeat(4,minmax(max-content,1fr))}
  .hf-hero .ht-figlink{padding:20px 20px}
  .hf-hero .ht-fignum{font-size:48px;line-height:56px}
  .hf-hero .ht-figword{font-size:16px;line-height:24px}
  .hf-hero .ht-strip{grid-area:strip;margin-top:24px;padding-top:16px}
}
@container app (min-width:768px){
  .hf{gap:16px;padding:16px}
  .hf > .ht-welcome,.hf > .hf-bento{margin:0}
  .hf > .ny{border-radius:var(--radius-4);border-left:1px solid var(--border-subtle);border-right:1px solid var(--border-subtle)}
  .hf-bento{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
  .hf-bento > .ht-apps-tile{grid-column:1 / -1}
}
@container app (min-width:1024px){.hf-bento{grid-template-columns:minmax(0,2fr) minmax(0,1fr) minmax(0,1fr)}.hf-bento > .ht-apps-tile{grid-column:auto}}
/* From 768: the page scrolls as one, and the list block is one screen high when it reaches the top, its list and detail scrolling inside it. */
@container app (min-width:768px){
  .hf{grid-template-rows:auto auto 100%;min-height:0;overflow:auto;overscroll-behavior:contain;scroll-snap-type:y proximity;scroll-padding-top:16px}
  .hf > .ny{scroll-snap-align:start}
}
/* From 1800px wide and 1300px tall everything fits, and the list block takes the rest of the
   height. A wide screen that is not that tall (1920 by 1080) scrolls as above. */
@media (min-height:1300px){
  @container app (min-width:1800px){.hf{grid-template-rows:auto auto minmax(0,1fr);min-height:0;overflow:hidden;scroll-snap-type:none}.hf > .ny{scroll-snap-align:none}}
}
@container app (min-width:768px){
  .ny-list{overflow:auto}
}
.hf:has(> .ny-is-empty){grid-template-rows:auto auto auto;scroll-snap-type:none}
.hf > .ny.ny-is-empty{height:auto}
/* Under 1024 no key hints; under 768 the page scrolls as a document and every target is 44px. */
@media (max-width:1023.98px){.kbd{display:none}}
@media (max-width:767.98px){
  html{scroll-padding-top:44px}
  .app{grid-template-rows:48px auto;height:auto;min-height:100vh;overflow:visible;align-content:start}
  .hf{overflow:visible}
  .ny{height:auto}
  .pane,.ny-scroll{overflow:visible;min-height:auto}
  .btn,.seg{height:44px}
  .group-head{height:44px}
  .facts{grid-template-columns:112px minmax(0,1fr)}
  .undo-last{padding-inline:8px}
  .ht-step,.ht .link-block,.ht-figlink,.apm-link,.ht-settings,.link-block{min-height:44px}
  .apm-wait li .link{display:inline-flex;align-items:center;min-height:44px}
  .table th:first-child,.table td:first-child{width:112px}
  .table th:nth-child(2),.table td:nth-child(2){width:80px}
}
@media (pointer:coarse){
  .btn,.seg{height:44px}
  .group-head{height:44px}
  .ht-step,.ht .link-block,.ht-figlink,.apm-link,.ht-settings,.link-block{min-height:44px}
  .apm-wait li .link{display:inline-flex;align-items:center;min-height:44px}
}
@media (max-height:499px){.group-head{position:static}}
@media (forced-colors:active){.row[aria-current="true"]{border-left-color:Highlight}.tag,.kbd,.ht-lamp{border:1px solid CanvasText}}
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
  var keys = document.getElementById('keys');
  var DONE = { done: 'Done', back: 'Put back' };

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
  // J, K or click on the page.
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

  // ---- the selection
  function rows() { return Array.prototype.slice.call(main.querySelectorAll('.ny-list .row[data-item]')); }
  function detailFor(id) {
    var found = null;
    main.querySelectorAll('.ny-detail-item').forEach(function (d) { if (d.dataset.for === id) found = d; });
    return found;
  }
  function inline() {
    var ny = el('ny');
    return !!ny && ny.getBoundingClientRect().width < 720;
  }
  // The selected item's detail sits in the pane beside the list, or under
  // its row when the block is narrower than 720px (a phone).
  function place() {
    var row = state.item && el('row-' + state.item);
    var d = state.item && detailFor(state.item);
    var pane = el('ny-pane'), store = el('ny-store');
    if (!d || !pane || !store) return;
    main.querySelectorAll('.ny-detail-item').forEach(function (x) {
      if (x !== d && x.parentNode !== store) { x.classList.remove('is-inline'); store.appendChild(x); }
    });
    if (inline() && row) {
      d.classList.add('is-inline');
      if (row.nextSibling !== d) row.parentNode.insertBefore(d, row.nextSibling);
    } else {
      d.classList.remove('is-inline');
      if (d.parentNode !== pane) pane.appendChild(d);
    }
  }
  function select(id, focus) {
    var row = el('row-' + id);
    if (!row) return;
    state.item = id;
    rows().forEach(function (r) {
      if (r === row) r.setAttribute('aria-current', 'true'); else r.removeAttribute('aria-current');
    });
    place();
    try { history.replaceState(null, '', '/' + query(id)); } catch (e) {}
    var name = row.querySelector('.r-title');
    if (name) document.title = name.textContent + ' \u00b7 ' + (state.show === 'done' ? 'Done on Home' : 'Home')
      + ' \u00b7 MedTech Radar';
    var d = detailFor(id);
    if (d) {
      var scroller = d.querySelector('.ny-scroll');
      if (scroller) scroller.scrollTop = 0;
    }
    if (focus) row.focus({ preventScroll: true });
    row.scrollIntoView({ block: 'nearest' });
  }
  // Where the page scrolls as one (768 to 1799, or a screen under 1300 tall),
  // the first J or K brings Needs you today to the top before it moves, so
  // the row and its detail are in view. Not on a phone, where the page is a
  // document and the row is scrolled to as it is.
  function bringUp() {
    var ny = el('ny');
    if (!ny || inline() || !(main.scrollHeight > main.clientHeight + 1)) return;
    var gap = ny.getBoundingClientRect().top - main.getBoundingClientRect().top;
    if (gap > 1) ny.scrollIntoView({ block: 'start' });
  }
  function step(by) {
    hush();
    var r = rows();
    if (!r.length) return;
    bringUp();
    var i = r.findIndex(function (x) { return x.dataset.item === state.item; });
    var n = Math.min(r.length - 1, Math.max(0, (i < 0 ? 0 : i + by)));
    select(r[n].dataset.item, true);
  }
  function openSelected() {
    var d = state.item && detailFor(state.item);
    var a = d && d.querySelector('[data-open]');
    if (a) a.click();
  }

  // ---- redrawing after a write: the main element, from the server
  function refresh(item, focusRow) {
    return fetch('/' + (query(item) || '?') + (query(item) ? '&' : '') + 'part=main',
                 { headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) {
        main.innerHTML = j.html;
        document.title = j.title;
        state.item = j.item;
        setStack(stack());
        if (j.item) {
          select(j.item, false);
          if (focusRow) { var row = el('row-' + j.item); if (row) row.focus({ preventScroll: true }); }
        } else {
          try { history.replaceState(null, '', '/' + query(null)); } catch (e) {}
          var h = el('ny-title');
          if (h) { h.tabIndex = -1; h.focus(); }
        }
      }).catch(function () { location.reload(); });
  }
  function neighbour(id) {
    var r = rows();
    var i = r.findIndex(function (x) { return x.dataset.item === id; });
    if (i < 0) return null;
    var other = r[i + 1] || r[i - 1];
    return other ? other.dataset.item : null;
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
        say((kind === 'done' ? 'Done: “' + title + '” is off Home. It stays where it lives.'
             : 'Put back on Home: “' + title + '”.'), false, true);
        return refresh(neighbour(id), true).then(function () { return 'ok'; });
      }
      say('Undone: “' + title + '” is ' + (kind === 'done' ? 'back on Home.' : 'off Home again.'));
      var back = (kind === 'done') === (state.show !== 'done');
      return refresh(back ? id : state.item, true).then(function () { return 'ok'; });
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
    btn.setAttribute('aria-busy', 'true');
    btn.textContent = 'Checking';
    say('The signal watcher is running. This can take up to ten minutes.');
    fetch('/watch', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j || j.ok === false) { say((j && j.note) || 'The run did not finish.', true); }
        else { say('The signal watcher ran.'); }
        refresh(state.item, false);
      }, function () { say('No usable answer came back. Reload to check.', true); refresh(state.item, false); });
  }

  // ---- clicks
  document.addEventListener('click', function (e) {
    if (toast.textContent && !(e.target.closest && e.target.closest('#toast'))) hush();
  }, true);
  main.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var row = e.target.closest('.ny-list .row[data-item]');
    if (row) {
      e.preventDefault();
      if (row.dataset.item === state.item && inline()) return;
      select(row.dataset.item, false);
      return;
    }
    var nav = e.target.closest('[data-step]');
    if (nav) { e.preventDefault(); step(Number(nav.dataset.step)); return; }
    var done = e.target.closest('[data-done]');
    if (done) { act('done', done.dataset.done, done.dataset.title, state.item, false); return; }
    var back = e.target.closest('[data-back]');
    if (back) { act('back', back.dataset.back, back.dataset.title, state.item, false); return; }
    var check = e.target.closest('[data-check]');
    if (check) { checkNow(check); return; }
    if (e.target.closest('#undo-last')) { undoLast(); }
  });
  var narrowWatch = window.matchMedia('(max-width: 1023.98px)');
  window.addEventListener('resize', function () { place(); });
  if (narrowWatch.addEventListener) narrowWatch.addEventListener('change', place);

  // ---- keys: J, K, Enter, E, Z and ?, never while typing, and off with the switch (WCAG 2.1.4)
  function typing(t) {
    return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  }
  function keysOn() {
    try { return localStorage.getItem('radar-home-keys') !== 'off'; } catch (e) { return true; }
  }
  var keysToggle = el('keys-on');
  if (keysToggle) {
    keysToggle.checked = keysOn();
    keysToggle.addEventListener('change', function () {
      try { localStorage.setItem('radar-home-keys', keysToggle.checked ? 'on' : 'off'); } catch (e) {}
    });
  }
  function openKeys() {
    if (keys.open) return;
    if (keys.showModal) keys.showModal(); else keys.setAttribute('open', '');
  }
  var keysBtn = el('keys-open');
  if (keysBtn) keysBtn.addEventListener('click', openKeys);
  document.addEventListener('keydown', function (e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
    if (typing(e.target) || keys.open) return;
    var onSelected = e.target && e.target.classList && e.target.classList.contains('row')
      && e.target.dataset.item === state.item;
    if (e.key === 'Enter' && onSelected) { e.preventDefault(); openSelected(); return; }
    if (!keysOn()) return;
    if (e.key === '?') { e.preventDefault(); openKeys(); return; }
    if (e.shiftKey) return;
    var k = (e.key || '').toLowerCase();
    if (k === 'j' || k === 'k') { e.preventDefault(); step(k === 'j' ? 1 : -1); return; }
    if (e.key === 'Enter' && (e.target === document.body || e.target === main)) {
      e.preventDefault(); openSelected(); return;
    }
    if (k === 'e') {
      var d = state.item && detailFor(state.item);
      var b = d && d.querySelector('[data-done],[data-back]');
      if (b) { e.preventDefault(); b.click(); }
      return;
    }
    if (k === 'z') { e.preventDefault(); undoLast(); }
  });

  // ---- on load
  setStack(stack());
  if (state.item) {
    place();
    var first = el('row-' + state.item);
    if (first && location.search.indexOf('item=') >= 0) first.focus({ preventScroll: true });
  }
})();
"""
