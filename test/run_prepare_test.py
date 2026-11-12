#!/usr/bin/env python
"""Acceptance checks for Prepare application.

No model calls and no network. RADAR_MOCK is forced on, the one page the code
may fetch is a stub, the CV is the test fixture, and every database is a
throwaway under a temporary directory, so a mistake here cannot spend a token,
ask LinkedIn for anything or touch a real row. The owner's own preferences
file is never read.

What it checks:

- the six nullable columns arrive on a copy of a database in the old shape,
  once however many processes open it at the moment, with every row untouched,
- the CV is numbered by one rule (a line break is a line feed, a carriage
  return and line feed, or a lone carriage return and nothing else), the card
  prompt is the file above the full description marker and so the bytes it
  always was, the full description prompt is the whole file with the CV
  numbered, and the ceiling for a full description holds the worst reply the
  prompt allows,
- each ask a reply gives is checked in code: a status of met, partly or
  missing, CV lines that exist and have text, a met or partly with none
  recorded as unanchored, and a reply with no list of asks is not a score,
- one model call per click, with no retry: a bad reply, a cut off reply and a
  raised error each cost one call, and a card still retries as it did,
- the pay quote is the advert's own words or it is not shown, and an advert
  that names none says so,
- one click, in every state a role can be in: the first click fetches the
  public page once and scores once, a second finds the work done and spends
  nothing, a changed description is scored once more, a tier that moves is
  recorded and reported and nothing is dismissed, every way the fetch can fail
  leaves the card's score and says so, every way the score can fail stores
  nothing and is not paid for again at once, and a role that is applied for, or
  not open, is shown and touched by nothing, including the race where it is
  applied for while the page is fetched or while the model answers,
- a double click, a storm of clicks and the same click over HTTP pay once,
- the fetch is the page checker's own, its user agent, no cookie and no login,
  and a 429 stops every later fetch for the checker's backoff,
- the page: what it shows for a role at each stage, that everything the advert
  or the model wrote is escaped, that opening it writes nothing, that it shows
  no CV line that was not cited, and the button on the Jobs page,
- the routes over a real server under the shipped guard, and the page script
  pressed under node when node is here: one POST, the address cleaned first,
  no timers, no second POST while the first is out,
- a scan of the code finds that nothing calls the click but the route, that it
  holds no loop over rows and no fetch of its own,
- the read-only queries handed to the box for the deploy checks run here, on
  seeded data, and each proof query is shown to catch the harm it exists for.

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
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Forced on before anything else loads. Nothing in this file may call a model.
os.environ["RADAR_MOCK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "test"):
    if str(REPO_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / sub))

import build_dashboard  # noqa: E402
import check_jobs  # noqa: E402
import cv_store  # noqa: E402
import mocks  # noqa: E402
import prepare_application as prep  # noqa: E402
import radar_common  # noqa: E402
import score_item  # noqa: E402
import serve_dashboard  # noqa: E402

# Hermetic. The live preferences file holds the owner's own floors, and a check
# must neither read it nor fail because it was edited.
TEMPLATE = REPO_ROOT / "config" / "preferences.template.md"
_real_load_config = radar_common.load_config
radar_common.load_config = lambda: {**_real_load_config(),
                                    "prefs_file": str(TEMPLATE)}
CFG = radar_common.load_config()
FLOORS = radar_common.read_rate_floors(CFG)

# The CV is the fixture, with a label the tests can change.
FIXTURE_CV = (REPO_ROOT / "test" / "fixtures" / "profile_snapshot.md"
              ).read_text(encoding="utf-8")
FIXTURE_LINES = cv_store.cv_lines(FIXTURE_CV)
CV_LABEL = ["cv-test-1.md"]
score_item.load_cv_text = lambda config: (FIXTURE_CV,
                                          "test/fixtures/profile_snapshot.md")
score_item.get_cv_version = lambda config: CV_LABEL[0]

HEADER = build_dashboard.WRITE_HEADER
PROXY = "medtech-radar.keogh.cloud"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# The button a page offers, as an element. The page's script names the same
# attribute in a selector, so the bare word proves nothing about the page.
BUTTON = re.compile(r"<button[^>]*" + "data-prepare-go" + r"\b")

failures: list[str] = []
skipped: list[str] = []
_ids = iter(range(4_200_000_001, 4_300_000_000))


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

FETCHES: list[str] = []
PAGES: dict = {}            # job id -> (status, final url, body)
FETCH_HOOK = {"fn": None}   # called with the url before the page is returned
CALLS: list[dict] = []      # every model call, as it was made
MODEL = {"hook": None}      # may answer a call itself, or raise
CLOCK = [10_000.0]          # the guard's monotonic clock, moved by hand

_real_fetch = check_jobs.fetch_job_page
_real_claude_call = radar_common.claude_call


def fake_fetch(url):
    FETCHES.append(url)
    if FETCH_HOOK["fn"] is not None:
        FETCH_HOOK["fn"](url)
    jid = check_jobs.job_view_id(url)
    status, final, body = PAGES.get(jid, (404, url, ""))
    return status, final or url, body


def counting_claude_call(model, system_blocks, user_content, max_tokens=1024,
                         mock_fn=None):
    call = {"model": model, "system": system_blocks, "user": user_content,
            "max_tokens": max_tokens}
    CALLS.append(call)
    if MODEL["hook"] is not None:
        got = MODEL["hook"](call)
        if got is not None:
            return got
    return _real_claude_call(model, system_blocks, user_content,
                             max_tokens=max_tokens, mock_fn=mock_fn)


def says(text: str, stop: str = "end_turn", out_tokens: int = 60):
    """A model reply, as claude_call returns one."""
    return text, {"input_tokens": 900, "output_tokens": out_tokens,
                  "cache_read_tokens": 0, "mode": "mock", "model": "mock",
                  "stop_reason": stop}


check_jobs.fetch_job_page = fake_fetch
radar_common.claude_call = counting_claude_call
prep._mono = lambda: CLOCK[0]
prep._sleep = lambda seconds: None


def reset() -> None:
    del FETCHES[:], CALLS[:]
    PAGES.clear()
    FETCH_HOOK["fn"] = MODEL["hook"] = None
    CLOCK[0] = 10_000.0
    CV_LABEL[0] = "cv-test-1.md"
    prep.GUARD.reset()


# -------------------------------------------------------------- the fixtures

DESCRIPTION = (
    "<p>About the role</p>"
    "<p>We are hiring a Director of Software for a medical device business.</p>"
    "<p>Requirements</p>"
    "<p>- IEC 62304 and ISO 13485 in a Class C IVD</p>"
    "<p>- Leadership of an FDA submission</p>"
    "<p>- Kubernetes certification</p>"
    "<p>- RADAR-UNANCHORED claim of ten years of Rust</p>"
    "<p>Pay</p>"
    "<p>£700 to £800 per day, outside IR35.</p>")


def linkedin_page(jid, title, description_html, apply_button=True,
                  closed=False) -> str:
    """A guest job page shaped the way the page checker reads one."""
    button = '<button class="apply-button">Apply</button>' if apply_button else ""
    marker = '<div class="closed-job">No longer accepting</div>' if closed else ""
    return (f'<!DOCTYPE html><html><head><title>{title} | LinkedIn</title></head>'
            f'<body><section class="top-card-layout"><h1 class='
            f'"top-card-layout__title">{title}</h1>{button}{marker}</section>'
            f'<section class="description"><div class="show-more-less-html__'
            f'markup show-more-less-html__markup--clamp-after-5">'
            f'{description_html}</div><button class="show-more-less-html__'
            f'button">Show more</button></section>'
            f'<code id="decoratedJobPostingId"><!--{jid}--></code></body></html>')


class Env:
    """One throwaway database and the helpers to seed and read it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.db = tmp / "prepare.sqlite"
        radar_common.get_db(self.db).close()

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def role(self, title="Director of Software", company="Veltrix Diagnostics",
             description=DESCRIPTION, page=True, jid=None, **cols) -> int:
        jid = jid or next(_ids)
        url = f"https://www.linkedin.com/comm/jobs/view/{jid}"
        row = {"url_hash": f"prep-{jid}", "first_seen": "2026-11-10T09:00:00Z",
               "last_seen": "2026-11-10T09:00:00Z", "status": "new",
               "thread_type": "inbound", "title": title, "company": company,
               "location": "Ghent, Belgium", "salary_rate": "",
               "source": "linkedin-alert", "source_url": url, "tier": "top",
               "cv_match": 80, "gate_sector": 1, "gate_cv": 1,
               "gate_location": 1, "gate_rate": 1, "failed_gates": "[]",
               "one_line_why": "Judged against the four gates.",
               "score_basis": "card", "prompt_version": "pre-record",
               "score_model": "unknown",
               "role_key": radar_common.role_key(title, company),
               **cols}
        c = self.conn()
        c.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES "
                  f"({', '.join('?' for _ in row)})", tuple(row.values()))
        c.commit()
        oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.close()
        if page:
            PAGES[str(jid)] = (200, f"https://www.linkedin.com/jobs/view/{jid}/",
                               linkedin_page(jid, title, description))
        return oid

    def row(self, oid) -> dict:
        c = self.conn()
        try:
            return dict(c.execute("SELECT * FROM opportunities WHERE id = ?",
                                  (oid,)).fetchone())
        finally:
            c.close()

    def jid(self, oid) -> str:
        return check_jobs.job_view_id(self.row(oid)["source_url"])

    def history(self, oid) -> list[dict]:
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(
                "SELECT * FROM score_history WHERE opportunity_id = ?"
                " ORDER BY id", (oid,))]
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
        """Every row of every table, to prove that nothing was written."""
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

    def apply(self, oid) -> None:
        self.sql("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                 ("2026-11-11T10:00:00Z", oid))

    def prepare(self, oid) -> dict:
        return prep.prepare(oid, self.db, CFG)


@contextlib.contextmanager
def fresh():
    reset()
    tmp = Path(tempfile.mkdtemp(prefix="radar-prepare-"))
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
    server = serve_dashboard.RadarServer(("127.0.0.1", 0),
                                         serve_dashboard.Handler)
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
    status, got, data = call(port, "POST", path, body,
                             headers or page_headers(port))
    try:
        parsed = json.loads(data or b"{}")
    except ValueError:
        parsed = {}
    return status, parsed, got


def page_text(html: str) -> str:
    """The words on a page, tags out and entities decoded."""
    import html as htmllib
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html)))


def render_inner(env, oid, notices=None) -> str:
    c = env.conn()
    try:
        return build_dashboard.render_prepare_inner(c, oid, CFG, notices=notices)
    finally:
        c.close()


def render_page(env, oid, go=False) -> str:
    c = env.conn()
    try:
        return build_dashboard.render_prepare_page(c, oid, CFG, "test.sqlite",
                                                   go=go)
    finally:
        c.close()


# ---------------------------------------------------------------- migration

NEW_OPP = ("description_fetched_at", "description_hash", "score_working")
NEW_HIST = ("description_hash", "working", "tier_from")


def columns(conn, table) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def migration_checks() -> None:
    with fresh() as env:
        old = env.tmp / "old.sqlite"
        radar_common.get_db(old).close()
        raw = sqlite3.connect(old)
        for col in NEW_OPP:
            raw.execute(f"ALTER TABLE opportunities DROP COLUMN {col}")
        for col in NEW_HIST:
            raw.execute(f"ALTER TABLE score_history DROP COLUMN {col}")
        raw.commit()
        raw.row_factory = sqlite3.Row
        gone = (not set(NEW_OPP) & set(columns(raw, "opportunities"))
                and not set(NEW_HIST) & set(columns(raw, "score_history")))
        raw.close()
        check(gone, "the fixture really is shaped like a database from before "
                    "this change")
        base = env.tmp / "base.sqlite"
        # A role and a history row in the old shape, to see them untouched.
        raw = sqlite3.connect(old)
        raw.execute(
            "INSERT INTO opportunities (url_hash, first_seen, status, title,"
            " company, tier, cv_match, one_line_why, score_basis) VALUES"
            " ('old-1', '2026-10-01T00:00:00Z', 'new', 'Old role', 'Oldco',"
            " 'top', 77, 'Written before.', 'card')")
        raw.execute(
            "INSERT INTO score_history (opportunity_id, recorded_at, event,"
            " writer, cv_match, tier) VALUES (1, '2026-10-01T00:00:00Z',"
            " 'scored', 'process_email', 77, 'top')")
        raw.commit()
        raw.close()
        shutil.copyfile(old, base)
        before = sqlite3.connect(base)
        before.row_factory = sqlite3.Row
        opp_before = dict(before.execute("SELECT * FROM opportunities"
                                         ).fetchone())
        hist_before = dict(before.execute("SELECT * FROM score_history"
                                          ).fetchone())
        before.close()

        copy = env.tmp / "copy.sqlite"
        shutil.copyfile(base, copy)
        conn = radar_common.get_db(copy)
        opp_cols, hist_cols = columns(conn, "opportunities"), columns(conn, "score_history")
        check(set(NEW_OPP) <= set(opp_cols) and set(NEW_HIST) <= set(hist_cols),
              "the first open adds the three columns to opportunities and the "
              "three to score_history")
        info = {r["name"]: r for r in conn.execute("PRAGMA table_info(opportunities)")}
        hinfo = {r["name"]: r for r in conn.execute("PRAGMA table_info(score_history)")}
        check(all(info[c]["notnull"] == 0 and info[c]["dflt_value"] is None
                  for c in NEW_OPP)
              and all(hinfo[c]["notnull"] == 0 and hinfo[c]["dflt_value"] is None
                      for c in NEW_HIST),
              "all six are nullable with no default, nothing destructive")
        check(opp_cols[-len(NEW_OPP):] == list(NEW_OPP)
              and hist_cols[-len(NEW_HIST):] == list(NEW_HIST),
              "and they sit at the end of their tables")
        opp_after = dict(conn.execute("SELECT * FROM opportunities").fetchone())
        hist_after = dict(conn.execute("SELECT * FROM score_history").fetchone())
        check({k: v for k, v in opp_after.items() if k in opp_before} == opp_before
              and all(opp_after[c] is None for c in NEW_OPP)
              and {k: v for k, v in hist_after.items() if k in hist_before} == hist_before
              and all(hist_after[c] is None for c in NEW_HIST),
              "every existing row is as it was, the new cells empty")
        snapshot = "\n".join(conn.iterdump())
        conn.close()
        conn = radar_common.get_db(copy)
        check("\n".join(conn.iterdump()) == snapshot,
              "opening it again changes nothing")
        conn.close()

        errors: list[str] = []
        for round_ in range(6):
            race = env.tmp / f"race{round_}.sqlite"
            shutil.copyfile(base, race)
            barrier = threading.Barrier(6)

            def opener():
                try:
                    barrier.wait(timeout=10)
                    radar_common.get_db(race).close()
                except Exception as err:  # noqa: BLE001  the test is to see none
                    errors.append(f"{type(err).__name__}: {err}")

            threads = [threading.Thread(target=opener) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        check(not errors, "six processes opening the old file at the same "
                          f"moment all succeed, six times over ({errors[:2]})")

    check(radar_common._HISTORY_FIELDS[-2:] == ("description_hash", "working")
          and len(radar_common._HISTORY_FIELDS) == 12,
          "the history keeps twelve fields, the last two new")

    # A card score holds neither new field, whatever the row still says.
    with fresh() as env:
        oid = env.role()
        env.prepare(oid)
        spec_row = env.row(oid)
        # A writer that knows nothing of the new columns replaces the score.
        env.sql("UPDATE opportunities SET score_basis = 'card', scored_at = ?,"
                " cv_match = 61 WHERE id = ?", ("2026-11-12T15:00:00Z", oid))
        c = radar_common.get_db(env.db)
        radar_common.archive_score(c, oid, "old-writer")
        radar_common.record_score(c, oid, "old-writer")
        c.commit()
        c.close()
        hist = env.history(oid)
        kept = [h for h in hist if h["writer"] == "old-writer"]
        check(spec_row["description_hash"] is not None
              and kept and all(h["score_basis"] == "card" for h in kept)
              and all(h["description_hash"] is None and h["working"] is None for h in kept)
              and any(h["description_hash"] == spec_row["description_hash"]
                      and h["working"] == spec_row["score_working"]
                      for h in hist if h["writer"] == "prepare"),
              "a card score that replaces a spec score is filed with no description "
              "hash and no working, though the row still holds both, and the spec "
              "score's own are kept where they were")
        page = page_text(render_inner(env, oid))
        check("No per-ask working is stored" in page and "moved this role" not in page,
              "and the page does not show the old working beside the card score")


# --------------------------------------------------- the CV numbering rule

def cv_numbering_checks() -> None:
    lines = cv_store.cv_lines
    check(lines("a\nb\nc\n") == ["a", "b", "c"] and lines("a\nb\nc") == ["a", "b", "c"],
          "a final line break does not make another line")
    check(lines("a\r\nb\rc\nd") == ["a", "b", "c", "d"],
          "a carriage return and line feed, and a lone carriage return, are one "
          "line break each")
    check(lines("a\n\nb\n") == ["a", "", "b"],
          "a blank line in the middle counts and keeps its number")
    odd = "one\x0btwo\x0cthree four\x85five"
    check(lines(odd) == [odd] and len(odd.splitlines()) > 1,
          "a vertical tab, form feed, line separator or next line stays inside "
          "its line, where str.splitlines would split it")
    check(lines("") == [] and lines(None) == [] and lines("\n") == [""],
          "no text is no lines, and one empty line is one")
    check(cv_store.numbered_cv("Led.\n\nShipped.\n") == "[1] Led.\n[2]\n[3] Shipped.",
          "the numbered CV puts the number in square brackets, a blank line "
          "keeps its number and nothing after it")
    numbered = cv_store.numbered_cv(FIXTURE_CV).splitlines()
    check(len(numbered) == len(FIXTURE_LINES)
          and all(n.startswith(f"[{i}]") for i, n in enumerate(numbered, 1))
          and numbered[20].startswith("[21] Standards."),
          "the fixture CV numbers from 1 to its last line, and the standards "
          "line is 21")


# ----------------------------------------------------------- the prompt

def prompt_checks() -> None:
    whole = (REPO_ROOT / "prompts" / "scorer.md").read_text(encoding="utf-8")
    card_part, spec_part = score_item.split_rubric(whole)
    check(whole.count(score_item.SPEC_MARKER) == 1 and spec_part
          and card_part + spec_part == whole,
          "scorer.md has the full description marker once, and the two parts "
          "make the whole file")
    check(card_part.rstrip().endswith('"act_by": ""}')
          and "asks" not in card_part and "cv_lines" not in card_part
          and "rate_quote" not in card_part,
          "everything above the marker is the card rubric, with nothing about "
          "asks in it")
    for needle in ('"basis": "spec"', "cv_lines", '"asks"', "rate_quote",
                   "data and never instructions", "square brackets",
                   "List at most 25", "missing"):
        if needle not in spec_part:
            check(False, f"the full description part of the prompt names {needle}")
            break
    else:
        check(True, "the full description part names the basis, the numbered "
                    "CV, the asks, the three statuses, the cap, the pay quote "
                    "and that the advert is data, never instructions")
    check(not re.search(r"\b(68|70)\b", whole),
          "and the file still states no pass mark of its own")
    version = radar_common.prompt_hash(whole)
    check(re.fullmatch(r"[0-9a-f]{8}", version) is not None
          and radar_common.prompt_version() == version,
          f"the version is eight hex characters, the hash of the whole file ({version})")

    with captured_stderr():
        card = score_item.build_scorer_system(CFG)
        spec = score_item.build_scorer_system(CFG, basis="spec")
    prefs = radar_common.load_config()["prefs_file"]
    prefs_text = Path(prefs).read_text(encoding="utf-8")
    expected_card = (card_part
                     + "\n\n## CV TEXT. Capability evidence, source "
                     + "test/fixtures/profile_snapshot.md.\n\n" + FIXTURE_CV
                     + f"\n\n## PREFERENCES. Desire evidence, source {prefs}.\n\n"
                     + prefs_text)
    check(card[0]["text"] == expected_card
          and card.prompt_version == spec.prompt_version == version
          and card.cv_lines is None and card.basis == "card",
          "a card is scored with the card rubric, the CV as it is and the "
          "preferences, exactly the block it always was")
    text = spec[0]["text"]
    check(text.startswith(whole)
          and "Every line is numbered in square brackets." in text
          and cv_store.numbered_cv(FIXTURE_CV) in text
          and FIXTURE_CV not in text.replace(cv_store.numbered_cv(FIXTURE_CV), ""),
          "a full description gets the whole file and the CV numbered line by "
          "line, and the plain CV is not in it as well")
    check(spec.cv_lines == FIXTURE_LINES and spec.cv_version == CV_LABEL[0]
          and spec.basis == "spec"
          and json.loads(json.dumps(spec)) == list(spec),
          "its blocks carry the lines they numbered and the CV version, and "
          "are still a plain list for the API")
    check(card[0]["cache_control"] == spec[0]["cache_control"] == {"type": "ephemeral"},
          "both are one cached block")


def ceiling_checks() -> None:
    check(score_item.MAX_TOKENS == 1024 and score_item.SPEC_MAX_TOKENS == 4096,
          "the card and extractor ceiling is the 1,024 it was, a full "
          "description's is 4,096")
    # The worst reply the prompt allows: 25 asks of 20 words, four CV lines
    # each, the longest gate notes, the longest pay quote. The words are as
    # long as an advert's, eight letters, not the shortest that would do.
    ask = " ".join(["standard"] * 20)
    worst = {"company": "C" * 40, "role_title": "T" * 60, "location": "L" * 40,
             "source_url": "https://www.linkedin.com/jobs/view/4200000000/",
             "gate_sector": True, "gate_sector_note": "n" * 160,
             "cv_match": 88, "gate_cv_note": "n" * 160,
             "gate_location": True, "gate_location_note": "n" * 160,
             "location_class": "hybrid", "gate_rate": True,
             "gate_rate_note": "n" * 160, "rate_stated": True,
             "rate_value": 800.0, "rate_basis": "day-rate", "ir35": "outside",
             "engagement_type": "contract", "engagement_note": "n" * 80,
             "one_line_why": "w" * 200, "question_text": "q" * 120,
             "suggested_action": "s" * 120, "act_by": "2026-11-30",
             "asks": [{"ask": ask, "status": "partly",
                       "cv_lines": [101, 102, 103, 104]} for _ in range(25)],
             "rate_quote": "q" * prep.RATE_QUOTE_MAX}
    size = len(json.dumps(worst))
    tokens = size / 3.0
    check(tokens <= score_item.SPEC_MAX_TOKENS * 0.75,
          f"the worst reply the prompt allows is {size:,} characters, about "
          f"{tokens:,.0f} tokens even at three characters a token, which is "
          f"harder than a tokenizer is, and under three quarters of the "
          f"{score_item.SPEC_MAX_TOKENS:,} ceiling")
    check(score_item.MAX_ASKS == 25 and "List at most 25" in
          (REPO_ROOT / "prompts" / "scorer.md").read_text(encoding="utf-8"),
          "and the cap the code keeps is the cap the prompt gives")


# -------------------------------------------------------------------- asks

def ask(text, status, lines=None):
    return {"ask": text, "status": status, "cv_lines": lines}


def asks_checks() -> None:
    cv = ["# Name", "", "Led IEC 62304 work.", "   ", "Shipped an FDA submission."]
    kept, dropped = score_item.normalise_asks(
        [ask("IEC 62304", "met", [3]), ask("FDA", "partly", [5, 3]),
         ask("Rust", "missing", [])], cv)
    check(dropped == 0 and [a["status"] for a in kept] == ["met", "partly", "missing"]
          and kept[0]["lines"] == [{"n": 3, "text": "Led IEC 62304 work."}]
          and [l["n"] for l in kept[1]["lines"]] == [5, 3]
          and kept[0]["anchored"] is True and kept[2]["anchored"] is None,
          "met and partly keep the lines that exist with their text, in the "
          "order given, and missing has nothing to anchor")
    kept, _ = score_item.normalise_asks([
        ask("a", "met", [99]), ask("b", "partly", [2]), ask("c", "met", [4]),
        ask("d", "met", []), ask("e", "met", None), ask("f", "met", [0, -1])], cv)
    check([a["anchored"] for a in kept] == [False] * 6
          and all(a["lines"] == [] and a["status"] in ("met", "partly") for a in kept),
          "a met or partly whose lines do not exist, are blank, are none, or "
          "are not lines at all is recorded as unanchored, and keeps its status")
    kept, _ = score_item.normalise_asks([
        ask("a", "met", ["3"]), ask("b", "met", [3.0]), ask("c", "met", 3),
        ask("d", "met", [True]), ask("e", "met", [3, 3, 3]),
        ask("f", "met", [3.5])], cv)
    check([len(a["lines"]) for a in kept] == [1, 1, 1, 0, 1, 0]
          and [a["anchored"] for a in kept] == [True, True, True, False, True, False],
          "a number written as text, a whole float and a lone number are read "
          "as the line they mean, a bool and a fraction are not lines, and a "
          "repeated line counts once")
    kept, _ = score_item.normalise_asks([ask("a", "missing", [3, 5])], cv)
    check(kept[0]["lines"] == [] and kept[0]["anchored"] is None,
          "a missing ask that cites lines anyway keeps none of them")
    long_cv = [f"line {i}" for i in range(1, 30)]
    kept, _ = score_item.normalise_asks(
        [ask("a", "met", list(range(1, 20)))], long_cv)
    check(len(kept[0]["lines"]) == score_item.CV_LINES_PER_ASK == 4,
          "at most four lines are kept for one ask")
    many = [ask(f"ask {i}", "missing", []) for i in range(40)]
    kept, dropped = score_item.normalise_asks(many, cv)
    check(len(kept) == 25 and dropped == 15,
          "at most 25 asks are kept and the rest are counted")
    kept, dropped = score_item.normalise_asks(
        [ask("fine", "met", [3]), ask("odd", "maybe", [3]), ask("", "met", [3]),
         "a string", None, 7, ask("x", None, [3])], cv)
    check(len(kept) == 1 and dropped == 6,
          "an entry with another status, no words or no shape is dropped and counted")
    kept, _ = score_item.normalise_asks(
        [ask("Cloud — AWS; Azure!", "missing", [])], cv)
    check(kept[0]["ask"] == "Cloud, AWS. Azure.",
          "the words of an ask go through the voice rules like every free-text "
          "field")
    kept, _ = score_item.normalise_asks(
        [ask("x" * 500, "met", [3])], ["y" * 500] * 3)
    check(len(kept[0]["ask"]) == score_item.ASK_TEXT_MAX
          and all(len(l["text"]) <= score_item.CV_LINE_TEXT_MAX
                  for l in kept[0]["lines"]),
          "an ask and a cited line are cut at their caps")
    check(score_item.normalise_asks(None, cv) == ([], 0)
          and score_item.normalise_asks("no", cv) == ([], 0)
          and score_item.normalise_asks({}, cv) == ([], 0),
          "anything but a list is no asks")

    # Through the scorer, with the mock standing in for the model.
    reset()
    with captured_stderr():
        blocks = score_item.build_scorer_system(CFG, basis="spec")
    opp = {"company": "Veltrix Diagnostics", "title": "Director of Software",
           "location": "Ghent, Belgium", "salary_rate": "", "basis": "spec",
           "description": "- IEC 62304 and ISO 13485 in a Class C IVD\n"
                          "- Leadership of an FDA submission\n"
                          "- Kubernetes certification\n"
                          "- RADAR-UNANCHORED claim of ten years of Rust\n"
                          "- RADAR-BLANKLINE a thing the CV does not show\n"
                          "£700 to £800 per day"}
    scored, usage, why = score_item.score_opportunity(
        opp, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS, basis="spec")
    asks = scored["asks"]
    counts = scored["working"]["counts"]
    check(why is None and [a["status"] for a in asks] ==
          ["met", "partly", "missing", "met", "partly"]
          and [a["anchored"] for a in asks] == [True, True, None, False, False],
          "a full description scored through the scorer comes back with its asks "
          "checked: met, partly, missing, and two unanchored")
    check(asks[0]["lines"][0]["n"] == 13
          and asks[0]["lines"][0]["text"] == FIXTURE_LINES[12]
          and asks[1]["lines"] == [{"n": 16, "text": FIXTURE_LINES[15]}],
          "the lines cited are the lines of the CV the model was shown, with "
          "their text")
    check(counts == {"met": 2, "partly": 2, "missing": 1, "unanchored": 2,
                     "dropped": 0}
          and scored["working"]["cv_line_count"] == len(FIXTURE_LINES)
          and scored["working"]["cv_version"] == CV_LABEL[0]
          and scored["working"]["prompt_version"] == scored["prompt_version"]
          and scored["working"]["scored_at"] == scored["scored_at"]
          and scored["working"]["description_hash"] == scored["description_hash"]
          == radar_common.description_version(opp["description"])
          and scored["score_basis"] == "spec",
          "the working carries its counts, the CV it cites, the prompt version, "
          "the date and the description it was made against")
    check(len(CALLS) == 1 and CALLS[0]["max_tokens"] == score_item.SPEC_MAX_TOKENS
          and "Score this opportunity." in CALLS[0]["user"]
          and '"basis": "spec"' in CALLS[0]["user"],
          "one call, at the full description ceiling, asking in the usual words")

    # One call, no retry, for a full description. A card still retries.
    for label, opp_x, hook_reply in (
            ("a reply that is not JSON",
             {**opp, "description": "RADAR-NOISE " + opp["description"]}, None),
            ("a reply with no list of asks",
             {**opp, "description": "RADAR-NOASKS " + opp["description"]}, None),
            ("a reply cut off at the ceiling", opp,
             says('{"cv_match": 90}', stop="max_tokens"))):
        reset()
        MODEL["hook"] = (lambda c, r=hook_reply: r)
        with captured_stderr():
            got, usage, why = score_item.score_opportunity(
                opp_x, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS,
                basis="spec")
        check(got is None and why and len(CALLS) == 1,
              f"{label} is not a score and costs the one call, no retry ({why})")
    reset()
    card_opp = {"company": "Nebulon Health", "title": "RADAR-UNSCORABLE Director",
                "location": "Ghent", "salary_rate": "€900 per day"}
    MODEL["hook"] = lambda c: says("not json at all")
    with captured_stderr():
        got, _, why = score_item.score_opportunity(
            card_opp, score_item.build_scorer_system(CFG), CFG,
            mock_fn=mocks.mock_scorer, floors=FLOORS)
    check(got is None and len(CALLS) == 2 and CALLS[0]["max_tokens"] == 1024
          and CALLS[1]["max_tokens"] == 1024
          and CALLS[1]["user"].endswith("Return only the JSON object, nothing else."),
          "while a card that comes back unreadable is still asked a second time, "
          "at the 1,024 it always had")
    reset()


# ------------------------------------------------------------ the pay quote

def rate_checks() -> None:
    d = ("A role.   We pay  £700 to £800 per\nday, outside IR35.\n"
         "Also a Competitive salary and a bonus of up to £10,000 per year.")
    got = prep.rate_line(d, "£700 to £800 per day")
    check(got == {"quote": "£700 to £800 per day", "from": "model"},
          "a quote that is in the description, with its white space folded, is shown")
    got = prep.rate_line(d, "competitive SALARY")
    check(got == {"quote": "Competitive salary", "from": "model"},
          "a quote found without regard to case comes back as the advert's own "
          "characters, not the model's")
    check(prep.rate_line(d, "") == {"quote": None, "from": "none"}
          and prep.rate_line(d, None) == {"quote": None, "from": "none"}
          and prep.rate_line(d, "   ") == {"quote": None, "from": "none"},
          "a reply that names no pay is believed: the description names none")
    got = prep.rate_line(d, "£999 per day")
    check(got["from"] == "pattern" and got["quote"] in d.replace("\n", " ").replace("  ", " "),
          "a quote that is not in the description is not shown, and the page "
          f"checker's own reader takes over, word for word ({got})")
    got = prep.rate_line("Pay is generous.", "£999 per day")
    check(got == {"quote": None, "from": "unverified"},
          "and where that finds nothing either, the page is told the scorer "
          "reported pay that the advert cannot be made to show")
    long_quote = "Pay " + "x" * 300
    got = prep.rate_line(long_quote, long_quote)
    check(got["from"] != "model" and len(got["quote"] or "") <= prep.RATE_QUOTE_MAX,
          "a quote that is not short is not shown as one, even when it is in "
          "the description")
    check(prep.rate_line("Rate: £650 a day.", "£650 a day")["quote"]
          == "£650 a day",
          "a figure with a period beside it is quoted as written")
    src = (REPO_ROOT / "scripts" / "prepare_application.py").read_text(encoding="utf-8")
    check(not re.search(r"\b(650|750|100000|100,000)\b", src),
          "no rate floor is written into the code that quotes the pay")


# ------------------------------------------------------------ one click

SCORE_COLUMNS = ("cv_match", "one_line_why", "tier", "failed_gates", "gate_sector",
                 "gate_cv", "gate_location", "gate_rate", "score_basis",
                 "prompt_version", "score_model", "scored_at", "cv_version",
                 "description_hash", "score_working")


def score_of(row: dict) -> dict:
    return {c: row.get(c) for c in SCORE_COLUMNS}


def first_click_checks() -> None:
    with fresh() as env:
        oid = env.role()
        jid = env.jid(oid)
        dump_before = env.dump()
        out = env.prepare(oid)
        check(out["ok"] and out["outcome"] == "scored" and out["fetched"]
              and out["called"] and out["scored"] and out["tier_move"] is None
              and out["notices"] == [],
              "the first click on an open role fetches, scores and says nothing "
              "went wrong")
        check(FETCHES == [f"https://www.linkedin.com/jobs/view/{jid}/"],
              "it fetched one page, the public job page, by the job id in the "
              "stored link")
        check(len(CALLS) == 1 and CALLS[0]["model"] == CFG["claude_model_score"]
              and CALLS[0]["max_tokens"] == score_item.SPEC_MAX_TOKENS,
              "and made one model call, to the scoring model, at the full "
              "description ceiling")
        row = env.row(oid)
        check(row["description_text"].startswith("About the role")
              and "£700 to £800 per day" in row["description_text"]
              and ISO.fullmatch(row["description_fetched_at"] or "") is not None
              and 0 < len(row["description_text"]) <= check_jobs.DESCRIPTION_CAP,
              "the description is stored in the advert's own words with the time "
              "it was fetched, inside the page checker's cap")
        check(row["score_basis"] == "spec"
              and row["prompt_version"] == radar_common.prompt_version()
              and row["score_model"] == "mock"
              and ISO.fullmatch(row["scored_at"] or "") is not None
              and row["cv_version"] == CV_LABEL[0]
              and row["description_hash"]
              == radar_common.description_version(row["description_text"]),
              "the score is stamped spec, with the prompt version, the model, "
              "the date, the CV and the hash of the description it was made on")
        working = prep.load_working(row)
        check(working is not None and working["counts"]
              == {"met": 2, "partly": 1, "missing": 1, "unanchored": 1, "dropped": 0}
              and [a["status"] for a in working["asks"]]
              == ["met", "partly", "missing", "met"]
              and working["rate_quote"] == "£700 to £800 per day"
              and working["rate_quote_from"] == "model",
              "the per-ask working is stored as structured data against the "
              "role, with the pay quote")
        check(row["tier"] == "top" and row["cv_match"] == 93
              and row["dismissed_at"] is None and row["acknowledged_at"] is None
              and row["closed_at"] is None and row["expired_at"] is None
              and row["applied_at"] is None,
              "the four gates and the tier are the scorer's as ever, and no "
              "lifecycle stamp was set")
        hist = env.history(oid)
        check([h["event"] for h in hist] == ["replaced", "scored"]
              and [h["score_basis"] for h in hist] == ["card", "spec"]
              and hist[0]["description_hash"] is None and hist[0]["working"] is None
              and hist[1]["description_hash"] == row["description_hash"]
              and json.loads(hist[1]["working"])["asks"] == working["asks"]
              and hist[1]["tier_from"] is None and all(h["writer"] == "prepare" for h in hist),
              "score_history keeps the card score it replaced and the new one, "
              "with its description hash and its working")
        runs = env.runs()
        check(len(runs) == 1 and runs[0]["items_in"] == 1 and runs[0]["items_new"] == 1
              and runs[0]["mode"] == "mock" and runs[0]["model"] == "mock",
              "and one row in runs, so the week's cost counts the call")

        # The second click finds the work done.
        dump_after = env.dump()
        del FETCHES[:], CALLS[:]
        again = env.prepare(oid)
        check(again["outcome"] == "reused" and not again["fetched"]
              and not again["called"] and not again["scored"]
              and not FETCHES and not CALLS and env.dump() == dump_after,
              "a second click reuses the stored description and the stored score, "
              "fetches nothing, calls nothing and writes nothing")
        check(dump_after != dump_before, "(and the first click did write)")

        # A changed description is a new version, scored once more (S9).
        env.sql("UPDATE opportunities SET description_text = ? WHERE id = ?",
                (row["description_text"] + "\n- Python experience", oid))
        third = env.prepare(oid)
        row3 = env.row(oid)
        hist3 = env.history(oid)
        check(third["outcome"] == "scored" and not third["fetched"]
              and len(CALLS) == 1 and not FETCHES
              and row3["description_hash"] != row["description_hash"],
              "a description that has changed is a new version: scored once "
              "more, from the stored text, with no fetch")
        # The score replaced is already the newest history row, the one its own
        # click wrote, so it is not kept a second time. That is the history's
        # rule, a score the newest row already holds is not kept twice.
        check([h["event"] for h in hist3] == ["replaced", "scored", "scored"]
              and hist3[1]["description_hash"] == row["description_hash"]
              and hist3[1]["working"] == hist[1]["working"]
              and hist3[2]["description_hash"] == row3["description_hash"]
              and hist3[2]["working"] == row3["score_working"],
              "and the score and the working it replaced stay in the history, "
              "beside the new ones")
        del CALLS[:]
        env.prepare(oid)
        env.prepare(oid)
        check(not CALLS and not FETCHES,
              "while the new version is not scored again however often it is clicked")
        c = env.conn()
        twice = c.execute(
            "SELECT COUNT(*) FROM (SELECT opportunity_id, description_hash FROM"
            " score_history WHERE writer = 'prepare' AND event = 'scored' GROUP BY"
            " opportunity_id, description_hash HAVING COUNT(*) > 1)").fetchone()[0]
        c.close()
        check(twice == 0, "no role has two scores for one version of its description")

    # A role that was never scored has nothing to keep.
    with fresh() as env:
        oid = env.role(tier=None, cv_match=None, gate_sector=None, gate_cv=None,
                       gate_location=None, gate_rate=None, failed_gates=None,
                       one_line_why=None, score_basis=None, prompt_version=None,
                       score_model=None)
        out = env.prepare(oid)
        hist = env.history(oid)
        check(out["outcome"] == "scored" and out["tier_move"] is None
              and [h["event"] for h in hist] == ["scored"],
              "a role that was never scored gets a score and one history row, "
              "and no move is reported from a tier it never had")

    # A stored description with white space round it is the version that was
    # scored, so it is scored once and not on every click.
    with fresh() as env:
        oid = env.role(page=False,
                       description_text="\n  - IEC 62304 experience\r\n£650 a day \n\n")
        env.prepare(oid)
        del CALLS[:]
        again = env.prepare(oid)
        check(again["outcome"] == "reused" and not CALLS
              and radar_common.description_version(" a \r\nb\n")
              == radar_common.description_version("a \nb"),
              "a description stored with white space round it is scored once, "
              "not on every click, and white space at the ends is not a new version")

    # A description the page checker stored needs no fetch and keeps no date.
    with fresh() as env:
        oid = env.role(page=False, description_text="- IEC 62304 experience\n£650 a day")
        out = env.prepare(oid)
        row = env.row(oid)
        check(out["outcome"] == "scored" and not FETCHES and not out["fetched"]
              and row["description_fetched_at"] is None and len(CALLS) == 1
              and "Stored by the page checker, date not recorded"
              in page_text(render_inner(env, oid)),
              "a description the page checker stored is used without a fetch, "
              "scored once, and shown as stored with no date rather than a made-up one")


def tier_move_checks() -> None:
    with fresh() as env:
        oid = env.role(description=DESCRIPTION + "<p>RADAR-LOW</p>")
        out = env.prepare(oid)
        row = env.row(oid)
        hist = env.history(oid)
        check(out["outcome"] == "scored" and out["tier_move"] == {"from": "top", "to": "filtered"}
              and row["tier"] == "filtered" and row["filter_reason"] == "cv match under 40"
              and row["cv_match"] == 25,
              "a full description that sinks the CV match moves the role from Top to "
              "Filtered, for the reason the floor names, and the click reports it")
        check(hist[-1]["tier_from"] == "top" and hist[-1]["tier"] == "filtered"
              and hist[-1]["event"] == "scored" and hist[-2]["tier"] == "top",
              "the move is recorded in score_history on the score that made it, "
              "beside the score it replaced")
        check(all(row[c] is None for c in ("dismissed_at", "acknowledged_at",
                                           "closed_at", "expired_at", "applied_at"))
              and row["status"] == "new",
              "and nothing is dismissed, acknowledged, closed, expired or applied")
        text = page_text(render_inner(env, oid))
        check("moved this role from Top prospects to Filtered" in text
              and "Nothing was dismissed" in text
              and "counts a Filtered role instead of listing it" in text,
              "the page reports the move, says nothing was dismissed, and says "
              "the Jobs page will count it instead of listing it")
        # The notice lasts as long as the score it came from, no longer.
        env.sql("UPDATE opportunities SET score_basis = 'card', scored_at = ? WHERE id = ?",
                ("2026-11-12T12:00:00Z", oid))
        text = page_text(render_inner(env, oid))
        check("moved this role" not in text and "No per-ask working is stored" in text,
              "once another writer has replaced the score, the page no longer "
              "claims the move or shows a working that belongs to the old score")
    with fresh() as env:
        oid = env.role(tier="reading", gate_sector=0, failed_gates='["sector"]',
                       description=DESCRIPTION)
        out = env.prepare(oid)
        check(out["tier_move"] == {"from": "reading", "to": "top"}
              and env.row(oid)["tier"] == "top",
              "a move upwards is reported the same way")
        out = env.prepare(oid)
        check(out["tier_move"] is None,
              "and a click that scores nothing reports no move")


def fetch_failure_checks() -> None:
    def case(label, setup, expect_in, retry=True, fetches=1):
        with fresh() as env:
            oid = env.role()
            jid = env.jid(oid)
            setup(jid)
            before = env.dump()
            with captured_stderr():
                out = env.prepare(oid)
            text = " ".join(n["text"] for n in out["notices"])
            check(out["ok"] and out["outcome"] == "no_description"
                  and len(FETCHES) == fetches and not CALLS and not out["scored"]
                  and text.startswith("The full description could not be fetched.")
                  and expect_in in text
                  and "made from the advert card" in text
                  and [n["retry"] for n in out["notices"]] == [retry]
                  and env.dump() == before,
                  f"{label}: the page still opens with the card's score, says "
                  "plainly that the full description could not be fetched, "
                  "and nothing is stored, scored or paid for")

    url = lambda jid: f"https://www.linkedin.com/jobs/view/{jid}/"
    case("LinkedIn cannot be reached", lambda j: PAGES.__setitem__(j, (0, url(j), "")),
         "could not reach LinkedIn")
    case("the fetch raises", lambda j: FETCH_HOOK.__setitem__(
        "fn", lambda u: (_ for _ in ()).throw(RuntimeError("boom"))),
         "could not reach LinkedIn")
    case("a 429", lambda j: PAGES.__setitem__(j, (429, url(j), "")), "429")
    case("the authwall", lambda j: PAGES.__setitem__(
        j, (200, "https://www.linkedin.com/authwall?trk=x", "<html>sign in</html>")),
         "asked for a login, and Radar never logs in")
    case("an expired job redirect", lambda j: PAGES.__setitem__(
        j, (200, url(j) + "?trk=expired_jd_redirect", "<html></html>")),
         "expired job page", retry=False)
    case("a closed advert", lambda j: PAGES.__setitem__(
        j, (200, url(j), linkedin_page(j, "Director of Software", DESCRIPTION,
                                       apply_button=False, closed=True))),
         "shows this advert as closed", retry=False)
    case("a page for another advert", lambda j: PAGES.__setitem__(
        j, (200, url(j), linkedin_page(j, "A Different Job Entirely", DESCRIPTION))),
         "could not match to this advert")
    case("a page with no description", lambda j: PAGES.__setitem__(
        j, (200, url(j), linkedin_page(j, "Director of Software", ""))),
         "carried no description text")
    case("a server error", lambda j: PAGES.__setitem__(j, (503, url(j), "")),
         "could not match to this advert")

    # Not a LinkedIn page: nothing to fetch, nothing asked of anyone.
    with fresh() as env:
        oid = env.role(page=False, source="reed-alert",
                       source_url="https://www.reed.co.uk/jobs/director/55555")
        before = env.dump()
        out = env.prepare(oid)
        check(out["outcome"] == "no_description" and not FETCHES and not CALLS
              and "not a LinkedIn job page" in out["notices"][0]["text"]
              and out["notices"][0]["retry"] is False and env.dump() == before,
              "an advert that is not a LinkedIn page is not fetched at all, and "
              "the click says why and offers no retry")

    # A 429 stops every later fetch for the page checker's backoff.
    with fresh() as env:
        a = env.role()
        b = env.role(title="Head of Quality", company="Orbase Diagnostics")
        PAGES[env.jid(a)] = (429, url(env.jid(a)), "")
        env.prepare(a)
        check(prep.GUARD.blocked_for() == check_jobs.BACKOFF_SECONDS == 600,
              "after a 429 nothing is asked of LinkedIn for the page checker's own "
              "backoff, ten minutes")
        slept: list = []
        prep._sleep = lambda seconds: slept.append(seconds) if seconds > 0 else None
        try:
            out = env.prepare(b)
        finally:
            prep._sleep = lambda seconds: None
        check(len(FETCHES) == 1 and out["outcome"] == "no_description"
              and "10 minutes" in out["notices"][0]["text"]
              and "429" in out["notices"][0]["text"],
              "a click on another role inside it fetches nothing and says why")
        check(not slept,
              "and answers at once, without first waiting out the politeness gap")
        CLOCK[0] += 599
        env.prepare(a)
        check(len(FETCHES) == 1, "still nothing one second before the ten minutes are up")
        CLOCK[0] += 2
        out = env.prepare(b)
        check(len(FETCHES) == 2 and out["outcome"] == "scored",
              "and a fetch is made again once they are")
        n_before = len(FETCHES)
        env.prepare(b)
        env.prepare(b)
        check(len(FETCHES) == n_before, "and never twice for a role that has its description")

    # Mock mode never reaches the network: the checker's own fetch refuses.
    with fresh() as env:
        check_jobs.fetch_job_page = _real_fetch
        try:
            oid = env.role(page=False)
            with captured_stderr():
                out = env.prepare(oid)
        finally:
            check_jobs.fetch_job_page = fake_fetch
        check(out["outcome"] == "no_description" and not CALLS
              and "could not reach LinkedIn" in out["notices"][0]["text"],
              "with RADAR_MOCK on, the real fetch refuses and the click reports "
              "a failed fetch rather than going to the network")


def score_failure_checks() -> None:
    def fresh_role(env, **kw):
        oid = env.role(**kw)
        return oid

    # An unreadable reply: one call, nothing stored, not paid for again at once.
    with fresh() as env:
        oid = fresh_role(env, description=DESCRIPTION + "<p>RADAR-NOISE</p>")
        card = score_of(env.row(oid))
        out = env.prepare(oid)
        row = env.row(oid)
        check(out["outcome"] == "score_failed" and len(CALLS) == 1 and out["called"]
              and not out["scored"]
              and "reply could not be used" in out["notices"][0]["text"]
              and "The reply was paid for." in out["notices"][0]["text"]
              and out["notices"][0]["retry"] is True,
              "a reply that is not JSON costs the one call and no retry, and the "
              "click says so, and that the reply was paid for, with a Try again")
        check(row["description_text"] and score_of(row) == card
              and not env.history(oid),
              "the description that was fetched is kept, the card's score is "
              "untouched and the history has nothing")
        runs = env.runs()
        check(len(runs) == 1 and runs[0]["items_new"] == 0 and "not stored" in runs[0]["note"],
              "the call that was paid for is in runs, marked not stored")
        out = env.prepare(oid)
        check(out["outcome"] == "cooling" and len(CALLS) == 1 and not FETCHES[1:]
              and "5 minutes" in out["notices"][0]["text"]
              and "paid for" not in out["notices"][0]["text"],
              "clicked again at once it is not paid for again, says for how long, "
              "and does not claim this click paid for anything")
        CLOCK[0] += 299
        env.prepare(oid)
        check(len(CALLS) == 1, "nor one second short of the five minutes")
        CLOCK[0] += 2
        env.prepare(oid)
        check(len(CALLS) == 2, "and after them it asks once more")

    # A reply cut off at the ceiling is the same.
    with fresh() as env:
        oid = fresh_role(env)
        MODEL["hook"] = lambda c: says('{"cv_match": 90, "asks": []}', stop="max_tokens")
        with captured_stderr() as err:
            out = env.prepare(oid)
        check(out["outcome"] == "score_failed" and len(CALLS) == 1
              and env.row(oid)["score_basis"] == "card"
              and "cut off" in out["notices"][0]["text"]
              and f"({score_item.SPEC_MAX_TOKENS})" in err.getvalue(),
              "a reply cut off at the ceiling is thrown away after one call, and "
              "the log names the ceiling")

    # No list of asks is not a score.
    with fresh() as env:
        oid = fresh_role(env, description=DESCRIPTION + "<p>RADAR-NOASKS</p>")
        out = env.prepare(oid)
        check(out["outcome"] == "score_failed" and len(CALLS) == 1
              and env.row(oid)["score_basis"] == "card"
              and "no list of asks" in out["notices"][0]["text"],
              "a reply without its asks is not stored as a score")

    # A call that raises: one attempt, a short wait, then another.
    with fresh() as env:
        oid = fresh_role(env)

        def raises(call):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        MODEL["hook"] = raises
        with captured_stderr():
            out = env.prepare(oid)
        check(out["outcome"] == "score_failed" and len(CALLS) == 1 and not out["called"]
              and "could not be reached (RuntimeError)" in out["notices"][0]["text"]
              and env.row(oid)["description_text"],
              "a call that raises is one attempt, says so without the message, "
              "and the fetched description stays")
        out = env.prepare(oid)
        check(out["outcome"] == "cooling" and len(CALLS) == 1
              and "20 seconds" in out["notices"][0]["text"],
              "and is not repeated inside twenty seconds")
        CLOCK[0] += 21
        MODEL["hook"] = None
        out = env.prepare(oid)
        check(out["outcome"] == "scored" and len(CALLS) == 2,
              "after which a click that works, works")

    # No usable CV is a plain message, not a dropped connection.
    with fresh() as env:
        oid = fresh_role(env)
        real = score_item.load_cv_text

        def no_cv(config):
            raise SystemExit("No usable CV in config/profile/ and live scoring "
                             "against the test fixture is refused.")
        score_item.load_cv_text = no_cv
        try:
            out = env.prepare(oid)
        finally:
            score_item.load_cv_text = real
        check(out["outcome"] == "score_failed" and not CALLS
              and "No usable CV" in out["notices"][0]["text"],
              "with no usable CV the click says so and calls no model, "
              "where a SystemExit in a handler thread would have said nothing")

    # A new CV or a new prompt is a new question, not the old failure.
    with fresh() as env:
        oid = fresh_role(env, description=DESCRIPTION + "<p>RADAR-NOISE</p>")
        env.prepare(oid)
        CV_LABEL[0] = "cv-test-2.md"
        out = env.prepare(oid)
        check(len(CALLS) == 2 and out["outcome"] == "score_failed",
              "a failure is remembered for that CV, a new CV may be tried at once")


def applied_checks() -> None:
    with fresh() as env:
        oid = env.role()
        sibling = env.role()                 # a repost of the same role
        env.apply(oid)
        before = env.dump()
        for target, label in ((oid, "the stamped row"), (sibling, "a repost on its chain")):
            out = env.prepare(target)
            check(out["ok"] and out["outcome"] == "applied" and not out["fetched"]
                  and not out["called"] and not out["scored"] and not FETCHES
                  and not CALLS and out["notices"][0]["text"] == prep.APPLIED_NOTE,
                  f"{label} of a role marked applied: nothing is fetched or scored, "
                  "and the click says why")
        check(env.dump() == before,
              "and the database is byte for byte as it was, history and runs included")

    # An applied role shows what it holds, read only.
    with fresh() as env:
        oid = env.role()
        env.prepare(oid)
        env.apply(oid)
        del FETCHES[:], CALLS[:]
        inner = render_inner(env, oid)
        text = page_text(inner)
        check("IEC 62304 and ISO 13485 in a Class C IVD" in text
              and "marked applied" in text and not BUTTON.search(inner)
              and 'data-run="1"' not in inner,
              "an applied role's stored result is shown, with the note, and no "
              "button to fetch or score")
        page = render_page(env, oid, go=True)
        check('data-run="0"' in page,
              "and a page opened by the click for it is told to do nothing")

    # Applied for while the page is being fetched.
    with fresh() as env:
        oid = env.role()
        FETCH_HOOK["fn"] = lambda u: env.apply(oid)
        before = score_of(env.row(oid))
        out = env.prepare(oid)
        row = env.row(oid)
        check(out["outcome"] == "applied" and len(FETCHES) == 1 and not CALLS
              and row["description_text"] is None and score_of(row) == before
              and not env.history(oid),
              "applied for while the page is fetched: nothing is stored, no model "
              "is called, and the role keeps the score it was applied with")
    # Applied for while the model answers.
    with fresh() as env:
        oid = env.role()
        before = score_of(env.row(oid))
        MODEL["hook"] = lambda c: env.apply(oid)
        out = env.prepare(oid)
        row = env.row(oid)
        check(out["outcome"] == "applied" and len(CALLS) == 1 and out["called"]
              and not out["scored"] and score_of(row) == before
              and not env.history(oid) and row["description_text"],
              "applied for while the model answers: the reply is not stored, the "
              "score and the history are as they were")
        runs = env.runs()
        check(len(runs) == 1 and "applied while scoring" in runs[0]["note"],
              "and the call that was paid for is in runs, marked not stored")

    # Not open: acknowledged, closed, dismissed, expired.
    for column in ("acknowledged_at", "closed_at", "dismissed_at", "expired_at"):
        with fresh() as env:
            oid = env.role(**{column: "2026-11-11T09:00:00Z"})
            before = env.dump()
            out = env.prepare(oid)
            check(out["outcome"] == "not_open" and not FETCHES and not CALLS
                  and env.dump() == before,
                  f"a role with {column} set is shown and touched by nothing")

    out = prep.prepare(999_999, Path(tempfile.gettempdir()) / "radar-none.sqlite", CFG)
    check(out == {"ok": False, "note": "no opportunity 999999"},
          "a role that does not exist is a plain refusal")
    try:
        (Path(tempfile.gettempdir()) / "radar-none.sqlite").unlink()
    except OSError:
        pass


def double_click_checks() -> None:
    def slow(call):
        time.sleep(0.4)
        return None

    def storm(env, oid, threads):
        outcomes: list[str] = []
        errors: list[str] = []
        barrier = threading.Barrier(threads)

        def click():
            try:
                barrier.wait(timeout=10)
                outcomes.append(env.prepare(oid)["outcome"])
            except Exception as err:  # noqa: BLE001  the test is to see none
                errors.append(f"{type(err).__name__}: {err}")

        pool = [threading.Thread(target=click) for _ in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join(timeout=60)
        return outcomes, errors

    with fresh() as env:
        oid = env.role()
        MODEL["hook"] = slow
        outcomes, errors = storm(env, oid, 2)
        check(not errors and sorted(outcomes) == ["reused", "scored"]
              and len(FETCHES) == 1 and len(CALLS) == 1,
              "a double click: the second click waits for the first and finds the "
              "work done, one fetch and one model call between them")
        check([h["event"] for h in env.history(oid)] == ["replaced", "scored"]
              and len(env.runs()) == 1,
              "one score in the history and one row in runs")
    with fresh() as env:
        oid = env.role()
        MODEL["hook"] = slow
        outcomes, errors = storm(env, oid, 12)
        check(not errors and sorted(outcomes) == ["reused"] * 11 + ["scored"]
              and len(FETCHES) == 1 and len(CALLS) == 1,
              "twelve clicks at once pay for one fetch and one model call")
        del FETCHES[:], CALLS[:]
        for _ in range(25):
            env.prepare(oid)
        check(not FETCHES and not CALLS,
              "and twenty-five more, one after another, pay for nothing")
    with fresh() as env:
        oid = env.role(description=DESCRIPTION + "<p>RADAR-NOISE</p>")
        MODEL["hook"] = slow
        outcomes, errors = storm(env, oid, 6)
        check(not errors and len(FETCHES) == 1 and len(CALLS) == 1
              and sorted(outcomes) == ["cooling"] * 5 + ["score_failed"],
              "even when the first click fails, the clicks that were waiting do "
              "not each pay to fail again")
    # Two different roles do not wait on each other's model call.
    with fresh() as env:
        a, b = env.role(), env.role(title="Head of Quality", company="Orbase Diagnostics")
        MODEL["hook"] = slow
        started = time.monotonic()
        results = {}

        def click(x):
            results[x] = env.prepare(x)["outcome"]
        pool = [threading.Thread(target=click, args=(x,)) for x in (a, b)]
        for t in pool:
            t.start()
        for t in pool:
            t.join(timeout=60)
        took = time.monotonic() - started
        check(results == {a: "scored", b: "scored"} and len(CALLS) == 2 and took < 0.78,
              "while two roles clicked together are scored side by side "
              f"({took:.2f} seconds for two 0.4 second calls)")
    # A click that cannot get the role's lock in time gives up and pays nothing.
    with fresh() as env:
        oid = env.role()
        MODEL["hook"] = lambda c: time.sleep(1.0)
        saved = prep.LOCK_WAIT_SECONDS
        prep.LOCK_WAIT_SECONDS = 0.2
        try:
            first = threading.Thread(target=lambda: env.prepare(oid))
            first.start()
            time.sleep(0.2)
            out = env.prepare(oid)
            first.join(timeout=30)
        finally:
            prep.LOCK_WAIT_SECONDS = saved
        check(out["outcome"] == "busy" and len(CALLS) == 1 and len(FETCHES) == 1
              and "still working" in out["notices"][0]["text"],
              "a click that waits too long for another on the same role says so "
              "and pays for nothing")


# --------------------------------------------------------------- the fetch

def fetch_reuse_checks() -> None:
    """The fetch is the page checker's own: its user agent, no cookie, no login."""
    import urllib.request as ur
    calls: list[dict] = []

    class Resp:
        status = 200

        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, _n=-1):
            return linkedin_page("4200000009", "Director of Software",
                                 DESCRIPTION).encode("utf-8")

        def geturl(self):
            return self.url

    def fake_urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "method": req.get_method(),
                      "data": req.data, "timeout": timeout,
                      "headers": {k.lower(): v for k, v in req.header_items()}})
        return Resp(req.full_url)

    with fresh() as env:
        oid = env.role(jid=4200000009, page=False)
        real_urlopen = ur.urlopen
        ur.urlopen = fake_urlopen
        os.environ["RADAR_MOCK"] = "0"
        check_jobs.fetch_job_page = _real_fetch
        conn = radar_common.get_db(env.db)
        try:
            row = dict(conn.execute("SELECT * FROM opportunities WHERE id = ?",
                                    (oid,)).fetchone())
            out = {"notices": [], "outcome": None, "fetched": False}
            text = prep._fetch_and_store(conn, row, out)
        finally:
            conn.close()
            ur.urlopen = real_urlopen
            os.environ["RADAR_MOCK"] = "1"
            check_jobs.fetch_job_page = fake_fetch
        sent = calls[0] if calls else {}
        headers = sent.get("headers", {})
        check(len(calls) == 1 and sent.get("method") == "GET"
              and sent.get("data") is None
              and sent.get("url") == "https://www.linkedin.com/jobs/view/4200000009/",
              "one anonymous GET of the public job page, by id")
        check(headers.get("user-agent") == check_jobs.CHROME_UA
              and headers.get("accept-encoding") == "identity"
              and sent.get("timeout") == check_jobs.FETCH_TIMEOUT,
              "with the page checker's user agent, its identity encoding and its "
              "own timeout")
        check(not ({"cookie", "authorization", "x-li-lang", "csrf-token"} & set(headers)),
              "carrying no cookie, no token and no login")
        check(text.startswith("About the role") and out["fetched"]
              and env.row(oid)["description_text"] == text,
              "and the description it read is stored")

    # The page checker stores the date beside the text it stores, so a date on
    # the page is never older than the words.
    with fresh() as env:
        oid = env.role(page=False)
        conn = radar_common.get_db(env.db)
        try:
            row = dict(conn.execute("SELECT * FROM opportunities WHERE id = ?",
                                    (oid,)).fetchone())
            page = {"description": "A role.", "employment_type": None, "pay": None,
                    "pay_from": None}
            check_jobs.apply_page(conn, row, page, FLOORS, CFG, write=True,
                                  thresholds={"pass": 70, "floor": 40})
            conn.commit()
            stored = dict(conn.execute(
                "SELECT description_text, description_fetched_at FROM opportunities"
                " WHERE id = ?", (oid,)).fetchone())
        finally:
            conn.close()
        check(stored["description_text"] == "A role."
              and ISO.fullmatch(stored["description_fetched_at"] or "") is not None,
              "the page checker's own write of a description now stores the date "
              "beside it")


# ---------------------------------------------------------------- the page

def page_checks() -> None:
    with fresh() as env:
        oid = env.role()
        before = env.dump()
        go = render_page(env, oid, go=True)
        plain = render_page(env, oid, go=False)
        check('data-run="1"' in go and "Working." in page_text(go)
              and 'data-run="0"' in plain
              and "Fetch the description and score it" in page_text(plain)
              and BUTTON.search(plain) and not BUTTON.search(go),
              "opened by the click, the page says it is working and its script has "
              "one POST to make, opened any other way it offers a button instead")
        for _ in range(5):
            render_page(env, oid, go=True)
        check(env.dump() == before and not FETCHES and not CALLS,
              "and opening it, however often and however it is opened, writes nothing, "
              "fetches nothing and calls nothing")

        env.prepare(oid)
        done = render_page(env, oid, go=True)
        check('data-run="0"' in done and not BUTTON.search(done),
              "once the role is scored the click has nothing left to do, and the "
              "page shows what is stored")
        text = page_text(render_inner(env, oid))
        for needle in ("Scored against the full description", "by mock",
                       f"prompt {radar_common.prompt_version()}", "CV cv-test-1.md",
                       "CV match 93", "Sector Passes.", "CV match Passes.",
                       "Location Passes.", "Rate Passes.",
                       "✓ Met", "◐ Partly", "✗ Missing",
                       "IEC 62304 and ISO 13485 in a Class C IVD",
                       "Leadership of an FDA submission", "Kubernetes certification",
                       "RADAR-UNANCHORED claim of ten years of Rust",
                       "2 met, 1 partly, 1 missing, 1 without a CV line",
                       "On the card: No usable figure stated.",
                       "Stored ", "About the role"):
            if needle not in text:
                check(False, f"the page shows {needle!r}")
                break
        else:
            check(True, "the page shows the score and how it was made, the four "
                        "gates, each ask with its status as a word as well as a "
                        "glyph, the counts, the card's pay and the description")
        check(f"CV line 13 {FIXTURE_LINES[12]}" in text
              and f"CV line 16 {FIXTURE_LINES[15]}" in text
              and text.count("Unanchored.") == 1,
              "each met or partly ask shows the CV lines that evidence it, by number "
              "and with their text, and the one with none is warned about")
        check("“£700 to £800 per day”" in text,
              "the rate line quotes the advert's own words")
        check(f"</b> {FIXTURE_LINES[12]}</li>" in render_inner(env, oid),
              "a cited line's number and its text are separate words in the markup, "
              "not only apart by the style")
        check(FIXTURE_LINES[32] not in text and "Luke Keogh" not in text
              and FIXTURE_LINES[6] not in text,
              "and no CV line that was not cited is on the page")
        check(not BUTTON.search(render_inner(env, oid)),
              "with nothing pending there is no button")

        # Versions that have moved on since the score was made.
        CV_LABEL[0] = "cv-test-2.md"
        text = page_text(render_inner(env, oid))
        check("the CV has changed since" in text and "the prompt has changed" not in text
              and "is not scored again from this page" in text,
              "a CV that has changed since the score is flagged, and the page says "
              "a scored description is not scored again from there")
        real_version = radar_common.prompt_version
        radar_common.prompt_version = lambda name="scorer.md": "ffffffff"
        try:
            text = page_text(render_inner(env, oid))
        finally:
            radar_common.prompt_version = real_version
        check("the prompt has changed since" in text and "the CV has changed since" in text,
              "and so is a prompt that has")

        # The working belongs to the score it was made with. A later score of
        # the same kind, by a writer that knows nothing of the working, hides it.
        CV_LABEL[0] = "cv-test-1.md"
        before = prep.load_working(env.row(oid))
        env.sql("UPDATE opportunities SET scored_at = ? WHERE id = ?",
                ("2026-11-12T12:34:56Z", oid))
        text = page_text(render_inner(env, oid))
        check(before is not None and prep.load_working(env.row(oid)) is None
              and "2 met, 1 partly, 1 missing" not in text
              and "✓ Met" not in text
              and "No per-ask working is stored" in text,
              "a working whose date is not the date of the score the row holds is "
              "not shown, even beside another score made on a description")

    # A role with nothing to fetch says why on a plain open, where no click
    # will ever carry the reason. A role that could be fetched says nothing
    # until a click has tried.
    with fresh() as env:
        reed = env.role(page=False, source="reed-alert",
                        source_url="https://www.reed.co.uk/jobs/director/55555")
        linked = env.role()
        inner = render_inner(env, reed)
        text = page_text(inner)
        check("The full description could not be fetched. This advert's link is not "
              "a LinkedIn job page" in text and "made from the advert card" in text
              and "Not read. There is no full description stored" in text
              and "No per-ask working. It is read from the full description, and "
                  "none is stored" in text and not BUTTON.search(inner),
              "an advert that is not a LinkedIn page says on a plain open that its "
              "full description could not be fetched and why, with no button to try")
        twice = page_text(render_inner(env, reed, notices=[
            {"kind": "fetch", "text": prep.fetch_failed_text(prep.NOT_LINKEDIN_WHY),
             "retry": False}]))
        check(twice.count("could not be fetched") == 1,
              "and says it once when a click has said it already")
        text = page_text(render_inner(env, linked))
        check("could not be fetched" not in text
              and "Not read. There is no full description stored" in text,
              "while an advert that can be fetched claims no failure before one is tried")
        stored = env.role(page=False, description_text="- IEC 62304 experience")
        text = page_text(render_inner(env, stored))
        check("Not read yet. The pay is quoted from the full description once the "
              "role is scored against it" in text
              and "No per-ask working is stored. It is made when the role is scored"
              in text,
              "and one with a description stored that has not been scored says the "
              "pay and the asks come once it is")

    # No pay named.
    with fresh() as env:
        oid = env.role(description="<p>Join us. We build medical device software.</p>"
                                   "<p>- IEC 62304 experience</p>")
        env.prepare(oid)
        text = page_text(render_inner(env, oid))
        check("The full description names no pay." in text,
              "an advert that names no pay says so")
    # Competitive, in the advert's words.
    with fresh() as env:
        oid = env.role(description="<p>A medical device team.</p>"
                                   "<p>Salary: Competitive salary and benefits.</p>")
        env.prepare(oid)
        text = page_text(render_inner(env, oid))
        check("“Competitive salary”" in text,
              "and one that says competitive is quoted as it says it")
    # The model quotes pay the advert does not carry.
    with fresh() as env:
        oid = env.role(description="<p>A medical device team.</p><p>RADAR-BADQUOTE</p>")
        env.prepare(oid)
        text = page_text(render_inner(env, oid))
        check("£999" not in text and "could not be found in the advert" in text
              and "names no pay" not in text,
              "a quote the advert does not carry is not shown, and the page does "
              "not claim the advert names none either")

    # Everything the advert or the model wrote is escaped.
    with fresh() as env:
        evil = ("- <img src=x onerror=alert(1)>\n<script>alert(2)</script>\n"
                "£700 per day")
        oid = env.role(page=False, title="<b>Boss</b>", company="A&B <i>Ltd</i>",
                       description_text=evil)
        real_cv = score_item.load_cv_text
        score_item.load_cv_text = lambda config: ("<i>Led</i> everything\n", "x")

        def hostile(call):
            text, usage = _real_claude_call(call["model"], call["system"], call["user"],
                                            max_tokens=call["max_tokens"],
                                            mock_fn=mocks.mock_scorer)
            reply = json.loads(text)
            reply["gate_sector_note"] = "<script>alert(3)</script>"
            reply["asks"].append({"ask": "<svg onload=alert(4)>", "status": "met",
                                  "cv_lines": [1]})
            return json.dumps(reply), usage
        MODEL["hook"] = hostile
        try:
            out = env.prepare(oid)
        finally:
            score_item.load_cv_text = real_cv
        page = render_page(env, oid)
        check(out["outcome"] == "scored"
              and "<script>alert" not in page and "<img src=x" not in page
              and "<svg onload" not in page and "<i>Led</i>" not in page
              and "<b>Boss</b>" not in page and "A&B <i>" not in page
              and "&lt;script&gt;alert(2)" in page and "&lt;script&gt;alert(3)" in page
              and "&lt;img src=x onerror=alert(1)&gt;" in page
              and "&lt;svg onload=alert(4)&gt;" in page
              and "&lt;i&gt;Led&lt;/i&gt; everything" in page
              and "&lt;b&gt;Boss&lt;/b&gt;" in page,
              "the description, an ask, a gate note, a cited CV line, the title and "
              "the company are escaped, whoever wrote them")

    # The notices are drawn too, and a retry has a button.
    with fresh() as env:
        oid = env.role()
        inner = render_inner(env, oid, notices=[
            {"kind": "fetch", "text": "The full description could not be fetched. <b>x</b>",
             "retry": True}])
        check("&lt;b&gt;x&lt;/b&gt;" in inner and len(BUTTON.findall(inner)) == 1
              and "Try again" in inner,
              "a notice is escaped, and one that can be retried carries its own button, "
              "so the page does not offer a second")


def jobs_page_checks() -> None:
    with fresh() as env:
        top = env.role(title="Director A", company="Alpha Diagnostics")
        question = env.role(title="Director B", company="Beta Diagnostics",
                            tier="question", gate_location=0,
                            failed_gates='["location"]',
                            question_text="Would they go remote")
        reading = env.role(title="Director C", company="Gamma Diagnostics",
                           tier="reading")
        filtered = env.role(title="Director D", company="Delta Diagnostics",
                            tier="filtered", cv_match=20,
                            filter_reason="cv match under 40")
        applied = env.role(title="Director E", company="Epsilon Diagnostics")
        env.apply(applied)
        acked = env.role(title="Director F", company="Zeta Diagnostics",
                         acknowledged_at="2026-11-11T09:00:00Z")
        chain_a = env.role(title="Director G", company="Eta Diagnostics")
        chain_b = env.role(title="Director G", company="Eta Diagnostics")
        with pointed_at(env.db):
            jobs = serve_dashboard.render("jobs").decode("utf-8")
            home = serve_dashboard.render("home").decode("utf-8")
            archive = serve_dashboard.render("archive").decode("utf-8")
        found = re.findall(r'<a class="act-btn" data-prepare="(\d+)" '
                           r'href="/prepare/(\d+)\?go=1">Prepare application</a>', jobs)
        ids = {int(a) for a, _b in found}
        check(all(a == b for a, b in found) and len(found) == len(ids)
              and top in ids and question in ids and reading in ids
              and len(ids & {chain_a, chain_b}) == 1
              and not ids & {filtered, applied, acked},
              "every open role on the Jobs page has a Prepare application link to "
              "its own page, a repost chain has one, and a Filtered, an applied "
              f"and an acknowledged role have none ({sorted(ids)})")
        check(jobs.count('data-prepare="') == jobs.count('data-apply="') == 4,
              "so the count of links is the count of Applied buttons, one for each "
              "open role, the same pair of patterns the box check greps for")
        check('data-prepare="' not in home and 'data-prepare="' not in archive,
              "and the Home and archive pages, which draw the same rows, have none")
        applied_section = jobs[jobs.find("<h2>Applied</h2>"):]
        check('data-prepare="' not in applied_section.split("</details>")[0],
              "nor has the Applied section")


# -------------------------------------------------------------- the server

def server_checks() -> None:
    with fresh() as env:
        oid = env.role()
        other = env.role(title="Head of Quality", company="Orbase Diagnostics")
        with serving(env.db) as port:
            own = f"127.0.0.1:{port}"
            before = env.dump()
            for _ in range(5):
                status, headers, data = call(port, "GET", f"/prepare/{oid}?go=1")
            check(status == 200 and "text/html" in headers.get("content-type", "")
                  and headers.get("cache-control") == "no-store"
                  and b'id="prepare-body"' in data and b'data-run="1"' in data
                  and headers.get("x-frame-options") == "DENY",
                  "GET /prepare/<id> is a page, never cached, under the enforce "
                  "guard's framing rules")
            check(env.dump() == before and not FETCHES and not CALLS,
                  "and five opens by the click's address write nothing, fetch "
                  "nothing and call nothing")
            for path in ("/prepare/abc", "/prepare/", "/prepare/99999999999999999999",
                         "/prepare/%C2%B2", "/prepare/-1", "/prepare/1.5",
                         "/prepare/999999", f"/prepare/{oid}x"):
                status, _h, _d = call(port, "GET", path)
                if status != 404:
                    check(False, f"GET {path!r} is a 404, got {status}")
                    break
            else:
                check(True, "an id that is not digits, is too long for SQLite or is "
                            "not a role is a 404, never a 500")
            status, _h, data = call(port, "GET", f"/prepare/{oid}",
                                    headers={"Host": "evil.example"})
            check(status == 403 and b"write guard" in data.lower(),
                  "a request for the page under a Host the guard does not know is refused")

            bare = {"Host": own, "Origin": f"http://{own}",
                    "Content-Type": "application/json"}
            status, parsed, _g = post(port, "/prepare", {"id": oid}, headers=bare)
            check(status == 403 and "write guard refused" in json.dumps(parsed).lower()
                  and not FETCHES and not CALLS and env.dump() == before,
                  "a POST without the page scripts' header is refused before the "
                  "route runs, with nothing fetched, called or written")
            evil = {**page_headers(port), "Origin": "https://evil.example"}
            status, _p, _g = post(port, "/prepare", {"id": oid}, headers=evil)
            check(status == 403 and not FETCHES and not CALLS,
                  "and so is one from another site's page, header and all")

            for body in ({"id": True}, {"id": "1"}, {"id": 1.5}, {"id": None}, {},
                         [], b"not json", {"id": 2 ** 63}, {"id": -2 ** 63 - 1}):
                status, parsed, got = post(port, "/prepare", body)
                if status != 400:
                    check(False, f"POST /prepare {body!r} is a 400, got {status}")
                    break
            else:
                check(not FETCHES and not CALLS,
                      "a body that is not a real integer id is a 400 and nothing runs")
            status, parsed, _g = post(port, "/prepare", {"id": 999999})
            check(status == 409 and parsed.get("ok") is False
                  and "no opportunity" in parsed.get("note", ""),
                  "an id that is not a role is a 409 with a note")

            status, parsed, got = post(port, "/prepare", {"id": oid})
            check(status == 200 and "application/json" in got.get("content-type", "")
                  and parsed.get("ok") is True and parsed.get("outcome") == "scored"
                  and isinstance(parsed.get("html"), str)
                  and "The asks" in parsed["html"]
                  and "IEC 62304 and ISO 13485 in a Class C IVD" in parsed["html"]
                  and len(FETCHES) == 1 and len(CALLS) == 1,
                  "a POST with the header runs the click: one fetch, one call, and "
                  "the answer carries the page's new inside")
            status, parsed, _g = post(port, "/prepare", {"id": oid})
            check(status == 200 and parsed.get("outcome") == "reused"
                  and len(FETCHES) == 1 and len(CALLS) == 1,
                  "and the same POST again finds it done and spends nothing")
            status, _h, data = call(port, "GET", f"/prepare/{oid}?go=1")
            check(status == 200 and b'data-run="0"' in data and len(CALLS) == 1,
                  "as does opening the page by the click again")

            # Two clicks at once, over HTTP.
            MODEL["hook"] = lambda c: time.sleep(0.4)
            answers: list = []

            def click():
                answers.append(post(port, "/prepare", {"id": other})[1])
            pool = [threading.Thread(target=click) for _ in range(2)]
            for t in pool:
                t.start()
            for t in pool:
                t.join(timeout=60)
            check(sorted(a.get("outcome") for a in answers) == ["reused", "scored"]
                  and len(CALLS) == 2 and len(FETCHES) == 2,
                  "two POSTs at once for one role pay for one fetch and one call "
                  "between them")


# ------------------------------------------------------------- page script

NODE_PREPARE = r"""
const vm = require('vm');
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let bad = 0;
const say = (ok, label) => {
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label);
  if (!ok) bad += 1;
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
function world(run) {
  const events = [];
  const handlers = [];
  const attrs = { 'data-id': '7', 'data-run': run ? '1' : '0' };
  let status = null;
  const pending = [];
  const calls = [];
  const root = {
    innerHTML: '<p>stored</p>', firstChild: null,
    getAttribute(n) { return n in attrs ? attrs[n] : null; },
    setAttribute(n, v) { attrs[n] = String(v); },
    querySelector() { return status; },
    insertBefore(node) { status = node; },
  };
  const sandbox = {
    Headers, calls,
    document: {
      addEventListener(type, fn) { handlers.push({ type, fn }); },
      getElementById(id) { return id === 'prepare-body' ? root : null; },
      querySelectorAll() { return []; },
      createElement() { return { setAttribute() {}, className: '', textContent: '' }; },
      documentElement: { setAttribute() {} },
    },
    localStorage: { getItem() { return null; }, setItem() {} },
    history: { replaceState(a, b, url) { events.push('replace:' + url); } },
    location: { pathname: '/prepare/7', reload() { events.push('reload'); } },
    setTimeout() { events.push('timer'); },
    setInterval() { events.push('timer'); },
  };
  sandbox.window = sandbox;
  sandbox.alert = () => { events.push('alert'); };
  sandbox.fetch = (url, opts) => {
    events.push('fetch:' + url);
    calls.push({ url, opts });
    return new Promise((resolve, reject) => { pending.push({ resolve, reject }); });
  };
  vm.createContext(sandbox);
  vm.runInContext(data.shared + '\n' + data.prepare, sandbox);
  const click = () => {
    const btn = { disabled: false };
    const event = { target: { closest(sel) {
      return sel.includes('[data-prepare-go]') ? btn : null;
    } } };
    for (const h of handlers.filter((x) => x.type === 'click')) h.fn(event);
    return btn;
  };
  return { root, attrs, events, pending, calls, click, status: () => status };
}
const answer = (w, value) => w.pending.shift().resolve({ json: () => Promise.resolve(value) });
(async () => {
  if (typeof Headers === 'undefined') {
    console.log('NOTE  this node has no Headers, so the Prepare script was not run');
    process.exit(0);
  }
  // Opened by the click.
  let w = world(true);
  await settle();
  const sent = w.calls[0] && w.calls[0].opts;
  say(w.calls.length === 1 && w.calls[0].url === '/prepare' && sent.method === 'POST'
      && sent.body === '{"id":7}' && sent.headers instanceof Headers
      && sent.headers.get(data.header.toLowerCase()) === '1'
      && sent.headers.get('content-type') === 'application/json',
      'opened by the click, the page makes one POST of its role id to /prepare, '
      + 'through the fetch wrapper, with the guard header');
  say(w.events[0] === 'replace:/prepare/7' && w.events[1] === 'fetch:/prepare'
      && w.attrs['data-run'] === '0',
      'and cleans its address before the POST goes, so a reload while it works '
      + 'opens the page and does nothing more');
  w.click(); w.click();
  await settle();
  say(w.calls.length === 1,
      'a click on the button while that POST is out sends nothing, however often');
  answer(w, { ok: true, html: '<p>done</p>' });
  await settle(); await settle();
  say(w.root.innerHTML === '<p>done</p>' && w.calls.length === 1,
      'the answer is swapped in as the page, and nothing is sent after it');
  say(!w.events.includes('timer') && !w.events.includes('reload'),
      'no timer and no reload anywhere in it');

  // Opened any other way.
  w = world(false);
  await settle();
  say(w.calls.length === 0 && w.events.length === 0,
      'opened any other way the page sends nothing and touches nothing');
  const btn = w.click();
  await settle();
  say(w.calls.length === 1 && btn.disabled === true,
      'the button sends one POST and disables itself');
  w.click(); w.click();
  await settle();
  say(w.calls.length === 1, 'and a second click while it is out sends nothing');
  answer(w, { ok: false, note: 'The write guard refused this request. Origin is not ours.' });
  await settle(); await settle();
  say(w.root.innerHTML === '<p>stored</p>' && w.status()
      && w.status().textContent === 'The write guard refused this request. Origin is not ours.'
      && /bad/.test(w.status().className),
      'a refusal shows the guard\'s own reason and leaves the page as it was');
  w.click();
  await settle();
  say(w.calls.length === 2, 'and after it an explicit click may send again');
  w.pending.shift().reject(new Error('network'));
  await settle(); await settle();
  say(w.status() && /No usable answer/.test(w.status().textContent)
      && /bad/.test(w.status().className) && w.root.innerHTML === '<p>stored</p>',
      'a request that fails says no answer came back and that the role may still be working');
  w.click();
  await settle();
  say(w.calls.length === 3, 'after which a click may send once more');
  say(!w.events.includes('timer'), 'and nothing retries on a timer');
  process.exit(bad ? 1 : 0);
})();
"""


def page_script_checks() -> None:
    script = build_dashboard.PREPARE_SCRIPT
    check(script.count("fetch(") == 1 and script.count("method: 'POST'") == 1
          and "XMLHttpRequest" not in script and "sendBeacon" not in script,
          "the page script makes its one request with fetch and method POST, the "
          "one shape the header wrapper covers")
    # Read without its comments, which say "while" in plain English.
    code = re.sub(r"//[^\n]*", "", script)
    check(not re.search(r"setInterval|setTimeout|requestAnimationFrame|location\.reload"
                        r"|\bwhile\b|\bfor \(", code)
          and code.count("start();") == 2,
          "it has no timer, no reload and no loop, and start() is called from two "
          "places only: the button, and the first load of a page opened by the click")
    node = shutil.which("node")
    if not node:
        note("node is not on the path, so the Prepare script was not run")
        return
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "prepare.js").write_text(NODE_PREPARE, encoding="utf-8")
        (Path(tmp) / "scripts.json").write_text(json.dumps({
            "shared": build_dashboard.SCRIPT, "prepare": script,
            "header": HEADER}), encoding="utf-8")
        try:
            done = subprocess.run(
                [node, str(Path(tmp) / "prepare.js"), str(Path(tmp) / "scripts.json")],
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as err:
            check(False, f"node ran the Prepare script ({type(err).__name__})")
            return
        lines = done.stdout.splitlines()
        for ln in lines:
            if ln.startswith(("PASS  ", "FAIL  ")):
                check(ln.startswith("PASS"), ln[6:])
            elif ln.startswith("NOTE  "):
                note(ln[6:])
        if done.returncode != 0 and not any(ln.startswith("FAIL  ") for ln in lines):
            check(False, "node ran the Prepare script ("
                  + (done.stderr.strip()[:200] or f"exit {done.returncode}") + ")")


# ------------------------------------------------------------ the static scan

def static_checks() -> None:
    scripts = sorted((REPO_ROOT / "scripts").glob("*.py"))
    callers = []
    for path in scripts:
        if path.name == "prepare_application.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "prepare"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "prepare_application"):
                fn = [f.name for f in ast.walk(tree)
                      if isinstance(f, ast.FunctionDef)
                      and f.lineno <= node.lineno <= f.end_lineno]
                callers.append((path.name, fn[-1] if fn else "<module>"))
    check(callers == [("serve_dashboard.py", "run_prepare")],
          f"nothing calls the click but the route's own function ({callers})")
    imports = sorted(p.name for p in scripts
                     if p.name != "prepare_application.py"
                     and re.search(r"^\s*(import|from)\s+prepare_application\b",
                                   p.read_text(encoding="utf-8"), re.MULTILINE))
    check(imports == ["build_dashboard.py", "serve_dashboard.py"]
          or imports == ["serve_dashboard.py"],
          f"and only the dashboard imports the module ({imports})")
    scheduled = [p.name for p in sorted((REPO_ROOT / "workflows").glob("*.json"))
                 if re.search(r"prepare_application|/prepare\b", p.read_text(encoding="utf-8"))]
    check(not scheduled, f"no n8n workflow runs it or posts to it ({scheduled})")

    src = (REPO_ROOT / "scripts" / "prepare_application.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom)}
    check(not imported & {"urllib", "requests", "http", "httpx", "socket", "subprocess"}
          and "urlopen" not in attrs | names
          and src.count("check_jobs.fetch_job_page(") == 1
          and "http_get" not in attrs | names,
          "the module has no fetcher of its own: it imports no HTTP library and "
          "calls the page checker's fetch_job_page in one place")
    check("fetchall" not in attrs and "executemany" not in attrs
          and not any(isinstance(n, ast.For) for n in ast.walk(tree)),
          "it holds no loop at all, so it cannot walk rows or retry a call")
    check(src.count("score_item.score_opportunity(") == 1
          and src.count("check_jobs.read_page(") == 1,
          "and it scores in one place, once")

    serve = (REPO_ROOT / "scripts" / "serve_dashboard.py").read_text(encoding="utf-8")
    render = serve[serve.index("def render_prepare("):serve.index("def run_prepare(")]
    check("mode=ro" in render and "get_db(" not in render
          and "prepare_application" not in render and "run_prepare" not in render,
          "the page's route opens the database read only and runs nothing")

    tree = ast.parse(serve)
    do_get = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "do_GET")
    body = ast.get_source_segment(serve, do_get)
    check("run_prepare" not in body and "prepare_application" not in body,
          "GET never reaches the click, only the guarded POST does")


# ----------------------------------------------------- the queries for the box

BOX_QUERIES = {
    "briefs 2 and 5 are live (expect 4, 2 and 1)":
        "SELECT (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name"
        " IN ('score_basis','prompt_version','score_model','scored_at')) AS record_columns,"
        " (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name IN"
        " ('applied_at','applied_cv')) AS applied_columns, (SELECT COUNT(*) FROM"
        " sqlite_master WHERE name = 'score_history') AS history_table;",
    "row counts, to compare after":
        "SELECT COUNT(*) AS roles, SUM(expired_at IS NOT NULL) AS expired,"
        " SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS"
        " dismissed, SUM(applied_at IS NOT NULL) AS applied, (SELECT COUNT(*) FROM"
        " score_history) AS history_rows FROM opportunities;",
    "the file is sound": "PRAGMA integrity_check;",
    "the new columns are there (expect 3 and 3)":
        "SELECT (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name"
        " IN ('description_fetched_at','description_hash','score_working'))"
        " AS on_opportunities, (SELECT COUNT(*) FROM pragma_table_info('score_history')"
        " WHERE name IN ('description_hash','working','tier_from')) AS on_history;",
    "scores by basis, with the descriptions stored":
        "SELECT COALESCE(score_basis, 'unscored') AS basis, COUNT(*) AS roles,"
        " SUM(description_text IS NOT NULL AND TRIM(description_text) <> '')"
        " AS with_description FROM opportunities GROUP BY 1 ORDER BY 1;",
    "roles scored on a full description, newest first":
        "SELECT id, company, title, tier, scored_at, description_fetched_at,"
        " LENGTH(description_text) AS chars, description_hash FROM opportunities"
        " WHERE score_basis = 'spec' ORDER BY scored_at DESC, id DESC LIMIT 20;",
    "the history of the role prepared last":
        "SELECT id, event, writer, score_basis, tier, tier_from, description_hash IS"
        " NOT NULL AS has_hash, working IS NOT NULL AS has_working, recorded_at FROM"
        " score_history WHERE opportunity_id = (SELECT id FROM opportunities WHERE"
        " score_basis = 'spec' ORDER BY scored_at DESC, id DESC LIMIT 1) ORDER BY id;",
    "every full description score has its hash, working and record (expect 0)":
        "SELECT COUNT(*) AS spec_scores_missing_something FROM opportunities"
        " WHERE score_basis = 'spec' AND (description_hash IS NULL OR score_working"
        " IS NULL OR prompt_version IS NULL OR score_model IS NULL OR scored_at IS NULL);",
    "no role was scored twice for one stored description (expect 0)":
        "SELECT COUNT(*) AS scored_twice FROM (SELECT opportunity_id, description_hash"
        " FROM score_history WHERE writer = 'prepare' AND event = 'scored' AND"
        " description_hash IS NOT NULL GROUP BY opportunity_id, description_hash"
        " HAVING COUNT(*) > 1);",
    "no applied role was prepared after the day it was applied (expect 0)":
        "WITH chain AS (SELECT role_key, MIN(applied_at) AS first_applied FROM"
        " opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL GROUP BY"
        " role_key) SELECT COUNT(*) AS prepared_after_applying FROM score_history h"
        " JOIN opportunities o ON o.id = h.opportunity_id LEFT JOIN chain c ON"
        " c.role_key = o.role_key WHERE h.writer = 'prepare' AND"
        " COALESCE(c.first_applied, o.applied_at) IS NOT NULL AND h.recorded_at >="
        " COALESCE(c.first_applied, o.applied_at);",
    "tier moves the full description made":
        "SELECT opportunity_id, tier_from, tier, scored_at FROM score_history"
        " WHERE writer = 'prepare' AND tier_from IS NOT NULL ORDER BY id DESC;",
    "the model calls Prepare application has made, and their tokens":
        "SELECT COUNT(*) AS calls, COALESCE(SUM(items_new), 0) AS stored,"
        " COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0)"
        " AS output_tokens, COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens"
        " FROM runs WHERE workflow = 'prepare';",
}


def box_query_checks() -> None:
    check(all(not any(ch in q for ch in ('"', "$", "`", "\\", "!"))
              for q in BOX_QUERIES.values()),
          "none of the box queries holds a character the shell would read: a "
          "double quote, dollar sign, backtick, backslash or exclamation mark")

    def ask(conn, start):
        q = next(v for k, v in BOX_QUERIES.items() if k.startswith(start))
        return [tuple(r) for r in conn.execute(q).fetchall()]

    with fresh() as env:
        a = env.role()
        b = env.role(title="Head of Quality", company="Orbase Diagnostics",
                     description=DESCRIPTION + "<p>RADAR-LOW</p>")
        c = env.role(title="Regulatory Lead", company="Fenix Diagnostics")
        for oid in (a, b, c):
            env.prepare(oid)
        conn = env.conn()
        check(ask(conn, "briefs 2 and 5") == [(4, 2, 1)],
              "the prerequisite query reads 4, 2 and 1 on a database that has the "
              "score record, the applied marker and the history table")
        counts = ask(conn, "row counts")[0]
        check(counts[0] == 3 and counts[1:5] == (0, 0, 0, 0) and counts[5] >= 3,
              f"the row count query counts three roles and their history ({counts})")
        check(ask(conn, "the file is sound") == [("ok",)]
              and ask(conn, "the new columns") == [(3, 3)],
              "the integrity and column queries answer ok and 3 and 3")
        by_basis = ask(conn, "scores by basis")
        check(by_basis == [("spec", 3, 3)],
              f"three roles are scored on a full description, all with theirs ({by_basis})")
        listing = ask(conn, "roles scored on a full")
        check(len(listing) == 3 and {r[0] for r in listing} == {a, b, c},
              "and the listing names them")
        check(ask(conn, "every full description score") == [(0,)]
              and ask(conn, "no role was scored twice") == [(0,)]
              and ask(conn, "no applied role was prepared") == [(0,)],
              "after three clicks the three proofs read 0")
        moves = ask(conn, "tier moves")
        check(len(moves) == 1 and moves[0][:3] == (b, "top", "filtered"),
              "the one tier move is listed")
        last = ask(conn, "the history of the role prepared last")
        check(last and last[-1][1:4] == ("scored", "prepare", "spec")
              and last[-1][6] == 1 and last[-1][7] == 1,
              f"the history of the role prepared last ends with its spec score, "
              f"with a hash and a working ({[r[1:4] for r in last]})")
        calls = ask(conn, "the model calls")[0]
        check(calls[0] == 3 and calls[1] == 3,
              f"the cost query counts three calls, all stored ({calls})")
        conn.close()

        # The proofs catch the harm they exist for.
        env.sql("UPDATE opportunities SET score_working = NULL WHERE id = ?", (a,))
        conn = env.conn()
        check(ask(conn, "every full description score") == [(1,)],
              "a full description score without its working is caught")
        conn.close()
        later = datetime.now(timezone.utc) + timedelta(minutes=5)
        env.sql("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                (later.strftime("%Y-%m-%dT%H:%M:%SZ"), c))
        env.sql("INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
                " description_hash) VALUES (?, ?, 'scored', 'prepare', 'beefbeefbeef')",
                (c, (later + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        conn = env.conn()
        check(ask(conn, "no applied role was prepared") == [(1,)],
              "a role prepared after it was applied is caught")
        conn.close()
        env.sql("INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
                " description_hash) VALUES (?, '2026-11-12T00:00:00Z', 'scored',"
                " 'prepare', 'beefbeefbeef')", (b,))
        env.sql("INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
                " description_hash) VALUES (?, '2026-11-12T00:00:01Z', 'scored',"
                " 'prepare', 'beefbeefbeef')", (b,))
        conn = env.conn()
        check(ask(conn, "no role was scored twice") == [(1,)],
              "and one scored twice for the same description is caught")
        conn.close()

        cli = shutil.which("sqlite3")
        if cli:
            bad = []
            for name, q in BOX_QUERIES.items():
                done = subprocess.run([cli, "-readonly", "-header", "-column",
                                       str(env.db), q], capture_output=True,
                                      text=True, timeout=30)
                if done.returncode != 0 or done.stderr.strip():
                    bad.append(f"{name[:30]}: {done.stderr.strip()[:80]}")
            check(not bad, f"each query runs under the sqlite3 command line ({bad})")
        else:
            note("the sqlite3 command line is not here, so the box queries were "
                 "run through Python only")


def docs_checks() -> None:
    doc = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check("## Prepare application" in doc, "the architecture notes have a section on it")
    section = " ".join(doc.split("## Prepare application")[1].split("\n## ")[0].split())
    for needle in ("GET /prepare/<id>", "POST /prepare", "go=1", "description_hash",
                   "score_working", "tier_from", "SPEC_MAX_TOKENS", "429", "begin_write",
                   "once per version", "no retry", "applied", "Rescore", "second first-open"):
        if needle not in section:
            check(False, f"the notes say {needle!r}")
            break
    else:
        check(True, "and they cover the routes, the columns, the ceiling, the "
                    "guards, the 429 wait, what an applied role gets, the Rescore "
                    "button and the deploy")
    cfg = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("run_prepare_test.py" in cfg, "and run_all.sh runs this file")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()
    for section in (migration_checks, cv_numbering_checks, prompt_checks,
                    ceiling_checks, asks_checks, rate_checks, first_click_checks,
                    tier_move_checks, fetch_failure_checks, score_failure_checks,
                    applied_checks, double_click_checks, fetch_reuse_checks,
                    page_checks, jobs_page_checks, server_checks,
                    page_script_checks, static_checks, box_query_checks,
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
        print(f"{len(failures)} PREPARE-APPLICATION CHECK(S) FAILED")
        return 1
    print("ALL PREPARE-APPLICATION CHECKS PASSED"
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
