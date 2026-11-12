#!/usr/bin/env python
"""Prepare application. The advert's full description, scored once, with its asks.

The button on a Jobs row opens /prepare/<id>. That page only reads. When it is
opened by the click (it says so in its address) its script makes the one write
this module serves, POST /prepare, and this is what that does for an open role:

1. If the row stores no description, fetch the advert's public LinkedIn page
   once, with the page checker's own fetch, user agent and ladder, and store
   what it says with the time. No login, no retry, no loop. A 429 is a wait,
   not a failure to hammer: nothing is stored, and nothing is asked of LinkedIn
   again from this page for the page checker's backoff, ten minutes. If the
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

An applied role is shown, never touched. Nothing is fetched, scored or written
for a role marked applied or for any row of its chain, and the writes below
check that again under the write lock, after the model has answered.

Nothing here runs on a schedule, and nothing here closes, dismisses or
acknowledges a role.
"""

from __future__ import annotations

import json
import re
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
    a click, and the waits and the failure memos are a few minutes long.
    """

    def __init__(self) -> None:
        self._master = threading.Lock()
        self._locks: dict = {}
        self.fetch_lock = threading.Lock()
        self.last_fetch = None          # monotonic time of the last page request
        self.blocked_until = 0.0        # no page request before this, after a 429
        self._failures: dict = {}       # key -> (monotonic expiry, text)

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

    def reset(self) -> None:
        """For tests, which start every scenario with nothing remembered."""
        with self._master:
            self._locks.clear()
            self._failures.clear()
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
    old working looking like the current one.
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

def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def rate_line(description: str, model_quote: str | None) -> dict:
    """What the description says about pay, short and word for word (J21).

    Returns {"quote": text or None, "from": where}. The model is asked for the
    shortest stretch of the advert that states pay, and nothing it writes is
    shown unless that stretch is found in the description, which makes the
    quote the advert's own words: what is returned is the description's
    characters, found without regard to case and with runs of white space
    folded. A reply that names none is believed, from none. A quote that is
    not in the description, or is not short, is not shown, and the page
    checker's own reader takes a turn over the description instead, from
    pattern, which is word for word by construction. Where that finds nothing
    either, the model said there was pay and the advert cannot be made to
    show it, from unverified, and the page says so and does not claim the
    advert names none. The floors the pay is judged against are not here, they
    are read from the preferences file where the gate is made.
    """
    quote = _flat(model_quote)
    if not quote:
        return {"quote": None, "from": "none"}
    flat = _flat(description)
    if len(quote) <= RATE_QUOTE_MAX:
        found = re.search(re.escape(quote), flat, re.IGNORECASE)
        if found:
            return {"quote": found.group(0), "from": "model"}
    pay = check_jobs.stated_pay(flat)
    if pay and 0 < len(pay["text"]) <= RATE_QUOTE_MAX:
        return {"quote": pay["text"], "from": "pattern"}
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


def _fetch_failed(out: dict, why: str, retry: bool = True) -> None:
    out["outcome"] = "no_description"
    _notice(out, "fetch",
            f"The full description could not be fetched. {why} The score "
            "shown is the one made from the advert card.", retry)


# ----------------------------------------------------------- the one fetch

def _fetch_and_store(conn, row: dict, out: dict) -> str:
    """Fetch the page once and store its description. Returns it, or "".

    Everything that stops this from fetching is told to the page in a notice,
    and so is everything that goes wrong after it has.
    """
    job_id = job_id_of(row)
    if job_id is None:
        _fetch_failed(out, "This advert's link is not a LinkedIn job page, "
                           "and LinkedIn job pages are the only ones Radar "
                           "reads.", retry=False)
        return ""
    wait = GUARD.blocked_for()
    if wait > 0:
        _fetch_failed(out, "LinkedIn answered 429, too many requests, a "
                           f"moment ago, so Radar is not asking again for "
                           f"{_minutes(wait)}.")
        return ""
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
            return ""
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
        _fetch_failed(out, "Radar could not reach LinkedIn from this machine.")
        return ""
    if status == 429:
        _fetch_failed(out, "LinkedIn answered 429, too many requests. Nothing "
                           "was stored, and Radar will not ask again for "
                           f"{_minutes(check_jobs.BACKOFF_SECONDS)}.")
        return ""
    verdict, reason = check_jobs.classify(status, final_url, body,
                                          row.get("title"), job_id)
    if verdict == "gone":
        _fetch_failed(out, "LinkedIn sent the link to its expired job page, "
                           "so the advert looks gone.", retry=False)
        return ""
    if verdict == "closed":
        _fetch_failed(out, "LinkedIn shows this advert as closed. Radar has "
                           "not closed the role in its own records, use Closed "
                           "on the Jobs row if you agree.", retry=False)
        return ""
    if verdict != "live":
        if "authwall" in reason:
            _fetch_failed(out, "LinkedIn asked for a login, and Radar never "
                               "logs in.")
        else:
            _fetch_failed(out, "LinkedIn served a page Radar could not match "
                               f"to this advert ({reason}).")
        return ""
    description = check_jobs.read_page(body).get("description") or ""
    if not description.strip():
        _fetch_failed(out, "The page was served but carried no description "
                           "text.")
        return ""
    # The write lock first, then look again. The page took a while to come,
    # and a role can be applied for in that time. An applied role keeps what
    # it has, and a description stored by someone else in the meantime stays.
    rc.begin_write(conn)
    if rc.role_is_applied(conn, row["id"]):
        conn.rollback()
        out["outcome"] = "applied"
        _notice(out, "info", APPLIED_NOTE)
        return ""
    conn.execute(
        "UPDATE opportunities SET description_text = ?,"
        " description_fetched_at = ? WHERE id = ? AND"
        " (description_text IS NULL OR TRIM(description_text) = '') AND "
        + rc.NOT_APPLIED_SQL, (description, rc.now_iso(), row["id"]))
    conn.commit()
    stored = conn.execute("SELECT description_text FROM opportunities"
                          " WHERE id = ?", (row["id"],)).fetchone()
    return (stored["description_text"] or "") if stored else ""


APPLIED_NOTE = ("This role is marked applied. Prepare application shows what "
                "is stored and fetches, scores and writes nothing for it.")
NOT_OPEN_NOTE = ("This role is not open, it has been acknowledged, closed, "
                 "dismissed or retired. Prepare application shows what is "
                 "stored and fetches, scores and writes nothing for it.")


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


def _log_call(conn, usage: dict, config: dict, stored: bool, note: str) -> None:
    """One row in runs for a model call, so the week's cost counts it."""
    if not usage:
        return
    try:
        rc.log_run(conn, "prepare",
                   mode="mock" if rc.mock_mode_active() else "live",
                   items_in=1, items_new=1 if stored else 0,
                   model=usage.get("model") or config.get("claude_model_score"),
                   usage=usage, note=note)
    except Exception as err:  # noqa: BLE001  a log line never costs the result
        print(f"prepare: the run record was not written ({type(err).__name__})",
              file=sys.stderr)


def _failure_key(row: dict, description: str, config: dict) -> tuple:
    """What a remembered failure is about: this role, this version of its
    description, this rubric and this CV. Change any of them and it is a new
    question, which a person may ask at once."""
    try:
        return (row["id"], rc.description_version(description),
                rc.prompt_version(), score_item.get_cv_version(config))
    except Exception:  # noqa: BLE001  a key that cannot be built is a short one
        return (row["id"], rc.description_version(description))


def _score(conn, row: dict, description: str, config: dict, out: dict) -> None:
    """Score the role against its description. One model call, no more."""
    key = _failure_key(row, description, config)
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
        _log_call(conn, usage, config, False, f"id {row['id']}, not stored, "
                  f"{note or 'no tier'}")
        out["outcome"] = "score_failed"
        # This click paid for the reply, and says so. The memory above does
        # not, because a later click that reads it paid for nothing.
        _notice(out, "score", text + " The reply was paid for.", retry=True)
        return

    working = scored["working"]
    line = rate_line(description, scored.get("rate_quote_raw"))
    working["rate_quote"], working["rate_quote_from"] = line["quote"], line["from"]

    # The write lock, then look again: the role may have been applied for while
    # the model answered. It keeps the score it was applied with, and the reply
    # that was paid for is not stored.
    rc.begin_write(conn)
    if rc.role_is_applied(conn, row["id"]):
        conn.rollback()
        _log_call(conn, usage, config, False,
                  f"id {row['id']}, applied while scoring, not stored")
        out["outcome"] = "applied"
        _notice(out, "info", "This role was marked applied while the scorer "
                "was answering, so the reply was not stored. " + APPLIED_NOTE)
        return
    held_tier = conn.execute("SELECT tier FROM opportunities WHERE id = ?",
                             (row["id"],)).fetchone()
    old_tier = held_tier["tier"] if held_tier else None
    rc.archive_score(conn, row["id"], "prepare")
    params = ((scored["cv_match"], scored["one_line_why"],
               json.dumps(scored["red_flags"]), scored["suggested_action"],
               scored["act_by"], scored["thread_type"],
               getattr(blocks, "cv_version", None),
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
        _log_call(conn, usage, config, False,
                  f"id {row['id']}, the row was not there to write, not stored")
        out["outcome"] = "score_failed"
        _notice(out, "score", "The score could not be stored, the role is "
                "gone or has been applied for. Nothing was changed.")
        return
    moved = old_tier if (old_tier and old_tier != scored["tier"]) else None
    rc.record_score(conn, row["id"], "prepare", tier_from=moved)
    conn.commit()
    _log_call(conn, usage, config, True,
              f"id {row['id']}, {len(working['asks'])} asks"
              + (f", tier {old_tier} to {scored['tier']}" if moved else ""))
    out["outcome"] = "scored"
    out["scored"] = True
    if moved:
        out["tier_move"] = {"from": moved, "to": scored["tier"]}


# ------------------------------------------------------------ the click

def prepare(item_id: int, db_path: Path | None = None,
            config: dict | None = None) -> dict:
    """Everything one click on Prepare application does. See the module note.

    Returns {"ok": True, "outcome": ..., "fetched": bool, "called": bool (a
    model call was made and answered), "scored": bool (and stored),
    "tier_move": {from, to} or None, "notices": [...]}. The
    outcome is scored, reused (the score for this version of the description
    was already stored), no_description (it could not be had), score_failed,
    cooling (a failure still remembered), applied, not_open or busy. A role
    that does not exist is {"ok": False, "note": ...}. A database that stays
    locked raises sqlite3.OperationalError, for the route to answer politely.
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
