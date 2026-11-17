#!/usr/bin/env python
"""Checks for the Watchlist final (design/watchlist/final and the decisions of
15 Nov 2026 in design/README.md).

No model calls and no network: RADAR_MOCK is forced on, Read again's one fetch
is stubbed, and every database is a throwaway under a temporary folder. Every
company, person, story and role here is invented, as the design's are.

1. The schema: the suggestions and people tables, the new columns, and the
   rows Set state wrote marked as state rows, which never close a thread.
2. Threads: the newest contact is the thread; overdue, today, upcoming, no
   date (an old next step with no date shows No date set and is never due),
   closed and none; threads due counts only Watchlist companies; removing
   stops it counting and adding back restores it.
3. Log a contact: a next step needs a due date, a due date needs a next step,
   empty closes the thread, no future contact, the date and what came back
   are kept, and a company not on the Watchlist joins it. Move the date,
   Clear the next step and their Undo.
4. Suggestions: buying windows, funding rounds at 40 or more and stories at
   70 or more,
   in code, never added by Radar; five at most, strongest first; folded after
   14 days; a dismissed company back once, marked, only on a stronger kind;
   a removed company only on a signal since; Accept, Dismiss, Undo and
   Suggest again.
5. By name: ignoring capitals, spacing and endings; an unknown name only
   when asked, as a bare row with nothing known; already watched.
6. From Insights: the arrival said once, labelled for the day; Insights' Undo
   leaves no removal behind. People by hand.
7. Read again: the cost first, one fetch and one call on a press, a run
   logged, each fact with its source and date, an empty new field keeping the
   old fact, and nothing spent without confirm.
8. The routes through the guard: pages, the lookup, every POST, their 400s
   and 409s, and a forged request refused with nothing written.
9. The watcher run fills the suggestions; a spot check of one source does not.
10. The page: one h1, the bar marking the Watchlist, the table, the record
    beside it and on its own, the keys and their switch, and the script.
11. The box checks: no command block is empty, every query in them runs, and
    the rollback never restores the backup.
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

import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402
import watchlist as wl  # noqa: E402
import seed_watchlist_sample as sample  # noqa: E402

failures: list[str] = []
CFG = rc.load_config()
NOW = datetime(2026, 11, 13, 9, 14, tzinfo=timezone.utc)   # Friday, as the design draws it


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(days: float = 0, hours: float = 0) -> str:
    return iso(NOW - timedelta(days=days, hours=hours))


def day(n: int) -> str:
    return (NOW + timedelta(days=n)).strftime("%Y-%m-%d")


def fresh(tmp: Path, name: str):
    path = tmp / f"{name}.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return path, rc.get_db(path)


def company(conn, name, city="Leeds", country="United Kingdom") -> int:
    cid = rc.resolve_company(conn, name, ago(40))
    conn.execute("UPDATE companies SET city = ?, country = ? WHERE id = ?", (city, country, cid))
    return cid


_n = [0]


def signal(conn, cid, name, relevance, days_ago, kind="funding", source="eu-digest", hidden=False) -> int:
    _n[0] += 1
    cur = conn.execute(
        "INSERT INTO signals (url_hash, first_seen, source_id, company, headline, relevance, why, status,"
        " company_id, story_type, acknowledged_at) VALUES (?,?,?,?,?,?,?,'new',?,?,?)",
        (f"wlt-{_n[0]}", ago(days_ago), source, name, f"{name} story {_n[0]}", relevance, "Invented.",
         cid, kind, ago(0) if hidden else None))
    return cur.lastrowid


def touch(conn, cid, name, days_ago, step=None, due=None, channel="comment", kind=None):
    conn.execute("INSERT INTO touches (company, touched_at, channel, note, next_action, next_action_date,"
                 " company_id, kind) VALUES (?,?,?,?,?,?,?,?)",
                 (name, ago(days_ago), channel, "Invented contact.", step, due, cid, kind))


def tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def cols(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


# ================================================================ 1. schema

def schema_checks(tmp: Path) -> None:
    path, conn = fresh(tmp, "schema")
    check({"watch_suggestions", "company_people"} <= tables(conn), "the suggestions and people tables are made")
    check({"watched_via", "watched_signal_id", "arrival_seen_at", "unwatched_at", "watched_was",
           "site_url", "fact_sources"} <= cols(conn, "companies") and "kind" in cols(conn, "touches"),
          "the companies and touches columns are added")
    conn.close()
    # An older database: the meta flag is gone and a Set state row is there.
    old = sqlite3.connect(path)
    old.execute("DELETE FROM meta WHERE key = 'migrated_touch_kind'")
    old.execute("INSERT INTO companies (norm_name, display_name, first_seen)"
                " VALUES ('fernhill medical', 'Fernhill Medical', ?)", (ago(9),))
    old.execute("INSERT INTO touches (company, touched_at, channel, note, next_action, next_action_date)"
                " VALUES ('Fernhill Medical', ?, 'comment', 'A comment.', 'Send the note.', ?)",
                (ago(3), day(2)))
    old.execute("INSERT INTO touches (company, touched_at, channel, note) VALUES"
                " ('Fernhill Medical', ?, 'other', 'State set to in conversation.')", (ago(1),))
    old.commit()
    old.close()
    conn = rc.get_db(path)
    kinds = [r[0] for r in conn.execute("SELECT kind FROM touches ORDER BY id")]
    check(kinds == [None, "state"], f"a Set state row on file is marked as a state row once ({kinds})")
    cid = conn.execute("SELECT id FROM companies WHERE norm_name = 'fernhill medical'").fetchone()[0]
    th = wl.thread_of(wl.contacts_for(conn, cid), NOW.date())
    check(th["state"] == "upcoming" and th["next_step"] == "Send the note.",
          "so the comment under it is still the thread")
    conn.close()


# ================================================================ 2. threads

def thread_checks(tmp: Path) -> None:
    _p, conn = fresh(tmp, "threads")
    names = {}
    for name, days_ago, step, due in (
            ("Qurio", 22, "Week three.", day(-1)), ("Lumenhal Photonics", 21, "Send the note.", day(0)),
            ("Corvane Surgical", 6, "Week three.", day(14)), ("Arvell Photonics", 15, "Look again.", None),
            ("Severn Renal Systems", 16, None, None), ("Okara Diagnostics", 8, "Follow up.", day(-3))):
        cid = company(conn, name)
        names[name] = cid
        touch(conn, cid, name, days_ago, step, due)
    names["Brackley Sensing"] = company(conn, "Brackley Sensing")
    for name in names:
        if name != "Okara Diagnostics":
            wl.watch(conn, names[name], "name", None, ago(30))
    today = NOW.date()
    states = {n: wl.thread_of(wl.contacts_for(conn, c), today)["state"] for n, c in names.items()}
    check(states == {"Qurio": "overdue", "Lumenhal Photonics": "today", "Corvane Surgical": "upcoming",
                     "Arvell Photonics": "nodate", "Severn Renal Systems": "closed",
                     "Okara Diagnostics": "overdue", "Brackley Sensing": "none"},
          f"each thread reads its state from the newest contact ({states})")
    due = wl.threads_due(conn, NOW)
    check([d["name"] for d in due] == ["Qurio", "Lumenhal Photonics"],
          f"threads due are the Watchlist's overdue and due today, most overdue first ({[d['name'] for d in due]})")
    check("Okara Diagnostics" not in [d["name"] for d in due],
          "an overdue thread at a company not on the Watchlist is not counted")
    check("Arvell Photonics" not in [d["name"] for d in due],
          "an existing next step with no date is not due")
    rows = {r["name"]: r for r in wl.company_rows(conn, NOW)}
    check(rows["Arvell Photonics"]["thread"]["state"] == "nodate", "and the table reads it as no date")
    order = [r["name"] for r in wl.company_rows(conn, NOW)]
    check(order[:4] == ["Qurio", "Lumenhal Photonics", "Corvane Surgical", "Arvell Photonics"],
          f"the table sorts overdue, today, the next dates, then no date ({order})")
    wl.unwatch(conn, names["Qurio"], NOW)
    check([d["name"] for d in wl.threads_due(conn, NOW)] == ["Lumenhal Photonics"],
          "removing a company stops its thread counting")
    check(wl.contacts_for(conn, names["Qurio"]), "and its thread stays on file")
    wl.restore(conn, names["Qurio"], NOW)
    check(len(wl.threads_due(conn, NOW)) == 2, "adding it back restores it")
    since = conn.execute("SELECT watched_at FROM companies WHERE id = ?", (names["Qurio"],)).fetchone()[0]
    check(since == ago(30), "with the date it first joined")
    # A Set state row on top of an open thread leaves it open.
    touch(conn, names["Corvane Surgical"], "Corvane Surgical", 0, channel="other", kind="state")
    check(wl.thread_of(wl.contacts_for(conn, names["Corvane Surgical"]), today)["state"] == "upcoming",
          "a state change is not a contact and leaves the thread as it was")
    # A touch stored with no company id (the signal Done booking) is found by name.
    conn.execute("INSERT INTO touches (company, touched_at, channel, note, next_action, next_action_date)"
                 " VALUES ('brackley sensing', ?, 'comment', 'Did the suggestion.', 'Week three.', ?)",
                 (ago(0, 1), day(0)))
    check(wl.thread_of(wl.contacts_for(conn, names["Brackley Sensing"]), today)["state"] == "today",
          "a touch stored without a company id is matched on the normalised name")
    # UK day: 23:30 UTC on the 12th is already the 12th in the UK in winter; a due date of tomorrow
    # is not due today.
    check(wl.thread_of([{"next_action": "x", "next_action_date": day(1), "touched_at": ago(1)}],
                       today)["due"] is False, "a thread due tomorrow is not due")
    conn.close()


# ================================================================ 3. log a contact

def log_checks(tmp: Path) -> None:
    _p, conn = fresh(tmp, "log")
    h = company(conn, "Hartwell Infusion Ltd", "Guildford")
    o = company(conn, "Okara Diagnostics", "Cork", "Ireland")
    wl.watch(conn, h, "name", None, ago(30))
    base = {"date": day(0), "channel": "connection-note", "note": "Sent the note.", "outcome": "none"}
    r = wl.log_contact(conn, h, {**base, "next_step": "Follow up if Rosa has not replied."}, NOW)
    check(not r["ok"] and r["field"] == "due" and "due date" in r["note"],
          "a next step without a due date is refused, on the due date field")
    r = wl.log_contact(conn, h, {**base, "due": day(7)}, NOW)
    check(not r["ok"] and r["field"] == "next_step", "a due date without a next step is refused")
    r = wl.log_contact(conn, h, {**base, "date": day(1)}, NOW)
    check(not r["ok"] and r["field"] == "date", "a contact in the future is refused")
    r = wl.log_contact(conn, h, {**base, "channel": "carrier pigeon"}, NOW)
    check(not r["ok"] and r["field"] == "channel", "a channel not on the list is refused")
    r = wl.log_contact(conn, h, {**base, "date": day(-2), "next_step": "Chase.", "due": day(-3)}, NOW)
    check(not r["ok"] and r["field"] == "due", "a due date before the contact is refused")
    check(conn.execute("SELECT COUNT(*) FROM touches").fetchone()[0] == 0, "and nothing was written")
    r = wl.log_contact(conn, h, {**base, "next_step": "Follow up if Rosa has not replied.", "due": day(7),
                                 "outcome": "reply"}, NOW)
    t = conn.execute("SELECT * FROM touches WHERE id = ?", (r["touch_id"],)).fetchone()
    check(r["ok"] and r["thread"]["state"] == "upcoming" and t["next_action_date"] == day(7)
          and t["outcome"] == "reply" and t["company_id"] == h and t["touched_at"] == iso(NOW),
          "a contact logged today keeps its next step, due date, outcome and company")
    r = wl.log_contact(conn, h, {**base, "date": day(-20), "next_step": "Old step.", "due": day(-19)}, NOW)
    check(not r["ok"] and r["field"] == "next_step" and "newest contact" in r["note"],
          "a contact before the newest one cannot set the next step, which would never show")
    r = wl.log_contact(conn, h, {**base, "date": day(-20)}, NOW)
    th = wl.thread_of(wl.contacts_for(conn, h), NOW.date())
    check(r["ok"] and th["next_step"] == "Follow up if Rosa has not replied.",
          "a contact logged for an earlier day is kept and does not replace the newer thread")
    check(conn.execute("SELECT touched_at FROM touches WHERE id = ?", (r["touch_id"],)).fetchone()[0]
          == f"{day(-20)}T12:00:00Z", "and carries the day it happened")
    r = wl.log_contact(conn, h, {**base, "note": ""}, NOW + timedelta(minutes=5))
    check(r["ok"] and r["thread"]["state"] == "closed", "leaving the next step empty closes the thread")
    check(not wl.is_watched(conn, o), "Okara is not on the Watchlist yet")
    r = wl.log_contact(conn, o, {**base, "next_step": "Send the article.", "due": day(3)}, NOW)
    via = conn.execute("SELECT watched_via FROM companies WHERE id = ?", (o,)).fetchone()[0]
    check(r["ok"] and r["added"] and wl.is_watched(conn, o) and via == "contact",
          "logging a contact for a company not on the Watchlist adds it, by contact")
    # Move the date, Clear the next step, and their Undo.
    r = wl.set_thread(conn, o, "Send the article.", day(5), NOW)
    check(r["ok"] and r["thread"]["due_date"] == day(5) and r["before"]["due"] == day(3), "Move the date")
    r = wl.set_thread(conn, o, "Send the article.", "", NOW)
    check(not r["ok"] and r["field"] == "due", "moving to no date is refused")
    r = wl.set_thread(conn, o, "", "", NOW)
    check(r["ok"] and r["thread"]["state"] == "closed", "Clear the next step closes the thread")
    r = wl.set_thread(conn, o, r["before"]["next_step"], r["before"]["due"], NOW, restore=True)
    check(r["ok"] and r["thread"]["state"] == "upcoming", "and Undo puts it back")
    # An old next step with no date comes back as it was on Undo.
    a = company(conn, "Arvell Photonics", "Ghent", "Belgium")
    touch(conn, a, "Arvell Photonics", 15, "Look again.", None)
    r = wl.set_thread(conn, a, "", "", NOW)
    r = wl.set_thread(conn, a, r["before"]["next_step"], r["before"]["due"], NOW, restore=True)
    check(r["ok"] and r["thread"]["state"] == "nodate", "Undo of a cleared undated step puts it back undated")
    r = wl.set_thread(conn, a, "Sneaky.", day(-30), NOW, restore=True)
    check(not r["ok"] and r["field"] == "due", "Undo cannot set a due date before the contact")
    tid = conn.execute("SELECT MAX(id) FROM touches").fetchone()[0]
    check(wl.set_outcome(conn, tid, "conversation")["ok"] and not wl.set_outcome(conn, tid, "maybe")["ok"],
          "what came back changes to an outcome on the list only")
    conn.close()


# ================================================================ 4. suggestions

def suggestion_checks(tmp: Path) -> None:
    _p, conn = fresh(tmp, "suggest")
    c = {n: company(conn, n) for n in ("Okara Diagnostics", "PDX Diagnostics", "Vantage Bio", "Low Story",
                                       "Hidden Story", "Old Funding", "Watched Co", "Dead Co", "Edge Story",
                                       "Low Funding")}
    signal(conn, c["Okara Diagnostics"], "Okara Diagnostics", 90, 1, kind="buying-window", source="job-advert")
    signal(conn, c["PDX Diagnostics"], "PDX Diagnostics", 40, 1, kind="funding")
    signal(conn, c["Low Funding"], "Low Funding", 39, 1, kind="funding")
    signal(conn, c["Vantage Bio"], "Vantage Bio", 72, 3, kind="accelerator")
    signal(conn, c["Low Story"], "Low Story", 69, 1, kind="market")
    signal(conn, c["Edge Story"], "Edge Story", 70, 2, kind="market")
    signal(conn, c["Hidden Story"], "Hidden Story", 95, 1, kind="market", hidden=True)
    signal(conn, c["Old Funding"], "Old Funding", 90, 31, kind="funding")
    signal(conn, c["Watched Co"], "Watched Co", 95, 1, kind="funding")
    signal(conn, c["Dead Co"], "Dead Co", 95, 1, kind="funding")
    wl.watch(conn, c["Watched Co"], "name", None, ago(10))
    conn.execute("UPDATE companies SET state = 'dead' WHERE id = ?", (c["Dead Co"],))
    unknown = rc.resolve_company(conn, "Unknown company")
    signal(conn, unknown, "Unknown company", 99, 1, kind="funding")
    watched_before = conn.execute("SELECT COUNT(*) FROM companies WHERE watched_at IS NOT NULL").fetchone()[0]
    r = wl.refresh_suggestions(conn, NOW)
    s = wl.suggestions(conn, NOW)["waiting"]
    names = [x["name"] for x in s]
    check(names == ["Okara Diagnostics", "PDX Diagnostics", "Vantage Bio", "Edge Story"],
          f"a buying window, a funding round at 40 and stories at 70 or more, strongest kind first ({names})")
    check("Low Funding" not in names and wl.signal_kind({"story_type": "funding", "relevance": 39}) is None
          and wl.signal_kind({"story_type": "funding", "relevance": 40}) == "funding",
          "a funding round at 39 suggests nothing; at 40 it does")
    check("Low Story" not in names and "Hidden Story" not in names and "Old Funding" not in names,
          "not a story at 69, a hidden story, or a signal over 30 days old")
    check("Watched Co" not in names and "Dead Co" not in names and "Unknown company" not in names,
          "not a company already watched, one marked Not pursuing, or an unnamed one")
    check(conn.execute("SELECT COUNT(*) FROM companies WHERE watched_at IS NOT NULL").fetchone()[0]
          == watched_before and r["made"] == 4, "Radar never adds one itself")
    check(s[0]["href"] == "/jobs" and s[0]["is_advert"] and s[1]["href"].startswith("/insights/story/"),
          "a buying window opens on Jobs, a story on Insights")
    # Five at most, strongest first.
    more = [company(conn, f"Extra {i}") for i in range(3)]
    for i, cid in enumerate(more):
        signal(conn, cid, f"Extra {i}", 80 + i, 1, kind="market")
    r = wl.refresh_suggestions(conn, NOW)
    waiting = wl.suggestions(conn, NOW)["waiting"]
    check(len(waiting) == 5 and r["made"] == 1 and r["held"] == 2,
          f"at most five wait ({len(waiting)}, made {r['made']}, held {r['held']})")
    check(waiting[-1]["name"] in ("Extra 2", "Edge Story") and "Extra 2" in [w["name"] for w in waiting],
          "the strongest of the new ones takes the free place")
    # Accept and Undo; Dismiss and Undo.
    okara = next(w for w in waiting if w["name"] == "Okara Diagnostics")
    r = wl.answer_suggestion(conn, okara["id"], "accept", NOW)
    via = conn.execute("SELECT watched_via, watched_signal_id FROM companies WHERE id = ?",
                       (okara["company_id"],)).fetchone()
    check(r["ok"] and via[0] == "suggestion" and via[1], "Add to Watchlist on a suggestion adds it, from a suggestion")
    check(not wl.answer_suggestion(conn, okara["id"], "dismiss", NOW)["ok"], "an answered suggestion takes no second answer")
    wl.answer_suggestion(conn, okara["id"], "undo", NOW)
    check(not wl.is_watched(conn, okara["company_id"])
          and conn.execute("SELECT unwatched_at FROM companies WHERE id = ?", (okara["company_id"],)).fetchone()[0]
          is None, "Undo takes it off again, with no removal recorded")
    added = wl.add_by_name(conn, company_id=okara["company_id"], now=NOW)
    gone = "Okara Diagnostics" not in [x["name"] for x in wl.suggestions(conn, NOW)["waiting"]]
    stale = wl.unwatch(conn, okara["company_id"], NOW, undo_add=True, since=ago(99))
    check(not stale["ok"] and wl.is_watched(conn, okara["company_id"]),
          "an Undo for another add is refused and the company stays on the Watchlist")
    wl.unwatch(conn, okara["company_id"], NOW, undo_add=True, since=added["since"])
    check(gone and "Okara Diagnostics" in [x["name"] for x in wl.suggestions(conn, NOW)["waiting"]],
          "adding a suggested company by name answers its suggestion, and Undo puts it back waiting")
    vb = next(w for w in waiting if w["name"] == "Vantage Bio")
    wl.answer_suggestion(conn, vb["id"], "dismiss", NOW)
    d = wl.suggestions(conn, NOW)
    check("Vantage Bio" in [x["name"] for x in d["dismissed"]] and "Vantage Bio" not in
          [x["name"] for x in d["waiting"]], "Dismiss moves it to the Dismissed fold")
    # A story of the same kind does not bring it back; a stronger kind does, once, marked.
    signal(conn, c["Vantage Bio"], "Vantage Bio", 88, 0, kind="market")
    wl.refresh_suggestions(conn, NOW)
    check("Vantage Bio" not in [x["name"] for x in wl.suggestions(conn, NOW)["waiting"]],
          "a dismissed company does not come back on another story")
    # Free a place, then a funding round.
    for w in wl.suggestions(conn, NOW)["waiting"]:
        if w["name"].startswith("Extra"):
            wl.answer_suggestion(conn, w["id"], "dismiss", NOW)
    # Undo never takes the waiting suggestions past five.
    full = wl.suggestions(conn, NOW)["waiting"]
    extra_c = company(conn, "Cap Filler")
    signal(conn, extra_c, "Cap Filler", 85, 0, kind="market")
    wl.refresh_suggestions(conn, NOW)
    if wl.waiting_count(conn) >= wl.WAITING_MAX:
        r = wl.answer_suggestion(conn, vb["id"], "undo", NOW)
        check(not r["ok"] and "already waiting" in r["note"], "Undo of a dismiss never makes six wait")
    else:
        check(False, f"the cap test needed five waiting ({len(full)})")
    cf = next(w for w in wl.suggestions(conn, NOW)["waiting"] if w["name"] == "Cap Filler")
    wl.answer_suggestion(conn, cf["id"], "dismiss", NOW)
    fid = signal(conn, c["Vantage Bio"], "Vantage Bio", 40, 0, kind="funding")
    wl.refresh_suggestions(conn, NOW)
    back = next((x for x in wl.suggestions(conn, NOW)["waiting"] if x["name"] == "Vantage Bio"), None)
    check(back is not None and back["came_back"] and back["kind"] == "funding",
          "a stronger kind of signal brings a dismissed company back once, marked")
    wl.answer_suggestion(conn, back["id"], "dismiss", NOW)
    signal(conn, c["Vantage Bio"], "Vantage Bio", 90, 0, kind="buying-window", source="job-advert")
    wl.refresh_suggestions(conn, NOW)
    check("Vantage Bio" not in [x["name"] for x in wl.suggestions(conn, NOW)["waiting"]],
          "and never a second time")
    check(fid, "the funding round is on file")
    # Fourteen days unanswered folds it quietly.
    later = NOW + timedelta(days=14, minutes=1)
    r = wl.refresh_suggestions(conn, later)
    gone = conn.execute("SELECT COUNT(*) FROM watch_suggestions WHERE dismissed_by = 'radar'").fetchone()[0]
    check(r["folded"] >= 1 and gone == r["folded"], f"a suggestion unanswered for 14 days folds into Dismissed ({gone})")
    check(not wl.suggestions(conn, later)["waiting"] or all(
        w["suggested_at"] >= iso(later - timedelta(days=14)) for w in wl.suggestions(conn, later)["waiting"]),
        "nothing older than 14 days still waits")
    # Suggest again.
    pdx = next(x for x in wl.suggestions(conn, later)["dismissed"] if x["name"] == "PDX Diagnostics")
    r = wl.answer_suggestion(conn, pdx["id"], "again", later)
    check(r["ok"] and "PDX Diagnostics" in [x["name"] for x in wl.suggestions(conn, later)["waiting"]],
          "Suggest again puts a dismissed one back")
    # A removed company comes back only on a signal since its removal.
    wc = c["Watched Co"]
    wl.unwatch(conn, wc, later)
    wl.refresh_suggestions(conn, later)
    check("Watched Co" not in [x["name"] for x in wl.suggestions(conn, later)["waiting"]],
          "a company removed is not suggested on the signal it already had")
    signal(conn, wc, "Watched Co", 75, -15, kind="market")      # found after the removal
    wl.refresh_suggestions(conn, later + timedelta(minutes=1))
    check("Watched Co" in [x["name"] for x in wl.suggestions(conn, later)["waiting"]],
          "but is on a new signal since")
    # Dismissed, then added by the owner, then removed: a new signal can suggest it again.
    lt = company(conn, "Late Twice")
    signal(conn, lt, "Late Twice", 80, -14, kind="market")
    t2 = later + timedelta(minutes=2)
    for w in wl.suggestions(conn, t2)["waiting"]:
        if w["name"] != "Late Twice":
            wl.answer_suggestion(conn, w["id"], "dismiss", t2)
    wl.refresh_suggestions(conn, t2)
    ltw = next(w for w in wl.suggestions(conn, t2)["waiting"] if w["name"] == "Late Twice")
    wl.answer_suggestion(conn, ltw["id"], "dismiss", t2)
    wl.add_by_name(conn, company_id=lt, now=t2 + timedelta(minutes=1))
    wl.unwatch(conn, lt, t2 + timedelta(minutes=2))
    signal(conn, lt, "Late Twice", 81, -16, kind="market")
    wl.refresh_suggestions(conn, t2 + timedelta(days=2))
    check("Late Twice" in [x["name"] for x in wl.suggestions(conn, t2 + timedelta(days=2))["waiting"]],
          "a company dismissed, then added and removed, is suggested again on a new signal")
    conn.close()


# ================================================================ 5. by name

def name_checks(tmp: Path) -> None:
    _p, conn = fresh(tmp, "names")
    h = company(conn, "Hartwell Infusion Ltd", "Guildford")
    company(conn, "Okara Diagnostics", "Cork", "Ireland")
    company(conn, "Okara Medical Imaging", "Lyon", "France")
    found = wl.find_names(conn, "okara")
    check([f["name"] for f in found] == ["Okara Diagnostics", "Okara Medical Imaging"], "typing finds both Okaras")
    check(wl.find_names(conn, "  HARTWELL   infusion ")[0]["id"] == h, "ignoring capitals and spacing")
    r = wl.add_by_name(conn, "hartwell infusion", now=NOW)
    check(r["ok"] and r["id"] == h and wl.is_watched(conn, h), "a name without its Ltd adds the one Radar has seen")
    r = wl.add_by_name(conn, "Hartwell Infusion Ltd", now=NOW)
    check(r["ok"] and r.get("already") and r["since"] == "13 Nov", "already watched says since when")
    r = wl.add_by_name(conn, "Okara", now=NOW)
    check(not r["ok"] and r.get("not_found"), "a name that only starts a company's name is not a match")
    r = wl.add_by_name(conn, "Halden Medical", now=NOW)
    check(not r["ok"] and r.get("not_found") and not conn.execute(
        "SELECT 1 FROM companies WHERE norm_name = 'halden medical'").fetchone(),
        "an unknown name is not added without being asked, and no row is made")
    r = wl.add_by_name(conn, "Halden Medical", anyway=True, now=NOW)
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (r["id"],)).fetchone()
    check(r["ok"] and r["unknown"] and row["watched_via"] == "name" and row["enriched_at"] is None
          and row["what_they_build"] is None, "added anyway, it joins as a bare row whose facts stay Not known")
    rec = wl.record(conn, r["id"], CFG, NOW)
    check(all(not f["value"] for f in rec["facts"]), "the record shows every fact as not known")
    check(not wl.add_by_name(conn, "", now=NOW)["ok"] and not wl.add_by_name(conn, "Unknown company", now=NOW)["ok"],
          "an empty name or a placeholder is refused")
    conn.close()


# ================================================================ 6. Insights and people

def arrival_checks(tmp: Path) -> None:
    _p, conn = fresh(tmp, "arrival")
    s = company(conn, "Selwick Photonics", "Enschede", "Netherlands")
    sid = signal(conn, s, "Selwick Photonics", 76, 0, kind="spin-off")
    wl.watch(conn, s, "insights", sid, NOW - timedelta(minutes=3))
    m = wl.build_model(conn, CFG, NOW)
    check([a["name"] for a in m["arrivals"]] == ["Selwick Photonics"] and m["arrivals"][0]["story"]["id"] == sid,
          "an add from Insights is said on the next visit, with its story")
    row = m["rows"][0]
    check(row["arrival"] and row["arrival"]["via"] == "insights", "and the row is labelled for the day")
    wl.mark_arrivals_seen(conn, [s], NOW)
    m = wl.build_model(conn, CFG, NOW)
    check(not m["arrivals"] and m["rows"][0]["arrival"], "the arrival is said once; the label stays for the day")
    check(not wl.build_model(conn, CFG, NOW + timedelta(days=1))["rows"][0]["arrival"], "and goes the next day")
    since = conn.execute("SELECT watched_at FROM companies WHERE id = ?", (s,)).fetchone()[0]
    wl.unwatch(conn, s, NOW, undo_add=True, since=since)
    row = conn.execute("SELECT watched_at, watched_via, unwatched_at FROM companies WHERE id = ?", (s,)).fetchone()
    check(tuple(row) == (None, None, None), "Insights' Undo puts it back as it was, with no removal recorded")
    p = wl.add_person(conn, s, "Imke Vos", "Head of Quality", NOW)
    check(p["ok"] and not wl.add_person(conn, s, "", "CTO", NOW)["ok"]
          and not wl.add_person(conn, s, "Someone", "", NOW)["ok"], "a person needs a name and a title")
    people = wl.record(conn, s, CFG, NOW)["people"]
    check([(x["name"], x["title"], x["hand"]) for x in people] == [("Imke Vos", "Head of Quality", True)],
          "a person added by hand is on the record, marked as added by you")
    wl.remove_person(conn, p["id"], now=NOW)
    check(not wl.record(conn, s, CFG, NOW)["people"], "removing hides them")
    wl.remove_person(conn, p["id"], undo=True)
    check(wl.record(conn, s, CFG, NOW)["people"], "and Undo brings them back")
    conn.close()


# ================================================================ 7. Read again

def reread_checks(tmp: Path) -> None:
    import enrich_company
    path, conn = fresh(tmp, "reread")
    v = company(conn, "Veltrix Diagnostics", "", "")
    conn.execute("UPDATE companies SET what_they_build = 'Old words.', enriched_at = ?, enrich_status = 'ok',"
                 " ecosystem = 'KU Leuven' WHERE id = ?", (ago(30), v))
    conn.execute("INSERT INTO signals (url_hash, first_seen, source_id, company, headline, summary, source_url,"
                 " relevance, status, company_id) VALUES ('rr1', ?, 'eu-digest', 'Veltrix Diagnostics',"
                 " 'Veltrix raises a Series A', 'See https://veltrix.example/news', 'https://news.example/1',"
                 " 60, 'new', ?)", (ago(2), v))
    conn.commit()
    cost = wl.reread_cost(conn, CFG)
    check(cost["low"] > 0 and cost["high"] > cost["low"] and "cents" in cost["words"],
          f"the cost is worked out before anything is spent ({cost['words']})")
    calls = {"robots": 0, "get": 0}
    saved = (rc.robots_allowed, rc.http_get)

    def robots(url, ua):
        calls["robots"] += 1
        return True

    def get(url, config=None, **_kw):
        calls["get"] += 1
        return 200, {}, "<html><body>Veltrix makes analysers.</body></html>"
    rc.robots_allowed, rc.http_get = robots, get
    try:
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        r = wl.reread(conn, v, CFG, NOW)
    finally:
        rc.robots_allowed, rc.http_get = saved
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (v,)).fetchone()
    sources = json.loads(row["fact_sources"])
    check(r["ok"] and calls["get"] == 1 and row["site_url"] == "https://veltrix.example/news",
          f"one polite fetch of the site the story names ({calls})")
    check(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs + 1
          and conn.execute("SELECT workflow, mode FROM runs ORDER BY id DESC").fetchone()[:] == ("enrich", "mock"),
          "one model call, logged as an enrich run")
    check(row["what_they_build"].startswith("Point of care") and sources["what_they_build"]["from"] == "site"
          and sources["what_they_build"]["label"] == "Company site, veltrix.example"
          and sources["stage"]["from"] == "item" and sources["stage"]["label"].startswith("The story"),
          "each fact keeps its own source and date")
    check(row["enriched_at"] > ago(1), "the facts carry the new date")
    # A thinner reading keeps what was there.
    conn.execute("UPDATE signals SET summary = '', source_url = '' WHERE company_id = ?", (v,))
    conn.execute("UPDATE companies SET site_url = NULL, display_name = 'Veltrix Diagnostics' WHERE id = ?", (v,))
    conn.commit()
    saved_mock = enrich_company.get_mock_fn
    enrich_company.get_mock_fn = lambda: (lambda _u: json.dumps({"what_they_build": "", "stage": "",
                                                                  "country": "", "city": "", "ecosystem": "",
                                                                  "software_content": "", "people": []}))
    try:
        r2 = wl.reread(conn, v, CFG, NOW)
    finally:
        enrich_company.get_mock_fn = saved_mock
    row2 = conn.execute("SELECT * FROM companies WHERE id = ?", (v,)).fetchone()
    check(r2["ok"] and row2["what_they_build"] == row["what_they_build"]
          and json.loads(row2["fact_sources"])["what_they_build"] == sources["what_they_build"],
          "a field the new reading leaves empty keeps the old fact and its source")
    saved_mock = enrich_company.get_mock_fn
    enrich_company.get_mock_fn = lambda: (lambda _u: "not json at all")
    try:
        r4 = wl.reread(conn, v, CFG, NOW)
    finally:
        enrich_company.get_mock_fn = saved_mock
    row4 = conn.execute("SELECT * FROM companies WHERE id = ?", (v,)).fetchone()
    check(not r4["ok"] and row4["enriched_at"] == row2["enriched_at"] and row4["enrich_status"] == row2["enrich_status"],
          "an unreadable reply to Read again leaves the earlier reading, its date and status as they were")
    empty = company(conn, "Nothing Known Ltd")
    conn.commit()
    r3 = wl.reread(conn, empty, CFG, NOW)
    check(not r3["ok"] and "no story, advert or site" in r3["note"],
          "with nothing to read from, Read again spends nothing and says why")
    conn.close()
    # Through the route: no confirm, no spend.
    with serving(path) as port:
        own = f"127.0.0.1:{port}"
        before = rc.get_db(path).execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        s, _t, b = call(port, "POST", "/watchlist/reread", json.dumps({"company_id": v}), write_headers(own))
        after = rc.get_db(path).execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        body = json.loads(b)
        check(s == 200 and body.get("confirm") and "cents" in body.get("words", "") and after == before,
              "the route without confirm only states the cost and spends nothing")


# ================================================================ 8. routes

@contextlib.contextmanager
def serving(db: Path):
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
             serve_dashboard._MIGRATED)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    serve_dashboard.GUARD = serve_dashboard.WriteGuard("enforce", serve_dashboard.LOOPBACK_NAMES)
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    serve_dashboard._MIGRATED = False
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
         serve_dashboard._MIGRATED) = saved


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def write_headers(own: str) -> dict:
    return {"Host": own, "Origin": f"http://{own}", "Content-Type": "application/json",
            serve_dashboard.WRITE_HEADER: "1"}


def seeded(tmp: Path, name: str) -> tuple:
    path = tmp / f"{name}.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    conn = rc.get_db(path)
    ids = sample.seed(conn, NOW)
    conn.close()
    return path, ids


def route_checks(tmp: Path) -> None:
    db, ids = seeded(tmp, "routes")
    h = ids["Hartwell Infusion Ltd"]
    with serving(db) as port:
        own = f"127.0.0.1:{port}"
        get = {"Host": own}
        w = write_headers(own)
        s, ctype, page = call(port, "GET", "/watchlist", headers=get)
        check(s == 200 and "text/html" in ctype and b"<h1" in page, "GET /watchlist serves the page")
        s, _c, page = call(port, "GET", f"/watchlist?company={h}", headers=get)
        check(s == 200 and b"Hartwell Infusion Ltd" in page, "?company= draws the record beside the table")
        s, _c, page = call(port, "GET", f"/watchlist/{h}", headers=get)
        check(s == 200 and b"Back to Watchlist" in page, "/watchlist/<id> is the record as its own view")
        check(call(port, "GET", "/watchlist/999999", headers=get)[0] == 404
              and call(port, "GET", "/watchlist/abc", headers=get)[0] == 404, "an unknown or odd id is a 404")
        s, _c, b = call(port, "GET", "/watchlist/find?q=okara", headers=get)
        check(s == 200 and [m["name"] for m in json.loads(b)["matches"]][:2]
              == ["Okara Diagnostics", "Okara Medical Imaging"], "the lookup finds names as typed")
        # A forged write is refused and writes nothing.
        before = rc.get_db(db).execute("SELECT COUNT(*) FROM touches").fetchone()[0]
        forged = call(port, "POST", "/touch/log", json.dumps({"company_id": h, "channel": "comment"}),
                      {"Host": own, "Origin": "http://evil.example", "Content-Type": "application/json"})
        check(forged[0] == 403 and rc.get_db(db).execute("SELECT COUNT(*) FROM touches").fetchone()[0] == before,
              "a forged contact is refused by the guard and writes nothing")
        bad = [call(port, "POST", p, b, w)[0] for p, b in (
            ("/touch/log", "not json"), ("/touch/log", json.dumps({"company_id": "3"})),
            ("/watchlist/suggestion", json.dumps({"id": 1, "action": "maybe"})),
            ("/watchlist/arrived", json.dumps({"company_ids": "1"})),
            ("/watchlist/add-name", json.dumps({"company_id": True})),
            ("/watchlist/person/remove", json.dumps({})), ("/watchlist/thread", json.dumps({"company_id": 1.5})))]
        check(bad == [400] * 7, f"a body that is not the route's shape is a 400 ({bad})")
        s, _c, b = call(port, "POST", "/touch/log", json.dumps(
            {"company_id": h, "channel": "comment", "next_step": "Chase.", "due": ""}), w)
        check(s == 409 and json.loads(b)["field"] == "due", "a next step without a date is a 409 on the due field")
        s, _c, b = call(port, "POST", "/touch/log", json.dumps(
            {"company_id": h, "channel": "connection-note", "note": "Sent the note.",
             "next_step": "Follow up.", "due": day(7)}), w)
        check(s == 200 and json.loads(b)["thread"]["due_date"] == day(7), "a contact with its next step is a 200")
        ok = ids["Okara Diagnostics"]
        s, _c, b = call(port, "POST", "/touch/log", json.dumps({"company_id": ok, "channel": "comment"}), w)
        check(s == 200 and json.loads(b)["added"], "a contact for a company not on the Watchlist adds it")
        sug = rc.get_db(db).execute("SELECT id FROM watch_suggestions WHERE accepted_at IS NULL AND"
                                    " dismissed_at IS NULL ORDER BY id").fetchone()[0]
        s1 = call(port, "POST", "/watchlist/suggestion", json.dumps({"id": sug, "action": "dismiss"}), w)[0]
        s2 = call(port, "POST", "/watchlist/suggestion", json.dumps({"id": sug, "action": "dismiss"}), w)[0]
        s3 = call(port, "POST", "/watchlist/suggestion", json.dumps({"id": sug, "action": "undo"}), w)[0]
        check((s1, s2, s3) == (200, 409, 200), f"Dismiss, again, and Undo ({s1}, {s2}, {s3})")
        s, _c, b = call(port, "POST", "/watchlist/add-name", json.dumps({"name": "Halden Medical"}), w)
        s4, _c, b4 = call(port, "POST", "/watchlist/add-name", json.dumps({"name": "Halden Medical",
                                                                            "anyway": True}), w)
        check(s == 409 and json.loads(b)["not_found"] and s4 == 200, "add by name: not found, then anyway")
        new_id = json.loads(b4)["id"]
        s5x = call(port, "POST", "/watchlist/remove", json.dumps({"company_id": new_id, "undo_add": True}), w)[0]
        check(s5x == 409 and wl.is_watched(rc.get_db(db), new_id), "an Undo without the add's own stamp is refused")
        s5 = call(port, "POST", "/watchlist/remove", json.dumps({"company_id": new_id, "undo_add": True,
                                                                 "since": json.loads(b4)["since"]}), w)[0]
        check(s5 == 200 and not wl.is_watched(rc.get_db(db), new_id), "and its Undo")
        s6 = call(port, "POST", "/watchlist/remove", json.dumps({"company_id": h}), w)[0]
        s7 = call(port, "POST", "/watchlist/restore", json.dumps({"company_id": h}), w)[0]
        check((s6, s7) == (200, 200) and wl.is_watched(rc.get_db(db), h), "Remove and its Undo")
        s8, _c, b8 = call(port, "POST", "/watchlist/thread", json.dumps({"company_id": h, "next_step": "",
                                                                         "due": ""}), w)
        check(s8 == 200 and json.loads(b8)["thread"]["state"] == "closed", "Clear the next step through the route")
        s9, _c, b9 = call(port, "POST", "/watchlist/person", json.dumps({"company_id": h, "name": "Imke Vos",
                                                                         "title": "Head of Quality"}), w)
        s10 = call(port, "POST", "/watchlist/person/remove", json.dumps({"id": json.loads(b9)["id"]}), w)[0]
        check((s9, s10) == (200, 200), "a person added by hand and removed")
        se = ids["Selwick Photonics"]
        s11, _c, b11 = call(port, "POST", "/watchlist/arrived", json.dumps({"company_ids": [se]}), w)
        check(s11 == 200 and json.loads(b11)["marked"] == 1, "the arrival is marked seen")
        # Insights' Add to Watchlist sends where it came from.
        v = ids["Vantage Bio"]
        story = rc.get_db(db).execute("SELECT id FROM signals WHERE company_id = ?", (v,)).fetchone()[0]
        s12 = call(port, "POST", "/watchlist/add", json.dumps({"company_id": v, "via": "insights",
                                                               "signal_id": story}), w)[0]
        row = rc.get_db(db).execute("SELECT watched_via, watched_signal_id FROM companies WHERE id = ?",
                                    (v,)).fetchone()
        check(s12 == 200 and tuple(row) == ("insights", story), "Add to Watchlist from Insights keeps its story")
        s, _c, page = call(port, "GET", "/watchlist", headers=get)
        check(b"Vantage Bio" in page and b"Just added from Insights" in page,
              "and the Watchlist shows the arrival")


# ================================================================ 9. the watcher run

def watcher_checks(tmp: Path) -> None:
    db, _ids = seeded(tmp, "watcher")
    conn = rc.get_db(db)
    conn.execute("DELETE FROM watch_suggestions")
    conn.commit()
    conn.close()
    sample_file = sorted((TEST_DIR / "sample_announcements").glob("*"))[0]
    out = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "check_signals.py"), "--db", str(db),
                          "--dry-run", "--inject", str(sample_file)], capture_output=True, text=True,
                         timeout=300, cwd=str(REPO_ROOT), env={**os.environ, "RADAR_MOCK": "1"})
    raw = out.stdout[out.stdout.find("{"):]
    try:
        result = json.loads(raw)
    except ValueError:
        result = {}
    n = rc.get_db(db).execute("SELECT COUNT(*) FROM watch_suggestions").fetchone()[0]
    check(out.returncode == 0 and result.get("suggestions", {}).get("made") == n and n >= 1,
          f"a watcher run fills the suggestions in code ({n}; {out.stderr.strip()[-200:]})")
    src = (REPO_ROOT / "scripts" / "check_signals.py").read_text(encoding="utf-8")
    block = src[src.index("watchlist.refresh_suggestions"):][:10]
    check("if not args.source:\n            try:\n                result[\"suggestions\"] = watchlist.refresh_suggestions"
          in src and block, "a spot check of one source leaves the suggestions alone")


# ================================================================ 10. the page

def page_checks(tmp: Path) -> None:
    try:
        import watchlist_page
    except ImportError as err:
        check(False, f"watchlist_page imports ({err})")
        return
    db, ids = seeded(tmp, "page")
    conn = rc.get_db(db)
    h = ids["Hartwell Infusion Ltd"]
    m = wl.build_model(conn, CFG, NOW, company_id=h)
    page = watchlist_page.render_page(m, None, "table")
    check(page.count("<h1") == 1, "one h1 on the table page")
    check('href="/watchlist" aria-current="page"' in page or "aria-current=\"page\">Watchlist" in page,
          "the bar marks the Watchlist as the page")
    check("Your companies" in page and "Hartwell Infusion Ltd" in page and "Qurio" in page,
          "the table lists the companies")
    check("<span>Overdue</span>" in page and "<span>Due today</span>" in page and ", no date set" in page
          and "No contact yet" in page,
          "the follow-ups say overdue, due today, no date set and no contact yet in words")
    check("<table" not in page and 'class="co-card card g-apps glow-row' in page
          and 'class="co-card card g-apps glow-row is-due' in page and 'aria-current="true"' in page,
          "each company is a card glowing teal, more strongly with a follow-up due, the selected one marked")
    title = page.split('<header class="pagehead">', 1)[1].split("</header>", 1)[0]
    check('<span class="gchip g-apps" aria-hidden="true">' in title and "<h1>Watchlist</h1>" in title
          and re.search(r'<span class="sec-count">\d+ compan(y|ies)</span>', title)
          and re.search(r'<span class="sec-count">\d+ follow-ups? due</span>', title)
          and 'placeholder="Add a company by name"' in title and "As of" not in page
          and "Times are UK time" not in page and '<p class="hint" id="add-hint"></p>' in title,
          "the title row has the teal chip, the counts as pills and the add box, and no As of line "
          "and no sentence under the box")
    cards = re.findall(r'<li class="co-card.*?</li>', page, re.S)
    one = next(c for c in cards if "Hartwell Infusion Ltd" in c)
    check('<p class="co-label fu-label">Follow-up</p>' in one and '<p class="co-label story-label">Latest story</p>' in one
          and '<p class="co-label roles-label">Roles on Jobs</p>' in one and 'class="role-pill' in one
          and 'data-focus-log href="/watchlist?company=' in one and "Relevance" not in one
          and "found" not in one and "Added from" not in page and "·" not in one,
          "a company's card has its follow-up, its latest story with no relevance or found, its roles "
          "as a pill and Log a contact, and no Added from tag")
    check("Log a contact" in page and "Drafts to copy" in page and "Read again" in page
          and m["selected"]["reread"]["words"] in page, "the record has Log a contact, the drafts and Read again with its cost")
    check("Single-key shortcuts" not in page and "<kbd" not in page and "aria-keyshortcuts" not in page
          and "radar-watchlist-keys" not in watchlist_page.WATCHLIST_SCRIPT
          and "e.key === 'Escape'" not in watchlist_page.WATCHLIST_SCRIPT.split("// ---- add a company", 1)[0],
          "no key hints, no single-key shortcuts and no switch for them, on the list or the record")
    check("Suggested by Radar" in page or "suggestions waiting" in page, "the suggestions are on the page")
    if m.get("suggestions"):
        sg = re.findall(r'<li class="sg card g-story glow-short".*?</li>', page, re.S)
        check(len(sg) == len(m["suggestions"])
              and all('class="btn btn-teal" data-sg="accept"' in c and 'class="btn btn-secondary" data-sg="dismiss"' in c
                      for c in sg)
              and '<span class="gchip g-story" aria-hidden="true">' in page.split('id="sg-h"', 1)[0][-600:]
              and 'id="sg-show"' not in page and 'id="sg-hide"' not in page and "sg-strip" not in page
              and "Relevance" not in "".join(sg),
              "each suggestion is a wide card glowing amber, Add to Watchlist in teal and Dismiss "
              "outlined, always shown, with no banner and no Show or Hide")
    else:
        check('id="sg-panel"' not in page, "with no suggestions the section is not drawn")
    check("Just added from Insights" in page and "Selwick Photonics" in page, "the arrival from Insights is said")
    rec = watchlist_page.render_page(m, None, "record")
    check(rec.count("<h1") == 1 and "Back to Watchlist" in rec, "the record on its own view: its own h1 and Back")
    empty_path, empty = fresh(tmp, "page_empty")
    e = watchlist_page.render_page(wl.build_model(empty, CFG, NOW), None, "table")
    check("No companies on your Watchlist yet." in e and "<ol" not in e,
          "an empty Watchlist says so, and no more")
    empty.close()
    bare = rc.resolve_company(conn, "Halden Medical")
    conn.commit()
    b = watchlist_page.render_page(wl.build_model(conn, CFG, NOW, company_id=bare), None, "record")
    check("Not known" in b, "a company Radar has not read shows its facts as Not known")
    js = watchlist_page.WATCHLIST_SCRIPT
    check("method: 'POST'" in js or 'method: "POST"' in js, "every write is a fetch POST, so the write header goes with it")
    node = shutil.which("node")
    if node:
        path = tmp / "watchlist.js"
        path.write_text(js, encoding="utf-8")
        out = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        check(out.returncode == 0, f"the script parses ({out.stderr.strip()[:200]})")
        harness = TEST_DIR / "watchlist_script_harness.js"
        if harness.exists():
            out = subprocess.run([node, str(harness), str(path)], capture_output=True, text=True, timeout=120)
            lines = [ln for ln in out.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
            for ln in lines:
                check(ln.startswith("PASS"), "the Watchlist script: " + ln[6:])
            check(out.returncode == 0 and lines, f"the script harness runs ({out.stderr.strip()[:200]})")
    else:
        print("NOTE  node is not installed, the script checks are skipped")
    conn.close()
    run_all = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("test/run_watchlist_test.py" in run_all, "run_all.sh runs this file")


# ================================================================ 11. the box checks

def box_doc_checks(tmp: Path) -> None:
    doc = (REPO_ROOT / "docs" / "box-checks-watchlist-final.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
    check(len(blocks) >= 6 and all(b.strip() for b in blocks),
          f"the box checks have their blocks ({len(blocks)}) and none is empty")
    for name in ("## BEFORE", "## The deploy, one block at a time", "## AFTER", "## Rollback"):
        check(name in doc, f"the box checks have {name[3:]}")
    rollback = doc[doc.index("## Rollback"):]
    check("backup" in rollback and "restore" not in rollback.replace("is not restored", "")
          and 'reset --hard "$(cat .git/rollback-watchlist-final)"' in rollback
          and "docker restart medtech-radar" in rollback,
          "the rollback goes back to the saved commit, restarts the dashboard and never restores the backup")
    gits = re.findall(r"(?:^|[;&|{(] *)(git) ", "\n".join(blocks), re.M)
    check(not gits, "every git in the blocks runs as radar")
    runs = [b for b in blocks if re.search(r'bin/python |"\$PY"', b)]
    check(runs and all("PYTHONDONTWRITEBYTECODE=1" in b for b in runs),
          "every python in the blocks runs with PYTHONDONTWRITEBYTECODE=1")
    check(all("!" not in b for b in blocks), "no block carries a ! that bash would expand")
    queries = re.findall(r' Q "(.*?)"(?:;|$| \|)', doc, re.M)
    db, _ids = seeded(tmp, "doc")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    bad = []
    for q in queries:
        try:
            conn.execute(q).fetchall()
        except sqlite3.Error as err:
            bad.append(f"{q[:60]}: {err}")
    conn.close()
    check(len(queries) >= 5 and not bad, f"every query in the box checks runs on a seeded database "
                                          f"({len(queries)}; bad: {bad})")
    snippets = re.findall(r'python -c "(.*?)"(?:;| &&| \)|$|\s*\|)', doc, re.M)
    broken = []
    for code in snippets:
        try:
            compile(code, "box", "exec")
        except SyntaxError as err:
            broken.append(f"{code[:50]}: {err}")
    check(snippets and not broken, f"every python -c in them compiles ({len(snippets)}; broken: {broken})")
    shell = [c[:40] for c in snippets if "$" in c or "`" in c]
    check(not shell, f"no python -c carries a $ or a backtick the shell would expand ({shell})")
    look = [c for c in snippets if "watchlist as wl" in c and "mode=ro" in c]
    ran = []
    for code in look:
        code = code.replace("file:db/radar.sqlite?mode=ro", f"file:{db.as_posix()}?mode=ro")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), capture_output=True,
                             text=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        ran.append(out.returncode == 0 and "threads due:" in out.stdout)
    check(look and all(ran), "the look at what the Watchlist will show runs on a seeded database")
    arch = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check("watchlist.py" in arch and "watch_suggestions" in arch, "the architecture note names the Watchlist's parts")


def main() -> int:
    ap_ = argparse.ArgumentParser(description="Watchlist final checks")
    ap_.add_argument("--db", help="accepted for suite symmetry, unused")
    ap_.parse_args()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        for section in (schema_checks, thread_checks, log_checks, suggestion_checks, name_checks,
                        arrival_checks, reread_checks, route_checks, watcher_checks, page_checks,
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
    print("PASS. Every Watchlist check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
