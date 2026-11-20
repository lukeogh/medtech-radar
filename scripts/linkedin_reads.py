"""One limiter for every read of a LinkedIn job page, whatever started it.

Four things read LinkedIn's guest job pages: the scheduled page check, its
sweep, the check made when a role is opened, and Prepare application when it
fetches a description. They run in different processes, the dashboard in its
container and the checker under n8n, so the limits live in the database they
share, in linkedin_reads, one row per read. A read is reserved before the
request goes out, under the write lock, and its outcome is written when it
comes back.

The limits (advert freshness):
- at least READ_GAP_SECONDS between any two reads, whatever started them
- no read at all while a 429 backoff is running, BACKOFF_SECONDS after the
  last 429 any read got
- one read per role per ROLE_GAP_SECONDS, for the checks (not for Prepare,
  which is a click asking for the description). A read that got a 429 or no
  answer at all does not count, it read nothing.
- at most DAILY_CAPS[kind] reads of that kind in a UTC day: 40 on open.
  Prepare has no daily cap, only the pace.

Nothing here fetches anything. A caller asks reserve(), and fetches only when
it says yes.

The clock is time.time. Under RADAR_MOCK the waits are not slept, they move a
virtual clock forward instead, so a test that runs the paced checker sees the
pace kept without waiting for it.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402

READ_GAP_SECONDS = 20            # between any two LinkedIn reads
BACKOFF_SECONDS = 600            # no read at all for this long after a 429
ROLE_GAP_SECONDS = 24 * 3600     # one check per role per day
DAILY_CAPS = {"open": 40}
KINDS = ("open", "scheduled", "sweep", "prepare")
ROLE_LIMITED = ("open", "scheduled", "sweep")

# Outcomes a read can end with. pending is a read reserved and not yet back.
OUTCOMES = ("pending", "live", "closed", "gone", "unknown", "429", "network")

_offset = [0.0]


def clock() -> float:
    """Seconds since the epoch, moved on by any wait skipped under mock."""
    return time.time() + _offset[0]


def sleep(seconds: float) -> None:
    """Wait. Under RADAR_MOCK the virtual clock moves instead."""
    if seconds <= 0:
        return
    if rc.mock_mode_active():
        _offset[0] += seconds
        return
    time.sleep(seconds)


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_start(at: float) -> float:
    day = datetime.fromtimestamp(at, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp()


def ensure_table(conn) -> None:
    """The table, for a database opened before the schema carried it."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS linkedin_reads ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " at REAL NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,"
        " opportunity_id INTEGER, outcome TEXT NOT NULL DEFAULT 'pending')")


def state(conn, kind: str, opportunity_id: int | None = None,
          at: float | None = None) -> dict:
    """What the limits say about one read now, without reserving it.

    Returns {"ok": bool, "why": None | "backoff" | "gap" | "daily" | "role",
    "wait": seconds until the gap or the backoff ends}. The backoff is
    looked at first, because nothing is read while it runs.
    """
    at = clock() if at is None else at
    last_429 = conn.execute(
        "SELECT MAX(at) AS t FROM linkedin_reads WHERE outcome = '429'"
    ).fetchone()["t"]
    if last_429 is not None and at - last_429 < BACKOFF_SECONDS:
        return {"ok": False, "why": "backoff",
                "wait": BACKOFF_SECONDS - (at - last_429)}
    if opportunity_id is not None and kind in ROLE_LIMITED:
        recent = conn.execute(
            "SELECT 1 FROM linkedin_reads WHERE opportunity_id = ?"
            " AND kind IN ('open', 'scheduled', 'sweep') AND at > ?"
            " AND outcome NOT IN ('429', 'network') LIMIT 1",
            (opportunity_id, at - ROLE_GAP_SECONDS)).fetchone()
        if recent:
            return {"ok": False, "why": "role", "wait": 0}
    cap = DAILY_CAPS.get(kind)
    if cap is not None:
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM linkedin_reads WHERE kind = ? AND at >= ?",
            (kind, _day_start(at))).fetchone()["n"]
        if used >= cap:
            return {"ok": False, "why": "daily", "wait": 0}
    last = conn.execute("SELECT MAX(at) AS t FROM linkedin_reads").fetchone()["t"]
    if last is not None and at - last < READ_GAP_SECONDS:
        return {"ok": False, "why": "gap", "wait": READ_GAP_SECONDS - (at - last)}
    return {"ok": True, "why": None, "wait": 0}


def reserve(conn, kind: str, opportunity_id: int | None = None) -> dict:
    """Take a read if the limits allow one now.

    Under the write lock, so two processes asking in the same moment cannot
    both pass the gap. Returns the state, with "id" set to the reserved row
    when ok. The caller fetches, then calls finish() with the outcome.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown read kind {kind!r}")
    ensure_table(conn)
    rc.begin_write(conn)
    try:
        at = clock()
        got = state(conn, kind, opportunity_id, at)
        if got["ok"]:
            cur = conn.execute(
                "INSERT INTO linkedin_reads (at, ts, kind, opportunity_id)"
                " VALUES (?, ?, ?, ?)", (at, _iso(at), kind, opportunity_id))
            got["id"] = cur.lastrowid
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return got


def reserve_waiting(conn, kind: str, opportunity_id: int | None = None,
                    most: float = READ_GAP_SECONDS + 1) -> dict:
    """reserve(), waiting out the gap between reads when that is all that
    stands in the way, up to most seconds in all. Any other refusal is
    returned as it is."""
    waited = 0.0
    while True:
        got = reserve(conn, kind, opportunity_id)
        if got["ok"] or got["why"] != "gap" or waited >= most:
            return got
        step = min(got["wait"], most - waited) + 0.05
        sleep(step)
        waited += step


def finish(conn, read_id: int, outcome: str) -> None:
    """Write how a reserved read ended. A 429 starts the backoff for every
    reader, because the backoff is worked out from these rows."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    try:
        conn.execute("UPDATE linkedin_reads SET outcome = ? WHERE id = ?",
                      (outcome, read_id))
        conn.commit()
    except sqlite3.OperationalError as err:
        # A database held busy past its timeout. The read has happened and
        # its row stays pending, which still counts it against every limit.
        # Only a 429 is lost from the shared backoff, so it is said aloud.
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        print(f"linkedin_reads: read {read_id} ended {outcome}, not recorded"
              f" ({err})", file=sys.stderr)


def outcome_of(status: int, verdict: str) -> str:
    """The outcome a read is logged with, from its status and verdict."""
    if status == 429:
        return "429"
    if status == 0:
        return "network"
    return verdict if verdict in OUTCOMES else "unknown"
