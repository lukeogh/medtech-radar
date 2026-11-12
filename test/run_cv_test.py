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
               "\ttabbed\tthrough", "line\nbreak", " nbsp", "trailing   "]

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
    legacy = ("Luke Keogh\nChichester | luke@example.com\n\nPROFILE\n"
              "Software leader.\n\n## Professional Experience\n### Director, Axithra\n"
              "* Led the IEC 62304 programme.\n\n#### Earlier role, Fujitsu\n"
              "• Defence work.\n\nKey Skills:\n- IEC 62304\n\n"
              "# Education & Certifications\n- BEng\nCertifications\n"
              "- ISO 13485 Lead Auditor\n\nINTERESTS\n- Sailing\n")
    doc = cv_doc.load_doc(legacy)
    titles = [s["title"] for s in doc["sections"]]
    check(doc["name"] == "Luke Keogh" and doc["headline"] == ["Chichester | luke@example.com"]
          and titles == ["Summary", "Experience", "Skills",
                         "Education and certifications", "Interests"],
          f"a Word CV is read the way a person reads it: the first line is the name, "
          f"the usual words and lines in capitals start sections, and Education and "
          f"Certifications become one ({titles})")
    exp = doc["sections"][1]
    check([e["heading"] for e in exp["entries"]] == ["Director, Axithra",
                                                    "Earlier role, Fujitsu"]
          and exp["entries"][0]["lines"] == [{"b": True, "t": "Led the IEC 62304 programme."}]
          and exp["entries"][1]["lines"] == [{"b": True, "t": "Defence work."}]
          and doc["sections"][3]["lines"][-1] == {"b": True, "t": "ISO 13485 Lead Auditor"},
          "deeper headings are entries, and star, dot and dash bullets are bullets")
    fixture = (REPO_ROOT / "test" / "fixtures" / "profile_snapshot.md").read_text(encoding="utf-8")
    fdoc = cv_doc.load_doc(fixture)
    check(not cv_doc.is_layout(fixture) and fdoc["name"].startswith("Profile snapshot")
          and len(fdoc["headline"]) > 10
          and [s["title"] for s in fdoc["sections"]] == list(cv_doc.STANDARD_SECTIONS),
          "a CV with no sections keeps every line, under the name, and gets the four "
          "standard sections to fill")

    # Cleaning, one way for every string.
    nasty = "a\tb c" + chr(0xFEFF) + "d " + chr(0xD800) + "e  f\x00g\x85h i"
    check(cv_doc.clean(nasty) == "a b cd e f g h i",
          "tabs, controls, separators and odd spaces become one space, and lone "
          "surrogates, the byte order mark and non-characters go")
    check(cv_doc.clean("Café") == "Café", "text is made NFC")
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
    check("no name" in refused("## Skills\n- x\n"), "a CV with no name is refused")
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
    check(score_item.CV_LINE_TEXT_MAX >= cv_doc.KEY_CHARS,
          "a cited line keeps at least as much text as it is found again by")

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
        check(env.save(oid, "## No name\n- x\n") == {
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
        out = env.rescore(oid, MASTER_TEXT)
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
              and cv_store.numbered_cv(MASTER_TEXT) in CALLS[0]["system"][0]["text"]
              and '"basis": "spec"' in CALLS[0]["user"]
              and DESCRIPTION.splitlines()[1] in json.loads(CALLS[0]["user"].split("\n\n", 1)[1])["description"],
              "it shows the scorer the copy, numbered, and the stored description")
        check(record and record["description_hash"] == desc_hash
              and record["cv_hash"] == copy["cv_hash"] == cv_store.cv_hash(MASTER_TEXT)
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
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "reused" and len(CALLS) == 1 and len(env.history(oid)) == 1
              and "nothing was spent" in out["notices"][0]["text"],
              "pressed again on the same copy, nothing is spent and nothing is written")

        # An edit, then scored: before is the copy's own last score.
        text = cv_doc.accept(edited(MASTER_TEXT, "- SpiraPlan and regulated ALM configuration",
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
        out = env.rescore(oid, MASTER_TEXT)
        record = tailored_cv.read_score(env.copy(oid))
        check(out["outcome"] == "reused" and len(CALLS) == 2
              and record["cv_hash"] == cv_store.cv_hash(MASTER_TEXT) and record["reused"]
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
        threads = [threading.Thread(target=lambda: outs.append(env.rescore(oid, MASTER_TEXT)))
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
        out = env.rescore(repost, MASTER_TEXT)
        check(out["outcome"] == "reused" and len(CALLS) == 1,
              "and the same copy pressed from a repost of the role finds the score made")

    # A reply that cannot be used: remembered five minutes.
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        MODEL["hook"] = lambda call: ("not json {{{", usage())
        out = env.rescore(oid, MASTER_TEXT)
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
        out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "cooling" and len(CALLS) == 1,
              "the same copy pressed again inside five minutes calls nothing")
        text = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
        MODEL["hook"] = None
        out = env.rescore(oid, text)
        check(out["outcome"] == "scored" and len(CALLS) == 2,
              "while an edited copy is a new question, asked at once")
        MODEL["hook"] = lambda call: ("not json {{{", usage())
        CLOCK[0] += prep.UNUSABLE_COOLDOWN_SECONDS + 1
        out = env.rescore(oid, MASTER_TEXT)
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
            out = env.rescore(oid, MASTER_TEXT)
        check(out["outcome"] == "score_failed" and len(CALLS) == 1
              and "could not be reached (RuntimeError)" in out["notices"][0]["text"]
              and "tailored copy was not scored" in out["notices"][0]["text"],
              "a call that raised stores nothing and says so")
        env.rescore(oid, MASTER_TEXT)
        CLOCK[0] += prep.ERROR_COOLDOWN_SECONDS + 1
        MODEL["hook"] = None
        out = env.rescore(oid, MASTER_TEXT)
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
            out = env.rescore(oid, MASTER_TEXT)
            again = env.rescore(oid, MASTER_TEXT)
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
            out = env.rescore(oid, MASTER_TEXT)
        holder["lock"].__exit__(None, None, None)
        check(out["outcome"] == "score_held" and len(CALLS) == 1
              and tailored_cv.read_score(env.copy(oid)) is None
              and "held in memory" in out["notices"][0]["text"],
              "a reply that comes while the database is busy is held, not lost")
        MODEL["hook"] = None
        other = edited(MASTER_TEXT, "Built the software team", "Built and led the team")
        out = env.rescore(oid, other)
        check(out["outcome"] == "scored" and len(CALLS) == 2
              and tailored_cv.read_score(env.copy(oid))["cv_hash"] == cv_store.cv_hash(other),
              "what is held is for its own copy only, another text is a call of its own")
        out = env.rescore(oid, MASTER_TEXT)
        record = tailored_cv.read_score(env.copy(oid))
        check(out["outcome"] == "scored" and len(CALLS) == 2
              and record["cv_hash"] == cv_store.cv_hash(MASTER_TEXT)
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
            out = env.rescore(oid, MASTER_TEXT)
            check(len(CALLS) == 1 and out["outcome"] in ("applied", "not_open")
                  and tailored_cv.read_score(env.copy(oid)) is None
                  and not env.history(oid)
                  and env.runs()[0]["items_new"] == 0 and "not stored" in env.runs()[0]["note"],
                  f"a role {label} while the model answers keeps what it had: the reply "
                  "is not stored, and its cost is recorded as not stored")


def history_checks() -> None:
    """A tailored row in the history is never the role's score to archive_score."""
    with fresh() as env:
        oid = env.role()
        env.open(oid)
        env.rescore(oid, MASTER_TEXT)
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
            status, got, _h = post(port, "/prepare/cv/rescore", {"id": oid, "text": "## x\n"})
            check(status == 400 and got.get("invalid") and "no name" in got.get("note", ""),
                  "a CV that cannot be taken is a 400 with the reason")
            status, got, _h = post(port, "/prepare/cv/open", {"id": oid})
            check(status == 200 and got["outcome"] == "made" and "html" in got
                  and got["copy"]["lines"] == cv_store.cv_lines(MASTER_TEXT),
                  "open answers 200 with the copy and the page's right hand side")
            status, got, _h = post(port, "/prepare/cv/rescore", {"id": oid, "text": MASTER_TEXT})
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

            # The download.
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
            check(read == [l for l in cv_store.cv_lines(MASTER_TEXT) if l.strip()],
                  "and it is the saved copy")
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
            status, got, _h = post(port, "/apply", {"id": named, "cv": "cv-2026-11-12.pdf"})
            check(status == 200 and env.row(named)["applied_cv"] == "cv-2026-11-12.pdf",
                  "a reference a request names is still kept where no copy was saved, as "
                  "Brief 5 had it")
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
              and re.search(r'<a class="act-btn" id="cv-download" href="/prepare/\d+/cv.docx" hidden>', pane)
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
        env.rescore(oid, MASTER_TEXT)
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
        asks = re.findall(r'<li class="ask" data-ask data-status="(\w+)" tabindex="0">', inner)
        cited = re.findall(r'<li data-cv-n="(\d+)" data-cv-text="([^"]*)">', inner)
        lines = cv_store.cv_lines(MASTER_TEXT)
        import html as htmllib
        check(asks == ["met", "partly", "met", "missing", "met"]
              and cited and all(lines[int(n) - 1].strip() == htmllib.unescape(t) for n, t in cited),
              "each ask carries its status, and each cited line its number and its text "
              "as the working stored them, which is what the script lights up by")
        data = pane_data(page)
        check(not data["needsCopy"] and data["lines"] == lines,
              "with a copy, the page shows the copy and asks for nothing")

        # The copy edited since the score.
        risk = "- Own the software risk file under ISO 14971."
        conn = radar_common.get_db(env.db)
        try:
            tailored_cv.store_text(conn, env.copy(oid)["role_ref"],
                                   edited(MASTER_TEXT, risk, "- Own the risk file."))
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
        env.rescore(oid2, MASTER_TEXT)
        better = cv_doc.accept(edited(MASTER_TEXT, "- SpiraPlan and regulated ALM configuration",
                                      "- SpiraPlan and regulated ALM configuration\n"
                                      "- Kubernetes and cloud deployment of clinical systems"))[1]
        env.rescore(oid2, better)
        inner = render_inner(env, oid2)
        check('<span class="ask-was">was missing</span>' in inner
              and "Now met, was missing." in page_text(inner) and "1 ask changed." in page_text(inner),
              "an ask that moved carries what it was, and the score lists what changed")

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
        env.rescore(oid, MASTER_TEXT)
        env.apply(oid, "CV - Luke Keogh - Veltrix Diagnostics - Head of Software")
        before = env.dump()
        page = render_page(env, oid)
        left = page[page.index('id="cv-pane"'):page.index('id="prepare-body"')]
        data = pane_data(page)
        check("This role is marked applied with CV - Luke Keogh - Veltrix Diagnostics - Head of Software."
              in page_text(left) and "as it was sent" in page_text(left)
              and data["lines"] == cv_store.cv_lines(saved) and data["editable"] is False
              and "data-cv-act" not in left and "<textarea" not in left
              and re.search(r'<a class="act-btn" id="cv-download" href="/prepare/\d+/cv.docx">Download</a>', left),
              "an applied role shows the copy that was saved and sent, read only, with "
              "Download and no button that would change anything")
        check(env.dump() == before, "and drawing it writes nothing")
        acked = env.role(title="Acked", company="Epsilon", acknowledged_at="2026-11-11T09:00:00Z")
        page = render_page(env, acked)
        left = page[page.index('id="cv-pane"'):page.index('id="prepare-body"')]
        check("This role is not open" in page_text(left) and "data-cv-act" not in left
              and "No tailored CV was made for this role." in page_text(left),
              "a role that is not open gets no copy and no buttons")

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
const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
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
    set textContent(v) { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; this._text = String(v); }
    set innerHTML(h) { this.textContent = ''; parse(String(h), this);
      observers.forEach((o) => { if (o.target === this) o.fn([]); }); }
    walk(fn) { for (const c of this.children) { if (c.nodeType === 1) { fn(c); c.walk(fn); } } }
    querySelectorAll(sel) { const out = []; this.walk((el) => { if (matches(el, sel)) out.push(el); }); return out; }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    closest(sel) { let cur = this; while (cur && cur.nodeType === 1) { if (matches(cur, sel)) return cur; cur = cur.parentNode; } return null; }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    focus() { document.activeElement = this; }
    setSelectionRange(a) { this.selectionStart = this.selectionEnd = a; }
    scrollIntoView() { events.push('scroll'); }
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
  say(rescore.textContent === 'Working' && rescore.disabled, 'its button says it is working');
  w.answer(data.answers.rescore);
  await settle(); await settle();
  say(w.sayText() === 'Rescored. CV match 80 to 90. 1 ask changed.' && !w.sayBad()
      && rescore.textContent === 'Rescore role' && !rescore.disabled,
      'the answer says the score before and after and how many asks changed, and gives the button back');
  say(w.qa('#prepare-body [data-ask]').length === 5 && w.q('#prepare-body .ask-was')
      && w.q('#prepare-body .ask-was').textContent === 'was missing',
      'and the new score and asks are swapped in on the right');
  say(w.calls.length === 2 && w.timers.length === 0, 'and nothing else is sent, and no timer is set');

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
  risk().value = was;
  w.fire(risk(), 'input');
  say(!riskAsk().classList.contains('ask-stale') && riskAsk().querySelector('.ask-status').textContent === '✓ Met'
      && label.textContent === 'CV line ' + data.lines.riskN,
      'and putting it back makes it met again');
  const above = w.q('textarea[data-edit="' + data.places.above + '"]');
  above.selectionStart = above.selectionEnd = 5;
  const enter = w.fire(above, 'keydown', { key: 'Enter' });
  await settle();
  const moved = riskAsk().querySelector('[data-cv-n="' + data.lines.riskN + '"] b').textContent;
  say(enter.defaultPrevented && moved === 'CV line ' + (data.lines.riskN + 1) + ' (' + data.lines.riskN + ' when scored)'
      && !riskAsk().classList.contains('ask-stale'),
      'Enter splits a line in two, the lines below move down one, and a cited line pushed down is found by its text, one further on');
  say(JSON.stringify(numbered(w).map((p) => p[0])) === JSON.stringify(data.lines.master.concat(['x']).map((_l, i) => i + 1)),
      'and the numbers stay the numbers of the text that would be sent');
  const halves = w.q('textarea[data-edit="' + data.places.splitNext + '"]');
  halves.value = '';
  w.fire(halves, 'input');
  const back = w.fire(halves, 'keydown', { key: 'Backspace' });
  await settle();
  say(back.defaultPrevented && numbered(w).length === data.lines.master.length,
      'Backspace in an empty line removes it');
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
  const lit = w.qa('#cv-editor .cvl.cv-hl').map((r) => r.getAttribute('data-place'));
  say(lit.includes(data.places.risk) && riskAsk().classList.contains('ask-hl'),
      'hovering an ask lights the CV lines it cites');
  w.fire(w.q('[data-place="' + data.places.risk + '"]'), 'mouseover');
  const asksLit = w.qa('#prepare-body .ask-hl').map((a) => a.textContent);
  say(asksLit.length === 1 && asksLit[0].includes('ISO 14971 risk') && !riskAsk().classList.contains('cv-hl'),
      'hovering a CV line lights the asks that cite it, and only those');
  w.fire(riskAsk().querySelector('[data-cv-text]'), 'click');
  say(w.events.includes('scroll'), 'a click on a cited line brings that line into view');

  // ---------------------------------------------------- Save CV, refused
  const ev = w.fire(w.sandbox, 'beforeunload');
  say(ev.defaultPrevented && ev.returnValue === '', 'leaving the page with edits not stored asks first');
  const save = w.q('[data-cv-act="save"]');
  w.click(save);
  await settle();
  say(w.calls.length === 3 && w.calls[2].url === '/prepare/cv/save' && typeof body(w, 2).text === 'string',
      'Save CV sends one POST with the CV as text');
  w.answer({ ok: false, note: 'The write guard refused this request.' });
  await settle(); await settle();
  say(w.sayText() === 'The write guard refused this request.' && w.sayBad() && !save.disabled,
      'a refusal shows its reason and gives the button back');
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

  // ----------------------------------------- Start again and Applied
  const calls = w.calls.length;
  w.setConfirm(false);
  w.click(w.q('[data-cv-act="reset"]'));
  w.click(w.q('[data-cv-act="apply"]'));
  await settle();
  say(w.calls.length === calls + 1 && w.confirms.length === 1 && w.calls[calls].url === '/apply',
      'Start again from master asks first and sends nothing when the answer is no, while Applied with a saved copy just goes');
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
  w.click(masterSave);
  await settle();
  say(w.calls.length === 1 && w.calls[0].url === '/cv/master/save'
      && body(w, 0).base === data.version && body(w, 0).text === data.texts.masterPage,
      'its Save as a new version sends the master as text and the version the page was opened on');
  w.answer({ ok: true, saved: true, version: 'cv-new.md' });
  await settle(); await settle();
  say(w.events.includes('reload'), 'and reloads to show the new version');
  process.exit(bad ? 1 : 0);
})();
"""


def editor_script_checks() -> None:
    scripts = (build_dashboard.SCRIPT + "\n" + cv_page.CV_MODEL_JS + "\n"
               + cv_page.CV_EDITOR_JS)
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
        env.rescore(oid, MASTER_TEXT)
        better = cv_doc.accept(edited(MASTER_TEXT, "- SpiraPlan and regulated ALM configuration",
                                      "- SpiraPlan and regulated ALM configuration\n"
                                      "- Kubernetes and cloud deployment of clinical systems"))[1]
        rescored = env.rescore(oid, better)
        rescored["html"] = render_inner(env, oid)
        rescored["copy"] = opened["copy"]
        rescored["tailored"] = {**rescored["tailored"], "before_cv_match": 80, "cv_match": 90}
        saved = env.save(oid, MASTER_TEXT)
        saved["copy"] = {**opened["copy"], "saved": saved["copy"]["saved"]}
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
               "#hashtag", "\ttab", "x" * 260, "• dotted bullet", "* starred", "Café",
               "a b", "nb sp", chr(0xFEFF) + "bom", "lone " + chr(0xD800) + " surrogate",
               "emoji \U0001F600 here", "ctrl\x01\x7f\x85", "#", "-", "- ", "### deep"]
    finds = []
    keys = [cv_doc.line_key(l) for l in lines + [lines[risk_n - 1]]]
    for cited in ({"n": risk_n, "text": lines[risk_n - 1]}, {"n": 3, "text": lines[risk_n - 1]},
                  {"n": 99, "text": lines[risk_n - 1]}, {"n": 2, "text": "gone"},
                  {"n": 1, "text": ""}, {"n": risk_n, "text": "* Own the software risk file under ISO 14971."}):
        finds.append({"cited": cited, "keys": keys,
                      "now": cv_doc.find_line(cited, lines + [lines[risk_n - 1]])})
    payload = {
        "scripts": scripts, "header": HEADER, "version": MASTER_LABEL[0],
        "ids": {"open": oid}, "savedName": "CV - Luke Keogh - Veltrix Diagnostics - Head of Software",
        "pages": {"open": open_page, "applied": applied_page, "mismatch": mismatch,
                  "unsaved": unsaved_page, "master": master_page},
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


def serve_dashboard_page(env) -> str:
    saved = serve_dashboard.ARGS
    serve_dashboard.ARGS = argparse.Namespace(db=str(env.db), push=False,
                                              host="127.0.0.1", port=0)
    try:
        return serve_dashboard.render_cv_page().decode("utf-8")
    finally:
        serve_dashboard.ARGS = saved


SECTIONS = []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()
    for section in SECTIONS or (migration_checks, layout_checks, agreement_checks,
                                scorer_checks, copy_checks, copy_busy_checks,
                                rescore_checks, rescore_guard_checks, history_checks,
                                stale_checks, word_checks, route_checks,
                                applied_checks, page_checks, master_page_checks,
                                editor_script_checks):
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
