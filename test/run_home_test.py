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
   Stale, Off or Not sending (grey, never amber), each with its own mark; the
   phone push worked out from the signal watcher's runs and the ntfy address.
   The top bar's light, which reads the same rules, is checked with the
   header in test/run_header_test.py.
3. The model on a seeded typical day: five figures, the stories and
   follow-ups each equal to its group of the list, Top prospects and One
   question away equal to the Jobs page's tier bands (jobs_page.tier_totals,
   both pages drawn through the server), an applied role
   in no count and no list (the control: undo it and it is counted), the
   funnel as the Jobs tabs count, the applications tile from the board, the
   week chart rebuilt from score history, and where each item opens.
4. Done: off Home only, Undo puts it back, a Radar item back when its state
   changes, an application step back when its date changes, and the item where
   it lives untouched (the database apart from home_done is byte for byte
   the same).
5. The page (trimmed 17 Nov): one h1, the date and the greeting and no
   sentence under them, the five figures with 0 dimmed, the lights in words,
   the health panel closed with Week by week, the flow and board freshness in
   it, Open roles in three rows, every item in full with no carousel, an
   urgent signal as an Insights story, no middle dot, no key hints, no
   question under a title, no footnote and no As of time.
6. The states: a quiet day, the first week, no applications yet and a Radar
   fault, as the states board draws them.
7. The routes: POST /home/done and /home/undone through the guard, their 400s,
   409s and 200s, a forged request refused with nothing written, GET /radar
   (Radar's health, the old Home) and the CV page's Settings.
5b. A role item is the Jobs row, character for character, linking to the role
   on Jobs, with Done and nothing else; no question line and no gate reason
   across the page; the .status rule wraps.
8. The script parses (node --check) and listens for no keys.
9. The box checks: no command block is empty, every query in them runs on a
   seeded database, every python -c compiles, and the rollback never
   restores the backup.
10. Company follow-ups due: the fifth figure equals its group, read by the Watchlist's
   own rule (watchlist.threads_due): only a Watchlist company's next step due
   today or overdue counts; one not on the Watchlist, one with no date and
   one due tomorrow do not; removing the company stops it counting; Done
   holds until the date moves.
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
import jobs_page as jp  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402
import watchlist  # noqa: E402

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


def role_rows(html: str) -> dict:
    """{role id: its Jobs row} for every role item on Home."""
    return {int(m.group(1)): m.group(0) for m in re.finditer(
        r'<div class="row row-queue[^"]*" data-id="(\d+)".*?</span></span></div>', html, re.S)}


def esc_attr(text: str) -> str:
    import html as _h
    return _h.escape(text, quote=True)


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
          "the signal watcher and the advert checker in dry run say their state, never Not sending")
    check(hp.job_light("digest", v("ok", "dry-run"))["word"] == "Not sending"
          and hp.job_light("digest", v("ok", "build"))["word"] == "On time"
          and hp.job_light("digest", v("ok", "commit"))["word"] == "On time",
          "the digest on time in dry run says Not sending; its build and commit are live")
    check(hp.job_light("digest", v("stale", "dry-run"))["word"] == "Stale",
          "a stale digest says Stale, dry run or not")
    marks = {w: sym for w, (_c, sym) in hp.LIGHTS.items()}
    check(len(set(marks.values())) == len(marks) == 6,
          f"each of the six words has its own mark as well as a colour ({marks})")
    check(marks["Late"] != marks["Not sending"], "Late and Not sending differ in shape, not only in words")
    check(hp.LIGHTS["Not sending"][0] == "is-neu" and "Dry run" not in hp.LIGHTS,
          "Not sending is grey, never amber, and Dry run is no longer a word on Home")
    # That the top bar's light reads these same rules is checked with the
    # header, in test/run_header_test.py.
    jobs ={"signals": v("ok", "live")}
    check(hp.push_light(jobs, {"ntfy_url": "", "ntfy_topic_radar": "x"}, None)["word"] == "Off",
          "the push is Off when radar.yaml names no ntfy address")
    check(hp.push_light({"signals": {"state": "never"}}, CFG, None)["word"] == "Off",
          "and Off when the watcher has never run")
    check(hp.push_light({"signals": v("stale", "dry-run")}, CFG, None)["word"] == "Stale"
          and hp.push_light({"signals": v("late", "dry-run")}, CFG, None)["word"] == "Late",
          "the watcher's state comes first: Stale or Late, in dry run or not")
    check(hp.push_light({"signals": v("ok", "dry-run")}, CFG, None)["word"] == "Not sending",
          "then Not sending while the watcher runs in dry run")
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
                   "digest": "Not sending", "push": "Not sending"},
          f"the box as it is set up: a quiet inbox, the digest and the push in dry run ({said})")
    check(not [i for i in m["todo"] if i["group"] == "radar"],
          "and none of it is an item: Not sending and Quiet are not faults")
    html = page_of(db)
    for job in ("digest", "push"):
        li = re.search(rf'<li class="ht-strip-job" data-job="{job}">(.*?)</li>', html)
        check(li is not None and 'class="ht-light is-neu"' in li.group(1) and "Not sending" in li.group(1)
              and 'class="ht-dot is-neu"' in li.group(1),
              f"the {job} light on the page is grey, dot and word, and says Not sending")


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
    # The Top prospects and One question away tiles are every open role in
    # the tier, the number on the Jobs page's tier band: both pages are drawn
    # here through the server from this database, and must agree. A role Done
    # on Home is still an open role, so it stays in the tile.
    with pointed_at(db):
        home_html = serve_dashboard.render("home", {}).decode()
        jobs_html = serve_dashboard.render("jobs", {}).decode()
    tiles = {k: int(n) for k, n in re.findall(
        r'id="figure-(top|question)"[^>]*>.*?<span class="ht-fignum">(\d+)</span>', home_html, re.S)}
    bands = {k: int(n) for k, n in re.findall(
        r'<h2 class="group-title" id="group-(top|question|reading)">.*?<span class="group-count">(\d+)</span>',
        jobs_html, re.S)}
    shared = jp.tier_totals(j)
    check(tiles == {"top": bands.get("top"), "question": bands.get("question")}
          and tiles == {"top": shared["top"], "question": shared["question"]}
          and tiles["top"] == 5 and tiles["question"] == 3
          and hp.figure_counts(m)["top"] == tiles["top"],
          f"the Top prospects and One question away tiles are the Jobs page's tier bands, from one "
          f"function, jobs_page.tier_totals (Home {tiles}, Jobs {bands})")
    conn = rc.get_db(db)
    hp.mark_done(conn, groups["top"][0]["key"], "a Top prospect")
    conn.close()
    with pointed_at(db):
        after = serve_dashboard.render("home", {}).decode()
    left = re.search(r'id="figure-top"[^>]*>.*?<span class="ht-fignum">(\d+)</span>', after, re.S)
    check(left is not None and int(left.group(1)) == tiles["top"],
          "a Top prospect Done on Home is still an open role, so its tile does not drop")
    conn = rc.get_db(db)
    hp.mark_undone(conn, groups["top"][0]["key"])
    conn.close()
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
    check("Radar fills" not in tile and "Filled by Radar" not in tile and "apm-radar" not in hp.HOME_CSS,
          "and no line on what Radar fills by itself")
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


# ================================================================ 3b. threads due

def thread_checks(tmp: Path) -> None:
    """The fifth figure and its group, from watchlist.threads_due."""
    db = tmp / "threads.sqlite"
    seed_home(db)
    conn = rc.get_db(db)
    cids = {r["display_name"]: r["id"] for r in conn.execute("SELECT id, display_name FROM companies")}

    def touch(name, step, due, days_ago=20, channel="comment"):
        conn.execute("INSERT INTO touches (company, touched_at, channel, next_action, next_action_date,"
                     " company_id) VALUES (?,?,?,?,?,?)",
                     (name, ago(days_ago), channel, step, due, cids[name]))
    # Watched and due: one overdue by a day, one due today.
    touch("Lumenhal Photonics", "Send the connection note to the chief executive.", uk_day(-1))
    touch("Kinross Optics", "One genuine engagement with their content, brief.", uk_day(0), 19,
          "connection-note")
    # Watched and not due: a next step with no date, one due tomorrow.
    touch("Brandt Cardio", "Comment on the next post", None)
    touch("PDX Diagnostics", "Ask about the analyser launch", uk_day(1))
    # Due today, but not on the Watchlist.
    touch("Marchetti Devices", "Reply to the interim brief", uk_day(0))
    for name in ("Lumenhal Photonics", "Kinross Optics", "Brandt Cardio", "PDX Diagnostics"):
        watchlist.watch(conn, cids[name], "name")
    conn.commit()
    conn.close()
    lum, kin = cids["Lumenhal Photonics"], cids["Kinross Optics"]
    m = model_of(db)
    group = [i for i in m["todo"] if i["group"] == "threads"]
    check(m["counts"]["threads"] == len(group) == 2
          and [i["company_id"] for i in group] == [lum, kin],
          f"the fifth figure equals the Company follow-ups due group, most overdue first "
          f"({m['counts']['threads']}, {[i['title'] for i in group]})")
    ro = reader(db)
    try:
        rule = watchlist.threads_due(ro)
    finally:
        ro.close()
    check(m["counts"]["threads"] == len(rule) and [t["company_id"] for t in rule] == [lum, kin],
          "and is the Watchlist's own rule, watchlist.threads_due")
    check(all(i["company_id"] != cids["Marchetti Devices"] for i in group),
          "a thread due today for a company not on the Watchlist is not counted")
    check(all(i["company_id"] != cids["Brandt Cardio"] for i in group),
          "a next step with no date is not counted")
    check(all(i["company_id"] != cids["PDX Diagnostics"] for i in group),
          "a thread due tomorrow is not counted")
    check([m["counts"][g] for g in ("urgent", "top", "question", "threads", "apps")] == [1, 3, 2, 2, 1],
          f"the five figures count company follow-ups due fourth ({m['counts']})")
    first, second = group
    check(first["title"] == "Lumenhal Photonics" and first["lead"] == "Send the connection note to the chief executive."
          and first["seen"] == hp.day(uk_day(-1)) and first["meta"] == f"Last contact {hp.day(ago(20))}, comment"
          and "Overdue by 1 day" in first["tag"] and 'tag-caution' in first["tag"] and "#st-cau" in first["tag"],
          "a thread item: the company, its next step, Due, Last contact and an Overdue tag with its mark")
    check("Due today" in second["tag"] and second["meta"].endswith(", connection note"),
          "one due today says Due today")
    check(first["open"] == (f"/watchlist/{lum}", "Open it on the Watchlist", False)
          and first["key"] == f"thread:{lum}:{uk_day(-1)}" and hp.clean_key(first["key"]) == first["key"],
          "it opens on the Watchlist and is keyed on the company and its due date")
    check([g for g, _w in hp.GROUPS].index("threads") == [g for g, _w in hp.GROUPS].index("question") + 1
          == [g for g, _w in hp.GROUPS].index("apps") - 1,
          "Company follow-ups due sits after One question away and before Applications")
    html = page_of(db)
    fig = re.search(r'<a class="ht-figlink glow-tile g-apps" id="figure-threads" href="([^"]+)" '
                    r'data-group="threads"><span class="gchip g-apps".*?</svg></span>'
                    r'<span class="ht-fignum">(\d+)</span><span class="ht-figword">([^<]+)</span>', html)
    check(fig is not None and fig.groups() == ("/watchlist", "2", "company follow-ups due"),
          f"the figure: 2 company follow-ups due, linking to the Watchlist ({fig.groups() if fig else None})")
    block = html.split('<div class="ny-group" id="group-threads">', 1)[1] if 'id="group-threads"' in html else ""
    check(block and html.index('id="group-question"') < html.index('id="group-threads"')
          < html.index('id="group-apps"')
          and block.startswith('<article class="ny-item ny-follow glow-wide g-apps"')
          and 'class="group-title"' not in html and "2 due, 1 overdue" not in html,
          "the follow-ups are drawn between One question away and Applications, teal cards with no "
          "subheading over them")
    check(f'id="thread-{lum}" data-item="thread-{lum}" data-key="thread:{lum}:{uk_day(-1)}"' in html,
          "each follow-up has its own address and Done key")
    sel = page_of(db, item=f"thread-{lum}")
    check(f'href="/watchlist/{lum}" data-open' in sel and '"target": "thread-' in sel.replace("&quot;", '"'),
          "it opens on the Watchlist, and its own address brings it into view")
    # Done holds until the date moves.
    conn = rc.get_db(db)
    hp.mark_done(conn, first["key"], first["title"])
    conn.close()
    m2 = model_of(db)
    check(m2["counts"]["threads"] == 1
          and all(i["company_id"] != lum for i in m2["todo"] if i["group"] == "threads"),
          "Done takes a thread off Home and out of the figure")
    conn = rc.get_db(db)
    conn.execute("UPDATE touches SET next_action_date = ? WHERE company_id = ? AND next_action IS NOT NULL",
                 (uk_day(0), lum))
    conn.commit()
    conn.close()
    m3 = model_of(db)
    check(m3["counts"]["threads"] == 2
          and any(i["key"] == f"thread:{lum}:{uk_day(0)}" for i in m3["todo"]),
          "and a new due date brings it back")
    # Removing the company stops it counting; adding it again restores it.
    conn = rc.get_db(db)
    watchlist.unwatch(conn, kin)
    conn.commit()
    conn.close()
    m4 = model_of(db)
    check(m4["counts"]["threads"] == 1
          and all(i["company_id"] != kin for i in m4["todo"] if i["group"] == "threads"),
          "removing a company from the Watchlist stops its thread counting")
    conn = rc.get_db(db)
    watchlist.restore(conn, kin)
    conn.commit()
    conn.close()
    check(model_of(db)["counts"]["threads"] == 2, "the control: back on the Watchlist, it counts again")


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
          and "1</span><span class=\"ht-figword\">application follow-up due or overdue" in hp.figures_html(m5),
          "and comes back when its date changes, now overdue and said so on its figure")
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
    check(all(i["id"] != "radar-signals" for i in m["todo"]),
          "Done takes it off the list")
    set_runs(db, *base, ("signals", 5, "live", "ok"), ("signals", 1, "live", "ok"))
    m = model_of(db)
    check(all(i["id"] != "radar-signals" for i in m["todo"])
          and next(l for l in m["lights"] if l["name"] == "signals")["word"] == "On time",
          "it recovers: On time, no item")
    # Later, the watcher misses its runs again: a new episode.
    set_runs(db, *base, ("signals", 9, "live", "ok"), ("signals", 4.5, "live", "ok"))
    m = model_of(db)
    again = next((i for i in m["todo"] if i["id"] == "radar-signals"), None)
    check(again is not None and again["key"] != late["key"],
          "Late again later is back in the list, under a new key")


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
    jobs = hp.jobs_tile(m2)
    row = jobs.split('id="funnel-question"')[1].split("</li>")[0] if 'id="funnel-question"' in jobs else ""
    check(row and f'<span class="hj-fig">{m2["jmodel"]["totals"]["question"]}</span>' in row
          and " new" not in jobs and hp.figure_counts(m2)["question"] == m2["jmodel"]["totals"]["question"],
          "and the Jobs card and the tile still count it, every open role in the tier, with no new count")
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
    check(ids["hartwell_role"] in tops and m4["counts"]["urgent"] == 0,
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
    # The Done view lists the item in full, with Put back in place of Done.
    conn = rc.get_db(db)
    first = model_of(db)["todo"][0]
    hp.mark_done(conn, first["key"], first["title"])
    conn.close()
    done_page = page_of(db, show="done")
    check(f'id="{first["id"]}" data-item="{first["id"]}"' in done_page
          and f'data-back="{first["key"]}"' in done_page and "data-done=" not in done_page,
          "the Done view shows the item in full with Put back")
    # The page scrolls as one document: no list block one screen high, no inner scrolling.
    css = hp.HOME_CSS
    check("@media (min-height:1300px)" not in css and "scroll-snap" not in css and "100dvh" not in css,
          "the page scrolls as one document at every width")
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
    check(figs == ["urgent", "top", "question", "threads", "apps"]
          and 'class="ht-figs ht-figs-5"' in body and 'id="group-threads"' not in body,
          f"five figures, company follow-ups due fourth, and no group for them with none due ({figs})")
    check(re.search(r'class="ht-figlink glow-tile g-apps is-zero" id="figure-threads"', body) is not None
          and re.search(r'class="ht-figlink glow-tile g-jobs" id="figure-top"', body) is not None
          and ".glow-tile.is-zero{" in hp.HOME_CSS
          and ".ht-figlink.is-zero .ht-fignum{color:var(--text-3)}" in hp.HOME_CSS,
          "a tile at 0 keeps its group's colour with the softer glow and a muted number")
    tiles = re.findall(r'class="ht-figlink glow-tile (g-\w+)[^"]*" id="figure-(\w+)"[^>]*>'
                       r'<span class="gchip (g-\w+)" aria-hidden="true"><svg viewBox="0 0 20 20" width="18" '
                       r'height="18" fill="none" stroke="currentColor" stroke-width="1.6" '
                       r'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">', body)
    check([(t[1], t[0]) for t in tiles] == [("urgent", "g-story"), ("top", "g-jobs"), ("question", "g-jobs"),
                                            ("threads", "g-apps"), ("apps", "g-apps")]
          and all(t[0] == t[2] for t in tiles),
          f"each tile has its group's colour and icon chip: stories amber, jobs purple, follow-ups teal ({tiles})")
    welcome = body.split('<div class="ht-welcome-in">', 1)[1].split('<div class="ht-strip"', 1)[0]
    texts = [t.strip() for t in re.sub(r"<[^>]+>", "\n", welcome).split("\n") if t.strip()]
    check(len(texts) == 3 and texts[1] == "Home." and re.match(r"\w+day \d+ \w+$", texts[0])
          and texts[2].startswith("Good ") and re.search(r'</section><nav class="ht-figs', body),
          f"the top section: the date and the greeting, no card and no sentence, then the tiles ({texts})")
    for gone in ("things need you", "thing needs you", "Nothing needs you today", "As of", "Times are UK time",
                 "Settings, on the CV page", "ht-status", "ht-asof", "ht-quiet"):
        check(gone not in body, f"no {gone!r} on Home")
    strip = body.split('class="ht-strip-list"', 1)[1].split("</ul>", 1)[0]
    lights = re.findall(r'data-job="(\w+)"><span class="ht-dot (is-\w+)" aria-hidden="true"></span>'
                        r'<span class="ht-job-name">([^<]+)</span><span class="visually-hidden">, </span>'
                        r'<span class="ht-light (is-\w+)">([A-Za-z ]+)</span></li>', strip)
    check([x[0] for x in lights] == ["inbox", "signals", "check", "digest", "push"]
          and [x[2] for x in lights] == ["Inbox", "News watcher", "Advert checker", "Monday digest", "Phone push"]
          and all(x[4] in hp.LIGHTS and x[1] == x[3] == hp.LIGHTS[x[4]][0] for x in lights),
          "five lights as chips, each a dot, the job's name and its word, the signal watcher named "
          "the News watcher")
    check("""<button type="button" class="ht-health-btn" id="health-btn" aria-expanded="false" """
          """aria-controls="health-panel">Radar's health</button>""" in body
          and ".ht-health-btn{" in hp.HOME_CSS and "text-decoration:underline" in hp.HOME_CSS.split(".ht-health-btn{", 1)[1].split("}", 1)[0],
          "Radar's health ends the row, a button drawn as a plain underlined link")
    panel = body.split('id="health-panel"', 1)[1].split('class="hf-bento"', 1)[0] if 'id="health-panel"' in body else ""
    check('id="health-btn" aria-expanded="false" aria-controls="health-panel"' in body
          and '<div class="ht-health" id="health-panel" hidden>' in body,
          "the health strip has a button for its panel, closed by default")
    check(panel and panel.index("Week by week") < panel.index("The flow, last fourteen days")
          < panel.index("Board freshness") < panel.index('href="/radar">Open the full health page</a>'),
          "the panel holds Week by week, the flow, board freshness, then the full health page")
    check('href="/jobs#group-top">Open Top prospects</a>' in panel and 'href="/insights">Open Insights</a>' in panel
          and "This week " in panel, "Week by week keeps its chart, its This week line and its two links")
    check('class="info' not in panel and "Hover a bar" not in panel and "Green emailed within" not in panel
          and "Weeks start on Monday" not in panel and "Is the flow picking up" not in panel,
          "without their captions, the footnote or the question")
    check('aria-label="Appearance"' not in body,
          "no light or dark switch on the page: Radar stays dark only")
    # The header's own checks (its menu, Settings, its light, its shortcuts
    # button) are in test/run_header_test.py; this is Home's one check of it.
    check('aria-current="page">Home</a>' in body, "the top bar marks Home current")
    check("Keyboard shortcuts" not in body and "<kbd" not in body
          and "aria-keyshortcuts" not in body and "<dialog" not in body,
          "no shortcuts dialog and no key hints on Home")
    # Needs you today: every item in full, no carousel.
    arts = re.findall(r'<article class="ny-item[^"]*" id="([\w-]+)" data-item="([\w-]+)"', body)
    check(len(arts) == 7 and all(a == b for a, b in arts), "every item is on the page in full, each with its own id")
    for gone in ('id="ny-pane"', 'id="ny-store"', "data-step", ">Previous<", "row-3", "Done takes this off Home",
                 "Scores are from the advert card", "Undo last puts it back"):
        check(gone not in body, f"no carousel and no line on how to use it: {gone!r} is gone")
    check("data-next" not in body and ">Next</a>" not in body and "ny-next" not in hp.HOME_CSS,
          "no Next button: every item is on screen")
    check(body.count("data-done=") == 7 and 'id="undo-last"' in body and '>To do</a>' in body
          and '?show=done"' in body, "Done on each, Undo last, and To do and Done stay")
    # Open actions: its title outside the cards, with its chip and count.
    head = re.search(r'<header class="sec-head ny-head"><span class="gchip g-neutral" aria-hidden="true">'
                     r'.*?</svg></span><h2 class="sec-title ny-title" id="ny-title">Open actions'
                     r'<span class="sec-count"><span class="visually-hidden">, </span>(\d+)'
                     r'<span class="visually-hidden"> items</span></span></h2><div class="ny-tools">', body)
    check(head is not None and head.group(1) == "7" and "Needs you today" not in body
          and 'class="group-title"' not in body.split('id="ny"', 1)[1],
          "Open actions, with its neutral chip and its count of items to do, outside the cards, "
          "and no subheadings in the list")
    check(re.findall(r'<div class="ny-group" id="group-(\w+)">', body) == ["urgent", "top", "question", "apps"],
          "each group's items keep its address for the tiles, in today's order")
    check(".ny-item{" in hp.HOME_CSS and "border-radius:14px" in hp.HOME_CSS.split(".ny-item{", 1)[1].split("}", 1)[0]
          and ".ny-acts .btn{height:44px;padding:0 16px;border-radius:8px}" in hp.HOME_CSS
          and "background:var(--surface-sunken);border-color:#39424a" in hp.HOME_CSS,
          "each item is a card; Done is 44px, quiet, radius 8px")
    check("inset 3px 0 0" not in hp.HOME_CSS and "border-left:3px" not in hp.HOME_CSS,
          "no coloured left-edge bar, the item an address brings into view included")
    # The urgent story: its own card.
    sig = re.search(r'<article class="ny-item ny-story glow-wide g-story" id="(signal-\d+)".*?</article>', body, re.S)
    card = sig.group(0) if sig else ""
    line = re.search(r'<p class="story-line"><span class="gchip g-story" aria-hidden="true">.*?</svg></span>'
                     r'<span class="story-kind">Urgent story</span><span class="fact-rule" aria-hidden="true">'
                     r'</span><span class="visually-hidden">, </span><span class="story-src">'
                     r'<span class="co">Hartwell Infusion Ltd</span>, via a job advert</span></p>', card)
    check(card and line and '<h3 class="story-h"' in card and 'class="story-sum"' in card
          and ">Read the advert" in card and ">Company record<" in card and "/company/" not in card
          and 'href="/watchlist/' in card and "data-done=" in card,
          "the urgent story is an amber card: its chip, Urgent story, a drawn rule and the source, "
          "then the headline, the summary, the announcement and the company record")
    check(card and "Relevance" not in card and "Found" not in card and "Details" not in card,
          "without relevance, the found date or the Details table")
    move = re.search(r'<p class="story-sum">.*?</p><section class="ny-move"[^>]*><h4 class="sec-h"[^>]*>'
                     r'Next move</h4><p class="ny-para">(.*?)</p></section><div class="action-bar ny-acts">'
                     r'<a class="link" href="[^"]+" target="_blank"', card)
    check(move is not None and move.group(1) == "Send the compliance-cost article, or the gap assessment one-pager."
          and ".ny-move{max-width:40rem;padding:14px 16px;background:#101417;border:1px solid #1f262b;border-radius:10px}" in hp.HOME_CSS,
          "its Next move, the playbook step, sits under the summary in an inset panel, then its links and Done")
    check(card and "<dt>" not in card and "On file for" not in card and "Builds" not in card,
          "without the fixed On file fields (builds, software, stage, where, people)")
    check("font-size:21px;line-height:1.3;font-weight:600" in hp.HOME_CSS
          and "var(--font-serif)" not in hp.HOME_CSS.split(".ny-story .story-h{", 1)[1].split("}", 1)[0],
          "its headline in the site's sans, 21px, weight 600")
    # Jobs: its title over the card with See all, then three rows, each its
    # tier's pill and total, no "roles" and no "new".
    section = body.split('<section class="ht-side ht-jobs-tile" aria-labelledby="tile-jobs">', 1)
    jobs = section[1].split("</section>", 1)[0] if len(section) == 2 else ""
    rows = re.findall(r'<li><a class="hj-row" id="funnel-(\w+)" href="([^"]+)"><span class="tier-pill t-(\w+)">'
                      r'([^<]+)</span><span class="visually-hidden">, </span><span class="hj-fig">(\d+)</span></a></li>', jobs)
    check([r[0] for r in rows] == ["top", "question", "reading"] and all(r[0] == r[2] for r in rows)
          and [r[1] for r in rows] == ["/jobs#group-top", "/jobs#group-question", "/jobs?reading=1#group-reading"]
          and re.search(r'<header class="sec-head"><span class="gchip g-jobs".*?<h2 class="sec-title" id="tile-jobs">'
                        r'Jobs</h2><a class="link sec-link" href="/jobs">See all', jobs)
          and "Open roles" not in body and " roles" not in re.sub(r"<[^>]+>", " ", jobs)
          and " new" not in jobs and 'class="ht-note"' not in jobs,
          f"Jobs: its chip and title over the card, See all to Jobs, then Top prospects, One question away "
          f"and Reading, each its pill and total linking to its tier, no roles word and no new ({rows})")
    check(dict((r[0], int(r[4])) for r in rows) == jp.tier_totals(model_of(db)["jmodel"]),
          "the Jobs card's totals are jobs_page.tier_totals, the Jobs page's bands")
    apps = body.split('<section class="ht-side ht-apps-tile" aria-labelledby="tile-applications">', 1)
    check(len(apps) == 2 and apps[1].startswith('<header class="sec-head"><span class="gchip g-apps"')
          and '<h2 class="sec-title" id="tile-applications">Applications</h2></header>'
              '<div class="side-card glow-wide g-apps">' in apps[1]
          and body.index('id="ny"') < body.index('id="tile-jobs"') < body.index('id="tile-applications"'),
          "Applications: its teal chip and title over its card, under Jobs; Open actions comes first")
    check(".hf-cols{grid-template-columns:minmax(0,1fr) min(calc((100% - 20px) / 3),560px);gap:20px}" in hp.HOME_CSS
          and ".hf-side{display:flex;flex-direction:column;gap:24px}" in hp.HOME_CSS
          and "align-items:start" in hp.HOME_CSS.split(".hf-cols{", 1)[1].split("}", 1)[0],
          "two columns, 2fr and 1fr with the right one at 560px at most, 20px apart and aligned "
          "at the top, Applications 24px under Jobs")
    # Links on Home: the text colour, underlined in grey, never teal.
    link = re.search(r"\.link\{([^}]*)\}", hp.HOME_CSS).group(1)
    hover = re.search(r"\.link:hover\{([^}]*)\}", hp.HOME_CSS).group(1)
    check("color:var(--text-1)" in link and "text-decoration-color:var(--link-line)" in link
          and "text-underline-offset:3px" in link and "--link-line:#5b6770" in hp.HOME_CSS
          and "text-decoration-color:var(--text-1)" in hover and "accent" not in link + hover
          and "btn-primary" not in body,
          "links on Home are the text colour, underlined in #5b6770, the text colour on hover; no teal "
          "link or button")
    check('class="ht-q"' not in body and "Where do my applications stand" not in body
          and "Where do the open roles narrow" not in body, "no question under a widget's title")
    check("\u00b7" not in html,
          "no middle dot anywhere on Home, the Jobs rows, title, script and styles included")
    # Every control is a link or a button, so the keyboard reaches it.
    clickable = re.findall(r"<(div|span|li|p)[^>]*\sonclick=", body)
    check(not clickable, "every control is a link or a button")
    check(" color " not in re.sub(r"<[^>]+>", " ", body).lower()
          and "favorite" not in body.lower(), "UK English in the words")
    item = f"role-{ids['top_new'][1]}"
    with_item = page_of(db, item=item)
    check(f'&quot;target&quot;: &quot;{item}&quot;' in with_item and f'id="{item}" data-item="{item}"' in with_item,
          "an item's address brings that item into view")
    gone = page_of(db, item="role-999999")
    check("That item is no longer on Home" in gone, "an address for an item not on Home says so")
    # The fragment and the Done view, through the server's own functions.
    with pointed_at(db):
        frag = serve_dashboard.render_home_main({"item": [item]})
        done_page = serve_dashboard.render("home", {"show": ["done"]}).decode()
        full = serve_dashboard.render("home", {"item": [item]}).decode()
        junk = serve_dashboard.render("home", {"item": ["<script>"]}).decode()
    check(frag["item"] == item and 'id="ny"' in frag["html"] and frag["count"] == 7,
          "the main fragment comes back with its item and count")
    check("Done on Home" in done_page and "<p>Nothing done.</p>" in done_page,
          "the Done view, empty")
    check(full.count('id="welcome-h"') == 1, "the server renders the page")
    check("<script>" not in junk.split("</head>", 1)[1].split("<script>", 1)[0],
          "an address with markup in it selects nothing and prints nothing")
    for width_rule in ("@container app (min-width:1024px)", "@container htw (min-width:1100px)",
                       "@container app (max-width:599.98px)", "@media (max-width:767.98px)",
                       "@container nyrole (max-width:479.98px)"):
        check(width_rule in hp.HOME_CSS, f"the layout has its rule {width_rule}")
    check("@media (pointer:coarse)" in hp.HOME_CSS and "min-height:44px" in hp.HOME_CSS,
          "44px targets on a phone and on touch")


# ================================================================ 5b. role items are the Jobs row

def role_row_checks(tmp: Path) -> None:
    """A role on Home is the Jobs row, drawn by jobs_page.row_html, with Done
    and nothing else; a long gate reason never widens the page."""
    db = tmp / "rows.sqlite"
    ids = seed_home(db)
    long_reason = ("Strong IEC 62304 and ISO 13485 background, product ownership in IVD, and "
                   "regulatory submissions across the EU and the US over twelve years")
    conn = rc.get_db(db)
    conn.execute("UPDATE opportunities SET gate_cv_note = ? WHERE id = ?", (long_reason, ids["top_new"][0]))
    conn.execute("UPDATE opportunities SET gate_location = 0, gate_rate = 1, question_text = ?,"
                 " gate_location_note = ? WHERE id = ?",
                 ("Would they go remote.", "Location fails, Edinburgh, five days on site",
                  ids["question_new"][0]))
    conn.commit()
    conn.close()
    html = page_of(db)
    body = html.split("<body>", 1)[1]
    roles = [i for i in model_of(db)["todo"] if i["kind"] == "role"]
    rows = role_rows(body)
    check(len(roles) == 5 and sorted(rows) == sorted(i["role"]["id"] for i in roles),
          f"each role in Needs you today is one row ({len(rows)} rows, {len(roles)} roles)")
    with pointed_at(db):
        jobs = serve_dashboard.render_jobs(None, {})[1].decode("utf-8")
    on_jobs = role_rows(jobs)
    same = [oid for oid, row in rows.items() if on_jobs.get(oid) == row]
    current = [oid for oid in rows if 'is-current' in on_jobs.get(oid, "")]
    check(set(same) | set(current) == set(rows) and len(same) >= len(rows) - 1,
          f"and each row is the row Jobs draws for that role, character for character "
          f"(the same {len(same)}, the one selected on Jobs apart: {current})")
    for i in roles:
        oid = i["role"]["id"]
        art = re.search(rf'<article class="ny-item ny-role glow-short g-jobs" id="{i["id"]}".*?</article>', body, re.S)
        art = art.group(0) if art else ""
        rest = art.replace(rows.get(oid, "\0"), "")
        pill = f'<span class="tier-pill t-{i["group"]}">{jp.GROUP_WORDS[i["group"]]}</span>'
        check(art and f'<a class="r-link" id="rt-{oid}" href="/jobs/{oid}"' in art
              and f'data-done="{esc_attr(i["key"])}"' in rest
              and re.search(r'^<article[^>]*><span class="gchip g-jobs" aria-hidden="true">', art)
              and f'<div class="action-bar ny-acts">{pill}<button' in rest
              and re.sub(r"<[^>]+>", "", rest).strip() == jp.GROUP_WORDS[i["group"]] + "Done",
              f"role {oid}: a purple card, its chip, the row linking to the role on Jobs, then its "
              f"tier's pill and Done, and nothing else")
    metas = [re.search(r'<span class="r-meta"[^>]*>(.*?)</span></span>', row).group(1) for row in rows.values()]
    check(metas and all("|" not in re.sub(r"<[^>]+>", "", m) and '<span class="visually-hidden">, </span>' in m
                        for m in metas),
          "between a row's facts, a drawn rule and a comma for a screen reader, never a typed bar")
    for gone in ("Why it is here", "The four gates", "Scored on the",
                 "Would they go remote", "Would they move on the rate"):
        check(gone not in body, f"{gone!r} appears nowhere on Home")
    check("First seen" not in body,
          "no first-seen date on a role row, as on Jobs")
    q = rows.get(ids["question_new"][0], "")
    check(q and 'class="r-lead"' not in q and "Edinburgh, five days on site." not in q
          and 'class="mk ' not in q,
          "a One question away row is its title and facts, as on Jobs: no reason and no marks")
    check(long_reason not in body,
          "a long gate reason appears nowhere on Home")
    css = hp.HOME_CSS
    status = re.search(r"\.status\{[^}]*\}", css).group(0)
    check("white-space:normal" in status and "nowrap" not in status and "overflow-wrap:anywhere" in status,
          "the .status rule wraps a long reason instead of pushing the page wide")
    check(jp.ROW_CSS in jp.JOBS_CSS and jp.ROW_NARROW_CSS in jp.JOBS_CSS
          and jp.ROW_CSS in html and jp.ROW_NARROW_CSS in html
          and "container:queue / inline-size" in css,
          "Home draws the row with Jobs' own row rules, shared, not copied, inside a queue container")
    check("jp.row_html(" in Path(hp.__file__).read_text(encoding="utf-8"),
          "the row comes from jobs_page.row_html")


# ================================================================ 6. the states

def state_checks(tmp: Path) -> None:
    db = tmp / "quiet.sqlite"
    seed_home(db, "quiet")
    m = model_of(db)
    html = page_of(db)
    check(not m["todo"], "a quiet day: nothing needs you today")
    check('class="ht-quiet"' not in html and "The signal watcher last ran at" not in html
          and "sources answering" not in html and "answered at" not in html,
          "and no sentence under the greeting about what Radar read")
    zero = re.findall(r'class="ht-figlink glow-tile g-\w+ is-zero" id="figure-(\w+)"', html)
    counts = hp.figure_counts(m)
    check(zero == [k for k in ("urgent", "top", "question", "threads", "apps") if not counts[k]]
          and {"urgent", "threads", "apps"} <= set(zero),
          f"the five tiles stay, each at 0 softened: the stories, the follow-ups and a tier with no open role ({zero})")
    empty = html.split('class="ny-body"', 1)[1].split("</section>", 1)[0] if 'class="ny-body"' in html else ""
    check("ny-is-empty" in html and re.sub(r"<[^>]+>", "", empty.split(">", 1)[1]).strip() == "Nothing today.",
          "the empty list reads Nothing today. and no more")
    gone = page_of(db, item="radar-signals")
    check("ny-is-empty" in gone and "That item is no longer on Home" in gone
          and gone.index("That item is no longer on Home") < gone.index("Nothing today."),
          "an address for an item that has gone says so on an empty Home too, above the empty state")
    check("That item is no longer on Home" not in html, "and an empty Home without an address does not")
    db = tmp / "first.sqlite"
    seed_home(db, "first")
    m = model_of(db)
    html = page_of(db)
    check(m["week"]["first_week"] and "this week so far" in html and 'class="ht-plot"' not in html
          and "Trends start after" not in html,
          "the first week: this week's two figures, no empty axes and no footnote")
    db = tmp / "noapps.sqlite"
    seed_home(db, "noapps")
    m = model_of(db)
    html = page_of(db)
    tile = html.split('id="tile-applications"', 1)[1].split("</section>", 1)[0]
    check('<div class="empty"><p>No applications yet.</p></div>' in tile and "<a " not in tile
          and m["counts"]["apps"] == 0 and 'id="group-apps"' not in html,
          "no applications yet: the tile reads No applications yet. and no more, no Applications group")
    check(re.search(r'class="ht-figlink glow-tile g-apps is-zero" id="figure-apps"', html),
          "its tile stays at 0, softened")
    db = tmp / "fault.sqlite"
    seed_home(db, "fault")
    m = model_of(db)
    html = page_of(db)
    words = {l["name"]: l["word"] for l in m["lights"]}
    check(words == {"inbox": "Quiet", "signals": "Stale", "check": "Late", "digest": "Not sending",
                    "push": "Stale"},
          f"a Radar fault: the strip names each job's state in a word ({words})")
    radar = [i for i in m["todo"] if i["group"] == "radar"]
    check([i["id"] for i in radar] == ["radar-signals", "radar-check", "radar-push"],
          "the faults head the list; the quiet inbox and the digest in dry run are not there")
    check("News watcher is stale, 22 hours since its last run" in html
          and "Stale after 6 hours" in html
          and '<article class="ny-item ny-radar" id="radar-signals"' in html,
          "the stale watcher is named with its age and its limit")
    check("Stale, 22 hours" in html and "\u00b7" not in html,
          "its tag says Stale, 22 hours, with a comma")
    check("data-check>Check now</button>" in html and "Signals as of" not in html and "As of" not in html,
          "the watcher's item offers Check now, and no As of time is on the page")
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
        check(s3 == 200 and f'id="{item["id"]}" data-item' not in frag["html"]
              and frag["item"] is not None, "the fragment no longer lists it")
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
    check("keydown" not in js and "radar-home-keys" not in js and "showModal" not in js,
          "the script listens for no keys and opens no shortcuts dialog")
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
    check(out.returncode == 0 and len(lines) >= 12,
          f"the Home script runs Done, Undo, the toast and the health panel in node "
          f"({out.stderr.strip()[:200]})")


# ================================================================ 9. the box checks

def box_doc_checks(tmp: Path) -> None:
    short = (REPO_ROOT / "docs" / "box-checks-source-count.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", short, re.S)
    check(len(blocks) == 3 and all(b.strip() for b in blocks),
          f"the source count's box checks have their three blocks ({len(blocks)}) and none is empty")
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


def phone_checks(tmp: Path) -> None:
    """Home on a phone (owner, 17 Nov): the lights fold behind one chip, the
    tiles are shorter, and the leftover wording and the company icon."""
    lit = [{"word": w} for w in ("On time", "Not sending", "On time", "Not sending", "On time")]
    check(hp.lights_summary(lit) == ("3 on time, 2 not sending", "is-neu")
          and hp.lights_summary([{"word": "On time"}] * 5) == ("5 on time", "is-pos")
          and hp.lights_summary([{"word": "Late"}, {"word": "On time"}])[0] == "1 on time, 1 late",
          "the lights' chip counts their words, green only when every light is on time")
    db = tmp / "phone.sqlite"
    seed_home(db)
    html = page_of(db)
    chip_at = html.find('<button type="button" class="ht-lights-sum" id="lights-btn" '
                        'aria-expanded="false" aria-controls="lights-list">')
    check(chip_at > 0 and chip_at < html.find('<ul class="ht-strip-list" id="lights-list"')
          < html.find('id="health-btn"')
          and html.count('<li class="ht-strip-job"') == 5
          and "<span class=\"visually-hidden\">Radar's five jobs: </span>" in html.replace("&#x27;", "'"),
          "the chip comes first, closed, says what it counts in words, and controls the five "
          "lights, which stay in the page; Radar's health follows")
    css = hp.HOME_CSS
    phone = css[css.index("@media (max-width:699.98px){"):]
    phone = phone[:phone.index("\n}\n") + 3]
    check(".ht-lights-sum{display:none}" in css
          and ".ht-strip-list{display:none;order:3;flex-basis:100%}" in phone
          and '.ht-lights-sum[aria-expanded="true"] ~ .ht-strip-list{display:flex}' in phone
          and "min-height:44px" in phone[phone.index(".ht-lights-sum{"):],
          "under 700px the five chips fold behind the summary chip, 44px tall, and open "
          "beneath it")
    check(".ht-figlink{display:grid;grid-template-columns:auto minmax(0,1fr);" in phone
          and "padding:12px}" in phone and ".ht-figword{grid-column:1 / -1;" in phone
          and ".ht-figlink{display:flex;flex-direction:column;align-items:flex-start;min-width:0;"
              "min-height:44px;padding:16px;" in css,
          "under 700px a tile has its chip and number on one line and its word under them, "
          "12px in; from 700px it is as it was")
    check(".ht-fignum{font-size:40px;line-height:48px;font-weight:600;" in css
          and ".ht-figword{margin-top:2px;font-size:16px;line-height:24px;font-weight:400;"
              "color:var(--text-2)}" in css
          and "grid-auto-rows:1fr;" in css
          and ".ht-fignum{font-size:28px;line-height:32px}" in phone
          and "font-size:14px;line-height:18px}" in phone[phone.index(".ht-figword{"):]
          and ".ht-figs{grid-auto-rows:auto;" in phone,
          "from 700px a tile's number is 40px and its word 16px, every tile the same height; "
          "on a phone 28px and 14px")
    check(".r-new{display:inline-flex;align-items:center;flex:none;height:20px;padding:0 8px;"
          "border-radius:10px;background:rgba(167,156,240,.18);color:#c9c2f8;" in html
          and "is-current" not in "".join(re.findall(r'<div class="row [^"]*"', html)),
          "Open actions' role rows carry the same purple New pill as Jobs, and no wash")
    # One count (owner, 17 Nov): Home's company follow-ups tile and the
    # Watchlist's follow-ups due pill read the same function, on the same data.
    hm = model_of(db)
    conn = reader(db)
    try:
        wm = watchlist.build_model(conn, CFG, hm["now"])
        direct = watchlist.follow_ups_due_count(conn, hm["now"])
    finally:
        conn.close()
    check(hp.figure_counts(hm)["threads"] == wm["counts"]["due"] == direct,
          f"Home's follow-ups tile and the Watchlist's pill give the same count "
          f"({hp.figure_counts(hm)['threads']}, {wm['counts']['due']}, {direct})")
    check("@container ny (min-width:1100px){" in hp.HOME_CSS
          and ".ny-story > .ny-move{grid-column:2;grid-row:1 / span 2;max-width:none}" in hp.HOME_CSS,
          "a story card wider than 1100px has the Next move and the actions on its right")
    check('<h5 class="sec-h">Thread</h5>' not in html,
          "Home's follow-up cards say follow-up, not thread")
    check("function setLights(open)" in hp.HOME_SCRIPT and "'#lights-btn'" in hp.HOME_SCRIPT
          and "setLights(lit);" in hp.HOME_SCRIPT,
          "tapping the chip shows or hides the lights, and a redraw keeps them as they were")
    check(hp.ICONS["threads"].startswith('<path d="M4.5 16.5V5.5')
          and hp.ICONS["threads"] != hp.ICONS["story"],
          "company follow-ups have a building, not the story's page")
    check(not any("signal" in v.lower() for v in hp.DOES.values())
          and "Stories at 40 or more" in html and "Signals at 40 or more" not in html,
          "Radar's own items and the Week by week chart say story, not signal")
    check("A next step you set" not in html and "Follow up, shows here" not in html,
          "the Applications card says only that nothing is waiting on you")
    import watchlist_page
    check(watchlist_page.run_words({"watcher_ran": None}) == "The news watcher has not run yet.",
          "the Watchlist names the news watcher")


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
                        role_row_checks, phone_checks,
                        state_checks, thread_checks, route_checks, script_checks, harness_checks,
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
    print("PASS. Every Home check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
