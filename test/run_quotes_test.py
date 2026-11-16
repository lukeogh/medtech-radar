#!/usr/bin/env python
"""Acceptance checks for the quoted reasons a score gives.

No model calls and no network. RADAR_MOCK is forced on, every reply a test
needs is the deterministic mock in test/mocks.py or a stub of
radar_common.claude_call, no page is ever fetched, the CV is a fixture, and
every database is a throwaway under a temporary directory, so a mistake here
cannot spend a token, ask LinkedIn for anything or touch a real row. The
owner's own preferences file is never read.

What it checks:

- the prompt asks for up to four reasons, each one plain line of 20 words or
  fewer with a quote of 25 words or fewer copied from the advert, on a card and
  so on a full description, whose quotes come from the description; nothing
  else in the prompt changed, and its version is a new one,
- the matching rule, which the pay line of a full description shares: word for
  word, case and runs of white space ignored, the text's own characters kept,
  never across two fields of a card, never from the card on a full description,
  and for a reason whole words at either end, so a word or a figure cut short
  is not kept, while the pay line is found as it was,
- each reason a reply gives, checked in code: a quote that is not found, empty
  or without a letter or digit, not text or over 25 words is dropped, the
  reason is kept and the drop is recorded on it, at most four are kept, and a
  reasons field of any shape, a lone surrogate among it, never costs a score,
- the card path stores the reasons with the score on the row and in
  score_history, through the inbox and through rescore.py, each quote held to
  the words the row stores as well as to the card that was sent, the full
  description path through the Prepare application click, and the tailored
  copy's score through Rescore role, on the copy and in the history, a score
  found again in the history keeps them, and each call is made at its path's
  ceiling,
- the migration on a database from before: two nullable columns with no
  default, NULL on every row that was there, nothing else touched, once however
  many processes open it, and nothing re-scored or rewritten to give an
  existing score reasons, on any path that opens or reads it,
- an applied role is not written, by the click, by Rescore role, by the
  re-score, or by a repost of it arriving,
- the Jobs row and the Prepare page show each reason with its quote, escaped,
  a dropped quote as its reason alone, nothing for a score without reasons of
  its own or for a record of another version, no quote beside a description
  that replaced the one it came from, and one line where code has judged the
  gates again since the reasons were given, by the regate or the page checker,
- the box queries for the deploy run here on seeded data, as the page that
  hands them over writes them, and each proof catches the harm it is for,
- the notes describe it.

Exits non zero on any failure with readable FAIL lines.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
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
import mocks  # noqa: E402
import prepare_application as prep  # noqa: E402
import process_email  # noqa: E402
import radar_common  # noqa: E402
import regate_rates  # noqa: E402
import rescore  # noqa: E402
import score_item  # noqa: E402
import serve_dashboard  # noqa: E402
import tailored_cv  # noqa: E402

# Hermetic. The live preferences file holds the owner's own floors and
# thresholds, and a check must neither read it nor fail because it was edited.
TEMPLATE = REPO_ROOT / "config" / "preferences.template.md"
_real_load_config = radar_common.load_config
radar_common.load_config = lambda: {**_real_load_config(),
                                    "prefs_file": str(TEMPLATE)}
CFG = radar_common.load_config()
FLOORS = radar_common.read_rate_floors(CFG)
SPEC = radar_common.SCORE_BASIS_SPEC
CARD = radar_common.SCORE_BASIS_CARD
NEW_VERSION = "422cdad4"     # the prompt now: this change's, with each ask's sentence
PART_B_VERSION = "22353628"  # the prompt this change left, before the advert sentences
OLD_VERSION = "97f11cce"     # the prompt Brief 3 left

# The master CV, in the editor's layout, so that a tailored copy can be made.
MASTER_DOC = {
    "name": "Luke Keogh",
    "headline": ["Software Director, regulated medical devices and IVD",
                 "Chichester, West Sussex"],
    "sections": [
        {"title": "Summary", "entries": [], "lines": [
            {"b": False, "t": "Software leader in regulated industries since "
                              "2011, IEC 62304 and ISO 13485 throughout."}]},
        {"title": "Experience", "lines": [], "entries": [
            {"heading": "Software Director, Axithra, 2023 to now", "lines": [
                {"b": True, "t": "Lead software for a Class C IVD under IEC 62304."},
                {"b": True, "t": "Oversaw FDA clearance for a Class B IVD."}]}]},
        {"title": "Skills", "entries": [], "lines": [
            {"b": True, "t": "IEC 62304, ISO 13485, ISO 14971"}]},
    ],
}
MASTER_TEXT = cv_doc.render(MASTER_DOC)
COPY_TEXT = MASTER_TEXT.replace("Chichester, West Sussex",
                                "Chichester, West Sussex, UK", 1)
OTHER_TEXT = COPY_TEXT.replace("Lead software for", "Led software for", 1)
score_item.load_cv_text = lambda config: (MASTER_TEXT,
                                          "config/profile/cv-20261110-090000.md")
score_item.get_cv_version = lambda config: "cv-20261110-090000.md"

DESCRIPTION = (
    "About the role\n"
    "We are hiring a Head of Software for a medical device business that builds\n"
    "point of care analysers for hospital laboratories across Europe and the UK.\n\n"
    "Requirements\n"
    "- IEC 62304 software lifecycle in a Class C IVD\n"
    "- Leadership of an FDA submission for a diagnostic\n"
    "- ISO 14971 risk management for software\n\n"
    "Pay\n£750 to £850 per day, outside IR35.")

failures: list[str] = []
skipped: list[str] = []
_ids = iter(range(4_900_000_001, 5_000_000_000))


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def note(text: str) -> None:
    """A check that could not run here. Printed, counted, never a failure."""
    print("NOTE  " + text)
    skipped.append(text)


@contextlib.contextmanager
def captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


# ----------------------------------------------------------- the stand-ins

CALLS: list[dict] = []      # every model call, as it was made
MODEL = {"hook": None}      # may answer a call itself
CLOCK = [10_000.0]
_real_claude_call = radar_common.claude_call


def counting_claude_call(model, system_blocks, user_content, max_tokens=1024,
                         mock_fn=None):
    text = (system_blocks[0]["text"] if isinstance(system_blocks, list)
            and system_blocks and isinstance(system_blocks[0], dict)
            else str(system_blocks))
    call = {"model": model, "user": user_content, "max_tokens": max_tokens,
            "scorer": "against four gates" in text,
            "extractor": "You extract job opportunities" in text}
    CALLS.append(call)
    if MODEL["hook"] is not None:
        got = MODEL["hook"](call)
        if got is not None:
            return got
    return _real_claude_call(model, system_blocks, user_content,
                             max_tokens=max_tokens, mock_fn=mock_fn)


def says(text: str):
    return text, {"input_tokens": 900, "output_tokens": 300,
                  "cache_read_tokens": 0, "mode": "mock", "model": "mock",
                  "stop_reason": "end_turn"}


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
    prep.GUARD.reset()


def scorer_calls() -> list[dict]:
    return [c for c in CALLS if c["scorer"]]


# -------------------------------------------------------------- the fixtures

class Env:
    """One throwaway database and the helpers to seed and read it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.db = tmp / "quotes.sqlite"
        radar_common.get_db(self.db).close()

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def role(self, title="Head of Software", company="Veltrix Diagnostics",
             description=DESCRIPTION, **cols) -> int:
        jid = cols.pop("jid", None) or next(_ids)
        url = f"https://www.linkedin.com/comm/jobs/view/{jid}"
        # The real hash of the link, so that the inbox knows the advert again.
        row = {"url_hash": radar_common.url_hash(url),
               "first_seen": "2026-11-10T09:00:00Z",
               "last_seen": "2026-11-10T09:00:00Z", "status": "new",
               "thread_type": "inbound", "title": title, "company": company,
               "location": "Ghent, Belgium (Hybrid)", "salary_rate": "£800 per day",
               "source": "linkedin-alert", "source_url": url,
               "tier": "top", "cv_match": 80, "gate_sector": 1, "gate_cv": 1,
               "gate_location": 1, "gate_rate": 1, "failed_gates": "[]",
               "one_line_why": "Judged against the four gates.",
               "score_basis": "card", "prompt_version": OLD_VERSION,
               "score_model": "claude-sonnet-4-6",
               "scored_at": "2026-11-11T08:00:00Z",
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

    def rows(self) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute("SELECT * FROM opportunities ORDER BY id")]
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

    def apply(self, oid, when="2026-11-12T10:00:00Z") -> None:
        self.sql("UPDATE opportunities SET applied_at = ? WHERE id = ?", (when, oid))

    def prepare(self, oid):
        return prep.prepare(oid, self.db, CFG)

    def open_copy(self, oid):
        return prep.open_copy(oid, self.db, CFG)

    def rescore_copy(self, oid, text):
        return prep.rescore_copy(oid, text, self.db, CFG)

    def copy(self, oid) -> dict | None:
        c = self.conn()
        try:
            return tailored_cv.for_row(c, dict(c.execute(
                "SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone()))
        finally:
            c.close()


@contextlib.contextmanager
def fresh():
    reset()
    tmp = Path(tempfile.mkdtemp(prefix="radar-quotes-"))
    try:
        yield Env(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def pointed_at(db):
    saved = serve_dashboard.ARGS
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False,
                                              host="127.0.0.1", port=0)
    try:
        yield
    finally:
        serve_dashboard.ARGS = saved


def record(raw) -> dict:
    """The stored reasons, read back as they were written."""
    return json.loads(raw) if isinstance(raw, str) else raw


def page_text(html: str) -> str:
    """The words on a page, tags out and entities decoded."""
    import html as htmllib
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html)))


def reasons_record(reasons, scored_at="2026-11-11T08:00:00Z", basis="card",
                   dropped=0, gates=None) -> str:
    """A stored reasons record, as score_item.reasons_json writes one."""
    rec = {"v": 1, "scored_at": scored_at, "basis": basis,
           "reasons": reasons, "dropped": dropped}
    if gates is not None:
        rec["gates"] = gates
    return json.dumps(rec, ensure_ascii=False)


# The gates of the seeded roles, as Env.role stores them, and as the record of
# a score made with them carries them.
ALL_PASS = {"sector": 1, "cv": 1, "location": 1, "rate": 1}
GATES_MOVED_LINE = ("The gates have been judged again in code since these "
                    "reasons were given.")


def email_b64(subject: str, body: str) -> str:
    raw = json.dumps({"subject": subject, "from": "jobs-noreply@linkedin.com",
                      "date": "2026-11-14", "body_text": body, "body_html": ""})
    return base64.b64encode(raw.encode()).decode("ascii")


def run_inbox(db: Path, subject: str, body: str) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), captured_stderr():
        code = process_email.main(["--mock", "--db", str(db),
                                   "--b64", email_b64(subject, body)])
    return {"code": code, "json": json.loads(out.getvalue().strip().splitlines()[-1])}


def card_block(title, company="Veltrix Diagnostics",
               location="Ghent, Belgium (Hybrid)", pay="£900 per day") -> str:
    """One advert as an alert email lays it out for the mock extractor."""
    return (f"{title}\n{company} · {location}\n{pay}\n"
            f"https://www.linkedin.com/jobs/view/{next(_ids)}/\n\n")


# ------------------------------------------------------------- the prompt

def prompt_checks() -> None:
    whole = (REPO_ROOT / "prompts" / "scorer.md").read_text(encoding="utf-8")
    card, spec = score_item.split_rubric(whole)
    block = card[card.index("## The reasons\n"):card.index("## Voice\n")]
    flat = " ".join(block.split())
    missing = [n for n in ("Return up to four reasons",
                           "the facts in the advert that most decided the gates",
                           "the most decisive first",
                           "Each reason is one plain line of 20 words or fewer",
                           "Each rests on words in the advert",
                           "copied character for character, 25 words or fewer",
                           "Quote the advert only, never the CV text or the preferences file",
                           "do not add words to a quote, shorten inside it or correct it",
                           "The reasons come beside the gate results.",
                           "The voice rules below apply to each reason and never to its quote",
                           "a reason may give a fact that a gate note also gives.")
               if n not in flat]
    check(not missing,
          f"the card rubric asks for up to four reasons, the most decisive first, "
          f"each one plain line of 20 words or fewer, each with a quote of 25 "
          f"words or fewer copied from the advert and never from the CV, beside "
          f"the gate results, the voice rules on the reason and never on its "
          f"quote ({missing})")
    voice = card[card.index("## Voice\n"):]
    check(card.index("## The reasons\n") < card.index("## Voice\n")
          and "Notes are one plain line each" in voice and "Each fact appears once." in voice,
          "and the voice rules the reasons point to follow them, as they were")
    check(card.rstrip().endswith(
              ' "one_line_why": "", "question_text": "", "suggested_action": "", '
              '"act_by": "",\n "reasons": [{"reason": "", "quote": ""}]}'),
          "the card's reply ends with the reasons, each a reason and a quote")
    spec_block = spec[spec.index("### The reasons\n"):spec.index("### The shape for a full description")]
    check(" ".join(spec_block.split()) == "### The reasons The reasons are as above, "
          "and each quote is copied from the description, never from the card "
          "fields beside it."
          and "Return the same JSON as above, with asks and rate_quote added at "
              "the end of it." in " ".join(spec.split()),
          "a full description gives the same reasons, its quotes from the "
          "description and never from the card, in the same reply as before")
    # Everything else is the prompt Brief 3 left, byte for byte, once the
    # advert sentence of each ask (Prepare application final, 15 Nov) is
    # taken out as well.
    sentence_para = whole[whole.index("\nFor every ask, met, partly or missing, give sentence"):
                          whole.index("\ncv_match comes from this same evidence.")]
    part_b = whole.replace(sentence_para, "").replace(', "sentence": ""}],', '}],')
    check(radar_common.prompt_hash(part_b) == PART_B_VERSION,
          f"take the advert sentences out and the file is the one this change "
          f"left, {PART_B_VERSION}")
    old = (part_b.replace(block, "").replace(spec_block, "")
           .replace('"act_by": "",\n "reasons": [{"reason": "", "quote": ""}]}',
                    '"act_by": ""}'))
    check(radar_common.prompt_hash(old) == OLD_VERSION,
          f"take the reasons out and the file is the one Brief 3 left, "
          f"{OLD_VERSION}: nothing else the prompt asks for changed")
    version = radar_common.prompt_version()
    check(version == NEW_VERSION and version != OLD_VERSION
          and radar_common.prompt_hash(whole) == version,
          f"so the prompt version moves, from {OLD_VERSION} to {version}")
    with captured_stderr():
        blocks = score_item.build_scorer_system(CFG)
        spec_blocks = score_item.build_scorer_system(CFG, basis=SPEC)
    check("## The reasons" in blocks[0]["text"] and "### The reasons" not in blocks[0]["text"]
          and "### The reasons" in spec_blocks[0]["text"]
          and blocks.prompt_version == spec_blocks.prompt_version == version,
          "a card is sent the reasons above the marker and a full description "
          "both parts, and both are stamped with the new version")


# ------------------------------------------------------- the matching rule

def matching_checks() -> None:
    fv = radar_common.find_verbatim
    check(fv("head  of\nSOFTWARE", "Fractional Head of Software, IVD") == "Head of Software",
          "a quote in other case and spacing is found, and kept as the text writes it")
    check(fv("Head of Software", "Fractional Head of\n   Software") == "Head of Software",
          "a quote is found across a line break and a run of spaces in the text")
    check(fv("ISO 13485", "ISO 13485 throughout") == "ISO 13485"
          and fv("ISO 13485", "ISO 13485 throughout") == "ISO 13485",
          "a non-breaking space is white space like any other")
    check(fv("IEC 62304", "IEC-62304 lifecycle") is None
          and fv("Head of Software Engineering", "Head of Software") is None
          and fv("Head Software", "Head of Software") is None
          and fv("don't", "don’t stop") is None,
          "a hyphen is not a space, and a word added, a word dropped or a "
          "straightened apostrophe is not word for word")
    check(fv("", "anything") is None and fv("   ", "anything") is None
          and fv(None, "x") is None and fv("x", None) is None,
          "an empty quote is never found")
    check(fv("Ghent (Hybrid)", "Ghent (Hybrid), Belgium") == "Ghent (Hybrid)"
          and fv("c.o", "cxo") is None and fv("a+b", "aab") is None,
          "the characters of a regular expression are only themselves")
    whole = lambda q, t: fv(q, t, whole_words=True)  # noqa: E731
    check(whole("£65", "Day rate £650 per day") is None
          and whole("Senior Software Engineer", "Senior Software Engineering Manager") is None
          and whole("oftware Director", "Software Director") is None
          and whole("ead of", "Head of Software") is None
          and whole("a", "Head of Software") is None
          and whole("2304", "IEC 62304") is None,
          "with whole words, which the reasons ask for, a word or a figure cut short "
          "at either end is not found: £65 of £650, Engineer of Engineering, a "
          "letter inside a word, a figure inside a figure")
    check(whole("£650", "Day rate £650 per day") == "£650"
          and whole("800", "£800 per day") == "800"
          and whole("62304", "IEC-62304 lifecycle") == "62304"
          and whole("Ghent (Hybrid)", "Ghent (Hybrid), Belgium") == "Ghent (Hybrid)"
          and whole("head  of\nSOFTWARE", "Fractional Head of Software, IVD") == "Head of Software"
          and whole("per day,", "£650 per day, outside IR35") == "per day,",
          "while a whole word or figure is found, beside a currency sign, a hyphen, "
          "a bracket or a comma, in any case and spacing, as the text writes it")
    check(whole(".", "a. b") is None and whole("—", "x — y") is None
          and whole("(  )", "f ( ) g") is None and fv(".", "a. b") == ".",
          "a quote with no letter or digit in it is never found with whole words, "
          "though the pay line's rule, which does not ask for them, still finds it")
    check(fv("£65", "Day rate £650 per day") == "£65",
          "and the pay line's rule is as it was before the reasons, a stretch of "
          "the text found wherever it falls")
    line = prep.rate_line("Pay\n£750  to £850 per day, outside IR35.", "£750 to £850 PER DAY")
    check(line == {"quote": "£750 to £850 per day", "from": "model"},
          "the pay line of a full description goes by the same rule, as it did: "
          f"other case and spacing found, the advert's words kept ({line})")
    check(prep.rate_line("Pay\n£750 per day", "£999 per day")["from"] == "pattern"
          and prep.rate_line("Pay\n£750 per day", "x" * 300)["from"] == "pattern"
          and prep.rate_line("No pay named.", "")["from"] == "none",
          "and a pay quote not in the text, or too long, still goes to the "
          "pattern reader, and none is still none")
    opp = {"title": "Head of Software", "company": "Veltrix Diagnostics",
           "location": "Ghent", "salary_rate": "£800 per day",
           "source_url": "https://www.linkedin.com/jobs/view/1/",
           "posted_date": "2 days ago", "pay_min": 800.0}
    sources = score_item.quote_sources(opp)
    check(sources == ["Head of Software", "Veltrix Diagnostics", "Ghent", "£800 per day"],
          "a card's quotes are held to its title, company, location and pay "
          "text, field by field, as the scorer was given them")
    hold = score_item._hold_quote
    check(hold("Software Veltrix", sources) == (None, "not_found")
          and hold("Head of Software Veltrix Diagnostics", sources) == (None, "not_found")
          and hold("linkedin.com", sources) == (None, "not_found")
          and hold("2 days ago", sources) == (None, "not_found")
          and hold("800", sources) == ("800", None),
          "a quote never runs from one field into the next, and the link, the "
          "date and the numbers beside the card are not its words")
    cut = score_item.quote_sources({**opp, "title": "Senior Software Engineering Manager",
                                    "salary_rate": "Day rate £650 per day"})
    check(hold("£65", cut) == (None, "not_found")
          and hold("Senior Software Engineer", cut) == (None, "not_found")
          and hold("a", ["Head of Software"]) == (None, "not_found")
          and hold("£650 per day", cut) == ("£650 per day", None)
          and hold("Senior Software Engineering Manager", cut)
          == ("Senior Software Engineering Manager", None),
          "a reason's quote that cuts a word or a figure short is dropped as not "
          "found, £65 of £650 and Engineer of Engineering, and a whole one is kept")
    check(hold(".", ["a. b"]) == (None, "empty") and hold(" - ", ["x - y"]) == (None, "empty")
          and hold("“”", ["“”"]) == (None, "empty"),
          "and a quote with no letter or digit in it, only punctuation, is dropped "
          "as empty, though the text holds it")
    spec_sources = score_item.quote_sources({**opp, "description": "We want a leader."}, SPEC)
    check(spec_sources == ["We want a leader."]
          and hold("Head of Software", spec_sources) == (None, "not_found"),
          "a full description's quotes are held to the description alone, so a "
          "quote of the card beside it is not kept")


# --------------------------------------------------- checking the reasons

SOURCES = ["Fractional Software Director, IVD platform", "Veltrix Diagnostics",
           "Ghent, Belgium (Hybrid)", "£950 - £1,100 per day"]


def normalise_checks() -> None:
    norm = score_item.normalise_reasons
    got, dropped = norm([
        {"reason": "The title names an IVD platform", "quote": "software  DIRECTOR, ivd"},
        {"reason": "The CV evidences the regulated work", "quote": mocks.CV_QUOTE},
        {"reason": "Too long to keep", "quote": " ".join(["advert"] * 26)},
        {"reason": "Nothing quoted", "quote": ""},
    ], SOURCES)
    check([g["quote"] for g in got] == ["Software Director, IVD", None, None, None]
          and [g["quote_dropped"] for g in got] == [None, "not_found", "too_long", "empty"]
          and [g["reason"] for g in got] == ["The title names an IVD platform",
                                             "The CV evidences the regulated work",
                                             "Too long to keep", "Nothing quoted"]
          and dropped == 0,
          "a quote found is kept as the card writes it, and one that is not "
          "found, too long or empty is dropped with the drop on its reason, "
          "and every reason is kept")
    check(all(set(g) == {"reason", "quote", "quote_dropped"} for g in got)
          and all((g["quote"] is None) == (g["quote_dropped"] is not None) for g in got)
          and {g["quote_dropped"] for g in got} - {None} <= set(score_item.QUOTE_DROPS),
          "each reason holds its words, the quote or none, and why there is none, "
          "in one of the four words for a drop")
    long_text = " ".join(f"word{i}" for i in range(40))
    at_cap = " ".join(f"word{i}" for i in range(25))
    over = " ".join(f"word{i}" for i in range(26))
    check(norm([{"reason": "r", "quote": at_cap}], [long_text])[0][0]["quote"] == at_cap
          and norm([{"reason": "r", "quote": over}], [long_text])[0][0]["quote_dropped"] == "too_long",
          "a quote of 25 words is kept and one of 26 is not, though both are in "
          "the text")
    odd, odd_dropped = norm([
        {"reason": "A number", "quote": 950}, {"reason": "A list", "quote": ["Ghent"]},
        {"reason": "A mapping", "quote": {"q": "Ghent"}}, {"reason": "None", "quote": None},
    ], SOURCES)
    odder, odder_dropped = norm([{"reason": "Absent"}, {"reason": "Blank", "quote": "   "}],
                                SOURCES)
    check([g["quote_dropped"] for g in odd + odder]
          == ["not_text", "not_text", "not_text", "empty", "empty", "empty"]
          and odd_dropped == odder_dropped == 0 and len(odd + odder) == 6,
          "a quote that is not text is dropped as not text, and a missing or "
          "blank one as empty, each reason kept")
    bare, bare_dropped = norm(["The sector passes", "  "], SOURCES)
    check(bare == [{"reason": "The sector passes", "quote": None, "quote_dropped": "empty"}]
          and bare_dropped == 1,
          "a reason given as a bare string is kept with no quote, and a blank "
          "one is left out and counted")
    junk, junk_dropped = norm([42, None, [], {"quote": "Ghent"}, {"reason": 5, "quote": "Ghent"},
                               {"reason": "   ", "quote": "Ghent"}, True, 1.5], SOURCES)
    check(junk == [] and junk_dropped == 8,
          "an entry that is not a reason, or has no words for one, is left out "
          "and counted")
    six = [{"reason": f"Reason {n}", "quote": "Ghent"} for n in range(1, 7)]
    capped, cap_dropped = norm(six, SOURCES)
    check([g["reason"] for g in capped] == ["Reason 1", "Reason 2", "Reason 3", "Reason 4"]
          and cap_dropped == 2 and all(g["quote"] == "Ghent" for g in capped),
          "at most four reasons are kept, the first four, and the rest are counted")
    mixed, mixed_dropped = norm([42] + six, SOURCES)
    check(len(mixed) == 4 and mixed_dropped == 3,
          "a malformed entry does not take a place among the four")
    for field in ("the sector passes", {"reason": "one"}, 7, True, 2.5):
        check(norm(field, SOURCES) == ([], 1),
              f"a reasons field that is {type(field).__name__} and not a list "
              "keeps no reasons and counts one")
    check(norm(None, SOURCES) == ([], 0) and norm([], SOURCES) == ([], 0),
          "no reasons field, or an empty list, is no reasons and nothing dropped")
    voiced, _ = norm([{"reason": "Sector passes — IVD; strong fit!", "quote": "Ghent"},
                      {"reason": "x" * 600, "quote": "Ghent"}], SOURCES)
    check(voiced[0]["reason"] == "Sector passes, IVD. Strong fit."
          and len(voiced[1]["reason"]) == score_item.REASON_TEXT_MAX,
          "a reason goes through the voice rules like every free-text field, "
          "and is cut at 240 characters, and its quote is the card's own words")
    big, big_dropped = norm([{"reason": f"R{n}", "quote": "Ghent"} for n in range(1000)],
                            SOURCES)
    check(len(big) == 4 and big_dropped == 996,
          "a list of a thousand is four kept and the rest counted")
    raised = []
    for shape in ([{"reason": {"a": [1, {"b": None}]}, "quote": {"c": 1}}],
                  [[["deep"]]], [{"reason": "r", "quote": "q" * 200_000}],
                  [{"reason": "\u0000퟿\U0001f600", "quote": "\U0001f600"}],
                  [{"reason": "r", "quote": "(((["}], {"reasons": []}, "", 0):
        try:
            norm(shape, SOURCES)
            score_item.reasons_json(shape, SOURCES, CARD, "2026-11-14T10:00:00Z")
        except Exception as err:  # noqa: BLE001  the test is to see none
            raised.append(f"{type(err).__name__}: {err}")
    check(not raised, f"no shape of reply makes the check raise ({raised[:2]})")
    real = score_item.normalise_reasons

    def boom(raw, sources):
        raise RuntimeError("a fault in the check itself")

    score_item.normalise_reasons = boom
    try:
        held = record(score_item.reasons_json([{"reason": "r"}], SOURCES, CARD,
                                              "2026-11-14T10:00:00Z"))
    finally:
        score_item.normalise_reasons = real
    check(held == {"v": 1, "scored_at": "2026-11-14T10:00:00Z", "basis": CARD,
                   "reasons": [], "dropped": 1},
          "and should the check ever raise, the score keeps no reasons and "
          "counts one dropped, and is still a score")
    with captured_stderr():
        blocks = score_item.build_scorer_system(CFG)
    outcomes = {}
    for marker in ("RADAR-REASONS-BAD", "RADAR-REASONS-NONE", "RADAR-REASONS-MANY"):
        opp = {"company": "Veltrix Diagnostics", "title": f"Head of IVD Software {marker}",
               "location": "Ghent, Belgium", "salary_rate": "£900 per day",
               "pay_currency": "GBP", "pay_period": "day", "pay_min": 900.0,
               "pay_max": 900.0}
        scored, _usage, why = score_item.score_opportunity(
            opp, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS)
        rec = record((scored or {}).get("score_reasons"))
        outcomes[marker] = (scored is not None and why is None and scored["tier"] == "top",
                            len(rec["reasons"]), rec["dropped"])
    check(outcomes == {"RADAR-REASONS-BAD": (True, 0, 1),
                       "RADAR-REASONS-NONE": (True, 0, 0),
                       "RADAR-REASONS-MANY": (True, 4, 2)},
          f"a reply whose reasons are a string, missing or six is still a full "
          f"score with its tier, keeping none, none and four ({outcomes})")


# ---------------------------------------------------------- the card path

def card_path_checks() -> None:
    with fresh() as env:
        body = (card_block("Fractional Software Director, IVD platform")
                + card_block("Head of Software Engineering", company="Caldora Medical",
                             location="Cambridge, England (Hybrid)", pay="£95,000 per annum"))
        got = run_inbox(env.db, "2 new jobs", body)
        rows = env.rows()
        check(got["code"] == 0 and got["json"]["new"] == 2 and len(rows) == 2,
              "the inbox stores the two adverts of an alert email")
        good = []
        for row in rows:
            rec = record(row["score_reasons"])
            kinds = [(r["quote"], r["quote_dropped"]) for r in rec["reasons"]]
            good.append(rec["v"] == 1 and rec["scored_at"] == row["scored_at"]
                        and rec["basis"] == CARD and rec["dropped"] == 1
                        and kinds == [(row["title"], None), (row["location"], None),
                                      (None, "not_found"), (None, "too_long")]
                        and row["prompt_version"] == NEW_VERSION
                        and rec["gates"] == {name: row[f"gate_{name}"]
                                             for name in radar_common.GATE_NAMES})
        check(all(good) and len(good) == 2,
              "each new card score stores its reasons with it, dated with it and "
              "with its four gates as the row stores them: the title quoted as "
              "written, the location quoted in other case and spacing kept as the "
              "card writes it, the CV's words and a quote of 26 words dropped and "
              "said, and the malformed entry counted")
        check(not any(radar_common.reasons_gates_moved(
                          radar_common.reasons_record(r["score_reasons"], r["scored_at"]), r)
                      for r in rows)
              and GATES_MOVED_LINE not in page_text(jobs_row(rows[0])),
              "so a new score's gates are the ones its reasons were given beside, "
              "and its row says nothing about gates judged again")
        hist = env.history()
        check(len(hist) == 2 and all(h["event"] == "scored" and h["writer"] == "process_email"
                                     and h["reasons"] == r["score_reasons"]
                                     for h, r in zip(hist, rows)),
              "and the history keeps the same reasons with each first score")
        scorer = scorer_calls()
        extractor = [c for c in CALLS if c["extractor"]]
        check(len(scorer) == 2 and all(c["max_tokens"] == score_item.CARD_MAX_TOKENS == 2048
                                       for c in scorer)
              and extractor and all(c["max_tokens"] == score_item.MAX_TOKENS == 1024
                                    for c in extractor),
              "each card score is asked for at its own ceiling of 2,048, and the "
              "extraction at the 1,024 it always had")

        # A reply whose reasons cannot be read still stores its score.
        reset()
        odd = run_inbox(env.db, "1 new job",
                        card_block("Head of IVD Software RADAR-REASONS-BAD"))
        row = [r for r in env.rows() if "RADAR-REASONS-BAD" in (r["title"] or "")][0]
        rec = record(row["score_reasons"])
        check(odd["code"] == 0 and row["tier"] == "top" and row["cv_match"] is not None
              and rec["reasons"] == [] and rec["dropped"] == 1,
              "an advert whose reply sends its reasons as a string is stored with its "
              "score and tier, no reasons and one counted")

    # The scorer's reply names the role otherwise than the card, and the row
    # stores the reply's name. A quote of the card's title is not in it.
    with fresh() as env:
        def renames(call):
            if not call["scorer"]:
                return None
            reply = json.loads(mocks.mock_scorer(call["user"]))
            reply["role_title"] = reply["role_title"].replace(" (m/w/d)", "")
            return says(json.dumps(reply))

        MODEL["hook"] = renames
        got = run_inbox(env.db, "1 new job", card_block("Head of IVD Software (m/w/d)"))
        row = env.rows()[0]
        rec = record(row["score_reasons"])
        kinds = [(r["quote"], r["quote_dropped"]) for r in rec["reasons"]]
        hist = env.history(row["id"])
        out = run_kept_check(env.db)
        check(got["code"] == 0 and row["title"] == "Head of IVD Software"
              and kinds == [(None, "not_found"), ("Ghent, Belgium (Hybrid)", None),
                            (None, "not_found"), (None, "too_long")]
              and rec["reasons"][0]["reason"] == "The title is the role as the card names it"
              and hist and hist[-1]["reasons"] == row["score_reasons"],
              "a reply that renames the title stores the row under its name, and the "
              "quote of the card's title, not in the stored title, is dropped as not "
              "found with its reason kept, on the row and in the history")
        check(out.endswith("not in the stored text 0 rows whose description has "
                           "changed since 0") and "quotes kept 1" in out,
              f"so the kept quote check finds every quote kept in the stored text "
              f"({out[-120:]})")
        held = score_item.hold_reasons(reasons_record(
            [{"reason": "r", "quote": "Head of IVD Software (m/w/d)", "quote_dropped": None},
             {"reason": "s", "quote": "Ghent", "quote_dropped": None},
             {"reason": "t", "quote": None, "quote_dropped": "empty"}], gates=ALL_PASS),
            ["Head of IVD Software", "Veltrix Diagnostics", "Ghent, Belgium", ""])
        echoed = score_item.hold_reasons(reasons_record(
            [{"reason": "r", "quote": "Head of IVD Software", "quote_dropped": None}]),
            ["HEAD OF IVD  SOFTWARE", "", "", ""])
        check([(r["quote"], r["quote_dropped"]) for r in record(held)["reasons"]]
              == [(None, "not_found"), ("Ghent", None), (None, "empty")]
              and record(echoed)["reasons"][0]["quote"] == "Head of IVD Software"
              and record(held)["gates"] == ALL_PASS
              and score_item.hold_reasons(None, ["x"]) is None
              and score_item.hold_reasons("not json", ["x"]) is None
              and score_item.hold_reasons('{"v": 1}', ["x"]) is None,
              "holding stored reasons to other words keeps what they hold, in the "
              "card's characters and not the echo's, drops the rest as not found, "
              "leaves a drop already made and the gates as they were, and gives no "
              "reasons, never an error, for a record it cannot read")

    # A reply can carry half of a UTF-16 pair as an escape, which SQLite cannot
    # store. It costs neither the advert nor the run its record.
    with fresh() as env:
        def surrogate(call):
            if not call["scorer"]:
                return None
            reply = json.loads(mocks.mock_scorer(call["user"]))
            reply["reasons"] = [{"reason": "Rate \ud83d fails", "quote": ""}]
            reply["one_line_why"] = "Fits \udc00 well."
            return says(json.dumps(reply))

        MODEL["hook"] = surrogate
        with captured_stderr():
            got = run_inbox(env.db, "1 new job", card_block("Head of Software"))
        rows = env.rows()
        c = env.conn()
        runs = c.execute("SELECT COUNT(*) FROM runs WHERE workflow = 'inbox'").fetchone()[0]
        c.close()
        rec = record(rows[0]["score_reasons"]) if rows else {"reasons": []}
        check(got["code"] == 0 and len(rows) == 1 and rows[0]["tier"] == "top"
              and rec["reasons"] == [{"reason": "Rate \ufffd fails", "quote": None,
                                      "quote_dropped": "empty"}]
              and rows[0]["one_line_why"] == "Fits \ufffd well."
              and runs == 1 and env.history(rows[0]["id"]),
              "a reply whose reason and sentence carry a lone surrogate is stored "
              "with its score, the character replaced, and the run is logged")
        check(radar_common.sanitise_free_text("a\ud800b\udfffc") == "a\ufffdb\ufffdc"
              and radar_common.sanitise_free_text("pair \U0001f600 kept") == "pair \U0001f600 kept"
              and radar_common.sanitise_free_text(None) is None,
              "every free-text field loses a lone surrogate to the replacement "
              "character, and a whole character outside the basic plane is kept")

    # rescore.py writes the reasons with every score it makes.
    with fresh() as env:
        unscorable = ("RADAR-UNSCORABLE Director of Software\nNebulon Health · Ghent, "
                      "Belgium\n€900 per day\nhttps://example.invalid/nebulon-quotes\n")
        run_inbox(env.db, "1 new job", unscorable)
        first = env.rows()[0]
        check(first["tier"] is None and first["score_reasons"] is None
              and not env.history(first["id"]),
              "an advert the scorer could not read is stored unscored, with no reasons")
        conn = radar_common.get_db(env.db)
        with captured_stderr():
            fixed, failed = rescore.rescore_opportunities(conn, CFG, {})
        conn.close()
        after = env.row(first["id"])
        rec = record(after["score_reasons"])
        hist = env.history(first["id"])
        check(fixed == 1 and failed == 0 and after["tier"] is not None
              and rec["scored_at"] == after["scored_at"] and rec["basis"] == CARD
              and rec["reasons"][0]["quote"] == after["title"]
              and [h["event"] for h in hist] == ["scored"]
              and hist[-1]["writer"] == "rescore" and hist[-1]["reasons"] == after["score_reasons"],
              "the re-score of it writes its reasons, held to the card it stores, "
              "on the row and in the history")
        # A stale CV re-score replaces a score that had reasons of its own.
        old = reasons_record([{"reason": "An earlier reason", "quote": "Ghent",
                               "quote_dropped": None}], scored_at="2026-11-01T08:00:00Z")
        oid = env.role(title="Director of Software", company="Orbase Diagnostics",
                       combined=80, cv_version="cv-old.md", scored_at="2026-11-01T08:00:00Z",
                       score_reasons=old, description=None)
        reset()
        conn = radar_common.get_db(env.db)
        with captured_stderr():
            fixed, failed, _remaining = rescore.rescore_stale_cv(conn, CFG, {}, 5)
        conn.close()
        now = env.row(oid)
        hist = env.history(oid)
        check(fixed == 1 and now["scored_at"] != "2026-11-01T08:00:00Z"
              and record(now["score_reasons"])["scored_at"] == now["scored_at"]
              and record(now["score_reasons"])["reasons"][0]["reason"] != "An earlier reason"
              and [h["event"] for h in hist] == ["replaced", "scored"]
              and hist[0]["reasons"] == old and hist[1]["reasons"] == now["score_reasons"],
              "a stale CV re-score keeps the old score's reasons in the history "
              "with it, and puts the new score's on the row")


# ---------------------------------------------- the full description path

def spec_path_checks() -> None:
    with fresh() as env:
        oid = env.role()
        out = env.prepare(oid)
        row = env.row(oid)
        rec = record(row["score_reasons"])
        kinds = [(r["quote"], r["quote_dropped"]) for r in rec["reasons"]]
        check(out["outcome"] == "scored" and row["score_basis"] == SPEC
              and rec["scored_at"] == row["scored_at"] and rec["basis"] == SPEC
              and kinds == [("IEC 62304 software lifecycle in a Class C IVD", None),
                            ("Leadership of an FDA submission for a diagnostic", None),
                            (None, "not_found"), (None, "too_long")]
              and rec["dropped"] == 1,
              "the Prepare application click stores the reasons with its score, each "
              "quote held to the stored description: an ask as written, one in "
              "other case and spacing kept as the description writes it, the CV's "
              "words dropped, the description's own first thirty words dropped for "
              "length, and the entry with no reason counted")
        hist = env.history(oid)
        check([h["event"] for h in hist] == ["replaced", "scored"]
              and hist[0]["reasons"] is None and hist[0]["score_basis"] == CARD
              and hist[1]["reasons"] == row["score_reasons"] and hist[1]["writer"] == "prepare",
              "the card score it replaced goes to the history with no reasons, "
              "because it had none, and the new score with its own")
        spec = scorer_calls()
        check(len(spec) == 1 and spec[0]["max_tokens"] == score_item.SPEC_MAX_TOKENS == 8192,
              "one call, at the full description's ceiling of 8,192 (6,144 before "
              "each ask carried its advert sentence)")

    # A card score that had reasons keeps them in the history.
    with fresh() as env:
        card = reasons_record([{"reason": "The card said hybrid", "quote": "Ghent, Belgium (Hybrid)",
                                "quote_dropped": None}])
        oid = env.role(score_reasons=card)
        env.prepare(oid)
        hist = env.history(oid)
        check([h["event"] for h in hist] == ["replaced", "scored"]
              and hist[0]["reasons"] == card
              and record(env.row(oid)["score_reasons"])["basis"] == SPEC,
              "a card score with reasons that the click replaces is kept in the "
              "history with its reasons, and the row holds the description's")

    # On a full description, a quote of the card beside it is not the advert.
    with fresh() as env:
        oid = env.role(title="Principal Engineer, Firmware", company="Kappa Diagnostics")

        def card_quote(call):
            reply = json.loads(mocks.mock_scorer(call["user"]))
            reply["reasons"] = [{"reason": "The title says principal",
                                 "quote": "Principal Engineer, Firmware"},
                                {"reason": "The pay", "quote": "£750  TO £850 per day"}]
            return says(json.dumps(reply))

        MODEL["hook"] = card_quote
        env.prepare(oid)
        rec = record(env.row(oid)["score_reasons"])
        check([(r["quote"], r["quote_dropped"]) for r in rec["reasons"]]
              == [(None, "not_found"), ("£750 to £850 per day", None)],
              "a quote of the card's title is dropped on a full description, which "
              "does not hold it, and a quote of the description is kept")

    # A writer that knows nothing of the reasons cannot leave them looking current.
    with fresh() as env:
        oid = env.role()
        env.prepare(oid)
        spec_reasons = env.row(oid)["score_reasons"]
        env.sql("UPDATE opportunities SET score_basis = 'card', scored_at = ?,"
                " cv_match = 61 WHERE id = ?", ("2026-11-13T15:00:00Z", oid))
        c = radar_common.get_db(env.db)
        radar_common.archive_score(c, oid, "old-writer")
        radar_common.record_score(c, oid, "old-writer")
        c.commit()
        c.close()
        kept = [h for h in env.history(oid) if h["writer"] == "old-writer"]
        row = env.row(oid)
        html = build_dashboard.render_opportunity(row, {"serve": True}, 1, show_basis=True)
        check(kept and all(h["reasons"] is None for h in kept)
              and row["score_reasons"] == spec_reasons
              and any(h["reasons"] == spec_reasons for h in env.history(oid)
                      if h["writer"] == "prepare")
              and "Reasons" not in page_text(html),
              "a score that replaced one with reasons, by a writer that knows "
              "nothing of them, is filed with none, though the row still holds the "
              "old ones, and no page shows them beside it")


# ------------------------------------------------------- the tailored copy

def tailored_checks() -> None:
    with fresh() as env:
        a = env.role()
        b = env.role()
        env.open_copy(a)
        before = env.row(a)
        out = env.rescore_copy(a, COPY_TEXT)
        copy = env.copy(a)
        score = tailored_cv.read_score(copy)
        reasons = (score or {}).get("reasons") or {}
        kinds = [(r["quote"], r["quote_dropped"]) for r in reasons.get("reasons") or []]
        tailored = [h for h in env.history(a) if h["event"] == "tailored"]
        check(out["outcome"] == "scored" and reasons.get("scored_at") == score["scored_at"]
              and reasons.get("basis") == SPEC
              and kinds == [("IEC 62304 software lifecycle in a Class C IVD", None),
                            ("Leadership of an FDA submission for a diagnostic", None),
                            (None, "not_found"), (None, "too_long")],
              "Rescore role stores the tailored score's reasons on the copy, its "
              "quotes held to the stored description")
        check(len(tailored) == 1 and record(tailored[0]["reasons"]) == reasons,
              "and the history keeps them on the tailored score's row")
        check(env.row(a) == before,
              "while the role's own score, and its reasons, are not touched")
        env.rescore_copy(a, OTHER_TEXT)
        del CALLS[:]
        again = env.rescore_copy(b, COPY_TEXT)
        found = tailored_cv.read_score(env.copy(b))
        check(again["outcome"] == "reused" and not scorer_calls()
              and found["reasons"] == reasons,
              "a copy put back, from another advert of the role, finds its score in "
              "the history with its reasons, and nothing is spent")
        with pointed_at(env.db):
            c = sqlite3.connect(f"file:{env.db.as_posix()}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            try:
                inner = build_dashboard.render_prepare_inner(c, b, CFG)
            finally:
                c.close()
        tailored_html = inner[inner.index('id="tailored-score"'):]
        check('class="reasons"' in tailored_html
              and "“Leadership of an FDA submission for a diagnostic”" in page_text(tailored_html),
              "and the tailored copy's card on the Prepare page lists its reasons "
              "with their quotes")


# ---------------------------------------------------------------- migration

def columns(conn, table) -> list:
    return [dict(r) for r in conn.execute(f"PRAGMA table_info({table})")]


def migration_checks() -> None:
    with fresh() as env:
        old = env.tmp / "old.sqlite"
        radar_common.get_db(old).close()
        raw = sqlite3.connect(old)
        raw.execute("ALTER TABLE opportunities DROP COLUMN score_reasons")
        raw.execute("ALTER TABLE score_history DROP COLUMN reasons")
        raw.execute(
            "INSERT INTO opportunities (url_hash, first_seen, status, title, company,"
            " tier, cv_match, one_line_why, score_basis, prompt_version, score_model,"
            " scored_at) VALUES ('old-card', '2026-11-01T00:00:00Z', 'new', 'Old role',"
            " 'Oldco', 'top', 77, 'Written before.', 'card', '97f11cce', 'm',"
            " '2026-11-01T00:00:00Z')")
        raw.execute(
            "INSERT INTO opportunities (url_hash, first_seen, status, title, company,"
            " tier, cv_match, one_line_why, score_basis, prompt_version, score_model,"
            " scored_at, description_text, description_hash, score_working) VALUES"
            " ('old-spec', '2026-11-02T00:00:00Z', 'new', 'Old spec role', 'Oldco',"
            " 'question', 70, 'Also before.', 'spec', '97f11cce', 'm',"
            " '2026-11-12T00:00:00Z', 'A description.', 'abcdefabcdef', '{}')")
        raw.execute(
            "INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
            " cv_match, tier, prompt_version) VALUES (1, '2026-11-01T00:00:00Z',"
            " 'scored', 'process_email', 77, 'top', '97f11cce')")
        raw.commit()
        raw.row_factory = sqlite3.Row
        gone = ("score_reasons" not in [c["name"] for c in columns(raw, "opportunities")]
                and "reasons" not in [c["name"] for c in columns(raw, "score_history")])
        opp_before = [dict(r) for r in raw.execute("SELECT * FROM opportunities ORDER BY id")]
        hist_before = [dict(r) for r in raw.execute("SELECT * FROM score_history ORDER BY id")]
        raw.close()
        check(gone, "the fixture is shaped like a database from before this change")
        base = env.tmp / "base.sqlite"
        shutil.copyfile(old, base)

        reset()
        conn = radar_common.get_db(old)
        opp_cols, hist_cols = columns(conn, "opportunities"), columns(conn, "score_history")
        added = [c for c in opp_cols if c["name"] == "score_reasons"] + \
                [c for c in hist_cols if c["name"] == "reasons"]
        check(len(added) == 2 and all(c["type"] == "TEXT" and c["notnull"] == 0
                                      and c["dflt_value"] is None for c in added)
              and opp_cols[-1]["name"] == "score_reasons" and hist_cols[-1]["name"] == "reasons",
              "the first open adds score_reasons and the history's reasons, TEXT, "
              "nullable with no default, at the end of their tables")
        opp_after = [dict(r) for r in conn.execute("SELECT * FROM opportunities ORDER BY id")]
        hist_after = [dict(r) for r in conn.execute("SELECT * FROM score_history ORDER BY id")]
        check([{k: v for k, v in r.items() if k != "score_reasons"} for r in opp_after] == opp_before
              and all(r["score_reasons"] is None for r in opp_after)
              and [{k: v for k, v in r.items() if k != "reasons"} for r in hist_after] == hist_before
              and all(r["reasons"] is None for r in hist_after),
              "every row that was there is as it was, card and full description "
              "alike, and its new cell is NULL")
        snapshot = "\n".join(conn.iterdump())
        conn.close()
        conn = radar_common.get_db(old)
        check("\n".join(conn.iterdump()) == snapshot, "opening it again changes nothing")
        conn.close()
        check(not CALLS, "and no model was asked anything while it opened")

        errors: list[str] = []
        for round_ in range(4):
            race = env.tmp / f"race{round_}.sqlite"
            shutil.copyfile(base, race)
            barrier = threading.Barrier(6)

            def opener(path=race):
                try:
                    barrier.wait(timeout=10)
                    radar_common.get_db(path).close()
                except Exception as err:  # noqa: BLE001  the test is to see none
                    errors.append(f"{type(err).__name__}: {err}")

            threads = [threading.Thread(target=opener) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            c = sqlite3.connect(race)
            c.row_factory = sqlite3.Row
            names = [x["name"] for x in columns(c, "opportunities")] + \
                    [x["name"] for x in columns(c, "score_history")]
            c.close()
            if names.count("score_reasons") != 1 or names.count("reasons") != 1:
                errors.append(f"round {round_}: {names.count('score_reasons')} and "
                              f"{names.count('reasons')} columns")
        check(not errors, "six processes opening the old file at the same moment all "
                          f"succeed and add each column once, four times over ({errors[:2]})")


# ------------------------------------------------------- nothing re-scored

def sql_statements():
    """(file, text) for each maximal string expression in the scripts, the way
    test/run_applied_test.py reads statements, so a statement written as
    adjacent literals or put together with + is read as one."""
    import ast
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree)
                   for child in ast.iter_child_nodes(node)}

        def textual(n) -> bool:
            return (isinstance(n, ast.JoinedStr)
                    or (isinstance(n, ast.Constant) and isinstance(n.value, str))
                    or (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add)
                        and (textual(n.left) or textual(n.right))))

        for n in ast.walk(tree):
            if not textual(n):
                continue
            parent = parents.get(n)
            if isinstance(parent, ast.BinOp) and textual(parent):
                continue
            yield path.name, ast.unparse(n)


def no_rescore_checks() -> None:
    with fresh() as env:
        card = env.role(title="Director A", company="Alpha Diagnostics", description=None)
        spec = env.role(title="Director B", company="Beta Diagnostics",
                        score_basis="spec", scored_at="2026-11-12T08:00:00Z",
                        description_hash=radar_common.description_version(DESCRIPTION))
        unchanged = env.role(title="Director C", company="Gamma Diagnostics",
                             description=None, tier="question", gate_rate=0,
                             failed_gates='["rate"]', salary_rate="£500 per day",
                             pay_currency="GBP", pay_period="day", pay_min=500.0,
                             pay_max=500.0)
        before = env.dump()
        with pointed_at(env.db):
            jobs = serve_dashboard.render("jobs").decode("utf-8")
        c = sqlite3.connect(f"file:{env.db.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            pages = [build_dashboard.render_prepare_inner(c, oid, CFG)
                     for oid in (card, spec, unchanged)]
        finally:
            c.close()
        check(env.dump() == before and not CALLS
              and 'class="reasons"' not in jobs
              and not any('class="reasons"' in p for p in pages),
              "the Jobs page and the Prepare pages of roles scored before the reasons "
              "show none, write nothing and ask nothing")
        out = env.prepare(spec)
        check(out["outcome"] == "reused" and not CALLS and env.dump() == before,
              "a role already scored on its description is not scored again to "
              "give it reasons")
        conn = radar_common.get_db(env.db)
        with captured_stderr():
            res = rescore.rescore_opportunities(conn, CFG, {})
        conn.close()
        check(res == (0, 0) and not CALLS and env.dump() == before,
              "the re-score finds nothing to do on rows that are scored, reasons "
              "or not, and asks nothing")
        conn = radar_common.get_db(env.db)
        dry = regate_rates.regate(conn, CFG, FLOORS, write=False)
        conn.close()
        check(not CALLS and env.dump() == before and dry["mode"] == "dry-run",
              "a regate dry run reads and writes nothing")
        jid = check_jobs.job_view_id(env.row(card)["source_url"])
        email = card_block("Director A", company="Alpha Diagnostics").replace(
            "\n\n", "").rsplit("\n", 1)[0]
        dup = run_inbox(env.db, "Seen again", f"{email}\n"
                        f"https://www.linkedin.com/comm/jobs/view/{jid}\n")
        check(dup["json"]["duplicates"] == 1 and not scorer_calls()
              and env.row(card)["score_reasons"] is None,
              "an advert seen again in the inbox is a sighting, and no score is "
              "asked for to give it reasons")
        writers = sorted({name for name, text in sql_statements()
                          if re.search(r"\b(UPDATE|INSERT OR IGNORE INTO|INSERT INTO)"
                                       r"\s+opportunities\b", text)
                          and "score_reasons" in text})
        check(writers == ["prepare_application.py", "process_email.py", "rescore.py"],
              f"only the three writers that make a score write the reasons beside it, "
              f"in the statement that writes the score, and no migration does ({writers})")
        mig = (REPO_ROOT / "scripts" / "radar_common.py").read_text(encoding="utf-8")
        body = mig[mig.index("def _migrate("):mig.index("LIVE_WINDOW_DAYS")]
        check("reasons" not in body,
              "the migration names no reasons: it adds the columns through "
              "_MIGRATIONS and fills nothing")

    # A regate re-tiers by code and keeps the reasons the score gave, with the
    # gates they were given beside, and the pages say the gates have moved.
    with fresh() as env:
        rec = reasons_record([{"reason": "The rate", "quote": "£500 per day",
                               "quote_dropped": None}], gates=ALL_PASS)
        oid = env.role(title="Director D", company="Delta Diagnostics", description=None,
                       salary_rate="£500 per day", pay_currency="GBP", pay_period="day",
                       pay_min=500.0, pay_max=500.0, score_reasons=rec)
        # No stored figure, so the regate leaves its gates as the score gave them.
        steady = env.role(title="Director F", company="Phi Diagnostics", description=None,
                          score_reasons=reasons_record(
                              [{"reason": "The rate", "quote": "£800 per day",
                                "quote_dropped": None}], gates=ALL_PASS))
        conn = radar_common.get_db(env.db)
        res = regate_rates.regate(conn, CFG, FLOORS, write=True)
        conn.close()
        row = env.row(oid)
        hist = env.history(oid)
        check(res["rows_changed"] == 1 and row["gate_rate"] == 0
              and row["score_reasons"] == rec and row["scored_at"] == "2026-11-11T08:00:00Z"
              and [h["event"] for h in hist] == ["replaced"] and hist[0]["reasons"] == rec
              and record(hist[0]["reasons"])["gates"] == ALL_PASS
              and json.loads(hist[0]["gates"]) == ALL_PASS and not CALLS,
              "a regate that fails the rate gate in code keeps the reasons on the "
              "row with the score's date and the gates they came with, keeps them in "
              "the history row it writes first beside those gates, and asks no model")
        moved = page_text(jobs_row(row))
        c = sqlite3.connect(f"file:{env.db.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            prepare_page = page_text(build_dashboard.render_prepare_inner(c, oid, CFG))
            steady_page = page_text(build_dashboard.render_prepare_inner(c, steady, CFG))
        finally:
            c.close()
        check("Failed rate." in moved and "The rate. “£500 per day”" in moved
              and GATES_MOVED_LINE in moved
              and moved.index("Gates") < moved.index("Reasons") < moved.index(GATES_MOVED_LINE)
              and GATES_MOVED_LINE in prepare_page,
              "so the Jobs row shows the failed rate gate, then the reasons with one "
              "line saying the gates have been judged again in code since they were "
              "given, and the Prepare page says so too")
        check(GATES_MOVED_LINE not in page_text(jobs_row(env.row(steady)))
              and GATES_MOVED_LINE not in steady_page
              and "“£800 per day”" in steady_page,
              "while a role the regate left as it was shows its reasons with no such line")

    # The page checker gives a card that stated no pay the page's figure, and
    # re-tiers it in code, which is the routine way the gates move.
    with fresh() as env:
        rec = reasons_record([
            {"reason": "Rate unstated, that's your first question", "quote": None,
             "quote_dropped": "empty"},
            {"reason": "Fully remote, so the location passes", "quote": "Remote",
             "quote_dropped": None}], gates=ALL_PASS)
        oid = env.role(title="Director E", company="Epsilon Diagnostics", description=None,
                       location="Remote", salary_rate="", rate_band="unstated",
                       score_reasons=rec)
        conn = radar_common.get_db(env.db)
        row = dict(conn.execute("SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone())
        page = {"description": "A role. Day rate £400 per day.", "employment_type": "",
                "pay": {"currency": "GBP", "period": "day", "min": 400.0, "max": 400.0,
                        "text": "£400 per day"}}
        out = check_jobs.apply_page(conn, row, page, FLOORS, CFG, write=True)
        conn.commit()
        conn.close()
        after = env.row(oid)
        text = page_text(jobs_row(after))
        check((out.get("tier") or {}).get("to") == "question" and after["gate_rate"] == 0
              and after["scored_at"] == row["scored_at"] and after["score_reasons"] == rec
              and "Failed rate." in text and GATES_MOVED_LINE in text
              and "Rate unstated, that's your first question." in text and not CALLS,
              "a page checker re-tier that fails the rate gate on the page's pay keeps "
              "the reasons, and the Jobs row shows them under the failed gate with the "
              "line that says the gates have been judged again since")


# ------------------------------------------------------------ applied roles

def applied_checks() -> None:
    with fresh() as env:
        oid = env.role()
        env.apply(oid)
        before = env.dump()
        out = env.prepare(oid)
        out_copy = env.rescore_copy(oid, COPY_TEXT)
        conn = radar_common.get_db(env.db)
        with captured_stderr():
            res = rescore.rescore_opportunities(conn, CFG, {})
        conn.close()
        check(out["outcome"] == "applied" and out_copy["outcome"] == "applied"
              and res == (0, 0) and not CALLS and env.dump() == before,
              "an applied role is not scored, not given reasons and not written, by "
              "the click, by Rescore role or by the re-score")
    with fresh() as env:
        # Unscored as the inbox stores an advert it could not score: no
        # gates, no tier, no sentence and no record of a score.
        oid = env.role(tier=None, cv_match=None, gate_sector=None, gate_cv=None,
                       gate_location=None, gate_rate=None, failed_gates=None,
                       one_line_why=None, scored_at=None, score_basis=None,
                       prompt_version=None, score_model=None, description=None)
        env.apply(oid)
        before = env.dump()
        conn = radar_common.get_db(env.db)
        with captured_stderr():
            res = rescore.rescore_opportunities(conn, CFG, {})
        conn.close()
        check(res == (0, 0) and not CALLS and env.dump() == before,
              "an applied role left unscored is not picked by the re-score either")
    # Applied for while the model answers.
    with fresh() as env:
        oid = env.role()
        before = env.row(oid)

        def apply_meanwhile(call):
            env.apply(oid)
            return None

        MODEL["hook"] = apply_meanwhile
        out = env.prepare(oid)
        after = env.row(oid)
        check(out["outcome"] == "applied" and len(scorer_calls()) == 1
              and {k: v for k, v in after.items() if k != "applied_at"}
              == {k: v for k, v in before.items() if k != "applied_at"}
              and after["score_reasons"] is None and not env.history(oid),
              "a role applied for while the model answers keeps its score and gets "
              "no reasons, and nothing goes to the history")
    # A repost of an applied role arrives.
    with fresh() as env:
        oid = env.role(title="Fractional Software Director, IVD platform",
                       company="Veltrix Diagnostics", description=None)
        env.apply(oid)
        before = env.row(oid)
        run_inbox(env.db, "1 new job", card_block("Fractional Software Director, IVD platform"))
        rows = env.rows()
        repost = [r for r in rows if r["id"] != oid]
        check(env.row(oid) == before and len(repost) == 1
              and radar_common.role_is_applied(sqlite3.connect(env.db), repost[0]["id"]),
              "a repost of an applied role is stored as a row of its own, as it always "
              "was, and the applied row is not touched")


# ------------------------------------------------------------------- pages

def jobs_row(row: dict, show=True) -> str:
    base = {"id": 7, "company": "Veltrix Diagnostics", "title": "Fractional Director",
            "location": "Ghent", "tier": "top", "cv_match": 93, "failed_gates": "[]",
            "one_line_why": "Fits.", "first_seen": "2026-11-01T09:00:00Z",
            "last_seen": "2026-11-09T09:00:00Z", "rate_band": "above",
            "source_url": "https://example.invalid/a", "score_basis": "card",
            "scored_at": "2026-11-11T08:00:00Z"}
    return build_dashboard.render_opportunity({**base, **row}, {"serve": True}, 1,
                                              show_basis=show)


def page_checks() -> None:
    rec = reasons_record([
        {"reason": "The title names an IVD platform", "quote": "Fractional Director",
         "quote_dropped": None},
        {"reason": "The CV evidences it", "quote": None, "quote_dropped": "not_found"},
        {"reason": "<script>alert(1)</script> & co", "quote": "<b>Ghent</b> & \"more\"",
         "quote_dropped": None}])
    html = jobs_row({"score_reasons": rec})
    text = page_text(html)
    check("Reasons" in text
          and "The title names an IVD platform. “Fractional Director”" in text
          and "The CV evidences it." in text,
          "the expanded Jobs row lists each reason as a sentence with its quote in "
          "quotation marks, and a reason whose quote was dropped alone")
    check("<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in html
          and "&lt;b&gt;Ghent&lt;/b&gt; &amp; &quot;more&quot;" in html,
          "the reason and the quote are both escaped")
    lowered = html.lower()
    check(not any(w in lowered for w in ("not_found", "not found", "dropped", "too_long",
                                         "too long", "quote_dropped")),
          "and nothing on the row says a quote was dropped beyond the reason itself")
    check(text.index("Gates") < text.index("Reasons") < text.index("Scored on"),
          "the reasons sit under the gates and above the line on how the score was made")
    check("Reasons" not in page_text(jobs_row({"score_reasons": None}))
          and "Reasons" not in page_text(jobs_row({"score_reasons": rec,
                                                   "scored_at": "2026-11-12T08:00:00Z"}))
          and "Reasons" not in page_text(jobs_row({"score_reasons": "not json"}))
          and "Reasons" not in page_text(jobs_row({"score_reasons": reasons_record([])})),
          "a score with no reasons, reasons made with another score, a record that "
          "cannot be read and an empty one show nothing")
    check("Reasons" not in page_text(jobs_row({"score_reasons": rec}, show=False)),
          "and a row drawn for any page but Jobs shows none")
    other_version = json.dumps({**record(rec), "v": 2})
    no_version = json.dumps({k: v for k, v in record(rec).items() if k != "v"})
    check(record(rec)["v"] == 1
          and "Reasons" in page_text(jobs_row({"score_reasons": rec}))
          and "Reasons" not in page_text(jobs_row({"score_reasons": other_version}))
          and "Reasons" not in page_text(jobs_row({"score_reasons": no_version}))
          and radar_common.score_reasons(other_version, "2026-11-11T08:00:00Z") == []
          and radar_common.score_reasons(no_version, "2026-11-11T08:00:00Z") == [],
          "a record of another version, or of none, is not read: the row shows no "
          "reasons for it")
    with fresh() as env:
        held = {}
        for name, raw in (("v1", rec), ("v2", other_version), ("none", no_version)):
            oid = env.role(score_reasons=raw, description=None)
            c = radar_common.get_db(env.db)
            try:
                held[name] = radar_common._held_score(c, oid)["reasons"]
                radar_common.archive_score(c, oid, "version-check")
                c.commit()
            finally:
                c.close()
            filed = [h["reasons"] for h in env.history(oid) if h["writer"] == "version-check"]
            held[name + " filed"] = filed
    seen = sorted(name for name, value in held.items() if value not in (None, [None]))
    check(held == {"v1": rec, "v1 filed": [rec], "v2": None, "v2 filed": [None],
                   "none": None, "none filed": [None]},
          "and the history files none for it, where a record of this version is "
          f"filed with its score (filed with reasons: {seen})")
    gated = {"gate_sector": 1, "gate_cv": 1, "gate_location": 1, "gate_rate": 1}
    check(GATES_MOVED_LINE not in page_text(jobs_row(
              {"score_reasons": reasons_record(record(rec)["reasons"], gates=ALL_PASS), **gated}))
          and GATES_MOVED_LINE in page_text(jobs_row(
              {"score_reasons": reasons_record(record(rec)["reasons"], gates=ALL_PASS),
               **gated, "gate_rate": 0, "failed_gates": '["rate"]', "tier": "question"}))
          and GATES_MOVED_LINE in page_text(jobs_row(
              {"score_reasons": reasons_record(record(rec)["reasons"],
                                               gates={**ALL_PASS, "rate": None}), **gated}))
          and GATES_MOVED_LINE not in page_text(jobs_row({"score_reasons": rec, **gated,
                                                          "gate_rate": 0})),
          "the line on gates judged again shows when a gate the row holds is not the "
          "one the reasons came with, a pass become a fail or an unknown become a "
          "pass, never when they are the same, and never for a record without gates")
    replaced = jobs_row({"score_reasons": reasons_record(
        [{"reason": "The first ask", "quote": "IEC 62304 software lifecycle",
          "quote_dropped": None}], basis=SPEC),
        "score_basis": SPEC, "description_text": "A new advert.",
        "description_hash": radar_common.description_version(DESCRIPTION)})
    check("The first ask." in page_text(replaced)
          and "IEC 62304 software lifecycle" not in page_text(replaced)
          and "The quotes are not shown, because the stored description has "
              "changed since this score." in page_text(replaced),
          "a full description score whose description has been replaced since "
          "shows its reasons without the quotes, and says why")
    held = jobs_row({"score_reasons": reasons_record(
        [{"reason": "The first ask", "quote": "IEC 62304 software lifecycle",
          "quote_dropped": None}], basis=SPEC),
        "score_basis": SPEC, "description_text": DESCRIPTION,
        "description_hash": radar_common.description_version(DESCRIPTION)})
    check("“IEC 62304 software lifecycle”" in page_text(held)
          and "are not shown" not in page_text(held),
          "while one on the description it was made on shows them")

    with fresh() as env:
        run_inbox(env.db, "1 new job", card_block("Fractional Software Director, IVD platform"))
        card = env.rows()[0]["id"]
        spec = env.role(title="Head of Software", company="Theta Diagnostics")
        env.prepare(spec)
        # The reasons are in the role pane the Jobs page draws for each role.
        with pointed_at(env.db):
            jobs = "".join(serve_dashboard.render_jobs(oid, {})[1].decode("utf-8")
                           for oid in (card, spec))
            archive = serve_dashboard.render("archive").decode("utf-8")
        jt = page_text(jobs)
        check(jobs.count('class="reasons"') == 2
              and "“Fractional Software Director, IVD platform”" in jt
              and "“Ghent, Belgium (Hybrid)”" in jt
              and "“IEC 62304 software lifecycle in a Class C IVD”" in jt,
              "today's Jobs page lists the reasons of a card score and of a full "
              "description score, each with its quotes")
        check('class="reasons"' not in archive,
              "and the archive page, which draws the same rows, does not")
        c = sqlite3.connect(f"file:{env.db.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            card_page = build_dashboard.render_prepare_inner(c, card, CFG)
            spec_page = build_dashboard.render_prepare_inner(c, spec, CFG)
        finally:
            c.close()
        master = spec_page[spec_page.index("score-master"):spec_page.index('id="tailored-score"')]
        mt = page_text(master)
        check('class="reasons"' in master
              and "The first ask is the heart of the role. “IEC 62304 software "
                  "lifecycle in a Class C IVD”" in mt
              and "The CV evidences the regulated work." in mt
              and mocks.CV_QUOTE not in mt,
              "the Prepare page lists the master CV score's reasons with their quotes "
              "from the description, and a dropped quote is not shown")
        cm = card_page[card_page.index("score-master"):card_page.index('id="tailored-score"')]
        check("“Fractional Software Director, IVD platform”" in page_text(cm),
              "and for a role scored on its card, the card's quotes")
        env.sql("UPDATE opportunities SET score_reasons = ? WHERE id = ?",
                (reasons_record([{"reason": "<i>x</i>", "quote": "Theta</p><p>",
                                  "quote_dropped": None}],
                                scored_at=env.row(spec)["scored_at"], basis=SPEC), spec))
        c = sqlite3.connect(f"file:{env.db.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            escaped = build_dashboard.render_prepare_inner(c, spec, CFG)
        finally:
            c.close()
        check("<i>x</i>" not in escaped and "&lt;i&gt;x&lt;/i&gt;" in escaped
              and "Theta&lt;/p&gt;&lt;p&gt;" in escaped,
              "and escapes them there too")
        env.sql("UPDATE opportunities SET description_text = ? WHERE id = ?",
                ("A new advert.\n- Kubernetes", spec))
        c = sqlite3.connect(f"file:{env.db.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            moved = page_text(build_dashboard.render_prepare_inner(c, spec, CFG))
        finally:
            c.close()
        check("Theta</p><p>" not in moved
              and "The quotes are not shown, because the stored description has "
                  "changed since this score." in moved,
              "and once the page checker has stored another description, the quotes "
              "of the one before are not drawn beside it")


# ------------------------------------------------------ the queries for the box

APPLIED_RULE = ("(applied_at IS NOT NULL OR (role_key IS NOT NULL AND role_key IN"
                " (SELECT role_key FROM opportunities"
                " WHERE applied_at IS NOT NULL AND role_key IS NOT NULL)))")
ROW_COUNTS = ("SELECT COUNT(*) AS roles, SUM(expired_at IS NOT NULL) AS expired,"
              " SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS"
              " dismissed, SUM(applied_at IS NOT NULL) AS applied, (SELECT COUNT(*) FROM"
              " score_history) AS history_rows FROM opportunities;")
OLD_SCORES = ("SELECT COUNT(*) AS scored_rows, COUNT(DISTINCT prompt_version) AS"
              " prompt_versions, SUM(cv_match) AS cv_sum, SUM(LENGTH(one_line_why)) AS"
              " why_chars, MAX(scored_at) AS newest_score FROM opportunities WHERE tier IS"
              " NOT NULL AND COALESCE(prompt_version, '') <> '" + PART_B_VERSION + "';")
APPLIED_LIST = ("SELECT id, applied_at, score_basis, prompt_version, scored_at, tier,"
                " cv_match FROM opportunities WHERE applied_at IS NOT NULL ORDER BY id;")
APPLIED_ROWS = ("SELECT COUNT(*) AS rows_on_applied_roles, SUM(cv_match) AS cv_sum,"
                " MAX(scored_at) AS newest_score, SUM(score_reasons IS NOT NULL) AS"
                " with_reasons FROM opportunities WHERE " + APPLIED_RULE + ";")
APPLIED_ROWS_BEFORE = ("SELECT COUNT(*) AS rows_on_applied_roles, SUM(cv_match) AS cv_sum,"
                       " MAX(scored_at) AS newest_score FROM opportunities WHERE "
                       + APPLIED_RULE + ";")


def tally(table: str, column: str, group: str) -> str:
    return ("SELECT " + group + ", SUM(t.key = 'reason') AS reasons,"
            " SUM(t.key = 'quote' AND t.type = 'text') AS quotes_kept,"
            " SUM(t.key = 'quote_dropped' AND t.atom = 'not_found') AS not_found,"
            " SUM(t.key = 'quote_dropped' AND t.atom = 'too_long') AS too_long,"
            " SUM(t.key = 'quote_dropped' AND t.atom = 'empty') AS empty,"
            " SUM(t.key = 'quote_dropped' AND t.atom = 'not_text') AS not_text,"
            " SUM(CASE WHEN t.key = 'dropped' THEN t.atom ELSE 0 END) AS entries_left_out"
            " FROM " + table + ", json_tree(" + column + ") t WHERE " + column
            + " IS NOT NULL GROUP BY " + ", ".join(str(n + 1) for n in range(group.count(",") + 1))
            + " ORDER BY " + ", ".join(str(n + 1) for n in range(group.count(",") + 1)) + ";")


BEFORE_QUERIES = {
    "SQLite can read the reasons when they come (expect json_ok 1)":
        "SELECT sqlite_version() AS sqlite, json_valid('[]') AS json_ok;",
    "Brief 4 is on the box, so the pull brings only this change (expect 1 and 1)":
        "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name ="
        " 'tailored_cvs') AS copies_table, (SELECT COUNT(*) FROM"
        " pragma_table_info('score_history') WHERE name = 'cv_hash') AS history_cv_hash;",
    "the reasons columns are not there yet (expect 0 and 0)":
        "SELECT (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name ="
        " 'score_reasons') AS on_opportunities, (SELECT COUNT(*) FROM"
        " pragma_table_info('score_history') WHERE name = 'reasons') AS on_history;",
    "row counts and history rows, to compare after": ROW_COUNTS,
    "the scores held now, to compare after": OLD_SCORES,
    "scores by basis and prompt version, to compare after":
        "SELECT COALESCE(score_basis, 'unscored') AS basis, COALESCE(prompt_version,"
        " 'none') AS prompt, COUNT(*) AS roles FROM opportunities GROUP BY 1, 2 ORDER BY 1, 2;",
    "applied roles and the score each holds, to compare after": APPLIED_LIST,
    "every row of an applied role, to compare after": APPLIED_ROWS_BEFORE,
    "what scoring cost in the last seven days, to compare with the week after":
        "SELECT workflow, COUNT(*) AS runs, SUM(items_new) AS new_items,"
        " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens FROM"
        " runs WHERE ts >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days') AND workflow"
        " IN ('inbox', 'prepare', 'rescore') GROUP BY 1 ORDER BY 1;",
}

AFTER_QUERIES = {
    "the file is sound": "PRAGMA integrity_check;",
    "row counts and history rows, the same query as before the deploy": ROW_COUNTS,
    "the two columns are there, TEXT, nullable and with no default (expect two rows,"
    " notnull 0 and dflt_value empty)":
        "SELECT 'opportunities' AS in_table, * FROM pragma_table_info('opportunities')"
        " WHERE name = 'score_reasons' UNION ALL SELECT 'score_history', * FROM"
        " pragma_table_info('score_history') WHERE name = 'reasons';",
    "no score carries reasons yet (expect 0 and 0)":
        "SELECT (SELECT COUNT(*) FROM opportunities WHERE score_reasons IS NOT NULL) AS"
        " rows_with_reasons, (SELECT COUNT(*) FROM score_history WHERE reasons IS NOT"
        " NULL) AS history_with_reasons;",
    "the scores held, the same numbers as before the deploy": OLD_SCORES,
    "applied roles and the score each holds, the same as before": APPLIED_LIST,
    "every row of an applied role, the same as before and with no reasons": APPLIED_ROWS,
}

NEW_QUERIES = {
    "new scores by basis (expect with_reasons equal to scores on each line)":
        "SELECT score_basis AS basis, COUNT(*) AS scores, SUM(score_reasons IS NOT NULL)"
        " AS with_reasons FROM opportunities WHERE prompt_version = '" + PART_B_VERSION
        + "' GROUP BY 1 ORDER BY 1;",
    "every new score carries the reasons made with it (expect 0)":
        "SELECT COUNT(*) AS new_scores_without_their_reasons FROM opportunities o WHERE"
        " o.prompt_version = '" + PART_B_VERSION + "' AND NOT EXISTS (SELECT 1 FROM"
        " json_tree(o.score_reasons) t WHERE t.key = 'scored_at' AND t.atom = o.scored_at);",
    "new scores with no reasons, which only old code with the new prompt makes (expect"
    " the header and no rows)":
        "SELECT id, score_basis, scored_at FROM opportunities WHERE prompt_version = '"
        + PART_B_VERSION + "' AND score_reasons IS NULL ORDER BY scored_at, id;",
    "the reasons the scores on the rows gave, and their quotes kept and dropped, by basis":
        tally("opportunities o", "o.score_reasons", "o.score_basis AS basis"),
    "the history keeps them (expect with_reasons equal to rows)":
        "SELECT event, writer, score_basis AS basis, COUNT(*) AS rows, SUM(reasons IS NOT"
        " NULL) AS with_reasons FROM score_history WHERE prompt_version = '" + PART_B_VERSION
        + "' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;",
    "the quotes the history keeps, kept and dropped, by event and basis":
        tally("score_history h", "h.reasons", "h.event, h.score_basis AS basis"),
    "no score made before the deploy carries reasons (expect 0 and 0)":
        "SELECT (SELECT COUNT(*) FROM opportunities WHERE COALESCE(prompt_version, '') <>"
        " '" + PART_B_VERSION + "' AND score_reasons IS NOT NULL) AS old_rows_with_reasons,"
        " (SELECT COUNT(*) FROM score_history WHERE COALESCE(prompt_version, '') <> '"
        + PART_B_VERSION + "' AND reasons IS NOT NULL) AS old_history_with_reasons;",
    "the scores made before the deploy, as before less any you re-scored since": OLD_SCORES,
    "applied roles and the score each holds, the same as before": APPLIED_LIST,
    "no row of an applied role was scored after the role was applied, reposts apart"
    " (expect 0)":
        "WITH chain AS (SELECT role_key, MIN(applied_at) AS first_applied FROM"
        " opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL GROUP BY"
        " role_key) SELECT COUNT(*) AS rescored_after_applying FROM opportunities o LEFT"
        " JOIN chain c ON c.role_key = o.role_key WHERE COALESCE(c.first_applied,"
        " o.applied_at) IS NOT NULL AND o.first_seen <= COALESCE(c.first_applied,"
        " o.applied_at) AND o.scored_at > COALESCE(c.first_applied, o.applied_at);",
    "what the new scores have cost, per run, to compare with the week before":
        "SELECT workflow, COUNT(*) AS runs, SUM(items_new) AS new_items,"
        " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens FROM runs"
        " WHERE ts >= (SELECT MIN(scored_at) FROM opportunities WHERE prompt_version = '"
        + PART_B_VERSION + "') AND workflow IN ('inbox', 'prepare', 'rescore') GROUP BY 1"
        " ORDER BY 1;",
}

# The proof that every quote kept on a row is in the text stored for it now,
# by the rule that kept it, pasted on the box as a here-document.
KEPT_QUOTES_CHECK = '''import sqlite3, sys
sys.path.insert(0, "scripts")
import radar_common as rc
c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True)
c.row_factory = sqlite3.Row
kept = missing = unchecked = 0
for row in c.execute("SELECT * FROM opportunities WHERE score_reasons IS NOT NULL"):
    record = rc.reasons_record(row["score_reasons"], row["scored_at"])
    if record is None:
        continue
    if row["score_basis"] == "spec":
        if row["description_hash"] != rc.description_version(row["description_text"]):
            unchecked += 1
            continue
        sources = [row["description_text"] or ""]
    else:
        sources = [row[f] or "" for f in ("title", "company", "location", "salary_rate")]
    for item in record["reasons"]:
        if item.get("quote"):
            kept += 1
            if not any(rc.find_verbatim(item["quote"], s, whole_words=True) for s in sources):
                missing += 1
                print("not in the stored text:", row["id"], item["quote"][:80])
print("quotes kept", kept, "not in the stored text", missing,
      "rows whose description has changed since", unchecked)
'''

# The role the new scores block opens on the Prepare page, by the line that
# sets ID on the box page.
ID_QUERY = ("SELECT id FROM opportunities WHERE score_basis = 'spec' AND prompt_version = '"
            + PART_B_VERSION + "' ORDER BY scored_at DESC, id DESC LIMIT 1;")
ID_LINE = 'ID=$(Q "' + ID_QUERY + '" | sed -n 2p); echo "role $ID"'
# The lines that show which code the dashboard serves, before and after.
CODE_LINE_BEFORE = ("echo '--- the dashboard runs the code from before the deploy (expect 0)'; "
                    "curl -s -m 15 $B/jobs | grep -c '\\.reasons{'")
CODE_LINE_AFTER = ("echo '--- the dashboard runs the new code (expect 1)'; "
                   "curl -s -m 15 $B/jobs | grep -c '\\.reasons{'")
STARTED_LINE = ("echo '--- and started again for it (expect a time after the restart in "
                "step 6)'; docker inspect -f '{{.State.StartedAt}}' medtech-radar")
# Brief 4's page picks its role with the sqlite3 command line. This page gives
# the same query through Q, for a box that has no sqlite3 command line.
BRIEF4_DOC = REPO_ROOT / "docs" / "box-checks-brief-4.md"
BRIEF4_ID = re.compile(r'^ID=\$\(sudo -u radar sqlite3 -readonly db/radar\.sqlite "([^"]+)"\); '
                       r'echo "role \$ID"$', re.M)
OUR_BRIEF4_ID = re.compile(r'^ID=\$\(Q "([^"]+)" \| sed -n 2p\); echo "role \$ID"$', re.M)

BOX_DOC = REPO_ROOT / "docs" / "box-checks-scorer-quotes.md"
# The one-line query helper the page defines, with the Python it runs.
Q_PYTHON = ('import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro",'
            ' uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in'
            ' cur.description)); [print(" | ".join("" if v is None else str(v) for v in r))'
            ' for r in cur]')


def ask(conn, queries: dict, start: str) -> list[tuple]:
    q = next(v for k, v in queries.items() if k.startswith(start))
    return [tuple(r) for r in conn.execute(q).fetchall()]


def run_kept_check(db: Path) -> str:
    code = KEPT_QUOTES_CHECK.replace("file:db/radar.sqlite?mode=ro",
                                     f"file:{db.as_posix()}?mode=ro")
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")
    return (done.stdout + done.stderr).strip()


def part_b_prompt() -> str:
    """scorer.md as this change left it, before each ask carried its advert
    sentence: the prompt the box page below deploys, stamped PART_B_VERSION."""
    whole = (REPO_ROOT / "prompts" / "scorer.md").read_text(encoding="utf-8")
    para = whole[whole.index("\nFor every ask, met, partly or missing, give sentence"):
                 whole.index("\ncv_match comes from this same evidence.")]
    return whole.replace(para, "").replace(', "sentence": ""}],', '}],')


def box_query_checks() -> None:
    """The box page for this change deploys the prompt it left, so the box
    is played through with that prompt, PART_B_VERSION; Prepare application
    final has its own page for the prompt after it."""
    real = score_item.load_prompt
    score_item.load_prompt = lambda name: part_b_prompt() if name == "scorer.md" else real(name)
    try:
        _box_query_checks()
    finally:
        score_item.load_prompt = real


def _box_query_checks() -> None:
    everything = {**BEFORE_QUERIES, **AFTER_QUERIES, **NEW_QUERIES}
    check(all(not any(ch in q for ch in ('"', "$", "`", "\\", "!"))
              for q in everything.values()),
          "none of the box queries holds a character the shell would read: a "
          "double quote, dollar sign, backtick, backslash or exclamation mark")
    doc = BOX_DOC.read_text(encoding="utf-8") if BOX_DOC.exists() else ""
    absent = [k for k, q in everything.items() if f'Q "{q}"' not in doc
              or f"echo '--- {k}'" not in doc]
    check(not absent, f"the box page carries each query as it is run here, with its "
                      f"label ({absent[:3]})")
    check(KEPT_QUOTES_CHECK in doc and Q_PYTHON in doc,
          "and the kept quote check and the query helper, word for word")
    sections = {name: doc[doc.index(f"## {name}"):] if f"## {name}" in doc else ""
                for name in ("BEFORE", "AFTER, before any new score",
                             "New scores, after the next inbox run")}
    before_block = sections["BEFORE"][:sections["BEFORE"].find("## The deploy")]
    after_block = sections["AFTER, before any new score"][
        :sections["AFTER, before any new score"].find("## New scores")]
    check(CODE_LINE_BEFORE in before_block and CODE_LINE_AFTER in after_block
          and STARTED_LINE in after_block
          and ID_LINE in sections["New scores, after the next inbox run"],
          "BEFORE reads which code the dashboard serves, AFTER reads it again and "
          "the time the container started, and the new scores block sets its role "
          "by the query this runner runs")
    deploy = doc[doc.index("## The deploy"):doc.index("## AFTER")] if "## The deploy" in doc else ""
    guarded = ("pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|"
               "regate|backfill_pay' && echo 'wait, something is running, nothing was pulled'"
               " || { sudo -u radar git pull --ff-only && git log --oneline -3; }")
    check(guarded in deploy and "which is harmless" not in doc
          and "press no Prepare" in " ".join(deploy.split())
          and deploy.index("git pull --ff-only") < deploy.index("radar_common.get_db().close()")
          < deploy.index("docker restart medtech-radar"),
          "the deploy pulls only when nothing is running, then opens the database and "
          "restarts, and asks for no click from the pull until the restart")
    ours = OUR_BRIEF4_ID.findall(doc)
    theirs = BRIEF4_ID.findall(BRIEF4_DOC.read_text(encoding="utf-8")) if BRIEF4_DOC.exists() else []
    check(len(theirs) == 1 and theirs[0] in ours and ID_QUERY in ours,
          "for a box without Brief 4, the page gives Brief 4's role line through Q, "
          "the same query word for word")

    with fresh() as env:
        # A box from before the deploy.
        old = env.tmp / "box-old.sqlite"
        radar_common.get_db(old).close()
        raw = sqlite3.connect(old)
        raw.execute("ALTER TABLE opportunities DROP COLUMN score_reasons")
        raw.execute("ALTER TABLE score_history DROP COLUMN reasons")
        raw.commit()
        raw.close()
        old_env = Env.__new__(Env)
        old_env.tmp, old_env.db = env.tmp, old
        a = old_env.role(title="Director A", company="Alpha Diagnostics", description=None)
        b = old_env.role(title="Director B", company="Beta Diagnostics")
        applied = old_env.role(title="Director C", company="Gamma Diagnostics")
        old_env.apply(applied)
        conn = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
        before = {k: ask(conn, BEFORE_QUERIES, k) for k in BEFORE_QUERIES}
        conn.close()
        check(before["SQLite can read the reasons when they come (expect json_ok 1)"][0][1] == 1
              and before["Brief 4 is on the box, so the pull brings only this change "
                         "(expect 1 and 1)"] == [(1, 1)]
              and before["the reasons columns are not there yet (expect 0 and 0)"] == [(0, 0)]
              and before["the scores held now, to compare after"][0][0] == 3
              and before["applied roles and the score each holds, to compare after"][0][0]
              == applied,
              "on a box before the deploy the BEFORE queries read JSON there, Brief 4 "
              "there, no reasons columns, three scores and the applied role")
        check(all(before[k] for k in BEFORE_QUERIES if not k.startswith("what scoring cost")),
              "and every BEFORE query has something to say on it")
        # The deploy's first open, then AFTER before any new score.
        radar_common.get_db(old).close()
        conn = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
        after = {k: ask(conn, AFTER_QUERIES, k) for k in AFTER_QUERIES}
        conn.close()
        cols = after["the two columns are there, TEXT, nullable and with no default "
                     "(expect two rows, notnull 0 and dflt_value empty)"]
        check(after["the file is sound"] == [("ok",)]
              and after["row counts and history rows, the same query as before the "
                        "deploy"] == before["row counts and history rows, to compare after"]
              and [(r[0], r[2], r[3], r[4], r[5]) for r in cols]
              == [("opportunities", "score_reasons", "TEXT", 0, None),
                  ("score_history", "reasons", "TEXT", 0, None)]
              and after["no score carries reasons yet (expect 0 and 0)"] == [(0, 0)]
              and after["the scores held, the same numbers as before the deploy"]
              == before["the scores held now, to compare after"]
              and after["applied roles and the score each holds, the same as before"]
              == before["applied roles and the score each holds, to compare after"]
              and after["every row of an applied role, the same as before and with no "
                        "reasons"][0][:3]
              == before["every row of an applied role, to compare after"][0],
              "after the first open the AFTER queries read ok, the same counts and "
              "scores as before, the two columns nullable with no default, no "
              "reasons anywhere, and the applied role as it was")

        # New scores on both paths, and a tailored one.
        run_inbox(old, "1 new job", card_block("Fractional Software Director, IVD platform"))
        prep.prepare(b, old, CFG)
        prep.open_copy(b, old, CFG)
        prep.rescore_copy(b, COPY_TEXT, old, CFG)
        conn = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
        new = {k: ask(conn, NEW_QUERIES, k) for k in NEW_QUERIES}
        conn.close()
        by_basis = new["new scores by basis (expect with_reasons equal to scores on each line)"]
        rows_tally = new["the reasons the scores on the rows gave, and their quotes kept "
                         "and dropped, by basis"]
        hist = new["the history keeps them (expect with_reasons equal to rows)"]
        hist_tally = new["the quotes the history keeps, kept and dropped, by event and basis"]
        check(by_basis == [("card", 1, 1), ("spec", 1, 1)]
              and new["every new score carries the reasons made with it (expect 0)"] == [(0,)]
              and rows_tally == [("card", 4, 2, 1, 1, 0, 0, 1), ("spec", 4, 2, 1, 1, 0, 0, 1)],
              f"after a new advert and a Prepare application click the new scores are "
              f"one on the card and one on the description, each with its reasons: "
              f"four, two quotes kept, one not found, one too long and one entry left "
              f"out ({by_basis}, {rows_tally})")
        check(sorted(hist) == [("scored", "prepare", "spec", 1, 1),
                               ("scored", "process_email", "card", 1, 1),
                               ("tailored", "prepare", "spec", 1, 1)]
              and sorted(hist_tally) == [("scored", "card", 4, 2, 1, 1, 0, 0, 1),
                                         ("scored", "spec", 4, 2, 1, 1, 0, 0, 1),
                                         ("tailored", "spec", 4, 2, 1, 1, 0, 0, 1)],
              f"and the history keeps them on both paths and on the tailored score "
              f"({sorted(hist)})")
        check(new["no score made before the deploy carries reasons (expect 0 and 0)"] == [(0, 0)]
              and new["no row of an applied role was scored after the role was applied, "
                      "reposts apart (expect 0)"] == [(0,)]
              and new["applied roles and the score each holds, the same as before"]
              == before["applied roles and the score each holds, to compare after"]
              and new["the scores made before the deploy, as before less any you "
                      "re-scored since"][0][0] == 2,
              "the scores from before carry none, the applied role is as it was, and "
              "the old scores are as before less the one the click replaced")
        cost = new["what the new scores have cost, per run, to compare with the week before"]
        check({r[0] for r in cost} == {"inbox", "prepare"},
              f"the cost query counts the inbox run and the clicks ({cost})")
        window = next(k for k in NEW_QUERIES if k.startswith("new scores with no reasons"))
        conn = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
        picked = [tuple(r) for r in conn.execute(ID_QUERY).fetchall()]
        conn.close()
        check(new[window] == [] and picked == [(b,)],
              f"no new score lacks its reasons, and the role line picks the role "
              f"scored on its description ({new[window]}, {picked})")
        with pointed_at(old):
            jobs = serve_dashboard.render("jobs").decode("utf-8")
        styled = [line for line in jobs.splitlines() if ".reasons{" in line]
        check(len(styled) == 1,
              f"the Jobs page the dashboard serves carries the reasons' style on one "
              f"line, which the code line on the box page counts ({len(styled)})")
        c = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        try:
            page = build_dashboard.render_prepare_page(c, b, CFG, "box.sqlite")
        finally:
            c.close()
        with pointed_at(old):
            opened = serve_dashboard.render_jobs(b, {})[1].decode("utf-8")
        check(page.count('class="reasons"') == 2 and opened.count('class="reasons"') >= 1,
              "the Prepare page the curl line reads lists two sets of reasons, the "
              "master score's and the tailored copy's, and the Jobs page at least one")
        out = run_kept_check(old)
        check(out.endswith("quotes kept 4 not in the stored text 0 rows whose "
                           "description has changed since 0"),
              f"the kept quote check, run as it is pasted, finds every kept quote in "
              f"the text stored for its row ({out[-120:]})")
        # The helper itself, as the page defines it, from a checkout's root.
        root = env.tmp / "box-root"
        (root / "db").mkdir(parents=True)
        shutil.copyfile(old, root / "db" / "radar.sqlite")
        done = subprocess.run([sys.executable, "-c", Q_PYTHON,
                               AFTER_QUERIES["the file is sound"]], cwd=root,
                              capture_output=True, text=True, timeout=60)
        check(done.returncode == 0 and done.stdout.split() == ["integrity_check", "ok"],
              f"and the query helper prints a header and the rows ({done.stdout.strip()})")

        # The proofs catch what they exist for.
        later = "2026-12-01T10:00:00Z"
        bad = sqlite3.connect(old)
        new_card = [r for r in old_env.rows() if r["prompt_version"] == PART_B_VERSION
                    and r["score_basis"] == CARD][0]["id"]
        b_scored_at = old_env.row(b)["scored_at"]
        bad.execute("UPDATE opportunities SET score_reasons = ? WHERE id = ?",
                    (reasons_record([], scored_at="2026-01-01T00:00:00Z"), new_card))
        bad.execute("UPDATE opportunities SET score_reasons = ? WHERE id = ?",
                    (reasons_record([]), a))
        bad.execute("UPDATE opportunities SET scored_at = ? WHERE id = ?", (later, applied))
        bad.execute("UPDATE opportunities SET score_reasons = ? WHERE id = ?",
                    (reasons_record([{"reason": "r", "quote": "words the advert never had",
                                      "quote_dropped": None}],
                                    scored_at=b_scored_at, basis=SPEC), b))
        bad.commit()
        bad.close()
        conn = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
        caught = (ask(conn, NEW_QUERIES, "every new score carries")[0][0],
                  ask(conn, NEW_QUERIES, "no score made before")[0][0],
                  ask(conn, NEW_QUERIES, "no row of an applied role")[0][0])
        conn.close()
        out = run_kept_check(old)
        check(caught == (1, 1, 1) and "not in the stored text 1" in out,
              f"a new score whose reasons are another score's, an old score given "
              f"reasons, an applied role scored again and a kept quote that is not in "
              f"the stored text are each caught ({caught})")
        # A score that old code made with the new prompt stores no reasons.
        bad = sqlite3.connect(old)
        bad.execute("UPDATE opportunities SET score_reasons = NULL WHERE id = ?", (new_card,))
        bad.commit()
        bad.close()
        conn = sqlite3.connect(f"file:{old.as_posix()}?mode=ro", uri=True)
        listed = ask(conn, NEW_QUERIES, "new scores with no reasons")
        counted = ask(conn, NEW_QUERIES, "every new score carries")[0][0]
        conn.close()
        check([r[:2] for r in listed] == [(new_card, CARD)] and counted == 1,
              f"and a new score with no reasons at all, as old code makes one with the "
              f"new prompt, is counted and named by the query for it ({listed})")


# -------------------------------------------------------------------- docs

def docs_checks() -> None:
    arch = " ".join((REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8").split())
    for needle in ("score_reasons", "quote_dropped", "CARD_MAX_TOKENS", "6,144",
                   "find_verbatim", "thirteen fields", "up to four reasons",
                   "25 words or fewer", "fourth first-open event"):
        if needle not in arch:
            check(False, f"the architecture notes say {needle!r}")
            break
    else:
        check(True, "the architecture notes describe the reasons, the drop, the two "
                    "ceilings, the matching rule, the history and the deploy")
    check(all(n in arch for n in ("hold_reasons", "reasons_gates_moved", "whole_words",
                                  "the pay backfill"))
          and "unless the reply renamed" not in arch,
          "the notes say a quote is held to the words the row stores and to whole "
          "words, that a page says when the gates were judged again, and whose "
          "ceiling MAX_TOKENS is, and no longer excuse a quote of a renamed title")
    cost = " ".join((REPO_ROOT / "docs" / "cost-note.md").read_text(encoding="utf-8").split())
    check("reasons" in cost and "2,048" in cost and "6,144" in cost,
          "the cost note prices the reasons and names both ceilings")
    # The table is the sum of the lines above it, at the note's own prices.
    typical = (0.08 + 0.33 + 0.30, 0.15 + 0.66 + 0.70)
    worst = 240 * 0.0025 + 240 * 0.018 + 200 * 0.014
    check("about 6 cents for one score" in cost and "a card asks once more" in cost
          and "| Typical range | $0.70 to $1.50 | $3 to $6.50 |" in cost
          and "| Worst case | about $7.75 | about $33 |" in cost
          and round(typical[0], 2) == 0.71 and round(typical[1], 2) == 1.51
          and abs(worst - 7.72) < 0.005 and round(worst * 4.33) == 33
          and "$0.71 to $1.51 a week" in cost and "about $7.72 a week" in cost,
          "the cost note says a runaway card score can bill two capped replies, and "
          "its table is the sum of its own lines, typical and worst case")
    doc = BOX_DOC.read_text(encoding="utf-8") if BOX_DOC.exists() else ""
    blocks = re.findall(r"```bash\n(.*?)```", doc, re.S)
    check(len(blocks) >= 8 and all(b.strip() for b in blocks)
          and "## BEFORE" in doc and "## AFTER" in doc and "## Rollback" in doc
          and PART_B_VERSION in doc and OLD_VERSION in doc,
          f"the box page has BEFORE, AFTER and rollback, {len(blocks)} command blocks "
          f"and none of them empty, and names both prompt versions")
    check("backup_radar.sh" in doc and "radar_common.get_db().close()" in doc
          and "docker restart medtech-radar" in doc
          and "never restore the pre-deploy backup" in " ".join(doc.lower().split()),
          "the deploy backs up first, opens the database by hand, restarts the "
          "dashboard, and the rollback is code only")
    flat_doc = " ".join(doc.split())
    check("can show here with the quote it was given" not in flat_doc
          and "which should never happen" in flat_doc
          and "Paste each of its blocks with its `Q() {` line replaced" in flat_doc,
          "the box page no longer excuses a kept quote missing from the stored text, "
          "and tells a box without Brief 4 how to run Brief 4's blocks without the "
          "sqlite3 command line")
    suite = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("run_quotes_test.py" in suite, "and run_all.sh runs this file")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()
    for section in (prompt_checks, matching_checks, normalise_checks, card_path_checks,
                    spec_path_checks, tailored_checks, migration_checks,
                    no_rescore_checks, applied_checks, page_checks, box_query_checks,
                    docs_checks):
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
        print(f"{len(failures)} QUOTED-REASONS CHECK(S) FAILED")
        return 1
    print("ALL QUOTED-REASONS CHECKS PASSED"
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
