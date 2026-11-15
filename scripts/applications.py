"""The applications board: where each application stands, and every move.

Built from design/jobs/applied-board v2 (design/round-1 branch, 15 Nov 2026).
Six stages, Preparing, Applied, In conversation, Interviewing, Offer and
Closed, one application per role (keyed as tailored_cvs is, tailored_cv's
role_ref), stored in the applications table with every change to it in
application_events, with when and who.

Who moves what:
- Radar fills Preparing, when Save CV stores a tailored CV for a role that is
  not applied for (on_saved), and Applied, when the role is marked applied on
  Jobs or Prepare application (on_applied). Undo applied on Jobs takes an
  Applied item back to Preparing, or off the board when no CV was saved
  (on_unapplied), and is refused once the owner has moved it on.
- Every later move is the owner's, on the board's Move to menu or its keys
  (move). Closed always carries a reason: no reply, turned down or withdrawn.
- The 21-day rule (run_rules, run by the signals job every two hours): an
  Applied item with no movement for 21 days is closed as no reply, without
  asking, and says so. From day 14 the board says when that will happen. A
  stage change, a next step set, changed or cleared, or a contact logged with
  the company counts as movement. The same run adds Radar's "Follow up" next
  step to an Applied item 7 days after it was applied for, once per stay in
  Applied and never over a step the owner set.

A closed application is not a closed advert. Nothing here touches closed_at,
the expiry or the page checker, and applied_at stays on the role whatever its
stage, so radar_common.APPLIED_SQL keeps protecting an applied role on the
board, Closed included.

Applications made for roles already on file when the board arrives (sync) are
dated from applied_at or saved_at, as the brief asks. So that the first run
after the deploy does not close every old application at once without the
warning ever having been on screen, an item is never closed sooner than
GRACE_DAYS after its application was made.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import radar_common as rc

STAGES = ("preparing", "applied", "conversation", "interviewing", "offer", "closed")
STAGE_WORDS = {"preparing": "Preparing", "applied": "Applied",
               "conversation": "In conversation", "interviewing": "Interviewing",
               "offer": "Offer", "closed": "Closed"}
# Lower case, for a sentence: "4 days in conversation".
STAGE_IN = {"preparing": "Preparing", "applied": "Applied",
            "conversation": "conversation", "interviewing": "Interviewing",
            "offer": "Offer", "closed": "Closed"}
OPEN_STAGES = STAGES[:5]           # what the Applied count covers
APPLIED_STAGES = STAGES[1:5]       # the stages that mean an application went in
REASONS = ("no-reply", "turned-down", "withdrawn")
REASON_WORDS = {"no-reply": "No reply", "turned-down": "Turned down",
                "withdrawn": "Withdrawn"}
RADAR, OWNER = "radar", "owner"

FOLLOW_UP = "Follow up"
FOLLOW_UP_DAYS = 7
WARN_DAYS = 14
CLOSE_DAYS = 21
GRACE_DAYS = 7
NEXT_STEP_MAX = 200

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


# ------------------------------------------------------------------ time

def parse(iso) -> datetime | None:
    """An ISO moment or date as an aware UTC datetime, None for nothing."""
    text = str(iso or "").strip()
    for fmt, size in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16),
                      ("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now(now) -> datetime:
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc)
    return parse(now) or datetime.now(timezone.utc)


def uk_date(moment: datetime):
    """The calendar day in the UK, which is what the page's dates are. UTC
    where the machine has no time zone database, right all winter."""
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/London")).date()
    except Exception:  # noqa: BLE001
        return moment.astimezone(timezone.utc).date()


def days_between(earlier, later) -> int:
    """Whole UK calendar days from one moment's day to another's, never negative."""
    a = earlier if isinstance(earlier, datetime) else parse(earlier)
    b = later if isinstance(later, datetime) else parse(later)
    if a is None or b is None:
        return 0
    return max(0, (uk_date(b) - uk_date(a)).days)


# ------------------------------------------------------------------ keys

def role_ref(row) -> str:
    """What an application is keyed on, the same as tailored_cv.role_ref."""
    key = rc._row_field(row, "role_key")
    return f"key:{key}" if key else f"id:{int(rc._row_field(row, 'id'))}"


def has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                        " AND name = 'applications'").fetchone() is not None


def _event(conn, app_id, at, actor, kind, from_stage=None, to_stage=None,
           reason=None, note=None) -> None:
    conn.execute("INSERT INTO application_events (application_id, at, actor, kind,"
                 " from_stage, to_stage, reason, note) VALUES (?,?,?,?,?,?,?,?)",
                 (app_id, at, actor, kind, from_stage, to_stage, reason, note))


def get(conn, ref: str) -> dict | None:
    row = conn.execute("SELECT * FROM applications WHERE role_ref = ?", (ref,)).fetchone()
    return dict(row) if row is not None else None


def _saved_cv(conn, ref: str) -> bool:
    return conn.execute("SELECT 1 FROM tailored_cvs WHERE role_ref = ?"
                        " AND saved_text IS NOT NULL", (ref,)).fetchone() is not None


def _insert(conn, ref, opportunity_id, stage, stage_at, now, note) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO applications (role_ref, opportunity_id, stage,"
        " stage_at, stage_by, moved_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (ref, opportunity_id, stage, stage_at, RADAR, stage_at, now))
    if not cur.rowcount:
        return 0
    app_id = cur.lastrowid
    _event(conn, app_id, now, RADAR, "stage", None, stage, None, note)
    return app_id


# ------------------------------------------------------------------ keeping it whole

def sync(conn, now: str | None = None) -> int:
    """Make the application of every applied role and every saved tailored CV
    that has none. Returns how many were made. Reads first and writes only
    when something is missing, so the many opens that find nothing take no
    write lock. The caller commits (get_db does)."""
    if not has_table(conn):
        return 0
    have = {r[0] for r in conn.execute("SELECT role_ref FROM applications")}
    applied: dict = {}
    for r in conn.execute("SELECT id, role_key, applied_at FROM opportunities"
                          " WHERE applied_at IS NOT NULL ORDER BY applied_at, id"):
        applied.setdefault(role_ref(r), r)
    saved = [r for r in conn.execute(
        "SELECT role_ref, opportunity_id, saved_at FROM tailored_cvs"
        " WHERE saved_text IS NOT NULL ORDER BY id")]
    todo_applied = [(ref, r) for ref, r in applied.items() if ref not in have]
    todo_saved = [r for r in saved
                  if r["role_ref"] not in have and r["role_ref"] not in applied]
    if not todo_applied and not todo_saved:
        return 0
    now = now or rc.now_iso()
    made = 0
    for ref, r in todo_applied:
        made += bool(_insert(conn, ref, r["id"], "applied", r["applied_at"], now,
                             "on file before the board, dated from the day applied"))
    for r in todo_saved:
        made += bool(_insert(conn, r["role_ref"], r["opportunity_id"], "preparing",
                             r["saved_at"] or now, now,
                             "on file before the board, dated from the CV saved"))
    return made


def on_saved(conn, row, now: str | None = None) -> None:
    """Save CV stored a tailored CV for a role not applied for: Preparing.
    Inside the caller's transaction; the caller commits."""
    if not has_table(conn):
        return
    now = now or rc.now_iso()
    ref = role_ref(row)
    app = get(conn, ref)
    if app is None:
        _insert(conn, ref, int(rc._row_field(row, "id")), "preparing", now, now,
                "a tailored CV was saved")
    elif app["stage"] is None:
        conn.execute("UPDATE applications SET stage = 'preparing', closed_reason = NULL,"
                     " stage_at = ?, stage_by = ?, moved_at = ? WHERE id = ?",
                     (now, RADAR, now, app["id"]))
        _event(conn, app["id"], now, RADAR, "stage", None, "preparing", None,
               "a tailored CV was saved")


def on_applied(conn, row_id: int, now: str | None = None) -> None:
    """The role was marked applied: Applied, from wherever it was (Preparing,
    off the board, or closed before it was ever applied for). Inside the
    caller's transaction."""
    if not has_table(conn):
        return
    row = conn.execute("SELECT id, role_key FROM opportunities WHERE id = ?",
                       (row_id,)).fetchone()
    if row is None:
        return
    now = now or rc.now_iso()
    ref = role_ref(row)
    app = get(conn, ref)
    if app is None:
        _insert(conn, ref, row_id, "applied", now, now, "marked applied")
    elif app["stage"] not in APPLIED_STAGES:
        conn.execute("UPDATE applications SET stage = 'applied', closed_reason = NULL,"
                     " stage_at = ?, stage_by = ?, moved_at = ?, opportunity_id = ?,"
                     " follow_up_added_at = NULL WHERE id = ?",
                     (now, RADAR, now, row_id, app["id"]))
        _event(conn, app["id"], now, RADAR, "stage", app["stage"], "applied", None,
               "marked applied")


def unapply_refusal(conn, row) -> str | None:
    """Why Undo applied may not run on this role now, or None. Once the owner
    has moved the application on, the board is where it moves back."""
    if not has_table(conn):
        return None
    app = get(conn, role_ref(row))
    if app is None or app["stage"] in (None, "preparing", "applied"):
        return None
    return (f"That application is at {STAGE_WORDS[app['stage']]} on the "
            "applications board. Move it back to Applied there first.")


def on_unapplied(conn, row, now: str | None = None) -> None:
    """Undo applied: Applied back to Preparing when a CV was saved for the
    role, otherwise off the board. Inside the caller's transaction."""
    if not has_table(conn):
        return
    now = now or rc.now_iso()
    ref = role_ref(row)
    app = get(conn, ref)
    if app is None or app["stage"] != "applied":
        return
    to = "preparing" if _saved_cv(conn, ref) else None
    conn.execute("UPDATE applications SET stage = ?, closed_reason = NULL, stage_at = ?,"
                 " stage_by = ?, moved_at = ? WHERE id = ?",
                 (to, now, RADAR, now, app["id"]))
    _event(conn, app["id"], now, RADAR, "stage", "applied", to, None,
           "application undone on Jobs")


# ------------------------------------------------------------------ the applied stamp

def stamp_applied(conn, item_id: int, role_key, now: str | None = None) -> int:
    """Stamp one open row applied, with the role's saved CV name, as the
    Applied button does. Returns the rows stamped, 0 when the row is not open
    or the role is already applied. Inside the caller's transaction."""
    target = {"id": item_id, "role_key": role_key}
    cur = conn.execute(
        "UPDATE opportunities SET applied_at = ?, applied_cv = ("
        "SELECT saved_name FROM tailored_cvs WHERE role_ref = ?"
        " AND saved_text IS NOT NULL)"
        " WHERE id = ? AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL"
        " AND expired_at IS NULL AND " + rc.NOT_APPLIED_SQL,
        (now or rc.now_iso(), role_ref(target), item_id))
    if cur.rowcount:
        # Acting on a role is reading it (the Jobs page's New).
        rc.mark_read(conn, item_id, role_key)
    return cur.rowcount


def clear_applied(conn, item_id: int, role_key) -> int:
    """Clear every applied stamp on the role's chain, as Undo applied does."""
    cur = conn.execute(
        "UPDATE opportunities SET applied_at = NULL, applied_cv = NULL"
        " WHERE (id = ? OR (role_key IS NOT NULL AND role_key = ?))"
        " AND applied_at IS NOT NULL",
        (item_id, role_key))
    return cur.rowcount


# ------------------------------------------------------------------ the owner's moves

def _refuse(conn, note: str) -> dict:
    if conn.in_transaction:
        conn.rollback()
    return {"ok": False, "note": note}


def _stamp_role(conn, row, now) -> int | None:
    """Stamp the role applied on the application's row, or on the newest open
    advert of its chain when that row's advert is no longer open. The row
    stamped, or None."""
    if stamp_applied(conn, row["id"], row["role_key"], now):
        return row["id"]
    if row["role_key"]:
        for other in conn.execute("SELECT id FROM opportunities WHERE role_key = ?"
                                  " AND id <> ? ORDER BY id DESC",
                                  (row["role_key"], row["id"])).fetchall():
            if stamp_applied(conn, other["id"], row["role_key"], now):
                return other["id"]
    return None


def _undoing(conn, app: dict, to: str) -> dict | None:
    """What the owner's last move changed, when a move to `to` undoes it: the
    last stage change was the owner's, from `to` to where the item is now."""
    last = conn.execute("SELECT * FROM application_events WHERE application_id = ?"
                        " AND kind = 'stage' ORDER BY id DESC LIMIT 1",
                        (app["id"],)).fetchone()
    if (last is None or last["actor"] != OWNER or last["to_stage"] != app["stage"]
            or last["from_stage"] != to):
        return None
    try:
        before = json.loads(last["note"] or "{}").get("before")
    except (TypeError, ValueError, AttributeError):
        return None
    return before if isinstance(before, dict) else None


def move(conn, app_id: int, to: str, reason=None, expect=None,
         now: str | None = None, undo: bool = False) -> dict:
    """The owner moves an application to a stage. expect is the stage the page
    drew it in: a page left open from before cannot move an item that has
    moved since. Moving into a stage that means an application went in stamps
    the role applied when it is not; moving back to Preparing, which only an
    Undo does, clears the stamp and needs a saved CV. undo says this move takes
    back the owner's last one: the item gets back the day it entered its
    column, its last movement and its Follow up record, and a stamp that move
    made is cleared, so an Undo leaves the item as it was. Commits."""
    if to not in STAGES:
        return {"ok": False, "note": "Choose a column from the Move to menu."}
    if to == "closed":
        if reason not in REASONS:
            return {"ok": False, "note": "Say why it closed: no reply, turned down or withdrawn."}
    else:
        reason = None
    if expect is not None and expect not in STAGES:
        return {"ok": False, "note": "The page sent a column it does not have. Reload it."}
    now = now or rc.now_iso()
    rc.begin_write(conn)
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if app is None or app["stage"] is None:
        return _refuse(conn, "That application is no longer on the board. Reload the page.")
    app = dict(app)
    if expect is not None and app["stage"] != expect:
        return _refuse(conn, f"That application has moved to {STAGE_WORDS[app['stage']]} "
                             "since the page was drawn. Reload to see where it is.")
    if app["stage"] == to and (to != "closed" or app["closed_reason"] == reason):
        return _refuse(conn, f"It is already in {STAGE_WORDS[to]}.")
    restore = _undoing(conn, app, to) if undo else None
    row = conn.execute("SELECT id, role_key FROM opportunities WHERE id = ?",
                       (app["opportunity_id"],)).fetchone()
    applied = row is not None and rc.role_is_applied(conn, row["id"])
    stamped = None
    if to in APPLIED_STAGES and not applied:
        if row is None:
            return _refuse(conn, "The role is no longer on file, so it cannot be marked applied.")
        stamped = _stamp_role(conn, row, now)
        if stamped is None:
            return _refuse(conn, "No advert of that role is open any more (acknowledged, closed, "
                                 "dismissed or expired), so it cannot be marked applied.")
    if to == "preparing":
        if not _saved_cv(conn, app["role_ref"]):
            return _refuse(conn, "Preparing is for a role with a tailored CV saved, and this "
                                 "one has none.")
        if applied:
            clear_applied(conn, row["id"], row["role_key"])
    elif restore and restore.get("stamped") and applied and to not in APPLIED_STAGES:
        # The move being undone stamped a role that had not been applied for.
        clear_applied(conn, row["id"], row["role_key"])
    if restore:
        conn.execute(
            "UPDATE applications SET stage = ?, closed_reason = ?, stage_at = ?,"
            " stage_by = ?, moved_at = ?, follow_up_added_at = ? WHERE id = ?",
            (to, reason, restore.get("stage_at") or now, restore.get("stage_by") or OWNER,
             restore.get("moved_at") or now, restore.get("follow_up_added_at"), app_id))
        _event(conn, app_id, now, OWNER, "stage", app["stage"], to, reason,
               json.dumps({"undo": True}))
    else:
        conn.execute(
            "UPDATE applications SET stage = ?, closed_reason = ?, stage_at = ?,"
            " stage_by = ?, moved_at = ?, opportunity_id = ?,"
            " follow_up_added_at = CASE WHEN ? = 'applied'"
            " THEN NULL ELSE follow_up_added_at END WHERE id = ?",
            (to, reason, now, OWNER, now, stamped or app["opportunity_id"], to, app_id))
        before = {k: app[k] for k in ("stage_at", "stage_by", "moved_at",
                                      "follow_up_added_at", "closed_reason")}
        before["stamped"] = stamped is not None
        _event(conn, app_id, now, OWNER, "stage", app["stage"], to, reason,
               json.dumps({"before": before}))
    conn.commit()
    return {"ok": True, "id": app_id, "stage": to, "reason": reason,
            "from": app["stage"], "from_reason": app["closed_reason"],
            "undone": bool(restore)}


def clean_next(text, due, interview) -> tuple:
    """(text, due, interview) as stored, or raises ValueError with what to fix."""
    if text is not None and not isinstance(text, str):
        raise ValueError("The next step must be text.")
    words = rc.sanitise_free_text(str(text or "").strip()) if text else ""
    words = " ".join(str(words or "").split())[:NEXT_STEP_MAX]
    if due is not None and not isinstance(due, str):
        raise ValueError("The due date must be a date.")
    due = (due or "").strip()
    if due and (not _DATE.match(due) or parse(due) is None):
        raise ValueError("The due date must be a date, like 2026-11-20.")
    if interview is not None and not isinstance(interview, str):
        raise ValueError("The interview must be a date and a time.")
    interview = (interview or "").strip()[:16]
    if interview:
        try:
            datetime.strptime(interview, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise ValueError("The interview must be a date and a time, like "
                             "2026-11-17T10:00.") from None
        if not _MINUTE.match(interview):
            raise ValueError("The interview must be a date and a time.")
    if words and not due:
        raise ValueError("Give the next step a due date.")
    if due and not words:
        raise ValueError("Say what the next step is, or clear the due date.")
    return words or None, due or None, interview or None


def set_next(conn, app_id: int, text, due, interview, now: str | None = None) -> dict:
    """The owner sets, changes or clears an application's next step and its
    interview. Counts as movement. Commits."""
    try:
        text, due, interview = clean_next(text, due, interview)
    except ValueError as err:
        return {"ok": False, "invalid": True, "note": str(err)}
    now = now or rc.now_iso()
    rc.begin_write(conn)
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if app is None or app["stage"] is None:
        return _refuse(conn, "That application is no longer on the board. Reload the page.")
    if app["stage"] == "closed":
        return _refuse(conn, "A closed application has no next step. Move it back first.")
    if (app["next_step"], app["next_step_due"], app["interview_at"]) == (text, due, interview):
        conn.rollback()
        return {"ok": True, "id": app_id, "unchanged": True}
    # Radar's Follow up stays Radar's while only the interview changes.
    if not text:
        by = None
    elif (text, due) == (app["next_step"], app["next_step_due"]):
        by = app["next_step_by"]
    else:
        by = OWNER
    # Once the owner has set a step in this stay in Applied, Radar adds no
    # Follow up to it, even if the owner later clears theirs.
    owner_step = bool(text) and by == OWNER and app["stage"] == "applied"
    conn.execute("UPDATE applications SET next_step = ?, next_step_due = ?,"
                 " next_step_by = ?, interview_at = ?, moved_at = ?,"
                 " follow_up_added_at = CASE WHEN ? THEN COALESCE(follow_up_added_at, ?)"
                 " ELSE follow_up_added_at END WHERE id = ?",
                 (text, due, by, interview, now, owner_step, now, app_id))
    _event(conn, app_id, now, OWNER, "next-step", app["stage"], app["stage"], None,
           json.dumps({"step": text, "due": due, "interview": interview}))
    conn.commit()
    return {"ok": True, "id": app_id, "next_step": text, "due": due,
            "interview": interview}


# ------------------------------------------------------------------ reading

LOAD_SQL = """
SELECT a.*, o.title, o.company, o.company_id, o.region, o.location_class,
       o.role_key, o.source,
       (SELECT MIN(x.applied_at) FROM opportunities x
         WHERE x.id = o.id OR (o.role_key IS NOT NULL AND x.role_key = o.role_key)) AS applied_on,
       (SELECT x.applied_cv FROM opportunities x
         WHERE (x.id = o.id OR (o.role_key IS NOT NULL AND x.role_key = o.role_key))
           AND x.applied_at IS NOT NULL ORDER BY x.applied_at, x.id LIMIT 1) AS applied_cv,
       t.saved_name, t.saved_at,
       (SELECT MAX(touched_at) FROM touches
         WHERE (o.company_id IS NOT NULL AND touches.company_id = o.company_id)
            OR (o.company IS NOT NULL AND lower(trim(touches.company)) = lower(trim(o.company)))
       ) AS last_touch
  FROM applications a
  LEFT JOIN opportunities o ON o.id = a.opportunity_id
  LEFT JOIN tailored_cvs t ON t.role_ref = a.role_ref AND t.saved_text IS NOT NULL
 WHERE a.stage IS NOT NULL
"""


def load(conn) -> list | None:
    """Every application on the board, with its role, or None when the board's
    tables are not there yet (a database no writer has opened since)."""
    try:
        if not has_table(conn):
            return None
        return [dict(r) for r in conn.execute(LOAD_SQL + " ORDER BY a.stage_at DESC, a.id DESC")]
    except sqlite3.OperationalError:
        return None


def last_movement(app: dict) -> datetime | None:
    moments = [parse(app.get(k)) for k in ("stage_at", "moved_at", "last_touch")]
    moments = [m for m in moments if m is not None]
    return max(moments) if moments else None


def close_on(app: dict) -> datetime | None:
    """When the 21-day rule closes an Applied item, None for any other stage."""
    if app.get("stage") != "applied":
        return None
    last = last_movement(app)
    if last is None:
        return None
    due = last + timedelta(days=CLOSE_DAYS)
    made = parse(app.get("created_at"))
    if made is not None:
        due = max(due, made + timedelta(days=GRACE_DAYS))
    return due


def still_days(app: dict, now) -> int:
    """Whole days since the item last moved."""
    last = last_movement(app)
    return days_between(last, _now(now)) if last else 0


def warned(app: dict, now) -> bool:
    """An Applied item says when Radar will close it from day 14 on."""
    return app.get("stage") == "applied" and still_days(app, now) >= WARN_DAYS


# ------------------------------------------------------------------ the scheduled rule

def run_rules(conn, now=None) -> dict:
    """The 21-day rule and Radar's Follow up, for the scheduled run. Makes any
    application missing first. Takes the write lock before it reads, so what
    it decides on is what it writes. Commits. Makes no model call."""
    if not has_table(conn):
        return {"made": 0, "closed": 0, "follow_ups": 0}
    moment = _now(now)
    stamp = iso(moment)
    rc.begin_write(conn)
    try:
        made = sync(conn, stamp)
        rows = [dict(r) for r in conn.execute(LOAD_SQL + " AND a.stage = 'applied'")]
        closed = follow = 0
        for app in rows:
            due = close_on(app)
            if due is not None and moment >= due:
                cur = conn.execute(
                    "UPDATE applications SET stage = 'closed', closed_reason = 'no-reply',"
                    " stage_at = ?, stage_by = ?,"
                    " next_step = CASE WHEN next_step_by = 'radar' THEN NULL ELSE next_step END,"
                    " next_step_due = CASE WHEN next_step_by = 'radar' THEN NULL ELSE next_step_due END,"
                    " next_step_by = CASE WHEN next_step_by = 'radar' THEN NULL ELSE next_step_by END"
                    " WHERE id = ? AND stage = 'applied'",
                    (stamp, RADAR, app["id"]))
                if cur.rowcount:
                    closed += 1
                    _event(conn, app["id"], stamp, RADAR, "stage", "applied", "closed",
                           "no-reply", f"{CLOSE_DAYS} days with no movement")
                continue
            entered = parse(app.get("stage_at"))
            if (not app.get("next_step") and not app.get("follow_up_added_at")
                    and entered is not None
                    and moment >= entered + timedelta(days=FOLLOW_UP_DAYS)):
                cur = conn.execute(
                    "UPDATE applications SET next_step = ?, next_step_due = ?,"
                    " next_step_by = ?, follow_up_added_at = ? WHERE id = ?"
                    " AND stage = 'applied' AND next_step IS NULL",
                    (FOLLOW_UP, uk_date(moment).isoformat(), RADAR, stamp, app["id"]))
                if cur.rowcount:
                    follow += 1
                    _event(conn, app["id"], stamp, RADAR, "next-step", "applied",
                           "applied", None, json.dumps({"step": FOLLOW_UP,
                                                        "due": uk_date(moment).isoformat(),
                                                        "interview": None}))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"made": made, "closed": closed, "follow_ups": follow}
