#!/usr/bin/env python
"""Acceptance checks for the CV page and the tailored CV (Brief 4).

No model calls and no network. RADAR_MOCK is forced on, the master CV is a
string held here, every database is a throwaway under a temporary directory,
and the profile directory the master editor writes to is a temporary one, so
a mistake here cannot spend a token, touch the owner's CV or touch a real row.
The owner's own preferences file is never read.

What it checks:

- the table of tailored copies and the history's CV hash arrive on a database
  in the old shape, once however many processes open it, nullable, with every
  row untouched,
- the layout: render and parse are each other's inverse, a text the editor
  wrote reads back to the byte, any other text is read the way a person would
  read it, every string is cleaned the one way, the caps hold, and the saved
  copy's name is the brief's and safe for a file name,
- the line numbers agree: the page's, the scorer's and the working's, for
  every CV in a corpus, and the page's script numbers them the same as the
  server under node,
- the scorer is shown the copy in place of the master and nothing else changes,
- the copy: made from the master with no model call the first time the page
  opens, one per role, shared by a repost chain, saved under its name, put back
  to the master on request, never written for an applied or closed role,
- Rescore role: one call, stored on the copy and in the history marked
  tailored, never on the role, once per pair of description and copy, from the
  copy, the history or the master itself, a double click and a storm pay once,
  failures remembered for Brief 3's windows, paid-for work parked when the
  database is busy, and a role applied for or closed mid-call keeps what it had,
- Re-score stale items leaves full-description scores alone,
- the Word file: its text and order are the saved copy's as the stdlib reader
  reads them, and its XML holds no table, text box, image, field, header or
  footer and only one column, with real list paragraphs and one plain font,
- the routes, over a real server under the shipped guard,
- Applied records the saved copy's name, and nothing else changes about it,
- the page: the split view, the tailored score beside the master's, the asks
  marked for highlighting and marked stale, the LinkedIn link, the read-only
  view of an applied role, escaping, and that opening it writes nothing,
- the editor's script under node: the one post it makes on its own, one post
  per click and none while one is out, numbering, editing, staleness and the
  highlighting both ways,
- the box queries handed over for the deploy run here on seeded data.

Exits non zero on any failure with readable FAIL lines.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import http.client
import io
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

# Forced on before anything else loads. Nothing in this file may call a model.
os.environ["RADAR_MOCK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "test"):
    if str(REPO_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / sub))

import build_dashboard  # noqa: E402
import check_jobs  # noqa: E402
import cv_doc  # noqa: E402
import cv_page  # noqa: E402
import cv_store  # noqa: E402
import mocks  # noqa: E402
import prepare_application as prep  # noqa: E402
import radar_common  # noqa: E402
import rescore  # noqa: E402
import score_item  # noqa: E402
import serve_dashboard  # noqa: E402
import tailored_cv  # noqa: E402

# Hermetic. The live preferences file holds the owner's own floors.
TEMPLATE = REPO_ROOT / "config" / "preferences.template.md"
_real_load_config = radar_common.load_config
radar_common.load_config = lambda: {**_real_load_config(),
                                    "prefs_file": str(TEMPLATE)}
CFG = radar_common.load_config()
SPEC = radar_common.SCORE_BASIS_SPEC
HEADER = build_dashboard.WRITE_HEADER
PROXY = "medtech-radar.keogh.cloud"

failures: list[str] = []
skipped: list[str] = []
_ids = iter(range(4_700_000_001, 4_800_000_000))


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def note(text: str) -> None:
    print("NOTE  " + text)
    skipped.append(text)


@contextlib.contextmanager
def captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


# ------------------------------------------------------------ the master CV

MASTER_DOC = {
    "name": "Luke Keogh",
    "headline": ["Software Director, regulated medical devices and IVD",
                 "Chichester, West Sussex"],
    "sections": [
        {"title": "Summary", "entries": [], "lines": [
            {"b": False, "t": "Software leader in regulated industries since "
                              "2011, IEC 62304 and ISO 13485 throughout."},
            {"b": False, "t": "Took a Class B IVD through FDA clearance."}]},
        {"title": "Experience", "lines": [], "entries": [
            {"heading": "Software Director, Axithra, 2023 to now", "lines": [
                {"b": True, "t": "Lead software for a Class C IVD under IEC 62304."},
                {"b": True, "t": "Built the software team from nothing."},
                {"b": True, "t": "Own the software risk file under ISO 14971."}]},
            {"heading": "Software Director, miDiagnostics, 2019 to 2023", "lines": [
                {"b": True, "t": "Oversaw FDA clearance for a Class B IVD."}]}]},
        {"title": "Skills", "entries": [], "lines": [
            {"b": True, "t": "IEC 62304, ISO 13485, ISO 14971"},
            {"b": True, "t": "SpiraPlan and regulated ALM configuration"}]},
        {"title": "Education and certifications", "entries": [], "lines": [
            {"b": True, "t": "BEng Electronic Engineering"}]},
    ],
}
MASTER_TEXT = cv_doc.render(MASTER_DOC)
# A copy tailored by the smallest edit, one that moves no line and no ask.
# Rescore role does not score the master unchanged for a role whose own score
# is not on its description (UNCHANGED_COPY), so a run that wants a first call
# on a copy starts from this.
COPY_TEXT = MASTER_TEXT.replace("Chichester, West Sussex", "Chichester, West Sussex, UK", 1)
MASTER = [MASTER_TEXT]
MASTER_LABEL = ["cv-20261110-090000.md"]
score_item.load_cv_text = lambda config: (MASTER[0], "config/profile/" + MASTER_LABEL[0])
score_item.get_cv_version = lambda config: MASTER_LABEL[0]

DESCRIPTION = (
    "About the role\n"
    "We are hiring a Head of Software for a medical device business.\n\n"
    "Requirements\n"
    "- IEC 62304 software lifecycle in a Class C IVD\n"
    "- Leadership of an FDA submission for a diagnostic\n"
    "- ISO 14971 risk management for software\n"
    "- Kubernetes and cloud deployment of clinical systems\n"
    "- Experience building a software team from scratch\n\n"
    "Pay\nCompetitive salary and benefits.")

# ----------------------------------------------------------- the stand-ins

CALLS: list[dict] = []
MODEL = {"hook": None}
CLOCK = [10_000.0]
_real_claude_call = radar_common.claude_call
_STOP = {"with", "from", "that", "this", "have", "will", "your", "about", "their",
         "years", "year", "experience", "strong", "good", "knowledge", "ability"}


def system_cv(system_blocks) -> dict:
    """The numbered CV in a system block, {n: text}, the way the model sees it."""
    text = system_blocks[0]["text"]
    body = text.split("Every line is numbered in square brackets.", 1)[1]
    body = body.split("\n\n## PREFERENCES", 1)[0]
    out = {}
    for raw in body.splitlines():
        found = re.match(r"^\[(\d+)\] ?(.*)$", raw)
        if found:
            out[int(found.group(1))] = found.group(2)
    return out


def judge(description: str, cv: dict) -> list[dict]:
    """Each bulleted line of the description as an ask, judged against the CV
    the model was shown, by the words they share."""
    asks = []
    for raw in description.splitlines():
        found = re.match(r"^\s*-\s+(.*\S)\s*$", raw)
        if not found:
            continue
        ask = found.group(1)
        words = {w for w in re.findall(r"[a-z0-9][a-z0-9.\-]{3,}", ask.lower())
                 if w not in _STOP}
        hits = sorted(((sum(1 for w in words if w in line.lower()), -n, n)
                       for n, line in cv.items() if line.strip()), reverse=True)
        hits = [h for h in hits if h[0]]
        if hits and hits[0][0] >= 2:
            asks.append({"ask": ask, "status": "met",
                         "cv_lines": [h[2] for h in hits[:2]]})
        elif hits:
            asks.append({"ask": ask, "status": "partly", "cv_lines": [hits[0][2]]})
        else:
            asks.append({"ask": ask, "status": "missing", "cv_lines": []})
    return asks


def usage(out_tokens=300, stop="end_turn"):
    return {"input_tokens": 1500, "output_tokens": out_tokens,
            "cache_read_tokens": 0, "mode": "mock", "model": "mock",
            "stop_reason": stop}


def spec_reply(call) -> tuple[str, dict]:
    """The mock scorer's reply for a full description, with its asks judged
    against the CV in the system block, as a real model would."""
    opp = json.loads(call["user"].split("\n\n", 1)[1])
    reply = json.loads(mocks.mock_scorer(call["user"]))
    reply["asks"] = judge(opp.get("description") or "", system_cv(call["system"]))
    met = sum(1 for a in reply["asks"] if a["status"] == "met")
    reply["cv_match"] = min(97, 50 + 10 * met)
    return json.dumps(reply), usage()


def counting_claude_call(model, system_blocks, user_content, max_tokens=1024,
                         mock_fn=None):
    call = {"model": model, "system": system_blocks, "user": user_content,
            "max_tokens": max_tokens}
    CALLS.append(call)
    if MODEL["hook"] is not None:
        got = MODEL["hook"](call)
        if got is not None:
            return got
    if '"basis": "spec"' in user_content:
        return spec_reply(call)
    return _real_claude_call(model, system_blocks, user_content,
                             max_tokens=max_tokens, mock_fn=mock_fn)


def no_fetch(url):
    raise AssertionError(f"this runner never asks for a page, but {url} was asked for")


radar_common.claude_call = counting_claude_call
check_jobs.fetch_job_page = no_fetch
prep._mono = lambda: CLOCK[0]
prep._sleep = lambda seconds: None


def reset() -> None:
    del CALLS[:]
    MODEL["hook"] = None
    CLOCK[0] = 10_000.0
    MASTER[0] = MASTER_TEXT
    MASTER_LABEL[0] = "cv-20261110-090000.md"
    prep.GUARD.reset()


# ------------------------------------------------------------ the fixtures

class Env:
    """One throwaway database and the helpers to seed and read it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.db = tmp / "cv.sqlite"
        radar_common.get_db(self.db).close()

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def role(self, title="Head of Software", company="Veltrix Diagnostics",
             description=DESCRIPTION, jid=None, **cols) -> int:
        jid = jid or next(_ids)
        url = cols.pop("source_url", f"https://www.linkedin.com/comm/jobs/view/{jid}")
        row = {"url_hash": f"cv-{jid}", "first_seen": "2026-11-10T09:00:00Z",
               "last_seen": "2026-11-10T09:00:00Z", "status": "new",
               "thread_type": "inbound", "title": title, "company": company,
               "location": "Remote, UK", "salary_rate": "",
               "source": "linkedin-alert", "source_url": url, "tier": "top",
               "cv_match": 80, "gate_sector": 1, "gate_cv": 1,
               "gate_location": 1, "gate_rate": 1, "failed_gates": "[]",
               "one_line_why": "Judged against the four gates.",
               "score_basis": "card", "prompt_version": "pre-record",
               "score_model": "unknown",
               "role_key": radar_common.role_key(title, company),
               "description_text": description,
               "description_fetched_at": "2026-11-12T09:00:00Z" if description else None,
               **cols}
        c = self.conn()
        c.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES "
                  f"({', '.join('?' for _ in row)})", tuple(row.values()))
        c.commit()
        oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.close()
        return oid

    def row(self, oid) -> dict:
        c = self.conn()
        try:
            return dict(c.execute("SELECT * FROM opportunities WHERE id = ?",
                                  (oid,)).fetchone())
        finally:
            c.close()

    def copy(self, oid) -> dict | None:
        c = self.conn()
        try:
            return tailored_cv.for_row(c, dict(c.execute(
                "SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone()))
        finally:
            c.close()

    def copies(self) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute("SELECT * FROM tailored_cvs ORDER BY id")]
        finally:
            c.close()

    def history(self, oid=None) -> list[dict]:
        c = self.conn()
        try:
            if oid is None:
                return [dict(r) for r in c.execute("SELECT * FROM score_history ORDER BY id")]
            return [dict(r) for r in c.execute(
                "SELECT * FROM score_history WHERE opportunity_id = ? ORDER BY id", (oid,))]
        finally:
            c.close()

    def runs(self) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM runs WHERE workflow = 'prepare' ORDER BY id")]
        finally:
            c.close()

    def dump(self) -> str:
        c = self.conn()
        try:
            return "\n".join(c.iterdump())
        finally:
            c.close()

    def sql(self, statement, params=()):
        c = self.conn()
        try:
            c.execute(statement, params)
            c.commit()
        finally:
            c.close()

    def apply(self, oid, cv=None) -> None:
        self.sql("UPDATE opportunities SET applied_at = ?, applied_cv = ? WHERE id = ?",
                 ("2026-11-12T10:00:00Z", cv, oid))

    def open(self, oid):
        return prep.open_copy(oid, self.db, CFG)

    def save(self, oid, text):
        return prep.save_copy(oid, text, self.db, CFG)

    def reset_copy(self, oid):
        return prep.reset_copy(oid, self.db, CFG)

    def rescore(self, oid, text):
        return prep.rescore_copy(oid, text, self.db, CFG)

    def prepare(self, oid):
        return prep.prepare(oid, self.db, CFG)


@contextlib.contextmanager
def fresh():
    reset()
    tmp = Path(tempfile.mkdtemp(prefix="radar-cv-"))
    try:
        yield Env(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def edited(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def opp_row_of(env, oid) -> dict:
    """The role's own score and stamps, everything the Jobs page reads."""
    row = env.row(oid)
    return {k: v for k, v in row.items() if k not in ("last_checked_at",)}


# ---------------------------------------------------------------- migration

def roles_of(env) -> list[str]:
    """Every role, whole statement by statement, as the dump writes them."""
    conn = env.conn()
    try:
        return [s for s in conn.iterdump() if s.startswith('INSERT INTO "opportunities"')]
    finally:
        conn.close()


def migration_checks() -> None:
    with fresh() as env:
        conn = env.conn()
        conn.execute("DROP TABLE tailored_cvs")
        conn.execute("ALTER TABLE score_history DROP COLUMN cv_hash")
        conn.commit()
        conn.close()
        oid = env.role()
        env.sql("INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
                " cv_match, tier) VALUES (?, '2026-11-01T00:00:00Z', 'scored', 'inbox', 80, 'top')",
                (oid,))
        before_roles = roles_of(env)
        conn = env.conn()
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(score_history)")]
        conn.close()
        check("tailored_cvs" not in names and "cv_hash" not in cols,
              "the copy of a database in the old shape has no copies table and no "
              "history CV hash")
        opened = []

        def open_it():
            radar_common.get_db(env.db).close()
            opened.append(1)
        threads = [threading.Thread(target=open_it) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        conn = env.conn()
        info = {r["name"]: r for r in conn.execute("PRAGMA table_info(tailored_cvs)")}
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(score_history)")]
        rows_now = [dict(r) for r in conn.execute(
            "SELECT id, opportunity_id, event, cv_match, cv_hash FROM score_history")]
        conn.close()
        check(len(opened) == 6 and "cv_hash" in cols and cols.count("cv_hash") == 1,
              "six processes opening it at once add the history's cv_hash once")
        check(set(info) == {"id", "role_ref", "opportunity_id", "created_at",
                            "master_version", "cv_text", "cv_hash", "updated_at",
                            "saved_text", "saved_hash", "saved_name", "saved_at",
                            "score"},
              f"and the copies table arrives with its thirteen columns ({sorted(info)})")
        check(all(info[c]["notnull"] == 0 for c in info if c not in ("role_ref",))
              and info["role_ref"]["notnull"] == 1,
              "every column of it is nullable but the role it is keyed on")
        check(rows_now == [{"id": 1, "opportunity_id": oid, "event": "scored",
                            "cv_match": 80, "cv_hash": None}],
              "the history row already there is untouched and reads no CV hash")
        check(roles_of(env) == before_roles, "and every role is as it was")
        radar_common.get_db(env.db).close()
        check(env.dump() == env.dump() and len(env.copies()) == 0,
              "a second open changes nothing and makes no copy")
        conn = env.conn()
        unique = [r for r in conn.execute("PRAGMA index_list(tailored_cvs)") if r["unique"]]
        conn.close()
        check(bool(unique), "the role a copy is keyed on is unique, one copy per role")


# ------------------------------------------------------------- the layout

def random_doc(rng: random.Random) -> dict:
    """A doc of the editor's shape, with words that try the cleaning."""
    words = ["IEC 62304", "Class C IVD", "led", "built", "team", "FDA", "–", "&",
             "<b>", "\"quoted\"", "naïve", "café", "50%", "C++", "C#", "#1", "ok."]
    awkward = ["", "  ", "- dash first", "## not a heading", "### deep",
               "\ttabbed\tthrough", "line\nbreak", "\u00a0nbsp", "trailing   "]

    def phrase():
        if rng.random() < 0.15:
            return rng.choice(awkward) + " " + rng.choice(words)
        return " ".join(rng.choice(words) for _ in range(rng.randint(1, 9)))

    def lines(n):
        return [{"b": rng.random() < 0.6, "t": phrase()} for _ in range(n)]
    doc = {"name": rng.choice(["Luke Keogh", "  Luke   Keogh ", "# Odd"]),
           "headline": [phrase() for _ in range(rng.randint(0, 3))],
           "sections": []}
    titles = list(cv_doc.STANDARD_SECTIONS) + ["Publications", "Languages"]
    rng.shuffle(titles)
    for title in titles[:rng.randint(1, len(titles))]:
        doc["sections"].append({
            "title": title, "lines": lines(rng.randint(0, 3)),
            "entries": [{"heading": phrase() or "Role", "lines": lines(rng.randint(0, 4))}
                        for _ in range(rng.randint(0, 3))]})
    return doc


def corpus() -> list[dict]:
    rng = random.Random(4)
    docs = [cv_doc.normalize(random_doc(rng)) for _ in range(80)]
    docs = [d for d in docs if d["name"] and all(
        e["heading"] for s in d["sections"] for e in s["entries"])]
    return [cv_doc.normalize(MASTER_DOC)] + docs


def layout_checks() -> None:
    docs = corpus()
    check(len(docs) > 40, f"the corpus has {len(docs)} CVs to try")

    def filled(doc):
        return {**doc, "sections": [s for s in doc["sections"]
                                    if s["lines"] or s["entries"]]}
    round_trip = all(filled(cv_doc.normalize(cv_doc.parse(cv_doc.render(d)))) == filled(d)
                     for d in docs)
    check(round_trip, "parse undoes render: every tidy CV reads back as itself, "
                      "but for where an empty section sits, which is not in the text")
    text_trip = all(cv_doc.render(cv_doc.load_doc(cv_doc.render(d))) == cv_doc.render(d)
                    for d in docs)
    check(text_trip, "and every text the editor writes reads back to the byte")
    check(all(cv_doc.is_layout(cv_doc.render(d)) for d in docs),
          "and is known as the editor's own layout")
    check(cv_doc.render(MASTER_DOC) == MASTER_TEXT and MASTER_TEXT.endswith("\n")
          and "\n\n\n" not in MASTER_TEXT and not MASTER_TEXT.startswith("\n"),
          "the text ends in one line feed, with single blank lines between blocks")
    lines = cv_store.cv_lines(MASTER_TEXT)
    check(lines[0] == "# Luke Keogh" and lines[3] == "" and lines[4] == "## Summary"
          and lines[7] == "" and lines[8] == "## Experience" and lines[9] == ""
          and lines[10] == "### Software Director, Axithra, 2023 to now"
          and lines[11] == "- Lead software for a Class C IVD under IEC 62304.",
          "the name is a level one heading, a section a level two, an entry a level "
          "three, a bullet a dash, and a blank line before each heading")

    # Texts the editor did not write.
    rng = random.Random(7)
    fixed = True
    for _ in range(60):
        raw = "\n".join(rng.choice([
            "", "PROFILE", "Skills:", "## Experience", "#### Deep role", "* starred",
            "• dotted", "- dashed", "plain words", "# Second name", "\tindented",
            "Education & Certifications", "key skills", "### Entry", "  ", "x" * 300,
        ]) for _ in range(rng.randint(0, 25)))
        once = cv_doc.render(cv_doc.load_doc(raw))
        fixed &= cv_doc.render(cv_doc.load_doc(once)) == once
    check(fixed, "any text is read into the layout, and what that gives is the "
                 "editor's own layout, which then reads back as itself")
    # A CV with no headings of its own: sections from the usual words alone.
    plain = ("Luke Keogh\nChichester | luke@example.com\n\nPROFILE\nSoftware leader.\n\n"
             "EXPERIENCE\nDirector, Axithra\nKey achievements:\n* Led the IEC 62304 programme.\n\n"
             "Key Skills:\n- IEC 62304\n\nEDUCATION & CERTIFICATIONS\n- BEng\n\nINTERESTS\n- Sailing\n")
    doc = cv_doc.load_doc(plain)
    titles = [s["title"] for s in doc["sections"]]
    check(doc["name"] == "Luke Keogh" and doc["headline"] == ["Chichester | luke@example.com"]
          and titles == ["Summary", "Experience", "Skills",
                         "Education and certifications", "Interests"]
          and doc["sections"][1]["lines"] == [{"b": False, "t": "Director, Axithra"},
                                              {"b": False, "t": "Key achievements:"},
                                              {"b": True, "t": "Led the IEC 62304 programme."}],
          "a CV with no headings of its own is read the way a person reads it: the first "
          "line is the name, a line that is only the usual word for a section starts it, "
          "Education and Certifications become one, and a label under a job, Key "
          f"achievements:, stays where it is ({titles})")
    # A Word CV, whose Heading styles came through as headings.
    word = ("Luke Keogh\nChichester\n\n# Profile\nSoftware leader.\n\n# Experience\n"
            "## Director, Axithra\nOverview:\nLed a team.\nKey skills:\n"
            "- Led the IEC 62304 programme.\n#### Earlier role, Fujitsu\n• Defence work.\n\n"
            "# Education\n## BEng, Limerick, 2006\nFirst class.\n# Certifications\n"
            "* ISO 13485 Lead Auditor\n# Career history\nEarlier roles, in brief:\n"
            "## Engineer, L3\n- ATC.\n# Interests\n- Sailing\n# Board roles\n")
    wdoc = cv_doc.load_doc(word)
    by = {s["title"]: s for s in wdoc["sections"]}
    exp = by["Experience"]
    check([s["title"] for s in wdoc["sections"]] == [
              "Summary", "Experience", "Skills", "Education and certifications",
              "Career history", "Interests"]
          and [e["heading"] for e in exp["entries"]] == ["Director, Axithra",
                                                        "Earlier role, Fujitsu"]
          and exp["entries"][0]["lines"] == [{"b": False, "t": "Overview:"},
                                             {"b": False, "t": "Led a team."},
                                             {"b": False, "t": "Key skills:"},
                                             {"b": True, "t": "Led the IEC 62304 programme."}]
          and exp["entries"][1]["lines"] == [{"b": True, "t": "Defence work."}],
          "a Word CV is read by its own headings: sections at the depth that names a "
          "usual section, deeper headings as entries, star, dot and dash bullets as "
          "bullets, and a label under a job, Overview: or Key skills:, stays with the job")
    edu = by["Education and certifications"]
    check([e["heading"] for e in edu["entries"]] == ["BEng, Limerick, 2006", "Certifications"]
          and edu["entries"][1]["lines"] == [{"b": True, "t": "ISO 13485 Lead Auditor"}]
          and by["Career history"]["lines"] == [{"b": False, "t": "Earlier roles, in brief:"}]
          and [e["heading"] for e in by["Career history"]["entries"]] == ["Engineer, L3"]
          and by["Interests"]["lines"] == [{"b": True, "t": "Sailing"},
                                           {"b": False, "t": "Board roles"}],
          "nothing moves up the page: Certifications after the degree becomes an entry "
          "after it, a usual section named again further on is a section of its own, and "
          "an empty heading gives its words back as the last line before it")

    folded = cv_doc.load_doc("Luke Keogh\n# Experience\n## Director, Axithra\n- Led.\n"
                             "# Board roles\n# Skills\n- IEC 62304\n")
    fexp = [s for s in folded["sections"] if s["title"] == "Experience"][0]
    check(fexp["entries"][0]["lines"] == [{"b": True, "t": "Led."},
                                          {"b": False, "t": "Board roles"}]
          and not fexp["lines"],
          "an empty heading after a job gives its words back after the job's own lines, "
          "not above the job")

    def sections_of(text):
        return {s["title"]: s for s in cv_doc.load_doc(text)["sections"]}
    early = cv_doc.load_doc("Luke Keogh\n# Board roles\n# Experience\n## Job\n- x\n")
    patents = sections_of("Luke Keogh\n# Experience\n## Job\n- x\n# Patents\n## US 123\n- y\n")
    check(early["headline"] == ["Board roles"]
          and [e["heading"] for e in patents["Patents"]["entries"]] == ["US 123"],
          "an empty heading before any section gives its words to the headline, and a "
          "section with an entry and no lines of its own keeps its heading")
    edu = sections_of("Luke Keogh\n# Education\n- BEng\n# Certifications\n- ISO 13485\n")
    skills = sections_of("Luke Keogh\n# Skills\n- a\n# Skills\n- b\n")
    check(edu["Education and certifications"]["lines"] == [
              {"b": True, "t": "BEng"}, {"b": False, "t": "Certifications"},
              {"b": True, "t": "ISO 13485"}]
          and skills["Skills"]["lines"] == [{"b": True, "t": "a"}, {"b": True, "t": "b"}],
          "a usual section named again with no entries keeps the words it was named by "
          "as a line, unless they are the section's own name")
    inner_skills = sections_of("Luke Keogh\n# Experience\n## Director\n- a\n### Skills\n- b\n")
    check([e["heading"] for e in inner_skills["Experience"]["entries"]] == ["Director", "Skills"]
          and not inner_skills["Skills"]["lines"],
          "a deeper heading that names a usual section, under an open job, is an entry and "
          "does not carry the job's lines off to that section")
    just_after = sections_of("Luke Keogh\n# Experience\nDirector, Axithra\nKey skills:\n- IEC 62304\n")
    check(just_after["Experience"]["lines"][1] == {"b": False, "t": "Key skills:"}
          and not just_after["Skills"]["lines"],
          "a CV whose one heading of its own comes straight after the name is still read "
          "by its headings")

    # And over many: every line of words comes out in the order it went in.
    rng = random.Random(23)
    heads = ["Summary", "Experience", "Skills", "Education", "Certifications", "Profile",
             "Key skills", "Interests", "Patents", "Board roles", "Career history",
             "Overview", "Key achievements"]
    kept_order = True
    for trial in range(400):
        marked = trial % 2 == 0
        out, content = ["Luke Keogh"], []
        for i in range(rng.randint(3, 40)):
            pick = rng.random()
            if pick < 0.25:
                word_ = rng.choice(heads)
                out.append("#" * rng.randint(1, 3) + " " + word_ if marked
                           else rng.choice([word_.upper(), word_ + ":", word_]))
            elif pick < 0.35 and marked:
                out.append("#" * rng.randint(2, 4) + f" Job {i}")
            else:
                text_ = f"c{i} words"
                out.append(rng.choice(["", "- ", "* ", "• "]) + text_)
                content.append(text_)
        rendered = cv_doc.render_lines(cv_doc.load_doc("\n".join(out) + "\n"))
        seen = [re.sub(r"^(#{1,3} |- )", "", line) for line in rendered]
        if [line for line in seen if line in set(content)] != content:
            kept_order = False
            break
    check(kept_order, "over 400 CVs with and without headings of their own, every line of "
                      "words is kept, in the order it came in")
    # Lines a person would never take for a heading stay lines: an acronym, a
    # short line in capitals, a line that only ends in a colon.
    loud = cv_doc.load_doc("Luke Keogh\nIVD\nSSE.\n## Experience\n### Director\n"
                           "- NHS\nFDA:\nRESULTS DRIVEN\n")
    exp = [s for s in loud["sections"] if s["title"] == "Experience"][0]
    check(loud["headline"] == ["IVD", "SSE."]
          and exp["entries"][0]["lines"] == [{"b": True, "t": "NHS"},
                                             {"b": False, "t": "FDA:"},
                                             {"b": False, "t": "RESULTS DRIVEN"}],
          "an acronym, a line in capitals or a line ending in a colon is content, kept "
          "where it was, and never taken for a heading")
    word1 = cv_doc.load_doc("Luke Keogh\n# Experience\n## Director, Axithra\n- Led.\n"
                            "# Skills\n- IEC 62304\n")
    word2 = cv_doc.load_doc("Luke Keogh\n## Experience\n### Director, Axithra\n- Led.\n"
                            "## Skills\n- IEC 62304\n")
    check(word1 == word2 and [e["heading"] for e in word1["sections"][1]["entries"]]
          == ["Director, Axithra"],
          "sections in Heading 1 with jobs in Heading 2 read as sections in Heading 2 "
          "with jobs in Heading 3 do")
    custom = cv_doc.load_doc("Luke Keogh\n## Experience\n### Director\n- Led.\n"
                             "## Patents\n- One.\n## Board roles\n## Skills\n- x\n")
    titles = [s["title"] for s in custom["sections"]]
    check("Patents" in titles and "Board roles" not in titles
          and {"b": False, "t": "Board roles"} in
          [s for s in custom["sections"] if s["title"] == "Patents"][0]["lines"],
          "a heading at a section's depth that names no usual section starts a section "
          "of its own, and one left empty gives its words back to the section before, "
          f"so nothing is lost ({titles})")
    words = set(re.findall(r"\w+", word)) - {"Profile"}
    kept = set(re.findall(r"\w+", cv_doc.render(wdoc)))
    check(words <= kept, "every word of the Word CV is in the layout read from it, "
                         f"but for the heading it renamed ({sorted(words - kept)})")
    check(cv_doc.CITED_TEXT_MAX == score_item.CV_LINE_TEXT_MAX,
          "the layout knows how much of a cited line a working keeps")
    start = time.time()
    lines_of_marks = "# A\n\n## Summary\n" + "".join("- " * 2_400 + f"x{i}\n" for i in range(40))
    flood = cv_doc.accept(lines_of_marks)[1]
    heads_flood = cv_doc.load_doc("Name\n- " + "## " * 1_600 + "x\n")
    took = time.time() - start
    check(flood.endswith("## Summary\n" + "".join(f"- x{i}\n" for i in range(40)))
          and heads_flood["headline"] == ["x"] and took < 5,
          f"lines of thousands of bullet marks, or of heading marks, are read in one pass "
          f"each, not one per mark ({took:.2f} s)")
    start = time.time()
    try:
        cv_doc.accept("# A\n\n## Summary\n" + "- " * 99_990 + "x\n")
        refused_flood = ""
    except cv_doc.CvError as err:
        refused_flood = str(err)
    took = time.time() - start
    check("no CV needs a line of more than" in refused_flood and took < 1,
          f"and a request of a hundred thousand marks on one line is refused before any of "
          f"it is read ({took:.2f} s)")
    fixture = (REPO_ROOT / "test" / "fixtures" / "profile_snapshot.md").read_text(encoding="utf-8")
    fdoc = cv_doc.load_doc(fixture)
    check(not cv_doc.is_layout(fixture) and fdoc["name"].startswith("Profile snapshot")
          and len(fdoc["headline"]) > 10
          and [s["title"] for s in fdoc["sections"]] == list(cv_doc.STANDARD_SECTIONS),
          "a CV with no sections keeps every line, under the name, and gets the four "
          "standard sections to fill")

    # Cleaning, one way for every string.
    nasty = "a\tb\u2028c" + chr(0xFEFF) + "d " + chr(0xD800) + "e  f\x00g\x85h\u00a0i"
    check(cv_doc.clean(nasty) == "a b cd e f g h i",
          "tabs, controls, separators and odd spaces become one space, and lone "
          "surrogates, the byte order mark and non-characters go")
    check(cv_doc.clean("Cafe\u0301") == "Café", "text is made NFC")
    check(cv_doc.clean_plain("## ### Not a heading") == "Not a heading"
          and cv_doc.clean_plain("- x", allow_bullet=False) == "x"
          and cv_doc.clean_plain("- x") == "- x"
          and cv_doc.clean_plain("#### four") == "#### four"
          and cv_doc.clean_plain("#hashtag") == "#hashtag",
          "a paragraph line cannot start like a heading, and a headline line cannot "
          "start like a bullet")
    tidy = cv_doc.normalize({"name": " Luke ", "headline": ["- dash", "", "## h"],
                             "sections": [
                                 {"title": "Skills", "lines": [
                                     {"b": False, "t": "- now a bullet"},
                                     {"b": True, "t": "  "}], "entries": []},
                                 {"title": "Hobbies", "lines": [], "entries": []},
                                 {"title": "Experience", "lines": [], "entries": [
                                     {"heading": "", "lines": []},
                                     {"heading": "Role", "lines": []}]}]})
    check(tidy["name"] == "Luke" and tidy["headline"] == ["dash", "h"]
          and tidy["sections"][1]["lines"] == [{"b": True, "t": "now a bullet"}]
          and [s["title"] for s in tidy["sections"]] == [
              "Summary", "Skills", "Experience", "Education and certifications"]
          and tidy["sections"][2]["entries"] == [{"heading": "Role", "lines": []}],
          "tidying drops empty lines, an empty entry and an empty extra section, makes "
          "a dash line a bullet, and puts a missing standard section where it belongs")
    sparse = cv_doc.render(tidy)
    check("## Summary" not in sparse and "## Education and certifications" not in sparse
          and "## Skills" in sparse,
          "an empty standard section puts no bare heading in the text")
    check(cv_doc.blank_doc()["sections"] == [{"title": t, "lines": [], "entries": []}
                                             for t in cv_doc.STANDARD_SECTIONS],
          "a blank CV has the four standard sections, in order")

    # The caps, and what cannot be taken.
    def refused(text) -> str:
        try:
            cv_doc.accept(text)
        except cv_doc.CvError as err:
            return str(err)
        return ""
    check("no name" in refused("## Skills\n- x\n") and "no name" in refused("PROFILE\nx\n")
          and cv_doc.load_doc("## Skills\n- x\n")["sections"][2]["lines"]
          == [{"b": True, "t": "x"}],
          "a CV whose first line is a section heading has no name, and is refused "
          "until it has one, its section read as a section")
    check("no heading" in refused(cv_doc.render({
        "name": "L", "headline": [], "sections": [{"title": "Experience", "lines": [],
            "entries": [{"heading": "", "lines": [{"b": True, "t": "x"}]}]}]})),
        "an entry with lines and no heading is refused, not guessed at")
    many = "# L\n## Skills\n" + "".join(f"- skill {i}\n" for i in range(cv_doc.CV_LINES_MAX))
    check("lines" in refused(many), f"over {cv_doc.CV_LINES_MAX} lines is refused")
    check("characters long" in refused("# L\n## Summary\n" + "w" * (cv_doc.CV_LINE_MAX + 1) + "\n"),
          f"a line over {cv_doc.CV_LINE_MAX} characters is refused")
    big = "# L\n## Summary\n" + "".join("x" * 900 + f" {i}\n" for i in range(30))
    check("characters long" in refused(big) and str(cv_doc.CV_TEXT_MAX)[:2] in refused(big),
          f"a CV over {cv_doc.CV_TEXT_MAX:,} characters is refused")
    check("no CV needs" in refused("x" * (cv_doc.RAW_TEXT_MAX + 1))
          and "as text" in refused(None) and "as text" in refused(["# L"]),
          "and a request that is not text, or is absurdly long, is refused before it is read")
    doc, text = cv_doc.accept(MASTER_TEXT)
    check(text == MASTER_TEXT and doc == cv_doc.normalize(MASTER_DOC),
          "a CV the editor wrote is taken exactly as it is")

    # The saved copy's name.
    check(cv_doc.saved_name("Veltrix Diagnostics", "Director of Software")
          == "CV - Luke Keogh - Veltrix Diagnostics - Director of Software",
          "a saved copy is named CV - Luke Keogh - <Company> - <Job title>")
    for unknown in (None, "", "  ", "Unknown", "unknown company", "Confidential.",
                    "N/A", "Undisclosed"):
        if cv_doc.saved_name(unknown, "Head of QA") != "CV - Luke Keogh - Head of QA":
            check(False, f"a company of {unknown!r} is not known and is left out")
            break
    else:
        check(True, "and CV - Luke Keogh - <Job title> where the company is not known")
    odd = cv_doc.saved_name('A/B: "Labs" <Ltd>|', 'Head of *QA*? \\ RA\t.\n')
    check(odd == "CV - Luke Keogh - A B Labs Ltd - Head of QA RA"
          and not re.search(r'[<>:"/\\|?*\x00-\x1f]', odd),
          f"characters a file name cannot hold become spaces ({odd!r})")
    long = cv_doc.saved_name("Company " * 30, "Very long title " * 30)
    check(len(long) <= cv_doc.NAME_MAX and not long.endswith((" ", ".", "-"))
          and long.startswith("CV - Luke Keogh - Company"),
          f"a long name is cut to {cv_doc.NAME_MAX} characters on a word, and never "
          "ends in a dot or a space")
    check(cv_doc.saved_name("Café Ølund", "Ingénieur") == "CV - Luke Keogh - Café Ølund - Ingénieur"
          and cv_doc.saved_name("Acme", "") == "CV - Luke Keogh - Acme - Untitled role",
          "letters outside ASCII are kept, and a role with no title still has a name")


def agreement_checks() -> None:
    """The brief's test: the CV line numbers on the page, in the stored working
    and in the scorer's view always agree, for every CV in a corpus."""
    docs = corpus()
    bad_page, bad_scorer, bad_working, bad_data = [], [], [], []
    for i, doc in enumerate(docs):
        text = cv_doc.render(doc)
        lines = cv_store.cv_lines(text)
        # The page: the numbered rows the server draws.
        rows = re.findall(r'<div class="cvl cvl-(\w+)" data-n="(\d+)">.*?'
                          r'<span class="cvl-t">(.*?)</span></div>',
                          cv_page.rows_html(text))
        mark = {"h1": "# ", "h2": "## ", "h3": "### ", "bullet": "- ",
                "plain": "", "blank": ""}
        import html as _html
        page = [(int(n), mark[kind] + _html.unescape(words)) for kind, n, words in rows]
        if page != list(enumerate(lines, 1)):
            bad_page.append(i)
        # The scorer's view: the system block a tailored copy is scored with.
        blocks = score_item.build_scorer_system(CFG, basis=SPEC, cv={
            "text": text, "label": "tailored copy x", "version": "tailored-x"})
        shown = system_cv(blocks)
        if (sorted(shown) != list(range(1, len(lines) + 1))
                or any(f"[{n}] {lines[n - 1]}".rstrip() != f"[{n}] {shown[n]}".rstrip()
                       for n in shown)
                or blocks.cv_lines != lines):
            bad_scorer.append(i)
        # The stored working: a reply that cites every line with words on it.
        cited = [n for n in range(1, len(lines) + 1) if lines[n - 1].strip()]

        def cite_all(call, cited=cited):
            reply = json.loads(spec_reply(call)[0])
            reply["asks"] = [{"ask": f"ask {n}", "status": "met", "cv_lines": [n]}
                             for n in cited[:25]]
            return json.dumps(reply), usage()
        MODEL["hook"] = cite_all
        scored, _usage, _note = score_item.score_opportunity(
            {"title": "Head of Software", "company": "Veltrix Diagnostics",
             "description": DESCRIPTION, "basis": SPEC}, blocks, CFG,
            mock_fn=mocks.mock_scorer, basis=SPEC)
        MODEL["hook"] = None
        got = [(c["n"], c["text"]) for a in scored["working"]["asks"] for c in a["lines"]]
        want = [(n, lines[n - 1].strip()[:score_item.CV_LINE_TEXT_MAX]) for n in cited[:25]]
        if got != want or scored["working"]["cv_hash"] != cv_store.cv_hash(text):
            bad_working.append(i)
        # What the page's script is handed: the same lines.
        attr = re.search(r'data-cv="([^"]*)"', cv_page.editor_html(text, {"mode": "tailored"}))
        data = json.loads(_html.unescape(attr.group(1)))
        if data["lines"] != lines or cv_doc.render(data["doc"]) != text:
            bad_data.append(i)
    check(not bad_page, f"the page numbers every line of {len(docs)} CVs as "
                        f"cv_lines does, row n is line n ({bad_page[:5]})")
    check(not bad_scorer, "the scorer is shown [n] before line n of the same text, "
                          f"for every one of them ({bad_scorer[:5]})")
    check(not bad_working, "a working cites line n with line n's text, and records "
                           f"the version of the CV it numbered ({bad_working[:5]})")
    check(not bad_data, "and the page's script is handed exactly those lines and a "
                        f"doc that renders back to the text ({bad_data[:5]})")
    # A cited line longer than a citation keeps is found again by all of it.
    long_doc = cv_doc.normalize({**MASTER_DOC, "sections": [
        {"title": "Summary", "entries": [], "lines": [
            {"b": False, "t": "Regulated software in IVD. " * 12 + "Ends with Rust."}]}]})
    long_text = cv_doc.render(long_doc)
    long_lines = cv_store.cv_lines(long_text)
    n = next(i for i, l in enumerate(long_lines, 1) if l.endswith("Ends with Rust."))
    blocks = score_item.build_scorer_system(CFG, basis=SPEC, cv={
        "text": long_text, "label": "tailored copy x", "version": "tailored-x"})

    def cite_long(call):
        reply = json.loads(spec_reply(call)[0])
        reply["asks"] = [{"ask": "Rust", "status": "met", "cv_lines": [n]},
                         {"ask": "Name", "status": "met", "cv_lines": [1]}]
        return json.dumps(reply), usage()
    MODEL["hook"] = cite_long
    scored, _usage, _note = score_item.score_opportunity(
        {"title": "Head of Software", "company": "Veltrix Diagnostics",
         "description": DESCRIPTION, "basis": SPEC}, blocks, CFG,
        mock_fn=mocks.mock_scorer, basis=SPEC)
    MODEL["hook"] = None
    working = scored["working"]
    whole = long_lines[n - 1]
    check(len(whole) > score_item.CV_LINE_TEXT_MAX
          and working["asks"][0]["lines"][0]["text"] == whole[:score_item.CV_LINE_TEXT_MAX]
          and working["long_lines"] == {str(n): whole},
          "a working keeps the start of a long cited line with the citation and the "
          "whole of it beside, by number, and nothing for a short one")
    rust_gone = long_lines[:n - 1] + [whole.replace("Rust", "Go")] + long_lines[n:]
    after = cv_doc.place_asks(working["asks"], rust_gone, working["long_lines"])
    check(after[0]["stale"] and after[0]["placed"][0]["now"] is None
          and after[0]["placed"][0]["whole"] == whole and not after[1]["stale"]
          and not cv_doc.place_asks(working["asks"], long_lines, working["long_lines"])[0]["stale"],
          "an edit past the start a citation keeps is seen, and makes the ask stale, "
          "and the line as it was is found")
    check(cv_doc.line_key("- " + whole) == cv_doc.line_key("* " + whole) == whole
          and cv_doc.line_key("## Skills") == "Skills"
          and cv_doc.line_key(whole + " more") != cv_doc.line_key(whole),
          "a line is known by all its words, its marks set aside")
    exact = "w" * cv_doc.CITED_TEXT_MAX
    cited = [{"ask": "Exact", "status": "met", "lines": [{"n": 1, "text": exact}]}]
    grown = [exact + " and more words"]
    check(cv_doc.place_asks(cited, grown, {})[0]["stale"]
          and not cv_doc.place_asks(cited, grown, None)[0]["stale"]
          and not cv_doc.cited_is_cut(cited[0]["lines"][0], {})
          and cv_doc.cited_is_cut(cited[0]["lines"][0], None),
          "a cited line of exactly the length a citation keeps is whole in a working that "
          "keeps whole lines, so words added to its end are seen, and is the start of a "
          "line only in a working from before, which has no long_lines at all")

    # The cited lines found again in a copy that has been edited.
    lines = cv_store.cv_lines(MASTER_TEXT)
    risk = lines.index("- Own the software risk file under ISO 14971.") + 1
    asks = [{"ask": "Risk", "status": "met",
             "lines": [{"n": risk, "text": lines[risk - 1]}]},
            {"ask": "Gone", "status": "partly",
             "lines": [{"n": 2, "text": "A line nobody wrote."}]},
            {"ask": "Kubernetes", "status": "missing", "lines": []},
            {"ask": "Unanchored", "status": "met", "lines": [], "anchored": False}]
    placed = cv_doc.place_asks(asks, lines)
    check(placed[0]["placed"][0]["now"] == risk and not placed[0]["stale"]
          and placed[1]["placed"][0]["now"] is None and placed[1]["stale"]
          and not placed[2]["stale"] and not placed[3]["stale"],
          "a cited line still there is found at its number, one that is gone makes a "
          "met or partly ask stale, and a missing or unanchored ask is never stale")
    moved = ["# Luke Keogh", "A new first line."] + lines[1:]
    shifted = cv_doc.place_asks(asks[:1], moved)[0]
    check(shifted["placed"][0]["now"] == risk + 1 and not shifted["stale"],
          "a line pushed down by an insertion is found one further on, by its text")
    starred = [l.replace("- Own", "* Own") for l in lines]
    check(cv_doc.find_line({"n": risk, "text": lines[risk - 1]}, starred) == risk,
          "a line is the same line whatever bullet mark a layout gives it")
    twice = lines + [lines[risk - 1]]
    check(cv_doc.find_line({"n": len(twice), "text": lines[risk - 1]}, twice) == len(twice)
          and cv_doc.find_line({"n": 3, "text": lines[risk - 1]}, twice) == risk,
          "a line written twice is found nearest the number it was cited at")
    check(cv_doc.find_line({"n": 1, "text": ""}, lines) is None
          and cv_doc.place_asks(asks, None)[1]["stale"] is False,
          "an empty citation finds nothing, and with no text to place in nothing is stale")
    before = [{"ask": "IEC 62304 software lifecycle", "status": "met"},
              {"ask": "Kubernetes and cloud", "status": "missing"},
              {"ask": "FDA submission", "status": "partly"},
              {"ask": "Rust", "status": "missing"}]
    after = [{"ask": "IEC 62304 software lifecycle.", "status": "met"},
             {"ask": "kubernetes and Cloud", "status": "met"},
             {"ask": "FDA submissions", "status": "missing"},
             {"ask": "Python", "status": "partly"}]
    changes = cv_doc.ask_changes(before, after)
    check(changes == [{"ask": "kubernetes and Cloud", "from": "missing", "to": "met"},
                      {"ask": "FDA submissions", "from": "partly", "to": "missing"},
                      {"ask": "Python", "from": None, "to": "partly"},
                      {"ask": "Rust", "from": "missing", "to": None}],
          "which asks changed: the same words set aside case and punctuation, or nine "
          f"tenths alike, and the ones new or no longer listed ({changes})")
    check([cv_doc.change_word(c) for c in changes] == ["better", "worse", "new", "gone"],
          "and each change reads as better, worse, new or gone")


# ------------------------------------------------------ the scorer's view

def scorer_checks() -> None:
    card = score_item.build_scorer_system(CFG)
    check(MASTER_TEXT in card[0]["text"] and card.cv_hash is None,
          "a card is still scored with the master as it is, and its blocks carry no "
          "CV hash")
    master = score_item.build_scorer_system(CFG, basis=SPEC)
    check(master.cv_version == MASTER_LABEL[0]
          and master.cv_hash == cv_store.cv_hash(MASTER_TEXT)
          and cv_store.numbered_cv(MASTER_TEXT) in master[0]["text"],
          "the master's full description blocks carry its version and the hash of "
          "it as numbered")
    copy_text = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
    real = score_item.load_cv_text

    def no_master(config):
        raise AssertionError("the master was read for a tailored copy")
    score_item.load_cv_text = no_master
    try:
        tailored = score_item.build_scorer_system(CFG, basis=SPEC, cv={
            "text": copy_text, "label": "tailored copy abc", "version": "tailored-abc"})
    finally:
        score_item.load_cv_text = real
    swapped = tailored[0]["text"].replace(cv_store.numbered_cv(copy_text),
                                          cv_store.numbered_cv(MASTER_TEXT))
    swapped = swapped.replace("tailored copy abc", "config/profile/" + MASTER_LABEL[0])
    check(swapped == master[0]["text"],
          "the copy is scored with the master's own blocks, the same rubric, "
          "preferences and numbering, with the copy's text and its label where the "
          "master's would be, and the master is not read")
    check(tailored.cv_version == "tailored-abc" and tailored.cv_lines == cv_store.cv_lines(copy_text)
          and tailored.cv_hash == cv_store.cv_hash(copy_text)
          and tailored.prompt_version == master.prompt_version == radar_common.prompt_version(),
          "its blocks carry the copy's version, lines and hash, and the prompt version "
          "is the prompt file's, unchanged")
    check(cv_store.cv_hash("# A\r\nb\r\n") == cv_store.cv_hash("# A\nb") != cv_store.cv_hash("# A\nc")
          and cv_store.cv_hash("") is None and len(cv_store.cv_hash("# A")) == 12,
          "a CV's hash is of the text as numbered: line endings and a final line feed "
          "make no other version, a changed line does, and an empty CV has none")


# ---------------------------------------------------------------- the copy

def copy_checks() -> None:
    with fresh() as env:
        oid = env.role()
        out = env.open(oid)
        copy = env.copy(oid)
        check(out["ok"] and out["outcome"] == "made" and not CALLS
              and copy["cv_text"] == MASTER_TEXT
              and copy["cv_hash"] == cv_store.cv_hash(MASTER_TEXT)
              and copy["master_version"] == MASTER_LABEL[0]
              and copy["role_ref"] == "key:" + env.row(oid)["role_key"]
              and copy["opportunity_id"] == oid and copy["saved_text"] is None,
              "the first open makes the copy from the master, with no model call, "
              "keyed on the role, naming the master version it came from")
        check(out["copy"]["lines"] == cv_store.cv_lines(MASTER_TEXT)
              and cv_doc.render(out["copy"]["doc"]) == MASTER_TEXT
              and out["copy"]["saved"] is None,
              "and hands the page the copy's lines and its doc")
        MASTER[0] = edited(MASTER_TEXT, "Chichester", "Brighton")
        again = env.open(oid)
        check(again["outcome"] == "found" and env.copy(oid)["cv_text"] == MASTER_TEXT
              and len(env.copies()) == 1,
              "a second open finds the copy and keeps it, even when the master has "
              "changed since")
        repost = env.role(title="Head of Software", company="Veltrix Diagnostics")
        check(env.open(repost)["outcome"] == "found" and len(env.copies()) == 1
              and env.copy(repost)["id"] == copy["id"],
              "a repost of the role shares its one copy")
        lone = env.role(title="", company="")
        env.open(lone)
        check(env.copy(lone)["role_ref"] == f"id:{lone}" and len(env.copies()) == 2,
              "a row with no role key has a copy of its own, keyed on the row")

        # Save CV.
        text = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
        saved = env.save(oid, text)
        copy = env.copy(oid)
        check(saved["ok"] and saved["outcome"] == "saved"
              and saved["saved_name"] == "CV - Luke Keogh - Veltrix Diagnostics - Head of Software"
              and copy["saved_text"] == text == copy["cv_text"]
              and copy["saved_hash"] == cv_store.cv_hash(text)
              and copy["saved_name"] == saved["saved_name"] and copy["saved_at"]
              and saved["copy"]["saved"]["same"] is True and not CALLS,
              "Save CV stores the copy on screen as the saved copy, under the brief's "
              "name, with no model call")
        first_at = copy["saved_at"]
        text2 = edited(text, "Oversaw FDA clearance", "Led FDA clearance")
        env.save(repost, text2)
        copy = env.copy(oid)
        check(copy["saved_text"] == text2 and len(env.copies()) == 2,
              "saving again, from either advert of the role, replaces the save before, "
              "and only for that role")
        check(env.copy(lone)["saved_text"] is None,
              "another role's copy keeps its own state")
        nameless = env.role(title="Quality Lead", company="Confidential")
        env.save(nameless, MASTER_TEXT)
        check(env.copy(nameless)["saved_name"] == "CV - Luke Keogh - Quality Lead",
              "a role whose company is not known is saved as CV - Luke Keogh - <Job title>")
        check(env.save(oid, "## Skills\n- x\n") == {
            "ok": False, "invalid": True, "note": "The CV has no name on its first line."}
              and env.copy(oid)["saved_text"] == text2,
              "a CV that cannot be taken is refused, and the save before stays")
        reset_out = env.reset_copy(oid)
        copy = env.copy(oid)
        check(reset_out["outcome"] == "reset" and copy["cv_text"] == MASTER[0]
              and copy["master_version"] == MASTER_LABEL[0]
              and copy["saved_text"] == text2 and not CALLS,
              "Start again from master puts the master as it is now on the copy, and "
              "keeps the saved copy")
        check(first_at <= copy["saved_at"], "the save keeps its own date")

        # Roles that may not be changed.
        applied = env.role(title="Applied Role", company="Alpha Diagnostics")
        env.apply(applied)
        mate = env.role(title="Applied Role", company="Alpha Diagnostics")
        acked = env.role(title="Acked", company="Beta",
                         acknowledged_at="2026-11-11T09:00:00Z")
        closed = env.role(title="Closed", company="Gamma", closed_at="2026-11-11T09:00:00Z")
        dumped = env.dump()
        outs = [env.open(applied), env.open(mate), env.save(applied, MASTER_TEXT),
                env.reset_copy(mate), env.rescore(applied, MASTER_TEXT),
                env.open(acked), env.save(closed, MASTER_TEXT), env.rescore(acked, MASTER_TEXT)]
        check([o["outcome"] for o in outs] == ["applied"] * 5 + ["not_open"] * 3
              and env.dump() == dumped and not CALLS,
              "an applied role, any advert of its chain, and an acknowledged or closed "
              "role get no copy, no save, no reset and no score, and nothing is written")
        check("is marked applied" in outs[0]["notices"][0]["text"]
              and "not open" in outs[5]["notices"][0]["text"],
              "and each says why")
        check(prep.open_copy(999999, env.db, CFG) == {"ok": False, "note": "no opportunity 999999"},
              "a role that does not exist is said to be so")

        # No master to start from.
        real = score_item.load_cv_text

        def refusal(config):
            raise SystemExit(score_item.FIXTURE_REFUSAL)
        score_item.load_cv_text = refusal
        try:
            fresh_role = env.role(title="Another", company="Delta Diagnostics")
            out = env.open(fresh_role)
        finally:
            score_item.load_cv_text = real
        check(out["outcome"] == "no_master" and "could not be read" in out["notices"][0]["text"]
              and env.copy(fresh_role) is None,
              "a master that cannot be read makes no copy and says so")


def master_untouched_checks() -> None:
    """The master CV is never changed by tailoring (journey step 4, C4)."""
    tmp = Path(tempfile.mkdtemp(prefix="radar-cv-master-"))
    real_load, real_version = score_item.load_cv_text, score_item.get_cv_version
    saved_dir = cv_store.PROFILE_DIR
    (tmp / "cv-20261110-090000.md").write_text(MASTER_TEXT, encoding="utf-8", newline="\n")
    (tmp / "cv.active").write_text("cv-20261110-090000.md\n", encoding="utf-8")

    def from_profile(config):
        name = cv_store.active_cv_name(tmp)
        return (tmp / name).read_text(encoding="utf-8"), f"config/profile/{name}"
    cv_store.PROFILE_DIR = tmp
    score_item.load_cv_text = from_profile
    score_item.get_cv_version = lambda config: cv_store.active_cv_name(tmp)

    def snapshot():
        return {p.name: p.read_bytes() for p in sorted(tmp.iterdir())}
    try:
        before = snapshot()
        with fresh() as env:
            oid = env.role()
            env.open(oid)
            changed = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
            env.rescore(oid, changed)
            env.save(oid, changed)
            env.reset_copy(oid)
            env.save(oid, changed)
            copy = env.copy(oid)
        check(snapshot() == before and copy["saved_text"] == changed
              and copy["master_version"] == "cv-20261110-090000.md",
              "opening, editing, rescoring, saving and starting again on a role's copy "
              "leave every file of the master CV, and its marker, exactly as they were")
    finally:
        cv_store.PROFILE_DIR = saved_dir
        score_item.load_cv_text, score_item.get_cv_version = real_load, real_version
        shutil.rmtree(tmp, ignore_errors=True)


class WriteLock:
    """Another process holding the write lock, as the inbox run does."""

    def __init__(self, db):
        self.db = db
        self.conn = None

    def __enter__(self):
        self.conn = sqlite3.connect(self.db, timeout=1, isolation_level=None)
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, *exc):
        self.conn.execute("ROLLBACK")
        self.conn.close()


@contextlib.contextmanager
def short_write_wait(seconds=0.2):
    saved = prep.WRITE_WAIT_SECONDS
    prep.WRITE_WAIT_SECONDS = seconds
    try:
        yield
    finally:
        prep.WRITE_WAIT_SECONDS = saved


def copy_busy_checks() -> None:
    with fresh() as env:
        oid = env.role()
        dumped = env.dump()
        with short_write_wait(), WriteLock(env.db):
            outs = [env.open(oid), env.save(oid, MASTER_TEXT), env.reset_copy(oid),
                    env.rescore(oid, MASTER_TEXT)]
        check([o["outcome"] for o in outs] == ["db_busy"] * 4
              and env.dump() == dumped and not CALLS,
              "while another process holds the write lock, open, save, reset and "
              "rescore store nothing and call nothing")
        check("nothing was spent" in outs[3]["notices"][0]["text"]
              and "Nothing on the page was lost" in outs[1]["notices"][0]["text"],
              "and say that nothing was lost and nothing was spent")
        lock = prep.GUARD.lock_for(("cv", "key:" + env.row(oid)["role_key"]))
        lock.acquire()
        saved_wait = prep.LOCK_WAIT_SECONDS
        prep.LOCK_WAIT_SECONDS = 0.05
        try:
            out = env.rescore(oid, MASTER_TEXT)
        finally:
            prep.LOCK_WAIT_SECONDS = saved_wait
            lock.release()
        check(out["outcome"] == "busy" and not CALLS and env.dump() == dumped,
              "a click while another click on the role's CV is working waits, then "
              "says so and does nothing")


# ------------------------------------------------------------ Rescore role

def rescore_checks() -> None:
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        role_before = opp_row_of(env, oid)
        out = env.rescore(oid, COPY_TEXT)
        copy = env.copy(oid)
        record = tailored_cv.read_score(copy)
        hist = env.history(oid)
        runs = env.runs()
        desc_hash = radar_common.description_version(DESCRIPTION)
        check(out["ok"] and out["outcome"] == "scored" and out["scored"]
              and len(CALLS) == 1 and CALLS[0]["max_tokens"] == score_item.SPEC_MAX_TOKENS,
              "the first Rescore role is one model call, at the full description's "
              "ceiling")
        check("tailored copy " + copy["cv_hash"] in CALLS[0]["system"][0]["text"]
              and cv_store.numbered_cv(COPY_TEXT) in CALLS[0]["system"][0]["text"]
              and '"basis": "spec"' in CALLS[0]["user"]
              and DESCRIPTION.splitlines()[1] in json.loads(CALLS[0]["user"].split("\n\n", 1)[1])["description"],
              "it shows the scorer the copy, numbered, and the stored description")
        check(record and record["description_hash"] == desc_hash
              and record["cv_hash"] == copy["cv_hash"] == cv_store.cv_hash(COPY_TEXT)
              and record["cv_match"] == 80 and record["tier"] == "top"
              and record["prompt_version"] == radar_common.prompt_version()
              and record["score_model"] == "mock"
              and [a["status"] for a in record["working"]["asks"]] ==
                  ["met", "partly", "met", "missing", "met"]
              and record["working"]["cv_version"] == "tailored-" + copy["cv_hash"]
              and record["before"]["source"] == "master" and record["before"]["cv_match"] == 80
              and record["changes"] is None and record["reused"] is False,
              "the tailored score is stored on the copy with the description and copy it "
              "was made on, its working, and the master score it is set beside")
        check(len(hist) == 1 and hist[0]["event"] == "tailored" and hist[0]["writer"] == "prepare"
              and hist[0]["cv_hash"] == copy["cv_hash"] and hist[0]["description_hash"] == desc_hash
              and hist[0]["score_basis"] == SPEC and hist[0]["cv_match"] == 80
              and hist[0]["cv_version"] == "tailored-" + copy["cv_hash"]
              and json.loads(hist[0]["working"])["asks"] == record["working"]["asks"],
              "a score_history row marked tailored keeps it, with the CV hash")
        check(len(runs) == 1 and runs[0]["items_new"] == 1
              and runs[0]["note"] == f"id {oid}, tailored copy {copy['cv_hash']}, 5 asks",
              "and its cost is a row in runs, under prepare, that names the copy")
        check(opp_row_of(env, oid) == role_before,
              "the role's own score, the one the Jobs page shows, is not touched")

        # Pressed again with the same text: nothing spent.
        out = env.rescore(oid, COPY_TEXT)
        check(out["outcome"] == "reused" and len(CALLS) == 1 and len(env.history(oid)) == 1
              and "nothing was spent" in out["notices"][0]["text"],
              "pressed again on the same copy, nothing is spent and nothing is written")

        # An edit, then scored: before is the copy's own last score.
        text = cv_doc.accept(edited(COPY_TEXT, "- SpiraPlan and regulated ALM configuration",
                                    "- SpiraPlan and regulated ALM configuration\n"
                                    "- Kubernetes and cloud deployment of clinical systems"))[1]
        out = env.rescore(oid, text)
        record = tailored_cv.read_score(env.copy(oid))
        check(out["outcome"] == "scored" and len(CALLS) == 2
              and env.copy(oid)["cv_text"] == text
              and record["cv_match"] == 90 and record["before"]["source"] == "tailored"
              and record["before"]["cv_match"] == 80
              and record["changes"] == [{"ask": "Kubernetes and cloud deployment of clinical systems",
                                         "from": "missing", "to": "met"}]
              and out["tailored"] == {"cv_match": 90, "tier": "top", "before_cv_match": 80,
                                      "before_tier": "top", "before_source": "tailored",
                                      "changes": 1},
              "an edited copy is one more call, stored as the copy, and its score says "
              "what it was before and which ask moved, missing to met")
        check(opp_row_of(env, oid) == role_before, "and the role's own score is still not touched")

        # Back to the earlier text: the history has that pair.
        out = env.rescore(oid, COPY_TEXT)
        record = tailored_cv.read_score(env.copy(oid))
        check(out["outcome"] == "reused" and len(CALLS) == 2
              and record["cv_hash"] == cv_store.cv_hash(COPY_TEXT) and record["reused"]
              and record["cv_match"] == 80 and record["before"]["cv_match"] == 90
              and len(env.history(oid)) == 2,
              "a copy put back to text scored before is shown that score again from the "
              "history, and nothing is spent")

        # The master as scored stands for an unchanged copy.
    with fresh() as env:
        oid = env.role()
        env.prepare(oid)
        check(len(CALLS) == 1 and env.row(oid)["score_basis"] == SPEC,
              "the role is scored against its description on the master, Brief 3's click")
        working = json.loads(env.row(oid)["score_working"])
        check(working["cv_hash"] == cv_store.cv_hash(MASTER_TEXT),
              "and that score's working records the hash of the master as numbered")
        env.open(oid)
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "reused" and len(CALLS) == 1
              and "master CV as it was scored" in out["notices"][0]["text"]
              and tailored_cv.read_score(env.copy(oid)) is None,
              "a copy that is the master unchanged is not scored again: the master's "
              "score against this description stands for it")
        text = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
        out = env.rescore(oid, text)
        record = tailored_cv.read_score(env.copy(oid))
        check(out["outcome"] == "scored" and len(CALLS) == 2
              and record["before"]["source"] == "master"
              and record["before"]["cv_hash"] == cv_store.cv_hash(MASTER_TEXT)
              and record["changes"] == [{"ask": "Experience building a software team from scratch",
                                         "from": "met", "to": "partly"}],
              "an edited copy is compared with the master's own asks, and an edit that "
              "weakens a line shows as an ask that got worse")

        # No description: nothing to score against.
        bare = env.role(title="No description", company="Zeta", description=None)
        env.open(bare)
        out = env.rescore(bare, MASTER_TEXT)
        check(out["outcome"] == "no_description" and len(CALLS) == 2
              and "nothing was spent" in out["notices"][0]["text"],
              "a role with no stored description is not scored, and says so")

        # A CV that cannot be taken.
        bad = env.rescore(oid, "## Skills\n- x\n")
        check(bad == {"ok": False, "invalid": True, "note": "The CV has no name on its first line."}
              and len(CALLS) == 2,
              "a CV that cannot be taken is refused before anything is written or paid for")


def rescore_guard_checks() -> None:
    # A double click, and a storm, pay once.
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        gate = threading.Event()

        def slow(call):
            gate.wait(5)
            return spec_reply(call)
        MODEL["hook"] = slow
        outs = []
        threads = [threading.Thread(target=lambda: outs.append(env.rescore(oid, COPY_TEXT)))
                   for _ in range(12)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        gate.set()
        for t in threads:
            t.join()
        check(len(CALLS) == 1 and sorted(o["outcome"] for o in outs) ==
              ["reused"] * 11 + ["scored"] and len(env.history(oid)) == 1,
              "twelve clicks at once on the same copy pay for one call")
        repost = env.role()
        out = env.rescore(repost, COPY_TEXT)
        check(out["outcome"] == "reused" and len(CALLS) == 1,
              "and the same copy pressed from a repost of the role finds the score made")

    # A reply that cannot be used: remembered five minutes.
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        MODEL["hook"] = lambda call: ("not json {{{", usage())
        out = env.rescore(oid, COPY_TEXT)
        check(out["outcome"] == "score_failed" and len(CALLS) == 1
              and "could not be used" in out["notices"][0]["text"]
              and "tailored copy" in out["notices"][0]["text"]
              and "The reply was paid for." in out["notices"][0]["text"]
              and tailored_cv.read_score(env.copy(oid)) is None
              and env.runs()[0]["items_new"] == 0
              and "tailored copy" in env.runs()[0]["note"]
              and "not stored" in env.runs()[0]["note"],
              "a reply that cannot be used stores nothing, says it was paid for, and its "
              "cost is a runs row marked not stored")
        out = env.rescore(oid, COPY_TEXT)
        check(out["outcome"] == "cooling" and len(CALLS) == 1,
              "the same copy pressed again inside five minutes calls nothing")
        text = edited(COPY_TEXT, "Built the software team", "Built and led the team")
        MODEL["hook"] = None
        out = env.rescore(oid, text)
        check(out["outcome"] == "scored" and len(CALLS) == 2,
              "while an edited copy is a new question, asked at once")
        MODEL["hook"] = lambda call: ("not json {{{", usage())
        CLOCK[0] += prep.UNUSABLE_COOLDOWN_SECONDS + 1
        out = env.rescore(oid, COPY_TEXT)
        check(out["outcome"] == "score_failed" and len(CALLS) == 3,
              "and after the five minutes the first copy may be asked again")

    # A call that raised: twenty seconds. A scorer that could not be set up: twenty.
    with fresh() as env:
        oid = env.role()
        env.open(oid)

        def boom(call):
            raise RuntimeError("no network")
        MODEL["hook"] = boom
        with captured_stderr():
            out = env.rescore(oid, COPY_TEXT)
        check(out["outcome"] == "score_failed" and len(CALLS) == 1
              and "could not be reached (RuntimeError)" in out["notices"][0]["text"]
              and "tailored copy was not scored" in out["notices"][0]["text"],
              "a call that raised stores nothing and says so")
        env.rescore(oid, COPY_TEXT)
        CLOCK[0] += prep.ERROR_COOLDOWN_SECONDS + 1
        MODEL["hook"] = None
        out = env.rescore(oid, COPY_TEXT)
        check(len(CALLS) == 2 and out["outcome"] == "scored",
              "it is not tried again for twenty seconds, then it is")
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        real = score_item.build_scorer_system

        def broken(*a, **k):
            raise SystemExit("no floor line")
        score_item.build_scorer_system = broken
        try:
            out = env.rescore(oid, COPY_TEXT)
            again = env.rescore(oid, COPY_TEXT)
        finally:
            score_item.build_scorer_system = real
        check(out["outcome"] == "score_failed" and not CALLS
              and "could not be set up" in out["notices"][0]["text"]
              and again["outcome"] == "cooling",
              "a scorer that could not be set up costs nothing and is not tried for twenty "
              "seconds")

    # A busy database when the reply comes: parked, and stored by the next click.
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        holder = {}

        def lock_then_answer(call):
            holder["lock"] = WriteLock(env.db).__enter__()
            return spec_reply(call)
        MODEL["hook"] = lock_then_answer
        with short_write_wait():
            out = env.rescore(oid, COPY_TEXT)
        holder["lock"].__exit__(None, None, None)
        check(out["outcome"] == "score_held" and len(CALLS) == 1
              and tailored_cv.read_score(env.copy(oid)) is None
              and "held in memory" in out["notices"][0]["text"],
              "a reply that comes while the database is busy is held, not lost")
        MODEL["hook"] = None
        other = edited(COPY_TEXT, "Built the software team", "Built and led the team")
        out = env.rescore(oid, other)
        check(out["outcome"] == "scored" and len(CALLS) == 2
              and tailored_cv.read_score(env.copy(oid))["cv_hash"] == cv_store.cv_hash(other),
              "what is held is for its own copy only, another text is a call of its own")
        out = env.rescore(oid, COPY_TEXT)
        record = tailored_cv.read_score(env.copy(oid))
        check(out["outcome"] == "scored" and len(CALLS) == 2
              and record["cv_hash"] == cv_store.cv_hash(COPY_TEXT)
              and len(env.runs()) == 2,
              "and the click that brings the held copy back stores the held reply with no "
              "second call")

    # Applied, or closed, while the model answers.
    for label, stamp in (("applied", "UPDATE opportunities SET applied_at = '2026-11-12T10:00:00Z' WHERE id = ?"),
                         ("closed", "UPDATE opportunities SET closed_at = '2026-11-12T10:00:00Z' WHERE id = ?")):
        with fresh() as env:
            oid = env.role()
            env.open(oid)
            MODEL["hook"] = lambda call, oid=oid, stamp=stamp: (env.sql(stamp, (oid,)), spec_reply(call))[1]
            out = env.rescore(oid, COPY_TEXT)
            check(len(CALLS) == 1 and out["outcome"] in ("applied", "not_open")
                  and tailored_cv.read_score(env.copy(oid)) is None
                  and not env.history(oid)
                  and env.runs()[0]["items_new"] == 0 and "not stored" in env.runs()[0]["note"],
                  f"a role {label} while the model answers keeps what it had: the reply "
                  "is not stored, and its cost is recorded as not stored")
            said = out["notices"][0]
            check(("was marked applied while" if label == "applied" else "closed, dismissed")
                  in said["text"] and not said.get("retry")
                  and (("applied while scoring" if label == "applied"
                        else "no longer open while scoring") in env.runs()[0]["note"]),
                  f"and the page is told the role was {label}, with no Try again under it, "
                  "and the cost row says so too")


# ------------------------------------------------------- the review's fixes

# A master as a Word import leaves it, not yet saved on the CV page.
UNLAID = ("Luke Keogh\nChichester, West Sussex\n\nPROFILE\nSoftware leader in regulated "
          "industries.\n\nEXPERIENCE\nSoftware Director, Axithra\n* Built the software "
          "team from nothing.\n")


def gate_checks() -> None:
    """A copy is made only from a master in the editor's layout, so its line
    numbers are the ones the master's own score cites (item 2, item 3)."""
    with fresh() as env:
        MASTER[0] = UNLAID
        check(not cv_doc.is_canonical(UNLAID) and cv_doc.is_canonical(MASTER_TEXT)
              and not tailored_cv.master(CFG)["canonical"],
              "a master imported from Word is not in the editor's layout, and one saved "
              "on the CV page is")
        oid = env.role()
        before = env.dump()
        outs = [env.open(oid), env.reset_copy(oid)]
        check([o["outcome"] for o in outs] == ["master_not_laid_out"] * 2
              and env.dump() == before and not CALLS
              and all(o["notices"][0]["text"] == prep.MASTER_NOT_LAID_OUT for o in outs),
              "open and Start again make no copy from it, write nothing, and say why")
        page = render_page(env, oid)
        left = page[page.index('id="cv-pane"'):page.index('id="prepare-body"')]
        data = pane_data(page)
        rows = re.findall(r'<div class="cvl cvl-\w+" data-n="(\d+)">.*?<span class="cvl-t">(.*?)</span>',
                          left)
        import html as htmllib
        check("has not been saved on the CV page" in page_text(left)
              and '<a href="/cv">Open the CV page</a>' in left
              and "as it is stored, numbered as the scorer numbers it" in page_text(left)
              and data["editable"] is False and data["needsCopy"] is False
              and "data-cv-act" not in left
              and data["lines"] == cv_store.cv_lines(UNLAID)
              and [(int(n), htmllib.unescape(t)) for n, t in rows if t]
              == [(n, l) for n, l in enumerate(cv_store.cv_lines(UNLAID), 1) if l],
              "the page shows the master as it is stored, each line under the number the "
              "scorer gives it, read only, with no button and no copy to make, and links "
              "to the CV page")
        env.prepare(oid)
        inner = render_inner(env, oid)
        cited = re.findall(r'<li data-cv-n="(\d+)" data-cv-text="([^"]*)"', inner)
        stored = cv_store.cv_lines(UNLAID)
        check(len(CALLS) == 1 and cited
              and all(stored[int(n) - 1].strip() == htmllib.unescape(t) for n, t in cited),
              "and once the role is scored on the master as it is, every line an ask "
              "cites is the line the page shows under that number")
        del CALLS[:]
        check("once the master has been saved on the CV page" in page_text(render_inner(env, oid)),
              "and the tailored score says when a copy will be made")
        MASTER[0] = MASTER_TEXT
        check(env.open(oid)["outcome"] == "made" and not CALLS,
              "once the master is in the layout, the copy is made")


def base_checks() -> None:
    """Rescore role and Save CV write over no copy that another tab changed."""
    with fresh() as env:
        oid = env.role()
        taken = env.open(oid)["copy"]["hash"]
        other_tab = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
        check(prep.save_copy(oid, other_tab, env.db, CFG, base=taken)["outcome"] == "saved",
              "a save from the tab that took the copy goes through")
        before = env.dump()
        mine = edited(MASTER_TEXT, "Oversaw FDA clearance", "Led FDA clearance")
        outs = [prep.save_copy(oid, mine, env.db, CFG, base=taken),
                prep.rescore_copy(oid, mine, env.db, CFG, base=taken)]
        check([o["outcome"] for o in outs] == ["copy_moved"] * 2 and env.dump() == before
              and not CALLS and all(o["notices"][0]["text"] == prep.COPY_MOVED for o in outs)
              and all(o.get("copy") is None for o in outs),
              "a tab that took the copy before another tab changed it stores nothing, "
              "spends nothing, and says to reload")
        now = env.copy(oid)["cv_hash"]
        out = prep.rescore_copy(oid, mine, env.db, CFG, base=now)
        check(out["outcome"] == "scored" and len(CALLS) == 1
              and out["copy"]["hash"] == cv_store.cv_hash(mine),
              "with the copy as it is now it goes through, and hands back the new version")
        check(prep.save_copy(oid, mine, env.db, CFG)["outcome"] == "saved",
              "a request that names no version, from a page before this check, is not checked")
        lone = env.role(title="New", company="Theta")
        check(prep.save_copy(lone, mine, env.db, CFG, base="0123456789ab")["outcome"] == "saved",
              "and a role with no copy yet has no copy to have moved")
        with serving(env.db) as port, captured_stderr():
            bad = []
            for base in (5, ["x"], {"a": 1}, "", "x" * 65, True):
                for path in ("/prepare/cv/save", "/prepare/cv/rescore"):
                    status, got, _h = post(port, path, {"id": oid, "text": mine, "base": base})
                    if status != 400:
                        bad.append(f"{path} {base!r} {status}")
            check(not bad and len(CALLS) == 1,
                  f"a version that is not short text is a 400 and runs nothing ({bad[:3]})")
            edges = []
            for base in ("a", "x" * 64):
                status, got, _h = post(port, "/prepare/cv/save", {"id": oid, "text": mine, "base": base})
                if not (status == 200 and got.get("outcome") == "copy_moved"):
                    edges.append(f"{len(base)} {status} {got.get('outcome')}")
            check(not edges, "a version of one character, or of sixty-four, is taken and "
                             f"compared ({edges})")
            before = env.dump()
            status, got, _h = post(port, "/prepare/cv/save", {"id": oid, "text": other_tab,
                                                              "base": taken})
            check(status == 200 and got["outcome"] == "copy_moved" and "html" not in got
                  and got.get("copy") is None and env.dump() == before,
                  "over HTTP a moved copy is an answer that says so, and leaves the page "
                  "as it is to be reloaded")
            status, got, _h = post(port, "/prepare/cv/save", {"id": oid, "text": other_tab,
                                                              "base": env.copy(oid)["cv_hash"]})
            check(status == 200 and got["outcome"] == "saved" and got["copy"]["hash"]
                  == cv_store.cv_hash(other_tab),
                  "and the version that is stored now lets it through")


def unchanged_checks() -> None:
    """Rescore role does not score the master unchanged for a role whose own
    score is not on its description yet: that is the click's question, asked
    once."""
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        before = env.dump()
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "unchanged_copy" and not CALLS and env.dump() == before
              and out["notices"][0]["text"] == prep.UNCHANGED_COPY and not out["scored"]
              and not out["called"],
              "the master unchanged, on a role scored only on its card, is not scored by "
              "Rescore role: nothing is spent and nothing is written")
        other = env.role(title="Other", company="Xi Diagnostics")
        env.open(other)
        with serving(env.db) as port, captured_stderr():
            status, got, _h = post(port, "/prepare/cv/rescore", {"id": other, "text": MASTER_TEXT})
        check(status == 200 and got["outcome"] == "unchanged_copy"
              and "data-prepare-go" in got["html"]
              and "Score it against the stored description" in got["html"]
              and prep.UNCHANGED_COPY not in page_text(got["html"]),
              "the right side it hands back still has the button under the description "
              "that its words point to, and the words themselves go beside the editor")
        env.rescore(oid, COPY_TEXT)
        env.reset_copy(oid)
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "unchanged_copy" and len(CALLS) == 1,
              "nor after Start again from master puts the master back on the copy")
        env.prepare(oid)
        out = env.rescore(oid, MASTER_TEXT)
        check(len(CALLS) == 2 and out["outcome"] == "reused",
              "once the click has scored the master on the description, the same copy "
              "finds that score and spends nothing")
        MASTER[0] = edited(MASTER_TEXT, "Chichester", "Brighton")
        out = env.rescore(oid, MASTER[0])
        check(out["outcome"] == "scored" and len(CALLS) == 3,
              "but a master changed since the role's own score is a copy to score, because "
              "the click does not score a description twice")
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        real = score_item.load_cv_text

        def unreadable(config):
            raise OSError("the master file is gone")
        score_item.load_cv_text = unreadable
        try:
            out = env.rescore(oid, COPY_TEXT)
            reset_out = env.reset_copy(oid)
        finally:
            score_item.load_cv_text = real
        check(out["outcome"] == "scored" and len(CALLS) == 1
              and reset_out["outcome"] == "no_master"
              and "The master CV could not be read." in reset_out["notices"][0]["text"]
              and env.copy(oid)["cv_text"] == COPY_TEXT,
              "with the master unreadable a copy is still scored on its own text, while "
              "Start again says the master cannot be read and leaves the copy as it was")


def legacy_checks() -> None:
    """Scores the Prepare application click stored before this branch: their
    workings keep no cv_hash and no whole long lines."""
    def as_brief3(env, oid):
        old = json.loads(env.row(oid)["score_working"])
        old.pop("cv_hash", None)
        old.pop("long_lines", None)
        env.sql("UPDATE opportunities SET score_working = ? WHERE id = ?",
                (json.dumps(old), oid))

    with fresh() as env:
        oid = env.role()
        env.prepare(oid)
        as_brief3(env, oid)
        env.open(oid)
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "reused" and len(CALLS) == 1
              and "master CV as it was scored" in out["notices"][0]["text"]
              and "The copy is the master CV as it was scored against this description"
              in page_text(render_inner(env, oid)),
              "an unchanged copy of a role scored before workings kept the CV's hash is "
              "known for the master by the dated version the role names, and nothing is "
              "spent")
    with fresh() as env:
        MASTER_LABEL[0] = "cv.txt"
        oid = env.role()
        env.prepare(oid)
        as_brief3(env, oid)
        env.open(oid)
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "scored" and len(CALLS) == 2,
              "under a name that may hold another text since, cv.txt, nothing is "
              "assumed and the copy is scored")
    with fresh() as env:
        MASTER_LABEL[0] = "cv-20261111-090000.md"
        oid = env.role()
        env.prepare(oid)
        as_brief3(env, oid)
        MASTER_LABEL[0] = "cv-20261112-090000.md"
        env.open(oid)
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "scored" and len(CALLS) == 2,
              "nor when the role was scored on a version that is no longer the active one")

    with fresh() as env:
        long_master = cv_doc.render(cv_doc.normalize({**MASTER_DOC, "sections": [
            {"title": "Summary", "entries": [], "lines": [
                {"b": False, "t": "Regulated software in IVD. " * 12 + "Ends with Rust."}]}]
            + MASTER_DOC["sections"][1:]}))
        lines = cv_store.cv_lines(long_master)
        n = next(i for i, l in enumerate(lines, 1) if l.endswith("Ends with Rust."))
        MASTER[0] = long_master
        oid = env.role()
        MODEL["hook"] = lambda call: (json.dumps({**json.loads(spec_reply(call)[0]), "asks": [
            {"ask": "Rust in regulated software", "status": "met", "cv_lines": [n]}]}), usage())
        env.prepare(oid)
        MODEL["hook"] = None
        as_brief3(env, oid)
        env.open(oid)
        inner = render_inner(env, oid)
        check('data-cv-cut="1"' in inner and "stale" not in page_text(inner)
              and f"CV line {n} " in page_text(inner),
              "a long cited line kept only in part by such a score is found again on an "
              "unedited copy, and marked for the page script to find by how it starts")
        conn = radar_common.get_db(env.db)
        try:
            tailored_cv.store_text(conn, env.copy(oid)["role_ref"],
                                   long_master.replace("Regulated software in IVD. ",
                                                       "Regulated medical software. ", 1))
            conn.commit()
        finally:
            conn.close()
        check("1 stale because a line it cited has been edited" in page_text(render_inner(env, oid)),
              "while an edit to the start it kept makes the ask stale")


def click_lock_checks() -> None:
    """Rescore role waits for the Prepare application click on its advert."""
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        gate, started = threading.Event(), threading.Event()

        def slow(call):
            started.set()
            gate.wait(5)
            return spec_reply(call)
        MODEL["hook"] = slow
        outs = {}
        click = threading.Thread(target=lambda: outs.update(click=env.prepare(oid)))
        click.start()
        started.wait(5)
        press = threading.Thread(target=lambda: outs.update(press=env.rescore(oid, MASTER_TEXT)))
        press.start()
        time.sleep(0.3)
        waiting = press.is_alive()
        gate.set()
        click.join(10)
        press.join(10)
        MODEL["hook"] = None
        check(waiting and len(CALLS) == 1 and outs["click"]["outcome"] == "scored"
              and outs["press"]["outcome"] == "reused"
              and not [h for h in env.history(oid) if h["event"] == "tailored"],
              "Rescore role pressed while the click scores the master waits for it, then "
              "finds the master's score for the unchanged copy: one call, not two")
        saved_wait = prep.LOCK_WAIT_SECONDS
        prep.LOCK_WAIT_SECONDS = 0.05
        lock = prep.GUARD.lock_for(oid)
        lock.acquire()
        try:
            out = env.rescore(oid, COPY_TEXT)
        finally:
            lock.release()
        copy_lock = prep.GUARD.lock_for(("cv", "key:" + env.row(oid)["role_key"]))
        copy_lock.acquire()
        try:
            click_out = env.prepare(oid)
        finally:
            copy_lock.release()
            prep.LOCK_WAIT_SECONDS = saved_wait
        check(out["outcome"] == "busy" and len(CALLS) == 1
              and "Prepare application click" in out["notices"][0]["text"],
              "a click that stays at work past the wait is said to be, and nothing is spent")
        check(click_out["outcome"] == "reused",
              "and the click never waits on the copy's lock, so the two cannot wait on each other")


def memo_checks() -> None:
    """A failure is remembered for the role's copy, which every advert of the
    role shares, and never holds up the master's own click."""
    with fresh() as env:
        a = env.role()
        b = env.role()
        env.open(a)
        MODEL["hook"] = lambda call: ("not json {{{", usage())
        first = env.rescore(a, COPY_TEXT)
        MODEL["hook"] = None
        again = env.rescore(b, COPY_TEXT)
        check(first["outcome"] == "score_failed" and again["outcome"] == "cooling"
              and len(CALLS) == 1,
              "a copy whose reply could not be used is not asked again from a repost of "
              "the role inside five minutes, the repost shares the copy")
        out = env.prepare(a)
        check(out["outcome"] == "scored" and len(CALLS) == 2,
              "while the master's own click on the advert is not held up by it")
    with fresh() as env:
        a = env.role()
        env.open(a)
        MODEL["hook"] = lambda call: ("not json {{{", usage())
        env.prepare(a)
        MODEL["hook"] = None
        out = env.rescore(a, COPY_TEXT)
        check(out["outcome"] == "scored" and len(CALLS) == 2,
              "nor the copy by a failure of the master's click")


def edge_checks() -> None:
    """The edges the mutation run found unchecked: the exact caps, a text with
    no final line feed, the parts of a saved name, which way an ask moved, a
    stored record that cannot be read, and reuse across the adverts of a role."""
    def refused(text) -> str:
        try:
            cv_doc.accept(text)
        except cv_doc.CvError as err:
            return str(err)
        return ""

    def with_lines(section, items):
        return cv_doc.render({"name": "L", "headline": [], "sections": [
            {"title": section, "lines": items, "entries": []}]})
    most = cv_doc.CV_LINES_MAX - 3   # "# L", the blank line and "## Skills"
    at = with_lines("Skills", [{"b": True, "t": f"s{i}"} for i in range(most)])
    over = with_lines("Skills", [{"b": True, "t": f"s{i}"} for i in range(most + 1)])
    check(len(cv_store.cv_lines(at)) == cv_doc.CV_LINES_MAX and refused(at) == ""
          and "lines" in refused(over),
          f"exactly {cv_doc.CV_LINES_MAX} lines is taken, and one more is not")
    long_ok = with_lines("Summary", [{"b": False, "t": "w" * cv_doc.CV_LINE_MAX}])
    long_bad = with_lines("Summary", [{"b": False, "t": "w" * (cv_doc.CV_LINE_MAX + 1)}])
    check(refused(long_ok) == "" and "characters long" in refused(long_bad),
          f"a line of exactly {cv_doc.CV_LINE_MAX} characters is taken, and one more is not")
    room = cv_doc.CV_TEXT_MAX - len(with_lines("Summary", [{"b": False, "t": ""}]))
    lines = [{"b": False, "t": "w" * 999}] * (room // 1000)
    filler = with_lines("Summary", lines)
    pad = cv_doc.CV_TEXT_MAX - len(filler) - 1
    exact = with_lines("Summary", lines + [{"b": False, "t": "w" * pad}])
    check(len(exact) == cv_doc.CV_TEXT_MAX and refused(exact) == ""
          and "characters long" in refused(exact.replace("w\n", "ww\n", 1)),
          f"a CV of exactly {cv_doc.CV_TEXT_MAX:,} characters is taken, and one more is not")
    check("no CV needs to be" not in refused("x" * cv_doc.RAW_TEXT_MAX)
          and "no CV needs to be" in refused("x" * (cv_doc.RAW_TEXT_MAX + 1)),
          "a request of exactly the raw cap is read, one character more is not")
    many_lines = "# L\n" + "\n" * (cv_doc.RAW_LINES_MAX - 1)
    check(f"more than {cv_doc.RAW_LINES_MAX:,}" not in refused(many_lines)
          and f"more than {cv_doc.RAW_LINES_MAX:,}" in refused(many_lines + "\n"),
          f"a text of {cv_doc.RAW_LINES_MAX:,} lines is read, one more is refused before it is")
    wide = "# L\n## Summary\n" + "w" * cv_doc.RAW_LINE_MAX + "\n"
    check(f"a line of more than {cv_doc.RAW_LINE_MAX:,}" not in refused(wide)
          and f"a line of more than {cv_doc.RAW_LINE_MAX:,}" in refused(wide.replace("ww", "www", 1)),
          f"a line of {cv_doc.RAW_LINE_MAX:,} characters is read, one more is refused before it is")
    start = time.time()
    try:
        cv_doc.load_doc("Name\n" + "".join(f"# Heading {i}\nline {i}\n" for i in range(999)))
    except cv_doc.CvError:
        pass
    took = time.time() - start
    check(took < 2, f"a text of a thousand headings is read in no more time than its length asks "
                    f"({took:.2f} s)")
    endings = ["", "a", "a\n", "a\nb", "a\r\nb\r\n", "a\rb", "\n", "\n\n", "a\n\nb"]
    check(all(cv_doc._lines_of(t) == cv_store.cv_lines(t) for t in endings)
          and cv_doc._lines_of("a\nb") == ["a", "b"]
          and cv_doc.load_doc("# L\n\n## Skills\n- x")["sections"][2]["lines"]
          == [{"b": True, "t": "x"}],
          "a text with no final line feed keeps its last line, and cv_doc splits "
          "lines as cv_store does")
    check(cv_doc._line("plain") == {"b": False, "t": "plain"}
          and cv_doc._line("- dash") == {"b": True, "t": "dash"}
          and cv_doc._line({"b": True, "t": "• x"}) == {"b": True, "t": "x"},
          "a line that is not a dict is a paragraph line unless it starts with a "
          "bullet mark, and a bullet keeps no mark")
    check(cv_doc.find_line({"n": 3, "text": "A"}, ["X", "A", "B"]) == 2
          and cv_doc.find_line({"n": 1, "text": "X"}, ["X", "A", "X"]) == 1
          and cv_doc.find_line({"n": 3, "text": "X"}, ["X", "A", "X"]) == 3,
          "a cited line is checked at its own number first, and else found where "
          "its text is now")
    part = cv_doc._name_part
    check(part("a" * 50, 50) == "a" * 50 and part("a" * 51, 50) == "a" * 50
          and part("a" * 30 + " " + "b" * 19, 50) == "a" * 30 + " " + "b" * 19
          and part("a" * 30 + " " + "b" * 19 + " c", 50) == "a" * 30 + " " + "b" * 19
          and part("a" * 30 + " " + "b" * 20 + " c", 50) == "a" * 30
          and part("a" * 25 + " " + "b" * 40, 50) == "a" * 25 + " " + "b" * 24
          and part("a" * 26 + " " + "b" * 40, 50) == "a" * 26,
          "a part of a name is cut on the last word that fits, unless that would "
          "keep less than half of it")
    company = "C" * cv_doc.COMPANY_PART_MAX
    title = "T" * cv_doc.TITLE_PART_MAX
    full = cv_doc.saved_name(company, title)
    check(full == f"CV - Luke Keogh - {company} - {title}" and len(full) == cv_doc.NAME_MAX
          and cv_doc.saved_name(company + "C", title + "T") == full,
          f"a company of {cv_doc.COMPANY_PART_MAX} and a title of "
          f"{cv_doc.TITLE_PART_MAX} characters fit whole, {cv_doc.NAME_MAX} in all, "
          "and longer ones are cut to them")
    word = cv_doc.change_word
    check([word({"from": a, "to": b}) for a, b in (
        ("missing", "partly"), ("partly", "met"), ("missing", "met"),
        ("met", "partly"), ("partly", "missing"), ("met", "missing"))]
          == ["better"] * 3 + ["worse"] * 3,
          "every step up from missing to partly to met is better, and every step "
          "down is worse")

    # Records that cannot be read are no record.
    good = {"v": 1, "working": {"asks": []}}
    check(tailored_cv.read_score({"score": json.dumps(good)}) == good
          and tailored_cv.read_score({"score": json.dumps({**good, "v": 2})}) is None
          and tailored_cv.read_score({"score": json.dumps({"v": 1, "working": {}})}) is None
          and tailored_cv.read_score({"score": "not json"}) is None
          and tailored_cv.read_score({"score": "[1]"}) is None,
          "a stored tailored score of another version, with no asks, or not JSON, is "
          "read as none")
    working = json.dumps({"asks": [], "scored_at": "2026-11-12T10:00:00Z"})
    spec = {"score_basis": SPEC, "score_working": working, "scored_at": "2026-11-12T10:00:00Z"}
    check(tailored_cv.master_working(spec) == json.loads(working)
          and tailored_cv.master_working({**spec, "score_basis": "card"}) is None
          and tailored_cv.master_working({**spec, "scored_at": "2026-11-12T11:00:00Z"}) is None
          and tailored_cv.master_working({**spec, "score_working": json.dumps({"asks": 1,
              "scored_at": "2026-11-12T10:00:00Z"})}) is None,
          "the master's working is read only beside the spec score it was made with")

    with fresh() as env:
        a = env.role()
        b = env.role()
        env.open(a)
        first = env.copy(a)
        check(first["created_at"] and first["updated_at"] == first["created_at"],
              "a copy is made with its dates")
        env.rescore(a, COPY_TEXT)
        scored = tailored_cv.read_score(env.copy(a))
        check(env.copy(a)["updated_at"] is not None, "and storing a text dates it")
        other = edited(COPY_TEXT, "Built the software team", "Built and led the team")
        env.rescore(a, other)
        out = env.rescore(b, COPY_TEXT)
        again = tailored_cv.read_score(env.copy(b))
        same = ("cv_match", "tier", "gates", "failed_gates", "working", "one_line_why",
                "prompt_version", "score_model", "scored_at")
        check(out["outcome"] == "reused" and len(CALLS) == 2
              and all(again[k] == scored[k] for k in same),
              "a copy put back, from another advert of the role, finds its score in the "
              "history, with the same match, tier, gates and working, and nothing is spent")
        env.reset_copy(a)
        check(env.copy(a)["updated_at"] is not None, "and Start again dates it too")
        lone = env.role(title="", company="")
        env.open(lone)
        env.rescore(lone, COPY_TEXT)
        env.rescore(lone, other)
        out = env.rescore(lone, COPY_TEXT)
        check(out["outcome"] == "reused" and len(CALLS) == 4,
              "and so does the copy of a row with no role key, from its own history")
        unscored = env.role(title="Unscored", company="Kappa", tier=None, cv_match=None)
        tiered = env.role(title="Tier only", company="Lambda", cv_match=None)
        env.open(unscored)
        env.open(tiered)
        env.rescore(unscored, COPY_TEXT)
        env.rescore(tiered, COPY_TEXT)
        before = tailored_cv.read_score(env.copy(tiered))["before"]
        check(tailored_cv.read_score(env.copy(unscored))["before"] is None
              and before["source"] == "master" and before["tier"] == "top"
              and before["cv_match"] is None,
              "a role with no score at all has nothing before, and one with a tier "
              "has it as before")


def history_checks() -> None:
    """A tailored row in the history is never the role's score to archive_score."""
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        env.rescore(oid, COPY_TEXT)
        conn = radar_common.get_db(env.db)
        try:
            wrote = radar_common.archive_score(conn, oid, "test")
            conn.commit()
        finally:
            conn.close()
        hist = env.history(oid)
        check(wrote and [h["event"] for h in hist] == ["tailored", "replaced"]
              and hist[1]["cv_match"] == 80 and hist[1]["cv_hash"] is None
              and hist[1]["score_basis"] == "card",
              "archive_score keeps the role's own score, comparing it with the role's "
              "last history row and never with a tailored one")
        conn = radar_common.get_db(env.db)
        try:
            again = radar_common.archive_score(conn, oid, "test")
            conn.commit()
        finally:
            conn.close()
        check(not again and len(env.history(oid)) == 2,
              "and keeps it once")
        with fresh() as env2:
            jobs_oid = env2.role()
            env2.open(jobs_oid)
            env2.rescore(jobs_oid, cv_doc.accept(edited(
                MASTER_TEXT, "- SpiraPlan and regulated ALM configuration",
                "- SpiraPlan and regulated ALM configuration\n- Kubernetes and cloud "
                "deployment of clinical systems"))[1])
            saved_args = serve_dashboard.ARGS
            serve_dashboard.ARGS = argparse.Namespace(db=str(env2.db), push=False,
                                                      host="127.0.0.1", port=0)
            try:
                jobs = serve_dashboard.render("jobs").decode("utf-8")
            finally:
                serve_dashboard.ARGS = saved_args
            check('<span class="col-cv">80%</span>' in jobs and "90%" not in jobs,
                  "the Jobs page shows the master CV's score, 80, and not the tailored 90")


# ------------------------------------------------- Re-score stale items

def stale_checks() -> None:
    """Item 10: a CV change cannot quietly swap a full description's score for
    a card's."""
    with fresh() as env:
        old = {"combined": 80, "cv_version": "cv-old.md"}
        spec = env.role(title="Spec role", company="Alpha", score_basis=SPEC,
                        scored_at="2026-11-11T10:00:00Z", **old)
        card = env.role(title="Card role", company="Beta", **old)
        unrecorded = env.role(title="Old role", company="Gamma", score_basis=None, **old)
        env.sql("UPDATE opportunities SET score_basis = NULL WHERE id = ?", (unrecorded,))
        spec_before = opp_row_of(env, spec)
        conn = radar_common.get_db(env.db)
        try:
            fixed, failed, remaining = rescore.rescore_stale_cv(conn, CFG, {}, cap=10)
        finally:
            conn.close()
        check(fixed == 2 and failed == 0 and remaining == 0,
              f"Re-score stale items picks and counts the card rows only ({fixed}, "
              f"{failed}, {remaining})")
        check(opp_row_of(env, spec) == spec_before,
              "and a role scored against its full description is left exactly as it was")
        check(env.row(card)["cv_version"] == MASTER_LABEL[0]
              and env.row(card)["score_basis"] == "card",
              "while a card row is re-scored against the active CV as before")
        conn = radar_common.get_db(env.db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM opportunities WHERE combined IS NOT NULL AND "
                + rescore.NOT_SPEC_SQL).fetchone()[0]
        finally:
            conn.close()
        check(count == 2, "the clause counts a NULL basis as not spec, so an unrecorded "
                          "legacy row is still reached")

    # A row that becomes a spec score between the selection and the write.
    with fresh() as env:
        oid = env.role(title="Racing role", company="Delta", combined=80,
                       cv_version="cv-old.md")
        conn = radar_common.get_db(env.db)
        rows = conn.execute("SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchall()

        def becomes_spec(call):
            env.sql("UPDATE opportunities SET score_basis = 'spec', cv_match = 91,"
                    " scored_at = '2026-11-12T11:00:00Z' WHERE id = ?", (oid,))
            return None
        MODEL["hook"] = becomes_spec
        try:
            fixed, failed = rescore._rescore_rows(conn, CFG, {}, rows)
        finally:
            conn.close()
        row = env.row(oid)
        check(fixed == 0 and row["score_basis"] == SPEC and row["cv_match"] == 91
              and not env.history(oid),
              "a role given a full description's score while the card was being scored "
              "keeps it: the write looks again under the lock and leaves it alone")


# ---------------------------------------------------------------- the Word file

BANNED = {
    "a table": r"<w:tbl[ >]", "a text box": r"txbxContent|<v:textbox|<wps:txbx",
    "a drawing or image": r"<w:drawing|<w:pict|<pic:|<a:blip|<v:imagedata|<v:shape",
    "an embedded object": r"<w:object|<o:OLEObject",
    "alternate content": r"<mc:AlternateContent",
    "a content control": r"<w:sdt[ >]",
    "a field": r"<w:fldSimple|<w:fldChar|<w:instrText",
    "a header or footer reference": r"<w:headerReference|<w:footerReference",
    "a frame": r"<w:framePr", "a tab": r"<w:tab/>", "a line break": r"<w:br/>",
}


def word_parts(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def word_checks() -> None:
    import cv_word
    tricky = cv_doc.normalize({
        "name": "Luke Keogh",
        "headline": ["Software leader & <IVD> \"quotes\" 50%", "Café Ølund · Brighton"],
        "sections": [
            {"title": "Summary", "lines": [{"b": False, "t": "Line one."},
                                           {"b": False, "t": "#1 in regulated ALM, C++ and C#."}],
             "entries": []},
            {"title": "Experience", "lines": [{"b": False, "t": "Section line before entries."}],
             "entries": [{"heading": "Director, A & B Ltd, 2020 to now",
                          "lines": [{"b": False, "t": "Context line in an entry."},
                                    {"b": True, "t": "- dash at the start of a bullet"},
                                    {"b": True, "t": "Bullet with ## inside"}]},
                         {"heading": "## Earlier", "lines": []}]},
            {"title": "Skills", "lines": [{"b": True, "t": "x" * 900}], "entries": []},
            {"title": "Publications", "lines": [{"b": True, "t": "A paper."}], "entries": []},
        ]})
    for label, text in (("the master", MASTER_TEXT), ("an awkward copy", cv_doc.render(tricky))):
        data = cv_word.build(text, "CV - Luke Keogh - Acme - Head")
        parts = word_parts(data)
        read_back = cv_store._docx_to_markdown(data).rstrip("\n").split("\n\n")
        want = [line for line in cv_store.cv_lines(text) if line.strip()]
        check(read_back == want,
              f"{label}: the Word file read back by cv_store's stdlib reader is the saved "
              "copy, every line, in order, headings and bullets marked as they are")
        xml = parts["word/document.xml"].decode("utf-8")
        found = [name for name, pattern in BANNED.items() if re.search(pattern, xml)]
        check(not found, f"{label}: the document holds no {', '.join(BANNED)} ({found})")
        cols = re.findall(r"<w:cols\b[^>]*>", xml)
        check(len(cols) == 1 and 'w:num="' not in cols[0],
              f"{label}: one section and one column ({cols})")
        names = sorted(parts)
        check(not any(re.search(r"header|footer|media/|thumbnail|embeddings|\.(png|jpe?g|gif|emf|wmf|bin)$", n)
                      for n in names),
              f"{label}: the package has no header, footer, image or embedded file ({names})")
        paras = re.findall(r"<w:p>(.*?)</w:p>", xml, re.S)
        styles = [re.search(r'<w:pStyle w:val="(\w+)"/>', p) for p in paras]
        styles = [s.group(1) if s else "Normal" for s in styles]
        expected = []
        for line in want:
            kind, _w = cv_page.line_kind(line)
            expected.append({"h1": "Heading1", "h2": "Heading2", "h3": "Heading3",
                             "bullet": "ListBullet", "plain": "Normal"}[kind])
        check(styles == expected,
              f"{label}: the name is Heading 1, sections Heading 2, entries Heading 3, "
              "bullets List Bullet and the rest Normal")
        bullets = [p for p in paras if 'w:val="ListBullet"' in p]
        check(bullets and all(re.search(r'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="\d+"/></w:numPr>', p)
                              for p in bullets),
              f"{label}: each bullet is a real list paragraph, its numbering on the paragraph")
        numbering = parts["word/numbering.xml"].decode("utf-8")
        num_id = re.search(r'<w:numId w:val="(\d+)"/>', bullets[0]).group(1)
        abstract = re.search(r'<w:num w:numId="' + num_id + r'"[^>]*>\s*<w:abstractNumId w:val="(\d+)"',
                             numbering).group(1)
        block = numbering.split(f'w:abstractNumId="{abstract}"', 1)[1].split("</w:abstractNum>", 1)[0]
        check('<w:numFmt w:val="bullet"/>' in block.split("</w:lvl>", 1)[0],
              f"{label}: and that numbering is a bullet list")
        style_xml = parts["word/styles.xml"].decode("utf-8")
        used = ("Normal", "Heading1", "Heading2", "Heading3", "ListBullet")
        plain = True
        for sid in used:
            body = style_xml.split(f'w:styleId="{sid}"', 1)[1].split("</w:style>", 1)[0]
            fonts = re.search(r"<w:rFonts ([^>]*)/>", body)
            colour = re.search(r'<w:color w:val="(\w+)"', body)
            plain &= bool(fonts) and all(f'w:{a}="{cv_word.FONT}"' in fonts.group(1)
                                         for a in ("ascii", "hAnsi", "cs", "eastAsia"))
            plain &= "Theme" not in fonts.group(1) if fonts else False
            plain &= colour is not None and colour.group(1) == "000000"
        check(plain, f"{label}: every style it uses is one plain font, {cv_word.FONT}, in black")
        core = parts["docProps/core.xml"].decode("utf-8")
        check("<dc:creator>Luke Keogh</dc:creator>" in core
              and "<dc:title>CV - Luke Keogh - Acme - Head</dc:title>" in core
              and "python-docx" not in core,
              f"{label}: the file's properties name the CV and its owner, and nothing of "
              "the machine that made it")
        page = re.search(r'<w:pgSz w:w="(\d+)" w:h="(\d+)"', xml)
        check(page and page.groups() == ("11906", "16838"), f"{label}: the page is A4")
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    pin = re.search(r"^python-docx>=([\d.]+),<([\d.]+)$", req, re.M)
    from importlib.metadata import version
    installed = version("python-docx")
    check(pin and pin.groups() == ("1.2.0", "1.3")
          and installed.startswith("1.2."),
          f"requirements.txt pins python-docx to the 1.2 line, the one this ran with "
          f"({installed})")
    importers = sorted(p.name for p in (REPO_ROOT / "scripts").glob("*.py")
                       if re.search(r"^\s*(import|from)\s+(cv_word|docx)\b",
                                    p.read_text(encoding="utf-8"), re.M))
    check(importers == ["cv_word.py", "serve_dashboard.py"],
          f"only the dashboard imports the Word builder, and only the builder imports "
          f"python-docx ({importers})")
    tree = ast.parse((REPO_ROOT / "scripts" / "serve_dashboard.py").read_text(encoding="utf-8"))
    top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    inside = [f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
              and any(isinstance(n, ast.Import) and any(a.name == "cv_word" for a in n.names)
                      for n in ast.walk(f))]
    check("cv_word" not in top and inside == ["download_cv"],
          "and imports it inside the download, so a dashboard without python-docx still "
          f"serves every page ({inside})")


# ------------------------------------------------------------ the routes

@contextlib.contextmanager
def serving(db, guard=None):
    """The real Handler on a loopback port, the shipped guard installed."""
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD,
             serve_dashboard.Handler.log_message)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False,
                                              host="127.0.0.1", port=0)
    serve_dashboard.GUARD = guard or serve_dashboard.WriteGuard(
        "enforce", (*serve_dashboard.LOOPBACK_NAMES, PROXY))
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD,
         serve_dashboard.Handler.log_message) = saved


def page_headers(port) -> dict:
    own = f"127.0.0.1:{port}"
    return {"Host": own, "Origin": f"http://{own}", HEADER: "1",
            "Content-Type": "application/json"}


def call(port, method, path, body=b"", headers=None, timeout=30):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body or None, headers=headers or {})
        resp = conn.getresponse()
        return (resp.status, {k.lower(): v for k, v in resp.getheaders()},
                resp.read())
    except (OSError, http.client.HTTPException) as err:
        note(f"{method} {path[:40]} failed with {type(err).__name__}")
        return 0, {}, b""
    finally:
        conn.close()


def post(port, path, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    status, got, data = call(port, "POST", path, body, headers or page_headers(port))
    try:
        parsed = json.loads(data or b"{}")
    except ValueError:
        parsed = {}
    return status, parsed, got


ROUTES = ("/prepare/cv/open", "/prepare/cv/save", "/prepare/cv/reset",
          "/prepare/cv/rescore")


def route_checks() -> None:
    with fresh() as env:
        oid = env.role()
        applied = env.role(title="Applied", company="Alpha")
        env.apply(applied)
        with serving(env.db) as port, captured_stderr():
            own = f"127.0.0.1:{port}"
            dumped = env.dump()
            refused = []
            for path in ROUTES + ("/cv/master/save",):
                body = {"id": oid, "text": MASTER_TEXT}
                for label, headers in (
                        ("no header", {"Host": own, "Content-Type": "application/json"}),
                        ("a foreign Origin", {**page_headers(port), "Origin": "https://evil.example"}),
                        ("a foreign Host", {**page_headers(port), "Host": "evil.example"})):
                    status, _got, _h = post(port, path, body, headers)
                    if status != 403:
                        refused.append(f"{path} {label} {status}")
            check(not refused and env.dump() == dumped and not CALLS,
                  f"every new write is refused without the header, from another Origin or "
                  f"by another Host, and nothing runs ({refused[:3]})")
            bad = []
            for path in ROUTES:
                for label, payload in (("missing", {}), ("a string", {"id": "1"}),
                                       ("true", {"id": True}), ("a float", {"id": 1.5}),
                                       ("too large", {"id": 2 ** 70})):
                    status, got, _h = post(port, path, {**payload, "text": MASTER_TEXT})
                    if status != 400:
                        bad.append(f"{path} {label} {status}")
            for path in ("/prepare/cv/save", "/prepare/cv/rescore"):
                for payload in ({"id": oid}, {"id": oid, "text": 5}, {"id": oid, "text": None}):
                    status, got, _h = post(port, path, payload)
                    if status != 400:
                        bad.append(f"{path} {payload} {status}")
            status, got, _h = post(port, "/prepare/cv/rescore", b"{not json")
            if status != 400:
                bad.append(f"not json {status}")
            check(not bad and env.dump() == dumped and not CALLS,
                  f"an id that is not a real integer, or a CV that is not text, is a 400 "
                  f"and writes nothing ({bad[:3]})")
            status, got, _h = post(port, "/prepare/cv/rescore", {"id": oid, "text": "## Skills\n- x\n"})
            check(status == 400 and got.get("invalid") and "no name" in got.get("note", ""),
                  "a CV that cannot be taken is a 400 with the reason")
            status, got, _h = post(port, "/prepare/cv/open", {"id": oid})
            check(status == 200 and got["outcome"] == "made" and "html" not in got
                  and got["copy"]["lines"] == cv_store.cv_lines(MASTER_TEXT),
                  "open answers 200 with the copy, and leaves the right hand side as it was "
                  "drawn, which the Prepare application click may be filling")
            status, got, _h = post(port, "/prepare/cv/rescore", {"id": oid, "text": COPY_TEXT})
            check(status == 200 and got["outcome"] == "scored" and len(CALLS) == 1
                  and "Tailored copy" in got["html"] and got["tailored"]["cv_match"] == 80,
                  "Rescore role over HTTP is one call, and answers with the new right hand side")
            status, got, _h = post(port, "/prepare/cv/save", {"id": oid, "text": MASTER_TEXT})
            check(status == 200 and got["saved_name"] == "CV - Luke Keogh - Veltrix Diagnostics - Head of Software",
                  "Save CV over HTTP saves and names the copy")
            dumped = env.dump()
            for path in ROUTES:
                status, got, _h = post(port, path, {"id": applied, "text": MASTER_TEXT})
                if not (status == 200 and got.get("outcome") == "applied"):
                    bad.append(f"{path} applied {status} {got.get('outcome')}")
            check(not bad and env.dump() == dumped and len(CALLS) == 1,
                  "for an applied role every one of them answers that it is applied, and "
                  "nothing is written or called")
            status, got, _h = post(port, "/prepare/cv/open", {"id": 99999})
            check(status == 409 and "no opportunity 99999" in got.get("note", ""),
                  "a role that does not exist is a 409 that says so")
            edges = []
            for value, want in ((2 ** 63 - 1, 409), (-2 ** 63, 409), (2 ** 63, 400),
                                (-2 ** 63 - 1, 400)):
                status, got, _h = post(port, "/prepare/cv/reset", {"id": value})
                if status != want:
                    edges.append(f"{value} {status}")
            check(not edges, f"an id SQLite can hold is looked for, and one past either "
                             f"end is a 400 ({edges})")
            status, got, page = call(port, "GET", f"/prepare/{oid}", headers={"Host": own})
            check(status == 200 and b'id="cv-editor"' in page
                  and got.get("content-type", "").startswith("text/html"),
                  "the Prepare page itself is still the page, not a download")
            import cv_word as _cw
            saved_build = _cw.build

            def broken_build(text, title=""):
                raise ValueError("a template part is missing")
            _cw.build = broken_build
            try:
                status, _got, data = call(port, "GET", f"/prepare/{oid}/cv.docx", headers={"Host": own})
            finally:
                _cw.build = saved_build
            check(status == 500 and b"could not be made" in data and b"ValueError" in data,
                  "a Word file that cannot be built is a 500 that says why")
            saved_action = serve_dashboard.COPY_ACTIONS["save"]

            def locked(*_a, **_k):
                raise sqlite3.OperationalError("database is locked")
            serve_dashboard.COPY_ACTIONS["save"] = locked
            try:
                status, got, _h = post(port, "/prepare/cv/save", {"id": oid, "text": MASTER_TEXT})
            finally:
                serve_dashboard.COPY_ACTIONS["save"] = saved_action
            check(status == 409 and got.get("ok") is False
                  and "database busy" in got.get("note", ""),
                  "a database that stays locked is a polite refusal, as on the other routes")

            # The download. The copy is edited past its save first, so that the
            # file can only be the saved copy if it reads as the saved copy.
            conn = radar_common.get_db(env.db)
            try:
                tailored_cv.store_text(conn, env.copy(oid)["role_ref"], COPY_TEXT)
                conn.commit()
            finally:
                conn.close()
            check(env.copy(oid)["cv_text"] == COPY_TEXT != env.copy(oid)["saved_text"],
                  "the copy has been edited since it was saved")
            dumped = env.dump()
            status, got, data = call(port, "GET", f"/prepare/{oid}/cv.docx", headers={"Host": own})
            check(status == 200 and got.get("content-type") == cv_page_mime()
                  and data[:2] == b"PK" and env.dump() == dumped,
                  "Download is a Word file and writes nothing")
            disposition = got.get("content-disposition", "")
            check(disposition == ('attachment; filename="CV - Luke Keogh - Veltrix Diagnostics - '
                                  'Head of Software.docx"; filename*=UTF-8\'\'CV%20-%20Luke%20Keogh'
                                  '%20-%20Veltrix%20Diagnostics%20-%20Head%20of%20Software.docx')
                  and got.get("x-frame-options") == "DENY" and got.get("cache-control") == "no-store",
                  "named by the saved copy, not cached, and not to be framed")
            read = cv_store._docx_to_markdown(data).rstrip("\n").split("\n\n")
            check(read == [l for l in cv_store.cv_lines(MASTER_TEXT) if l.strip()]
                  and read != [l for l in cv_store.cv_lines(COPY_TEXT) if l.strip()],
                  "and it is the saved copy, not the copy as edited since")
            status, _got, data = call(port, "GET", f"/prepare/{oid}/cv.docx",
                                      headers={"Host": "evil.example"})
            check(status == 403, "Download is behind the same Host check as every page")
            other = env.role(title="Unsaved", company="Beta")
            for path, want in ((f"/prepare/{other}/cv.docx", 404), ("/prepare/99999/cv.docx", 404),
                               ("/prepare/abc/cv.docx", 404), ("/prepare/1x/cv.docx", 404),
                               ("/prepare/%31/cv.docx", 404)):
                status, _got, data = call(port, "GET", path, headers={"Host": own})
                if status != want:
                    bad.append(f"{path} {status}")
            check(not bad, f"a role with no saved copy, no such role and an id that is not "
                           f"digits are each a 404 ({bad[:3]})")
            status, _got, data = call(port, "GET", f"/prepare/{applied}/cv.docx", headers={"Host": own})
            check(status == 404 and b"No CV has been saved" in data,
                  "an applied role with no saved copy says so")
            unicode_role = env.role(title="Ingénieur logiciel / QA", company="Ølund Médical")
            env.save(unicode_role, MASTER_TEXT)
            status, got, data = call(port, "GET", f"/prepare/{unicode_role}/cv.docx",
                                     headers={"Host": own})
            disposition = got.get("content-disposition", "")
            plain = re.search(r'filename="([^"]*)"', disposition)
            check(status == 200 and plain and all(32 <= ord(c) < 127 for c in plain.group(1))
                  and "filename*=UTF-8''CV%20-%20Luke%20Keogh%20-%20%C3%98lund%20M%C3%A9dical"
                      "%20-%20Ing%C3%A9nieur%20logiciel%20QA.docx" in disposition,
                  f"a name with letters outside ASCII is sent as itself, with a plain "
                  f"fallback ({disposition})")
            import cv_word
            real = cv_word._docx

            def missing():
                raise cv_word.WordUnavailable("python-docx is not installed")
            cv_word._docx = missing
            try:
                status, _got, data = call(port, "GET", f"/prepare/{oid}/cv.docx", headers={"Host": own})
            finally:
                cv_word._docx = real
            check(status == 503 and b"python-docx is not installed" in data,
                  "a dashboard without python-docx says so, with a 503, and serves every "
                  "other page as before")


def cv_page_mime() -> str:
    import cv_word
    return cv_word.MIME


# --------------------------------------------------------------- Applied

def applied_checks() -> None:
    with fresh() as env:
        saved_role = env.role()
        repost = env.role()
        bare = env.role(title="Bare", company="Alpha")
        named = env.role(title="Named", company="Beta")
        env.save(saved_role, MASTER_TEXT)
        with serving(env.db) as port, captured_stderr():
            status, got, _h = post(port, "/apply", {"id": repost})
            check(status == 200 and got == {"ok": True, "id": repost, "applied": True}
                  and env.row(repost)["applied_cv"] == "CV - Luke Keogh - Veltrix Diagnostics - Head of Software",
                  "Applied records the role's saved copy by its name, from whichever advert "
                  "of the role it is pressed on, and answers as it always did")
            status, got, _h = post(port, "/apply", {"id": bare})
            check(status == 200 and env.row(bare)["applied_cv"] is None,
                  "and records nothing where no copy was saved")
            before = env.dump()
            status, got, _h = post(port, "/apply", {"id": named, "cv": "bad\x07bell"})
            check(status == 400 and env.dump() == before,
                  "a reference a request names is still checked, and junk is a 400 that "
                  "writes nothing, as Brief 5 had it")
            status, got, _h = post(port, "/apply", {"id": named, "cv": "cv-2026-11-12.pdf"})
            check(status == 200 and env.row(named)["applied_at"]
                  and env.row(named)["applied_cv"] is None,
                  "but what it names is not recorded: no copy was saved, so the column is "
                  "left empty (item 9)")
            post(port, "/unapply", {"id": repost})
            row = env.row(repost)
            check(row["applied_at"] is None and row["applied_cv"] is None,
                  "Undo clears the date and the name, as it always did")
            post(port, "/unapply", {"id": named})
            env.save(named, MASTER_TEXT)
            status, got, _h = post(port, "/apply", {"id": named, "cv": "something else"})
            check(env.row(named)["applied_cv"] == "CV - Luke Keogh - Beta - Named",
                  "and where a copy was saved, its name is what is recorded")
            status, got, _h = post(port, "/prepare/cv/save", {"id": named, "text": MASTER_TEXT})
            check(status == 200 and got["outcome"] == "applied",
                  "after Applied, Save CV is refused for the role")


# -------------------------------------------------------- the pages

def page_text(html: str) -> str:
    import html as htmllib
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html)))


def render_page(env, oid) -> str:
    c = env.conn()
    try:
        return build_dashboard.render_prepare_page(c, oid, CFG, "test.sqlite")
    finally:
        c.close()


def render_inner(env, oid) -> str:
    c = env.conn()
    try:
        return build_dashboard.render_prepare_inner(c, oid, CFG)
    finally:
        c.close()


def pane_data(page: str) -> dict:
    import html as htmllib
    found = re.search(r'<div id="cv-editor"[^>]*data-cv="([^"]*)"', page)
    return json.loads(htmllib.unescape(found.group(1))) if found else {}


def page_checks() -> None:
    with fresh() as env:
        oid = env.role()
        before = env.dump()
        page = render_page(env, oid)
        for _ in range(4):
            render_page(env, oid)
        check(env.dump() == before and not CALLS,
              "opening the page, however often, writes nothing and calls nothing, "
              "even for a role with no copy yet")
        left, right = page.index('id="cv-pane"'), page.index('id="prepare-body"')
        pane = page[left:right]
        check('<div class="prep-split">' in page and '<section class="prep-left" id="cv-pane"' in page
              and '<div class="prep-right">' in page and left < right
              and "</section>" in pane and 'id="cv-editor"' in pane,
              "the page is a split view, the CV on the left and the role, its score, its "
              "asks and the description on the right, the editor outside the part a "
              "click swaps")
        css = build_dashboard.PREPARE_CSS
        check("@media (max-width:1100px){.prep-split{grid-template-columns:minmax(0,1fr)}" in css
              and ".prep-split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)" in css,
              "two columns side by side, one under the other on a narrow screen")
        data = pane_data(page)
        check(data.get("mode") == "tailored" and data.get("id") == oid and data["editable"]
              and data["needsCopy"] and data["lines"] == cv_store.cv_lines(MASTER_TEXT)
              and cv_doc.render(data["doc"]) == MASTER_TEXT,
              "a role with no copy yet shows the master, and tells its script to make the "
              "copy from it")
        check(re.findall(r'data-cv-act="(\w+)" disabled', pane) == ["rescore", "save", "reset", "apply"]
              and re.search(r'<a class="act-btn" id="cv-download" href="/prepare/\d+/cv.docx" download hidden>', pane)
              and "This is the master CV." in page_text(pane),
              "it has Rescore role, Save CV, Start again from master and Applied, off until "
              "the script runs, and no Download before a save")
        rows = re.findall(r'data-n="(\d+)"', pane)
        check(rows == [str(n) for n in range(1, len(cv_store.cv_lines(MASTER_TEXT)) + 1)],
              "every line of the CV is on the page with its number")
        inner = render_inner(env, oid)
        check("Luke Keogh" not in inner and "Chichester" not in inner,
              "the right hand side holds no line of the CV, the CV is on the left")
        check('<a href="https://www.linkedin.com/jobs/view/' + env.row(oid)["source_url"].rsplit("/", 1)[1] + '/"'
              in inner and "Open the advert on LinkedIn" in inner,
              "the role's LinkedIn advert is linked by its job page (C11)")
        check("No tailored copy yet." in page_text(inner),
              "the tailored score says there is no copy yet")

        env.open(oid)
        env.rescore(oid, COPY_TEXT)
        page = render_page(env, oid)
        inner = render_inner(env, oid)
        text = page_text(inner)
        check("Master CV" in text and "Tailored copy" in text
              and "Scored against the full description and this copy" in text
              and "CV match 80, Top prospects." in text
              and "Before this rescore, on the master CV: CV match 80, Top prospects." in text,
              "the tailored score sits beside the master's, with what it was made on and "
              "the score before it")
        check(text.count("✓ Met") == 3 and "◐ Partly" in text and "✗ Missing" in text
              and "CV lines as numbered in the tailored copy as it was scored" in text,
              "the asks drawn are the tailored score's, each with its mark")
        asks = re.findall(r'<li class="ask" data-ask data-ask-status="(\w+)" tabindex="0">', inner)
        cited = re.findall(r'<li data-cv-n="(\d+)" data-cv-text="([^"]*)">', inner)
        lines = cv_store.cv_lines(COPY_TEXT)
        import html as htmllib
        check(asks == ["met", "partly", "met", "missing", "met"]
              and cited and all(lines[int(n) - 1].strip() == htmllib.unescape(t) for n, t in cited),
              "each ask carries its status, and each cited line its number and its text "
              "as the working stored them, which is what the script lights up by")
        check(not re.search(r"<li[^>]*\bdata-status=", inner)
              and inner.count("data-ask-status=") == 5,
              "an ask's status is in data-ask-status, so the Prepare application script, "
              "which looks for data-status, can only ever find its Working notice")
        li = build_dashboard._cited_li({"n": 3, "text": "Short start",
                                        "whole": "Short start and the rest", "now": 3})
        check('data-cv-text="Short start and the rest"' in li and "</b> Short start</li>" in li,
              "a long cited line shows the start the working kept, and hands the script "
              "the whole line to find it by")
        data = pane_data(page)
        check(not data["needsCopy"] and data["lines"] == lines,
              "with a copy, the page shows the copy and asks for nothing")
        left = page[page.index('id="cv-pane"'):page.index('id="prepare-body"')]
        check(f"Made from the master CV {MASTER_LABEL[0]}" in page_text(left)
              and data["hash"] == cv_store.cv_hash(COPY_TEXT) and data["saved"] is None,
              "and says which master it was made from, and hands its script the "
              "version of the copy it shows")
        env.save(oid, MASTER_TEXT)
        conn = radar_common.get_db(env.db)
        try:
            tailored_cv.store_text(conn, env.copy(oid)["role_ref"], COPY_TEXT)
            conn.commit()
        finally:
            conn.close()
        data = pane_data(render_page(env, oid))
        check(data["lines"] == lines and data["saved"]["same"] is False
              and data["saved"]["name"] == "CV - Luke Keogh - Veltrix Diagnostics - Head of Software",
              "an open role shows the copy as it is now, not the copy saved before, and "
              "tells its script the two differ")
        env.save(oid, COPY_TEXT)
        check(pane_data(render_page(env, oid))["saved"]["same"] is True,
              "and that they are the same once it is saved again")

        # The copy edited since the score.
        risk = "- Own the software risk file under ISO 14971."
        conn = radar_common.get_db(env.db)
        try:
            tailored_cv.store_text(conn, env.copy(oid)["role_ref"],
                                   edited(COPY_TEXT, risk, "- Own the risk file."))
            conn.commit()
        finally:
            conn.close()
        inner = render_inner(env, oid)
        text = page_text(inner)
        n = lines.index(risk) + 1
        check("The copy has been edited since this score." in text
              and "! Stale, was met" in text and f"CV line {n} when scored, since changed" in text
              and 'class="cv-gone"' in inner and "1 stale because a line it cited has been edited" in text,
              "a copy edited since its score marks the ask whose cited line is gone as "
              "stale and not met, and says the copy has changed")
        check("2 met, 1 partly, 1 missing, 1 stale because a line it cited has been edited" in text,
              "and counts it as stale, not as met")

    with fresh() as env:
        # The master as scored stands for the copy, and a score made elsewhere.
        oid = env.role()
        env.prepare(oid)
        env.open(oid)
        text = page_text(render_inner(env, oid))
        check("The copy is the master CV as it was scored against this description" in text,
              "a copy that is the master unchanged shows the master's score standing for it")
        env.sql("UPDATE opportunities SET description_text = ? WHERE id = ?",
                (DESCRIPTION + "\n- RADAR something else", oid))
        conn = radar_common.get_db(env.db)
        try:
            tailored_cv.set_score(conn, env.copy(oid)["role_ref"], {
                "v": 1, "description_hash": "000000000000", "cv_hash": "x",
                "scored_at": "2026-11-11T10:00:00Z", "working": {"asks": []}})
            conn.commit()
        finally:
            conn.close()
        text = page_text(render_inner(env, oid))
        check("Last scored against another advert’s description, 11 Nov." in text,
              "a tailored score made on another advert's description is said to be so")

        # Changes are tagged on the asks they moved.
        oid2 = env.role(title="Second", company="Beta Diagnostics")
        env.open(oid2)
        env.rescore(oid2, COPY_TEXT)
        better = cv_doc.accept(edited(COPY_TEXT, "- SpiraPlan and regulated ALM configuration",
                                      "- SpiraPlan and regulated ALM configuration\n"
                                      "- Kubernetes and cloud deployment of clinical systems"))[1]
        env.rescore(oid2, better)
        inner = render_inner(env, oid2)
        check('<span class="ask-was">was missing</span>' in inner
              and "Now met, was missing." in page_text(inner) and "1 ask changed." in page_text(inner),
              "an ask that moved carries what it was, and the score lists what changed")
        card = page_text(inner[inner.index('id="tailored-score"'):inner.index("The rate line")])
        rate = page_text(inner[inner.index("The rate line"):inner.index("The asks")])
        check("Sector Passes." in card and "CV match Passes." in card
              and "Location Passes." in card and "Rate Passes." in card
              and "Competitive salary" in rate and "Not read yet" not in rate
              and "CV lines as numbered in the tailored copy as it was scored" in page_text(inner),
              "the tailored score shows its own four gates, the pay line read from its "
              "own working, and says its asks are numbered in the copy it scored")
        same_asks = cv_doc.accept(edited(better, "Chichester, West Sussex, UK",
                                         "Chichester, West Sussex, United Kingdom"))[1]
        env.rescore(oid2, same_asks)
        check("No ask changed." in page_text(render_inner(env, oid2)),
              "and a rescore that moves no ask says so")
        conn = radar_common.get_db(env.db)
        try:
            tailored_cv.store_text(conn, env.copy(oid2)["role_ref"],
                                   same_asks.replace("## Summary\n", "## Summary\nA new first line.\n", 1))
            conn.commit()
        finally:
            conn.close()
        moved_text = page_text(render_inner(env, oid2))
        risk_n = cv_store.cv_lines(same_asks).index("- Own the software risk file under ISO 14971.") + 1
        check(f"CV line {risk_n + 1} ({risk_n} when scored)" in moved_text,
              "a cited line pushed down by a line added above is shown where it is now, and "
              "where it was when scored")
    with fresh() as env:
        oid = env.role()
        env.prepare(oid)
        check(f"CV lines as numbered in {MASTER_LABEL[0]}" in page_text(render_inner(env, oid)),
              "the master's own asks say which version of the master numbered them")

    with fresh() as env:
        # LinkedIn from a repost, and none at all.
        reed = env.role(title="Head of QA", company="Gamma Diagnostics",
                        source="reed-alert", source_url="https://www.reed.co.uk/jobs/head-of-qa/1")
        linked = env.role(title="Head of QA", company="Gamma Diagnostics", jid=4470000123)
        lone = env.role(title="Lone", company="Delta", source="reed-alert",
                        source_url="https://www.reed.co.uk/jobs/lone/2")
        check('href="https://www.linkedin.com/jobs/view/4470000123/"' in render_inner(env, reed),
              "a role whose advert is on another board links the LinkedIn advert of its repost")
        check("No LinkedIn advert is stored for this role." in page_text(render_inner(env, lone)),
              "and one with none says so")
        del linked

    with fresh() as env:
        # An applied role: the saved copy, read only.
        oid = env.role()
        saved = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
        env.save(oid, saved)
        env.open(oid)
        env.rescore(oid, COPY_TEXT)
        env.apply(oid, "CV - Luke Keogh - Veltrix Diagnostics - Head of Software")
        before = env.dump()
        page = render_page(env, oid)
        left = page[page.index('id="cv-pane"'):page.index('id="prepare-body"')]
        data = pane_data(page)
        check("This role is marked applied with CV - Luke Keogh - Veltrix Diagnostics - Head of Software."
              in page_text(left) and "as it was sent" in page_text(left)
              and data["lines"] == cv_store.cv_lines(saved) and data["editable"] is False
              and "data-cv-act" not in left and "<textarea" not in left
              and re.search(r'<a class="act-btn" id="cv-download" href="/prepare/\d+/cv.docx" download>Download</a>', left),
              "an applied role shows the copy that was saved and sent, read only, with "
              "Download and no button that would change anything")
        check(env.dump() == before, "and drawing it writes nothing")
        right = page_text(render_inner(env, oid))
        check("The copy saved and sent was not scored on its own against this "
              "description." in right and "edited since" not in right
              and "stale" not in right and "Rescore role" not in right,
              "beside it, a score made later on an unsaved text is not shown as the sent "
              "copy's, and nothing offers a Rescore role the page does not have")
        # Roles that can no longer change, by what was saved for them.
        closed = env.role(title="Closed later", company="Zeta Diagnostics")
        env.open(closed)
        env.rescore(closed, COPY_TEXT)
        env.save(closed, COPY_TEXT)
        env.sql("UPDATE opportunities SET acknowledged_at = '2026-11-12T12:00:00Z' WHERE id = ?",
                (closed,))
        unsent = env.role(title="Never saved", company="Eta Diagnostics")
        env.open(unsent)
        env.rescore(unsent, COPY_TEXT)
        env.apply(unsent)
        reset_only = env.role(title="Reset only", company="Theta Diagnostics")
        env.open(reset_only)
        env.apply(reset_only)
        words = [page_text(render_inner(env, r)) for r in (closed, unsent, reset_only)]
        check("Scored against the full description and the copy saved for this role" in words[0]
              and "saved and sent" not in words[0]
              and "Scored against the full description and the copy as it was left, never saved"
              in words[1] and "saved and sent" not in words[1]
              and "The copy as it was left, never saved was not scored on its own" in words[2],
              "a role closed with a copy saved, one applied with nothing saved, and one "
              "applied with an unscored copy each say which copy the score beside it is, "
              "and none says a copy was sent that was not")
        acked = env.role(title="Acked", company="Epsilon", acknowledged_at="2026-11-11T09:00:00Z")
        page = render_page(env, acked)
        left = page[page.index('id="cv-pane"'):page.index('id="prepare-body"')]
        check("This role is not open" in page_text(left) and "data-cv-act" not in left
              and "No tailored CV was made for this role." in page_text(left),
              "a role that is not open gets no copy and no buttons")

    with fresh() as env:
        # Scored, saved, then scored again on a later text that was never saved,
        # then applied: the page shows the sent copy beside the sent copy's score.
        oid = env.role()
        env.open(oid)
        env.rescore(oid, COPY_TEXT)
        env.save(oid, COPY_TEXT)
        later = cv_doc.accept(edited(COPY_TEXT, "- SpiraPlan and regulated ALM configuration",
                                     "- SpiraPlan and regulated ALM configuration\n"
                                     "- Kubernetes and cloud deployment of clinical systems"))[1]
        env.rescore(oid, later)
        env.apply(oid, "CV - Luke Keogh - Veltrix Diagnostics - Head of Software")
        inner = render_inner(env, oid)
        right = page_text(inner)
        check(len(CALLS) == 2
              and "Scored against the full description and the copy saved and sent" in right
              and "CV match 80, Top prospects." in right and "CV match 90" not in right
              and "edited since" not in right and "Before this rescore" not in right
              and "stale" not in right and 'class="ask ask-stale"' not in inner
              and "Kubernetes and cloud deployment of clinical systems" in right
              and "✗ Missing" in right,
              "an applied role shows the sent copy's own score, from the history, with its "
              "own asks, not the score of a text made later and never sent")

    with fresh() as env:
        # Escaping, whoever wrote the words.
        MASTER[0] = cv_doc.render(cv_doc.normalize({**MASTER_DOC, "headline": [
            "<script>alert(1)</script> & \"quotes\" 'single'"]}))
        oid = env.role(title="<b>Boss</b>", company="A&B <i>Ltd</i>")
        page = render_page(env, oid)
        check("<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
              and "<b>Boss</b>" not in page and "&lt;b&gt;Boss&lt;/b&gt;" in page
              and pane_data(page)["lines"][1].startswith("<script>alert(1)</script>"),
              "the CV, the title and the company are escaped on the page and in what the "
              "script is handed, and the script gets them back exactly")


def master_page_checks() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="radar-cv-profile-"))
    saved_dir = cv_store.PROFILE_DIR
    real_load = score_item.load_cv_text
    real_version = score_item.get_cv_version
    cv_store.PROFILE_DIR = tmp
    score_item.PROFILE_DIR = tmp

    def from_profile(config):
        name = cv_store.active_cv_name(tmp)
        return (tmp / name).read_text(encoding="utf-8"), f"config/profile/{name}"
    score_item.load_cv_text = from_profile
    score_item.get_cv_version = lambda config: cv_store.active_cv_name(tmp) or "cv.txt"
    try:
        legacy = "Luke Keogh\nChichester\n\nPROFILE\nSoftware leader.\n\nSKILLS\n* IEC 62304\n"
        (tmp / "cv-20261101-080000.md").write_text(legacy, encoding="utf-8")
        (tmp / "cv.active").write_text("cv-20261101-080000.md\n", encoding="utf-8")
        reset()
        with fresh() as env:
            page = serve_dashboard_page(env)
            text = page_text(page)
            data = pane_data(page)
            check("<h1>CV</h1>" in page and data.get("mode") == "master"
                  and data["version"] == "cv-20261101-080000.md" and data["editable"]
                  and 'data-cv-act="master" disabled' in page
                  and "saved 1 Nov 2026 at 08:00 UTC" in text,
                  "the CV page holds the master in the editor, with its version and the "
                  "date it was saved (C1, C2)")
            check([s["title"] for s in data["doc"]["sections"]] == list(cv_doc.STANDARD_SECTIONS)
                  and data["doc"]["sections"][0]["lines"] == [{"b": False, "t": "Software leader."}]
                  and data["doc"]["sections"][2]["lines"] == [{"b": True, "t": "IEC 62304"}],
                  "with sections for summary, experience, skills, and education and "
                  "certifications, filled from the version that is active")
            check("This version was not written by this editor" in text,
                  "a version imported from Word says it is shown in the editor's layout "
                  "and is read as it is until it is saved")
            heads = re.findall(r"<h2>([^<]+)</h2>", page)
            check(heads == ["Master CV", "History", "One-off import from a Word file",
                            "After a change"]
                  and 'id="cv-file" accept=".md,.markdown,.txt,.docx,.pdf"' in page
                  and "For bringing the master in from a Word file once." in text
                  and "cv-20261101-080000.md — active" in text,
                  "the upload is relabelled as the one-off import from a Word file and still "
                  "works as it did, beside the history")
            check("A role scored against its full description is left alone" in text,
                  "and the re-score after a change says it leaves full description scores alone")

            with serving(env.db) as port, captured_stderr():
                doc = data["doc"]
                doc["name"] = "Luke Keogh"
                text_v2 = cv_doc.render(cv_doc.normalize(doc))
                status, got, _h = post(port, "/cv/master/save", {"text": text_v2, "base": "cv-old.md"})
                check(status == 409 and "has changed since this page was opened" in got.get("note", "")
                      and sorted(p.name for p in tmp.iterdir()) == ["cv-20261101-080000.md", "cv.active"],
                      "a save from a page opened on another version is refused and writes nothing")
                status, got, _h = post(port, "/cv/master/save",
                                       {"text": text_v2, "base": "cv-20261101-080000.md"})
                names = sorted(p.name for p in tmp.iterdir())
                active = cv_store.active_cv_name(tmp)
                check(status == 200 and got["saved"] and got["version"] == active
                      and active != "cv-20261101-080000.md" and len(names) == 3
                      and (tmp / active).read_text(encoding="utf-8") == text_v2
                      and (tmp / "cv-20261101-080000.md").read_text(encoding="utf-8") == legacy
                      and not any(n.startswith(".cv.active") for n in names),
                      "a save makes a new dated version in the editor's layout, points the "
                      "scorer at it, keeps the old one as it was, and leaves no stray file")
                status, got, _h = post(port, "/cv/master/save", {"text": text_v2, "base": active})
                check(status == 200 and got == {"ok": True, "saved": False, "version": active}
                      and len(list(tmp.iterdir())) == 3,
                      "saving the same text again writes nothing")
                status, got, _h = post(port, "/cv/master/save", {"text": "## Skills\n- x\n", "base": active})
                check(status == 409 and "no name" in got.get("note", "") and len(list(tmp.iterdir())) == 3,
                      "a master that cannot be taken is refused with the reason")
                for payload in ({}, {"text": 5}, {"text": None}):
                    status, got, _h = post(port, "/cv/master/save", payload)
                    if status != 400:
                        check(False, f"a body of {payload} is a 400")
                        break
                else:
                    check(True, "a body with no CV text is a 400")
                status, got, _h = post(port, "/cv/master/save", {"text": text_v2, "base": 7})
                check(status == 409 and "not text" in got.get("note", ""),
                      "a base that is not text is refused")
            page = serve_dashboard_page(env)
            check("This version was not written by this editor" not in page_text(page),
                  "once saved in the editor's layout the page no longer says it was not")
            # An import as big as the upload allows, which no CV could be.
            (tmp / "cv-20261113-080000.md").write_text(
                "Luke Keogh\n" + "A line.\n" * (cv_doc.RAW_LINES_MAX + 10), encoding="utf-8")
            (tmp / "cv.active").write_text("cv-20261113-080000.md\n", encoding="utf-8")
            start = time.time()
            page = serve_dashboard_page(env)
            took = time.time() - start
            check("The master CV could not be read." in page_text(page)
                  and f"no CV needs more than {cv_doc.RAW_LINES_MAX:,}" in page_text(page)
                  and 'id="cv-editor"' not in page and took < 5,
                  "a master too big for any CV is refused before it is read, and the page "
                  f"says why instead of laying it out ({took:.2f} s)")
    finally:
        cv_store.PROFILE_DIR = saved_dir
        score_item.PROFILE_DIR = saved_dir
        score_item.load_cv_text = real_load
        score_item.get_cv_version = real_version
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------- the script, under node

NODE_EDITOR = r"""
const vm = require('vm');
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let bad = 0;
const say = (ok, label) => {
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label);
  if (!ok) bad += 1;
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
              rsquo: '’', ldquo: '“', rdquo: '”', bull: '•', mdash: '—' };
function decode(s) {
  return String(s).replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') return String.fromCodePoint(e[1] === 'x' || e[1] === 'X'
      ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
    return ENT[e.toLowerCase()] !== undefined ? ENT[e.toLowerCase()] : m;
  });
}
function compound(s) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = re.exec(s))) {
    if (m[1]) out.tag = m[1].toLowerCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else out.attrs.push([m[4], m[5] === undefined ? null : m[5]]);
  }
  return out;
}
function matchOne(el, c) {
  if (!el || !el.tagName) return false;
  if (c.tag && el.tagName.toLowerCase() !== c.tag) return false;
  if (c.id && el.getAttribute('id') !== c.id) return false;
  for (const k of c.classes) if (!el.classList.contains(k)) return false;
  for (const [n, v] of c.attrs) {
    const a = el.getAttribute(n);
    if (a === null || (v !== null && a !== v)) return false;
  }
  return true;
}
function matchChain(el, sel) {
  const parts = sel.trim().split(/\s+/).map(compound);
  if (!matchOne(el, parts[parts.length - 1])) return false;
  let i = parts.length - 2;
  let cur = el.parentNode;
  while (i >= 0 && cur) { if (matchOne(cur, parts[i])) i -= 1; cur = cur.parentNode; }
  return i < 0;
}
const matches = (el, sel) => sel.split(',').some((s) => matchChain(el, s));
function world(html) {
  const events = [];
  const calls = [];
  const pending = [];
  const timers = [];
  const confirms = [];
  let confirmAnswer = true;
  const observers = [];
  class Text { constructor(t) { this.nodeType = 3; this._t = t; this.parentNode = null; }
    get textContent() { return this._t; } set textContent(v) { this._t = String(v); } }
  class El {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.nodeType = 1; this.attrs = {}; this.children = [];
      this.parentNode = null; this.listeners = {}; this.style = {}; this._text = '';
      this.value = ''; this.disabled = false; this.hidden = false; this.rows = 2;
      const me = this;
      this.classList = {
        contains: (k) => (me.attrs['class'] || '').split(/\s+/).includes(k),
        add: (k) => { if (!me.classList.contains(k)) me.attrs['class'] = ((me.attrs['class'] || '') + ' ' + k).trim(); },
        remove: (k) => { me.attrs['class'] = (me.attrs['class'] || '').split(/\s+/).filter((x) => x && x !== k).join(' '); },
        toggle: (k, on) => { const want = on === undefined ? !me.classList.contains(k) : !!on;
          if (want) me.classList.add(k); else me.classList.remove(k); return want; },
      };
    }
    setAttribute(n, v) { this.attrs[n] = String(v); if (n === 'hidden') this.hidden = true;
      if (n === 'disabled') this.disabled = true; }
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null; }
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n); }
    removeAttribute(n) { delete this.attrs[n]; }
    get className() { return this.attrs['class'] || ''; }
    set className(v) { this.attrs['class'] = String(v); }
    get title() { return this.attrs.title || ''; } set title(v) { this.attrs.title = String(v); }
    get type() { return this.attrs.type || ''; } set type(v) { this.attrs.type = String(v); }
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
    insertBefore(c, ref) { c.parentNode = this; const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
    get firstChild() { return this.children[0] || null; }
    get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text; }
    set textContent(v) {
      // As Chrome and Edge do: a focused field that is taken away is told it
      // lost focus first, while it is still in the page.
      const active = document.activeElement;
      if (active && active !== this && this.contains(active)) {
        document.activeElement = null;
        fire(active, 'focusout');
      }
      this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v);
    }
    set innerHTML(h) { this.textContent = ''; parse(String(h), this);
      observers.forEach((o) => { if (o.target === this) o.fn([]); }); }
    walk(fn) { for (const c of this.children) { if (c.nodeType === 1) { fn(c); c.walk(fn); } } }
    querySelectorAll(sel) { const out = []; this.walk((el) => { if (matches(el, sel)) out.push(el); }); return out; }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    closest(sel) { let cur = this; while (cur && cur.nodeType === 1) { if (matches(cur, sel)) return cur; cur = cur.parentNode; } return null; }
    contains(node) { let cur = node; while (cur) { if (cur === this) return true; cur = cur.parentNode; } return false; }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    focus() { document.activeElement = this; }
    setSelectionRange(a) { this.selectionStart = this.selectionEnd = a; }
    scrollIntoView() { events.push('scroll:' + (this.getAttribute('data-place') || this.getAttribute('data-n'))); }
    get scrollHeight() { return 20; }
    get clientWidth() { return 600; }
  }
  const document = new El('#document');
  document.documentElement = new El('html');
  document.createElement = (t) => new El(t);
  document.createTextNode = (t) => new Text(t);
  document.getElementById = (id) => document.querySelector('#' + id);
  const VOID = new Set(['br', 'img', 'input', 'meta', 'link', 'hr', 'col', 'source', 'wbr']);
  const RAW = new Set(['script', 'style', 'textarea', 'noscript', 'title']);
  function parse(h, root) {
    const stack = [root];
    const re = /<!--[\s\S]*?-->|<![^>]*>|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/g;
    let m;
    let last = 0;
    const text = (t) => { if (t) stack[stack.length - 1].appendChild(new Text(decode(t))); };
    while ((m = re.exec(h))) {
      text(h.slice(last, m.index));
      last = re.lastIndex;
      if (m[0][1] === '!') continue;
      if (m[1]) {
        const tag = m[1].toLowerCase();
        for (let k = stack.length - 1; k > 0; k -= 1) {
          if (stack[k].tagName.toLowerCase() === tag) { stack.length = k; break; }
        }
        continue;
      }
      const tag = m[2].toLowerCase();
      const el = new El(tag);
      const are = /([^\s=>\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = are.exec(m[3]))) {
        const v = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4] !== undefined ? a[4] : '';
        el.setAttribute(a[1], decode(v));
      }
      stack[stack.length - 1].appendChild(el);
      if (RAW.has(tag)) {
        const end = h.toLowerCase().indexOf('</' + tag, last);
        const inner = h.slice(last, end < 0 ? h.length : end);
        if (tag === 'textarea') el.value = decode(inner);
        re.lastIndex = end < 0 ? h.length : h.indexOf('>', end) + 1;
        last = re.lastIndex;
        continue;
      }
      if (!VOID.has(tag) && !m[4]) stack.push(el);
    }
    text(h.slice(last));
  }
  const body = new El('body');
  document.appendChild(body);
  parse(html, body);
  const windowListeners = {};
  const sandbox = {
    Headers, JSON, Array, Object, String, Number, Promise, Math, Error,
    document, localStorage: { getItem() { return null; }, setItem() {} },
    location: { pathname: '/prepare/7', reload() { events.push('reload'); } },
    setTimeout() { timers.push('timeout'); }, setInterval() { timers.push('interval'); },
    confirm(q) { confirms.push(q); return confirmAnswer; },
    alert() { events.push('alert'); },
    MutationObserver: class { constructor(fn) { this.fn = fn; }
      observe(target) { observers.push({ target, fn: this.fn }); } },
    ResizeObserver: class { constructor(fn) { this.fn = fn; } observe() {} },
    addEventListener(t, fn) { (windowListeners[t] = windowListeners[t] || []).push(fn); },
  };
  sandbox.window = sandbox;
  sandbox.fetch = (url, opts) => {
    calls.push({ url, opts });
    return new Promise((resolve, reject) => { pending.push({ resolve, reject }); });
  };
  vm.createContext(sandbox);
  vm.runInContext(data.scripts, sandbox);
  function fire(target, type, extra) {
    const ev = Object.assign({ type, target, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; } }, extra || {});
    let cur = target;
    while (cur) { (cur.listeners && cur.listeners[type] || []).forEach((fn) => fn(ev)); cur = cur.parentNode; }
    (windowListeners[type] || []).forEach((fn) => fn(ev));
    return ev;
  }
  const q = (sel) => document.querySelector(sel);
  const qa = (sel) => document.querySelectorAll(sel);
  return { document, sandbox, events, calls, pending, timers, confirms, q, qa, fire,
           setConfirm(v) { confirmAnswer = v; },
           click(el) { return fire(el, 'click'); },
           answer(value) { pending.shift().resolve({ json: () => Promise.resolve(value) }); },
           answerAt(i, value) { pending.splice(i, 1)[0].resolve({ json: () => Promise.resolve(value) }); },
           fail() { pending.shift().reject(new Error('network')); },
           sayText() { const n = document.getElementById('cv-say'); return n ? n.textContent : null; },
           sayBad() { const n = document.getElementById('cv-say'); return n ? n.classList.contains('bad') : null; },
           RadarCv: sandbox.RadarCv };
}
const body = (w, i) => JSON.parse(w.calls[i].opts.body);
const numbered = (w) => w.qa('#cv-editor .cvl[data-n]').map((r) => [Number(r.getAttribute('data-n')),
  r.querySelector('.cvl-n').textContent]);
(async () => {
  if (typeof Headers === 'undefined') {
    console.log('NOTE  this node has no Headers, so the editor script was not run');
    process.exit(0);
  }
  // ---------------------------------------------------------- the model
  const M = world('<div></div>').RadarCv;
  let same = 0;
  data.parity.docs.forEach((c) => { if (JSON.stringify(M.layout(c.doc).lines) === JSON.stringify(c.lines)) same += 1; });
  say(same === data.parity.docs.length, `the script lays out ${data.parity.docs.length} CVs, tidy and untidy, to the server's very lines (${same})`);
  let words = 0;
  data.parity.strings.forEach((c) => {
    if (M.clean(c.s) === c.clean && M.cleanPlain(c.s, false) === c.plain && M.key(c.s) === c.key
        && JSON.stringify(M.line({ b: false, t: c.s })) === JSON.stringify(c.line)) words += 1;
  });
  say(words === data.parity.strings.length, `it cleans, tidies and keys ${data.parity.strings.length} awkward strings as the server does (${words})`);
  let found = 0;
  data.parity.finds.forEach((c) => { if (M.findLine(c.cited, c.keys) === c.now) found += 1; });
  say(found === data.parity.finds.length, `and finds cited lines again where the server does (${found} of ${data.parity.finds.length})`);
  say(M.text(data.parity.master.doc) === data.parity.master.text, 'the text it sends is the text the server stores');

  // ----------------------------------------------- opened with no copy
  let w = world(data.pages.open);
  await settle();
  const sent = w.calls[0] && w.calls[0].opts;
  say(w.calls.length === 1 && w.calls[0].url === '/prepare/cv/open' && sent.method === 'POST'
      && body(w, 0).id === data.ids.open && sent.headers instanceof Headers
      && sent.headers.get(data.header) === '1',
      'a page with no copy makes one POST, to make the copy, through the fetch wrapper with the guard header');
  const acts = () => w.qa('[data-cv-act]');
  say(acts().length === 4 && acts().every((b) => b.disabled) && w.qa('#cv-editor textarea').every((t) => t.disabled),
      'and while it is out nothing can be pressed and nothing typed');
  w.answer(data.answers.open);
  await settle(); await settle();
  say(acts().every((b) => !b.disabled) && w.qa('#cv-editor textarea').every((t) => !t.disabled),
      'once the copy is made the buttons and the lines are given back');
  say(JSON.stringify(numbered(w).map((p) => p[0])) === JSON.stringify(data.lines.master.map((_l, i) => i + 1))
      && numbered(w).every((p) => String(p[0]) === p[1]),
      'every line is numbered, and the number shown is the number of the line in the text');
  say(w.q('#cv-status').textContent.includes('Made from the master CV ' + data.version),
      'and the page says which master version the copy was made from');

  // ----------------------------------------------------- Rescore role
  const rescore = w.q('[data-cv-act="rescore"]');
  w.click(rescore); w.click(rescore);
  await settle();
  say(w.calls.length === 2 && w.calls[1].url === '/prepare/cv/rescore' && body(w, 1).id === data.ids.open
      && body(w, 1).text === data.texts.master,
      'Rescore role sends one POST with the CV on screen as text, and a second click while it is out sends nothing');
  say(body(w, 0).base === undefined && body(w, 1).base === data.hashes.open,
      'and the version of the copy the page took from the server, so a copy changed in another tab is not written over');
  say(rescore.textContent === 'Working' && rescore.disabled, 'its button says it is working');
  const rowsOut = w.qa('#cv-editor .cvl').length;
  w.click(w.q('#cv-editor [data-add="bullet"][data-at="s2"]'));
  say(w.qa('#cv-editor .cvl').length === rowsOut, 'while a press is out, the editing buttons do nothing');
  w.answer(data.answers.rescore);
  await settle(); await settle();
  say(w.sayText() === 'Rescored. CV match 80 to 90. 1 ask changed.' && !w.sayBad()
      && rescore.textContent === 'Rescore role' && !rescore.disabled,
      'the answer says the score before and after and how many asks changed, and gives the button back');
  say(w.qa('#prepare-body [data-ask]').length === 5 && w.q('#prepare-body .ask-was')
      && w.q('#prepare-body .ask-was').textContent === 'was missing',
      'and the new score and asks are swapped in on the right');
  say(w.calls.length === 2 && w.timers.length === 0, 'and nothing else is sent, and no timer is set');
  const counts = () => w.q('#prepare-body [data-ask-counts]').textContent;
  const editedNote = () => w.q('#copy-edited-note');
  say(counts() === data.summaries.master && editedNote() && editedNote().hidden,
      'the counts over the asks are counted for the copy on screen, in the server\'s own words, and the score is not said to be out of date');

  // ----------------------------------------------------------- editing
  const risk = () => w.q('textarea[data-edit="' + data.places.risk + '"]');
  const riskAsk = () => w.qa('#prepare-body [data-ask]').find((a) => a.textContent.includes('ISO 14971 risk'));
  say(riskAsk() && !riskAsk().classList.contains('ask-stale'), 'the risk ask starts met');
  const was = risk().value;
  risk().value = 'Own the risk file.';
  w.fire(risk(), 'input');
  const label = riskAsk().querySelector('[data-cv-n="' + data.lines.riskN + '"] b');
  say(riskAsk().classList.contains('ask-stale') && riskAsk().querySelector('.ask-status').textContent === '! Stale, was met'
      && label.textContent === 'CV line ' + data.lines.riskN + ' when scored, since changed',
      'editing a cited line away marks its ask stale, not met, as it is typed');
  say(counts() === data.summaries.risk && !editedNote().hidden,
      'and at once the counts count it as stale, as the server would, and the score card says the copy was edited since');
  risk().value = was;
  w.fire(risk(), 'input');
  say(!riskAsk().classList.contains('ask-stale') && riskAsk().querySelector('.ask-status').textContent === '✓ Met'
      && label.textContent === 'CV line ' + data.lines.riskN && counts() === data.summaries.master,
      'and putting it back makes it met again, and counted as met');
  const above = w.q('textarea[data-edit="' + data.places.above + '"]');
  const whole = above.value;
  above.selectionStart = above.selectionEnd = 5;
  const enter = w.fire(above, 'keydown', { key: 'Enter' });
  await settle();
  const moved = riskAsk().querySelector('[data-cv-n="' + data.lines.riskN + '"] b').textContent;
  say(enter.defaultPrevented && moved === 'CV line ' + (data.lines.riskN + 1) + ' (' + data.lines.riskN + ' when scored)'
      && !riskAsk().classList.contains('ask-stale'),
      'Enter splits a line in two, the lines below move down one, and a cited line pushed down is found by its text, one further on');
  const firstHalf = w.q('textarea[data-edit="' + data.places.above + '"]');
  const secondHalf = w.q('textarea[data-edit="' + data.places.splitNext + '"]');
  say(firstHalf.value === whole.slice(0, 5) && secondHalf.value === whole.slice(5)
      && w.q('[data-place="' + data.places.splitNext + '"]').classList.contains('cvl-bullet')
      && w.document.activeElement === secondHalf && secondHalf.selectionStart === 0,
      'the line keeps the words before the cursor, a new bullet below takes the words after it, and the cursor goes to its start');
  say(JSON.stringify(numbered(w).map((p) => p[0])) === JSON.stringify(data.lines.master.concat(['x']).map((_l, i) => i + 1)),
      'and the numbers stay the numbers of the text that would be sent');
  const halves = w.q('textarea[data-edit="' + data.places.splitNext + '"]');
  halves.value = '';
  w.fire(halves, 'input');
  const num = (p) => { const r = w.q('[data-place="' + p + '"]'); return r ? r.querySelector('.cvl-n').textContent : null; };
  say(num(data.places.splitNext) === '' && num('s1.e0.l3') === String(data.lines.riskN)
      && w.q('[data-place="' + data.places.splitNext + '"]').getAttribute('data-n') === null,
      'a line emptied by typing loses its number at once, and the lines below take theirs back');
  const back = w.fire(halves, 'keydown', { key: 'Backspace' });
  await settle();
  const before = w.q('textarea[data-edit="' + data.places.above + '"]');
  say(back.defaultPrevented && numbered(w).length === data.lines.master.length
      && w.q('textarea[data-edit="' + data.places.splitNext + '"]').value === 'Built the software team from nothing.'
      && w.document.activeElement === before && before.selectionStart === before.value.length,
      'Backspace in an empty line removes it, the line below moves up, and the cursor goes to the end of the line above');
  w.fire(w.q('textarea[data-edit="' + data.places.above + '"]'), 'focusout');
  const add = w.q('#cv-editor [data-add="bullet"][data-at="' + data.places.skills + '"]');
  w.click(add);
  const fresh = w.q('textarea[data-edit="' + data.places.newSkill + '"]');
  say(fresh && fresh.parentNode.getAttribute('data-n') === null && numbered(w).length === data.lines.master.length,
      'a new empty bullet has no number until it has words, so no number moves while it is empty');
  fresh.value = 'Kubernetes on AWS';
  w.fire(fresh, 'input');
  say(fresh.parentNode.getAttribute('data-n') === String(data.lines.newSkillN),
      'and takes its number when it does');
  const paste = w.q('textarea[data-edit="' + data.places.newSkill + '"]');
  paste.value = 'One\nTwo';
  w.fire(paste, 'input');
  say(w.q('textarea[data-edit="' + data.places.newSkill + '"]').value === 'One'
      && w.q('textarea[data-edit="' + data.places.newSkillNext + '"]').value === 'Two',
      'text pasted with a line break becomes two lines');
  const plain = w.q('textarea[data-edit="' + data.places.summaryLine + '"]');
  plain.value = '- now a bullet';
  w.fire(plain, 'input');
  w.fire(plain, 'focusout');
  say(w.q('[data-place="' + data.places.summaryLine + '"]').classList.contains('cvl-bullet'),
      'a paragraph line that starts with a dash becomes a bullet when it is left');

  // ------------------------------------------------------- highlighting
  w.fire(riskAsk(), 'mouseover');
  const lit = w.qa('#cv-editor .cvl.cv-hl').map((r) => r.getAttribute('data-n'));
  const cites = riskAsk().querySelectorAll('[data-now]').map((li) => li.getAttribute('data-now')).filter(Boolean);
  say(lit.length === cites.length && cites.every((n) => lit.includes(n))
      && w.q('[data-place="' + data.places.risk + '"]').classList.contains('cv-hl')
      && riskAsk().classList.contains('ask-hl'),
      'hovering an ask lights the CV lines it cites, and only those');
  w.fire(w.q('[data-place="' + data.places.risk + '"]'), 'mouseover');
  const asksLit = w.qa('#prepare-body .ask-hl').map((a) => a.textContent);
  say(asksLit.length === 1 && asksLit[0].includes('ISO 14971 risk')
      && w.qa('#cv-editor .cvl.cv-hl').map((r) => r.getAttribute('data-place')).join() === data.places.risk,
      'hovering a CV line lights that line and the asks that cite it, and only those');
  w.fire(riskAsk().querySelector('[data-cv-text]'), 'click');
  say(w.events.includes('scroll:' + data.places.risk) && w.events.filter((e) => e.startsWith('scroll')).length === 1,
      'a click on a cited line brings that line, and no other, into view');

  // ---------------------------------------------------- Save CV, refused
  const ev = w.fire(w.sandbox, 'beforeunload');
  say(ev.defaultPrevented && ev.returnValue === '', 'leaving the page with edits not stored asks first');
  const save = w.q('[data-cv-act="save"]');
  w.click(save);
  await settle();
  const sentText = body(w, 2).text;
  say(w.calls.length === 3 && w.calls[2].url === '/prepare/cv/save' && typeof sentText === 'string'
      && sentText.includes('\n- Lead\n') && !sentText.includes('software for a Class C IVD')
      && sentText.includes('\n- One\n- Two\n') && sentText.includes('\n- now a bullet\n')
      && sentText.includes('\n- Own the software risk file under ISO 14971.\n'),
      'Save CV sends one POST with the copy as it is on screen, every edit in it');
  w.answer({ ok: false, note: 'The write guard refused this request.' });
  await settle(); await settle();
  say(w.sayText() === 'The write guard refused this request.' && w.sayBad() && !save.disabled,
      'a refusal shows its reason and gives the button back');
  const linesBefore = numbered(w).length;
  w.click(save);
  await settle();
  say(body(w, w.calls.length - 1).base === data.hashes.rescored,
      'Save CV sends the version of the copy it took, the one the rescore handed back');
  w.answer({ ok: true, outcome: 'copy_moved', notices: [{ kind: 'info', text: data.copyMoved }] });
  await settle(); await settle();
  say(w.sayText() === data.copyMoved && w.sayBad() && numbered(w).length === linesBefore && !save.disabled,
      'a copy changed in another tab since is said to be, in red, and the copy on screen is kept as it is');
  w.click(save);
  await settle();
  w.fail();
  await settle(); await settle();
  say(/No usable answer came back/.test(w.sayText()) && !save.disabled,
      'a request that fails says so and gives the button back');
  w.click(save);
  await settle();
  w.answer(data.answers.save);
  await settle(); await settle();
  say(w.sayText() === 'Saved as ' + data.savedName + '.' && !w.q('#cv-download').hidden
      && w.q('#cv-saved').textContent === 'Saved as ' + data.savedName + '.',
      'a save says the name it was saved under and shows Download');
  const ev2 = w.fire(w.sandbox, 'beforeunload');
  say(!ev2.defaultPrevented, 'and once stored, leaving asks nothing');

  // ------------------------------------------ edits after the save
  const headline = w.q('textarea[data-edit="h.1"]');
  say(w.q('#cv-download-hint').textContent === '', 'with nothing edited since the save, Download needs no word');
  headline.value = '• West Sussex';
  w.fire(headline, 'input');
  say(/Save CV again/.test(w.q('#cv-download-hint').textContent),
      'the moment the copy is edited after a save, the page says Download gives the copy as it was saved');
  w.fire(headline, 'focusout');
  say(w.q('textarea[data-edit="h.1"]').value === 'West Sussex' && w.q('[data-place="h.1"]').classList.contains('cvl-plain'),
      'a headline line pasted with a bullet mark loses the mark and stays a line');
  const first = w.q('textarea[data-edit="s0.l0"]');
  first.value = '▪ Pasted from Word';
  w.fire(first, 'input');
  w.fire(first, 'focusout');
  say(w.q('[data-place="s0.l0"]').classList.contains('cvl-bullet')
      && w.q('textarea[data-edit="s0.l0"]').value === 'Pasted from Word',
      'a line pasted with a bullet mark from Word becomes a bullet, without the mark');
  const lone = w.q('textarea[data-edit="s1.e1.l0"]');
  lone.value = '';
  w.fire(lone, 'input');
  w.fire(lone, 'keydown', { key: 'Backspace' });
  w.q('#cv-editor').listeners.focusout.forEach((fn) => fn({ type: 'focusout', target: lone }));
  w.click(w.q('#cv-editor [data-add="bullet"][data-at="s2"]'));
  say(!w.q('[data-place="s1.e1.l0"]') && w.q('[data-place="s1.e1.h"]'),
      'a field a redraw took away that then says it lost focus changes nothing, and leaves no empty line behind');
  const riskField = w.q('textarea[data-edit="' + data.places.risk + '"]');
  riskField.value = 'Own the risk file.';
  w.fire(riskField, 'input');
  w.q('#prepare-body').innerHTML = data.answers.rescore.html;
  say(riskAsk().classList.contains('ask-stale') && riskAsk().querySelector('.ask-status').textContent === '! Stale, was met'
      && !editedNote().hidden,
      'a right side swapped in by the Prepare application script is marked against the copy on screen as well, and its score card says the copy was edited since');
  riskField.value = 'Own the software risk file under ISO 14971.';
  w.fire(riskField, 'input');

  // ----------------------------------------- Start again and Applied
  const calls = w.calls.length;
  w.setConfirm(false);
  w.click(w.q('[data-cv-act="reset"]'));
  w.click(w.q('[data-cv-act="apply"]'));
  await settle();
  say(w.calls.length === calls && w.confirms.length === 2
      && /not the copy saved as/.test(w.confirms[1]) && w.confirms[1].includes(data.savedName),
      'Start again asks first, and so does Applied while the copy on screen is not the copy saved, naming it, and on a no neither sends anything');
  w.setConfirm(true);
  w.click(w.q('[data-cv-act="apply"]'));
  await settle();
  say(w.calls.length === calls + 1 && w.calls[calls].url === '/apply', 'on a yes Applied goes');
  w.answer({ ok: true, id: data.ids.open, applied: true });
  await settle(); await settle();
  say(w.events.includes('reload'), 'and the page reloads to show the role applied');
  say(w.timers.length === 0, 'no timer anywhere in it');

  // ------------------------------------------------- other pages
  w = world(data.pages.applied);
  await settle();
  say(w.calls.length === 0 && w.qa('[data-cv-act]').length === 0 && w.qa('#cv-editor textarea').length === 0
      && w.qa('#cv-editor .cvl[data-n]').length === data.lines.saved.length,
      'an applied role\'s page sends nothing and has nothing to edit or press, only its saved copy');
  w = world(data.pages.mismatch);
  await settle();
  say(w.calls.length === 0 && w.qa('#cv-editor textarea').length === 0
      && w.qa('[data-cv-act]').every((b) => b.disabled) && /could not line up/.test(w.sayText()),
      'a CV whose numbers the script cannot match to the server\'s is shown read only, with no button, and says why');
  say(JSON.stringify(numbered(w).map((p) => p[0])) === JSON.stringify(data.lines.master.map((_l, i) => i + 1))
      && numbered(w).every((p) => String(p[0]) === p[1])
      && w.qa('#cv-editor .cvl').every((r) => r.getAttribute('data-place') === null),
      'and the rows it shows are the ones the server drew and numbered, not redrawn by the script');
  w = world(data.pages.unsaved);
  await settle();
  w.setConfirm(false);
  w.click(w.q('[data-cv-act="apply"]'));
  await settle();
  say(w.confirms.length === 1 && w.calls.length === 0,
      'Applied with no saved copy asks first, and sends nothing when the answer is no');
  w = world(data.pages.master);
  await settle();
  say(w.calls.length === 0, 'the CV page sends nothing when it opens');
  const masterSave = w.q('[data-cv-act="master"]');
  const masterLine = w.q('textarea[data-edit="s0.l0"]');
  const masterWas = masterLine.value;
  masterLine.value = 'Edited on the CV page.';
  w.fire(masterLine, 'input');
  const leaveMaster = w.fire(w.sandbox, 'beforeunload');
  w.click(masterSave);
  await settle();
  say(leaveMaster.defaultPrevented && w.calls.length === 1 && w.calls[0].url === '/cv/master/save'
      && body(w, 0).base === data.version
      && body(w, 0).text === data.texts.masterPage.replace(masterWas, 'Edited on the CV page.'),
      'its Save as a new version sends the master as it is on screen, the edit in it, and the version the page was opened on');
  w.answer({ ok: true, saved: true, version: 'cv-new.md' });
  await settle(); await settle();
  const leaveSaved = w.fire(w.sandbox, 'beforeunload');
  say(w.events.includes('reload') && !leaveSaved.defaultPrevented,
      'and reloads to show the new version, without asking about the edit it has just stored');

  w = world(data.pages.differs);
  await settle();
  say(w.calls.length === 0 && /Save CV again/.test(w.q('#cv-download-hint').textContent) && !w.q('#cv-download').hidden,
      'a page whose copy was edited after its save says at once that Download gives the save');
  w.setConfirm(false);
  w.click(w.q('[data-cv-act="apply"]'));
  await settle();
  say(w.confirms.length === 1 && /not the copy saved as/.test(w.confirms[0]) && w.calls.length === 0,
      'and Applied there asks first, though nothing was typed, since the copy is not the one saved');

  // ----------------------------------------------- the editing buttons
  w = world(data.pages.ops);
  await settle();
  say(w.calls.length === 0, 'a role that has a copy sends nothing when its page opens');
  const val = (p) => { const t = w.q('textarea[data-edit="' + p + '"]'); return t ? t.value : null; };
  const opAt = (p, kind) => { const b = w.q('[data-op="' + kind + '"][data-at="' + p + '"]'); if (b) w.click(b); return !!b; };
  const rowAt = (p) => w.q('[data-place="' + p + '"]');
  say(rowAt('s2.l0').querySelector('.cvl-mark').textContent === '•' && rowAt('s0.l0').querySelector('.cvl-mark').textContent === ''
      && rowAt('s0.l0').querySelector('.cvl-n').getAttribute('aria-hidden') === 'true'
      && w.q('textarea[data-edit="name"]').getAttribute('aria-label') === 'Name'
      && w.q('textarea[data-edit="h.0"]').getAttribute('aria-label') === 'Headline line'
      && w.q('textarea[data-edit="s0.l0"]').getAttribute('aria-label') === 'Line'
      && w.q('textarea[data-edit="s2.l0"]').getAttribute('aria-label') === 'Bullet'
      && w.q('textarea[data-edit="s1.e0.h"]').getAttribute('aria-label') === 'Entry heading'
      && w.q('textarea[data-edit="s4.t"]').getAttribute('aria-label') === 'Section title',
      'a bullet shows a dot and a line none, the numbers are hidden from screen readers, and every field is labelled');
  const nameField = w.q('textarea[data-edit="name"]');
  nameField.value = 'Luke Keogh';
  w.fire(nameField, 'input');
  say(nameField.style.height === '22px', 'a field grows to fit what is in it');
  opAt('s1.e0.l1', 'up');
  say(val('s1.e0.l0') === 'Built the software team from nothing.' && val('s1.e0.l1').startsWith('Lead software'),
      'Move up swaps a line with the one above');
  opAt('s1.e0.l0', 'up');
  opAt('s1.e0.l2', 'down');
  say(val('s1.e0.l0') === 'Built the software team from nothing.' && val('s1.e0.l2') === 'Own the software risk file under ISO 14971.',
      'and the first line does not move up, nor the last down');
  opAt('s1.e0.l0', 'down');
  say(val('s1.e0.l0').startsWith('Lead software') && val('s1.e0.l1') === 'Built the software team from nothing.',
      'Move down swaps a line with the one below');
  opAt('s1.e0.l1', 'kind');
  say(rowAt('s1.e0.l1').classList.contains('cvl-plain') && val('s1.e0.l1') === 'Built the software team from nothing.',
      'the bullet button makes a bullet a paragraph line, keeping its words');
  opAt('s1.e0.l1', 'kind');
  say(rowAt('s1.e0.l1').classList.contains('cvl-bullet'), 'and back');
  opAt('s1.e0.l2', 'del');
  say(val('s1.e0.l2') === null && numbered(w).length === data.opsLines - 1, 'Delete removes the line, and the lines are numbered again');
  say(!w.q('[data-op="kind"][data-at="h.0"]') && w.q('[data-op="up"][data-at="h.0"]'), 'a headline line can move but has no bullet button');
  opAt('s1.e1.h', 'eup');
  say(val('s1.e0.h').includes('miDiagnostics') && val('s1.e0.l0') === 'Oversaw FDA clearance for a Class B IVD.'
      && val('s1.e1.h').includes('Axithra'),
      'an entry moves up past the one above, taking its lines with it');
  opAt('s1.e0.h', 'eup');
  opAt('s1.e1.h', 'edown');
  say(val('s1.e0.h').includes('miDiagnostics') && val('s1.e1.h').includes('Axithra'),
      'and the first entry does not move up, nor the last down');
  opAt('s1.e0.h', 'edown');
  say(val('s1.e0.h').includes('Axithra') && val('s1.e1.h').includes('miDiagnostics'), 'an entry moves down');
  w.setConfirm(false);
  opAt('s1.e1.h', 'edel');
  say(val('s1.e1.h') !== null && w.confirms.length === 1 && /Delete this entry and its 1 line\?/.test(w.confirms[0]),
      'deleting an entry with lines asks first, saying how many, and keeps it on a no');
  w.setConfirm(true);
  opAt('s1.e1.h', 'edel');
  say(val('s1.e1.h') === null && w.confirms.length === 2, 'and deletes it on a yes');
  w.click(w.q('#cv-editor [data-add="entry"][data-at="s1"]'));
  say(w.document.activeElement === w.q('textarea[data-edit="s1.e1.h"]') && rowAt('s1.e1.l0').classList.contains('cvl-bullet'),
      '+ Entry adds an entry with an empty bullet, and puts the cursor on its heading');
  opAt('s1.e1.h', 'edel');
  say(val('s1.e1.h') === null && w.confirms.length === 2, 'an entry with no words is deleted without asking');
  w.click(w.q('#cv-editor [data-add="line"][data-at="s1.e0"]'));
  say(rowAt('s1.e0.l2').classList.contains('cvl-plain') && w.document.activeElement === w.q('textarea[data-edit="s1.e0.l2"]'),
      '+ Line adds a paragraph line to an entry, with the cursor in it');
  w.click(w.q('#cv-editor [data-add="bullet"][data-at="s1.e0"]'));
  say(rowAt('s1.e0.l3').classList.contains('cvl-bullet'), '+ Bullet adds a bullet to it');
  w.click(w.q('#cv-editor [data-add="line"][data-at="h"]'));
  const hl = w.q('textarea[data-edit="h.2"]');
  say(hl && hl.value === '' && w.document.activeElement === hl, '+ Headline line adds an empty headline line, with the cursor in it');
  hl.value = 'One\nTwo';
  w.fire(hl, 'input');
  say(val('h.2') === 'One' && val('h.3') === 'Two' && w.document.activeElement === w.q('textarea[data-edit="h.3"]'),
      'a paste with a line break in the headline becomes two headline lines, the cursor on the last');
  say(!w.q('textarea[data-edit="s0.t"]') && !w.q('[data-op="delsec"][data-at="s0.t"]')
      && w.q('textarea[data-edit="s4.t"]') && w.q('[data-op="delsec"][data-at="s4.t"]'),
      'a standard section\'s title cannot be edited or deleted, and another section\'s can');
  w.setConfirm(false);
  opAt('s4.t', 'delsec');
  say(w.q('textarea[data-edit="s4.t"]') && w.confirms.length === 3, 'deleting a section asks first and keeps it on a no');
  w.setConfirm(true);
  opAt('s4.t', 'delsec');
  say(!w.q('textarea[data-edit="s4.t"]'), 'and deletes it on a yes');
  const back0 = w.q('textarea[data-edit="s0.l0"]');
  back0.value = '';
  w.fire(back0, 'input');
  w.document.activeElement = null;
  w.fire(back0, 'keydown', { key: 'Backspace' });
  say(val('s0.l0').startsWith('Took a Class B IVD') && w.document.activeElement === null,
      'Backspace in an empty first line removes it and moves the cursor nowhere');

  // ------------------------------------------------- the other answers
  const rescoreOps = w.q('[data-cv-act="rescore"]');
  w.click(rescoreOps);
  await settle();
  say(w.sayText() === data.doing.rescore && !w.sayBad(), 'while Rescore role is out the page says what it is doing, not in red');
  const opsSent = body(w, w.calls.length - 1).text;
  say(w.calls[w.calls.length - 1].url === '/prepare/cv/rescore' && opsSent.includes('\nOne\nTwo\n')
      && !opsSent.includes('## Patents') && !opsSent.includes('Software leader in regulated')
      && !opsSent.includes('miDiagnostics') && !opsSent.includes('Own the software risk file')
      && opsSent.includes('- Lead software for a Class C IVD under IEC 62304.\n- Built the software team from nothing.\n'),
      'Rescore role sends the copy as it is on screen after the editing buttons, every edit in it');
  w.answer({ ok: true, outcome: 'reused', notices: [{ kind: 'info', text: 'Scored before, nothing was spent.' }] });
  await settle(); await settle();
  say(w.sayText() === 'Scored before, nothing was spent.' && !w.sayBad(), 'a press that spent nothing says what the server said');
  w.click(rescoreOps);
  await settle();
  w.answer({ ok: true, outcome: 'scored', notices: [], tailored: { cv_match: 90, tier: 'top', before_cv_match: null, changes: 0 } });
  await settle(); await settle();
  say(w.sayText() === 'Rescored. CV match 90.', 'a first score with nothing before it says only the new match');
  w.click(rescoreOps);
  await settle();
  w.answer({ ok: true, outcome: 'cooling', notices: [{ kind: 'score', text: 'Not tried again yet.' }] });
  await settle(); await settle();
  say(w.sayText() === 'Not tried again yet.' && w.sayBad(), 'a press held back by a recent failure says so in red');
  w.click(w.q('[data-cv-act="reset"]'));
  await settle();
  w.answer({ ok: true, outcome: 'reset', notices: [], copy: Object.assign({}, data.answers.open.copy, { master_version: 'cv-new.md' }) });
  await settle(); await settle();
  say(w.sayText() === 'Done.' && w.q('#cv-status').textContent.includes('Made from the master CV cv-new.md')
      && val('s1.e0.l2') === 'Own the software risk file under ISO 14971.',
      'Start again puts the master back on screen, says it is done, and names the master it came from');

  // ----------------------- a field a redraw takes away, in two browsers
  // A paste leaves "- x" a paragraph line until it is left, and the line
  // removed before it names the place it then has.
  const pasteAt = w.q('textarea[data-edit="s0.l0"]');
  pasteAt.value = 'a\n- x\nb';
  w.fire(pasteAt, 'input');
  const gone0 = w.q('textarea[data-edit="s0.l0"]');
  gone0.focus();
  gone0.value = '';
  w.fire(gone0, 'input');
  w.fire(gone0, 'keydown', { key: 'Backspace' });
  say(rowAt('s0.l0').classList.contains('cvl-plain') && val('s0.l0') === '- x',
      'as Chrome does, a field told it lost focus while the redraw takes it away touches no line, not even the one that now has its place');
  w.q('#cv-editor').listeners.focusout.forEach((fn) => fn({ type: 'focusout', target: gone0 }));
  say(rowAt('s0.l0').classList.contains('cvl-plain') && val('s0.l0') === '- x',
      'and a browser that tells it after the field is gone does no more');

  // ------------------------------------------- opened from the Jobs row
  w = world(data.pages.go);
  await settle();
  say(w.calls.length === 2 && w.calls[0].url === '/prepare' && body(w, 0).id === data.go.id
      && w.calls[1].url === '/prepare/cv/open' && body(w, 1).id === data.go.id,
      'a page opened from its Jobs row with work to do makes the Prepare application POST and the copy\'s, one each');
  w.answer({ ok: true, outcome: 'scored', html: data.go.html });
  await settle(); await settle();
  w.answer({ ok: true, outcome: 'made', copy: data.go.copy, notices: [] });
  await settle(); await settle();
  const goCited = w.qa('#prepare-body [data-cv-text]');
  say(goCited.length > 0 && goCited.every((li) => li.getAttribute('data-now') === li.getAttribute('data-cv-n'))
      && w.qa('#prepare-body .ask-stale').length === 0 && w.calls.length === 2,
      'and the asks the Prepare application answer swaps in are marked against the copy on screen, each cited line where it was cited');
  // As a live click usually goes: the copy at once, the score seconds later.
  w = world(data.pages.go);
  await settle();
  w.answerAt(1, { ok: true, outcome: 'made', copy: data.go.copy, notices: [] });
  await settle(); await settle();
  w.answer({ ok: true, outcome: 'scored', html: data.go.html });
  await settle(); await settle();
  const lateCited = w.qa('#prepare-body [data-cv-text]');
  say(lateCited.length > 0 && lateCited.every((li) => li.getAttribute('data-now') === li.getAttribute('data-cv-n')),
      'and so are they when the score lands after the copy, marked as they are swapped in');

  // ------------------------------------- Applied, saved copy on screen
  w = world(data.pages.same);
  await settle();
  w.setConfirm(false);
  const sameLine = w.q('textarea[data-edit="s0.l0"]');
  const sameWas = sameLine.value;
  sameLine.value = sameWas + ' and more';
  w.fire(sameLine, 'input');
  const leaveTyped = w.fire(w.sandbox, 'beforeunload');
  sameLine.value = sameWas;
  w.fire(sameLine, 'input');
  const leaveUndone = w.fire(w.sandbox, 'beforeunload');
  say(leaveTyped.defaultPrevented && !leaveUndone.defaultPrevented && w.q('#cv-download-hint').textContent === '',
      'an edit typed and undone again is no edit: leaving asks nothing and Download needs no word');
  w.click(w.q('[data-cv-act="apply"]'));
  await settle();
  say(w.confirms.length === 0 && w.calls.length === 1 && w.calls[0].url === '/apply',
      'Applied with the saved copy on screen, unedited, goes without asking');

  // -------------------- a long line, from a score made before this branch
  w = world(data.pages.legacy);
  await settle();
  const longLi = w.q('#prepare-body [data-cv-cut="1"]');
  say(w.calls.length === 0 && longLi && longLi.getAttribute('data-now') === String(data.longN)
      && w.qa('#prepare-body .ask-stale').length === 0,
      'a long cited line kept only in part by a score made before this branch is found by how it starts, so an unedited copy shows nothing stale');
  const longField = w.q('textarea[data-edit="s0.l0"]');
  const longWas = longField.value;
  longField.value = 'Changed at the start. ' + longWas;
  w.fire(longField, 'input');
  say(w.qa('#prepare-body .ask-stale').length === 1, 'and an edit to its start makes the ask stale');
  longField.value = longWas.replace('Rust', 'Go');
  w.fire(longField, 'input');
  say(w.qa('#prepare-body .ask-stale').length === 0,
      'while an edit past the part such a score kept cannot be seen, as the score never had it');
  process.exit(bad ? 1 : 0);
})();
"""


def editor_script_checks() -> None:
    # The page's scripts in the page's order, Brief 3's own among them.
    scripts = (build_dashboard.SCRIPT + "\n" + build_dashboard.PREPARE_SCRIPT + "\n"
               + cv_page.CV_MODEL_JS + "\n" + cv_page.CV_EDITOR_JS)
    code = re.sub(r"//[^\n]*", "", cv_page.CV_EDITOR_JS)
    check(not re.search(r"setTimeout|setInterval|requestAnimationFrame|XMLHttpRequest|sendBeacon",
                        code)
          and code.count("fetch(") == 1 and "method: 'POST'" in code,
          "the editor posts with fetch and method POST, the one shape the guard header "
          "wrapper covers, and holds no timer")
    sends = re.findall(r"send\('(\w+)'", code)
    check(sends == ["open"] and "send(act, b)" in code
          and re.search(r"if \(data\.mode === 'tailored' && editable && data\.needsCopy\) send\('open', null\);", code),
          "it posts on its own only to make the copy of a role that has none, and "
          "everything else from a button")
    node = shutil.which("node")
    if not node:
        note("node is not on the path, so the editor script was not run")
        return
    with fresh() as env:
        oid = env.role()
        unsaved = env.role(title="Unsaved", company="Beta")
        applied = env.role(title="Applied", company="Gamma")
        saved_text = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
        env.save(applied, saved_text)
        env.apply(applied, "CV - Luke Keogh - Gamma - Applied")
        open_page = render_page(env, oid)
        env.open(unsaved)
        unsaved_page = render_page(env, unsaved)
        applied_page = render_page(env, applied)
        opened = env.open(oid)
        open_answer = {"ok": True, "outcome": "made", "copy": opened["copy"], "notices": []}
        env.rescore(oid, COPY_TEXT)
        better = cv_doc.accept(edited(COPY_TEXT, "- SpiraPlan and regulated ALM configuration",
                                      "- SpiraPlan and regulated ALM configuration\n"
                                      "- Kubernetes and cloud deployment of clinical systems"))[1]
        rescored = env.rescore(oid, better)
        rescored["html"] = render_inner(env, oid)
        # The right side the server draws for the text the runs below keep on
        # screen, the master's, beside this score: before the risk line is
        # edited away and after. The script's own counts must read the same.
        import html as htmllib

        def counts_of(inner):
            return htmllib.unescape(re.search(r"data-ask-counts[^>]*>([^<]*)</span>", inner).group(1))
        risk_line = "- Own the software risk file under ISO 14971."
        conn = radar_common.get_db(env.db)
        try:
            ref = env.copy(oid)["role_ref"]
            tailored_cv.store_text(conn, ref, MASTER_TEXT)
            conn.commit()
            summary_master = counts_of(render_inner(env, oid))
            tailored_cv.store_text(conn, ref, edited(MASTER_TEXT, risk_line, "- Own the risk file."))
            conn.commit()
            summary_risk = counts_of(render_inner(env, oid))
        finally:
            conn.close()
        # The copy the answer hands back is the one on screen, under the
        # version the server gave it after the rescore.
        rescored["copy"] = {**opened["copy"], "hash": cv_store.cv_hash(better)}
        rescored["tailored"] = {**rescored["tailored"], "before_cv_match": 80, "cv_match": 90}
        saved = env.save(oid, MASTER_TEXT)
        saved["copy"] = {**opened["copy"], "saved": saved["copy"]["saved"]}
        # A copy with a section of its own, for the editing buttons.
        ops_role = env.role(title="Ops", company="Mu Diagnostics")
        env.open(ops_role)
        ops_text = cv_doc.render(cv_doc.normalize({**MASTER_DOC, "sections": MASTER_DOC["sections"] + [
            {"title": "Patents", "lines": [{"b": True, "t": "One patent."}], "entries": []}]}))
        # A copy edited since it was saved.
        differs_role = env.role(title="Differs", company="Nu Diagnostics")
        env.save(differs_role, MASTER_TEXT)
        conn = radar_common.get_db(env.db)
        try:
            tailored_cv.store_text(conn, env.copy(ops_role)["role_ref"], ops_text)
            tailored_cv.store_text(conn, env.copy(differs_role)["role_ref"], COPY_TEXT)
            conn.commit()
        finally:
            conn.close()
        ops_page = render_page(env, ops_role)
        differs_page = render_page(env, differs_role)
        # A role opened from its Jobs row, with work to do: both scripts post.
        go_role = env.role(title="Go", company="Omicron Diagnostics")
        c = env.conn()
        try:
            go_page = build_dashboard.render_prepare_page(c, go_role, CFG, "test.sqlite", go=True)
        finally:
            c.close()
        env.prepare(go_role)
        go_html = render_inner(env, go_role)
        go_copy = env.open(go_role)["copy"]
        # A saved copy that is the copy on screen.
        same_role = env.role(title="Same", company="Pi Diagnostics")
        env.save(same_role, MASTER_TEXT)
        same_page = render_page(env, same_role)
        # A role scored before this branch, on a master with a long line: its
        # working keeps the start of the line and no whole line, and no hash.
        long_master = cv_doc.render(cv_doc.normalize({**MASTER_DOC, "sections": [
            {"title": "Summary", "entries": [], "lines": [
                {"b": False, "t": "Regulated software in IVD. " * 12 + "Ends with Rust."}]}]
            + MASTER_DOC["sections"][1:]}))
        long_n = next(i for i, l in enumerate(cv_store.cv_lines(long_master), 1)
                      if l.endswith("Ends with Rust."))
        MASTER[0] = long_master
        legacy_role = env.role(title="Legacy", company="Rho Diagnostics")
        MODEL["hook"] = lambda call: (json.dumps({**json.loads(spec_reply(call)[0]), "asks": [
            {"ask": "Rust in regulated software", "status": "met", "cv_lines": [long_n]}]}), usage())
        env.prepare(legacy_role)
        MODEL["hook"] = None
        old = json.loads(env.row(legacy_role)["score_working"])
        old.pop("cv_hash", None)
        old.pop("long_lines", None)
        env.sql("UPDATE opportunities SET score_working = ? WHERE id = ?", (json.dumps(old), legacy_role))
        env.open(legacy_role)
        legacy_page = render_page(env, legacy_role)
        MASTER[0] = MASTER_TEXT
        mismatch = open_page.replace("Chichester, West Sussex", "Chichester, West Sussex!", 1)
        import html as htmllib
        attr = re.search(r'data-cv="([^"]*)"', mismatch)
        mdata = json.loads(htmllib.unescape(attr.group(1)))
        mdata["lines"][2] = "Somewhere else"
        mismatch = mismatch.replace(attr.group(0), 'data-cv="' + htmllib.escape(json.dumps(mdata)) + '"')
    master_env_dir = Path(tempfile.mkdtemp(prefix="radar-cv-node-"))
    saved_dir = cv_store.PROFILE_DIR
    cv_store.PROFILE_DIR = master_env_dir
    (master_env_dir / "cv-20261110-090000.md").write_text(MASTER_TEXT, encoding="utf-8")
    (master_env_dir / "cv.active").write_text("cv-20261110-090000.md\n", encoding="utf-8")
    try:
        with fresh() as env:
            master_page = serve_dashboard_page(env)
    finally:
        cv_store.PROFILE_DIR = saved_dir
        shutil.rmtree(master_env_dir, ignore_errors=True)
    lines = cv_store.cv_lines(MASTER_TEXT)
    risk_n = lines.index("- Own the software risk file under ISO 14971.") + 1
    rng = random.Random(11)
    raw_docs = [random_doc(rng) for _ in range(60)] + [cv_doc.normalize(d) for d in corpus()[:20]]
    strings = ["", "  a  b ", "## ### Not a heading", "- dash", "- - double", "#### four",
               "#hashtag", "\ttab", "x" * 260, "• dotted bullet", "* starred", "Cafe\u0301",
               "a\u2028b", "nb\u00a0sp", chr(0xFEFF) + "bom", "lone " + chr(0xD800) + " surrogate",
               "emoji \U0001F600 here", "ctrl\x01\x7f\x85", "#", "-", "- ", "### deep"]
    finds = []
    keys = [cv_doc.line_key(l) for l in lines + [lines[risk_n - 1]]]
    for cited in ({"n": risk_n, "text": lines[risk_n - 1]}, {"n": 3, "text": lines[risk_n - 1]},
                  {"n": 99, "text": lines[risk_n - 1]}, {"n": 2, "text": "gone"},
                  {"n": 1, "text": ""}, {"n": risk_n, "text": "* Own the software risk file under ISO 14971."}):
        finds.append({"cited": cited, "keys": keys,
                      "now": cv_doc.find_line(cited, lines + [lines[risk_n - 1]])})
    long_line = "- " + "word " * 60 + "end"
    cut = long_line.strip()[:cv_doc.CITED_TEXT_MAX]
    for small, cited in ((["X", "A", "B"], {"n": 3, "text": "A"}),
                         (["A", "X", "A"], {"n": 2, "text": "A"}),
                         (["A", "X", "A"], {"n": None, "text": "A"}),
                         (["X", "A"], {"n": 2, "text": "A"}),
                         (["A", "X"], {"n": 1, "text": "A"}),
                         # The start of a long line, as a working stored before
                         # whole lines were kept.
                         (["X", long_line, "B"], {"n": 2, "text": cut}),
                         (["X", "B", long_line], {"n": 2, "text": cut}),
                         ([long_line.replace("end", "fin"), "X"], {"n": 2, "text": cut}),
                         (["X", "word " * 40], {"n": 2, "text": cut}),
                         (["X", long_line], {"n": 2, "text": long_line[:120]})):
        finds.append({"cited": {**cited, "cut": cv_doc.cited_is_cut(cited)},
                      "keys": [cv_doc.line_key(l) for l in small],
                      "now": cv_doc.find_line(cited, small)})
    edges = [chr(c) for c in (0x1f, 0x20, 0x7e, 0x7f, 0x80, 0x9f, 0xa0, 0x2027, 0x2028,
                              0x2029, 0x202a, 0xd7ff, 0xd800, 0xdbff, 0xdc00, 0xdfff,
                              0xe000, 0xfefe, 0xfeff, 0xfffd, 0xfffe, 0xffff)]
    strings += ["a" + ch + "b" for ch in edges] + [ch + "- x" for ch in edges]
    payload = {
        "scripts": scripts, "header": HEADER, "version": MASTER_LABEL[0],
        "ids": {"open": oid}, "savedName": "CV - Luke Keogh - Veltrix Diagnostics - Head of Software",
        "hashes": {"open": opened["copy"]["hash"], "rescored": cv_store.cv_hash(better)},
        "copyMoved": prep.COPY_MOVED,
        "summaries": {"master": summary_master, "risk": summary_risk},
        "go": {"html": go_html, "copy": go_copy, "id": go_role},
        "longN": long_n,
        "pages": {"open": open_page, "applied": applied_page, "mismatch": mismatch,
                  "unsaved": unsaved_page, "master": master_page, "ops": ops_page,
                  "differs": differs_page, "go": go_page, "same": same_page,
                  "legacy": legacy_page},
        "opsLines": len(cv_store.cv_lines(ops_text)),
        "doing": {"rescore": "Scoring the copy on screen against the full description, once. "
                             "This takes a few seconds."},
        "answers": {"open": open_answer, "rescore": rescored, "save": saved},
        "texts": {"master": MASTER_TEXT, "masterPage": MASTER_TEXT},
        "lines": {"master": lines, "saved": cv_store.cv_lines(saved_text), "riskN": risk_n,
                  "newSkillN": lines.index("- SpiraPlan and regulated ALM configuration") + 2},
        "places": {"risk": "s1.e0.l2", "above": "s1.e0.l0", "splitNext": "s1.e0.l1",
                   "skills": "s2", "newSkill": "s2.l2", "newSkillNext": "s2.l3",
                   "summaryLine": "s0.l1"},
        "parity": {
            "docs": [{"doc": d, "lines": cv_doc.render_lines(cv_doc.normalize(d))} for d in raw_docs],
            "strings": [{"s": s, "clean": cv_doc.clean(s),
                         "plain": cv_doc.clean_plain(s, allow_bullet=False),
                         "key": cv_doc.line_key(s), "line": cv_doc._line({"b": False, "t": s})}
                        for s in strings],
            "finds": finds,
            "master": {"doc": cv_doc.load_doc(MASTER_TEXT), "text": MASTER_TEXT},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "editor.js").write_text(NODE_EDITOR, encoding="utf-8")
        (Path(tmp) / "data.json").write_text(json.dumps(payload), encoding="utf-8")
        try:
            done = subprocess.run([node, str(Path(tmp) / "editor.js"), str(Path(tmp) / "data.json")],
                                  capture_output=True, text=True, encoding="utf-8", timeout=120)
        except (OSError, subprocess.TimeoutExpired) as err:
            check(False, f"node ran the editor script ({type(err).__name__})")
            return
        out = done.stdout.splitlines()
        for ln in out:
            if ln.startswith(("PASS  ", "FAIL  ")):
                check(ln.startswith("PASS"), ln[6:])
            elif ln.startswith("NOTE  "):
                note(ln[6:])
        if done.returncode != 0 and not any(ln.startswith("FAIL  ") for ln in out):
            check(False, "node ran the editor script ("
                  + (done.stderr.strip()[-400:] or f"exit {done.returncode}") + ")")


# ------------------------------------------------------------ the static scan

def static_checks() -> None:
    scripts = sorted((REPO_ROOT / "scripts").glob("*.py"))
    src = (REPO_ROOT / "scripts" / "prepare_application.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check(not any(isinstance(n, (ast.For, ast.While)) for n in ast.walk(tree))
          and "fetchall" not in attrs and "executemany" not in attrs
          and src.count("score_item.score_opportunity(") == 1,
          "the click's module still holds no loop and scores in one place, which the "
          "copy's Rescore role goes through too")
    importers = sorted(p.name for p in scripts if p.name != "prepare_application.py"
                       and re.search(r"^\s*(import|from)\s+prepare_application\b",
                                     p.read_text(encoding="utf-8"), re.M))
    check(importers == ["build_dashboard.py", "serve_dashboard.py"],
          f"only the dashboard imports it ({importers})")
    light = {}
    for name in ("cv_doc.py", "cv_page.py", "tailored_cv.py", "cv_word.py"):
        text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        light[name] = sorted(set(re.findall(r"^\s*(?:import|from)\s+(\w+)", text, re.M)))
    check(not any(set(v) & {"prepare_application", "serve_dashboard", "build_dashboard",
                            "urllib", "requests", "http", "socket", "subprocess", "anthropic"}
                  for v in light.values()),
          f"the new modules call no model, open no connection and import no page ({light})")
    flows = [p.name for p in sorted((REPO_ROOT / "workflows").glob("*.json"))
             if re.search(r"/prepare/cv|/cv/master|tailored|cv_doc|cv_word", p.read_text(encoding="utf-8"))]
    check(not flows, f"no n8n workflow runs any of it or posts to it ({flows})")
    check(radar_common.prompt_version() == "97f11cce",
          "the scorer prompt is the one Brief 3 left, 97f11cce: nothing in it changed")
    serve = (REPO_ROOT / "scripts" / "serve_dashboard.py").read_text(encoding="utf-8")
    stree = ast.parse(serve)
    do_get = next(n for n in ast.walk(stree) if isinstance(n, ast.FunctionDef) and n.name == "do_GET")
    body = ast.get_source_segment(serve, do_get)
    check(not re.search(r"run_copy|prepare_application|save_master|get_db\(", body)
          and "download_cv(" in body,
          "GET reaches no write: the download is the one new GET, and it only reads")
    down = ast.get_source_segment(serve, next(n for n in ast.walk(stree)
                                             if isinstance(n, ast.FunctionDef) and n.name == "download_cv"))
    check("mode=ro" in down and "get_db(" not in down and "commit" not in down,
          "and it opens the database read only")
    pane = (REPO_ROOT / "scripts" / "build_dashboard.py").read_text(encoding="utf-8")
    pane = pane[pane.index("def render_cv_pane("):]
    pane = pane[:pane.index("\ndef ", 10)] if "\ndef " in pane[10:] else pane
    check("get_db(" not in pane and "commit" not in pane and "open_copy" not in pane,
          "the page's CV pane only reads, and leaves making the copy to the script's POST")


# ---------------------------------------------------- the queries for the box

BOX_DOC = REPO_ROOT / "docs" / "box-checks-brief-4.md"


def box_statements() -> list[str]:
    """Every query the box checks hand over, in the order they appear."""
    text = BOX_DOC.read_text(encoding="utf-8")
    return re.findall(r'Q "([^"]+)"', text) + re.findall(
        r'sqlite3 -readonly db/radar\.sqlite "([^"]+)"', text)


def run_query(db, sql):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def box_query_checks() -> None:
    statements = box_statements()
    check(len(statements) >= 15, f"the box checks hand over {len(statements)} queries")
    find = {s.split("(", 1)[0][:40]: s for s in statements}
    proof = {
        "pairs": next(s for s in statements if "pairs_scored_twice" in s),
        "landed": next(s for s in statements if "roles_with_a_tailored_score" in s),
        "after": next(s for s in statements if "tailored_after_applying" in s),
        "saved_after": next(s for s in statements if "saved_after_applying" in s),
        "open_role": next(s for s in statements if s.startswith("SELECT id FROM opportunities o")),
        "before4": next(s for s in statements if "copies_table" in s and "copy_columns" not in s),
        "after4": next(s for s in statements if "copy_columns" in s),
        "none_yet": next(s for s in statements if "tailored_scores" in s and "copies" in s),
    }
    del find
    with fresh() as env:
        # The old shape first, for the BEFORE query.
        conn = env.conn()
        conn.execute("DROP TABLE tailored_cvs")
        conn.execute("ALTER TABLE score_history DROP COLUMN cv_hash")
        conn.commit()
        conn.close()
        check(run_query(env.db, proof["before4"]) == [(0, 0)],
              "on a database before the deploy, the BEFORE check reads 0 and 0")
        radar_common.get_db(env.db).close()
        check(run_query(env.db, proof["after4"]) == [(1, 13, 1)]
              and run_query(env.db, proof["none_yet"]) == [(0, 0)],
              "after the first open it reads 1, 13 and 1, and no copy or tailored score yet")
        a = env.role()
        b = env.role(title="Second", company="Beta")
        env.open(a)
        env.rescore(a, COPY_TEXT)
        env.rescore(a, edited(COPY_TEXT, "Built the software team", "Built and led the team"))
        env.rescore(a, COPY_TEXT)
        env.save(b, MASTER_TEXT)
        # Saved, then applied an hour later, as a person would.
        env.sql("UPDATE tailored_cvs SET saved_at = '2026-11-12T09:00:00Z',"
                " updated_at = '2026-11-12T09:00:00Z' WHERE opportunity_id = ?", (b,))
        env.apply(b, "CV - Luke Keogh - Beta - Second")
        repost_b = env.role(title="Second", company="Beta")
        broken = []
        for sql in statements:
            try:
                run_query(env.db, sql)
            except sqlite3.Error as err:
                broken.append(f"{sql[:50]} {err}")
        check(not broken, f"every query on the page runs on a database the deploy made ({broken[:2]})")
        check(run_query(env.db, proof["pairs"]) == [(0,)]
              and run_query(env.db, proof["landed"]) == [(0,)]
              and run_query(env.db, proof["after"]) == [(0,)]
              and run_query(env.db, proof["saved_after"]) == [(0, 0)],
              "on what the code does, the four proof queries read 0, a copy put back "
              "to an earlier text included")
        picked = [r[0] for r in run_query(env.db, proof["open_role"])]
        check(picked and picked[0] not in (b, repost_b),
              "the role the AFTER block tries is an open LinkedIn role that is not applied, "
              "and not a repost of an applied one")
        env.sql("UPDATE tailored_cvs SET saved_at = '2026-11-12T11:00:00Z' WHERE opportunity_id = ?", (b,))
        check(run_query(env.db, proof["saved_after"]) == [(1, 0)],
              "the fourth catches a copy saved after its role was applied")
        env.sql("UPDATE tailored_cvs SET updated_at = '2026-11-12T11:00:00Z' WHERE opportunity_id = ?", (b,))
        check(run_query(env.db, proof["saved_after"]) == [(1, 1)],
              "and one changed after it")
        env.sql("INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
                " description_hash, cv_hash) SELECT opportunity_id, recorded_at, event, writer,"
                " description_hash, cv_hash FROM score_history WHERE event = 'tailored' LIMIT 1")
        check(run_query(env.db, proof["pairs"]) == [(1,)],
              "and the first catches a pair scored twice")
        env.sql("UPDATE opportunities SET cv_version = 'tailored-abc' WHERE id = ?", (b,))
        check(run_query(env.db, proof["landed"]) == [(1,)],
              "the second a tailored score written onto a role")
        env.sql("INSERT INTO score_history (opportunity_id, recorded_at, event, writer)"
                " VALUES (?, '2026-11-13T00:00:00Z', 'tailored', 'prepare')", (b,))
        check(run_query(env.db, proof["after"]) == [(1,)],
              "and the third a tailored score made after the role was applied")


def docs_checks() -> None:
    doc = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check("## The CV page and the tailored CV" in doc,
          "the architecture notes have a section on it")
    section = " ".join(doc.split("## The CV page and the tailored CV")[1].split("\n## ")[0].split())
    for needle in ("GET /cv", "POST /cv/master/save", "POST /prepare/cv/open",
                   "/prepare/cv/reset", "POST /prepare/cv/rescore", "POST /prepare/cv/save",
                   "GET /prepare/<id>/cv.docx", "tailored_cvs", "cv_hash", "event `tailored`",
                   "cv_store.cv_lines", "CV_MODEL_JS", "_score", "SPEC_MAX_TOKENS",
                   "applied", "python-docx", "first-open", "Re-score stale items",
                   "CV - Luke Keogh - <Company> - <Job", "is_canonical", "long_lines",
                   "COPY_MOVED", "UNCHANGED_COPY", "Prepare application click's lock"):
        if needle not in section:
            check(False, f"the notes say {needle!r}")
            break
    else:
        check(True, "and they cover the pages, the routes, the table, the history, the "
                    "numbering, the guards, the Word file, applied roles and the deploy")
    box = BOX_DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```(\w*)\n(.*?)```", box, re.S)
    check(len(blocks) >= 10 and all(lang == "bash" and body.strip() for lang, body in blocks),
          f"the box checks are {len(blocks)} fenced bash blocks, none of them empty")
    for heading in ("## BEFORE", "## The deploy, one block at a time", "## AFTER, before any click",
                    "## The click block, after your first real use", "## Rollback"):
        if heading not in box:
            check(False, f"the box checks have {heading!r}")
            break
    else:
        check(True, "with BEFORE, the deploy, AFTER, the first use and the rollback")
    check("/opt/biz-admin/radar-venv/bin/python" in box and "docker restart medtech-radar" in box
          and "backup_radar.sh" in box,
          "the deploy backs up, opens the database with the venv's Python and restarts "
          "the dashboard")
    cfg = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("run_cv_test.py" in cfg, "and run_all.sh runs this file")


def serve_dashboard_page(env) -> str:
    saved = serve_dashboard.ARGS
    serve_dashboard.ARGS = argparse.Namespace(db=str(env.db), push=False,
                                              host="127.0.0.1", port=0)
    try:
        return serve_dashboard.render_cv_page().decode("utf-8")
    finally:
        serve_dashboard.ARGS = saved



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()
    for section in (migration_checks, layout_checks, agreement_checks,
                    scorer_checks, copy_checks, master_untouched_checks, copy_busy_checks,
                    rescore_checks, rescore_guard_checks, gate_checks, base_checks,
                    unchanged_checks, legacy_checks, click_lock_checks, memo_checks,
                    edge_checks, history_checks,
                    stale_checks, word_checks, route_checks, applied_checks,
                    page_checks, master_page_checks, editor_script_checks,
                    static_checks, box_query_checks, docs_checks):
        # One section that raises is one FAIL that names it. The sections
        # after it still run, so a mistake in the first does not hide the rest.
        try:
            section()
        except Exception as err:  # noqa: BLE001  reported, never swallowed
            import traceback
            traceback.print_exc()
            check(False, f"{section.__name__} stopped on {type(err).__name__}: {err}")
        finally:
            reset()
    print()
    if failures:
        print(f"{len(failures)} CV-PAGE CHECK(S) FAILED")
        return 1
    print("ALL CV-PAGE CHECKS PASSED"
          + (f" ({len(skipped)} skipped, see the NOTE lines)" if skipped else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # noqa: BLE001  readable FAIL, never a traceback
        import traceback
        print(f"\nFAIL. Unexpected {type(err).__name__}. {err}")
        traceback.print_exc()
        sys.exit(1)
