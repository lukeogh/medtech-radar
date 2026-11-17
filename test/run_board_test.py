#!/usr/bin/env python
"""Checks for the applications board (design/jobs/applied-board v2).

No model calls and no network: RADAR_MOCK is forced on, the signals run is
given no sources, and every database is a throwaway under a temporary folder.

1. The store: the two tables arrive on a database in the old shape, every
   applied role lands in Applied dated from applied_at and every saved
   tailored CV in Preparing dated from saved_at, once, behind a flag, by
   Radar. The CHECKs refuse a stage, a reason or a who that is not one.
2. Radar fills Preparing and Applied: Save CV's hook, Mark applied on POST
   /apply (from Preparing or from nothing), and Undo applied back to Preparing
   or off the board, refused once the owner has moved the item on.
3. The owner's moves: every column, Closed only with a reason, a page left
   open from before refused, Undo as a move back, a move into an applied
   column stamping the role and Undo back to Preparing clearing it, and every
   change in the history with when and who.
4. A closed application is not a closed advert: closed_at, expired_at and the
   page checker's selection are untouched, and the role stays applied and
   protected in every stage.
5. The 21-day rule and Follow up, at chosen moments: quiet to day 13, the
   warning from day 14, closed as no reply at 21 by Radar, movement (a stage
   change, a next step, a logged contact) restarting the clock, the grace for
   applications made by the migration, Follow up on day 7, once per stay, never
   over the owner's step. The signals run carries the rule and says so.
6. Next steps: set, change, clear, the checks on what is sent, closed refused,
   Waiting on you soonest first and never a closed item.
7. The page: the Board by default at /jobs?show=applied, the List at
   layout=list, a role's address always the List, six columns in order with
   Radar's two marked, the Applied count Preparing to Offer, the warning and
   the Radar close in words, no dragging, the keys, the phone picker, and the
   board fragment.
8. The routes: POST /application/move and /application/next through the
   guard, their 400s, 409s and 200s, and a forged request refused with the
   database left as it was.
9. The scripts parse (node --check), and the board's own script is driven in
   node by test/board_script_harness.js: Move to from its button, Tab and
   Escape, a move joining Undo last, Undo last, and M, Z and ? doing
   nothing. The List's undo of a board move is read from its script. Undo on
   the store restores the item as it was, the applied stamp included.
10. The box checks: no command block is empty, every query in them runs on a
    seeded database, every python -c compiles, and the rollback never
    restores the backup.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["RADAR_MOCK"] = "1"

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(TEST_DIR))

import applications as ap  # noqa: E402
import board_page  # noqa: E402
import build_dashboard  # noqa: E402
import check_jobs  # noqa: E402
import check_signals  # noqa: E402
import jobs_page  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
CFG = rc.load_config()
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def day(n: float) -> str:
    """The moment n days after T0."""
    return iso(T0 + timedelta(days=n))


_n = 0


def put(conn, title="Head of Software", company="Alpha Medical", **cols) -> int:
    global _n
    _n += 1
    row = {"url_hash": f"board-{_n}", "first_seen": day(-5), "last_seen": day(-5),
           "source": "linkedin-alert", "title": title, "company": company,
           "location": "Guildford", "tier": "top", "cv_match": 80,
           "role_key": rc.role_key(title, company)}
    row.update(cols)
    conn.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES"
                 f" ({', '.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()
    return conn.execute("SELECT id FROM opportunities WHERE url_hash = ?",
                        (row["url_hash"],)).fetchone()[0]


def company(conn, name) -> int:
    conn.execute("INSERT INTO companies (norm_name, display_name, first_seen) VALUES (?,?,?)",
                 (name.lower(), name, day(-30)))
    conn.commit()
    return conn.execute("SELECT id FROM companies WHERE norm_name = ?", (name.lower(),)).fetchone()[0]


def saved_cv(conn, oid, at=None) -> None:
    row = conn.execute("SELECT id, role_key, company, title FROM opportunities WHERE id = ?",
                       (oid,)).fetchone()
    conn.execute("INSERT INTO tailored_cvs (role_ref, opportunity_id, cv_text, saved_text,"
                 " saved_name, saved_at) VALUES (?,?,?,?,?,?)",
                 (ap.role_ref(row), oid, "cv", "cv",
                  f"CV - Alex Rennie - {row['company']} - {row['title']}", at or day(0)))
    conn.commit()


def app_of(conn, oid) -> dict | None:
    row = conn.execute("SELECT id, role_key FROM opportunities WHERE id = ?", (oid,)).fetchone()
    return ap.get(conn, ap.role_ref(row))


def events(conn, app_id) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM application_events WHERE application_id = ? ORDER BY id", (app_id,))]


def dump(db) -> str:
    c = sqlite3.connect(db)
    try:
        return "\n".join(c.iterdump())
    finally:
        c.close()


def collect(db) -> dict:
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return build_dashboard.collect(conn, CFG)
    finally:
        conn.close()


# ------------------------------------------------------------------ 1. the store

def store_checks(tmp: Path) -> None:
    db = tmp / "old.sqlite"
    conn = rc.get_db(db)
    # The database as it stood before the board: no tables, no flag.
    conn.execute("DROP TABLE applications")
    conn.execute("DROP TABLE application_events")
    conn.execute("DELETE FROM meta WHERE key = 'migrated_applications'")
    conn.commit()
    a = put(conn, "Quality Lead", "Brackley Surgical", applied_at=day(-30), applied_cv="CV - A")
    put(conn, "Quality Lead", "Brackley Surgical", applied_at=day(-20))  # a repost, stamped later
    b = put(conn, "Software Lead", "Tamesis Surgical", applied_at=day(-3))
    p = put(conn, "Head of RA", "Quayle Health")
    saved_cv(conn, p, day(-2))
    q = put(conn, "QA Manager", "Pendle Medical")      # nothing applied, nothing saved
    conn.close()
    before_rows = dump(db).count("INSERT INTO \"opportunities\"")
    conn = rc.get_db(db)
    apps = {r["role_ref"]: dict(r) for r in conn.execute("SELECT * FROM applications")}
    ra = ap.get(conn, ap.role_ref({"id": a, "role_key": rc.role_key("Quality Lead", "Brackley Surgical")}))
    check(len(apps) == 3 and ra["stage"] == "applied" and ra["stage_at"] == day(-30)
          and ra["opportunity_id"] == a and ra["stage_by"] == "radar",
          "the migration puts each applied role in Applied once, on its first stamp, "
          "dated from applied_at, by Radar")
    rp = app_of(conn, p)
    check(rp["stage"] == "preparing" and rp["stage_at"] == day(-2) and app_of(conn, q) is None,
          "a saved tailored CV lands in Preparing dated from saved_at, and a role with "
          "neither has no application")
    ev = events(conn, ra["id"])
    check(len(ev) == 1 and ev[0]["actor"] == "radar" and ev[0]["to_stage"] == "applied",
          "and the history records it, with when and who")
    check(dump(db).count("INSERT INTO \"opportunities\"") == before_rows
          and conn.execute("SELECT COUNT(*) FROM opportunities WHERE applied_at IS NOT NULL").fetchone()[0] == 3,
          "the migration leaves every role row as it was")
    conn.close()
    snap = dump(db)
    rc.get_db(db).close()
    check(dump(db) == snap, "behind its flag, a second open changes nothing")
    conn = rc.get_db(db)
    late = put(conn, "Embedded Lead", "Kestle Respiratory", applied_at=day(-1))
    conn.close()
    conn = rc.get_db(db)
    held = app_of(conn, late) is None
    made = ap.run_rules(conn, day(0))["made"]
    check(held and made == 1 and app_of(conn, late)["stage"] == "applied",
          "a role stamped by something else after the migration waits for the scheduled "
          "rule, which makes its application; opening the database does not")
    conn.close()
    conn = rc.get_db(db)
    bad = []
    for stmt, args in (("UPDATE applications SET stage = 'maybe' WHERE id = 1", ()),
                       ("UPDATE applications SET closed_reason = 'bored' WHERE id = 1", ()),
                       ("INSERT INTO application_events (application_id, at, actor, kind)"
                        " VALUES (1, 'x', 'someone', 'stage')", ())):
        try:
            conn.execute(stmt, args)
            bad.append(stmt)
        except sqlite3.IntegrityError:
            pass
    conn.rollback()
    conn.close()
    check(not bad, "the table refuses a stage, a close reason or a who it does not know")


# ------------------------------------------------------------------ 2. Radar fills

@contextlib.contextmanager
def pointed_at(db):
    saved = serve_dashboard.ARGS
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    try:
        yield
    finally:
        serve_dashboard.ARGS = saved


def radar_fills_checks(tmp: Path) -> None:
    db = tmp / "fills.sqlite"
    conn = rc.get_db(db)
    p = put(conn, "Head of RA", "Quayle Health")
    ap.on_saved(conn, conn.execute("SELECT * FROM opportunities WHERE id = ?", (p,)).fetchone(),
                day(0))
    saved_cv(conn, p, day(0))
    conn.commit()
    check(app_of(conn, p)["stage"] == "preparing",
          "Save CV's hook puts a role not applied for in Preparing")
    src = (REPO_ROOT / "scripts" / "prepare_application.py").read_text(encoding="utf-8")
    save = src[src.index("def save_copy"):src.index("def reset_copy")]
    check("applications.on_saved(conn, current)" in save
          and save.index("applications.on_saved") < save.index("conn.commit()"),
          "and Save CV calls it in the same transaction as the save")
    lone = put(conn, "Software Lead", "Tamesis Surgical")
    real = put(conn, "Software Quality Lead", "Hartwell Infusion Ltd")
    conn.close()
    import prepare_application
    import run_cv_test
    saved = prepare_application.save_copy(real, run_cv_test.MASTER_TEXT, db, CFG)
    conn = rc.get_db(db)
    check(saved.get("ok") and saved.get("outcome") == "saved"
          and app_of(conn, real)["stage"] == "preparing"
          and events(conn, app_of(conn, real)["id"])[0]["actor"] == "radar",
          "Save CV on Prepare application, the real one, puts the role in Preparing")
    conn.close()
    with pointed_at(db):
        r1 = serve_dashboard.set_applied(p, True)
        r2 = serve_dashboard.set_applied(lone, True)
    conn = rc.get_db(db)
    ap_p, ap_l = app_of(conn, p), app_of(conn, lone)
    check(r1["ok"] and r2["ok"] and ap_p["stage"] == "applied" and ap_l["stage"] == "applied"
          and ap_p["stage_by"] == "radar" and ap_l["stage_by"] == "radar",
          "Mark applied moves it to Applied, from Preparing or from nothing, filled by Radar")
    check(events(conn, ap_p["id"])[-1]["from_stage"] == "preparing",
          "and the history says it came from Preparing")
    conn.close()
    with pointed_at(db):
        u1 = serve_dashboard.set_applied(p, False)
        u2 = serve_dashboard.set_applied(lone, False)
    conn = rc.get_db(db)
    check(u1["ok"] and u2["ok"] and app_of(conn, p)["stage"] == "preparing"
          and app_of(conn, lone)["stage"] is None
          and not rc.role_is_applied(conn, p) and not rc.role_is_applied(conn, lone),
          "Undo applied takes it back to Preparing when a CV was saved, and off the "
          "board when none was")
    conn.close()
    with pointed_at(db):
        serve_dashboard.set_applied(lone, True)
        aid = app_of(rc.get_db(db), lone)["id"]
        moved = serve_dashboard.move_application(aid, "conversation", None, "applied")
        refused = serve_dashboard.set_applied(lone, False)
    conn = rc.get_db(db)
    check(moved["ok"] and not refused["ok"] and "Move it back to Applied" in refused["note"]
          and rc.role_is_applied(conn, lone) and app_of(conn, lone)["stage"] == "conversation",
          "once the owner has moved it on, Undo applied on Jobs is refused and says "
          "where to move it back")
    conn.close()


# ------------------------------------------------------------------ 3. the owner's moves

def move_checks(tmp: Path) -> None:
    db = tmp / "moves.sqlite"
    conn = rc.get_db(db)
    a = put(conn, "Quality Lead", "Brackley Surgical", applied_at=day(0))
    ap.on_applied(conn, a, day(0))
    conn.commit()
    aid = app_of(conn, a)["id"]
    seen = []
    for to in ("conversation", "interviewing", "offer", "applied"):
        out = ap.move(conn, aid, to, None, app_of(conn, a)["stage"], day(1))
        seen.append(out["ok"] and app_of(conn, a)["stage"] == to)
    check(all(seen), "the owner can move an item to each column")
    no_reason = ap.move(conn, aid, "closed", None, "applied", day(2))
    bad_reason = ap.move(conn, aid, "closed", "bored", "applied", day(2))
    check(not no_reason["ok"] and not bad_reason["ok"] and app_of(conn, a)["stage"] == "applied",
          "Closed is refused without a reason, or with one it does not know")
    stale = ap.move(conn, aid, "offer", None, "interviewing", day(2))
    check(not stale["ok"] and "since the page was drawn" in stale["note"],
          "a page left open from before cannot move an item that has moved since")
    same = ap.move(conn, aid, "applied", None, "applied", day(2))
    unknown = ap.move(conn, aid, "limbo", None, "applied", day(2))
    check(not same["ok"] and not unknown["ok"], "nor move it where it is, or somewhere that is not a column")
    for reason in ap.REASONS:
        out = ap.move(conn, aid, "closed", reason, app_of(conn, a)["stage"], day(3))
        check(out["ok"] and app_of(conn, a)["closed_reason"] == reason,
              f"Closed carries its reason: {reason}")
    back = ap.move(conn, aid, "applied", None, "closed", day(4))
    check(back["ok"] and app_of(conn, a)["closed_reason"] is None and back["from_reason"] == "withdrawn",
          "Undo is a move back, and the answer says where it came from to undo it again")
    ev = [e for e in events(conn, aid) if e["kind"] == "stage"]
    check(all(e["actor"] == "owner" for e in ev[1:]) and len(ev) == 1 + 4 + 3 + 1
          and all(e["at"] for e in ev),
          "every move is in the history, with when and who")
    # Preparing to a later column stamps the role; Undo back to Preparing clears it.
    p = put(conn, "Head of RA", "Quayle Health")
    saved_cv(conn, p)
    ap.on_saved(conn, conn.execute("SELECT * FROM opportunities WHERE id = ?", (p,)).fetchone())
    conn.commit()
    pid = app_of(conn, p)["id"]
    up = ap.move(conn, pid, "applied", None, "preparing", day(5))
    row = conn.execute("SELECT applied_at, applied_cv FROM opportunities WHERE id = ?", (p,)).fetchone()
    check(up["ok"] and row["applied_at"] == day(5) and row["applied_cv"].startswith("CV - Alex Rennie"),
          "a move out of Preparing into Applied stamps the role applied, with its saved CV")
    down = ap.move(conn, pid, "preparing", None, "applied", day(5))
    check(down["ok"] and not rc.role_is_applied(conn, p) and app_of(conn, p)["stage"] == "preparing",
          "and its Undo back to Preparing clears the stamp")
    nosave = put(conn, "Embedded Lead", "Kestle Respiratory", applied_at=day(0))
    ap.on_applied(conn, nosave, day(0))
    conn.commit()
    nid = app_of(conn, nosave)["id"]
    check(not ap.move(conn, nid, "preparing", None, "applied", day(6))["ok"],
          "Preparing is refused for a role with no saved CV")
    shut = put(conn, "Verification Lead", "Garside Endoscopy", closed_at=day(1))
    saved_cv(conn, shut)
    ap.on_saved(conn, conn.execute("SELECT * FROM opportunities WHERE id = ?", (shut,)).fetchone())
    conn.commit()
    sid = app_of(conn, shut)["id"]
    out = ap.move(conn, sid, "applied", None, "preparing", day(6))
    check(not out["ok"] and not rc.role_is_applied(conn, shut),
          "a role whose advert is closed cannot be stamped applied from the board")
    withdrawn = ap.move(conn, sid, "closed", "withdrawn", "preparing", day(6))
    check(withdrawn["ok"] and not rc.role_is_applied(conn, shut),
          "but a Preparing item can be withdrawn without ever being applied for")
    conn.close()


def undo_checks(tmp: Path) -> None:
    db = tmp / "undo.sqlite"
    conn = rc.get_db(db)
    p = put(conn, "Head of RA", "Quayle Health")
    saved_cv(conn, p)
    ap.on_saved(conn, conn.execute("SELECT * FROM opportunities WHERE id = ?", (p,)).fetchone(), day(0))
    conn.commit()
    pid = app_of(conn, p)["id"]
    ap.move(conn, pid, "closed", "withdrawn", "preparing", day(1))
    up = ap.move(conn, pid, "applied", None, "closed", day(2))
    stamped = rc.role_is_applied(conn, p)
    undone = ap.move(conn, pid, "closed", "withdrawn", "applied", day(2), undo=True)
    app = app_of(conn, p)
    check(up["ok"] and stamped and undone["ok"] and undone["undone"]
          and not rc.role_is_applied(conn, p) and app["stage"] == "closed"
          and app["closed_reason"] == "withdrawn" and app["stage_at"] == day(1),
          "Undo of a move that stamped a role applied takes the stamp back with it")
    a = put(conn, "Software Lead", "Tamesis Surgical", applied_at=day(0))
    ap.on_applied(conn, a, day(0))
    conn.commit()
    aid = app_of(conn, a)["id"]
    ap.run_rules(conn, day(7))
    ap.set_next(conn, aid, "", "", None, day(8))
    before = app_of(conn, a)
    ap.move(conn, aid, "closed", "no-reply", "applied", day(9))
    ap.move(conn, aid, "applied", None, "closed", day(9), undo=True)
    after = app_of(conn, a)
    check(all(before[k] == after[k] for k in ("stage", "stage_at", "stage_by", "moved_at",
                                              "follow_up_added_at", "closed_reason"))
          and ap.run_rules(conn, day(10))["follow_ups"] == 0,
          "Undo leaves the item as it was: the day it entered its column, its last "
          "movement and its Follow up record, so no second Follow up comes")
    plain = ap.move(conn, aid, "conversation", None, "applied", day(11))
    again = ap.move(conn, aid, "applied", None, "conversation", day(12))
    check(plain["ok"] and again["ok"] and not again["undone"] and app_of(conn, a)["stage_at"] == day(12),
          "a move back that is not an Undo is a move, and starts the column's days again")
    # A move into Applied stamps an open repost when the saved row's advert has gone.
    gone = put(conn, "Interim Lead", "Marlow Medical", closed_at=day(1))
    saved_cv(conn, gone)
    ap.on_saved(conn, conn.execute("SELECT * FROM opportunities WHERE id = ?", (gone,)).fetchone())
    repost = put(conn, "Interim Lead", "Marlow Medical")
    conn.commit()
    gid = app_of(conn, gone)["id"]
    out = ap.move(conn, gid, "applied", None, "preparing", day(3))
    row = conn.execute("SELECT applied_at FROM opportunities WHERE id = ?", (repost,)).fetchone()
    check(out["ok"] and row["applied_at"] == day(3) and app_of(conn, gone)["opportunity_id"] == repost,
          "a move into Applied stamps the role's open repost when the saved advert has closed")
    conn.close()


# ------------------------------------------------------------------ 4. not a closed advert

def protection_checks(tmp: Path) -> None:
    db = tmp / "protect.sqlite"
    conn = rc.get_db(db)
    a = put(conn, "Quality Lead", "Brackley Surgical", applied_at=day(0), last_seen=day(0),
            source_url="https://www.linkedin.com/jobs/view/8100001/")
    control = put(conn, "Regulatory Lead", "Orbase Diagnostics", last_seen=day(0),
                  source_url="https://www.linkedin.com/jobs/view/8100002/")
    ap.on_applied(conn, a, day(0))
    conn.commit()
    aid = app_of(conn, a)["id"]
    before = dict(conn.execute("SELECT * FROM opportunities WHERE id = ?", (a,)).fetchone())
    ok = []
    for stage, reason in (("conversation", None), ("interviewing", None), ("offer", None),
                          ("closed", "no-reply"), ("closed", "turned-down"), ("closed", "withdrawn")):
        moved = ap.move(conn, aid, stage, reason, app_of(conn, a)["stage"], day(1))["ok"]
        row = dict(conn.execute("SELECT * FROM opportunities WHERE id = ?", (a,)).fetchone())
        picked = conn.execute("SELECT COUNT(*) FROM opportunities WHERE id = ? AND "
                              + rc.NOT_APPLIED_SQL, (a,)).fetchone()[0]
        ok.append(moved and row == before and rc.role_is_applied(conn, a) and picked == 0)
    check(all(ok), "in every stage, Closed included, the role row is untouched: applied_at "
                   "stays, closed_at and expired_at are never set, and NOT_APPLIED_SQL "
                   "still leaves it out")
    due = [r["id"] for r in check_jobs.due_candidates(conn, day(60))]
    check(a not in due and control in due,
          "the page checker does not pick an applied role on the board, closed or not, "
          "and does pick the same role not applied for")
    for name in ("applications.py", "board_page.py"):
        text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        check(not re.search(r"(SET|,)\s*(closed_at|expired_at|closed_source|last_check)", text),
              f"{name} never writes closed_at, expiry or the page checker's columns")
    conn.close()


# ------------------------------------------------------------------ 5. the rule

def rule_db(tmp: Path, name: str, applied_day: float = 0, created=None):
    db = tmp / name
    conn = rc.get_db(db)
    cid = company(conn, f"Co {name}")
    a = put(conn, "Quality Lead", f"Co {name}", applied_at=day(applied_day), company_id=cid)
    ap.on_applied(conn, a, day(applied_day))
    if created:
        conn.execute("UPDATE applications SET created_at = ?", (created,))
    conn.commit()
    return conn, a, cid


def rule_checks(tmp: Path) -> None:
    conn, a, _c = rule_db(tmp, "rule.sqlite")
    app = app_of(conn, a)
    data = {"applications": ap.load(conn), "applied": []}
    m13 = board_page.board_model(data, day(13))
    m14 = board_page.board_model(data, day(14))
    html13 = board_page.item_html(m13["columns"]["applied"][0], m13)
    html14 = board_page.item_html(m14["columns"]["applied"][0], m14)
    check("item-warn" not in html13 and "No movement for 14 days. Radar closes it as no reply on 22 Oct unless it moves." in html14,
          "quiet to day 13; from day 14 the item says when Radar will close it")
    r20 = ap.run_rules(conn, day(20.9))
    check(r20["closed"] == 0 and app_of(conn, a)["stage"] == "applied", "nothing closes before day 21")
    r21 = ap.run_rules(conn, day(21))
    app = app_of(conn, a)
    last = events(conn, app["id"])[-1]
    check(r21["closed"] == 1 and app["stage"] == "closed" and app["closed_reason"] == "no-reply"
          and app["stage_by"] == "radar" and last["actor"] == "radar"
          and "21 days" in (last["note"] or ""),
          "at 21 days Radar closes it as no reply, without asking, and the history says why")
    m = board_page.board_model({"applications": ap.load(conn), "applied": []}, day(21))
    closed_html = board_page.item_html(m["columns"]["closed"][0], m)
    check("Moved here by Radar on 22 Oct after 21 days with no movement." in closed_html
          and 'data-move-to="applied"' in closed_html and "Move back to Applied" in closed_html,
          "the item says Radar closed it, and when, with a Move back to Applied button")
    check(rc.role_is_applied(conn, a), "and the role is still applied, and protected")
    back = ap.move(conn, app["id"], "applied", None, "closed", day(22))
    check(back["ok"] and ap.run_rules(conn, day(42))["closed"] == 0
          and ap.run_rules(conn, day(43))["closed"] == 1,
          "Move back to Applied starts the 21 days again")
    conn.close()

    conn, a, _c = rule_db(tmp, "step.sqlite")
    aid = app_of(conn, a)["id"]
    ap.set_next(conn, aid, "Ring the recruiter", day(12)[:10], None, day(10))
    check(ap.run_rules(conn, day(21))["closed"] == 0 and ap.run_rules(conn, day(31))["closed"] == 1,
          "a next step set counts as movement")
    conn.close()

    conn, a, cid = rule_db(tmp, "touch.sqlite")
    conn.execute("INSERT INTO touches (company, touched_at, channel, company_id) VALUES (?,?,?,?)",
                 ("Co touch.sqlite", day(15), "comment", cid))
    conn.commit()
    check(ap.run_rules(conn, day(21))["closed"] == 0 and ap.run_rules(conn, day(36))["closed"] == 1,
          "a contact logged with the company counts as movement")
    conn.close()

    conn, a, _c = rule_db(tmp, "named.sqlite")
    conn.execute("INSERT INTO touches (company, touched_at, channel) VALUES (?,?,?)",
                 ("co named.sqlite ", day(15), "other"))
    conn.commit()
    check(ap.run_rules(conn, day(21))["closed"] == 0,
          "a contact logged by company name only, as Home's Did the suggestion logs it, counts too")
    conn.close()

    conn, a, _c = rule_db(tmp, "grace.sqlite", applied_day=0, created=day(30))
    check(ap.run_rules(conn, day(31))["closed"] == 0 and ap.run_rules(conn, day(37))["closed"] == 1,
          "an application the migration made is never closed sooner than 7 days after, "
          "so the first run after the deploy does not sweep old applications unwarned")
    conn.close()

    conn, a, _c = rule_db(tmp, "follow.sqlite")
    aid = app_of(conn, a)["id"]
    r6 = ap.run_rules(conn, day(6.9))
    r7 = ap.run_rules(conn, day(7))
    app = app_of(conn, a)
    check(r6["follow_ups"] == 0 and r7["follow_ups"] == 1 and app["next_step"] == "Follow up"
          and app["next_step_due"] == day(7)[:10] and app["next_step_by"] == "radar",
          "Radar adds Follow up 7 days after Applied, due that day")
    ap.set_next(conn, aid, "", "", None, day(8))
    check(ap.run_rules(conn, day(9))["follow_ups"] == 0 and app_of(conn, a)["next_step"] is None,
          "once per stay in Applied: cleared, it does not come back")
    ap.move(conn, aid, "conversation", None, "applied", day(9))
    ap.move(conn, aid, "applied", None, "conversation", day(9))
    check(ap.run_rules(conn, day(16))["follow_ups"] == 1,
          "a new stay in Applied gets its own Follow up")
    conn.close()

    conn, a, _c = rule_db(tmp, "own.sqlite")
    aid = app_of(conn, a)["id"]
    ap.set_next(conn, aid, "Send the portfolio", day(9)[:10], None, day(2))
    ap.run_rules(conn, day(8))
    check(app_of(conn, a)["next_step"] == "Send the portfolio",
          "Follow up is never put over a step the owner set")
    ap.set_next(conn, aid, "", "", None, day(9))
    check(ap.run_rules(conn, day(10))["follow_ups"] == 0,
          "nor added once the owner has set and cleared their own in this stay")
    conn.close()

    conn, a, _c = rule_db(tmp, "dropped.sqlite")
    ap.run_rules(conn, day(7))
    ap.run_rules(conn, day(21))
    app = app_of(conn, a)
    check(app["stage"] == "closed" and app["next_step"] is None,
          "Radar's close drops Radar's own Follow up, so Move back to Applied brings no stale step")
    conn.close()

    # The signals run carries the rule.
    conn, a, _c = rule_db(tmp, "signals.sqlite", applied_day=-40, created=day(-40))
    conn.close()
    saved = check_signals.load_watchlist
    check_signals.load_watchlist = lambda: []
    out = tempfile.TemporaryFile(mode="w+")
    try:
        with contextlib.redirect_stdout(out):
            code = check_signals.main(["--dry-run", "--mock", "--db", str(tmp / "signals.sqlite")])
    finally:
        check_signals.load_watchlist = saved
    out.seek(0)
    result = json.loads(out.read())
    conn, spot_a, _c = rule_db(tmp, "spot.sqlite", applied_day=-40, created=day(-40))
    conn.close()
    saved = (check_signals.load_watchlist, check_signals.run_source)
    check_signals.load_watchlist = lambda: [{"id": "spot", "method": "html", "status": "live",
                                             "url": "https://example.invalid/"}]
    check_signals.run_source = lambda *a, **k: None
    spot_out = tempfile.TemporaryFile(mode="w+")
    try:
        with contextlib.redirect_stdout(spot_out):
            check_signals.main(["--dry-run", "--mock", "--db", str(tmp / "spot.sqlite"),
                                "--source", "spot"])
    finally:
        check_signals.load_watchlist, check_signals.run_source = saved
    spot_out.seek(0)
    spot = json.loads(spot_out.read())
    conn = rc.get_db(tmp / "spot.sqlite")
    check("applications" not in spot and app_of(conn, spot_a)["stage"] == "applied",
          "a spot check of one source (--source) leaves the board alone")
    conn.close()
    conn = rc.get_db(tmp / "signals.sqlite")
    run_note = conn.execute("SELECT note FROM runs WHERE workflow = 'signals' ORDER BY id DESC").fetchone()
    check(code == 0 and result.get("applications", {}).get("closed") == 1
          and app_of(conn, a)["stage"] == "closed"
          and "1 closed as no reply" in (run_note[0] or ""),
          "the signals run, every two hours, carries the rule and its run record says what it did")
    conn.close()


# ------------------------------------------------------------------ 6. next steps

def next_step_checks(tmp: Path) -> None:
    db = tmp / "next.sqlite"
    conn = rc.get_db(db)
    ids = []
    for n, (t, c) in enumerate((("Quality Lead", "Brackley Surgical"), ("Software Lead", "Tamesis Surgical"),
                                ("QA Manager", "Pendle Medical"))):
        oid = put(conn, t, c, applied_at=day(0))
        ap.on_applied(conn, oid, day(0))
        conn.commit()
        ids.append(app_of(conn, oid)["id"])
    bad = [ap.set_next(conn, ids[0], "Call", "", None, day(1)),
           ap.set_next(conn, ids[0], "", "2026-10-09", None, day(1)),
           ap.set_next(conn, ids[0], "Call", "9 Oct", None, day(1)),
           ap.set_next(conn, ids[0], "Call", "2026-02-30", None, day(1)),
           ap.set_next(conn, ids[0], "Call", "2026-10-09", "tomorrow", day(1)),
           ap.set_next(conn, ids[0], 7, "2026-10-09", None, day(1))]
    check(all(not b["ok"] and b.get("invalid") for b in bad),
          "a next step needs its text and a real due date, and an interview a real date and time")
    good = ap.set_next(conn, ids[0], "Reply to the recruiter", "2026-10-05", "2026-10-07T10:00", day(1))
    a0 = dict(conn.execute("SELECT * FROM applications WHERE id = ?", (ids[0],)).fetchone())
    check(good["ok"] and a0["next_step"] == "Reply to the recruiter" and a0["interview_at"] == "2026-10-07T10:00"
          and a0["next_step_by"] == "owner" and a0["moved_at"] == day(1),
          "the owner sets a next step with its due date and an interview, and it counts as movement")
    ap.set_next(conn, ids[1], "Chase the agency", "2026-10-03", None, day(1))
    ap.set_next(conn, ids[2], "Send references", "2026-10-04", None, day(1))
    ap.move(conn, ids[2], "closed", "turned-down", "applied", day(1))
    data = {"applications": ap.load(conn), "applied": []}
    model = board_page.board_model(data, day(2))
    order = [a["id"] for a in model["waiting"]]
    check(order == [ids[1], ids[0]],
          "Waiting on you lists them soonest first, and never a closed item")
    wait = board_page.waiting_html(model)
    check("Chase the agency, Tamesis Surgical, due today" in wait
          and "Reply to the recruiter, Brackley Surgical, by Mon 5 Oct, interview Wed 7 Oct, 10:00" in wait,
          "each says what, with whom and when, and links to its item")
    cleared = ap.set_next(conn, ids[1], "", "", None, day(3))
    check(cleared["ok"] and ap.load(conn) and not [a for a in ap.load(conn) if a["id"] == ids[1]][0]["next_step"],
          "and can clear it")
    closed = ap.set_next(conn, ids[2], "Ask for feedback", "2026-10-09", None, day(3))
    check(not closed["ok"], "a closed application takes no next step")
    long = ap.set_next(conn, ids[0], "x" * 500, "2026-10-09", None, day(4))
    check(long["ok"] and len(long["next_step"]) == ap.NEXT_STEP_MAX, "a long step is cut to its limit")
    conn.close()


# ------------------------------------------------------------------ 7. the page

@contextlib.contextmanager
def serving(db):
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    serve_dashboard.GUARD = serve_dashboard.WriteGuard("enforce", serve_dashboard.LOOPBACK_NAMES)
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD,
         serve_dashboard.Handler.log_message) = saved


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def seed_page(tmp: Path, name: str):
    db = tmp / name
    conn = rc.get_db(db)
    now = datetime.now(timezone.utc)
    ago = lambda d: iso(now - timedelta(days=d))  # noqa: E731
    ids = {}
    p = put(conn, "Head of RA", "Quayle Health", first_seen=ago(9))
    saved_cv(conn, p, ago(1))
    ap.on_saved(conn, conn.execute("SELECT * FROM opportunities WHERE id = ?", (p,)).fetchone(), ago(1))
    ids["preparing"] = p
    for stage, (t, c) in (("applied", ("Software Lead", "Tamesis Surgical")),
                          ("conversation", ("IEC 62304 Consultant", "Clearwater Ophthalmic")),
                          ("interviewing", ("Interim Quality Lead", "Severn Renal Systems")),
                          ("offer", ("QA/RA Lead", "Wrenfield Digital Health")),
                          ("closed", ("Verification Manager", "Ashdown Diagnostics")),
                          ("stale", ("Interim Head of Software", "Marlow Medical"))):
        oid = put(conn, t, c, applied_at=ago(16 if stage == "stale" else 5), first_seen=ago(30))
        ap.on_applied(conn, oid, ago(16 if stage == "stale" else 5))
        conn.commit()
        aid = app_of(conn, oid)["id"]
        if stage == "closed":
            conn.execute("UPDATE applications SET stage = 'closed', closed_reason = 'no-reply',"
                         " stage_by = 'radar', stage_at = ? WHERE id = ?", (ago(1), aid))
        elif stage not in ("applied", "stale"):
            conn.execute("UPDATE applications SET stage = ?, stage_by = 'owner', stage_at = ?"
                         " WHERE id = ?", (stage, ago(2), aid))
        ids[stage] = oid
    conn.execute("UPDATE applications SET created_at = ?", (ago(40),))
    conn.commit()
    conn.close()
    return db, ids


def page_checks(tmp: Path) -> None:
    db, ids = seed_page(tmp, "page.sqlite")
    with pointed_at(db):
        s_board, board_html, _b = serve_dashboard.render_jobs(None, {"show": ["applied"]})
        s_list, list_html, _l = serve_dashboard.render_jobs(None, {"show": ["applied"], "layout": ["list"]})
        s_role, role_html, _r = serve_dashboard.render_jobs(ids["applied"], {"show": ["applied"]})
        s_frag, frag, _f = serve_dashboard.render_jobs(None, {"show": ["applied"], "part": ["board"]})
        s_open, open_html, _o = serve_dashboard.render_jobs(None, {})
    board_html, list_html, role_html, open_html = (x.decode("utf-8") for x in
                                                   (board_html, list_html, role_html, open_html))
    check(s_board == 200 and 'id="board"' in board_html and 'class="queue"' not in board_html,
          "/jobs?show=applied draws the Board by default")
    check(s_list == 200 and 'id="queue"' in list_html and 'id="board"' not in list_html
          and 'aria-current="page">List' in list_html and 'aria-current="page">Board' in board_html,
          "layout=list draws the List, and the switch marks the one shown")
    check(s_role == 200 and 'id="role"' in role_html and 'id="board"' not in role_html,
          "a role's address always opens the List beside its role pane")
    heads = re.findall(r'<h2 class="col-title" id="colh-([a-z]+)"', board_html)
    check(heads == list(ap.STAGES), "six columns, Preparing to Closed, in order")
    check("Filled by Radar" not in board_html and 'class="col-note"' not in board_html,
          "no column carries a line explaining itself (owner, 17 Nov)")
    check('data-applied-count>6</span>' in board_html and 'data-applied-count>6</span>' in open_html,
          "the Applied count covers Preparing to Offer (6 here), not Closed (1)")
    check("No movement for 16 days. Radar closes it as no reply" in board_html,
          "the 21-day warning shows on the item")
    check("Moved here by Radar on" in board_html and "Move back to Applied" in board_html
          and "No reply" in board_html and "1 no reply" in board_html,
          "a Radar close says so in words, with the reason in the column head")
    check("draggable" not in board_html and "dragstart" not in board_html,
          "nothing is dragged")
    check(board_html.count('aria-haspopup="menu"') == 7 and 'id="move-menu" role="menu"' in board_html,
          "every item on file has its Move to button, a menu button")
    check("Keys:" not in board_html and "Single-key" not in board_html and "<kbd" not in board_html
          and 'id="keys-open"' not in board_html and "<dialog" not in board_html
          and "·" not in re.sub(r"<(style|script)[^>]*>.*?</>", "", board_html, flags=re.S),
          "no list of keys, no key hints and no middle dot on the board")
    check('class="picker"' in board_html and board_html.count("data-pick=") == 6,
          "the phone picker has six columns with their counts")
    check("Undo last" in board_html and "Waiting on you" in board_html,
          "Undo last and Waiting on you are on the board")
    payload = json.loads(frag)
    check(s_frag == 200 and payload["open"] == 6 and 'id="col-closed"' in payload["html"],
          "the board fragment is JSON with the board and the count")
    listed = re.findall(r'class="row row-queue row-applied" data-id="(\d+)"', list_html)
    check(listed and all(f'href="/jobs/{i}?show=applied&amp;layout=list"' in board_html for i in listed),
          f"every role on the List ({len(listed)}) has its item on the Board")
    row = re.search(rf'<div class="row row-queue row-applied" data-id="{ids["conversation"]}".*?</div>',
                    list_html, re.S)
    check(row is not None and "In conversation" in row.group(0),
          "the List says where an application stands once it has moved on")
    # Location chips filter the board's columns, the counts stay whole.
    with pointed_at(db):
        _s, filtered, _t = serve_dashboard.render_jobs(None, {"show": ["applied"], "loc": ["europe"]})
    filtered = filtered.decode("utf-8")
    check("0 of 5" in filtered or "Nothing here matches the location chips." in filtered,
          "the location chips filter the columns and say so")


# ------------------------------------------------------------------ 8. routes

def route_checks(tmp: Path) -> None:
    db, ids = seed_page(tmp, "routes.sqlite")
    conn = rc.get_db(db)
    aid = app_of(conn, ids["applied"])["id"]
    conn.close()
    with serving(db) as port:
        own = f"127.0.0.1:{port}"
        write = {"Host": own, "Origin": f"http://{own}", "X-Radar-Request": "1",
                 "Content-Type": "application/json"}
        before = dump(db)
        forged = call(port, "POST", "/application/move",
                      json.dumps({"id": aid, "to": "offer", "from": "applied"}),
                      {"Host": own, "Origin": "http://evil.example", "Content-Type": "application/json"})
        check(forged[0] == 403 and dump(db) == before,
              "a forged move is refused by the guard, and the database is as it was")
        s_bool = call(port, "POST", "/application/move", json.dumps({"id": True, "to": "offer"}), write)[0]
        s_text = call(port, "POST", "/application/move", json.dumps({"id": aid, "to": 3}), write)[0]
        s_next = call(port, "POST", "/application/next", json.dumps({"id": "1"}), write)[0]
        check(s_bool == 400 and s_text == 400 and s_next == 400, "a bad id or field is a 400")
        s1, _t, b1 = call(port, "POST", "/application/move",
                          json.dumps({"id": aid, "to": "conversation", "from": "applied"}), write)
        s2, _t, b2 = call(port, "POST", "/application/move",
                          json.dumps({"id": aid, "to": "offer", "from": "applied"}), write)
        check(s1 == 200 and json.loads(b1)["stage"] == "conversation" and s2 == 409,
              "a move is a 200, and a stale one a 409 with its reason")
        s3, _t, b3 = call(port, "POST", "/application/next",
                          json.dumps({"id": aid, "text": "Reply", "due": "", "interview": ""}), write)
        s4, _t, b4 = call(port, "POST", "/application/next",
                          json.dumps({"id": aid, "text": "Reply", "due": "2026-12-01", "interview": ""}), write)
        check(s3 == 400 and s4 == 200 and json.loads(b4)["next_step"] == "Reply",
              "a next step missing its date is a 400, a good one a 200")
        s5, _t, b5 = call(port, "GET", "/jobs?show=applied&part=board", headers={"Host": own})
        check(s5 == 200 and "Reply, Tamesis Surgical" in json.loads(b5)["html"],
              "the board fragment shows the change")


# ------------------------------------------------------------------ 9. the script

def script_checks(tmp: Path) -> None:
    node = shutil.which("node")
    if not node:
        print("NOTE  node is not installed, the script parse check is skipped")
        return
    for name, text in (("board.js", board_page.BOARD_SCRIPT), ("jobs.js", jobs_page.JOBS_SCRIPT)):
        path = tmp / name
        path.write_text(text, encoding="utf-8")
        out = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        check(out.returncode == 0, f"{name} parses ({out.stderr.strip()[:200]})")
    path = tmp / "board_script.js"
    path.write_text(board_page.BOARD_SCRIPT, encoding="utf-8")
    out = subprocess.run([node, str(TEST_DIR / "board_script_harness.js"), str(path)],
                         capture_output=True, text=True, timeout=120)
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
    for ln in lines:
        check(ln.startswith("PASS"), "the board script: " + ln[6:])
    check(out.returncode == 0 and len(lines) >= 12,
          f"the board script runs its keys, its menu and its undo in node ({out.stderr.strip()[:200]})")
    undo = re.search(r"last\.kind === 'move'.*?JSON\.stringify\((\{.*?\})\)",
                     jobs_page.JOBS_SCRIPT, re.S)
    body = re.sub(r"\s+", " ", undo.group(1)) if undo else ""
    check("to: last.from" in body and "from: last.to" in body and "undo: true" in body
          and "reason: last.fromReason" in body,
          "the List undoes a board move by moving it back, as an undo")
    run_all = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("test/run_board_test.py" in run_all, "run_all.sh runs this file")


# ------------------------------------------------------------------ 10. the box checks

def box_doc_checks(tmp: Path) -> None:
    """The deploy checks: no empty command block, every query runs here on a
    seeded database, and every python -c in them compiles."""
    doc = (REPO_ROOT / "docs" / "box-checks-applications-board.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
    check(len(blocks) >= 8 and all(b.strip() for b in blocks),
          f"the box checks have their blocks ({len(blocks)}) and none is empty")
    for name in ("## BEFORE", "## The deploy, one block at a time", "## AFTER", "## Rollback"):
        check(name in doc, f"the box checks have {name[3:]}")
    rollback = doc[doc.index("## Rollback"):]
    check("backup" in rollback and "restore" not in rollback.replace("is not restored", "")
          and "reset --hard \"$(cat .git/rollback-applications-board)\"" in rollback,
          "the rollback goes back to the saved commit and never restores the backup")
    check("check_signals" in doc and "every two hours" in doc,
          "the box checks say which scheduled run carries the 21-day rule")
    queries = re.findall(r' Q "(.*?)"(?:;|$| \|)', doc, re.M)
    db, _ids = seed_page(tmp, "doc.sqlite")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    bad = []
    for q in queries:
        try:
            conn.execute(q).fetchall()
        except sqlite3.Error as err:
            bad.append(f"{q[:60]}: {err}")
    conn.close()
    check(len(queries) >= 12 and not bad,
          f"every query in the box checks runs on a seeded database ({len(queries)}; bad: {bad})")
    snippets = re.findall(r'python -c "(.*?)"(?:;| &&| \)|$)', doc, re.M)
    broken = []
    for code in snippets:
        try:
            compile(code, "box", "exec")
        except SyntaxError as err:
            broken.append(f"{code[:50]}: {err}")
    check(len(snippets) >= 3 and not broken,
          f"every python -c in them compiles ({len(snippets)}; broken: {broken})")
    for bang in ("!",):
        check(all(bang not in b for b in blocks), "no block carries a ! that bash would expand")


def main() -> int:
    ap_ = argparse.ArgumentParser(description="Applications board checks")
    ap_.add_argument("--db", help="accepted for suite symmetry, unused")
    ap_.parse_args()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        for section in (store_checks, radar_fills_checks, move_checks, undo_checks, protection_checks,
                        rule_checks, next_step_checks, page_checks, route_checks, script_checks,
                        box_doc_checks):
            try:
                section(tmp_path)
            except Exception as err:  # noqa: BLE001  one section stopping is a failure
                import traceback
                traceback.print_exc()
                check(False, f"{section.__name__} stopped on {type(err).__name__}: {err}")
    print()
    if failures:
        print(f"FAIL. {len(failures)} check(s) failed.")
        for label in failures:
            print("  - " + label)
        return 1
    print("PASS. Every applications board check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
