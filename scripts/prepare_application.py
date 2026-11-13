#!/usr/bin/env python
"""Prepare application. The advert's full description, scored once, with its asks.

The button on a Jobs row opens /prepare/<id>. That page only reads. When it is
opened by the click (it says so in its address, and the browser says the click
came from this site) its script makes the one write this module serves,
POST /prepare, and this is what that does for an open role:

1. If the row stores no description, fetch the advert's public LinkedIn page
   once, with the page checker's own fetch, user agent and ladder, and store
   what it says with the time. No login, no retry, no loop. A 429 is a wait,
   not a failure to hammer: nothing is stored, and nothing is asked of LinkedIn
   again from this page for the page checker's backoff, ten minutes. Any other
   page that cannot be read is not asked for again for five minutes. If the
   page cannot be had, the page opens with the card's score and says so.
2. Score the role against the stored description, once per version of it. The
   score is stamped spec, with the prompt version and the model, the old score
   goes to score_history first, and the new one is recorded there too. The
   scorer returns each ask the advert makes as met, partly or missing, with the
   CV lines that evidence it, and the page shows them. If the tier moved, the
   move is recorded in the history and reported, and nothing is dismissed.

What stops one click being paid for twice. A role has one lock in this process,
so a second click on it waits for the first, and then finds the score stored
for this version of the description and spends nothing. A score is made once
per version of the stored description (S9), so a reload, a second tab and the
button pressed again all find the work done. One click is one model call, with
no retry on a bad reply. A reply that could not be used is not paid for again
inside five minutes and a call that raised is not repeated inside twenty
seconds, so a failure that repeats cannot be clicked into a bill. The page
script sends one POST per click and none on its own after that.

Work that has been paid for is not thrown away because the database is busy.
The inbox run holds the write lock for the whole of an email, model calls
included, so a click can land in the middle of one. The writes below wait for
the lock for up to a minute and a half. If it stays busy, the page that was
read or the reply that was paid for is held in memory for half an hour and the
next click stores it, without a second fetch or a second call.

An applied role is shown, never touched. Nothing is fetched, scored or written
for a role marked applied or for any row of its chain, and the same goes for a
role that is acknowledged, closed, dismissed or expired. The writes below look
at the role again under the write lock, after the page has come and after the
model has answered, because either can take long enough for that to change.

Nothing here runs on a schedule, and nothing here closes, dismisses or
acknowledges a role.

The tailored CV. The same page holds a copy of the master CV for the role,
made from the master the first time the page opens, with no model call, and
edited there (tailored_cv.py stores it, cv_doc.py lays it out). Rescore role
scores the copy on screen against the stored description through the same
steps as the click above: the same lock idea, held per role so that the
adverts of a repost chain share it, the same one call with no retry, the same
failure windows and the same parking of a paid-for reply. A pair of
description and copy is scored once, by the version of each. The score is the
copy's: it is stored on the copy and in score_history marked tailored, and the
role keeps the master CV's score, which is what the Jobs page shows. Save CV
stores the copy as the one to send, and Start again from master puts the
master back on screen. None of them touches an applied role, or one that is
acknowledged, closed, dismissed or expired, and none of them writes the master.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402
import check_jobs  # noqa: E402
import cv_doc  # noqa: E402
import cv_store  # noqa: E402
import score_item  # noqa: E402
import tailored_cv  # noqa: E402

SPEC = rc.SCORE_BASIS_SPEC

LOCK_WAIT_SECONDS = 150          # a second click waits this long for the first
UNUSABLE_COOLDOWN_SECONDS = 300  # a reply that was no use is not paid for again
ERROR_COOLDOWN_SECONDS = 20      # a call that raised is not repeated at once
PAGE_FAILURE_COOLDOWN_SECONDS = 300  # a page that could not be read is not asked for again
WRITE_WAIT_SECONDS = 90          # a write waits this long for another process's lock
PARK_SECONDS = 1800              # work paid for, held when it could not be stored
RATE_QUOTE_MAX = 200             # a pay quote is short or it is not a quote

# The columns a score writes, the same ones a re-score writes beside the
# score itself, so a row scored against its full description reads exactly as
# a row scored against its card does.
GATE_COLS = ("gate_sector", "gate_sector_note", "gate_cv", "gate_cv_note",
             "gate_location", "gate_location_note", "gate_rate",
             "gate_rate_note", "tier", "failed_gates", "question_text",
             "filter_reason", "rate_stated", "rate_value", "rate_basis",
             "ir35", "location_class", "engagement_type", "engagement_note")
RECORD_COLS = ("score_basis", "prompt_version", "score_model", "scored_at",
               "description_hash")

TIER_WORDS = {"top": "Top prospects", "question": "One question away",
              "reading": "Reading", "filtered": "Filtered"}


# ----------------------------------------------------------------- the guard

def _mono() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """The politeness gap between page requests. Tests never wait."""
    if seconds > 0 and not rc.mock_mode_active():
        time.sleep(seconds)


class Guard:
    """What stops one click being paid for twice, kept in memory.

    One per process, which is one per dashboard. A restart forgets it, and
    forgets only what is safe to forget: the locks are held for the length of
    a click, the waits and the failure memos are a few minutes long, and what
    is parked is work that costs a fetch or a call to do again.
    """

    def __init__(self) -> None:
        self._master = threading.Lock()
        self._locks: dict = {}
        self.fetch_lock = threading.Lock()
        self.last_fetch = None          # monotonic time of the last page request
        self.blocked_until = 0.0        # no page request before this, after a 429
        self._failures: dict = {}       # key -> (monotonic expiry, text)
        self._parked: dict = {}         # (kind, key) -> (monotonic expiry, value)

    def lock_for(self, item_id: int) -> threading.Lock:
        with self._master:
            return self._locks.setdefault(item_id, threading.Lock())

    def blocked_for(self) -> float:
        """Seconds still to wait before LinkedIn may be asked for anything."""
        return max(0.0, self.blocked_until - _mono())

    def block(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, _mono() + seconds)

    def remember_failure(self, key: tuple, seconds: float, text: str) -> None:
        with self._master:
            self._failures[key] = (_mono() + seconds, text)

    def recent_failure(self, key: tuple):
        """(text, seconds left) of a failure not yet forgotten, else None."""
        with self._master:
            held = self._failures.get(key)
            if held is None:
                return None
            left = held[0] - _mono()
            if left <= 0:
                del self._failures[key]
                return None
            return held[1], left

    def park(self, kind: str, key, value, seconds: float | None = None) -> None:
        """Hold work that was paid for and could not be stored, for the next click."""
        with self._master:
            self._parked[(kind, key)] = (
                _mono() + (PARK_SECONDS if seconds is None else seconds), value)

    def take_parked(self, kind: str, key):
        """The work parked under this key if it is still fresh, taken out, else None."""
        with self._master:
            held = self._parked.pop((kind, key), None)
        if held is None or held[0] <= _mono():
            return None
        return held[1]

    def reset(self) -> None:
        """For tests, which start every scenario with nothing remembered."""
        with self._master:
            self._locks.clear()
            self._failures.clear()
            self._parked.clear()
        self.last_fetch = None
        self.blocked_until = 0.0


GUARD = Guard()


# ------------------------------------------------------- what a row holds

def is_open(row: dict) -> bool:
    """Not acknowledged, closed, dismissed or expired. Applied is judged apart."""
    return not any(row.get(col) for col in
                   ("acknowledged_at", "closed_at", "dismissed_at", "expired_at"))


def load_working(row: dict) -> dict | None:
    """The per-ask working that belongs to the score this row holds, or None.

    The working is stored beside a score and means nothing beside another. It
    is only returned for a row whose score is a spec score and whose working
    carries the very date that score was made, so a writer that has since
    replaced the score, and knows nothing about the working, cannot leave an
    old working looking like the current one. A page also asks whether the
    score was made on the description the row stores now, see
    scored_on_description, before it draws anything from the working. The
    rule is written once, in tailored_cv.master_working, which the tailored
    copy also reads it by.
    """
    return tailored_cv.master_working(row)


def scored_on_description(row: dict) -> bool:
    """Is the score this row holds one made against the description it stores."""
    version = rc.description_version(row.get("description_text"))
    return (version is not None and row.get("score_basis") == SPEC
            and row.get("description_hash") == version)


def needs_score(row: dict) -> bool:
    """Has a description that no score has been made against yet (S9)."""
    return (rc.description_version(row.get("description_text")) is not None
            and not scored_on_description(row))


def job_id_of(row: dict) -> str | None:
    return check_jobs.job_view_id(row.get("source_url") or "")


def pending(row: dict, applied: bool, config: dict | None = None) -> bool:
    """Would a click do anything for this row, fetch or score.

    The page asks this to decide whether its script has a POST to make, and
    whether to offer the button. A role that is applied or not open, one with
    nothing to fetch and no description, and one already scored against its
    description all have nothing to do. Given the config, so that the key of a
    remembered failure can be built, a click that this process would refuse for
    the next few minutes is not pending either, and the page says why instead
    of offering a button that is refused.
    """
    if applied or not is_open(row):
        return False
    description = (row.get("description_text") or "").strip()
    if description:
        if not needs_score(row):
            return False
        return config is None or score_cooling_text(row, description, config) is None
    return fetch_blocked_why(row) is None


def fetch_blocked_why(row: dict) -> str | None:
    """Why the advert's page cannot be asked for just now, else None.

    The advert is not a LinkedIn page, or a page for it could not be used a
    moment ago, or LinkedIn has just said 429. The sentence completes "The full
    description could not be fetched."
    """
    if job_id_of(row) is None:
        return NOT_LINKEDIN_WHY
    held = GUARD.recent_failure(("page", row["id"]))
    if held:
        return (f"{held[0]} That was a moment ago, and it is not asked for "
                f"again for {_minutes(held[1])}.")
    wait = GUARD.blocked_for()
    if wait > 0:
        return ("LinkedIn answered 429, too many requests, a moment ago, so "
                f"Radar is not asking again for {_minutes(wait)}.")
    return None


def score_cooling_text(row: dict, description: str, config: dict,
                       cv_version: str | None = None, who=None) -> str | None:
    """The sentence for a score that failed a moment ago and is not tried
    again yet, else None. It is about this role, this version of its
    description, this prompt and this CV, so any of them changing ends it.
    cv_version names a tailored copy (tailored_cv.copy_version), and is the
    master CV's version when it is not given; who is the copy's role_ref."""
    held = GUARD.recent_failure(_failure_key(row, description, config,
                                             cv_version, who))
    if not held:
        return None
    return (f"The last try for this role failed. {held[0]} "
            f"It is not tried again for {_minutes(held[1])}.")


# ----------------------------------------------------------------- the pay

# The advert's own words for pay that is not a figure. Each is pay only beside
# a pay noun, or in a phrase that is pay in itself, because the bare words turn
# up all over an advert: "a highly competitive market", "non-negotiable",
# "start date negotiable", "a scale depending on experience".
_PAY_NOUN = (r"(?:salary|salaries|rates?|pay|package|packages|compensation"
             r"|remuneration|fees?)")
PAY_WORDS = re.compile(
    r"(?<![\w-])(?:"
    r"competitive\s+(?:salary|rates?|day\s+rates?|pay|package|compensation"
    r"|remuneration)"
    r"|" + _PAY_NOUN + r"\s*(?:is|are|:|-|–)?\s*competitive"
    r"|" + _PAY_NOUN + r"\b[^.\n]{0,30}?\bnegotiable"
    r"|negotiable\s+(?:salary|rates?|day\s+rates?|pay|package)"
    r"|market\s+rate"
    r"|" + _PAY_NOUN + r"\b[^.\n]{0,40}?\b(?:depending|dependent)\s+on\s+experience"
    r")(?![\w-])", re.IGNORECASE)

# What a day, an hour and a year of pay can plausibly be. The page checker's
# reader takes the first sterling figure with a period near it, so a turnover of
# "£50m per year" reads as fifty pounds a year, and is not offered as pay.
_PLAUSIBLE = {"day": (100, 5_000), "hour": (8, 500), "year": (10_000, 1_000_000)}


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _plausible(pay: dict) -> bool:
    low, high = _PLAUSIBLE.get(pay.get("period"), (0, float("inf")))
    return low <= pay["max"] <= high


def _figure_candidate(flat: str, depth: int = 0) -> str | None:
    """The first sterling figure with a period that pay could be, past any that
    cannot be. Recursion, five deep at most, because this module holds no loop."""
    pay = check_jobs.stated_pay(flat)
    if not pay:
        return None
    text = pay["text"]
    if 0 < len(text) <= RATE_QUOTE_MAX and _plausible(pay):
        return text
    at = flat.find(text) if text else -1
    if at < 0 or depth >= 5:
        return None
    return _figure_candidate(flat[at + len(text):], depth + 1)


def _pay_candidate(flat: str) -> str | None:
    """Wording in a description that may be pay, by pattern, short and word for word.

    A sterling figure with its period beside it, through the page checker's own
    reader, if it is a figure a day, an hour or a year can be, else one of the
    advert's own pay words beside a pay noun. It cannot tell a salary from a
    bonus or a budget, which is why it only ever speaks beside the scorer.
    """
    figure = _figure_candidate(flat)
    if figure:
        return figure
    words = PAY_WORDS.search(flat)
    return words.group(0) if words else None


def rate_line(description: str, model_quote: str | None) -> dict:
    """What the description says about pay, short and word for word (J21).

    Returns {"quote": text or None, "from": where}. The model is asked for the
    shortest stretch of the advert that states pay, and nothing it writes is
    shown unless that stretch is found in the description, which makes the
    quote the advert's own words: what is returned is the description's
    characters, found without regard to case and with runs of white space
    folded, from model. A quote that is not in the description, or is not
    short, is not shown, and the pattern reader takes a turn over the
    description instead, from pattern, which is word for word by construction.
    Where that finds nothing either, the model said there was pay and the
    advert cannot be made to show it, from unverified.

    A reply that names none is not taken on its word alone, because "the
    description names no pay" is the claim most worth being right about. The
    pattern reader looks too. If it finds wording that may be pay the page
    shows it as a disagreement, from disagrees, and says it may not be pay.
    Only when both find nothing is the answer none. The floors the pay is
    judged against are not here, they are read from the preferences file where
    the gate is made.
    """
    quote = _flat(model_quote)
    flat = _flat(description)
    if not quote:
        candidate = _pay_candidate(flat)
        if candidate:
            return {"quote": candidate, "from": "disagrees"}
        return {"quote": None, "from": "none"}
    if len(quote) <= RATE_QUOTE_MAX:
        found = re.search(re.escape(quote), flat, re.IGNORECASE)
        if found:
            return {"quote": found.group(0), "from": "model"}
    candidate = _pay_candidate(flat)
    if candidate:
        return {"quote": candidate, "from": "pattern"}
    return {"quote": None, "from": "unverified"}


# ------------------------------------------------------------ the notices

def _notice(out: dict, kind: str, text: str, retry: bool = False) -> None:
    out["notices"].append({"kind": kind, "text": text, "retry": retry})


def _minutes(seconds: float) -> str:
    """A wait in words: seconds under a minute and a half, else whole minutes."""
    if seconds < 90:
        whole = max(1, int(round(seconds)))
        return f"{whole} second" + ("" if whole == 1 else "s")
    whole = int(round(seconds / 60.0))
    return f"{whole} minute" + ("" if whole == 1 else "s")


NOT_LINKEDIN_WHY = ("This advert's link is not a LinkedIn job page, and "
                    "LinkedIn job pages are the only ones Radar reads.")

APPLIED_NOTE = ("This role is marked applied. Prepare application shows what "
                "is stored and fetches, scores and writes nothing for it.")
NOT_OPEN_NOTE = ("This role is not open, it has been acknowledged, closed, "
                 "dismissed or retired. Prepare application shows what is "
                 "stored and fetches, scores and writes nothing for it.")


def fetch_failed_text(why: str) -> str:
    """The sentence a page says when the full description is not to be had."""
    return (f"The full description could not be fetched. {why} The score "
            "shown is the one made from the advert card.")


def _fetch_failed(out: dict, why: str, retry: bool = False) -> None:
    """A page that was not had. Try again is only offered where trying again
    could work, which is a network that was down and not much else: a 429 and a
    page that could not be read are not asked for again for minutes."""
    out["outcome"] = "no_description"
    _notice(out, "fetch", fetch_failed_text(why), retry)


def _refuse(out: dict, why: str, during: str) -> None:
    """The role changed while this click was working. Said, and nothing stored."""
    if why == "applied":
        out["outcome"] = "applied"
        _notice(out, "info", f"This role was marked applied while {during}, so "
                "nothing was stored. " + APPLIED_NOTE)
    elif why == "not_open":
        out["outcome"] = "not_open"
        _notice(out, "info", f"This role was acknowledged, closed, dismissed or "
                f"retired while {during}, so nothing was stored. " + NOT_OPEN_NOTE)
    else:
        out["outcome"] = "score_failed"
        _notice(out, "info", f"This role was no longer there while {during}, "
                "so nothing was stored.")


# ------------------------------------------------------- the write helpers

def _begin_write(conn) -> bool:
    """Take the write lock, waiting for another process's. False if it stayed busy.

    The inbox run holds the lock for the whole of an email, so the default five
    second wait is too short for a click that lands in the middle of one.
    """
    try:
        conn.execute(f"PRAGMA busy_timeout={int(WRITE_WAIT_SECONDS * 1000)}")
        rc.begin_write(conn)
        return True
    except sqlite3.OperationalError:
        return False


def _fresh_open_row(conn, item_id: int):
    """Under the write lock: the row as it is now, and why it may not be written.

    Returns (row or None, None | "applied" | "not_open" | "gone"). The
    statement carries the applied condition, so an applied role is never
    returned however it got there.
    """
    raw = conn.execute("SELECT * FROM opportunities WHERE id = ? AND "
                       + rc.NOT_APPLIED_SQL, (item_id,)).fetchone()
    if raw is None:
        return None, ("applied" if rc.role_is_applied(conn, item_id)
                      else "gone")
    row = dict(raw)
    return row, (None if is_open(row) else "not_open")


def _log_run(conn, usage: dict, config: dict, stored: bool, note: str,
             commit: bool = True) -> None:
    """One row in runs for a model call, so the week's cost counts it.

    With commit False the row joins the transaction the caller is in, so that a
    score and the record of what it cost are stored together or not at all.
    """
    if not usage:
        return
    try:
        conn.execute(
            "INSERT INTO runs (ts, workflow, mode, items_in, items_new, model,"
            " input_tokens, output_tokens, cache_read_tokens, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rc.now_iso(), "prepare",
             "mock" if rc.mock_mode_active() else "live", 1, 1 if stored else 0,
             usage.get("model") or config.get("claude_model_score"),
             usage.get("input_tokens", 0), usage.get("output_tokens", 0),
             usage.get("cache_read_tokens", 0), note))
        if commit:
            conn.commit()
    except Exception as err:  # noqa: BLE001  a log line never costs the result
        print(f"prepare: the run record was not written ({type(err).__name__})",
              file=sys.stderr)


# ----------------------------------------------------------- the one fetch

def _fetch_page(row: dict, out: dict):
    """Ask LinkedIn for the advert's page once. (description, fetched_at) or None.

    None means a notice has said why. Everything that stops this from asking,
    and everything that goes wrong after it has, is told to the page.
    """
    why = fetch_blocked_why(row)
    if why:
        _fetch_failed(out, why)
        return None
    job_id = job_id_of(row)
    key = ("page", row["id"])
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    with GUARD.fetch_lock:
        # The checker spaces its requests 8 to 12 seconds apart. Clicks on
        # two roles in quick succession are spaced the same way.
        if GUARD.last_fetch is not None:
            _sleep(check_jobs.SPACING_SECONDS[0] - (_mono() - GUARD.last_fetch))
        if GUARD.blocked_for() > 0:       # a 429 came in while this waited
            _fetch_failed(out, "LinkedIn answered 429, too many requests, a "
                               "moment ago, so Radar is not asking again for "
                               f"{_minutes(GUARD.blocked_for())}.")
            return None
        out["fetched"] = True
        try:
            status, final_url, body = check_jobs.fetch_job_page(url)
        except Exception as err:  # noqa: BLE001  never a 500 for a fetch
            status, final_url, body = 0, url, ""
            print(f"prepare: the fetch for id {row['id']} raised "
                  f"{type(err).__name__}", file=sys.stderr)
        GUARD.last_fetch = _mono()
        if status == 429:
            GUARD.block(check_jobs.BACKOFF_SECONDS)
    if status == 0:
        _fetch_failed(out, "Radar could not reach LinkedIn from this machine.",
                      retry=True)
        return None
    if status == 429:
        _fetch_failed(out, "LinkedIn answered 429, too many requests. Nothing "
                           "was stored, and Radar will not ask again for "
                           f"{_minutes(check_jobs.BACKOFF_SECONDS)}.")
        return None

    def cannot(why: str):
        """A page a second try would give again, so it is not asked for again."""
        GUARD.remember_failure(key, PAGE_FAILURE_COOLDOWN_SECONDS, why)
        _fetch_failed(out, why)
        return None

    verdict, reason = check_jobs.classify(status, final_url, body,
                                          row.get("title"), job_id)
    if verdict == "gone":
        return cannot("LinkedIn sent the link to its expired job page, so the "
                      "advert looks gone.")
    if verdict == "closed":
        return cannot("LinkedIn shows this advert as closed. Radar has not "
                      "closed the role in its own records, use Closed on the "
                      "Jobs row if you agree.")
    if verdict != "live":
        if "authwall" in reason:
            return cannot("LinkedIn asked for a login, and Radar never logs in.")
        return cannot("LinkedIn served a page Radar could not match to this "
                      f"advert ({reason}).")
    description = check_jobs.read_page(body).get("description") or ""
    if not description.strip():
        return cannot("The page was served but carried no description text.")
    return description, rc.now_iso()


def _store_description(conn, row: dict, description: str, fetched_at: str,
                       out: dict) -> str:
    """Store a description that was read, if the role is still open to it.

    The write lock first, then look again: the page took a while to come, and a
    role can be applied for or dismissed in that time. An applied role keeps
    what it has, and a description stored by someone else in the meantime
    stays. If the database stays busy the page is parked, so that the next click
    stores it without asking LinkedIn again. Returns the description stored, or
    "" with a notice said.
    """
    if not _begin_write(conn):
        GUARD.park("page", row["id"], (description, fetched_at))
        out["outcome"] = "no_description"
        _notice(out, "fetch",
                "The full description was read from LinkedIn, but it could not "
                "be stored yet because the database was busy. It is held in "
                "memory, and Try again stores it without asking LinkedIn "
                "again. The score shown is the one made from the advert card.",
                retry=True)
        return ""
    _current, why = _fresh_open_row(conn, row["id"])
    if why:
        conn.rollback()
        _refuse(out, why, "the page was being fetched")
        return ""
    conn.execute(
        "UPDATE opportunities SET description_text = ?,"
        " description_fetched_at = ? WHERE id = ? AND"
        " (description_text IS NULL OR TRIM(description_text) = '') AND "
        + rc.NOT_APPLIED_SQL, (description, fetched_at, row["id"]))
    conn.commit()
    stored = conn.execute("SELECT description_text FROM opportunities"
                          " WHERE id = ?", (row["id"],)).fetchone()
    return (stored["description_text"] or "") if stored else ""


def _fetch_and_store(conn, row: dict, out: dict) -> str:
    """Get the description of a role that stores none, and store it. "" if not.

    A page parked by an earlier click that could not store it is used before
    LinkedIn is asked for another.
    """
    got = GUARD.take_parked("page", row["id"])
    if got is None:
        got = _fetch_page(row, out)
    if got is None:
        return ""
    return _store_description(conn, row, got[0], got[1], out)


# ------------------------------------------------------------- the score

def _spec_opportunity(row: dict, description: str) -> dict:
    """What the scorer is shown: the card fields as stored, and the advert."""
    return {
        "company": row.get("company") or "",
        "title": row.get("title") or "",
        "location": row.get("location") or "",
        "salary_rate": row.get("salary_rate") or "",
        "source_url": row.get("source_url") or "",
        # The stored pay columns ride along so the code gate judges a figure
        # against its floor, exactly as on first arrival.
        "pay_currency": row.get("pay_currency"),
        "pay_period": row.get("pay_period"),
        "pay_min": row.get("pay_min"), "pay_max": row.get("pay_max"),
        "basis": SPEC,
        "description": description,
    }


def _failure_key(row: dict, description: str, config: dict,
                 cv_version: str | None = None, who=None) -> tuple:
    """What a remembered failure, or parked work, is about: this role, this
    version of its description, this rubric and this CV. Change any of them and
    it is a new question, which a person may ask at once. A tailored copy is
    named by its own version, so an edit to it is a new question too, and is
    about the role's copy (who, its role_ref) and not the advert, because every
    advert of a repost chain shares that copy and so shares what it failed."""
    try:
        return (row["id"] if who is None else who,
                rc.description_version(description), rc.prompt_version(),
                cv_version or score_item.get_cv_version(config))
    except Exception:  # noqa: BLE001  a key that cannot be built is a short one
        return (row["id"] if who is None else who,
                rc.description_version(description))


def _store_score(conn, row: dict, payload: dict, config: dict, key: tuple,
                 out: dict) -> None:
    """Store a score that was paid for, if the role is still open to one.

    The write lock first, then look again: the role may have been applied for
    or dismissed while the model answered. It keeps what it had, and the reply
    that was paid for is not stored, but its cost is still recorded. If the
    database stays busy the reply is parked, so that the next click stores it
    without a second call. The score, its history and the record of what it
    cost are one transaction.
    """
    scored, usage = payload["scored"], payload["usage"]
    working = scored["working"]
    if not _begin_write(conn):
        GUARD.park("score", key, payload)
        out["outcome"] = "score_held"
        _notice(out, "score",
                "The scorer answered, but the score could not be stored yet "
                "because the database was busy. It is held in memory, and Try "
                "again stores it without another call.", retry=True)
        return
    current, why = _fresh_open_row(conn, row["id"])
    if why:
        _log_run(conn, usage, config, False,
                 f"id {row['id']}, "
                 + ("applied" if why == "applied" else "no longer open")
                 + " while scoring, not stored")
        _refuse(out, why, "the scorer was answering")
        return
    old_tier = current["tier"]
    rc.archive_score(conn, row["id"], "prepare")
    params = ((scored["cv_match"], scored["one_line_why"],
               json.dumps(scored["red_flags"]), scored["suggested_action"],
               scored["act_by"], scored["thread_type"],
               payload.get("cv_version"),
               json.dumps(working, ensure_ascii=False))
              + tuple(scored.get(c) for c in GATE_COLS + RECORD_COLS)
              + (row["id"],))
    cur = conn.execute(
        "UPDATE opportunities SET cv_match = ?, one_line_why = ?,"
        " red_flags = ?, suggested_action = ?, act_by = ?, thread_type = ?,"
        " notes = NULL, cv_version = ?, score_working = ?, "
        + ", ".join(f"{c} = ?" for c in GATE_COLS + RECORD_COLS)
        + " WHERE id = ? AND " + rc.NOT_APPLIED_SQL, params)
    if not cur.rowcount:
        # Nothing was written, so the history row kept above goes with it.
        conn.rollback()
        _log_run(conn, usage, config, False,
                 f"id {row['id']}, the row was not there to write, not stored")
        out["outcome"] = "score_failed"
        _notice(out, "score", "The score could not be stored, the role is "
                "gone or has been applied for. Nothing was changed.")
        return
    moved = old_tier if (old_tier and old_tier != scored["tier"]) else None
    rc.record_score(conn, row["id"], "prepare", tier_from=moved)
    _log_run(conn, usage, config, True,
             f"id {row['id']}, {len(working['asks'])} asks"
             + (f", tier {old_tier} to {scored['tier']}" if moved else ""),
             commit=False)
    conn.commit()
    out["outcome"] = "scored"
    out["scored"] = True
    if moved:
        out["tier_move"] = {"from": moved, "to": scored["tier"]}


# What a failed score says, for the master CV and for a tailored copy.
_MASTER_WORDS = {
    "setup": ("The description was fetched but could not be scored, the "
              "scorer could not be set up."),
    "raised": ("The description was fetched but the scorer could not be "
               "reached ({name}). No score was stored."),
    "unusable": ("The scorer's reply could not be used ({why}). Nothing was "
                 "stored for the score."),
}
_COPY_WORDS = {
    "setup": ("The tailored copy could not be scored, the scorer could not "
              "be set up."),
    "raised": ("The scorer could not be reached ({name}), so the tailored "
               "copy was not scored. No score was stored."),
    "unusable": ("The scorer's reply could not be used ({why}). Nothing was "
                 "stored for the tailored copy."),
}


def _score(conn, row: dict, description: str, config: dict, out: dict,
           tailored: dict | None = None) -> None:
    """Score the role against its description. One model call, no more.

    tailored is None to score the master CV and store the role's own score.
    For Rescore role it is the copy, {"ref", "text", "hash"}, which is scored
    through every step below in place of the master, and stored on the copy
    by _store_tailored and never on the role.
    """
    cv_version = (tailored_cv.copy_version(tailored["hash"]) if tailored
                  else None)
    who = tailored["ref"] if tailored else None
    words = _COPY_WORDS if tailored else _MASTER_WORDS
    store = _store_tailored if tailored else _store_score
    what = (f"id {row['id']}, tailored copy {tailored['hash']}, " if tailored
            else f"id {row['id']}, ")
    key = _failure_key(row, description, config, cv_version, who)
    # A reply paid for on an earlier click that the database was too busy to
    # take is stored first, and no call is made.
    parked = GUARD.take_parked("score", key)
    if parked is not None:
        store(conn, row, parked, config, key, out)
        return
    cooling = score_cooling_text(row, description, config, cv_version, who)
    if cooling:
        out["outcome"] = "cooling"
        _notice(out, "score", cooling, retry=False)
        return
    try:
        blocks = score_item.build_scorer_system(
            config, basis=SPEC, cv=None if tailored is None else {
                "text": tailored["text"], "version": cv_version,
                "label": tailored_cv.copy_source(tailored["hash"])})
        floors = rc.read_rate_floors(config)
        thresholds = rc.read_score_thresholds(config)
    except (Exception, SystemExit) as err:  # noqa: BLE001  no CV, no floor line
        text = f"{words['setup']} {str(err)[:300]}"
        GUARD.remember_failure(key, ERROR_COOLDOWN_SECONDS, text)
        out["outcome"] = "score_failed"
        # No Try again, because the next click is refused for a while. The page
        # offers the button again once the wait is over.
        _notice(out, "score", text + " It is not tried again for "
                f"{_minutes(ERROR_COOLDOWN_SECONDS)}.", retry=False)
        return
    _unused, score_fn = score_item.get_mock_fns()
    opp = _spec_opportunity(row, description)
    try:
        scored, usage, note = score_item.score_opportunity(
            opp, blocks, config, mock_fn=score_fn, floors=floors,
            thresholds=thresholds, basis=SPEC)
    except Exception as err:  # noqa: BLE001  no key, no network, an API error
        text = words["raised"].format(name=type(err).__name__)
        GUARD.remember_failure(key, ERROR_COOLDOWN_SECONDS, text)
        print(f"prepare: the scorer raised {type(err).__name__} for id "
              f"{row['id']}: {str(err)[:200]}", file=sys.stderr)
        out["outcome"] = "score_failed"
        _notice(out, "score", text + " It is not tried again for "
                f"{_minutes(ERROR_COOLDOWN_SECONDS)}.", retry=False)
        return
    out["called"] = bool(usage)
    if scored is None or note or scored.get("tier") is None:
        text = words["unusable"].format(why=note or "no tier came back")
        GUARD.remember_failure(key, UNUSABLE_COOLDOWN_SECONDS, text)
        # What the reply cost is recorded, which can wait for the lock as long
        # as a score can. If the database stays busy the cost is lost, and said
        # so on stderr.
        if _begin_write(conn):
            _log_run(conn, usage, config, False, f"{what}not stored, "
                     f"{note or 'no tier'}")
        else:
            print("prepare: the run record was not written, the database "
                  "stayed busy", file=sys.stderr)
        out["outcome"] = "score_failed"
        # This click paid for the reply, and says so. The memory above does
        # not, because a later click that reads it paid for nothing. No Try
        # again either: the next click is refused until the wait is over.
        _notice(out, "score", text + " It is not tried again for "
                f"{_minutes(UNUSABLE_COOLDOWN_SECONDS)}. The reply was paid for.",
                retry=False)
        return

    working = scored["working"]
    line = rate_line(description, scored.get("rate_quote_raw"))
    working["rate_quote"], working["rate_quote_from"] = line["quote"], line["from"]
    payload = {"scored": scored, "usage": usage,
               "cv_version": getattr(blocks, "cv_version", None)}
    if tailored:
        payload["tailored"] = tailored
    store(conn, row, payload, config, key, out)


# ------------------------------------------------------------ the click

def prepare(item_id: int, db_path: Path | None = None,
            config: dict | None = None) -> dict:
    """Everything one click on Prepare application does. See the module note.

    Returns {"ok": True, "outcome": ..., "fetched": bool, "called": bool (a
    model call was made and answered), "scored": bool (and stored),
    "tier_move": {from, to} or None, "notices": [...]}. The
    outcome is scored, reused (the score for this version of the description
    was already stored), no_description (it could not be had or stored),
    score_failed, score_held (paid for, parked for the next click), cooling (a
    failure still remembered), applied, not_open or busy. A role
    that does not exist is {"ok": False, "note": ...}. A database that stays
    locked while it is opened raises sqlite3.OperationalError, for the route to
    answer politely.
    """
    out: dict = {"ok": True, "id": item_id, "outcome": None, "fetched": False,
                 "called": False, "scored": False, "tier_move": None,
                 "notices": []}
    lock = GUARD.lock_for(item_id)
    if not lock.acquire(timeout=LOCK_WAIT_SECONDS):
        out["outcome"] = "busy"
        _notice(out, "info", "Another click on this role is still working. "
                "Wait for it to finish, then reload this page.")
        return out
    try:
        conn = rc.get_db(db_path)
        try:
            return _prepare_locked(conn, item_id, out, config)
        finally:
            conn.close()
    finally:
        lock.release()


def _prepare_locked(conn, item_id: int, out: dict, config: dict | None) -> dict:
    raw = conn.execute("SELECT * FROM opportunities WHERE id = ?",
                       (item_id,)).fetchone()
    if raw is None:
        return {"ok": False, "note": f"no opportunity {item_id}"}
    row = dict(raw)
    if rc.role_is_applied(conn, item_id):
        out["outcome"] = "applied"
        _notice(out, "info", APPLIED_NOTE)
        return out
    if not is_open(row):
        out["outcome"] = "not_open"
        _notice(out, "info", NOT_OPEN_NOTE)
        return out
    config = config or rc.load_config()
    description = (row.get("description_text") or "").strip()
    if not description:
        description = _fetch_and_store(conn, row, out).strip()
        if not description:
            out["outcome"] = out["outcome"] or "no_description"
            return out
        row = dict(conn.execute("SELECT * FROM opportunities WHERE id = ?",
                                (item_id,)).fetchone())
    if not needs_score(row):
        out["outcome"] = "reused"
        return out
    _score(conn, row, description, config, out)
    return out


# --------------------------------------------------------- the tailored CV

COPY_APPLIED_NOTE = ("This role is marked applied. Its CV is shown as it was "
                     "stored, and nothing is scored, saved or changed for it.")
COPY_NOT_OPEN_NOTE = ("This role is not open, it has been acknowledged, closed, "
                      "dismissed or retired. Its CV is shown as it was stored, "
                      "and nothing is scored, saved or changed for it.")
NO_DESCRIPTION_FOR_COPY = (
    "There is no full description stored for this role, so there is nothing "
    "to score the copy against and nothing was spent. Fetch and score the "
    "description first.")
COPY_BUSY = ("The database was busy, so nothing was stored{also}. Nothing on "
             "the page was lost, try again in a moment.")
MASTER_NOT_LAID_OUT = (
    "The master CV on file is not in the editor's layout yet: it was imported, "
    "or written by hand, and has not been saved on the CV page. A copy made "
    "from it would number its lines differently from the score the master "
    "was given, so no copy is made from it. Open the CV page, check how the "
    "master reads there, and save it once.")
COPY_MOVED = ("The copy was changed in another tab or window after this page "
              "took it, so nothing was stored and nothing was spent. Reload the "
              "page to see the copy as it is now.")
UNCHANGED_COPY = (
    "This copy is the master CV as it stands, and the role has no score of its "
    "own on this description yet. That score is the role's, and the button "
    "under the description makes it, once, whenever the page offers it. "
    "Rescore role scores the copy once it differs from the master. Nothing was "
    "spent.")


def summary(record: dict | None) -> dict | None:
    """What the page script needs of a tailored score to say what moved."""
    if not record:
        return None
    before = record.get("before") or {}
    return {"cv_match": record.get("cv_match"), "tier": record.get("tier"),
            "before_cv_match": before.get("cv_match"),
            "before_tier": before.get("tier"),
            "before_source": before.get("source"),
            "changes": len(record.get("changes") or [])}


def _copy_shut(conn, row: dict, out: dict) -> bool:
    """Say so, and True, when the copy of this role may not be changed."""
    if rc.role_is_applied(conn, row["id"]):
        out["outcome"] = "applied"
        _notice(out, "info", COPY_APPLIED_NOTE)
        return True
    if not is_open(row):
        out["outcome"] = "not_open"
        _notice(out, "info", COPY_NOT_OPEN_NOTE)
        return True
    return False


def _copy_write(conn, row: dict, out: dict, during: str, also: str = ""):
    """Take the write lock and look at the role again under it.

    Returns the role as it is now, or None with the reason said: the
    database stayed busy, or the role was applied for or closed in the
    meantime.
    """
    if not _begin_write(conn):
        out["outcome"] = "db_busy"
        _notice(out, "info", COPY_BUSY.format(also=also))
        return None
    current, why = _fresh_open_row(conn, row["id"])
    if why:
        conn.rollback()
        _refuse(out, why, during)
        return None
    return current


def _with_copy_lock(item_id: int, db_path, work, row_lock: bool = False) -> dict:
    """Run work(conn, row, out) holding the lock of the role's copy.

    The lock is the role's, not the row's, because the adverts of a repost
    chain share one copy, so two tabs on two adverts of one role wait for each
    other like two clicks on one. With row_lock, the lock of the Prepare
    application click on this advert is taken as well, after the copy's, so a
    Rescore role pressed while that click is still scoring the master waits
    for it, and then finds its score. The click never takes the copy's lock,
    so the two cannot wait on each other. A role that does not exist is
    {"ok": False, "note": ...}.
    """
    out: dict = {"ok": True, "id": item_id, "outcome": None, "called": False,
                 "scored": False, "notices": [], "copy": None, "tailored": None}
    probe = rc.get_db(db_path)
    try:
        raw = probe.execute("SELECT * FROM opportunities WHERE id = ?",
                            (item_id,)).fetchone()
    finally:
        probe.close()
    if raw is None:
        return {"ok": False, "note": f"no opportunity {item_id}"}
    copy_lock = GUARD.lock_for(("cv", tailored_cv.role_ref(raw)))
    click_lock = GUARD.lock_for(item_id) if row_lock else None
    if not copy_lock.acquire(timeout=LOCK_WAIT_SECONDS):
        out["outcome"] = "busy"
        _notice(out, "info", "Another click on this role's CV is still "
                "working. Wait for it to finish, then try again.")
        return out
    try:
        if click_lock is not None and not click_lock.acquire(timeout=LOCK_WAIT_SECONDS):
            out["outcome"] = "busy"
            _notice(out, "info", "The Prepare application click on this role "
                    "is still working. Wait for it to finish, then try again.")
            return out
        try:
            conn = rc.get_db(db_path)
            try:
                raw = conn.execute("SELECT * FROM opportunities WHERE id = ?",
                                   (item_id,)).fetchone()
                if raw is None:
                    return {"ok": False, "note": f"no opportunity {item_id}"}
                return work(conn, dict(raw), out)
            finally:
                conn.close()
        finally:
            if click_lock is not None:
                click_lock.release()
    finally:
        copy_lock.release()


def _master_version(config: dict) -> str | None:
    try:
        return score_item.get_cv_version(config)
    except Exception:  # noqa: BLE001  the copy is kept either way
        return None


def open_copy(item_id: int, db_path: Path | None = None,
              config: dict | None = None) -> dict:
    """Make the role's copy from the master, the first time its page opens.

    No model call. A role with a copy keeps it and gets it back. An applied
    or closed role gets no copy, and the page shows what it has. A master that
    is not in the editor's layout gives no copy either (MASTER_NOT_LAID_OUT),
    because the copy's line numbers would not be the ones the master's own
    score cites.
    """
    def work(conn, row, out):
        if _copy_shut(conn, row, out):
            return out
        ref = tailored_cv.role_ref(row)
        copy = tailored_cv.get(conn, ref)
        if copy is None:
            try:
                start = tailored_cv.master(config or rc.load_config())
            except cv_doc.CvError as err:
                out["outcome"] = "no_master"
                _notice(out, "info", "The master CV could not be read, so no "
                        f"tailored copy was made. {err}")
                return out
            if not start["canonical"]:
                out["outcome"] = "master_not_laid_out"
                _notice(out, "info", MASTER_NOT_LAID_OUT)
                return out
            current = _copy_write(conn, row, out, "the copy was being made",
                                  ", the copy is made by the first Rescore "
                                  "role or Save CV instead")
            if current is None:
                return out
            made = tailored_cv.create(conn, ref, current["id"], start["text"],
                                      start["version"])
            conn.commit()
            copy = tailored_cv.get(conn, ref)
            out["outcome"] = "made" if made else "found"
        else:
            out["outcome"] = "found"
        out["copy"] = tailored_cv.view(copy)
        return out
    return _with_copy_lock(item_id, db_path, work)


def _moved(conn, ref: str, base, out: dict) -> bool:
    """Under the write lock: has the copy moved on from the one the page took.

    base is the cv_hash of the copy the page last took, sent with Rescore role
    and Save CV. A copy that another tab or window changed since is not
    written over without a word: nothing is stored or spent, and the page is
    told to reload. A page that sends no base is not checked, and neither is
    a role with no copy yet.
    """
    if base is None:
        return False
    copy = tailored_cv.get(conn, ref)
    if copy is None or copy.get("cv_hash") == base:
        return False
    conn.rollback()
    out["outcome"] = "copy_moved"
    _notice(out, "info", COPY_MOVED)
    return True


def save_copy(item_id: int, text, db_path: Path | None = None,
              config: dict | None = None, base=None) -> dict:
    """Save CV: store the copy on screen as the role's saved copy.

    It replaces the copy saved before for this role, and only a save does.
    The name is the one the brief gives, made safe for a file name. A CV that
    cannot be taken is {"ok": False, "invalid": True, "note": why}. base is
    the copy the page took, see _moved.
    """
    try:
        _doc, clean_text = cv_doc.accept(text)
    except cv_doc.CvError as err:
        return {"ok": False, "invalid": True, "note": str(err)}

    def work(conn, row, out):
        if _copy_shut(conn, row, out):
            return out
        current = _copy_write(conn, row, out, "the CV was being saved")
        if current is None:
            return out
        ref = tailored_cv.role_ref(current)
        if _moved(conn, ref, base, out):
            return out
        if tailored_cv.get(conn, ref) is None:
            tailored_cv.create(conn, ref, current["id"], clean_text,
                               _master_version(config or rc.load_config()))
        name = cv_doc.saved_name(current.get("company"), current.get("title"))
        tailored_cv.save(conn, ref, clean_text, name)
        conn.commit()
        out["outcome"] = "saved"
        out["saved_name"] = name
        out["copy"] = tailored_cv.view(tailored_cv.get(conn, ref))
        return out
    return _with_copy_lock(item_id, db_path, work)


def reset_copy(item_id: int, db_path: Path | None = None,
               config: dict | None = None) -> dict:
    """Start again from master: the copy becomes the master as it is now.

    The saved copy and the tailored scores are kept, because they are
    records of what was saved and what was scored. A master that is not in
    the editor's layout is not put on the copy, as open_copy says.
    """
    def work(conn, row, out):
        if _copy_shut(conn, row, out):
            return out
        try:
            start = tailored_cv.master(config or rc.load_config())
        except cv_doc.CvError as err:
            out["outcome"] = "no_master"
            _notice(out, "info", f"The master CV could not be read. {err}")
            return out
        if not start["canonical"]:
            out["outcome"] = "master_not_laid_out"
            _notice(out, "info", MASTER_NOT_LAID_OUT)
            return out
        current = _copy_write(conn, row, out, "the copy was being reset")
        if current is None:
            return out
        ref = tailored_cv.role_ref(current)
        if tailored_cv.get(conn, ref) is None:
            tailored_cv.create(conn, ref, current["id"], start["text"],
                               start["version"])
        else:
            tailored_cv.restart(conn, ref, start["text"], start["version"])
        conn.commit()
        out["outcome"] = "reset"
        out["copy"] = tailored_cv.view(tailored_cv.get(conn, ref))
        return out
    return _with_copy_lock(item_id, db_path, work)


def rescore_copy(item_id: int, text, db_path: Path | None = None,
                 config: dict | None = None, base=None) -> dict:
    """Rescore role: score the copy on screen against the stored description.

    The text on screen is stored as the copy first, so a reload shows what
    was scored, and then the pair of description and copy is scored once.
    A pair scored before, on this copy, from the history, or as the master CV
    itself, is shown again and nothing is spent. A copy that is the master as
    it stands, for a role whose own score is not on this description yet, is
    not scored here: that is the role's own score, which the Prepare
    application click makes. Otherwise one model call through _score, with
    every guard the click has, its lock among them. base is the copy the page
    took, see _moved.
    """
    try:
        _doc, clean_text = cv_doc.accept(text)
    except cv_doc.CvError as err:
        return {"ok": False, "invalid": True, "note": str(err)}

    def work(conn, row, out):
        if _copy_shut(conn, row, out):
            return out
        if not (row.get("description_text") or "").strip():
            out["outcome"] = "no_description"
            _notice(out, "info", NO_DESCRIPTION_FOR_COPY)
            return out
        cfg = config or rc.load_config()
        current = _copy_write(conn, row, out, "the copy was being stored",
                              " and nothing was spent")
        if current is None:
            return out
        # The description as it is now, under the lock, which is the one the
        # score is made against and the one its version is taken from.
        description = (current.get("description_text") or "").strip()
        if not description:
            conn.rollback()
            out["outcome"] = "no_description"
            _notice(out, "info", NO_DESCRIPTION_FOR_COPY)
            return out
        ref = tailored_cv.role_ref(current)
        if _moved(conn, ref, base, out):
            return out
        # Looked for first, under the write lock, so a copy that is there is
        # only ever updated: an insert that is ignored still moves the
        # table's id counter on.
        if tailored_cv.get(conn, ref) is None:
            tailored_cv.create(conn, ref, current["id"], clean_text,
                               _master_version(cfg))
        else:
            tailored_cv.store_text(conn, ref, clean_text)
        conn.commit()
        copy = tailored_cv.get(conn, ref)
        out["copy"] = tailored_cv.view(copy)
        desc_hash = rc.description_version(description)
        cv_hash = copy["cv_hash"]
        standing = tailored_cv.standing(current, copy, cv_hash,
                                        tailored_cv.scored_master_hash(current, cfg))
        if standing["kind"] in ("current", "master"):
            out["outcome"] = "reused"
            out["tailored"] = summary(standing["record"])
            _notice(out, "info", (
                "This copy has been scored against this description already, "
                "so nothing was spent." if standing["kind"] == "current" else
                "This copy is the master CV as it was scored against this "
                "description, so that score stands for it and nothing was "
                "spent."))
            return out
        found = tailored_cv.history_pair(conn, current, desc_hash, cv_hash)
        if found is not None:
            again = _copy_write(conn, current, out, "the score was being found")
            if again is None:
                return out
            record = tailored_cv.from_history(again, copy, found, desc_hash,
                                              cv_hash)
            tailored_cv.set_score(conn, ref, record)
            conn.commit()
            out["outcome"] = "reused"
            out["tailored"] = summary(record)
            _notice(out, "info", "This copy was scored against this "
                    "description before, so that score is shown again and "
                    "nothing was spent.")
            return out
        if _is_master_unscored(current, cv_hash, cfg):
            out["outcome"] = "unchanged_copy"
            _notice(out, "info", UNCHANGED_COPY)
            return out
        _score(conn, current, description, cfg, out,
               tailored={"ref": ref, "text": clean_text, "hash": cv_hash})
        return out
    return _with_copy_lock(item_id, db_path, work, row_lock=True)


def _is_master_unscored(row: dict, cv_hash: str, config: dict) -> bool:
    """Is the copy the master as it stands, for a role whose own score is not
    yet on its stored description. That pair is the role's own question, and
    the click under the description answers it, once."""
    try:
        start = tailored_cv.master(config)
    except cv_doc.CvError:
        return False
    return (start["canonical"] and cv_store.cv_hash(start["text"]) == cv_hash
            and not scored_on_description(row))


def _store_tailored(conn, row: dict, payload: dict, config: dict, key: tuple,
                    out: dict) -> None:
    """Store a tailored score that was paid for, on the copy and in the history.

    The same manners as _store_score: the write lock first, then look at the
    role again, the reply parked if the database stays busy, and the score,
    its history row and its cost one transaction. The role's own score, the
    one the Jobs page shows, is not read for writing and not written.
    """
    scored, usage, target = payload["scored"], payload["usage"], payload["tailored"]
    working = scored["working"]
    if not _begin_write(conn):
        GUARD.park("score", key, payload)
        out["outcome"] = "score_held"
        _notice(out, "score",
                "The scorer answered, but the tailored score could not be "
                "stored yet because the database was busy. It is held in "
                "memory, and Rescore role on the same copy stores it without "
                "another call.", retry=False)
        return
    current, why = _fresh_open_row(conn, row["id"])
    what = f"id {row['id']}, tailored copy {target['hash']}, "
    if why:
        _log_run(conn, usage, config, False, what
                 + ("applied" if why == "applied" else "no longer open")
                 + " while scoring, not stored")
        _refuse(out, why, "the scorer was answering")
        return
    copy = tailored_cv.get(conn, target["ref"])
    if copy is None:
        conn.rollback()
        _log_run(conn, usage, config, False, what + "the copy was not there, "
                 "not stored")
        out["outcome"] = "score_failed"
        _notice(out, "score", "The tailored score could not be stored, the "
                "copy was not there. Nothing was changed.")
        return
    record = tailored_cv.from_scored(current, copy, scored, working,
                                     scored["description_hash"], target["hash"])
    tailored_cv.set_score(conn, target["ref"], record)
    rc.record_tailored_score(conn, row["id"], tailored_cv.history_state(record),
                             target["hash"])
    _log_run(conn, usage, config, True,
             what + f"{len(working['asks'])} asks", commit=False)
    conn.commit()
    out["outcome"] = "scored"
    out["scored"] = True
    out["tailored"] = summary(record)
