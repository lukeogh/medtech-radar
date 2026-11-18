"""The Watchlist's data: the companies the owner watches, Radar's suggestions,
the threads, the contacts, the people added by hand and Read again.

Built from the Watchlist final (design/round-1 branch, design/watchlist/final,
15 Nov 2026) and the decisions of that day in design/README.md. The page
itself is watchlist_page.py; Home reads threads_due from here, so the figure
on Home and the tags on the Watchlist come from one rule and cannot disagree.

The rules, in one place:

- A company is on the Watchlist when companies.watched_at is set. It joins only
  when the owner adds it: from Insights (Add to Watchlist), from a suggestion
  (Add to Watchlist on the card), by name, or by logging a contact with it.
  Removing it clears watched_at and keeps everything else, its thread among it,
  which stops counting; adding it again restores it.
- A thread is a company's newest contact (touches, newest touched_at, then
  newest id; the rows Set state writes are not contacts). It is open when that
  contact set a next step. A next step needs a due date when it is logged here;
  one already on file with no date shows "No date set" and never counts as
  due. A thread is due when its date is today or earlier, UK time, and only a
  Watchlist company's thread counts.
- Suggestions are worked out in code after each signal watcher run
  (refresh_suggestions), never by a model: a company Radar has seen and the
  owner does not watch, with a buying window, a funding round at 40 or more
  or an Insights story at 70 or more, found in the last 30 days. At most five wait, strongest
  first (a buying window, then a funding round, then a story, then by
  relevance and newest). One unanswered for 14 days is folded into Dismissed
  quietly. A dismissed company comes back once, marked, and only on a
  stronger kind of signal than the one it was dismissed on. Nothing joins
  until the owner presses Add.
- Read again re-reads one company's facts with one polite fetch of its own
  site and one model call (enrich_company with force), on a button press
  only, with the cost stated first.

Everything here takes a connection and leaves committing to the caller,
apart from the write helpers named as committing.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import radar_common as rc

SUGGEST_BAR = 70          # an Insights story suggests a company from here
FUNDING_BAR = 40          # a funding round suggests a company from here (decided 16 Nov)
WAITING_MAX = 5           # suggestions waiting at once
FOLD_DAYS = 14            # an unanswered suggestion folds into Dismissed
DISMISSED_DAYS = 90       # the Dismissed fold keeps this long
SIGNAL_DAYS = 30          # a suggestion rests on a signal found this recently
NOTE_MAX = 500
STEP_MAX = 200
NAME_MAX = 120
PERSON_MAX = 80

KIND_RANK = {"story": 1, "funding": 2, "buying-window": 3}
KIND_WORDS = {"buying-window": "Buying window", "funding": "Funding round", "story": "Story"}
VIA_WORDS = {"insights": "from Insights", "suggestion": "from a suggestion",
             "name": "by name", "contact": "when you logged a contact"}
TYPE_WORDS = {"buying-window": "Buying window", "first-hire": "First hire", "funding": "Funding round",
              "spin-off": "Spin-off launch", "accelerator": "Accelerator entry",
              "regulation": "Regulation", "market": "Market news", "other": "Other news"}
CODE_TYPES = {"job-advert": "buying-window", "qa-tripwire": "first-hire"}
CHANNELS = (("comment", "Comment"), ("connection-note", "Connection note"),
            ("engagement", "Engagement"), ("artefact", "Article sent"), ("other", "Other"))
CHANNEL_WORDS = dict(CHANNELS)
OUTCOMES = ("none", "reply", "conversation")
OUTCOME_WORDS = {"none": "None", "reply": "Reply", "conversation": "Conversation"}
# The state labels in plain words (notes frame, question 11).
STATE_WORDS = {"new": "Not contacted", "seen": "Not contacted", "touched": "Contacted",
               "window open": "Buying window open", "in conversation": "In conversation",
               "client": "Working with them", "dead": "Not pursuing"}
SET_STATES = (("in-conversation", "In conversation"), ("client", "Working with them"),
              ("dead", "Not pursuing"))
TIER_WORDS = {"top": "Top prospect", "question": "One question away", "reading": "Reading",
              "filtered": "Filtered"}
FACT_FIELDS = (("what_they_build", "Builds"), ("software_content", "Software"),
               ("stage", "Stage"), ("where", "Where"))
# Endings a name is looked up without, so "Hartwell Infusion" finds
# "Hartwell Infusion Ltd". For the lookup only: the companies table's natural
# key stays radar_common.normalise_company, which strips nothing.
_ENDINGS = ("ltd", "limited", "plc", "inc", "llc", "gmbh", "bv", "b.v.", "nv", "n.v.", "sa",
            "s.a.", "ag", "srl", "oy", "ab", "co", "corp", "company", "group", "holdings")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ================================================================ time

def parse(value) -> datetime | None:
    text = str(value or "").strip()
    for fmt, size in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16),
                      ("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def moment_of(now=None) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return parse(now) or datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uk(moment: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/London"))
    except Exception:  # noqa: BLE001  no time zone database: UTC, right all winter
        return moment.astimezone(timezone.utc)


def uk_day(value) -> date | None:
    """The UK calendar day of a stored moment; a bare date is that date."""
    text = str(value or "").strip()
    if _DATE.match(text[:10]) and len(text) <= 10:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    m = parse(text)
    return uk(m).date() if m else None


def day_words(value, weekday: bool = False) -> str:
    """13 Nov, or Fri 13 Nov."""
    d = uk_day(value)
    if d is None:
        return ""
    head = f"{d:%a} " if weekday else ""
    return f"{head}{d.day} {MONTHS[d.month - 1]}"


def hm(value) -> str:
    m = parse(value)
    return f"{uk(m):%H:%M}" if m else ""


# ================================================================ companies

def loose_name(name) -> str:
    """A name for looking up, ignoring capitals, spacing, punctuation and
    endings such as Ltd. Never a key: see _ENDINGS."""
    words = re.sub(r"[^\w\s.]", " ", str(name or "").lower()).split()
    while words and words[-1].strip(".") in {e.strip(".") for e in _ENDINGS} and len(words) > 1:
        words.pop()
    return " ".join(w.strip(".") for w in words if w.strip("."))


def place_of(c: dict) -> str:
    return ", ".join(x for x in (c.get("city"), c.get("country")) if x)


def _companies(conn) -> dict:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM companies")}


def _norm_index(companies: dict) -> dict:
    return {c["norm_name"]: cid for cid, c in companies.items()}


def company_row(conn, company_id) -> dict | None:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


# ================================================================ contacts and threads

def _contacts_by_company(conn, companies: dict | None = None) -> dict:
    """Every contact, by company id, newest first. A touch stored without a
    company id (the signal Done booking wrote them so before) is matched on
    the normalised name, the companies table's own key."""
    companies = companies if companies is not None else _companies(conn)
    norm = _norm_index(companies)
    out: dict = {}
    for r in conn.execute(
            "SELECT * FROM touches WHERE kind IS NULL OR kind <> 'state'"
            " ORDER BY touched_at DESC, id DESC"):
        t = dict(r)
        cid = t.get("company_id") or norm.get(rc.normalise_company(t.get("company")))
        if cid:
            out.setdefault(cid, []).append(t)
    return out


def contacts_for(conn, company_id) -> list:
    c = company_row(conn, company_id)
    if not c:
        return []
    rows = conn.execute(
        "SELECT * FROM touches WHERE (company_id = ? OR company_id IS NULL)"
        " AND (kind IS NULL OR kind <> 'state') ORDER BY touched_at DESC, id DESC",
        (company_id,)).fetchall()
    return [dict(r) for r in rows
            if r["company_id"] == company_id
            or (r["company_id"] is None and rc.normalise_company(r["company"]) == c["norm_name"])]


def thread_of(contacts: list, today: date) -> dict:
    """The thread from a company's contacts, newest first.

    state is none (no contact yet), closed (the newest contact set no next
    step), nodate (a next step with no date, from before a date was needed),
    overdue, today or upcoming. Only overdue and today are due."""
    if not contacts:
        return {"state": "none", "due": False, "last": None}
    last = contacts[0]
    step = (last.get("next_action") or "").strip()
    out = {"last": last, "next_step": step, "set_at": last.get("touched_at"),
           "due_date": None, "days": 0, "due": False}
    if not step:
        out["state"] = "closed"
        return out
    when = uk_day(last.get("next_action_date")) if last.get("next_action_date") else None
    if when is None:
        out["state"] = "nodate"
        return out
    out["due_date"] = when.isoformat()
    delta = (when - today).days
    out["days"] = delta
    if delta < 0:
        out["state"] = "overdue"
    elif delta == 0:
        out["state"] = "today"
    else:
        out["state"] = "upcoming"
    out["due"] = delta <= 0
    return out


def threads_due(conn, now=None) -> list:
    """The Watchlist companies whose thread is due today or overdue, most
    overdue first. Home's fifth figure and its Threads due group read this,
    and the Watchlist's tags read the same rule, thread_of."""
    today = uk(moment_of(now)).date()
    companies = _companies(conn)
    contacts = _contacts_by_company(conn, companies)
    out = []
    for cid, c in companies.items():
        if not c.get("watched_at"):
            continue
        th = thread_of(contacts.get(cid) or [], today)
        if th["due"]:
            out.append({"company_id": cid, "name": c["display_name"], "place": place_of(c),
                        "company": c, "thread": th})
    out.sort(key=lambda t: (t["thread"]["due_date"] or "", t["name"].lower()))
    return out


def follow_ups_due_count(conn, now=None) -> int:
    """How many Watchlist companies have a follow-up due today or overdue: the
    one count the Watchlist's pill and Home's tile both show (owner, 17 Nov)."""
    return len(threads_due(conn, now))


def clean_date(value) -> str | None:
    text = str(value or "").strip()
    if not _DATE.match(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def clean_next(step, due, contact_day: date | None = None) -> tuple:
    """(step, due, refusal). A next step needs a due date; a due date needs a
    next step; leaving both empty closes the thread. refusal is (field, note)."""
    text = rc.sanitise_free_text(" ".join(str(step or "").split()))[:STEP_MAX]
    raw_due = str(due or "").strip()
    when = clean_date(raw_due)
    if raw_due and when is None:
        return None, None, ("due", "Give the due date as a date, for example 20 Nov 2026.")
    if text and not when:
        return None, None, ("due", "Give the next step a due date, or leave the next step empty "
                                   "to close the thread.")
    if when and not text:
        return None, None, ("next_step", "Give a next step for that date, or clear the date.")
    if when and contact_day and date.fromisoformat(when) < contact_day:
        return None, None, ("due", "The due date is before the contact. Choose that day or later.")
    return text or None, when, None


# ================================================================ watching

def is_watched(conn, company_id) -> bool:
    row = conn.execute("SELECT watched_at FROM companies WHERE id = ?", (company_id,)).fetchone()
    return bool(row and row["watched_at"])


def watch(conn, company_id, via: str, signal_id=None, now=None) -> dict:
    """Put a company on the Watchlist. A waiting suggestion for it is answered
    by this, so it leaves the suggestions. Does not commit."""
    stamp = iso(moment_of(now))
    row = conn.execute("SELECT id, watched_at FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        return {"ok": False, "note": "That company is no longer on file. Reload the page."}
    if row["watched_at"]:
        return {"ok": True, "id": company_id, "already": True, "since": row["watched_at"]}
    conn.execute(
        "UPDATE companies SET watched_at = ?, watched_via = ?, watched_signal_id = ?,"
        " arrival_seen_at = ?, watched_was = NULL WHERE id = ?",
        (stamp, via, signal_id, None if via == "insights" else stamp, company_id))
    conn.execute("UPDATE watch_suggestions SET accepted_at = ? WHERE company_id = ?"
                 " AND accepted_at IS NULL AND dismissed_at IS NULL", (stamp, company_id))
    return {"ok": True, "id": company_id, "watched_at": stamp}


def unwatch(conn, company_id, now=None, undo_add: bool = False, since=None) -> dict:
    """Take a company off. Its contacts, signals and roles stay on file and its
    thread stops counting. undo_add is the Undo right after an add, which puts
    the company back as it was before, never marked as removed; since is the
    watched_at that add returned, and an Undo whose add found the company
    already watched, or that comes after another add, is refused, so it never
    takes off a company that was watched before. Does not commit."""
    row = conn.execute("SELECT id, watched_at FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        return {"ok": False, "note": "That company is no longer on file. Reload the page."}
    if not row["watched_at"]:
        return {"ok": True, "id": company_id, "already": True}
    if undo_add and (not since or row["watched_at"] != since):
        return {"ok": False, "note": "That company was already on your Watchlist before this add, so Undo "
                                     "leaves it there. Use Remove from Watchlist on its record."}
    if undo_add:
        # The suggestion the add answered (watch stamps it with the add's own
        # moment) is waiting again, as if the add never happened.
        conn.execute("UPDATE watch_suggestions SET accepted_at = NULL WHERE company_id = ?"
                     " AND accepted_at = ? AND dismissed_at IS NULL", (company_id, row["watched_at"]))
        conn.execute("UPDATE companies SET watched_at = NULL, watched_via = NULL,"
                     " watched_signal_id = NULL, arrival_seen_at = NULL WHERE id = ?", (company_id,))
    else:
        conn.execute("UPDATE companies SET watched_was = watched_at, watched_at = NULL,"
                     " unwatched_at = ? WHERE id = ?", (iso(moment_of(now)), company_id))
    return {"ok": True, "id": company_id}


def restore(conn, company_id, now=None) -> dict:
    """Remove's Undo: back on the list with the date it first joined."""
    row = conn.execute("SELECT watched_at, watched_was FROM companies WHERE id = ?",
                       (company_id,)).fetchone()
    if row is None:
        return {"ok": False, "note": "That company is no longer on file. Reload the page."}
    if row["watched_at"]:
        return {"ok": True, "id": company_id, "already": True}
    stamp = iso(moment_of(now))
    conn.execute("UPDATE companies SET watched_at = COALESCE(watched_was, ?), watched_was = NULL"
                 " WHERE id = ?", (stamp, company_id))
    conn.execute("UPDATE watch_suggestions SET accepted_at = ? WHERE company_id = ?"
                 " AND accepted_at IS NULL AND dismissed_at IS NULL", (stamp, company_id))
    return {"ok": True, "id": company_id}


def mark_arrivals_seen(conn, ids, now=None) -> int:
    ids = [int(i) for i in ids if str(i).isdigit()][:50]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute(f"UPDATE companies SET arrival_seen_at = ? WHERE id IN ({marks})"
                       " AND arrival_seen_at IS NULL AND watched_at IS NOT NULL",
                       (iso(moment_of(now)), *ids))
    return cur.rowcount


# ================================================================ by name

def _company_counts(conn) -> dict:
    out: dict = {}
    for r in conn.execute("SELECT company_id, COUNT(*) AS n FROM signals WHERE company_id IS NOT NULL"
                          " AND COALESCE(source_id, '') <> 'job-advert' GROUP BY company_id"):
        out.setdefault(r["company_id"], [0, 0])[0] = r["n"]
    for r in conn.execute("SELECT company_id, COUNT(*) AS n FROM opportunities"
                          " WHERE company_id IS NOT NULL GROUP BY company_id"):
        out.setdefault(r["company_id"], [0, 0])[1] = r["n"]
    return out


def seen_words(stories: int, adverts: int) -> str:
    bits = []
    if stories:
        bits.append(f"{stories} {'story' if stories == 1 else 'stories'}")
    if adverts:
        bits.append(f"{adverts} {'advert' if adverts == 1 else 'adverts'}")
    return ", ".join(bits) or "seen, no stories or adverts kept"


def role_words(n: int) -> str:
    """"3 roles", "1 role", or "" for none."""
    return "" if not n else f"{n} {'role' if n == 1 else 'roles'}"


def _role_counts(conn) -> dict:
    """Roles per company: its adverts, one per repost chain."""
    return {r["company_id"]: r["n"] for r in conn.execute(
        "SELECT company_id, COUNT(DISTINCT COALESCE(role_key, 'row-' || id)) AS n"
        " FROM opportunities WHERE company_id IS NOT NULL GROUP BY company_id")}


def find_names(conn, query, limit: int = 5) -> list:
    """Companies Radar has seen whose name matches what was typed, ignoring
    capitals, spacing and endings such as Ltd. Whole-name matches first, then
    names that start with it, then names that contain it. Five at most, each
    with its place where known and its roles where it has any."""
    want = loose_name(query)
    if len(want) < 2:
        return []
    counts = _company_counts(conn)
    roles = _role_counts(conn)
    found = []
    for r in conn.execute("SELECT id, display_name, city, country, watched_at FROM companies"):
        if rc.is_placeholder_company(r["display_name"]):
            continue
        name = loose_name(r["display_name"])
        if not name or want not in name:
            continue
        rank = 0 if name == want else 1 if name.startswith(want) else 2
        stories, adverts = counts.get(r["id"], [0, 0])
        found.append((rank, r["display_name"].lower(), {
            "id": r["id"], "name": r["display_name"], "place": place_of(dict(r)),
            "seen": seen_words(stories, adverts), "roles": role_words(roles.get(r["id"], 0)),
            "watched": bool(r["watched_at"]),
            "since": day_words(r["watched_at"]) if r["watched_at"] else ""}))
    found.sort(key=lambda f: (f[0], f[1]))
    return [f[2] for f in found[:limit]]


def add_by_name(conn, name=None, company_id=None, anyway: bool = False, now=None) -> dict:
    """Add one company by name. A chosen match is added; a name that matches
    one company Radar has seen, by its whole name, adds that one; a name Radar
    has never seen is added only when anyway is set, as a bare row whose
    facts stay Not known. Does not commit."""
    if company_id is not None:
        c = company_row(conn, company_id)
        if c is None:
            return {"ok": False, "note": "That company is no longer on file. Reload the page."}
    else:
        text = " ".join(str(name or "").split())[:NAME_MAX]
        text = rc.sanitise_free_text(text).strip() if text else ""
        if not text:
            return {"ok": False, "field": "name", "note": "Type a company's name first."}
        if rc.is_placeholder_company(text):
            return {"ok": False, "field": "name", "note": "That is not a company's name."}
        want = loose_name(text)
        exact = [m for m in find_names(conn, text, limit=20) if loose_name(m["name"]) == want]
        if len(exact) == 1:
            c = company_row(conn, exact[0]["id"])
        elif len(exact) > 1:
            return {"ok": False, "choose": exact,
                    "note": f"{len(exact)} companies Radar has seen go by that name. Choose one from the list."}
        elif not anyway:
            return {"ok": False, "not_found": True, "name": text,
                    "note": "Nothing Radar has seen matches. Check the spelling, or add it anyway."}
        else:
            cid = rc.resolve_company(conn, text, iso(moment_of(now)))
            c = company_row(conn, cid)
            c["_new"] = True
    if c.get("watched_at"):
        return {"ok": True, "already": True, "id": c["id"], "name": c["display_name"],
                "since": day_words(c["watched_at"])}
    out = watch(conn, c["id"], "name", None, now)
    out.update({"name": c["display_name"], "unknown": bool(c.get("_new")), "since": out.get("watched_at")})
    return out


# ================================================================ suggestions

def signal_kind(s: dict) -> str | None:
    """What a signal can suggest a company on, or None."""
    stype = s.get("story_type") or CODE_TYPES.get(s.get("source_id") or "")
    if s.get("source_id") == "job-advert" or stype == "buying-window":
        return "buying-window"
    if stype == "funding":
        # Under 40 it is not a suggestion at all: under the story bar too.
        return "funding" if (s.get("relevance") or 0) >= FUNDING_BAR else None
    if (s.get("relevance") or 0) >= SUGGEST_BAR:
        return "story"
    return None


def _strength(kind: str, relevance, found) -> tuple:
    return (KIND_RANK.get(kind, 0), relevance or 0, found or "")


def refresh_suggestions(conn, now=None) -> dict:
    """Fold, fill and strengthen the suggestions, after a signal watcher run.
    Code only. Does not commit."""
    moment = moment_of(now)
    stamp = iso(moment)
    folded = conn.execute(
        "UPDATE watch_suggestions SET dismissed_at = ?, dismissed_by = 'radar'"
        " WHERE accepted_at IS NULL AND dismissed_at IS NULL AND suggested_at <= ?",
        (stamp, iso(moment - timedelta(days=FOLD_DAYS)))).rowcount
    since = iso(moment - timedelta(days=SIGNAL_DAYS))
    companies = _companies(conn)
    best: dict = {}
    for r in conn.execute(
            "SELECT id, company_id, company, source_id, story_type, relevance, first_seen"
            " FROM signals WHERE company_id IS NOT NULL AND acknowledged_at IS NULL"
            " AND relevance IS NOT NULL AND first_seen >= ?", (since,)):
        s = dict(r)
        c = companies.get(s["company_id"])
        if not c or c.get("watched_at") or c.get("state") == "dead":
            continue
        if rc.is_placeholder_company(c["display_name"]) or rc.is_placeholder_company(s.get("company")):
            continue
        if c.get("unwatched_at") and (s["first_seen"] or "") <= c["unwatched_at"]:
            continue          # removed: only a signal since the removal brings it back
        kind = signal_kind(s)
        if not kind:
            continue
        key = _strength(kind, s["relevance"], s["first_seen"])
        if s["company_id"] not in best or key > best[s["company_id"]][0]:
            best[s["company_id"]] = (key, kind, s)
    history: dict = {}
    for r in conn.execute("SELECT * FROM watch_suggestions ORDER BY id"):
        history.setdefault(r["company_id"], []).append(dict(r))
    made = upgraded = 0
    fresh = []
    for cid, (key, kind, s) in best.items():
        rows = history.get(cid) or []
        waiting = [h for h in rows if not h["accepted_at"] and not h["dismissed_at"]]
        if waiting:
            w = waiting[-1]
            if key > _strength(w["kind"], w["relevance"], ""):
                if KIND_RANK[kind] > KIND_RANK.get(w["kind"], 0) or s["id"] != w["signal_id"]:
                    conn.execute("UPDATE watch_suggestions SET kind = ?, signal_id = ?, relevance = ?"
                                 " WHERE id = ?", (kind, s["id"], s["relevance"], w["id"]))
                    upgraded += 1
            continue
        # The latest answer decides. An accepted suggestion for a company that
        # was later removed is answered; the removal rule above already asked
        # for a signal since.
        dismissed = [h for h in rows if h["dismissed_at"]]
        last = rows[-1] if rows else None
        came_back = 0
        # A dismissal the owner later answered by adding the company (and then
        # removing it) no longer holds it back: the removal rule above already
        # asked for a signal since.
        gone = companies[cid].get("unwatched_at")
        if last and last["dismissed_at"] and not (gone and gone > last["dismissed_at"]):
            if any(h["came_back"] for h in dismissed):
                continue          # it has had its one return
            if KIND_RANK[kind] <= max(KIND_RANK.get(h["kind"], 0) for h in dismissed):
                continue          # only a stronger kind of signal brings it back
            came_back = 1
        fresh.append((key, cid, kind, s, came_back))
    free = WAITING_MAX - waiting_count(conn)
    fresh.sort(key=lambda f: f[0], reverse=True)
    for key, cid, kind, s, came_back in fresh[:max(free, 0)]:
        conn.execute("INSERT INTO watch_suggestions (company_id, kind, signal_id, relevance,"
                     " suggested_at, came_back) VALUES (?,?,?,?,?,?)",
                     (cid, kind, s["id"], s["relevance"], stamp, came_back))
        made += 1
    return {"folded": folded, "made": made, "upgraded": upgraded,
            "held": max(len(fresh) - max(free, 0), 0)}


def waiting_count(conn) -> int:
    """The suggestions waiting that the page shows: unanswered, for a company
    not on the Watchlist."""
    return conn.execute(
        "SELECT COUNT(*) FROM watch_suggestions s JOIN companies c ON c.id = s.company_id"
        " WHERE s.accepted_at IS NULL AND s.dismissed_at IS NULL AND c.watched_at IS NULL").fetchone()[0]


def _suggestion(conn, sid) -> dict | None:
    row = conn.execute("SELECT * FROM watch_suggestions WHERE id = ?", (sid,)).fetchone()
    return dict(row) if row else None


def answer_suggestion(conn, sid: int, action: str, now=None) -> dict:
    """accept, dismiss, undo (either answer) or again (Suggest again from the
    Dismissed fold). Does not commit."""
    stamp = iso(moment_of(now))
    s = _suggestion(conn, sid)
    if s is None:
        return {"ok": False, "note": "That suggestion is no longer on file. Reload the page."}
    c = company_row(conn, s["company_id"]) or {}
    name = c.get("display_name") or "That company"
    if action == "accept":
        if s["accepted_at"] or s["dismissed_at"]:
            return {"ok": False, "note": f"{name} has already been answered, perhaps in another tab. "
                                         "Reload the page."}
        out = watch(conn, s["company_id"], "suggestion", s["signal_id"], stamp)
        conn.execute("UPDATE watch_suggestions SET accepted_at = ? WHERE id = ?", (stamp, sid))
        out.update({"name": name, "company_id": s["company_id"]})
        return out
    if action == "dismiss":
        if s["accepted_at"] or s["dismissed_at"]:
            return {"ok": False, "note": f"{name} has already been answered, perhaps in another tab. "
                                         "Reload the page."}
        conn.execute("UPDATE watch_suggestions SET dismissed_at = ?, dismissed_by = 'owner'"
                     " WHERE id = ?", (stamp, sid))
        return {"ok": True, "id": sid, "name": name}
    if action == "undo":
        if (s["accepted_at"] or s["dismissed_at"]) and waiting_count(conn) >= WAITING_MAX:
            return {"ok": False, "note": f"{WAITING_MAX} suggestions are already waiting, so {name} stays "
                                         "where it is. Add or dismiss one first."}
        if s["accepted_at"]:
            if c.get("watched_at") == s["accepted_at"]:
                unwatch(conn, s["company_id"], now, undo_add=True, since=s["accepted_at"])
            conn.execute("UPDATE watch_suggestions SET accepted_at = NULL WHERE id = ?", (sid,))
        elif s["dismissed_at"]:
            conn.execute("UPDATE watch_suggestions SET dismissed_at = NULL, dismissed_by = NULL"
                         " WHERE id = ?", (sid,))
        else:
            return {"ok": False, "note": f"{name} is already waiting. Reload the page."}
        return {"ok": True, "id": sid, "name": name}
    if action == "again":
        if not s["dismissed_at"]:
            return {"ok": False, "note": f"{name} is not in Dismissed. Reload the page."}
        if c.get("watched_at"):
            return {"ok": False, "note": f"{name} is already on your Watchlist."}
        if waiting_count(conn) >= WAITING_MAX:
            return {"ok": False, "note": f"{WAITING_MAX} suggestions are already waiting. "
                                         "Add or dismiss one first."}
        conn.execute("UPDATE watch_suggestions SET dismissed_at = NULL, dismissed_by = NULL,"
                     " suggested_at = ? WHERE id = ?", (stamp, sid))
        return {"ok": True, "id": sid, "name": name}
    return {"ok": False, "note": "Unknown answer."}


def _signal_link(conn, s: dict) -> tuple:
    """(href, words) for where a signal opens: a buying window on Jobs (hiring
    is not shown on Insights), anything else as its story on Insights."""
    if s.get("source_id") == "job-advert":
        row = conn.execute("SELECT id FROM opportunities WHERE url_hash = ?",
                           (s.get("url_hash"),)).fetchone() if s.get("url_hash") else None
        return (f"/jobs/{row['id']}" if row else "/jobs", "Open the role on Jobs")
    return (f"/insights/story/{s['id']}", "Open the story on Insights")


def suggestions(conn, now=None) -> dict:
    """{"waiting": [...], "dismissed": [...]} for the page, strongest first."""
    moment = moment_of(now)
    rows = [dict(r) for r in conn.execute("SELECT * FROM watch_suggestions ORDER BY id")]
    latest: dict = {}
    for r in rows:
        latest[r["company_id"]] = r
    companies = _companies(conn)
    contacts = _contacts_by_company(conn, companies)
    signals = {}
    ids = [r["signal_id"] for r in rows if r["signal_id"]]
    if ids:
        marks = ",".join("?" * len(ids))
        signals = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM signals WHERE id IN ({marks})", ids)}
    waiting, dismissed = [], []
    cut = iso(moment - timedelta(days=DISMISSED_DAYS))
    for cid, r in latest.items():
        c = companies.get(cid)
        if not c:
            continue
        s = signals.get(r["signal_id"]) or {}
        href, link_words = _signal_link(conn, s) if s else ("", "")
        last = (contacts.get(cid) or [None])[0]
        item = {"id": r["id"], "company_id": cid, "name": c["display_name"], "place": place_of(c),
                "kind": r["kind"], "kind_words": KIND_WORDS[r["kind"]],
                "type_words": TYPE_WORDS.get(s.get("story_type") or CODE_TYPES.get(s.get("source_id") or "")
                                             or "other", "Other news"),
                "relevance": r["relevance"], "headline": rc.clean_headline(s.get("headline") or ""),
                "summary": s.get("card_summary") or "", "found": s.get("first_seen"),
                "is_advert": s.get("source_id") == "job-advert", "href": href, "link_words": link_words,
                "came_back": bool(r["came_back"]), "suggested_at": r["suggested_at"],
                "dismissed_at": r["dismissed_at"], "dismissed_by": r["dismissed_by"],
                "last_contact": last}
        if not r["accepted_at"] and not r["dismissed_at"] and not c.get("watched_at"):
            waiting.append(item)
        elif r["dismissed_at"] and r["dismissed_at"] >= cut and not c.get("watched_at"):
            dismissed.append(item)
    waiting.sort(key=lambda i: _strength(i["kind"], i["relevance"], i["found"]), reverse=True)
    dismissed.sort(key=lambda i: i["dismissed_at"] or "", reverse=True)
    return {"waiting": waiting, "dismissed": dismissed}


# ================================================================ writes from the record

def log_contact(conn, company_id, fields: dict, now=None) -> dict:
    """One contact, by the owner: its date, how, a note, what came back, and
    the next step with its due date. Leaving the next step empty closes the
    thread. A company not on the Watchlist joins it. Does not commit."""
    moment = moment_of(now)
    today = uk(moment).date()
    c = company_row(conn, company_id)
    if c is None:
        return {"ok": False, "note": "That company is no longer on file. Reload the page."}
    raw_day = str(fields.get("date") or "").strip()
    when = clean_date(raw_day) if raw_day else today.isoformat()
    if when is None:
        return {"ok": False, "field": "date", "note": "Give the date as a date, for example 13 Nov 2026."}
    contact_day = date.fromisoformat(when)
    if contact_day > today:
        return {"ok": False, "field": "date", "note": "A contact cannot be in the future. Choose today or earlier."}
    channel = str(fields.get("channel") or "")
    if channel not in CHANNEL_WORDS:
        return {"ok": False, "field": "channel", "note": "Choose how the contact was made from the list."}
    outcome = str(fields.get("outcome") or "none")
    if outcome not in OUTCOMES:
        return {"ok": False, "field": "outcome", "note": "Choose what came back: None, Reply or Conversation."}
    note = rc.sanitise_free_text(str(fields.get("note") or "").strip())[:NOTE_MAX] or None
    step, due, refusal = clean_next(fields.get("next_step"), fields.get("due"), contact_day)
    if refusal:
        return {"ok": False, "field": refusal[0], "note": refusal[1]}
    touched = iso(moment) if contact_day == today else f"{when}T12:00:00Z"
    newest = (contacts_for(conn, company_id) or [None])[0]
    if step and newest and (newest.get("touched_at") or "") > touched:
        return {"ok": False, "field": "next_step",
                "note": f"The thread follows the newest contact, on {day_words(newest['touched_at'])}, so a "
                        "contact before it cannot set the next step. Leave the next step empty, or move the "
                        "date on the thread."}
    cur = conn.execute(
        "INSERT INTO touches (company, touched_at, channel, note, next_action, next_action_date,"
        " outcome, company_id) VALUES (?,?,?,?,?,?,?,?)",
        (c["display_name"], touched, channel, note, step, due, outcome, company_id))
    added = False
    if not c.get("watched_at"):
        watch(conn, company_id, "contact", None, now)
        added = True
    th = thread_of(contacts_for(conn, company_id), today)
    return {"ok": True, "id": company_id, "touch_id": cur.lastrowid, "added": added,
            "name": c["display_name"], "thread": _thread_public(th)}


def set_thread(conn, company_id, step, due, now=None, restore: bool = False) -> dict:
    """Move the date or clear the next step of the open thread, on its newest
    contact. restore is Undo, which puts back exactly what was there before,
    a next step with no date among it. Does not commit."""
    today = uk(moment_of(now)).date()
    contacts = contacts_for(conn, company_id)
    if not contacts:
        return {"ok": False, "note": "There is no contact to set a next step on. Log a contact first."}
    last = contacts[0]
    if restore and step and not due:
        # Undo of a cleared step that was on file with no date, from before a
        # date was needed. Nothing else is let past the rule.
        clean_step = rc.sanitise_free_text(" ".join(str(step or "").split()))[:STEP_MAX] or None
        clean_due = None
    else:
        clean_step, clean_due, refusal = clean_next(step, due, uk_day(last["touched_at"]))
        if refusal:
            return {"ok": False, "field": refusal[0], "note": refusal[1]}
    before = {"next_step": last.get("next_action") or "", "due": last.get("next_action_date") or ""}
    conn.execute("UPDATE touches SET next_action = ?, next_action_date = ? WHERE id = ?",
                 (clean_step, clean_due, last["id"]))
    th = thread_of(contacts_for(conn, company_id), today)
    return {"ok": True, "id": company_id, "before": before, "thread": _thread_public(th)}


def set_outcome(conn, touch_id: int, outcome: str) -> dict:
    if outcome not in OUTCOMES:
        return {"ok": False, "note": "Choose what came back: None, Reply or Conversation."}
    cur = conn.execute("UPDATE touches SET outcome = ? WHERE id = ?", (outcome, touch_id))
    if not cur.rowcount:
        return {"ok": False, "note": "That contact is no longer on file. Reload the page."}
    return {"ok": True, "id": touch_id, "outcome": outcome}


def add_person(conn, company_id, name, title, now=None) -> dict:
    c = company_row(conn, company_id)
    if c is None:
        return {"ok": False, "note": "That company is no longer on file. Reload the page."}
    who = rc.sanitise_free_text(" ".join(str(name or "").split()))[:PERSON_MAX]
    what = rc.sanitise_free_text(" ".join(str(title or "").split()))[:PERSON_MAX]
    if not who:
        return {"ok": False, "field": "name", "note": "Give the person's name."}
    if not what:
        return {"ok": False, "field": "title", "note": "Give the person's title, for example Head of Quality."}
    cur = conn.execute("INSERT INTO company_people (company_id, name, title, added_at) VALUES (?,?,?,?)",
                       (company_id, who, what, iso(moment_of(now))))
    return {"ok": True, "id": cur.lastrowid, "company_id": company_id, "name": who, "title": what}


def remove_person(conn, person_id, undo: bool = False, now=None) -> dict:
    cur = conn.execute(
        "UPDATE company_people SET removed_at = ? WHERE id = ? AND removed_at IS "
        + ("NOT NULL" if undo else "NULL"), (None if undo else iso(moment_of(now)), person_id))
    if not cur.rowcount:
        return {"ok": False, "note": "That person is no longer on file. Reload the page."}
    return {"ok": True, "id": person_id}


# ================================================================ Read again

# What Read again sends the fast model: the enricher prompt, up to 4,000
# characters of the newest story or advert and up to 6,000 of the company's
# site, about four characters a token; and what it can send back, 900 tokens
# at most. Priced as Home prices the week.
_PROMPT_CHARS = 4200


def reread_cost(conn, config: dict) -> dict:
    """What Read again costs, in US cents, before it is pressed: the lowest is
    the prompt and a short item with no site, the highest a full item, a full
    site and the longest reply."""
    import build_dashboard
    import enrich_company
    model = config.get("claude_model_extract", "claude-haiku-4-5")
    p_in, p_out = build_dashboard.PRICES_PER_MTOK.get(model, build_dashboard.PRICE_FALLBACK)
    low = ((_PROMPT_CHARS + 400) / 4 * p_in + 150 * p_out) / 1_000_000 * 100
    high = ((_PROMPT_CHARS + 4000 + 6000) / 4 * p_in + enrich_company.MAX_TOKENS * p_out) / 1_000_000 * 100
    lo, hi = round(low, 1), round(high, 1)
    return {"low": lo, "high": hi, "words": f"about {lo:g} to {hi:g} cents", "model": model}


def reread_source(conn, company_id) -> dict:
    """The text Read again sends and the site it fetches: the newest story or
    advert that names the company, and the site the facts were read from, or
    one that story names. No site is ever guessed from the name."""
    import enrich_company
    c = company_row(conn, company_id) or {}
    item, label = "", ""
    s = conn.execute("SELECT headline, summary, source_url, first_seen FROM signals WHERE company_id = ?"
                     " ORDER BY first_seen DESC, id DESC LIMIT 1", (company_id,)).fetchone()
    o = conn.execute("SELECT title, description_text, source_url, first_seen FROM opportunities"
                     " WHERE company_id = ? ORDER BY first_seen DESC, id DESC LIMIT 1",
                     (company_id,)).fetchone()
    if s and (not o or (s["first_seen"] or "") >= (o["first_seen"] or "")):
        item = "\n".join(x for x in (s["headline"], s["summary"], s["source_url"]) if x)
        label = f"The story {rc.clean_headline(s['headline'] or '')}, {day_words(s['first_seen'])}"
    elif o:
        item = "\n".join(x for x in (o["title"], o["description_text"], o["source_url"]) if x)
        label = f"The advert {o['title'] or ''}, {day_words(o['first_seen'])}"
    site = c.get("site_url") or enrich_company.candidate_site(item)
    return {"item": item, "label": label.strip(", "), "site": site}


def reread(conn, company_id, config: dict, now=None) -> dict:
    """Read the company's facts again: one polite fetch of its site, robots
    respected, and one call to the fast model. Logs a run. Commits."""
    import enrich_company
    c = company_row(conn, company_id)
    if c is None:
        return {"ok": False, "note": "That company is no longer on file. Reload the page."}
    src = reread_source(conn, company_id)
    if not src["item"] and not src["site"]:
        return {"ok": False, "note": f"Radar has no story, advert or site for {c['display_name']} to "
                                     "read from yet. It reads the facts when one names the company."}
    res = enrich_company.enrich_company(conn, company_id, src["item"], config, None, force=True,
                                        site_url=src["site"], item_label=src["label"])
    enrich_company._busy_retry(conn.commit, tries=4)
    # The run row records what the call cost, so it is written even when the
    # reading itself could not be, and tried again while the database is busy.
    enrich_company._busy_retry(lambda: rc.log_run(
        conn, "enrich", mode="mock" if rc.mock_mode_active() else "live",
        items_in=1, items_new=1 if res.get("enriched") else 0,
        model=config.get("claude_model_extract"), usage=res.get("usage") or {},
        note=f"Read again from the Watchlist: {res.get('status')}"[:500],
        outcome="ok" if res.get("enriched") else "failed"), tries=4)
    enrich_company._busy_retry(conn.commit, tries=4)
    import cv_screen
    cents = round(cv_screen.dollars_of(res.get("usage") or {}, config.get("claude_model_extract")) * 100, 2)
    return {"ok": bool(res.get("enriched")), "id": company_id, "status": res.get("status"),
            "cents": cents, "site": src["site"] or "",
            "note": "" if res.get("enriched") else f"The facts could not be read again: {res.get('status')}."}


# ================================================================ reading for the page

def _place_or_unknown(c: dict) -> str:
    return place_of(c) or "Place not known"


def _people(c: dict, hand: list) -> list:
    out = []
    try:
        published = json.loads(c.get("people") or "[]")
    except (TypeError, ValueError):
        published = []
    src = (fact_source(c, "people") or {})
    for p in published if isinstance(published, list) else []:
        if isinstance(p, dict) and p.get("name"):
            role = {"ceo": "CEO", "cto": "CTO"}.get(str(p.get("role") or "").lower(), str(p.get("role") or ""))
            out.append({"name": p["name"], "title": role, "hand": False, "source": src.get("label", ""),
                        "at": src.get("at")})
    for p in hand:
        out.append({"id": p["id"], "name": p["name"], "title": p["title"] or "", "hand": True,
                    "source": "Added by you", "at": p["added_at"]})
    return out


def fact_source(c: dict, field: str) -> dict | None:
    try:
        sources = json.loads(c.get("fact_sources") or "{}")
    except (TypeError, ValueError):
        sources = {}
    got = sources.get(field) if isinstance(sources, dict) else None
    if isinstance(got, dict):
        return got
    if not c.get("enriched_at"):
        return None
    status = str(c.get("enrich_status") or "")
    label = ("The company site or the first story" if status.startswith("ok")
             else "The first story or advert Radar saw")
    return {"from": "site" if status.startswith("ok") else "item", "label": label, "at": c["enriched_at"]}


def facts(c: dict) -> list:
    rows = []
    for key, label in FACT_FIELDS:
        if key == "where":
            value = place_of(c)
            src = fact_source(c, "city") or fact_source(c, "country")
        else:
            value = c.get(key) or ""
            if key == "stage" and value:
                value = ("Series " + value.split(" ", 1)[1].upper()) if value.lower().startswith("series ") \
                    else value[:1].upper() + value[1:]
            src = fact_source(c, key)
        rows.append({"label": label, "value": value, "source": (src or {}).get("label", "") if value else "",
                     "at": (src or {}).get("at") if value else None})
    if c.get("region"):
        rows.append({"label": "Region", "value": c["region"],
                     "source": "Worked out by Radar from Where, using your local places list", "at": None})
    # Ecosystem, as the old company page showed it, only when known.
    if c.get("ecosystem"):
        src = fact_source(c, "ecosystem")
        rows.append({"label": "Ecosystem", "value": c["ecosystem"],
                     "source": (src or {}).get("label", ""), "at": (src or {}).get("at")})
    return rows


def _latest_signals(conn, companies: dict) -> dict:
    """The newest shown signal per company, one per event."""
    out: dict = {}
    for r in conn.execute("SELECT * FROM signals WHERE company_id IS NOT NULL AND acknowledged_at IS NULL"
                          " AND relevance IS NOT NULL ORDER BY first_seen DESC, id DESC"):
        if r["company_id"] not in out:
            out[r["company_id"]] = dict(r)
    return out


def _open_roles(conn) -> dict:
    out: dict = {}
    for r in conn.execute("SELECT company_id, COUNT(*) AS n FROM opportunities WHERE company_id IS NOT NULL"
                          " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
                          " AND acknowledged_at IS NULL GROUP BY company_id"):
        out[r["company_id"]] = r["n"]
    return out


def _thread_public(th: dict) -> dict:
    last = th.get("last") or {}
    return {"state": th["state"], "due": th.get("due", False), "next_step": th.get("next_step") or "",
            "due_date": th.get("due_date"), "days": th.get("days", 0),
            "last_at": last.get("touched_at"), "last_channel": last.get("channel"),
            "set_at": th.get("set_at")}


SORT_GROUP = {"overdue": 0, "today": 1, "upcoming": 2, "nodate": 3, "closed": 4, "none": 4}


def _signal_public(conn, s: dict | None) -> dict | None:
    if not s:
        return None
    kind = s.get("story_type") or CODE_TYPES.get(s.get("source_id") or "") or "other"
    href, words = _signal_link(conn, s)
    return {"id": s["id"], "type": kind, "type_words": TYPE_WORDS.get(kind, "Other news"),
            "relevance": s.get("relevance") or 0, "found": s.get("first_seen"),
            "headline": rc.clean_headline(s.get("headline") or "Untitled"),
            "summary": s.get("card_summary") or "", "href": href, "link_words": words,
            "is_advert": s.get("source_id") == "job-advert"}


def company_rows(conn, now=None) -> list:
    """The watched companies in the table's order: overdue, then today, then
    the next due dates, then threads with no date, then companies with no
    next step, newest signal first."""
    moment = moment_of(now)
    today = uk(moment).date()
    companies = _companies(conn)
    contacts = _contacts_by_company(conn, companies)
    latest = _latest_signals(conn, companies)
    roles = _open_roles(conn)
    windows = {r[0] for r in conn.execute(
        "SELECT DISTINCT company_id FROM signals WHERE source_id = 'job-advert' AND company_id IS NOT NULL")}
    items = {r[0] for r in conn.execute(
        "SELECT company_id FROM opportunities WHERE company_id IS NOT NULL UNION"
        " SELECT company_id FROM signals WHERE company_id IS NOT NULL")}
    rows = []
    for cid, c in companies.items():
        if not c.get("watched_at"):
            continue
        th = thread_of(contacts.get(cid) or [], today)
        state = rc.company_display_state(c.get("state"), cid in items, bool(contacts.get(cid)),
                                         cid in windows)
        sig = _signal_public(conn, latest.get(cid))
        arrived_today = uk_day(c["watched_at"]) == today
        rows.append({
            "id": cid, "name": c["display_name"], "place": place_of(c), "company": c,
            "thread": _thread_public(th), "latest": sig, "roles": roles.get(cid, 0),
            "state": state, "state_words": STATE_WORDS.get(state, "Not contacted"),
            "via": c.get("watched_via"), "watched_at": c["watched_at"],
            "arrival": ({"via": c.get("watched_via"), "at": c["watched_at"]}
                        if arrived_today and c.get("watched_via") in ("insights", "suggestion") else None),
        })

    def key(r):
        t = r["thread"]
        group = SORT_GROUP[t["state"]]
        if group <= 2:
            return (group, t["due_date"] or "", r["name"].lower())
        newest = (r["latest"] or {}).get("found") or ""
        return (group, "".join(chr(0x10FFFF - ord(ch)) for ch in newest), r["name"].lower())
    rows.sort(key=key)
    return rows


def record(conn, company_id, config: dict | None = None, now=None) -> dict | None:
    """Everything the record shows for one company, watched or not."""
    moment = moment_of(now)
    today = uk(moment).date()
    c = company_row(conn, company_id)
    if c is None:
        return None
    contacts = contacts_for(conn, company_id)
    th = thread_of(contacts, today)
    windows = conn.execute("SELECT 1 FROM signals WHERE company_id = ? AND source_id = 'job-advert' LIMIT 1",
                           (company_id,)).fetchone() is not None
    has_items = conn.execute("SELECT 1 FROM opportunities WHERE company_id = ? UNION"
                             " SELECT 1 FROM signals WHERE company_id = ? LIMIT 1",
                             (company_id, company_id)).fetchone() is not None
    state = rc.company_display_state(c.get("state"), has_items, bool(contacts), windows)
    sigs, seen_events = [], set()
    for r in conn.execute("SELECT * FROM signals WHERE company_id = ? AND acknowledged_at IS NULL"
                          " AND relevance IS NOT NULL ORDER BY first_seen DESC, id DESC", (company_id,)):
        key = r["event_key"] or f"e{r['id']}"
        if key in seen_events:
            continue
        seen_events.add(key)
        reports = conn.execute("SELECT COUNT(*) FROM signals WHERE COALESCE(event_key, 'e' || id) = ?",
                               (key,)).fetchone()[0]
        item = _signal_public(conn, dict(r))
        item["reports"] = reports
        item["why"] = r["why"] or ""
        item["from_here"] = r["id"] == c.get("watched_signal_id")
        sigs.append(item)
    drafts = None
    d = conn.execute("SELECT id, headline, first_seen, draft_comment, draft_note, drafted_at FROM signals"
                     " WHERE company_id = ? AND (COALESCE(draft_comment, '') <> '' OR COALESCE(draft_note, '') <> '')"
                     " ORDER BY first_seen DESC, id DESC LIMIT 1", (company_id,)).fetchone()
    if d:
        drafts = {"signal_id": d["id"], "headline": rc.clean_headline(d["headline"] or ""),
                  "found": d["first_seen"], "written": d["drafted_at"] or d["first_seen"],
                  "comment": d["draft_comment"] or "", "note": d["draft_note"] or "",
                  "href": f"/insights/story/{d['id']}"}
    roles = []
    for r in conn.execute(
            "SELECT * FROM opportunities WHERE company_id = ? AND closed_at IS NULL AND dismissed_at IS NULL"
            " AND expired_at IS NULL AND acknowledged_at IS NULL ORDER BY first_seen DESC, id DESC LIMIT 12",
            (company_id,)):
        roles.append({"id": r["id"], "title": r["title"] or "A role",
                      "tier": "applied" if r["applied_at"] else (r["tier"] or ""),
                      "tier_words": "Applied" if r["applied_at"] else TIER_WORDS.get(r["tier"] or "", "Not scored"),
                      "score": r["cv_match"], "basis": r["score_basis"], "location": r["location"] or "",
                      "pay": r["salary_rate"] or "", "engagement": r["engagement_type"] or "",
                      "first_seen": r["first_seen"], "href": f"/jobs/{r['id']}",
                      "prepare": f"/prepare/{r['id']}", "window": bool(r["buying_window"])})
    hand = [dict(r) for r in conn.execute(
        "SELECT * FROM company_people WHERE company_id = ? AND removed_at IS NULL ORDER BY id", (company_id,))]
    from_story = None
    if c.get("watched_signal_id"):
        s = conn.execute("SELECT id, headline FROM signals WHERE id = ?", (c["watched_signal_id"],)).fetchone()
        if s:
            from_story = {"id": s["id"], "headline": rc.clean_headline(s["headline"] or "")}
    return {
        "id": company_id, "name": c["display_name"], "place": place_of(c), "company": c,
        "watched": bool(c.get("watched_at")), "watched_at": c.get("watched_at"),
        "via": c.get("watched_via"), "via_words": VIA_WORDS.get(c.get("watched_via") or "", ""),
        "from_story": from_story, "state": state, "state_words": STATE_WORDS.get(state, "Not contacted"),
        "stored_state": c.get("state"), "thread": _thread_public(th), "contacts": contacts,
        "drafts": drafts, "signals": sigs, "roles": roles, "facts": facts(c),
        "people": _people(c, hand), "enriched_at": c.get("enriched_at"),
        "enrich_status": c.get("enrich_status"), "site_url": c.get("site_url") or "",
        "reread": reread_cost(conn, config or {}) if config is not None else None,
        "today": today.isoformat(),
    }


def last_watcher_run(conn) -> str | None:
    row = conn.execute("SELECT MAX(ts) FROM runs WHERE workflow = 'signals'").fetchone()
    return row[0] if row else None


def build_model(conn, config: dict | None = None, now=None, company_id=None) -> dict:
    """The whole page: the header counts, the arrival from Insights, the
    suggestions, the table and the selected company's record."""
    moment = moment_of(now)
    rows = company_rows(conn, moment)
    sugg = suggestions(conn, moment)
    due = [r for r in rows if r["thread"]["due"]]
    arrivals = []
    for r in rows:
        c = r["company"]
        if c.get("watched_via") == "insights" and not c.get("arrival_seen_at"):
            story = None
            if c.get("watched_signal_id"):
                s = conn.execute("SELECT id, headline FROM signals WHERE id = ?",
                                 (c["watched_signal_id"],)).fetchone()
                if s:
                    story = {"id": s["id"], "headline": rc.clean_headline(s["headline"] or ""),
                             "href": f"/insights/story/{s['id']}"}
            arrivals.append({"id": r["id"], "name": r["name"], "at": c["watched_at"], "story": story})
    selected = None
    missing = False
    if company_id is not None:
        selected = record(conn, company_id, config, moment)
        missing = selected is None
    ids = [r["id"] for r in rows]
    position = None
    if selected and selected["id"] in ids:
        i = ids.index(selected["id"])
        position = {"index": i + 1, "total": len(ids),
                    "prev": ids[i - 1] if i > 0 else None,
                    "next": ids[i + 1] if i + 1 < len(ids) else None}
    return {
        "now": moment, "rows": rows, "suggestions": sugg["waiting"], "dismissed": sugg["dismissed"],
        "arrivals": arrivals, "selected": selected, "missing": missing,
        "missing_id": company_id if missing else None, "position": position,
        "counts": {"watched": len(rows), "due": follow_ups_due_count(conn, moment),
                   "today": sum(1 for r in due if r["thread"]["state"] == "today"),
                   "overdue": sum(1 for r in due if r["thread"]["state"] == "overdue"),
                   "waiting": len(sugg["waiting"])},
        "watcher_ran": last_watcher_run(conn),
        "channels": CHANNELS, "outcomes": OUTCOME_WORDS, "set_states": SET_STATES,
    }
