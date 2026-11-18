#!/usr/bin/env python
"""Acceptance checks for the CV page final (cv_screen.py, design/cv/final).

No model calls and no network. RADAR_MOCK is forced on, the master CV and the
preferences file are written to a temporary profile, every database is a
throwaway, and nothing reads the owner's own files. The rescores run on the
deterministic mock scorer.

What it checks:

- the preference writes: the four scoring lines and the location and sector
  words are written to the live file only, never the template, and every other
  byte of the file is left as it was, line feeds or CRLF; bad values, a floor
  over the pass mark, a heading or a labelled line in the prose are refused,
- each re-sort, with exact counts: the day-rate floor, the salary floor, the CV
  pass mark and the Filtered floor each move the roles they should and no
  others, a role on the boundary stays where the rule puts it, the rule turned
  off (the CV gate not judged again) moves nothing, applied roles keep their
  tier, each move is kept in score_history first, and a dry run writes nothing,
- what cannot be worked out again: a rate the advert states that Radar holds no
  figure for keeps the scorer's verdict and is counted, roles never gated are
  counted and left, and a location or sector change re-sorts nothing,
- the offer for location and sector: every open role, with its count and its
  cost, and nothing spent without the confirm, which must name the count,
- the master-change flag and Rescore affected: every role on an earlier CV in
  one confirmed run, past today's 25, on the mocks, leaving out acknowledged,
  applied and full-description roles, logged as one run, over a real server,
- the cost: the cost note's range, widened by the token use the runs table holds,
- the page: the editor contract (.cvl[data-n] numbered as cv_store.cv_lines
  numbers the text, inside the part wrappers), /cv#settings, the notice, the
  history and compare, the over-two-pages notice, the Word import's report,
- the page estimate and the design: the browser's sums are the server's under
  node, and the Word file takes the design,
- the page's script under node (test/cv_screen_harness.js): parts, Enter and
  Escape, Undo, Ctrl S, Ctrl Z, Alt Shift arrows, no single key doing anything, the
  toolbar and the tabs, Add section, Rearrange, Design, Rescore's confirm, the
  Preferences tab, the read-only older version and the phone's Edit,
- the box checks: every query in docs/box-checks-prepare-cv-final.md runs on a
  seeded database and every python -c compiles.

Exits non zero on any failure with readable FAIL lines.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["RADAR_MOCK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "test"):
    if str(REPO_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / sub))

import build_dashboard  # noqa: E402
import cv_doc  # noqa: E402
import cv_page  # noqa: E402
import cv_screen  # noqa: E402
import cv_store  # noqa: E402
import radar_common as rc  # noqa: E402
import regate_rates  # noqa: E402
import rescore  # noqa: E402
import score_item  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
HEADER = build_dashboard.WRITE_HEADER
TEMPLATE = REPO_ROOT / "config" / "preferences.template.md"
BOX_DOC = REPO_ROOT / "docs" / "box-checks-prepare-cv-final.md"


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def note(text: str) -> None:
    print("NOTE  " + text)


# The invented world of the design: the owner Alex Rennie and the employers.
MASTER = """# Alex Rennie
Software quality and regulatory leader, medical device software
Reading, Berkshire

## Summary
Software quality and regulatory leader for medical device software, 17 years in industry.
- One US regulatory clearance with software as a key part of the submission.

## Experience

### Interim Head of Software Quality, Corbel Medical, 2024 to 2026
- Brought the ventilator software file to audit readiness in seven months.
- Closed 41 software CAPAs, 12 of them from the previous notified body audit.

### Software QA Manager, Pellam Diagnostics, 2017 to 2022
- Kept SOUP records and vulnerability monitoring for 120 third-party components.

## Skills
Standards: IEC 62304, ISO 13485, ISO 14971, IEC 60601-1, IVDR, MDR.

## Education and certifications
ISO 13485:2016 lead auditor certificate, 2023.
"""
OLD = MASTER.replace("17 years", "16 years")
OLDER = "Alex Rennie\nPROFILE\nSoftware leader.\n"
V_OLDER, V_OLD, V_NOW = ("cv-20261002-102000.md", "cv-20261021-180500.md",
                         "cv-20261113-085200.md")


class Profile:
    """A temporary profile: the three versions, the preferences file, and
    every reader of them pointed at it. Undone on exit."""

    def __init__(self, crlf: bool = False, master: str = MASTER):
        self.dir = Path(tempfile.mkdtemp(prefix="radar-cvfinal-"))
        (self.dir / V_OLDER).write_text(OLDER, encoding="utf-8")
        (self.dir / V_OLD).write_text(OLD, encoding="utf-8")
        (self.dir / V_NOW).write_text(master, encoding="utf-8")
        (self.dir / "cv.active").write_text(V_NOW + "\n", encoding="utf-8")
        self.prefs = self.dir / "preferences.md"
        text = TEMPLATE.read_text(encoding="utf-8")
        data = text.replace("\n", "\r\n") if crlf else text
        self.prefs.write_bytes(data.encode("utf-8"))
        self.cfg = {**rc.load_config(), "prefs_file": str(self.prefs)}
        self.db = self.dir / "radar.sqlite"

    def __enter__(self):
        tmp = self.dir
        self.saved = (cv_store.PROFILE_DIR, score_item.PROFILE_DIR, score_item.load_cv_text,
                      score_item.get_cv_version, rc.load_config)
        cv_store.PROFILE_DIR = tmp
        score_item.PROFILE_DIR = tmp

        def from_profile(config):
            name = cv_store.active_cv_name(tmp)
            return (tmp / name).read_text(encoding="utf-8"), f"config/profile/{name}"
        score_item.load_cv_text = from_profile
        score_item.get_cv_version = lambda config: cv_store.active_cv_name(tmp) or "cv.txt"
        cfg = self.cfg
        rc.load_config = lambda: dict(cfg)
        return self

    def __exit__(self, *exc):
        (cv_store.PROFILE_DIR, score_item.PROFILE_DIR, score_item.load_cv_text,
         score_item.get_cv_version, rc.load_config) = self.saved
        with cv_screen._RUN_LOCK:
            thread = cv_screen.RUN.get("thread")
        if thread is not None:
            thread.join(30)
        cv_screen.RUN.clear()
        cv_screen.RUN["state"] = "idle"
        shutil.rmtree(self.dir, ignore_errors=True)


_N = [0]


def put(conn, **cols) -> int:
    """One opportunity row, scored, open, on the active CV unless told."""
    _N[0] += 1
    row = {"url_hash": f"h{_N[0]}", "source": "linkedin-alert", "company": f"Firm {_N[0]}",
           "title": f"Role {_N[0]}", "status": "new", "first_seen": "2026-11-01T00:00:00Z",
           "last_seen": "2026-11-01T00:00:00Z", "tier": "top", "cv_match": 80,
           "gate_sector": 1, "gate_cv": 1, "gate_location": 1, "gate_rate": 1,
           "failed_gates": "[]", "cv_version": V_NOW, "score_basis": "card",
           "scored_at": "2026-11-01T00:00:00Z", "role_key": f"role-{_N[0]}"}
    row.update(cols)
    cur = conn.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES "
                       f"({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return cur.lastrowid


def tier_of(conn, oid):
    return conn.execute("SELECT tier FROM opportunities WHERE id = ?", (oid,)).fetchone()[0]


def history_of(conn, oid):
    return [tuple(r) for r in conn.execute(
        "SELECT event, writer, tier FROM score_history WHERE opportunity_id = ?"
        " ORDER BY id", (oid,)).fetchall()]


def runs(conn, workflow):
    return [tuple(r) for r in conn.execute(
        "SELECT items_in, items_new, mode, note FROM runs WHERE workflow = ?"
        " ORDER BY id", (workflow,)).fetchall()]


# ------------------------------------------------------- the preference writes

def write_checks() -> None:
    for crlf in (False, True):
        with Profile(crlf=crlf) as p:
            raw = p.prefs.read_bytes()
            tmpl = TEMPLATE.read_bytes()
            vals = rc.clean_scoring_prefs({"day_rate_floor_gbp": "£800",
                                           "salary_floor_gbp": "85,000",
                                           "cv_pass_score": 75, "cv_floor_score": "45"}, p.cfg)
            rc.write_scoring_prefs(vals, p.cfg)
            after = p.prefs.read_bytes()
            want = (raw.replace(b"day_rate_floor_gbp: 750", b"day_rate_floor_gbp: 800")
                    .replace(b"salary_floor_gbp: 100000", b"salary_floor_gbp: 85000")
                    .replace(b"cv_pass_score: 70", b"cv_pass_score: 75")
                    .replace(b"cv_floor_score: 40", b"cv_floor_score: 45"))
            kind = "CRLF" if crlf else "line feed"
            check(after == want and TEMPLATE.read_bytes() == tmpl,
                  f"the four scoring lines are written in place and every other byte of a "
                  f"{kind} file is as it was; the template is untouched")
            check(rc.read_rate_floors(p.cfg) == {"day": 800.0, "salary": 85000.0}
                  and rc.read_score_thresholds(p.cfg) == {"pass": 75, "floor": 45},
                  f"and the gates read the new values back from the {kind} file")
            before = p.prefs.read_bytes()
            body = "Remote in the UK, or hybrid within 90 minutes of Reading.\nEurope considered."
            rc.write_pref_section("location", rc.clean_pref_section("location", body), p.cfg)
            got = p.prefs.read_bytes()
            nl = b"\r\n" if crlf else b"\n"
            old = re.search(rb"## Location and travel\.\r?\n(.*?)## Sectors", before, re.S).group(1)
            new = nl + body.replace("\n", nl.decode()).encode() + nl + nl
            check(got == before.replace(old, new) and rc.read_pref_section("location", p.cfg) == body,
                  f"the location words replace that section alone, its heading and the rest "
                  f"of the {kind} file kept byte for byte")
            rc.write_pref_section("sector", rc.clean_pref_section("sector", "Medtech and IVD first."), p.cfg)
            check(rc.read_pref_section("sector", p.cfg) == "Medtech and IVD first."
                  and rc.read_pref_section("location", p.cfg) == body
                  and b"cv_pass_score: 75" in p.prefs.read_bytes(),
                  "and the sector words their own, the other sections as they were")
    with Profile() as p:
        for values, why in (({"cv_pass_score": "abc"}, "plain number"),
                            ({"cv_pass_score": 101}, "from 0 to 100"),
                            ({"day_rate_floor_gbp": True}, "plain number"),
                            ({"cv_floor_score": 80}, "at or under the CV pass mark"),
                            ({"target_title": "x"}, "not a line this page edits"),
                            ({}, "Nothing to save")):
            try:
                rc.clean_scoring_prefs(values, p.cfg)
                check(False, f"{values} is refused")
            except ValueError as err:
                check(why in str(err), f"{values} is refused: {err}")
        for body, why in (("## Sneaky", "start with #"),
                          ("cv_pass_score: 10", "labelled names"),
                          ("  ", "empty section"), ("x" * 2001, "the most it may be")):
            try:
                rc.clean_pref_section("location", body)
                check(False, f"location words {body[:20]!r} are refused")
            except ValueError as err:
                check(why in str(err), f"location words {body[:20]!r} are refused: {why}")
        text = p.prefs.read_text(encoding="utf-8").replace(
            "Fully\nremote works well.", "Fully\nremote works well.\nsalary_floor_gbp: 90000")
        p.prefs.write_text(text, encoding="utf-8")
        held = p.prefs.read_bytes()
        try:
            rc.write_pref_section("location", "New words.", p.cfg)
            check(False, "a section holding a labelled line is not replaced")
        except ValueError as err:
            check("holds a labelled line" in str(err) and p.prefs.read_bytes() == held,
                  "a section that holds a labelled line is not replaced, so the line is never lost")
        p.prefs.unlink()
        try:
            rc.write_scoring_prefs({"cv_pass_score": "75"}, p.cfg)
            check(False, "with no live file nothing is written")
        except ValueError as err:
            check("Copy config/preferences.template.md" in str(err)
                  and TEMPLATE.read_bytes() == tmpl,
                  "with no live preferences file the page writes nothing, and never the template")


# ------------------------------------------------------------- the re-sorts

def resort_checks() -> None:
    with Profile() as p:
        p.prefs.write_bytes(p.prefs.read_bytes().replace(b"day_rate_floor_gbp: 750", b"day_rate_floor_gbp: 700")
                            .replace(b"salary_floor_gbp: 100000", b"salary_floor_gbp: 85000"))
        conn = rc.get_db(p.db)
        # Day rates of 720 pass the 700 floor, and salaries of 95,000 the 85,000 one.
        fl = {"pay_currency": "GBP", "pay_period": "day"}
        day = [put(conn, **fl, pay_min=700, pay_max=720, rate_stated=1) for _ in range(3)]
        day_edge = put(conn, **fl, pay_min=750, pay_max=750, rate_stated=1)
        day_high = put(conn, **fl, pay_min=800, pay_max=900, rate_stated=1)
        sal = [put(conn, pay_currency="GBP", pay_period="year", pay_min=90000, pay_max=95000,
                   rate_stated=1) for _ in range(2)]
        cv74 = [put(conn, cv_match=74) for _ in range(4)]
        cv75 = put(conn, cv_match=75, gate_cv_note="The CV covers the asks.")
        # A CV gate that flips while its tier stands: the Sector gate fails it
        # to Reading whatever the CV gate says.
        cv74_reading = put(conn, cv_match=74, gate_sector=0, tier="reading",
                           failed_gates='["sector"]', gate_cv_note="Passes the CV gate.")
        cv45 = [put(conn, cv_match=45, gate_cv=0, tier="reading", failed_gates='["cv"]')
                for _ in range(2)]
        # The scorer said a rate was stated and Radar holds no figure for it.
        # Rate is code's now, so both read not stated: the pass stays Top, and
        # the fail, One question away on the scorer's word, moves up to Top.
        prose_rate = put(conn, rate_stated=1, gate_rate=1)            # no figure stored
        prose_fail = put(conn, rate_stated=1, gate_rate=0, tier="question",
                         failed_gates='["rate"]', question_text="Would they move on the rate")
        ungated = put(conn, tier=None, cv_match=None, combined=55)
        applied = put(conn, **fl, pay_min=700, pay_max=720, rate_stated=1, cv_match=74,
                      applied_at="2026-11-10T09:00:00Z")
        repost = put(conn, **fl, pay_min=700, pay_max=720, rate_stated=1,
                     role_key=conn.execute("SELECT role_key FROM opportunities WHERE id = ?",
                                           (applied,)).fetchone()[0])
        closed = put(conn, **fl, pay_min=700, pay_max=720, rate_stated=1,
                     closed_at="2026-11-12T00:00:00Z")
        conn.close()
        raw = p.prefs.read_bytes()
        res = cv_screen.save_preferences({"values": {"day_rate_floor_gbp": 760}, "dry_run": True},
                                         p.cfg, p.db)
        conn = rc.get_db(p.db)
        check(res["ok"] and res["moved"] == 5
              and res["moves"] == {"top to question": 4, "question to top": 1}
              and p.prefs.read_bytes() == raw and all(tier_of(conn, i) == "top" for i in day)
              and conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0] == 0
              and not runs(conn, "regate"),
              "a dry run of a day-rate floor of 760 says 5 roles would move, three at 720 and "
              "the one at 750, and the rate the scorer failed with no figure on file, and "
              "writes nothing: not the file, not a tier, not the history")
        conn.close()
        res = cv_screen.save_preferences({"values": {"day_rate_floor_gbp": 750}}, p.cfg, p.db)
        conn = rc.get_db(p.db)
        check(res["ok"] and res["moved"] == 4
              and res["moves"] == {"top to question": 3, "question to top": 1}
              and all(tier_of(conn, i) == "question" for i in day)
              and tier_of(conn, day_edge) == "top" and tier_of(conn, day_high) == "top",
              "a day-rate floor of 750 moves exactly the three roles at 720 to One question "
              "away; the role at exactly 750 meets it and stays, as does 800 to 900")
        check(rc.read_rate_floors(p.cfg)["day"] == 750
              and p.prefs.read_bytes() == raw.replace(b"day_rate_floor_gbp: 700",
                                                      b"day_rate_floor_gbp: 750"),
              "the file holds the floor the re-sort used")
        check(tier_of(conn, applied) == "top" and tier_of(conn, repost) == "top"
              and tier_of(conn, closed) == "top" and res["applied_left"] == 2,
              "the applied role and its repost keep their tier, and so does a closed role; "
              "the page counts the 2 applied it left")
        hist = history_of(conn, day[0])
        check(hist == [("replaced", "cv_preferences", "top")]
              and runs(conn, "regate")[-1][1] == 4
              and runs(conn, "regate")[-1][3].startswith("from the CV page, pass 70 floor 40"),
              "each move keeps the score it replaced in score_history first, writer "
              "cv_preferences, and the re-sort is one regate run that says it came from the CV page")
        unclear = [conn.execute("SELECT gate_rate, rate_stated, question_text FROM opportunities"
                                " WHERE id = ?", (i,)).fetchone() for i in (prose_rate, prose_fail)]
        check(res["rate_unclear"] == 2 and tier_of(conn, prose_rate) == "top"
              and tier_of(conn, prose_fail) == "top"
              and all(tuple(u) == (None, 0, None) for u in unclear)
              and "2 roles may state a rate Radar holds no figure for, so Rate reads not "
                  "stated on them." in res["note"],
              "the two roles the scorer said state a rate Radar holds no figure for read not "
              "stated, in code, and the page counts them and says so (rate_unclear 2)")
        check(res["ungated"] == 1 and tier_of(conn, ungated) is None
              and "no re-sort reaches it and nothing on this page rescores it" in res["note"]
              and "python scripts/rescore.py --regate, run by hand on the box" in res["note"]
              and "about 1 to 2 cents" in res["note"] and "left for a rescore" not in res["note"],
              "the role scored before the gates is counted and left, and the page names "
              "honestly where it is scored, the script on the box, with its cost")
        conn.close()
        res = cv_screen.save_preferences({"values": {"salary_floor_gbp": 96000}}, p.cfg, p.db)
        conn = rc.get_db(p.db)
        check(res["moved"] == 2 and all(tier_of(conn, i) == "question" for i in sal),
              "a salary floor of 96,000 moves exactly the two salaries of 90,000 to 95,000")
        conn.close()
        # The rule, broken: without judging the CV gate again a pass mark moves nothing.
        conn = rc.get_db(p.db)
        broken = regate_rates.regate(conn, p.cfg, {"day": 750, "salary": 96000}, write=False,
                                     thresholds={"pass": 75, "floor": 40})
        conn.close()
        res = cv_screen.save_preferences({"values": {"cv_pass_score": 75}}, p.cfg, p.db)
        conn = rc.get_db(p.db)
        check(broken["tier_moves_total"] == 0 and broken["cv_gate_stale"] == 5,
              "the control: with the CV gate left as stored, a pass mark of 75 would move "
              "nothing and leave 5 gates stale")
        check(res["moved"] == 4 and res["moves"] == {"top to reading": 4}
              and all(tier_of(conn, i) == "reading" for i in cv74)
              and tier_of(conn, cv75) == "top"
              and conn.execute("SELECT gate_cv FROM opportunities WHERE id = ?", (cv74[0],)).fetchone()[0] == 0,
              "with it, a pass mark of 75 moves exactly the four CV matches of 74 to Reading, "
              "their CV gate now failed, while the match of exactly 75 passes and stays")
        check(tier_of(conn, applied) == "top", "and the applied role, a CV match of 74, keeps Top")
        note_of = lambda i: tuple(conn.execute("SELECT gate_cv, gate_cv_note FROM opportunities"
                                               " WHERE id = ?", (i,)).fetchone())
        check(note_of(cv74[0]) == (0, "CV match 74 against the pass mark of 75, below, "
                                      "judged in code.")
              and note_of(cv75) == (1, "The CV covers the asks."),
              "a CV gate the re-sort fails gets code's note, naming the pass mark, so no fail "
              "mark sits beside the scorer's words that it passes; a gate that stands keeps "
              "the scorer's note")
        check(tier_of(conn, cv74_reading) == "reading"
              and note_of(cv74_reading)[0] == 0
              and note_of(cv74_reading)[1].startswith("CV match 74 against the pass mark of 75")
              # Two kept scores: the first save wrote its Rate as not stated, as it
              # has no figure on file, and this one its CV gate.
              and history_of(conn, cv74_reading) == [("replaced", "cv_preferences", "reading")] * 2,
              "a CV gate that flips while its tier stands is written with its note, and the "
              "score it replaced is kept in score_history first")
        conn.close()
        res = cv_screen.save_preferences({"values": {"cv_floor_score": 50}}, p.cfg, p.db)
        conn = rc.get_db(p.db)
        check(res["moved"] == 2 and res["moves"] == {"reading to filtered": 2}
              and all(tier_of(conn, i) == "filtered" for i in cv45)
              and conn.execute("SELECT filter_reason FROM opportunities WHERE id = ?",
                               (cv45[0],)).fetchone()[0] == "cv match under 50",
              "a Filtered floor of 50 files exactly the two CV matches of 45, naming the floor")
        again = cv_screen.save_preferences({"values": {"cv_floor_score": 50}}, p.cfg, p.db)
        check(again["moved"] == 0 and again["history_rows_written"] == 0,
              "saving the same values again moves nothing and keeps nothing twice")
        back = cv_screen.save_preferences({"values": {"cv_pass_score": 70, "cv_floor_score": 40,
                                                      "day_rate_floor_gbp": 700}}, p.cfg, p.db)
        conn.close()
        conn = rc.get_db(p.db)
        check(back["moved"] == 9 and all(tier_of(conn, i) == "top" for i in cv74 + day)
              and all(tier_of(conn, i) == "reading" for i in cv45),
              "putting the old values back moves the same roles back: 9, the four 74s and the "
              "three day rates to Top, the two 45s to Reading")
        check(conn.execute("SELECT gate_cv_note FROM opportunities WHERE id = ?", (cv74[0],))
              .fetchone()[0] == "CV match 74 against the pass mark of 70, at or above, judged in code.",
              "and code's own note follows the gate back")
        conn.close()
        bad = cv_screen.save_preferences({"values": {"cv_floor_score": 90}}, p.cfg, p.db)
        check(not bad["ok"] and bad["invalid"] and "at or under" in bad["note"],
              "a Filtered floor over the pass mark is refused before anything is written")


# --------------------------------------------- location and sector, the offer

def offer_checks() -> None:
    with Profile() as p:
        conn = rc.get_db(p.db)
        opens = [put(conn) for _ in range(5)]
        put(conn, acknowledged_at="2026-11-02T00:00:00Z")
        put(conn, applied_at="2026-11-02T00:00:00Z")
        put(conn, score_basis=rc.SCORE_BASIS_SPEC, cv_version=V_OLD)
        put(conn, closed_at="2026-11-02T00:00:00Z")
        conn.close()
        tiers = lambda: [r[0] for r in sqlite3.connect(p.db).execute(
            "SELECT tier || cv_match FROM opportunities ORDER BY id")]
        was = tiers()
        res = cv_screen.save_section({"key": "location", "text": "Remote in the UK only."}, p.cfg, p.db)
        offer = res["offer"]
        cost = cv_screen.rescore_cost(None, 5)
        check(res["ok"] and offer["scope"] == "open" and offer["count"] == 5
              and offer["applied"] == 1 and offer["spec"] == 1 and offer["acknowledged"] == 1
              and offer["cost"]["words"] == cost["words"] == "about 5 to 9 cents",
              "a location change offers a rescore of the 5 open roles, about 5 to 9 cents, "
              "and counts the applied, full-description and acknowledged roles it leaves out")
        conn = rc.get_db(p.db)
        check(tiers() == was and not runs(conn, "rescore") and not runs(conn, "regate")
              and conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0] == 0,
              "and runs nothing: no tier moves, no score is kept or made, no run is logged")
        conn.close()
        res = cv_screen.save_section({"key": "sector", "text": "Medtech first."}, p.cfg, p.db)
        check(res["ok"] and res["offer"]["count"] == 5 and "Sector gate" in res["note"],
              "a sector change says the same about the Sector gate")
        for body, want in (({"key": "location"}, False), ({"key": "rates", "text": "x"}, False)):
            got = cv_screen.save_section(body, p.cfg, p.db)
            check(got["ok"] is want and got.get("invalid"), f"{body} is refused")
        h = offer["ids_hash"]
        for body in ({}, {"confirm": False, "scope": "open", "expect": 5, "ids_hash": h},
                     {"confirm": True, "scope": "open", "ids_hash": h},
                     {"confirm": True, "scope": "all", "expect": 5, "ids_hash": h},
                     {"confirm": True, "scope": "open", "expect": True, "ids_hash": h},
                     {"confirm": True, "scope": "open", "expect": 5},
                     {"confirm": True, "scope": "open", "expect": 5, "ids_hash": "nothex"}):
            got = cv_screen.start_rescore(body, p.cfg, p.db, threading.Lock())
            check(not got["ok"] and got.get("invalid") and cv_screen.RUN["state"] == "idle",
                  f"Rescore without a confirm that names the scope, the count and the roles' "
                  f"hash starts nothing: {body}")
        got = cv_screen.start_rescore({"confirm": True, "scope": "open", "expect": 4,
                                       "ids_hash": h}, p.cfg, p.db, threading.Lock())
        cv_screen.RUN["thread"].join(30)
        st = cv_screen.rescore_status()
        conn = rc.get_db(p.db)
        check(got["ok"] and st["state"] == "refused" and st["result"]["count"] == 5
              and not runs(conn, "rescore") and tiers() == was,
              "a confirm for 4 when 5 are affected now is refused, says 5, and scores nothing")
        conn.close()
        busy = threading.Lock()
        busy.acquire()
        got = cv_screen.start_rescore({"confirm": True, "scope": "open", "expect": 5,
                                       "ids_hash": h}, p.cfg, p.db, busy)
        check(not got["ok"] and got["outcome"] == "busy",
              "and none starts while Check now holds the background lock")
        busy.release()
        got = cv_screen.start_rescore({"confirm": True, "scope": "open", "expect": 5,
                                       "ids_hash": h}, p.cfg, p.db, busy)
        cv_screen.RUN["thread"].join(60)
        st = cv_screen.rescore_status()
        conn = rc.get_db(p.db)
        done = conn.execute("SELECT COUNT(*) FROM score_history WHERE writer = 'rescore'"
                            " AND event = 'scored'").fetchone()[0]
        check(st["state"] == "done" and st["result"]["rescored"] == 5 and done == 5
              and runs(conn, "rescore")[-1][:3] == (5, 5, "mock")
              and busy.acquire(blocking=False),
              "the confirmed run scores the 5 on the mocks, records each, logs one run, and "
              "lets the lock go")
        busy.release()
        conn.close()


# --------------------------------- the master-change flag and Rescore affected

def stale_checks() -> None:
    with Profile() as p:
        conn = rc.get_db(p.db)
        old = [put(conn, cv_version=V_OLD) for _ in range(30)]
        older = [put(conn, cv_version=V_OLDER) for _ in range(8)]
        current = put(conn)
        ack = put(conn, cv_version=V_OLD, acknowledged_at="2026-11-03T00:00:00Z")
        app = put(conn, cv_version=V_OLD, applied_at="2026-11-03T00:00:00Z")
        spec = put(conn, cv_version=V_OLD, score_basis=rc.SCORE_BASIS_SPEC)
        conn.close()
        ro = cv_screen._ro(p.db)
        summary = cv_screen.affected_summary(ro, p.cfg, "stale")
        ro.close()
        check(summary["count"] == 38 and summary["spec"] == 1 and summary["applied"] == 1
              and summary["acknowledged"] == 1
              and [(g["version"], g["count"]) for g in summary["groups"]] == [(V_OLD, 30), (V_OLDER, 8)]
              and summary["cost"]["words"] == "about 41 to 69 cents",
              "after a master change 38 roles are flagged, 30 on the 21 Oct version and 8 on "
              "the 2 Oct one, about 41 to 69 cents; the acknowledged, applied and "
              "full-description roles are left out and counted")
        with serving(p.db) as port:
            status, _h, body = call(port, "GET", "/cv", headers={"Host": f"127.0.0.1:{port}"})
            page = body.decode("utf-8")
            check(status == 200 and "38 roles were scored on an earlier version of this CV." in page
                  and "30 on the 21 Oct version and 8 on the 2 Oct version" in page
                  and 'data-rescore="stale"' in page
                  and "Rescore 38 roles, about 41 to 69 cents" in page,
                  "the page's notice names the count, the versions and the cost on its button")
            status, got, _h = post(port, "/cv/rescore", {"confirm": True, "scope": "stale"})
            check(status == 400, "the route refuses a confirm without the count")
            status, got, _h = post(port, "/cv/rescore", {"confirm": True, "scope": "stale", "expect": 38,
                                                         "ids_hash": summary["ids_hash"]})
            check(status == 200 and got["outcome"] == "started" and got["count"] == 38,
                  "and starts one run for all 38 with it")
            for _ in range(200):
                status, _h, raw = call(port, "GET", "/cv/rescore/status",
                                       headers={"Host": f"127.0.0.1:{port}"})
                st = json.loads(raw)
                if st["state"] != "running":
                    break
                time.sleep(0.05)
            check(st["state"] == "done" and st["result"]["rescored"] == 38
                  and st["result"]["count"] == 38 and st["result"]["mode"] == "mock",
                  "the status says it rescored all 38 in the one run, past the old 25 a press")
            conn = rc.get_db(p.db)
            versions = {r[0] for r in conn.execute(
                "SELECT cv_version FROM opportunities WHERE id IN (%s)"
                % ",".join(str(i) for i in old + older))}
            kept = conn.execute("SELECT cv_version FROM opportunities WHERE id IN (?,?,?)",
                                (ack, app, spec)).fetchall()
            check(versions == {V_NOW} and all(k[0] == V_OLD for k in kept)
                  and runs(conn, "rescore")[-1][:3] == (38, 38, "mock")
                  and history_of(conn, current) == [],
                  "every flagged role is now scored on the active CV, the three left out are "
                  "not, the role already on it was not touched, and the run is one row in runs")
            conn.close()
            status, _h, body = call(port, "GET", "/cv", headers={"Host": f"127.0.0.1:{port}"})
            check("roles were scored on an earlier version" not in body.decode("utf-8"),
                  "afterwards the page flags nothing")
            status, got, _h = post(port, "/cv/rescore", {"confirm": True, "scope": "stale", "expect": 0,
                                                         "ids_hash": rescore.ids_hash([])})
            st = cv_screen.RUN["thread"].join(10) or cv_screen.rescore_status()
            check(st["state"] == "done" and st["result"]["count"] == 0,
                  "a confirm for none scores none")


def cost_checks() -> None:
    check(cv_screen.rescore_cost(None, 38)["words"] == "about 41 to 69 cents"
          and cv_screen.rescore_cost(None, 6)["words"] == "about 6 to 11 cents"
          and cv_screen.rescore_cost(None, 0)["words"] == "under a cent"
          and cv_screen.rescore_cost(None, 100)["words"] == "about $1.10 to $1.80",
          "the cost is the cost note's 1.1 to 1.8 cents a card score, in US cents, and in "
          "dollars from a dollar up")
    with Profile() as p:
        conn = rc.get_db(p.db)
        conn.execute("INSERT INTO runs (ts, workflow, mode, items_in, items_new, model,"
                     " input_tokens, output_tokens, cache_read_tokens) VALUES"
                     " ('2026-11-10T00:00:00Z', 'rescore', 'live', 10, 10, 'claude-sonnet-4-6',"
                     " 40000, 9000, 30000)")
        conn.execute("INSERT INTO runs (ts, workflow, mode, items_in, items_new, model,"
                     " input_tokens, output_tokens) VALUES"
                     " ('2026-11-10T00:00:00Z', 'rescore', 'mock', 10, 10, 'mock', 99999999, 9999999)")
        conn.commit()
        got = cv_screen.rescore_cost(conn, 38)
        seen = (40000 * 3 + 9000 * 15 + 30000 * 0.3) / 1_000_000 * 100 / 10
        conn.close()
        check(abs(got["per_role_seen"] - seen) < 1e-9 and got["runs_seen"] == 1
              and abs(got["high"] - 38 * seen) < 1e-9 and got["low"] == 38 * 1.1
              and got["words"] == cv_screen.cents_words(38 * 1.1, 38 * seen),
              f"a live rescore on file that cost {seen:.2f} cents a role widens the range to take "
              "it in; a mock run counts for nothing")


# ------------------------------------------------------------------ the page

def page_checks() -> None:
    with Profile() as p:
        conn = rc.get_db(p.db)
        put(conn, cv_version=V_OLD)
        conn.close()
        serve_dashboard.ARGS = argparse.Namespace(db=str(p.db), push=False, host="127.0.0.1", port=0)
        page = serve_dashboard.render_cv_page({}).decode("utf-8")
        attr = re.search(r'id="cv-editor"[^>]*data-cv="([^"]*)"', page)
        import html as htmllib
        data = json.loads(htmllib.unescape(attr.group(1)))
        lines = cv_store.cv_lines(MASTER)
        rows = re.findall(r'<div class="cvl cvl-\w+" data-n="(\d+)">', page)
        check(data["sheet"] is True and data["lines"] == lines
              and rows == [str(i) for i in range(1, len(lines) + 1)],
              "the server draws one .cvl[data-n] per line, numbered as cv_store.cv_lines "
              "numbers the master, before the script runs")
        check('id="settings"' not in page and 'id="pref-first_name"' not in page
              and 'data-pref-save="target_title"' not in page
              and "if (h === '#settings' || h === '#preferences') { location.replace('/settings'); return; }"
                  in cv_screen.CV_SCREEN_JS,
              "the Preferences tab is gone from the CV page, and /cv#settings leads to /settings")
        # That the top bar's Settings links there, on every page, is checked
        # with the header in test/run_header_test.py.
        check('id="keys-on"' not in page and "Single" not in page
              and set(re.findall(r'aria-keyshortcuts="([^"]+)"', page))
              == {"Control+S", "Control+Z", "Control+Shift+Z"}
              and not hasattr(cv_screen, "KEYS") and "Keys on this page" not in page
              and "<kbd" not in page and ">Save version</button>" in page,
              "the page has no single-key shortcuts and no switch for them, no keys panel and no "
              "Ctrl S label; Ctrl S, Ctrl Z and Ctrl Shift Z still work")
        check('<h2 class="visually-hidden" id="sheet-h">The CV sheet</h2>' in page
              and 'class="phone-note"' not in page
              and page.index('id="sheet-h"') < page.index('id="cv-sheet"')
              and page.index('id="versions-h"') < page.index('id="history"')
              and page.index('id="import-h"') < page.index('id="import"'),
              "the sheet has its own visually hidden h2, and Versions and Import their titles, "
              "with no phone note")
        live = re.search(r'<p class="visually-hidden" id="rescore-live" role="status" '
                         r'aria-live="polite"></p>', page)
        run_box = re.search(r'<section class="notice" id="rescore-run"[^>]*>', page).group(0)
        check(live is not None and "aria-live" not in run_box and "hidden" in run_box,
              "Rescore's status region is on the page, empty, from the start, outside the "
              "box that is unhidden in one step, which carries no live region of its own")
        check("Changing these changes how roles are scored." not in page
              and 'class="sheet sheet-paper"' in page and "#f8f6f1" not in CV_CSS_ONLY(),
              "the sheet is the shared light paper, no paper colour of its own, and the "
              "preferences' explanation has gone with them to Settings")
        check("1 role was scored on an earlier version of this CV." in page,
              "the earlier-CV notice is there")
        hist = re.findall(r'<p class="vfile"><span class="dv"><span class="dv-in"><span class="dv-i">Version (\d)</span>'
                          r'<span class="dv-i"><span class="visually-hidden">, </span>(cv-[\d-]+\.md)</span>', page)
        check(hist == [("3", V_NOW), ("2", V_OLD), ("1", V_OLDER)]
              and "1 open role still scored on it" in page,
              "History lists the versions newest first with their numbers and the roles still on each")
        view = serve_dashboard.render_cv_page({"view": [V_OLD]}).decode("utf-8")
        vdata = json.loads(htmllib.unescape(re.search(r'id="cv-editor"[^>]*data-cv="([^"]*)"', view).group(1)))
        check(vdata["editable"] is False and "Viewing version 2" in view
              and "16 years" in view and "Back to version 3" in view,
              "View opens an older version read only, marked, with the way back")
        comp = serve_dashboard.render_cv_page({"compare": [V_OLD]}).decode("utf-8")
        check('id="compare"' in comp and "1 line changed" in comp and "17 years" in comp,
              "Compare says what changed line by line against the active version")
        bad = serve_dashboard.render_cv_page({"view": ["../radar.yaml"]}).decode("utf-8")
        check("Viewing version" not in bad, "a view of a name that is not a version shows the master")
        rep = cv_screen.import_report("Alex Rennie\nPROFILE\nA leader.\n\nKEY SKILLS\n• IEC 62304\n", MASTER)
        check(rep["lines_read"] == 5 and rep["guesses"] == [{"from": "PROFILE", "to": "Summary"},
                                                             {"from": "KEY SKILLS", "to": "Skills"}]
              and rep["bullet_marks"] == 1 and rep["laid_text"].startswith("# Alex Rennie\n")
              and rep["against_active"]["removed"] > 0,
              "a Word import's report names its guesses, PROFILE read as Summary, KEY SKILLS as "
              "Skills, the bullet marks, and how it differs from the master")
        token = cv_store.stage("Alex Rennie\nPROFILE\nA leader.\n", p.dir)
        got = cv_store.confirm(token, p.dir, laid_out=True)
        saved = (p.dir / got["version"]).read_text(encoding="utf-8")
        check(got["laid_out"] and saved == "# Alex Rennie\n\n## Summary\nA leader.\n",
              "saving an import saves it as the editor laid it out, the text the page showed")
        token = cv_store.stage("## Skills\n- x\n", p.dir)
        got = cv_store.confirm(token, p.dir, laid_out=True)
        check(not got["laid_out"] and (p.dir / got["version"]).read_text(encoding="utf-8") == "## Skills\n- x\n",
              "an import the layout cannot take, one with no name, is kept as it was read")


def CV_CSS_ONLY() -> str:
    return cv_screen.CV_SCREEN_CSS + cv_page.CV_CSS


# --------------------------------- the page estimate, both ways, and the design

def parity_checks(tmp: Path) -> dict:
    import random
    rng = random.Random(7)
    cases = []
    for i in range(60):
        lines = ["# Name"]
        for s in range(rng.randint(1, 6)):
            lines += ["", "## Section " + str(s)]
            for e in range(rng.randint(0, 5)):
                lines += ["", "### Entry " + "w " * rng.randint(1, 40)]
                lines += [("- " if rng.random() < .7 else "") + "word " * rng.randint(1, 60)
                          for _ in range(rng.randint(0, 9))]
        design = {k: rng.choice(v) for k, v in cv_doc.DESIGN_CHOICES.items()}
        cases.append({"lines": lines, "design": design,
                      "pages": cv_doc.estimate_pages(lines, design)})
    many = [c for c in cases if len(c["pages"]) > 2]
    check(len(many) > 5 and any(len(c["pages"]) == 1 for c in cases),
          "the corpus has CVs of one page and of three or more")
    check(cv_doc.estimate_pages(cv_store.cv_lines(MASTER))
          == [{"page": 1, "first": 1, "last": len(cv_store.cv_lines(MASTER))}],
          "the master estimates at one page")
    # A heading is never left last on a page.
    ok = all(c["lines"][pg["last"] - 1][:1] != "#" or pg["last"] == len(c["lines"])
             for c in cases for pg in c["pages"][:-1]
             if c["lines"][pg["last"] - 1])
    check(ok, "a heading never ends a page, as Word keeps it with what follows")
    check(cv_doc.clean_design({"font": "Comic Sans", "size": True, "margins": 20.0})
          == {"font": "Arial", "size": 10.5, "spacing": "standard", "margins": 20},
          "a design takes only the offered choices, the default for anything else")
    return {"cases": cases}


def word_checks() -> None:
    try:
        import docx  # noqa: F401
    except ImportError:
        note("python-docx is not installed here, so the Word file's design is checked in the container")
        return
    import cv_word
    with Profile() as p:
        cv_store.save_design({"font": "Georgia", "size": 11, "spacing": "relaxed", "margins": 25}, p.dir)
        data = cv_word.build(MASTER, "CV")
        import zipfile
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/styles.xml").decode("utf-8")
        doc = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
        check('w:ascii="Georgia"' in xml and 'w:ascii="Arial"' not in xml
              and 'w:left="1417"' in doc and 'w:top="1304"' in doc,
              "the Word file takes the saved design: Georgia, 25 mm at the sides, 23 mm top and bottom")
        check("<w:tbl" not in doc and "<w:drawing" not in doc and "w:hdr" not in doc,
              "and stays one plain column")


# ----------------------------------------------------------- node harness

def harness_checks(parity: dict) -> None:
    node = shutil.which("node")
    if not node:
        note("node is not installed, the page script harness is skipped")
        return
    tmp = Path(tempfile.mkdtemp(prefix="radar-cvfinal-node-"))
    try:
        js = tmp / "pages.js"
        js.write_text(cv_page.CV_MODEL_JS + "\nconsole.log(JSON.stringify(process.argv.length && "
                      "JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8')).cases.map("
                      "(c) => RadarCv.pages(c.lines, c.design))));\n", encoding="utf-8")
        cases = tmp / "cases.json"
        cases.write_text(json.dumps(parity), encoding="utf-8")
        out = subprocess.run([node, str(js), str(cases)], capture_output=True, text=True,
                             encoding="utf-8", timeout=120)
        try:
            got = json.loads(out.stdout)
        except ValueError:
            got = None
        want = [c["pages"] for c in parity["cases"]]
        check(got == want, f"the browser estimates the pages of {len(want)} CVs, every font, "
                           "size, spacing and margin, exactly as the server does")
        with Profile() as p:
            conn = rc.get_db(p.db)
            for _ in range(3):
                put(conn, cv_version=V_OLD)
            put(conn, cv_version=V_OLD, score_basis=rc.SCORE_BASIS_SPEC)
            put(conn, cv_version=V_OLD, applied_at="2026-11-04T00:00:00Z")
            conn.close()
            serve_dashboard.ARGS = argparse.Namespace(db=str(p.db), push=False,
                                                      host="127.0.0.1", port=0)
            master = tmp / "master.html"
            master.write_text(serve_dashboard.render_cv_page({}).decode("utf-8"), encoding="utf-8")
            viewing = tmp / "viewing.html"
            viewing.write_text(serve_dashboard.render_cv_page({"view": [V_OLD]}).decode("utf-8"),
                               encoding="utf-8")
            long_master = MASTER.replace(
                "### Software QA Manager",
                "".join(f"### Role {i}, Firm {i}, 2000 to 2001\n" + "- A careful line of work on "
                        "regulated medical device software, audited.\n" * 6 + "\n" for i in range(14))
                + "### Software QA Manager")
            (p.dir / "cv-20261114-090000.md").write_text(long_master, encoding="utf-8")
            (p.dir / "cv.active").write_text("cv-20261114-090000.md\n", encoding="utf-8")
            over = tmp / "over.html"
            over.write_text(serve_dashboard.render_cv_page({}).decode("utf-8"), encoding="utf-8")
            over_pages = cv_doc.estimate_pages(cv_store.cv_lines(long_master))
            check(len(over_pages) >= 3, f"the long master runs to {len(over_pages)} pages")
            lines = cv_store.cv_lines(MASTER)
            doc = cv_doc.load_doc(MASTER)
            sections = [s["title"] for s in doc["sections"]]

            def label(title, first, last, head=False):
                return (f"{title}{'' if head else ', section'}, lines {first} to {last}. Enter to edit")
            parts = [label("Name and headline", 1, 3, True)]
            n = 4
            for sec in doc["sections"]:
                block = cv_doc.render_lines({"name": "x", "headline": [], "sections": [sec]})[1:]
                nums = [n + i for i, line in enumerate(block) if line]
                parts.append(label(sec["title"], min(nums), max(nums)))
                n += len(block)
            words = []
            for line in lines:
                kind, w = cv_page.line_kind(line)
                words.append(w)
            ro = cv_screen._ro(p.db)
            stale = cv_screen.affected_summary(ro, p.cfg, "stale")
            ro.close()
            payload = {"pages": {"master": str(master), "viewing": str(viewing), "over": str(over)},
                       "expect": {"lines": lines, "words": words, "parts": parts,
                                  "sections": sections, "sectionLine": lines.index("## Summary") + 1,
                                  "pages": cv_doc.estimate_pages(lines), "version": V_NOW,
                                  "viewText": cv_doc.render(cv_doc.load_doc(OLD)),
                                  "overFirst": over_pages[2]["first"],
                                  "keys": ["".join(k) for k, _w in cv_screen.KEYS],
                                  "stale": {"count": stale["count"], "cost": stale["cost"]["words"],
                                            "ids_hash": stale["ids_hash"]}}}
            spec = tmp / "harness.json"
            spec.write_text(json.dumps(payload), encoding="utf-8")
        out = subprocess.run([node, str(REPO_ROOT / "test" / "cv_screen_harness.js"), str(spec)],
                             capture_output=True, text=True, encoding="utf-8", timeout=300)
        for line in out.stdout.splitlines():
            if line.startswith(("PASS", "FAIL", "NOTE")):
                print(line)
                if line.startswith("FAIL"):
                    failures.append(line[6:])
        check(out.returncode == 0 and "harness done" in out.stdout,
              "node ran the page script harness" + ("" if out.returncode == 0
                                                     else f" ({out.stderr.strip()[-400:]})"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------- the server, end to end

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
        (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message) = saved


def call(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=body or None, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def post(port, path, payload, guarded=True):
    own = f"127.0.0.1:{port}"
    headers = {"Host": own, "Origin": f"http://{own}", "Content-Type": "application/json"}
    if guarded:
        headers[HEADER] = "1"
    status, h, raw = call(port, "POST", path, json.dumps(payload).encode(), headers)
    try:
        return status, json.loads(raw or b"{}"), h
    except ValueError:
        return status, {}, h


def route_checks() -> None:
    with Profile() as p:
        conn = rc.get_db(p.db)
        put(conn, cv_match=72)
        conn.close()
        raw = p.prefs.read_bytes()
        with serving(p.db) as port:
            status, got, _h = post(port, "/cv/preferences", {"values": {"cv_pass_score": 75}}, guarded=False)
            check(status == 403 and p.prefs.read_bytes() == raw,
                  "the write guard refuses a preferences save without the page's header")
            status, got, _h = post(port, "/cv/preferences", {"values": {"cv_pass_score": "x"}})
            check(status == 422 and not got["ok"] and p.prefs.read_bytes() == raw,
                  "a bad value is a 422 and writes nothing")
            status, got, _h = post(port, "/cv/preferences", {"values": {"cv_pass_score": 75}})
            check(status == 200 and got["moved"] == 1 and b"cv_pass_score: 75" in p.prefs.read_bytes(),
                  "a good one is saved and re-sorts: 1 role moved")
            status, got, _h = post(port, "/cv/preferences/section", {"key": "sector", "text": "Medtech."})
            check(status == 200 and got["offer"]["count"] == 1, "the sector route answers with its offer")
            status, got, _h = post(port, "/cv/design", {"font": "Calibri", "size": 10, "spacing": "compact",
                                                        "margins": 15})
            check(status == 200 and cv_store.read_design(p.dir)["font"] == "Calibri",
                  "the design route saves the design beside the versions")
            status, got, _h = post(port, "/cv/design", {"font": "Comic Sans"})
            check(status == 422 and cv_store.read_design(p.dir)["font"] == "Calibri",
                  "and refuses a font it does not offer")
            status, got, _h = post(port, "/cv/rescore", {})
            check(status == 400, "Rescore without a confirm is a 400, as before")
            status, _h, body = call(port, "GET", "/cv", headers={"Host": f"127.0.0.1:{port}"})
            check(status == 200 and b"&quot;font&quot;: &quot;Calibri&quot;" in body,
                  "the page carries the saved design for the sheet")


# ------------------------------------------------------------------ the box

def box_checks() -> None:
    if not BOX_DOC.exists():
        check(False, f"{BOX_DOC.name} exists")
        return
    text = BOX_DOC.read_text(encoding="utf-8")
    for heading in ("## BEFORE", "## The deploy, one block at a time", "## AFTER", "## Rollback"):
        check(heading in text, f"the box checks have a {heading[3:]} section")
    blocks = re.findall(r"```(?:sh|bash)?\n(.*?)```", text, re.S)
    check(blocks and all(b.strip() for b in blocks), f"none of its {len(blocks)} command blocks is empty")
    gits = [line for line in text.splitlines()
            if re.search(r"\bgit (status|log|fetch|pull|reset|rev-parse)\b", line)]
    check(gits and all("sudo -u radar" in line for line in gits), "git runs as radar")
    pys = [line for line in text.splitlines()
           if re.search(r'(/bin/python|"\$PY"|\bpython) -c', line)]
    check(pys and all("PYTHONDONTWRITEBYTECODE=1" in line for line in pys),
          "every python line runs with PYTHONDONTWRITEBYTECODE=1")
    check("radar.sqlite.bak" not in text.split("## Rollback", 1)[1]
          and "cp " not in text.split("## Rollback", 1)[1],
          "the rollback never restores the backup")
    check("first open" in text and "nothing paid runs" in text.lower().replace("\n", " "),
          "it says what the first open does and that nothing paid runs without a confirm")
    guarded = [line for line in text.splitlines()
               if "git pull --ff-only" in line or "git reset --hard" in line]
    check(len(guarded) == 2 and all(
              "/cv/rescore/status | grep -o '\"state\": \"running\"'" in line
              and line.index("rescore/status") < line.index("&& echo 'wait")
              and ("git pull" not in line or line.index("rescore/status") < line.index("git pull"))
              for line in guarded),
          "the pull and the rollback both wait for a Rescore running in the dashboard, which "
          "pgrep cannot see, as well as for a job")
    check("cache_write_tokens" in text and "_migrate" in text,
          "and it names the one new column and how it is added")
    sqls = re.findall(r'\bQ "([^"]+)"', text)
    with Profile() as p:
        conn = rc.get_db(p.db)
        put(conn, cv_version=V_OLD)
        conn.close()
        ran = 0
        for sql in sqls:
            try:
                sqlite3.connect(p.db).execute(sql).fetchall()
                ran += 1
            except sqlite3.Error as err:
                check(False, f"box query runs: {sql[:60]} ({err})")
        check(sqls and ran == len(sqls), f"all {len(sqls)} box queries run on a seeded database")
    codes = re.findall(r'python3? -c "((?:[^"\\]|\\.)*)"', text)
    codes += re.findall(r'"\$PY" -c \'([^\']*)\'', text)
    good = 0
    for code in codes:
        try:
            compile(code.replace('\\"', '"'), "<box>", "exec")
            good += 1
        except SyntaxError as err:
            check(False, f"box python -c compiles: {code[:50]} ({err})")
    check(codes and good == len(codes), f"all {len(codes)} python -c lines compile")


def docs_checks() -> None:
    arch = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check("cv_screen.py" in arch and "Rescore affected" in arch and "re-sort" in arch,
          "the architecture notes describe the CV page final")
    run_all = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("run_cv_final_test.py" in run_all, "run_all.sh runs this runner")


# ------------------------------------------------- the review's fixes, 16 Nov

@contextlib.contextmanager
def held_lock(db):
    """Another writer holding the write lock, as an inbox run does for a whole
    email, until release() is called."""
    other = sqlite3.connect(db, timeout=1, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    state = {"held": True}

    def release():
        if state["held"]:
            state["held"] = False
            other.rollback()
    try:
        yield release
    finally:
        release()
        other.close()


@contextlib.contextmanager
def scorer_watch(on_call):
    """score_item.score_opportunity with on_call(n) run before each call. The
    usage of each answer is given tokens, so the cost has something to count;
    the reply is still the mock's."""
    real = score_item.score_opportunity
    n = [0]

    def watched(*a, **k):
        n[0] += 1
        on_call(n[0])
        scored, usage, note = real(*a, **k)
        usage = {**usage, "input_tokens": 1000, "output_tokens": 200,
                 "cache_read_tokens": 3000, "cache_write_tokens": 500}
        return scored, usage, note
    score_item.score_opportunity = watched
    try:
        yield n
    finally:
        score_item.score_opportunity = real


def busy_rescore_checks() -> None:
    """Rescore affected on a busy database: a paid reply is held and tried
    again, one that still cannot be written is counted as not stored, and the
    run's row in runs is written however the run ends."""
    saved = (rescore.BUSY_TRIES, rescore.BUSY_PAUSE)
    rescore.BUSY_TRIES, rescore.BUSY_PAUSE = 2, 1.0
    try:
        # Held, then let go during the pause: stored on the second try.
        with Profile() as p:
            conn = rc.get_db(p.db)
            ids = [put(conn, cv_version=V_OLD) for _ in range(3)]
            conn.execute("PRAGMA busy_timeout=200")
            box = {}

            def on_call(n):
                if n == 2:
                    box["cm"] = held_lock(p.db)
                    release = box["cm"].__enter__()
                    threading.Timer(0.5, release).start()
            usage: dict = {}
            with scorer_watch(on_call):
                res = rescore.rescore_affected(conn, p.cfg, usage, "stale", 3)
            box["cm"].__exit__(None, None, None)
            check(res["rescored"] == 3 and res["unstored"] == 0
                  and all(conn.execute("SELECT cv_version FROM opportunities WHERE id = ?",
                                       (i,)).fetchone()[0] == V_NOW for i in ids),
                  "a paid reply the database was too busy to take is held, tried again after "
                  "a pause, and stored: all 3 rescored")
            conn.close()
        # Held past every try: counted as not stored, and the run goes on.
        with Profile() as p:
            conn = rc.get_db(p.db)
            ids = [put(conn, cv_version=V_OLD) for _ in range(3)]
            conn.execute("PRAGMA busy_timeout=200")
            box = {}

            def on_call(n):
                if n == 2:
                    box["cm"] = held_lock(p.db)
                    box["release"] = box["cm"].__enter__()
                if n == 3:
                    box["release"]()
            usage = {}
            with scorer_watch(on_call):
                res = rescore.rescore_affected(conn, p.cfg, usage, "stale", 3)
            box["cm"].__exit__(None, None, None)
            vers = [conn.execute("SELECT cv_version FROM opportunities WHERE id = ?",
                                 (i,)).fetchone()[0] for i in ids]
            row = conn.execute("SELECT items_in, items_new, input_tokens, cache_write_tokens,"
                               " outcome, note FROM runs WHERE workflow = 'rescore'").fetchall()
            check(res["ok"] and res["rescored"] == 2 and res["unstored"] == 1 and res["left"] == 0
                  and vers == [V_NOW, V_OLD, V_NOW],
                  "a reply still not writable after every try is counted as not stored, that "
                  "role keeps its earlier score, and the run goes on to the next")
            check(len(row) == 1 and tuple(row[0][:5]) == (3, 2, 3000, 1500, "partial")
                  and "1 scored but not stored, the database busy" in row[0][5],
                  "and the one run row holds the tokens of all three paid calls, cache writes "
                  "included, and says one was not stored")
            conn.close()
        # The run stops part-way: its row in runs is written all the same.
        with Profile() as p:
            conn = rc.get_db(p.db)
            [put(conn, cv_version=V_OLD) for _ in range(3)]
            conn.close()
            seen = {}

            def on_call(n):
                if n == 2:
                    seen["cost"] = cv_screen.rescore_status()["cost_cents"]
                if n == 3:
                    raise RuntimeError("the model went away")
            with scorer_watch(on_call):
                summary = cv_screen.affected_summary(cv_screen._ro(p.db), p.cfg, "stale")
                got = cv_screen.start_rescore({"confirm": True, "scope": "stale", "expect": 3,
                                               "ids_hash": summary["ids_hash"]},
                                              p.cfg, p.db, threading.Lock())
                cv_screen.RUN["thread"].join(30)
            st = cv_screen.rescore_status()
            one = cv_screen.dollars_of({"input_tokens": 1000, "output_tokens": 200,
                                        "cache_read_tokens": 3000, "cache_write_tokens": 500},
                                       p.cfg.get("claude_model_score")) * 100
            conn = rc.get_db(p.db)
            row = conn.execute("SELECT items_in, items_new, input_tokens, outcome, note FROM runs"
                               " WHERE workflow = 'rescore'").fetchall()
            conn.close()
            check(got["ok"] and abs(seen.get("cost", 0) - one) < 1e-9,
                  f"while it runs the status carries the cost so far, {one:.2f} cents after one role")
            check(st["state"] == "failed" and "2 roles were rescored and stored before it stopped"
                  in st["note"] and abs(st["cost_cents"] - 2 * one) < 1e-9,
                  "a run that stops says what was stored before it stopped and what it cost")
            check(len(row) == 1 and tuple(row[0][:4]) == (3, 2, 2000, "failed")
                  and "stopped: RuntimeError" in row[0][4],
                  "and its row in runs is written anyway, with the tokens spent so far, so "
                  "they reach the week's cost")
    finally:
        rescore.BUSY_TRIES, rescore.BUSY_PAUSE = saved


def confirm_hash_checks() -> None:
    """The confirm names its roles: the same count over another set is refused."""
    with Profile() as p:
        conn = rc.get_db(p.db)
        ids = [put(conn, cv_version=V_OLD) for _ in range(3)]
        conn.close()
        summary = cv_screen.affected_summary(cv_screen._ro(p.db), p.cfg, "stale")
        check(summary["ids_hash"] == rescore.ids_hash(ids) == rescore.ids_hash(ids[::-1])
              and rescore.ids_hash(ids[:2] + [ids[2] + 1]) != summary["ids_hash"],
              "the confirm carries a hash of its roles, the same in any order, another for "
              "another set of the same size")
        conn = rc.get_db(p.db)
        conn.execute("UPDATE opportunities SET acknowledged_at = '2026-11-15T00:00:00Z'"
                     " WHERE id = ?", (ids[0],))
        conn.commit()
        swapped = put(conn, cv_version=V_OLD)
        conn.close()
        got = cv_screen.start_rescore({"confirm": True, "scope": "stale", "expect": 3,
                                       "ids_hash": summary["ids_hash"]},
                                      p.cfg, p.db, threading.Lock())
        cv_screen.RUN["thread"].join(30)
        st = cv_screen.rescore_status()
        conn = rc.get_db(p.db)
        check(st["state"] == "refused" and st["result"]["outcome"] == "set_changed"
              and "not the same 3" in st["note"]
              and conn.execute("SELECT cv_version FROM opportunities WHERE id = ?",
                               (swapped,)).fetchone()[0] == V_OLD
              and not runs(conn, "rescore"),
              "a confirm for 3 roles when 3 other roles are affected now is refused and "
              "scores nothing, though the count is the same")
        conn.close()
        fresh = cv_screen.affected_summary(cv_screen._ro(p.db), p.cfg, "stale")
        cv_screen.start_rescore({"confirm": True, "scope": "stale", "expect": 3,
                                 "ids_hash": fresh["ids_hash"]}, p.cfg, p.db, threading.Lock())
        cv_screen.RUN["thread"].join(30)
        check(cv_screen.rescore_status()["state"] == "done"
              and cv_screen.rescore_status()["result"]["rescored"] == 3,
              "the control: the same confirm with the hash of the roles as they are runs")


def busy_resort_checks() -> None:
    """A preferences save on a busy database: the file is saved, nothing is
    re-sorted, and the answer says so, so the page keeps its Re-sort action."""
    with Profile() as p:
        conn = rc.get_db(p.db)
        oid = put(conn, cv_match=74)
        conn.close()
        with held_lock(p.db):
            res = cv_screen.save_preferences({"values": {"cv_pass_score": 75}}, p.cfg, p.db)
        conn = rc.get_db(p.db)
        check(res["ok"] and res["saved"] and res["resorted"] is False
              and b"cv_pass_score: 75" in p.prefs.read_bytes() and tier_of(conn, oid) == "top"
              and "No role moved. Re-sort now tries again." in res["note"],
              "with another writer holding the lock past the timeout, the line is saved, no "
              "role moves, and the answer says resorted false with Re-sort now")
        conn.close()
        again = cv_screen.save_preferences({"values": {"cv_pass_score": 75}}, p.cfg, p.db)
        conn = rc.get_db(p.db)
        check(again["resorted"] and again["moved"] == 1 and tier_of(conn, oid) == "reading",
              "and the same values sent again, once the lock is free, re-sort: 1 role moved")
        conn.close()


def partner_line_checks() -> None:
    """A floor or pass mark whose partner line is missing is judged against
    the readers' default for it."""
    with Profile() as p:
        text = p.prefs.read_bytes().decode()
        p.prefs.write_bytes(re.sub(r"(?m)^cv_pass_score:.*\n", "", text).encode())
        check(rc.read_scoring_prefs(p.cfg)["cv_pass_score"] is None
              and rc.read_score_thresholds(p.cfg)["pass"] == 70,
              "a file with no cv_pass_score line reads as the default, 70")
        try:
            rc.clean_scoring_prefs({"cv_floor_score": 80}, p.cfg)
            check(False, "a Filtered floor of 80 with no pass mark line is refused")
        except ValueError as err:
            check("at or under the CV pass mark (70)" in str(err),
                  f"a Filtered floor of 80 with no pass mark line is refused against the "
                  f"default 70, which the readers would use: {err}")
        check(rc.clean_scoring_prefs({"cv_floor_score": 60}, p.cfg) == {"cv_floor_score": "60"},
              "the control: a floor of 60 under the default 70 is taken")
        p.prefs.write_bytes(re.sub(r"(?m)^cv_floor_score:.*\n", "", text).encode())
        try:
            rc.clean_scoring_prefs({"cv_pass_score": 30}, p.cfg)
            check(False, "a pass mark of 30 with no floor line is refused")
        except ValueError as err:
            check("(40) must be at or under the CV pass mark (30)" in str(err),
                  "and a pass mark of 30 with no floor line is refused against the default 40")


def pref_line_checks() -> None:
    """Title, Keywords and Name: written in one step, every other byte kept,
    and never a floor or a threshold."""
    for crlf in (False, True):
        with Profile(crlf=crlf) as p:
            raw = p.prefs.read_bytes()
            nl = b"\r\n" if crlf else b"\n"
            written = []
            real = rc._write_prefs

            def watch(path, text):
                written.append(Path(path))
                real(path, text)
            rc._write_prefs = watch
            try:
                old = re.search(rb"(?m)^keywords:[^\r\n]*", raw)
                stored = rc.update_pref_line("keywords", "IEC 62304, ISO 13485", p.cfg)
            finally:
                rc._write_prefs = real
            want = raw[:old.start()] + b"keywords: IEC 62304, ISO 13485" + raw[old.end():]
            kind = "CRLF" if crlf else "line feed"
            check(stored == "IEC 62304, ISO 13485" and p.prefs.read_bytes() == want
                  and written == [p.prefs] and nl in p.prefs.read_bytes(),
                  f"a keywords save goes through the one-step write and leaves every other "
                  f"byte of a {kind} file as it was")
    with Profile() as p:
        raw = p.prefs.read_bytes()
        for key, value in (("day_rate_floor_gbp", "650"), ("salary_floor_gbp", "90000"),
                           ("cv_pass_score", "60"), ("cv_floor_score", "20")):
            try:
                rc.update_pref_line(key, value, p.cfg)
                check(False, f"/profile/setting refuses {key}")
            except ValueError as err:
                check("Scoring on the Settings page" in str(err) and p.prefs.read_bytes() == raw,
                      f"the one-line save refuses {key}, which only Settings' Scoring "
                      "saves, with its re-sort and history, and writes nothing")
        check("day_rate_floor_gbp" not in rc.PREF_EDITABLE
              and rc.read_pref_line("day_rate_floor_gbp", p.cfg) == "750",
              "the floor is no longer a one-line setting, and still reads back")
        tmpl = TEMPLATE.read_bytes()
        p.prefs.unlink()
        try:
            rc.update_pref_line("keywords", "x", p.cfg)
            check(False, "with no live file the one-line save writes nothing")
        except ValueError:
            check(TEMPLATE.read_bytes() == tmpl,
                  "with no live file the one-line save refuses, and never writes the template")
    with Profile() as p:
        with serving(p.db) as port:
            raw = p.prefs.read_bytes()
            status, got, _h = post(port, "/profile/setting", {"key": "day_rate_floor_gbp",
                                                              "value": "650"})
            check(status == 422 and not got["ok"] and p.prefs.read_bytes() == raw,
                  "the route refuses a floor: 422, and the file is as it was")
            status, got, _h = post(port, "/profile/setting", {"key": "target_title",
                                                              "value": "Head of Software Quality"})
            check(status == 200 and got["ok"]
                  and rc.read_pref_line("target_title", p.cfg) == "Head of Software Quality",
                  "and still saves the title, as the CV page's Title button sends it")
            status, got, _h = post(port, "/profile/setting", {"key": "first_name", "value": "Alex"})
            check(status == 200 and rc.read_first_name(p.cfg) == "Alex",
                  "and the first name Home greets")
        page = serve_dashboard.render_settings_page().decode("utf-8")
        check('data-pref-save="day_rate_floor_gbp"' not in page and 'data-pref-save="first_name"' in page
              and all(f'data-chips-edit="{k}"' in page for k in ("target_title", "keywords")),
              "Settings' one-line saves are the title and keywords (through their dialog) and the "
              "name, no floor")


def cache_token_checks() -> None:
    """Cache-write tokens are recorded where usage is logged and priced at 1.25
    times input."""
    import types
    seen = {}

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="{}")],
                usage=types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                            cache_read_input_tokens=300,
                                            cache_creation_input_tokens=1200),
                model="claude-test-1", stop_reason="end_turn")
    saved = (sys.modules.get("anthropic"), os.environ.get("RADAR_MOCK"))
    key_name = rc.load_config().get("api_key_env", "ANTHROPIC_API_KEY")
    saved_key = os.environ.get(key_name)
    os.environ["RADAR_MOCK"] = "0"
    os.environ[key_name] = "not-a-real-key"
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=FakeClient)
    try:
        _text, usage = rc.claude_call("claude-asked-for", "s", "u")
    finally:
        os.environ["RADAR_MOCK"] = saved[1] if saved[1] is not None else "1"
        if saved_key is None:
            os.environ.pop(key_name, None)
        else:
            os.environ[key_name] = saved_key
        if saved[0] is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = saved[0]
    check(usage["cache_write_tokens"] == 1200 and usage["cache_read_tokens"] == 300,
          "a live call's usage carries the tokens it wrote to the prompt cache (the SDK faked, "
          "nothing sent)")
    total = rc.add_usage({}, usage)
    rc.add_usage(total, {"input_tokens": 1})
    check(total["cache_write_tokens"] == 1200 and total["input_tokens"] == 11,
          "add_usage sums them, and a usage without them counts none")
    with Profile() as p:
        conn = rc.get_db(p.db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        rc.log_run(conn, "rescore", mode="live", items_in=1, items_new=1,
                   model="claude-sonnet-4-6", usage=total)
        got = conn.execute("SELECT cache_write_tokens FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()
    check("cache_write_tokens" in cols and got == 1200,
          "runs has a cache_write_tokens column, added by _migrate, and log_run fills it")
    d = cv_screen.dollars_of({"input_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
                              "cache_read_tokens": 1_000_000}, "claude-sonnet-4-6")
    check(abs(d - (3.0 + 3.0 * 1.25 + 3.0 * 0.1)) < 1e-9,
          "a cache write is priced at 1.25 times the input price, a cache read at 0.1")
    prep = (REPO_ROOT / "scripts" / "prepare_application.py").read_text(encoding="utf-8")
    check('usage.get("cache_write_tokens", 0)' in prep,
          "Prepare application's own run row records them too")


def inbox_lag_checks() -> None:
    """An inbox run sorts its roles on the floors as they stand once it holds
    the write lock, not as they stood when it started."""
    import base64
    import process_email
    with Profile() as p:
        p.prefs.write_bytes(p.prefs.read_bytes().replace(b"day_rate_floor_gbp: 750",
                                                         b"day_rate_floor_gbp: 700"))
        body = ("Head of Software Quality\nCorbel Medical · Reading, UK\n£720 per day\n"
                "https://example.invalid/corbel-quality\n")
        b64 = base64.b64encode(json.dumps({"subject": "1 new job", "from": "alerts@jobs.example",
                                           "date": "2026-11-16", "body_text": body,
                                           "body_html": ""}).encode()).decode("ascii")
        real = score_item.extract_opportunities

        def mid_run(*a, **k):
            # A save on the Preferences tab lands while the email is read.
            p.prefs.write_bytes(p.prefs.read_bytes().replace(b"day_rate_floor_gbp: 700",
                                                             b"day_rate_floor_gbp: 750"))
            return real(*a, **k)
        score_item.extract_opportunities = mid_run
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = process_email.main(["--mock", "--db", str(p.db), "--b64", b64])
        finally:
            score_item.extract_opportunities = real
        conn = rc.get_db(p.db)
        row = conn.execute("SELECT tier, gate_rate, gate_rate_note FROM opportunities").fetchone()
        conn.close()
        check(code == 0 and row is not None and tuple(row[:2]) == ("question", 0)
              and "£750 floor" in row[2],
              "a floor saved while an inbox run reads its email reaches that run's roles: "
              "the floors are read again under the run's write lock, so £720 a day fails the "
              "new £750 floor (read at the start only, it passed the old £700)")



def main() -> int:
    # run_all.sh hands every runner --db; this one builds its own throwaway
    # profiles and databases, so it takes the flag and leaves it unused.
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted from run_all.sh, not used")
    parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for fn in (write_checks, resort_checks, offer_checks, stale_checks, cost_checks,
               page_checks, word_checks, route_checks, busy_rescore_checks,
               confirm_hash_checks, busy_resort_checks, partner_line_checks,
               pref_line_checks, cache_token_checks, inbox_lag_checks):
        fn()
    tmp = Path(tempfile.mkdtemp(prefix="radar-cvfinal-p-"))
    try:
        harness_checks(parity_checks(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    box_checks()
    docs_checks()
    if failures:
        print(f"\n{len(failures)} CV FINAL CHECK(S) FAILED")
        return 1
    print("\nALL CV FINAL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
