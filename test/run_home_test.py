#!/usr/bin/env python
"""Checks for Home final (design/home/final and the decisions of 15 Nov 2026).

No model calls and no network: RADAR_MOCK is forced on and every database is
a throwaway under a temporary folder. Every company, person and role here is
invented, as the design's are.

1. The welcome name comes from the preferences file's optional first_name
   line, never the code: missing or empty greets without a name, the line is
   read like the other labelled lines, and an empty line never reads the line
   after it. The template carries the empty line.
2. The lights: each job_health verdict in one of five words, On time, Late,
   Stale, Off or Dry run, each with its own mark; the phone push worked out
   from the signal watcher's runs and the ntfy address.
3. The model on a seeded typical day: four figures, each equal to its group
   of Needs you today, the status sentence counting the same, an applied role
   in no count and no list (the control: undo it and it is counted), the
   funnel as the Jobs tabs count, the applications tile from the board, the
   week chart rebuilt from score history, and where each item opens.
4. Done: off Home only, Undo puts it back, a Radar item back when its state
   changes, an application step back when its date changes, and the item where
   it lives untouched (the database apart from home_done is byte for byte
   the same).
5. The page: one h1, the four figures and no Threads due, the lights in words,
   every control a link or a button, the item addresses, the Done view, the
   fragment, and no light or dark switch.
6. The states: a quiet day, the first week, no applications yet and a Radar
   fault, as the states board draws them.
7. The routes: POST /home/done and /home/undone through the guard, their 400s,
   409s and 200s, a forged request refused with nothing written, GET /radar
   (Radar's health, the old Home) and the CV page's Settings.
8. The script parses (node --check) and keeps its keys off while typing and
   behind the shortcut switch.
9. The box checks: no command block is empty, every query in them runs on a
   seeded database, every python -c compiles, and the rollback never
   restores the backup.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
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
import build_dashboard as bd  # noqa: E402
import home_page as hp  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
CFG = rc.load_config()
NOW = datetime.now(timezone.utc)


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(days: float = 0, hours: float = 0) -> str:
    return iso(NOW - timedelta(days=days, hours=hours))


def uk_day(days: float = 0) -> str:
    """A UK calendar date days from today, as a next step's due date is kept."""
    return (hp.uk(NOW).date() + timedelta(days=days)).isoformat()


# ================================================================ the seed

COMPANIES = {
    "Hartwell Infusion Ltd": dict(what_they_build="Volumetric and syringe infusion pumps, Class IIb.",
                                  software_content="Embedded firmware, IEC 62304 class C.",
                                  stage="series-c", city="Guildford", country="UK",
                                  people=json.dumps([{"name": "R. Okoye", "role": "VP Quality"}])),
    "Marchetti Devices": {}, "PDX Diagnostics": {}, "Brandt Cardio": {}, "Kinross Optics": {},
    "Carrow Dental Systems": {}, "Wrenfield Digital Health": {}, "Calder Neurotech": {},
    "Severn Renal Systems": {}, "Lumenhal Photonics": {}, "Ashdown Diagnostics": {},
    "Quayle Health": {}, "Tamesis Surgical": {}, "Orrin Medical": {},
}


class Seed:
    """A seeded Home, the typical day of the design unless told otherwise."""

    def __init__(self, path: Path):
        self.path = path
        self.conn = rc.get_db(path)
        self.ids: dict = {}
        self.n = 0
        self.cids = {}
        for name, facts in COMPANIES.items():
            cid = rc.resolve_company(self.conn, name, ago(60))
            if facts:
                sets = ", ".join(f"{k} = ?" for k in facts)
                self.conn.execute(f"UPDATE companies SET {sets} WHERE id = ?", (*facts.values(), cid))
            self.cids[name] = cid
        self.conn.commit()

    def role(self, title, company, tier="reading", cv=60, first=3.0, read=True, **cols) -> int:
        self.n += 1
        row = {"url_hash": cols.pop("url_hash", f"home-{self.n}"), "first_seen": ago(first),
               "last_seen": ago(first), "source": "linkedin-alert", "title": title,
               "company": company, "company_id": self.cids.get(company), "location": "Guildford",
               "tier": tier, "cv_match": cv, "role_key": rc.role_key(title, company),
               "scored_at": ago(first), "gate_sector": 1, "gate_cv": 1, "gate_location": 1,
               "gate_rate": 1, "one_line_why": f"{title} at {company}.",
               "read_at": ago(first) if read else None}
        row.update(cols)
        row.setdefault("rate_band", "above" if row.get("salary_rate") else "unstated")
        self.conn.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES"
                          f" ({', '.join('?' for _ in row)})", tuple(row.values()))
        oid = self.conn.execute("SELECT id FROM opportunities WHERE url_hash = ?",
                                (row["url_hash"],)).fetchone()[0]
        self.conn.commit()
        return oid

    def signal(self, headline, company, relevance, first, kind="funding", source="fiercebiotech",
               url_hash=None, **cols) -> int:
        self.n += 1
        row = {"url_hash": url_hash or f"sig-{self.n}", "first_seen": ago(first),
               "source_id": source, "company": company, "company_id": self.cids.get(company),
               "headline": headline, "relevance": relevance, "why": f"Why {headline}.",
               "status": "new", "story_type": kind, "card_summary": f"Summary of {headline}.",
               "event_key": f"ev-{self.n}", "source_url": "https://example.com/story"}
        row.update(cols)
        self.conn.execute(f"INSERT INTO signals ({', '.join(row)}) VALUES"
                          f" ({', '.join('?' for _ in row)})", tuple(row.values()))
        sid = self.conn.execute("SELECT id FROM signals WHERE url_hash = ?",
                                (row["url_hash"],)).fetchone()[0]
        self.conn.commit()
        return sid

    def run(self, workflow, hours, mode="live", outcome="ok", note=None) -> None:
        self.conn.execute("INSERT INTO runs (ts, workflow, mode, note, outcome) VALUES (?,?,?,?,?)",
                          (ago(hours=hours), workflow, mode, note, outcome))
        self.conn.commit()

    def apply(self, oid, days, stage="applied", step=None, due=None, by="owner", reason=None):
        self.conn.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?", (ago(days), oid))
        ap.on_applied(self.conn, oid, ago(days))
        self.conn.commit()
        row = self.conn.execute("SELECT id, role_key FROM opportunities WHERE id = ?", (oid,)).fetchone()
        app = ap.get(self.conn, ap.role_ref(row))
        self.conn.execute("UPDATE applications SET stage = ?, closed_reason = ?, next_step = ?,"
                          " next_step_due = ?, next_step_by = ?, created_at = ?,"
                          " stage_by = ? WHERE id = ?",
                          (stage, reason, step, due, by if step else None, ago(days),
                           "radar" if stage in ("applied", "closed") and by == "radar" else "owner",
                           app["id"]))
        self.conn.commit()
        return app["id"]

    def prepare(self, oid, days):
        row = self.conn.execute("SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone()
        self.conn.execute("INSERT INTO tailored_cvs (role_ref, opportunity_id, cv_text, saved_text,"
                          " saved_name, saved_at) VALUES (?,?,?,?,?,?)",
                          (ap.role_ref(row), oid, "cv", "cv", f"CV - {row['company']}", ago(days)))
        ap.on_saved(self.conn, row, ago(days))
        self.conn.commit()

    def close(self):
        self.conn.close()


def seed_home(path: Path, variant: str = "typical") -> dict:
    """The design's world. variant: typical, quiet, first (Radar a few days
    old), noapps (no applications) or fault (the watcher stale and the push
    with it, the checker late, the digest in dry run and the inbox quiet)."""
    s = Seed(path)
    ids = s.ids
    first_week = variant == "first"
    old = 1.0 if first_week else 30.0
    # ---- the runs: every job on time and live, but the fault's
    fault = variant == "fault"
    s.run("inbox", 15 if fault else 0.2)
    s.run("signals", 22 if fault else 1.0, "dry-run" if fault else "live")
    s.run("check", 30 if fault else 7)
    s.run("digest", 50, "dry-run" if fault else "build")
    # ---- the open roles
    ids["hartwell_role"] = s.role("Head of Software Quality, Class II infusion pumps",
                                  "Hartwell Infusion Ltd", "top", 86, first=min(old, 4),
                                  url_hash="advert-hartwell", salary_rate="£700–£750/day")
    if variant != "quiet":
        ids["top_new"] = [
            s.role("Interim Head of Software | Class IIb | 6 months | £700-£750pd", "Marchetti Devices",
                   "top", 83, first=0.05, read=False, salary_rate="£700-£750pd", engagement_type="contract",
                   location="Basingstoke"),
            s.role("Fractional Software Director, IVD analyser", "PDX Diagnostics", "top", 81,
                   first=0.1, read=False, salary_rate="£800/day", location="Cambridge"),
            s.role("Principal Software Engineer, cardiac monitoring", "Brandt Cardio", "top", 79,
                   first=1.0, read=False, location="Bracknell", rate_band="unstated"),
        ]
        ids["question_new"] = [
            s.role("Software Verification Manager, ophthalmic lasers", "Kinross Optics", "question", 77,
                   first=0.2, read=False, gate_rate=0, salary_rate="£675/day",
                   question_text="Would they move on the rate? £675/day is £25 under the £700 floor."),
            s.role("Software Quality Lead, dental imaging", "Carrow Dental Systems", "question", 74,
                   first=1.0, read=False, gate_rate=0, salary_rate="£600/day",
                   question_text="Would they move on the rate? £600/day is £100 under the £700 floor."),
        ]
    for k in range(6):
        s.role(f"Reading role {k}", "Orrin Medical", "reading", 55 + k, first=min(old, 2 + k))
    for k in range(4):
        s.role(f"Filtered role {k}", "Orrin Medical", "filtered", 30, first=min(old, 3 + k),
               filter_reason="not medtech")
    s.role("Question role read", "Lumenhal Photonics", "question", 70, first=min(old, 9))
    # An applied Top role: in no count and no list of Home's. Not on file on
    # the day with no applications, where nothing has been applied for.
    if variant != "noapps":
        ids["applied_top"] = s.role("VP Software, applied for", "Ashdown Diagnostics", "top", 90,
                                    first=min(old, 2), read=False)
    # ---- the board
    if variant != "noapps":
        ids["applied_top_app"] = s.apply(ids["applied_top"], 2, "applied")
        w = s.role("Fractional QA/RA Lead, SaMD", "Wrenfield Digital Health", "top", 80,
                   first=min(old, 10), read=True)
        ids["wrenfield_app"] = s.apply(w, 7, "applied", ap.FOLLOW_UP,
                                       uk_day(4) if variant == "quiet" else uk_day(0), by="radar")
        c = s.role("Software Lead, neurostimulation", "Calder Neurotech", "top", 78, first=min(old, 12))
        ids["calder_app"] = s.apply(c, 9, "conversation", "Reply to Calder Neurotech",
                                    uk_day(3 if variant == "quiet" else 1))
        v = s.role("Interim Quality Lead", "Severn Renal Systems", "top", 76, first=min(old, 14))
        ids["severn_app"] = s.apply(v, 12, "interviewing",
                                    "Prepare for the Severn Renal Systems interview", uk_day(3))
        for k, (t, c2, reason) in enumerate((("Verification Manager", "Quayle Health", "no-reply"),
                                             ("QA Lead", "Tamesis Surgical", "turned-down"))):
            x = s.role(t, c2, "top", 70, first=min(old, 25))
            s.apply(x, 22, "closed", reason=reason, by="radar" if reason == "no-reply" else "owner")
        p = s.role("Head of RA, Preparing", "Quayle Health", "top", 75, first=min(old, 5))
        s.prepare(p, 1)
    # ---- signals
    if variant != "quiet":
        ids["window"] = s.signal(
            "Hartwell Infusion Ltd advertises for a Head of Software Quality, a first hire at a "
            "company you have been in touch with", "Hartwell Infusion Ltd", 90, 2.0,
            kind="buying-window", source="job-advert", url_hash="advert-hartwell",
            playbook_step="Send the compliance-cost article, or the gap assessment one-pager.",
            pushed_at=ago(2))
    ids["round"] = s.signal("Hartwell Infusion raises £32m Series C", "Hartwell Infusion Ltd", 81,
                            min(old, 30))
    for k, (rel, days) in enumerate(((55, 1), (45, 2), (42, 3), (60, 9), (41, 16), (48, 23),
                                     (20, 1), (39, 2))):
        if not first_week or days < 3:
            s.signal(f"Story {k} at {rel}", "Orrin Medical", rel, days, kind="market")
    # ---- score history: a role that was Top two weeks ago and is Reading now
    if not first_week:
        gone = s.role("Once a Top prospect", "Orrin Medical", "reading", 65, first=15)
        s.conn.execute("INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
                       " scored_at, tier) VALUES (?,?,?,?,?,?)",
                       (gone, ago(14), "replaced", "rescore", ago(15), "top"))
        s.conn.commit()
        ids["history_top"] = gone
    # ---- a thread for the company record
    s.conn.execute("INSERT INTO touches (company, touched_at, channel, next_action, next_action_date,"
                   " company_id) VALUES (?,?,?,?,?,?)",
                   ("Hartwell Infusion Ltd", ago(14), "comment", "Connection note to the VP Quality",
                    uk_day(2), s.cids["Hartwell Infusion Ltd"]))
    s.conn.commit()
    if variant == "first":
        # Every row arrived this week: move the earliest dates inside it.
        monday = hp._monday(hp.uk(NOW).date())
        floor = iso(datetime(monday.year, monday.month, monday.day, 9, tzinfo=timezone.utc))
        for table in ("opportunities", "signals"):
            s.conn.execute(f"UPDATE {table} SET first_seen = ? WHERE first_seen < ?", (floor, floor))
        s.conn.execute("UPDATE opportunities SET scored_at = first_seen")
        s.conn.commit()
    if variant == "quiet":
        # Everything read, nothing urgent, nothing due.
        s.conn.execute("UPDATE opportunities SET read_at = ? WHERE read_at IS NULL", (ago(0),))
        s.conn.commit()
    s.close()
    return ids


# ================================================================ helpers

def reader(db: Path):
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def model_of(db: Path, cfg=None, **kw) -> dict:
    conn = reader(db)
    try:
        data = bd.collect(conn, cfg or CFG)
        return hp.build_model(conn, data, cfg or CFG, **kw)
    finally:
        conn.close()


def page_of(db: Path, cfg=None, **kw) -> str:
    conn = reader(db)
    try:
        data = bd.collect(conn, cfg or CFG)
        return hp.render(conn, data, cfg or CFG, **kw)
    finally:
        conn.close()


def fingerprint(db: Path) -> str:
    """Every table but home_done, as one hash."""
    conn = sqlite3.connect(db)
    out = hashlib.sha256()
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                " AND name NOT IN ('home_done', 'sqlite_sequence') ORDER BY name"):
        for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid"):
            out.update(repr(row).encode())
    conn.close()
    return out.hexdigest()


@contextlib.contextmanager
def pointed_at(db: Path):
    saved = serve_dashboard.ARGS
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False)
    try:
        yield
    finally:
        serve_dashboard.ARGS = saved


@contextlib.contextmanager
def prefs(text: str | None):
    """A preferences file with this text (None for no file) for config."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "preferences.md"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        rel = os.path.relpath(path, rc.REPO_ROOT)
        yield {**CFG, "prefs_file": rel}


# ================================================================ 1. the name

def name_checks(tmp: Path) -> None:
    with prefs("day_rate_floor_gbp: 700\nfirst_name: Sam\n") as cfg:
        check(rc.read_first_name(cfg) == "Sam", "the first_name line is read from the preferences file")
        check(hp.greeting(datetime(2026, 11, 13, 9, 14, tzinfo=timezone.utc), rc.read_first_name(cfg))
              == "Good morning, Sam", "and greets: Good morning, Sam")
    with prefs("first_name:\n\n## Rates.\nday_rate_floor_gbp: 700\n") as cfg:
        check(rc.read_first_name(cfg) is None,
              "an empty first_name line reads as no name, never the line after it")
    with prefs("day_rate_floor_gbp: 700\n") as cfg:
        name = rc.read_first_name(cfg)
        check(name is None and hp.greeting(datetime(2026, 11, 13, 9, tzinfo=timezone.utc), name)
              == "Good morning.", "no line greets without a name: Good morning.")
    with prefs("first_name:   Jo   \r\nkeywords: a\r\n") as cfg:
        check(rc.read_first_name(cfg) == "Jo", "spaces and a Windows line end are not part of the name")
    check(hp.greeting(datetime(2026, 11, 13, 13, tzinfo=timezone.utc), None) == "Good afternoon."
          and hp.greeting(datetime(2026, 11, 13, 19, tzinfo=timezone.utc), None) == "Good evening.",
          "the greeting follows the UK time: afternoon from 12:00, evening from 18:00")
    template = (REPO_ROOT / "config" / "preferences.template.md").read_text(encoding="utf-8")
    check(re.search(r"^first_name:[ \t]*$", template, re.M) is not None,
          "the template carries the optional first_name line, empty")
    code = "\n".join((REPO_ROOT / "scripts" / n).read_text(encoding="utf-8")
                     for n in ("home_page.py", "build_dashboard.py", "page_frame.py"))
    check("Alex" not in code and "Good morning, " not in code.replace('f"Good {part}, {name}"', ""),
          "no name is written in the code")
    check("first_name" in rc.PREF_EDITABLE, "the CV page's Settings can set it")


# ================================================================ 2. the lights

def light_checks(tmp: Path) -> None:
    def v(state, mode="live", **kw):
        return {"state": state, "mode": mode, "last_run": ago(hours=1), "hours_ago": 1.0,
                "late_hours": 4, "stale_hours": 6, **kw}
    words = {st: hp.job_light("signals", v(st))["word"] for st in
             ("ok", "partial", "late", "failing", "stale", "never")}
    check(words == {"ok": "On time", "partial": "Late", "late": "Late", "failing": "Late",
                    "stale": "Stale", "never": "Off"},
          f"each verdict has its word, a run finished with failures Late ({words})")
    check(hp.job_light("inbox", v("late"))["word"] == "Quiet"
          and hp.job_light("inbox", v("stale"))["word"] == "Stale"
          and hp.job_light("inbox", v("partial"))["word"] == "Late",
          "the inbox under its stale limit is Quiet, a sixth word; stale is still Stale")
    check(hp.job_light("signals", v("ok", "dry-run"))["word"] == "On time"
          and hp.job_light("check", v("ok", "dry-run"))["word"] == "On time",
          "the signal watcher and the advert checker in dry run say their state, never Dry run")
    check(hp.job_light("digest", v("ok", "dry-run"))["word"] == "Dry run"
          and hp.job_light("digest", v("ok", "build"))["word"] == "On time"
          and hp.job_light("digest", v("ok", "commit"))["word"] == "On time",
          "the digest on time in dry run says Dry run; its build and commit are live")
    check(hp.job_light("digest", v("stale", "dry-run"))["word"] == "Stale",
          "a stale digest says Stale, dry run or not")
    marks = {w: sym for w, (_c, sym) in hp.LIGHTS.items()}
    check(len(set(marks.values())) == len(marks) == 6,
          f"each of the six words has its own mark as well as a colour ({marks})")
    check(marks["Late"] != marks["Dry run"], "Late and Dry run differ in shape, not only in words")
    jobs = {"signals": v("ok", "live")}
    check(hp.push_light(jobs, {"ntfy_url": "", "ntfy_topic_radar": "x"}, None)["word"] == "Off",
          "the push is Off when radar.yaml names no ntfy address")
    check(hp.push_light({"signals": {"state": "never"}}, CFG, None)["word"] == "Off",
          "and Off when the watcher has never run")
    check(hp.push_light({"signals": v("stale", "dry-run")}, CFG, None)["word"] == "Stale"
          and hp.push_light({"signals": v("late", "dry-run")}, CFG, None)["word"] == "Late",
          "the watcher's state comes first: Stale or Late, in dry run or not")
    check(hp.push_light({"signals": v("ok", "dry-run")}, CFG, None)["word"] == "Dry run",
          "then Dry run while the watcher runs in dry run")
    check(hp.push_light(jobs, CFG, None)["word"] == "On time",
          "and On time with a live watcher on time")
    db = tmp / "dry.sqlite"
    seed_home(db)
    conn = rc.get_db(db)
    conn.execute("DELETE FROM runs")
    for workflow, hours, mode in (("inbox", 15, "live"), ("signals", 1, "dry-run"),
                                  ("check", 7, "dry-run"), ("digest", 50, "dry-run")):
        conn.execute("INSERT INTO runs (ts, workflow, mode, outcome) VALUES (?,?,?,'ok')",
                     (ago(hours=hours), workflow, mode))
    conn.commit()
    conn.close()
    m = model_of(db)
    said = {light["name"]: light["word"] for light in m["lights"]}
    check(said == {"inbox": "Quiet", "signals": "On time", "check": "On time",
                   "digest": "Dry run", "push": "Dry run"},
          f"the box as it is set up: a quiet inbox, the digest and the push in dry run ({said})")
    check(not [i for i in m["todo"] if i["group"] == "radar"]
          and "dry run" not in hp.status_sentence(m) and "quiet" not in hp.status_sentence(m).lower(),
          "and none of it is an item or a part of the sentence: Dry run and Quiet are not faults")


# ================================================================ 3. the model

def model_checks(tmp: Path) -> None:
    db = tmp / "typical.sqlite"
    ids = seed_home(db)
    m = model_of(db)
    c = m["counts"]
    groups = {g: [i for i in m["todo"] if i["group"] == g] for g, _w in hp.GROUPS}
    check(c == {g: len(v) for g, v in groups.items()} and (c["urgent"], c["top"], c["question"], c["apps"])
          == (1, 3, 2, 1), f"each figure equals its group of the list ({c})")
    check(c["radar"] == 0 and [l["word"] for l in m["lights"]] == ["On time"] * 5,
          "a day with every job live and on time has no Radar group")
    check(hp.status_sentence(m) == "Four things need you: 1 urgent signal, 3 new Top prospects, "
          "2 new One question away roles and 1 application follow-up due today.",
          f"the status sentence counts four kinds ({hp.status_sentence(m)})")
    keys = [i["key"] for i in m["todo"]]
    check(len(keys) == len(set(keys)) and len({i["id"] for i in m["todo"]}) == len(keys),
          "every item has its own key and its own address")
    tops = [i["role"]["id"] for i in groups["top"]]
    check(tops == ids["top_new"], "new Top prospects are the unread Top roles, highest CV match first")
    check(ids["applied_top"] not in tops and all(i["role"]["id"] != ids["applied_top"]
                                                 for i in m["todo"] if i["kind"] == "role"),
          "a Top role applied for is not a new Top prospect")
    j = m["jmodel"]
    # Top: Hartwell, the three new and the role in Preparing; all open adds
    # the two new and one read One question away, six Reading, four Filtered
    # and the role Top once and Reading now.
    check(j["totals"]["top"] == 5 and j["open_total"] == 5 + 3 + 6 + 4 + 1,
          f"the funnel counts as the Jobs tabs do, the applied role in none "
          f"(top {j['totals']['top']}, all open {j['open_total']})")
    w = m["week"]
    this = w["this"]
    check(not w["first_week"] and len(w["weeks"]) == hp.WEEKS,
          "after the first week the chart draws four weeks")
    total_top = sum(x["top"] for x in w["weeks"])
    # Hartwell, three new, the Wrenfield/Calder/Severn and closed ones are Top
    # but outside or inside the window by their dates; the applied ones are out.
    # The new ones are seeded up to two days ago, so early in the week (a
    # Monday morning) they fall in last week: count this week and the one before.
    recent = w["this"]["top"] + w["last"]["top"]
    check(all(x["top"] >= 0 for x in w["weeks"]) and recent >= 3,
          f"the last two weeks' new Top prospects include the three new ones ({recent})")
    hist_week = hp._monday(hp.uk(NOW - timedelta(days=15)).date())
    check(any(x["week"] == hist_week and x["top"] >= 1 for x in w["weeks"]),
          "a role scored Top two weeks ago and Reading now is counted then, from score history")
    # The control for the applied role: undo every application, and it counts.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE opportunities SET applied_at = NULL")
    conn.commit()
    conn.close()
    m2 = model_of(db)
    check(sum(x["top"] for x in m2["week"]["weeks"]) > total_top
          and any(i["kind"] == "role" and i["role"]["id"] == ids["applied_top"] for i in m2["todo"])
          and m2["jmodel"]["totals"]["top"] > j["totals"]["top"],
          "the control: with the applications undone the applied role is counted, listed and in the funnel")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?", (ago(2), ids["applied_top"]))
    conn.commit()
    conn.close()
    sig = this["signals"] + w["last"]["signals"]
    check(sig >= 3, f"signals at 40 or more in the last two weeks counted once a story ({sig})")
    # ---- where things open
    urgent = groups["urgent"][0]
    check(urgent["open"][0] == f"/jobs/{ids['hartwell_role']}" and urgent["open"][1] == "Open the role on Jobs",
          "a buying window opens the role it was read from on Jobs, as hiring is not on Insights")
    check(groups["top"][0]["open"][0] == f"/jobs/{ids['top_new'][0]}", "a role opens on Jobs")
    app = groups["apps"][0]
    check(app["title"] == "Follow up with Wrenfield Digital Health"
          and app["open"][0] == f"/jobs?show=applied#app-{ids['wrenfield_app']}",
          "Radar's Follow up names the company and opens the item on the board")
    check(app["key"] == f"app:{ids['wrenfield_app']}:{uk_day(0)}", "an application item is keyed on its date")
    # ---- the tile
    b = m["bmodel"]
    check(b["totals"]["applied"] == 2 and b["totals"]["closed"] == 2 and b["totals"]["preparing"] == 1,
          f"the applications tile reads the board ({b['totals']})")
    tile = hp.apps_tile(m)
    check("Closed: 1 no reply (1 closed by Radar) and 1 turned down." in tile,
          "Closed's reasons are in words, with what Radar closed")
    check("Follow up with Wrenfield Digital Health" in tile and "Reply to Calder Neurotech" in tile
          and "due today" in tile and "due tomorrow" in tile,
          "Waiting on you lists the next steps, soonest first, with their due words")
    check('href="/jobs?show=applied#col-preparing"' in tile, "each stage links to its column on the board")
    # ---- a non-hiring urgent story opens on Insights
    conn = rc.get_db(db)
    conn.execute("INSERT INTO signals (url_hash, first_seen, source_id, company, headline, relevance,"
                 " status, story_type, event_key) VALUES ('x-reg', ?, 'mhra', 'Orrin Medical',"
                 " 'A regulation story', 80, 'new', 'regulation', 'ev-reg')", (ago(0.5),))
    conn.commit()
    reg = conn.execute("SELECT id FROM signals WHERE url_hash = 'x-reg'").fetchone()[0]
    conn.close()
    m3 = model_of(db)
    reg_item = next(i for i in m3["todo"] if i["id"] == f"signal-{reg}")
    check(reg_item["open"] == (f"/insights/story/{reg}", "Open the story on Insights", False),
          "any other urgent story opens on Insights")
    check([i["group"] for i in m3["todo"]] == sorted((i["group"] for i in m3["todo"]),
                                                    key=lambda g: [x for x, _ in hp.GROUPS].index(g)),
          "the groups come most urgent first: Radar, urgent signals, Top, One question away, applications")


# ================================================================ 4. Done

def done_checks(tmp: Path) -> None:
    db = tmp / "done.sqlite"
    ids = seed_home(db)
    rc.get_db(db).close()   # the first open's own migrations, before the fingerprint
    before = fingerprint(db)
    m = model_of(db)
    item = next(i for i in m["todo"] if i["group"] == "top")
    conn = rc.get_db(db)
    r1 = hp.mark_done(conn, item["key"], item["title"])
    r2 = hp.mark_done(conn, item["key"], item["title"])
    conn.close()
    check(r1["ok"] and not r2["ok"], "Done takes an item off Home, and a second Done says it is off already")
    m2 = model_of(db)
    check(all(i["key"] != item["key"] for i in m2["todo"]) and m2["counts"]["top"] == 2
          and [i["key"] for i in m2["done"]] == [item["key"]],
          "it leaves To do and its figure, and is listed under Done")
    m3 = model_of(db, show="done")
    check([i["id"] for i in m3["items"]] == [item["id"]] and m3["selected"]["id"] == item["id"],
          "the Done view lists it, selected")
    check(fingerprint(db) == before,
          "and nothing changes where it lives: every table but home_done is as it was")
    m4 = model_of(db)
    check(any(o.get("_unread") and o["id"] == item["role"]["id"] for o in m4["jmodel"]["tiers"]["top"]),
          "the role is still new on Jobs")
    conn = rc.get_db(db)
    u1 = hp.mark_undone(conn, item["key"])
    u2 = hp.mark_undone(conn, item["key"])
    conn.close()
    check(u1["ok"] and not u2["ok"] and model_of(db)["counts"]["top"] == 3,
          "Undo last puts it back, and a second undo says it is on Home already")
    # An application step returns when its date changes.
    app_item = next(i for i in model_of(db)["todo"] if i["group"] == "apps")
    conn = rc.get_db(db)
    hp.mark_done(conn, app_item["key"], app_item["title"])
    conn.close()
    check(model_of(db)["counts"]["apps"] == 0, "an application step done on Home leaves Home")
    conn = rc.get_db(db)
    conn.execute("UPDATE applications SET next_step_due = ? WHERE id = ?", (uk_day(-1), ids["wrenfield_app"]))
    conn.commit()
    conn.close()
    m5 = model_of(db)
    check(m5["counts"]["apps"] == 1 and m5["overdue"] == 1
          and "1 application follow-up due or overdue" in hp.status_sentence(m5),
          "and comes back when its date changes, now overdue and said so")
    # A Radar item is done until its job's state changes.
    fault = tmp / "done-fault.sqlite"
    seed_home(fault, "fault")
    mf = model_of(fault)
    radar = [i for i in mf["todo"] if i["group"] == "radar"]
    stale = next(i for i in radar if i["id"] == "radar-signals")
    conn = rc.get_db(fault)
    hp.mark_done(conn, stale["key"], stale["title"])
    conn.close()
    check(all(i["id"] != "radar-signals" for i in model_of(fault)["todo"]),
          "a Radar item done on Home is off Home")
    conn = rc.get_db(fault)
    conn.execute("INSERT INTO runs (ts, workflow, mode, outcome) VALUES (?, 'signals', 'live', 'ok')",
                 (ago(hours=5),))
    conn.commit()
    conn.close()
    back = next((i for i in model_of(fault)["todo"] if i["id"] == "radar-signals"), None)
    check(back is not None and back["light"]["word"] == "Late",
          "and back when the job's state changes (stale to late here)")
    check(hp.clean_key("role:a b|c") == "role:a b|c", "a role key with its spaces is a key")
    bad = []
    for value in (None, 1, True, "", "nothing:1", "role:" + "x" * 400, "role:a\nb"):
        try:
            hp.clean_key(value)
            bad.append(repr(value)[:20])
        except ValueError:
            pass
    check(not bad, f"anything that is not an item key is refused ({bad})")


# ================================================================ 4b. what the review asked

def set_runs(db: Path, *runs) -> None:
    """Replace the runs with these (workflow, hours ago, mode, outcome)."""
    conn = rc.get_db(db)
    conn.execute("DELETE FROM runs")
    for workflow, hours, mode, outcome in runs:
        conn.execute("INSERT INTO runs (ts, workflow, mode, outcome) VALUES (?,?,?,?)",
                     (ago(hours=hours), workflow, mode, outcome))
    conn.commit()
    conn.close()


def episode_checks(tmp: Path) -> None:
    """Done on a Radar item holds for that fault only: Late, Done, recovered,
    Late again later, and the item is back."""
    db = tmp / "episode.sqlite"
    seed_home(db)
    base = (("inbox", 0.2, "live", "ok"), ("check", 7, "live", "ok"), ("digest", 50, "build", "ok"))
    set_runs(db, *base, ("signals", 5, "live", "ok"))
    m = model_of(db)
    late = next((i for i in m["todo"] if i["id"] == "radar-signals"), None)
    check(late is not None and late["light"]["word"] == "Late", "the watcher five hours on is Late")
    conn = rc.get_db(db)
    hp.mark_done(conn, late["key"], late["title"])
    conn.close()
    m = model_of(db)
    check(all(i["id"] != "radar-signals" for i in m["todo"]) and "watcher" not in hp.status_sentence(m),
          "Done takes it off the list and out of the sentence")
    set_runs(db, *base, ("signals", 5, "live", "ok"), ("signals", 1, "live", "ok"))
    m = model_of(db)
    check(all(i["id"] != "radar-signals" for i in m["todo"])
          and next(l for l in m["lights"] if l["name"] == "signals")["word"] == "On time",
          "it recovers: On time, no item")
    # Later, the watcher misses its runs again: a new episode.
    set_runs(db, *base, ("signals", 9, "live", "ok"), ("signals", 4.5, "live", "ok"))
    m = model_of(db)
    again = next((i for i in m["todo"] if i["id"] == "radar-signals"), None)
    check(again is not None and again["key"] != late["key"]
          and "the signal watcher is late" in hp.status_sentence(m),
          "Late again later is back in the list and the sentence, under a new key")


def review_checks(tmp: Path) -> None:
    db = tmp / "review.sqlite"
    ids = seed_home(db)
    m = model_of(db)
    page = page_of(db)
    check('id="figure-urgent" href="#group-urgent"' in page and 'id="group-urgent"' in page,
          "the urgent figure opens its own group of the list")
    # A role Done as One question away comes back as a new Top prospect.
    q = next(i for i in m["todo"] if i["group"] == "question")
    check(q["key"].startswith("role:question:"), f"a role's key names its group ({q['key']})")
    conn = rc.get_db(db)
    hp.mark_done(conn, q["key"], q["title"])
    conn.close()
    m2 = model_of(db)
    check(m2["counts"]["question"] == 1 and all(i["key"] != q["key"] for i in m2["todo"]),
          "Done on a One question away role takes it off Home")
    funnel = hp.funnel_tile(m2)
    check('id="funnel-question"' in funnel and "1 new" in funnel.split('id="funnel-question"')[1].split("</li>")[0],
          "and Open roles says 1 new, agreeing with the figure")
    conn = rc.get_db(db)
    conn.execute("UPDATE opportunities SET tier = 'top' WHERE id = ?", (q["role"]["id"],))
    conn.commit()
    conn.close()
    m3 = model_of(db)
    back = next((i for i in m3["todo"] if i["group"] == "top" and i["role"]["id"] == q["role"]["id"]), None)
    check(back is not None and m3["counts"]["top"] == 4,
          "promoted to Top prospect it comes back, as a new Top prospect")
    # A buying window whose role is a new Top prospect is listed once.
    conn = rc.get_db(db)
    conn.execute("UPDATE opportunities SET read_at = NULL WHERE id = ?", (ids["hartwell_role"],))
    conn.commit()
    conn.close()
    m4 = model_of(db)
    tops = [i["role"]["id"] for i in m4["todo"] if i["group"] == "top"]
    check(ids["hartwell_role"] in tops and m4["counts"]["urgent"] == 0
          and "urgent signal" not in hp.status_sentence(m4),
          "a buying window read from an advert listed as a new Top prospect is not listed again")
    # The tile's Waiting on you names an interview with no step.
    conn = rc.get_db(db)
    conn.execute("UPDATE applications SET next_step = NULL, next_step_due = NULL, next_step_by = NULL,"
                 " interview_at = ? WHERE id = ?", (uk_day(2) + "T10:00", ids["severn_app"]))
    conn.commit()
    conn.close()
    m5 = model_of(db)
    tile = hp.apps_tile(m5)
    check("Interview with Severn Renal Systems" in tile
          and all("Severn" not in i["title"] for i in m5["todo"]) and m5["counts"]["apps"] == 1,
          "an interview with no step is on the tile's Waiting on you, and never an item or a figure")
    # Done-view rows keep the view in their address.
    conn = rc.get_db(db)
    first = model_of(db)["todo"][0]
    hp.mark_done(conn, first["key"], first["title"])
    conn.close()
    done_page = page_of(db, show="done")
    check(f'href="/?item={first["id"]}&amp;show=done"' in done_page,
          "a row in the Done view links with show=done")
    # The fill layout only on a screen tall enough.
    css = hp.HOME_CSS
    fill = css[css.index("@media (min-height:1300px)"):]
    check("@container app (min-width:1800px)" in fill.split("}}")[0] and "overflow:hidden" in fill.split("}}")[0],
          "from 1800px the list block fills the rest only when the screen is 1300px tall or more")
    # The first name can be cleared from Settings.
    with prefs("keywords: a\nfirst_name: Sam\n") as cfg:
        stored = rc.update_pref_line("first_name", "", cfg)
        text = (rc.REPO_ROOT / cfg["prefs_file"]).read_text(encoding="utf-8")
        check(stored == "" and re.search(r"^first_name:$", text, re.M) and rc.read_first_name(cfg) is None,
              "Settings can clear the first name: the line stays, empty, and Home greets without one")
        try:
            rc.update_pref_line("keywords", "", cfg)
            refused = False
        except ValueError:
            refused = True
        check(refused, "every other line still refuses an empty value")


def greeting_page_checks(tmp: Path) -> None:
    db = tmp / "greet.sqlite"
    seed_home(db, "quiet")
    with prefs("day_rate_floor_gbp: 700\nfirst_name: Sam\n") as cfg:
        page = page_of(db, cfg)
    check(re.search(r'id="welcome-h"><span class="visually-hidden">Home. </span>'
                    r'Good (morning|afternoon|evening), Sam</h1>', page) is not None,
          "a preferences file with first_name: Sam greets Sam on the page")
    with prefs("day_rate_floor_gbp: 700\n") as cfg:
        page = page_of(db, cfg)
    check(re.search(r'</span>Good (morning|afternoon|evening)\.</h1>', page) is not None
          and "Sam" not in page, "and none greets without a name")


def week_exact_checks(tmp: Path) -> None:
    """The week chart on fixed dates, with exact counts: a two-advert repost
    chain counts once, two reports of one story count once, a role Top in the
    history and Reading now counts in its week, and an applied role, a hiring
    story and a story under 40 do not."""
    db = tmp / "weeks.sqlite"
    s = Seed(db)
    at = lambda d, h=12: f"2026-{d}T{h:02d}:00:00Z"  # noqa: E731
    s.role("Repost Role", "Orrin Medical", "top", 80, first_seen=at("11-03"), last_seen=at("11-03"),
           scored_at=at("11-03"))
    s.role("Repost Role", "Orrin Medical", "top", 80, first_seen=at("11-10"), last_seen=at("11-10"),
           scored_at=at("11-10"))
    s.role("This Week Role", "Kinross Optics", "top", 80, first_seen=at("11-10"), last_seen=at("11-10"),
           scored_at=at("11-10"))
    was = s.role("Once Top Role", "Brandt Cardio", "reading", 60, first_seen=at("10-27"),
                 last_seen=at("10-27"), scored_at=at("11-06"))
    s.conn.execute("INSERT INTO score_history (opportunity_id, recorded_at, event, writer, scored_at, tier)"
                   " VALUES (?,?,?,?,?,?)", (was, at("11-06"), "replaced", "rescore", at("10-27"), "top"))
    s.role("Applied Top Role", "Ashdown Diagnostics", "top", 85, first_seen=at("11-04"),
           last_seen=at("11-04"), scored_at=at("11-04"), applied_at=at("11-05"))
    s.signal("One story, first report", "Orrin Medical", 50, 0, first_seen=at("11-05"), event_key="ev-shared")
    s.signal("One story, second report", "Orrin Medical", 30, 0, first_seen=at("11-04"), event_key="ev-shared")
    s.signal("A story this week", "Orrin Medical", 45, 0, first_seen=at("11-10"))
    s.signal("A buying window", "Hartwell Infusion Ltd", 90, 0, kind="buying-window", source="job-advert",
             first_seen=at("11-10"))
    s.signal("Under the bar", "Orrin Medical", 39, 0, first_seen=at("11-10"))
    s.signal("Hidden, early", "Orrin Medical", 41, 0, first_seen=at("10-20"), acknowledged_at=at("10-21"))
    s.conn.commit()
    s.close()
    conn = reader(db)
    data = bd.collect(conn, CFG)
    w = hp.week_model(data, hp.top_history(conn), datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
    conn.close()
    got = [(str(x["week"]), x["top"], x["signals"]) for x in w["weeks"]]
    check(got == [("2026-10-19", 0, 1), ("2026-10-26", 1, 0), ("2026-11-02", 1, 1), ("2026-11-09", 1, 1)],
          f"each week's Top prospects and signals at 40 or more, exactly ({got})")
    check(w["this"] == w["weeks"][-1] and w["last"]["top"] == 1 and not w["first_week"],
          "this week and last week are the last two weeks")


# ================================================================ 5. the page

def page_checks(tmp: Path) -> None:
    db = tmp / "page.sqlite"
    ids = seed_home(db)
    html = page_of(db)
    body = html.split("<body>", 1)[1]
    check(body.count("<h1") == 1 and 'id="welcome-h"' in body, "one h1, the greeting")
    figs = re.findall(r'id="figure-(\w+)"', body)
    check(figs == ["urgent", "top", "question", "apps"] and "threads due" not in body.lower()
          and "Threads due" not in body,
          f"four figures and no Threads due anywhere ({figs})")
    strip = body.split('class="ht-strip-list"', 1)[1].split("</ul>", 1)[0]
    lights = re.findall(r'data-job="(\w+)"><span class="ht-light (is-\w+)"><span class="ht-lamp">'
                        r'<svg[^>]*><use href="#(st-\w+)"></use></svg></span>([A-Za-z ]+)</span>', strip)
    check([x[0] for x in lights] == ["inbox", "signals", "check", "digest", "push"]
          and all(x[3] in hp.LIGHTS for x in lights),
          "five lights, each a mark, a colour and its word")
    check('href="/radar">Radar\'s health</a>' in body and 'href="/cv#settings"' in body,
          "Radar's health and Settings are one link away")
    check("Light" not in re.sub(r"<[^>]+>", " ", body.split("</header>", 1)[0])
          and 'aria-label="Appearance"' not in body,
          "no light or dark switch: Radar stays dark only")
    check('aria-current="page">Home</a>' in body, "the top bar marks Home current")
    rows = re.findall(r'<a class="row row-3[^"]*" id="row-([\w-]+)" href="/\?item=([\w-]+)"', body)
    check(rows and all(a == b for a, b in rows), "every item's row links to its own address")
    selected = re.findall(r'id="row-([\w-]+)"[^>]*aria-current="true"', body)
    check(len(selected) == 1, "one row is selected")
    pane = body.split('id="ny-pane"', 1)[1].split('id="ny-store"', 1)[0]
    check('data-done="' in pane and "data-open" in pane and "Done takes this off Home" in pane,
          "the selected item's detail has Open, Done and the line under them")
    # Every control is a link or a button, so the keyboard reaches it.
    clickable = re.findall(r"<(div|span|li|p)[^>]*\sonclick=", body)
    check(not clickable and "tabindex=\"-1\"" not in body.replace('tabindex="-1">', ""),
          "every control is a link or a button")
    check(" color " not in re.sub(r"<[^>]+>", " ", body).lower()
          and "favorite" not in body.lower(), "UK English in the words")
    item = f"role-{ids['top_new'][1]}"
    with_item = page_of(db, item=item)
    check(f'id="row-{item}" href="/?item={item}" data-item="{item}" data-key' in with_item
          and re.search(rf'id="row-{item}"[^>]*aria-current="true"', with_item) is not None,
          "an item's address selects it")
    gone = page_of(db, item="role-999999")
    check("That item is no longer on Home" in gone, "an address for an item not on Home says so")
    # The fragment and the Done view, through the server's own functions.
    with pointed_at(db):
        frag = serve_dashboard.render_home_main({"item": [item]})
        done_page = serve_dashboard.render("home", {"show": ["done"]}).decode()
        full = serve_dashboard.render("home", {"item": [item]}).decode()
        junk = serve_dashboard.render("home", {"item": ["<script>"]}).decode()
    check(frag["item"] == item and 'id="ny"' in frag["html"] and frag["count"] == 7,
          "the main fragment comes back with its selection and count")
    check("Done on Home" in done_page and "Nothing is done on Home" in done_page,
          "the Done view, empty")
    check(full.count('id="welcome-h"') == 1, "the server renders the page")
    check("<script>" not in junk.split("</head>", 1)[1].split("<script>", 1)[0],
          "an address with markup in it selects nothing and prints nothing")
    for width_rule in ("@container ny (min-width:720px)", "@container ny (min-width:1500px)",
                       "@container app (min-width:1800px)", "@media (max-width:767.98px)"):
        check(width_rule in hp.HOME_CSS, f"the layout has its rule {width_rule}")
    check("@media (pointer:coarse)" in hp.HOME_CSS and "min-height:44px" in hp.HOME_CSS,
          "44px targets on a phone and on touch")


# ================================================================ 6. the states

def state_checks(tmp: Path) -> None:
    db = tmp / "quiet.sqlite"
    seed_home(db, "quiet")
    m = model_of(db)
    html = page_of(db)
    check(not m["todo"] and hp.status_sentence(m) == "Nothing needs you today.",
          "a quiet day: nothing needs you today")
    check('class="ht-quiet"' in html and "The signal watcher last ran at" in html,
          "and a line of what Radar read and when")
    check(html.count('class="ht-figlink is-zero"') == 4, "the four figures stay, each at 0, dimmed")
    check("ny-is-empty" in html and "Look through the 1 One question away role on Jobs" in html,
          "the list block gives one fact and one next step")
    gone = page_of(db, item="radar-signals")
    check("ny-is-empty" in gone and "That item is no longer on Home" in gone
          and gone.index("That item is no longer on Home") < gone.index("Nothing needs you today. An item"),
          "an address for an item that has gone says so on an empty Home too, above the empty state")
    check("That item is no longer on Home" not in html, "and an empty Home without an address does not")
    db = tmp / "first.sqlite"
    seed_home(db, "first")
    m = model_of(db)
    html = page_of(db)
    check(m["week"]["first_week"] and "Trends start after your first full week" in html
          and 'class="ht-plot"' not in html,
          "the first week: this week's two figures and the day the trend starts, no empty axes")
    db = tmp / "noapps.sqlite"
    seed_home(db, "noapps")
    m = model_of(db)
    html = page_of(db)
    check("No applications yet. Radar adds an item when you save a tailored CV or mark a role applied."
          in html and m["counts"]["apps"] == 0 and "group-apps" not in html,
          "no applications yet: the tile's one fact and link, no Applications group")
    check("application follow-up" not in hp.status_sentence(m)
          and re.search(r'class="ht-figlink is-zero" id="figure-apps"', html),
          "the sentence leaves the zero out and its figure stays at 0")
    db = tmp / "fault.sqlite"
    seed_home(db, "fault")
    m = model_of(db)
    html = page_of(db)
    words = {l["name"]: l["word"] for l in m["lights"]}
    check(words == {"inbox": "Quiet", "signals": "Stale", "check": "Late", "digest": "Dry run",
                    "push": "Stale"},
          f"a Radar fault: the strip names each job's state in a word ({words})")
    radar = [i for i in m["todo"] if i["group"] == "radar"]
    check([i["id"] for i in radar] == ["radar-signals", "radar-check", "radar-push"]
          and m["selected"]["id"] == "radar-signals",
          "the faults head the list, the first selected; the quiet inbox and the digest in dry run are not there")
    check("Signal watcher is stale, 22 hours since its last run" in html
          and "3 of 5 jobs need a look" in html and "Stale after 6 hours" in html,
          "the stale watcher is named with its age and its limit")
    check(hp.status_sentence(m).endswith(
          "the signal watcher is stale, the advert checker is late and the phone push is stale."),
          f"and the sentence names the faults after the four counts ({hp.status_sentence(m)})")
    check("Check now" in html and "Signals as of" in html,
          "the watcher's item offers Check now, and the trend says how old its signals are")
    check('<table class="table">' in html and "<caption" in html,
          "the item's last runs are a table with a caption")


# ================================================================ 7. the routes

@contextlib.contextmanager
def serving(db: Path):
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
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def route_checks(tmp: Path) -> None:
    db = tmp / "routes.sqlite"
    seed_home(db)
    item = next(i for i in model_of(db)["todo"] if i["group"] == "top")
    with serving(db) as port:
        own = f"127.0.0.1:{port}"
        write = {"Host": own, "Origin": f"http://{own}", "Content-Type": "application/json",
                 serve_dashboard.WRITE_HEADER: "1"}
        body = json.dumps({"key": item["key"], "title": item["title"]})
        forged = call(port, "POST", "/home/done", body,
                      {"Host": own, "Origin": "http://evil.example", "Content-Type": "application/json"})
        check(forged[0] == 403 and not rc.get_db(db).execute("SELECT COUNT(*) FROM home_done").fetchone()[0],
              "a forged Done is refused by the guard and writes nothing")
        bad = [call(port, "POST", "/home/done", b, write)[0] for b in (
            json.dumps({"key": True}), json.dumps({"key": "nothing"}), json.dumps({}),
            json.dumps({"key": item["key"], "title": 3}), "not json")]
        check(bad == [400] * 5, f"a key that is not an item key, or a title that is not text, is a 400 ({bad})")
        s1, _t, b1 = call(port, "POST", "/home/done", body, write)
        s2, _t, b2 = call(port, "POST", "/home/done", body, write)
        check(s1 == 200 and json.loads(b1)["done"] is True and s2 == 409,
              "Done is a 200, and Done again a 409")
        s3, _t, b3 = call(port, "GET", f"/?part=main&item={item['id']}", headers={"Host": own})
        frag = json.loads(b3)
        check(s3 == 200 and f'id="row-{item["id"]}"' not in frag["html"]
              and frag["item"] is not None, "the fragment no longer lists it and selects another")
        s4, _t, _b4 = call(port, "POST", "/home/undone", body, write)
        s5, _t, _b5 = call(port, "POST", "/home/undone", body, write)
        check(s4 == 200 and s5 == 409, "Undo is a 200, and again a 409")
        s6, ctype, page = call(port, "GET", "/", headers={"Host": own})
        check(s6 == 200 and "text/html" in ctype and b'id="welcome-h"' in page, "GET / serves Home")
        s7, _t, radar = call(port, "GET", "/radar", headers={"Host": own})
        check(s7 == 200 and b"<h1>Radar&#x27;s health</h1>" in radar.replace(b"Radar's", b"Radar&#x27;s")
              and b"The machine" in radar and b"Board freshness" in radar,
              "GET /radar serves Radar's health, the old Home's telemetry")
        check(b'class="entrance"' not in radar and b'id="pref-target_title"' not in radar,
              "without the entrance cards or the scoring panel")
        s8, _t, cv = call(port, "GET", "/cv", headers={"Host": own})
        check(s8 == 200 and b'id="settings"' in cv and b'id="pref-first_name"' in cv
              and b'data-pref-save="target_title"' in cv,
              "the CV page has Settings, with the first name beside the scorer's lines")


# ================================================================ 8. the script

def script_checks(tmp: Path) -> None:
    js = hp.HOME_SCRIPT
    node = shutil.which("node")
    if node:
        path = tmp / "home.js"
        path.write_text(js, encoding="utf-8")
        out = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        check(out.returncode == 0, f"the script parses ({out.stderr.strip()[:200]})")
    else:
        print("NOTE  node is not installed, the script parse check is skipped")
    check("if (typing(e.target) || keys.open) return;" in js
          and "if (!keysOn()) return;" in js and "radar-home-keys" in js,
          "the keys rest while typing and turn off with the shortcut switch (WCAG 2.1.4)")
    check("'/home/done'" in js and "'/home/undone'" in js and "sessionStorage" in js,
          "Done and Undo last post to the two routes, with the undo stack kept for the tab")
    check("method: 'POST'" in js, "every write is a fetch POST, so the write header goes with it")
    run_all = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("test/run_home_test.py" in run_all, "run_all.sh runs this file")


def harness_checks(tmp: Path) -> None:
    """Home's own script, driven in node by test/home_script_harness.js."""
    node = shutil.which("node")
    if not node:
        print("NOTE  node is not installed, the script harness is skipped")
        return
    path = tmp / "home_script.js"
    path.write_text(hp.HOME_SCRIPT, encoding="utf-8")
    out = subprocess.run([node, str(TEST_DIR / "home_script_harness.js"), str(path)],
                         capture_output=True, text=True, timeout=120)
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
    for ln in lines:
        check(ln.startswith("PASS"), "the Home script: " + ln[6:])
    check(out.returncode == 0 and len(lines) >= 15,
          f"the Home script runs its keys, Done, Undo and the toast in node ({out.stderr.strip()[:200]})")


# ================================================================ 9. the box checks

def box_doc_checks(tmp: Path) -> None:
    doc = (REPO_ROOT / "docs" / "box-checks-home-final.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
    check(len(blocks) >= 6 and all(b.strip() for b in blocks),
          f"the box checks have their blocks ({len(blocks)}) and none is empty")
    for name in ("## BEFORE", "## The deploy, one block at a time", "## AFTER", "## Rollback"):
        check(name in doc, f"the box checks have {name[3:]}")
    rollback = doc[doc.index("## Rollback"):]
    check("backup" in rollback and "restore" not in rollback.replace("is not restored", "")
          and 'reset --hard "$(cat .git/rollback-home-final)"' in rollback
          and "docker restart medtech-radar" in rollback,
          "the rollback goes back to the saved commit, restarts the dashboard and never restores the backup")
    check("first_name:" in doc and "preferences.md" in doc, "the box checks say how to add the first name")
    gits = re.findall(r"(?:^|[;&|{(] *)(git) ", "\n".join(blocks), re.M)
    check(not gits, "every git in the blocks runs as radar")
    runs = [b for b in blocks if re.search(r'bin/python |"\$PY"', b)]
    check(len(runs) >= 4 and all("PYTHONDONTWRITEBYTECODE=1" in b for b in runs),
          "every python in the blocks runs with PYTHONDONTWRITEBYTECODE=1")
    queries = re.findall(r' Q "(.*?)"(?:;|$| \|)', doc, re.M)
    db = tmp / "doc.sqlite"
    seed_home(db)
    conn = reader(db)
    bad = []
    for q in queries:
        try:
            conn.execute(q).fetchall()
        except sqlite3.Error as err:
            bad.append(f"{q[:60]}: {err}")
    conn.close()
    check("That item is no longer on Home" in doc and "?item=" in doc,
          "AFTER looks for the item's row, or the line that says it is gone, at an item's address")
    check(len(queries) >= 6 and not bad,
          f"every query in the box checks runs on a seeded database ({len(queries)}; bad: {bad})")
    snippets = re.findall(r'python -c "(.*?)"(?:;| &&| \)|$|\s*\|)', doc, re.M)
    broken = []
    for code in snippets:
        try:
            compile(code, "box", "exec")
        except SyntaxError as err:
            broken.append(f"{code[:50]}: {err}")
    check(len(snippets) >= 2 and not broken,
          f"every python -c in them compiles ({len(snippets)}; broken: {broken})")
    shell = [c[:40] for c in snippets if "$" in c or "`" in c]
    check(not shell, f"no python -c carries a $ or a backtick the shell would expand ({shell})")
    look = [c for c in snippets if "home_page as hp" in c]
    ran = []
    for code in look:
        code = code.replace("file:db/radar.sqlite?mode=ro", f"file:{db.as_posix()}?mode=ro")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), capture_output=True,
                             text=True, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        ran.append(out.returncode == 0 and "lights:" in out.stdout and "groups:" in out.stdout)
    check(look and all(ran), "step 6's look at what Home will list runs on a seeded database")
    check(all("!" not in b for b in blocks), "no block carries a ! that bash would expand")
    arch = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check("home_page.py" in arch and "home_done" in arch, "the architecture note names Home's parts")


def main() -> int:
    ap_ = argparse.ArgumentParser(description="Home final checks")
    ap_.add_argument("--db", help="accepted for suite symmetry, unused")
    ap_.add_argument("--seed", help="write a seeded database here and stop (for screenshots)")
    ap_.add_argument("--variant", default="typical")
    args = ap_.parse_args()
    if args.seed:
        path = Path(args.seed)
        if path.exists():
            path.unlink()
        print(json.dumps(seed_home(path, args.variant)))
        return 0
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        for section in (name_checks, light_checks, model_checks, done_checks, episode_checks,
                        review_checks, greeting_page_checks, week_exact_checks, page_checks,
                        state_checks, route_checks, script_checks, harness_checks, box_doc_checks):
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
    print("PASS. Every Home check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
