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
import score_item  # noqa: E402

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
    scored_on_description, before it draws anything from the working.
    """
    raw = row.get("score_working")
    if not raw or row.get("score_basis") != SPEC:
        return None
    try:
        working = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if (not isinstance(working, dict) or not isinstance(working.get("asks"), list)
            or working.get("scored_at") != row.get("scored_at")):
        return None
    return working


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


def pending(row: dict, applied: bool) -> bool:
    """Would a click do anything for this row, fetch or score.

    The page asks this to decide whether its script has a POST to make. A role
    that is applied or not open, one with nothing to fetch and no description,
    and one already scored against its description all have nothing to do.
    """
    if applied or not is_open(row):
        return False
    if (row.get("description_text") or "").strip():
        return needs_score(row)
    return job_id_of(row) is not None


# ----------------------------------------------------------------- the pay

# The advert's own words for pay that is not a figure.
PAY_WORDS = re.compile(
    r"\b(?:competitive(?: salary| rate| pay| package)?|negotiable|market rate"
    r"|(?:depending|dependent) on experience)\b", re.IGNORECASE)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _pay_candidate(flat: str) -> str | None:
    """Wording in a description that may be pay, by pattern, short and word for word.

    A sterling figure with its period beside it, through the page checker's own
    reader, else one of the advert's own pay words. It cannot tell a salary from
    a bonus or a budget, which is why it only ever speaks beside the scorer.
    """
    pay = check_jobs.stated_pay(flat)
    if pay and 0 < len(pay["text"]) <= RATE_QUOTE_MAX:
        return pay["text"]
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
    job_id = job_id_of(row)
    if job_id is None:
        _fetch_failed(out, NOT_LINKEDIN_WHY)
        return None
    key = ("page", row["id"])
    held = GUARD.recent_failure(key)
    if held:
        _fetch_failed(out, f"{held[0]} That was a moment ago, and it is not "
                           f"asked for again for {_minutes(held[1])}.")
        return None
    wait = GUARD.blocked_for()
    if wait > 0:
        _fetch_failed(out, "LinkedIn answered 429, too many requests, a "
                           f"moment ago, so Radar is not asking again for "
                           f"{_minutes(wait)}.")
        return None
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


def _failure_key(row: dict, description: str, config: dict) -> tuple:
    """What a remembered failure, or parked work, is about: this role, this
    version of its description, this rubric and this CV. Change any of them and
    it is a new question, which a person may ask at once."""
    try:
        return (row["id"], rc.description_version(description),
                rc.prompt_version(), score_item.get_cv_version(config))
    except Exception:  # noqa: BLE001  a key that cannot be built is a short one
        return (row["id"], rc.description_version(description))


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


def _score(conn, row: dict, description: str, config: dict, out: dict) -> None:
    """Score the role against its description. One model call, no more."""
    key = _failure_key(row, description, config)
    # A reply paid for on an earlier click that the database was too busy to
    # take is stored first, and no call is made.
    parked = GUARD.take_parked("score", key)
    if parked is not None:
        _store_score(conn, row, parked, config, key, out)
        return
    held = GUARD.recent_failure(key)
    if held:
        out["outcome"] = "cooling"
        _notice(out, "score", f"The last try for this role failed. {held[0]} "
                f"It is not tried again for {_minutes(held[1])}.", retry=False)
        return
    try:
        blocks = score_item.build_scorer_system(config, basis=SPEC)
        floors = rc.read_rate_floors(config)
        thresholds = rc.read_score_thresholds(config)
    except (Exception, SystemExit) as err:  # noqa: BLE001  no CV, no floor line
        text = (f"The description was fetched but could not be scored, the "
                f"scorer could not be set up. {str(err)[:300]}")
        GUARD.remember_failure(key, ERROR_COOLDOWN_SECONDS, text)
        out["outcome"] = "score_failed"
        _notice(out, "score", text, retry=True)
        return
    _unused, score_fn = score_item.get_mock_fns()
    opp = _spec_opportunity(row, description)
    try:
        scored, usage, note = score_item.score_opportunity(
            opp, blocks, config, mock_fn=score_fn, floors=floors,
            thresholds=thresholds, basis=SPEC)
    except Exception as err:  # noqa: BLE001  no key, no network, an API error
        text = ("The description was fetched but the scorer could not be "
                f"reached ({type(err).__name__}). No score was stored.")
        GUARD.remember_failure(key, ERROR_COOLDOWN_SECONDS, text)
        print(f"prepare: the scorer raised {type(err).__name__} for id "
              f"{row['id']}: {str(err)[:200]}", file=sys.stderr)
        out["outcome"] = "score_failed"
        _notice(out, "score", text, retry=True)
        return
    out["called"] = bool(usage)
    if scored is None or note or scored.get("tier") is None:
        text = ("The scorer's reply could not be used "
                f"({note or 'no tier came back'}). Nothing was stored for "
                "the score.")
        GUARD.remember_failure(key, UNUSABLE_COOLDOWN_SECONDS, text)
        _log_run(conn, usage, config, False, f"id {row['id']}, not stored, "
                 f"{note or 'no tier'}")
        out["outcome"] = "score_failed"
        # This click paid for the reply, and says so. The memory above does
        # not, because a later click that reads it paid for nothing.
        _notice(out, "score", text + " The reply was paid for.", retry=True)
        return

    working = scored["working"]
    line = rate_line(description, scored.get("rate_quote_raw"))
    working["rate_quote"], working["rate_quote_from"] = line["quote"], line["from"]
    _store_score(conn, row, {"scored": scored, "usage": usage,
                             "cv_version": getattr(blocks, "cv_version", None)},
                 config, key, out)


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
