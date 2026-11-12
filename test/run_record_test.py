#!/usr/bin/env python
"""Acceptance checks for the scorer's record-keeping.

No model calls and no network. RADAR_MOCK is forced on, and every reply a
test needs from a model is a deterministic mock or a stub of
radar_common.claude_call, so a mistake here cannot spend a token. Every
database is a throwaway under a temporary directory.

What it checks:

- every score records its basis, the prompt version, the model and the date,
  on the row, for a first score and for a re-score,
- the prompt version is a short hash of prompts/scorer.md, it follows the
  file, and the rubric no longer hard-codes the pass mark,
- score_history gets a row for every new score, and the outgoing values are
  kept before rescore.py, regate_rates.py or the page checker's rate re-tier
  overwrite a score in place, with nothing written for a dry run,
- the CV pass mark and the Filtered floor come from the preferences file,
  fall back to 70 and 40 with one warning per run when a line is missing or
  unreadable, and never raise,
- at 70 and 40 the tier ladder gives the answers it gave before, across
  every combination of gates and every CV number, and a regate dry run over
  the test data moves no tier,
- a reply cut off at max_tokens is a failed score that changes nothing,
- the migration labels an existing database without guessing, leaves its
  data alone, runs once however many processes open it at the same moment,
- the Jobs row says how its score was made, and no other page does.

The owner's own preferences file is never read. Every config loaded here,
by this file or by a script it drives, points at the tracked template, so
editing cv_pass_score or cv_floor_score on a live copy cannot fail a check.

Exits non zero on any failure with readable FAIL lines. A mistake in the
runner itself also prints a traceback, so it can be found.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import gc
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

# Forced on before anything else loads. Nothing in this file may call a model.
os.environ["RADAR_MOCK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "test"):
    if str(REPO_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / sub))

import build_dashboard
import check_jobs
import mocks
import process_email
import radar_common
import regate_rates
import rescore
import score_item

# Hermetic. The live preferences file holds the owner's own thresholds and
# floors, and a check must neither read it nor fail because it was edited.
# Every load of the config, here and in the scripts, gets the template.
TEMPLATE = REPO_ROOT / "config" / "preferences.template.md"
_real_load_config = radar_common.load_config


def _template_config() -> dict:
    return {**_real_load_config(), "prefs_file": str(TEMPLATE)}


radar_common.load_config = _template_config
CFG = radar_common.load_config()
FLOORS = {"day": 750.0, "salary": 100000.0}
SAMPLES = REPO_ROOT / "test" / "sample_emails"
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

failures: list[str] = []
skipped: list[str] = []
_seq = iter(range(1, 10_000))


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


@contextlib.contextmanager
def counted_reads():
    """Count the calls made to read_score_thresholds while the block runs."""
    real = radar_common.read_score_thresholds
    calls: list[int] = []

    def counting(config=None):
        calls.append(1)
        return real(config)

    radar_common.read_score_thresholds = counting
    try:
        yield calls
    finally:
        radar_common.read_score_thresholds = real


def fresh_warnings() -> None:
    """Forget which warnings this process has already given."""
    radar_common._WARNED.clear()


# ------------------------------------------------------------------ helpers

def seed(conn, **cols) -> int:
    """Insert one opportunity row. Only url_hash and first_seen are needed."""
    row = {"url_hash": f"seed-{next(_seq)}", "first_seen": "2026-11-01T09:00:00Z",
           "last_seen": "2026-11-01T09:00:00Z", **cols}
    conn.execute(
        f"INSERT INTO opportunities ({', '.join(row)}) VALUES"
        f" ({', '.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def row_of(conn, oid) -> dict:
    return dict(conn.execute("SELECT * FROM opportunities WHERE id = ?",
                             (oid,)).fetchone())


def history(conn, oid) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM score_history WHERE opportunity_id = ? ORDER BY id",
        (oid,))]


def prefs_config(tmp, body: str, name: str = "prefs.md") -> dict:
    path = Path(tmp) / name
    path.write_text(body, encoding="utf-8")
    return {"prefs_file": str(path.resolve())}


def thresholds_of(config) -> tuple[dict, str]:
    """What the reader gives for a config, and what it wrote to stderr."""
    fresh_warnings()
    with captured_stderr() as buf:
        got = radar_common.read_score_thresholds(config)
    return got, buf.getvalue()


# A scored row as the pipeline stores it, for the writers to overwrite.
def scored_row(**over) -> dict:
    base = {
        "company": "Veltrix Diagnostics", "title": "Fractional Software Director",
        "location": "Ghent, Belgium", "salary_rate": "£800 per day",
        "cv_match": 93, "one_line_why": "Judged against the four gates.",
        "gate_sector": 1, "gate_cv": 1, "gate_location": 1, "gate_rate": 1,
        "tier": "top", "failed_gates": "[]", "cv_version": "cv-2026-10.txt",
        "score_basis": "card", "prompt_version": "abcd1234",
        "score_model": "claude-sonnet-4-6", "scored_at": "2026-11-05T10:00:00Z",
        "pay_currency": "GBP", "pay_period": "day", "pay_min": 800.0,
        "pay_max": 800.0, "role_key": "k",
    }
    base.update(over)
    return base


def old_shaped(path: Path) -> None:
    """Make path look like a database the code before this change wrote.

    The current schema, less the four score record columns and the history
    table, with the one-time migration flags already set, so that only what
    this change adds has anything to do on the first open.
    """
    conn = radar_common.get_db(path)
    for column in ("score_basis", "prompt_version", "score_model", "scored_at"):
        conn.execute(f"ALTER TABLE opportunities DROP COLUMN {column}")
    conn.execute("DROP TABLE score_history")
    conn.commit()
    conn.close()


def email_b64(subject: str, body: str) -> str:
    raw = json.dumps({"subject": subject, "from": "alerts@jobs.example",
                      "date": "2026-11-09", "body_text": body, "body_html": ""})
    return base64.b64encode(raw.encode()).decode("ascii")


def run_process_email(db: Path, b64: str) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), captured_stderr():
        code = process_email.main(["--mock", "--db", str(db), "--b64", b64])
    return {"code": code, "json": json.loads(out.getvalue().strip().splitlines()[-1])}


UNSCORABLE = ("RADAR-UNSCORABLE Director of Software\nNebulon Health · Ghent, "
              "Belgium\n€900 per day\nhttps://example.invalid/nebulon-record\n")


# The tier ladder exactly as it stood before the thresholds moved out of the
# code, with 70 and 40 written into it. The new code must agree with this at
# the default thresholds on every input it can be given.
def reference_ladder(gates: dict, cv):
    failed = [n for n in ("sector", "cv", "location", "rate")
              if gates.get(n) is False]
    real = bool(gates["sector"]) or (cv is not None and cv >= 40)
    out = {"tier": "reading", "failed_gates": failed, "filter_reason": None}
    if cv is not None and cv < 40:
        out.update(tier="filtered", filter_reason="cv match under 40")
        return out, real
    if not real:
        out.update(tier="filtered", filter_reason="not a real leadership role")
        return out, real
    if not failed:
        out["tier"] = "top"
        return out, real
    if len(failed) == 1 and failed[0] in ("location", "rate"):
        out["tier"] = "question"
    return out, real


# ------------------------------------------------------------------- checks

def prompt_version_checks() -> None:
    pv = radar_common.prompt_version()
    text = (REPO_ROOT / "prompts" / "scorer.md").read_text(encoding="utf-8")
    check(re.fullmatch(r"[0-9a-f]{8}", pv) is not None
          and pv == hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
          "the prompt version is eight hex characters, the sha256 of the file")
    check(radar_common.prompt_hash(text + " ") != pv
          and radar_common.prompt_hash(text.replace("scorer", "scorer ", 1)) != pv,
          "any edit to the file changes the version")
    check(radar_common.prompt_hash(text.replace("\n", "\r\n")) == pv,
          "the same file hashes the same whatever its line endings")
    with captured_stderr():
        blocks = score_item.build_scorer_system(CFG)
    # A card is scored with the file above the full description marker, the
    # bytes it always was. The full description's part is sent to a full
    # description only, and the version is the hash of the whole file either way.
    card_part = text.partition(score_item.SPEC_MARKER)[0]
    check(isinstance(blocks, list) and blocks.prompt_version == pv
          and json.loads(json.dumps(blocks)) == list(blocks)
          and blocks[0]["text"].startswith(card_part)
          and score_item.SPEC_MARKER not in blocks[0]["text"],
          "the system blocks are still a plain list for the API and carry the "
          "version of the rubric they were built from, and a card is scored "
          "with the file above the full description marker")
    head = text.split("-->")[0]
    check(text.lstrip().startswith("<!--") and "v2.0, 3 August 2026" in head
          and "-->" in text[:600],
          "the human label stays in the header of scorer.md, now as a comment")
    check(not re.search(r"\b(68|70)\b", text) and "cv_pass_score" in text,
          "the rubric no longer states the pass mark or an example that only makes "
          "sense beside it, it points at the setting")


def thresholds_checks() -> None:
    template = REPO_ROOT / "config" / "preferences.template.md"
    got, said = thresholds_of({"prefs_file": str(template)})
    check(got == {"pass": 70, "floor": 40} and not said,
          f"the template carries both lines at 70 and 40 and warns of nothing ({got})")
    body = (template.read_text(encoding="utf-8")
            if template.exists() else "")
    check(re.search(r"^cv_pass_score: 70$", body, re.M) is not None
          and re.search(r"^cv_floor_score: 40$", body, re.M) is not None
          and re.search(r"^day_rate_floor_gbp: 750$", body, re.M) is not None,
          "the template holds the two new lines beside the two rate floors")
    live = radar_common.read_score_thresholds(CFG)
    check(live == {"pass": 70, "floor": 40}
          and radar_common.load_config()["prefs_file"] == str(TEMPLATE),
          f"this runner reads the template and never the owner's own file ({live})")

    with tempfile.TemporaryDirectory() as tmp:
        got, said = thresholds_of(prefs_config(
            tmp, "cv_pass_score: 65\ncv_floor_score: 35\n"))
        check(got == {"pass": 65, "floor": 35} and not said,
              f"both lines are read from a preferences file ({got})")
        got, _ = thresholds_of(prefs_config(
            tmp, "CV_PASS_SCORE:   72.5  \ncv_floor_score:\t30\n", "case.md"))
        check(got == {"pass": 72.5, "floor": 30},
              f"case, spaces, tabs and a decimal are tolerated ({got})")

        got, said = thresholds_of(prefs_config(tmp, "day_rate_floor_gbp: 750\n", "none.md"))
        check(got == {"pass": 70, "floor": 40}
              and said.count("WARNING.") == 1
              and "cv_pass_score" in said and "cv_floor_score" in said
              and "using 70" in said and "using 40" in said,
              "a missing line gives 70 and 40 and one warning naming both lines")
        fresh_warnings()
        with captured_stderr() as buf:
            for _ in range(5):
                radar_common.read_score_thresholds(prefs_config(tmp, "x\n", "again.md"))
        check(buf.getvalue().count("WARNING.") == 1,
              "the warning is given once per run however often the lines are read "
              f"(got {buf.getvalue().count('WARNING.')} for two missing lines)")
        fresh_warnings()
        check(not radar_common.thresholds_fell_back(),
              "a run that has read nothing has not fallen back")
        with captured_stderr():
            radar_common.read_score_thresholds(prefs_config(tmp, "x\n", "fell.md"))
        check(radar_common.thresholds_fell_back(),
              "a run that found a line missing knows it, for the run record")
        fresh_warnings()
        with captured_stderr():
            radar_common.read_score_thresholds(prefs_config(
                tmp, "cv_pass_score: 70\ncv_floor_score: 40\n", "fine.md"))
        check(not radar_common.thresholds_fell_back(),
              "and a run that read both lines has not")

        got, said = thresholds_of(prefs_config(
            tmp, "cv_pass_score: 80\n", "one.md"))
        check(got == {"pass": 80, "floor": 40}
              and said.count("WARNING.") == 1 and "cv_floor_score" in said
              and "cv_pass_score" not in said,
              "one missing line falls back alone, the other is still honoured")
        got, said = thresholds_of(prefs_config(
            tmp, "cv_pass_score: seventy\ncv_floor_score: 40%\n", "words.md"))
        check(got == {"pass": 70, "floor": 40} and said.count("WARNING.") == 1
              and "'cv_pass_score: seventy'" in said
              and "'cv_floor_score: 40%'" in said,
              "a value that is not a number is unreadable, the default stands and the "
              "warning shows the line it could not read")
        got, said = thresholds_of(prefs_config(
            tmp, "cv_floor_score: 75\n", "floor-only.md"))
        check(got == {"pass": 70, "floor": 40} and said.count("WARNING.") == 1
              and "do not fit" in said and "no cv_pass_score line" in said,
              "a valid floor above the default pass mark cannot stand alone, and the "
              "warning says both the missing line and why the pair was refused")
        got, said = thresholds_of(prefs_config(
            tmp, "cv_pass_score: 50\ncv_floor_score: 50\n", "equal.md"))
        check(got == {"pass": 50, "floor": 50} and not said,
              "a floor equal to the pass mark is allowed")
        bom = Path(tmp) / "bom.md"
        bom.write_bytes(b"\xef\xbb\xbfcv_pass_score: 66\ncv_floor_score: 36\n")
        got, said = thresholds_of({"prefs_file": str(bom)})
        check(got == {"pass": 66, "floor": 36} and not said,
              "a byte order mark in front of the first line does not hide it")

        class Dead:
            def write(self, *_args):
                raise OSError("broken pipe")

            def flush(self):
                pass

        fresh_warnings()
        saved_err, sys.stderr = sys.stderr, Dead()
        try:
            got = radar_common.read_score_thresholds(
                prefs_config(tmp, "cv_pass_score: 80\n", "dead.md"))
        except Exception as err:  # noqa: BLE001  the check is that nothing escapes
            got = f"raised {type(err).__name__}"
        finally:
            sys.stderr = saved_err
        check(got == {"pass": 80, "floor": 40},
              "a stderr that cannot be written to neither stops the reader nor costs it "
              f"the line it did read ({got})")
        got, said = thresholds_of(prefs_config(
            tmp, "cv_pass_score: 150\ncv_floor_score: 40\n", "big.md"))
        check(got == {"pass": 70, "floor": 40} and "WARNING." in said,
              "a pass mark over 100 is refused, both fall back to 70 and 40")
        got, said = thresholds_of(prefs_config(
            tmp, "cv_pass_score: 50\ncv_floor_score: 60\n", "swap.md"))
        check(got == {"pass": 70, "floor": 40} and "WARNING." in said,
              "a floor above the pass mark is refused, both fall back to 70 and 40")
        bad = Path(tmp) / "binary.md"
        bad.write_bytes(b"\xff\xfe\x00cv_pass_score: 70\n\x80\x81")
        got, said = thresholds_of({"prefs_file": str(bad)})
        check(got == {"pass": 70, "floor": 40} and "WARNING." in said,
              "a file that cannot be decoded gives 70 and 40, it never raises")
        got, said = thresholds_of({"prefs_file": 12345})
        check(got == {"pass": 70, "floor": 40} and "WARNING." in said,
              "a broken config gives 70 and 40, it never raises")

        saved = radar_common.REPO_ROOT
        radar_common.REPO_ROOT = Path(tmp) / "empty-checkout"
        try:
            got, said = thresholds_of({"prefs_file": "config/profile/preferences.md"})
        finally:
            radar_common.REPO_ROOT = saved
        check(got == {"pass": 70, "floor": 40} and "No preferences file" in said,
              "no preferences file at all gives 70 and 40 and says so")

    check(radar_common.read_rate_floors(CFG).keys() == {"day", "salary"},
          "read_rate_floors still returns exactly its two floors")


def tier_checks() -> None:
    import itertools
    th = radar_common.read_score_thresholds(CFG)
    wrong = []
    combos = 0
    for combo in itertools.product((True, False, None), repeat=4):
        gates = dict(zip(("sector", "cv", "location", "rate"), combo))
        for cv in [None] + list(range(0, 101)):
            combos += 1
            want, want_real = reference_ladder(gates, cv)
            real = score_item.is_real(gates["sector"], cv, th["floor"])
            got = score_item.derive_tier(gates, cv, real, th["floor"])
            gate = score_item.cv_gate(cv, th["pass"])
            if (got != want or real != want_real
                    or gate != ((cv >= 70) if cv is not None else None)):
                wrong.append((combo, cv))
    check(not wrong,
          f"at 70 and 40 the tier ladder, the CV gate and is_real agree with "
          f"the old code on all {combos} combinations of gates and CV number "
          f"({len(wrong)} differ)")

    G = lambda **k: {"sector": True, "cv": True, "location": True, "rate": True, **k}
    check(score_item.derive_tier(G(cv=False), 22, True)["filter_reason"]
          == "cv match under 40",
          "the default floor still writes cv match under 40")
    check(score_item.derive_tier(G(cv=False), 44, True, 45)["filter_reason"]
          == "cv match under 45"
          and score_item.derive_tier(G(), 44, True, 45)["tier"] == "filtered"
          and score_item.derive_tier(G(), 45, True, 45)["tier"] == "top",
          "the reason is built from the floor in force, and 45 itself is not filtered")
    check(score_item.derive_tier(G(cv=False), 42.5, True, 42.5)["tier"] == "reading"
          and score_item.derive_tier(G(), 40, True, 42.5)["filter_reason"]
          == "cv match under 42.5",
          "a floor with a decimal reads back without padding")
    check(score_item.cv_gate(70, 70) is True and score_item.cv_gate(69, 70) is False
          and score_item.cv_gate(72, 75) is False and score_item.cv_gate(None, 70) is None,
          "the CV gate passes at the pass mark itself, and an unknown is not a fail")
    check(score_item.is_real(False, 44, 45) is False
          and score_item.is_real(False, 45, 45) is True
          and score_item.is_real(True, 0, 45) is True
          and score_item.is_real(False, None, 45) is False,
          "a role is real on the sector gate or a CV number that reaches the floor")


def score_record_checks() -> None:
    with captured_stderr():
        blocks = score_item.build_scorer_system(CFG)
    pv = radar_common.prompt_version()
    opp = {"company": "Veltrix Diagnostics",
           "title": "Fractional Software Director, IVD platform",
           "location": "Ghent, Belgium (Hybrid)", "salary_rate": "€950 per day",
           "source_url": "https://example.invalid/veltrix"}
    scored, usage, note = score_item.score_opportunity(
        opp, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS)
    check(note is None and scored["score_basis"] == "card"
          and scored["prompt_version"] == pv and scored["score_model"] == "mock"
          and ISO.fullmatch(scored["scored_at"] or "") is not None,
          "a score carries its basis, prompt version, model and date")
    check(usage.get("model") == "mock" and usage.get("stop_reason") == "end_turn",
          "the usage the scorer returns names the model and how it stopped")
    # A full description is a mode of its own since Prepare application: the
    # advert says so, the reply carries its asks, and a card is not one.
    spec_opp = {**opp, "basis": "spec", "description": "- IEC 62304 experience"}
    spec_blocks = score_item.build_scorer_system(CFG, basis="spec")
    spec, _, _ = score_item.score_opportunity(
        spec_opp, spec_blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS,
        basis="spec")
    check(spec["score_basis"] == "spec", "a scorer given the full spec records spec")
    plain, _, _ = score_item.score_opportunity(
        opp, list(blocks), CFG, mock_fn=mocks.mock_scorer, floors=FLOORS)
    check(plain["prompt_version"] == pv,
          "blocks that carry no version fall back to the hash of the file")

    strict, _, _ = score_item.score_opportunity(
        opp, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS,
        thresholds={"pass": 95, "floor": 40})
    check(strict["gate_cv"] == 0 and strict["tier"] == "reading"
          and strict["cv_match"] == 93,
          "a higher pass mark fails the CV gate of a 93 and the tier follows")
    high_floor, _, _ = score_item.score_opportunity(
        opp, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS,
        thresholds={"pass": 70, "floor": 95})
    check(high_floor["tier"] == "filtered"
          and high_floor["filter_reason"] == "cv match under 95",
          "a higher floor filters the same role and names the floor it used")
    default, _, _ = score_item.score_opportunity(
        opp, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS)
    check(default["tier"] == "top" and default["gate_cv"] == 1
          and default["filter_reason"] is None,
          "left alone the same role is Top, as it always was")
    generic = {"company": "Brightline Payments", "title": "Software Test Analyst",
               "location": "Manchester, England (Hybrid)", "salary_rate": "£45,000 per annum"}
    weak, _, _ = score_item.score_opportunity(
        generic, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS)
    lower, _, _ = score_item.score_opportunity(
        generic, blocks, CFG, mock_fn=mocks.mock_scorer, floors=FLOORS,
        thresholds={"pass": 70, "floor": 30})
    check(weak["cv_match"] == 30 and weak["tier"] == "filtered"
          and weak["filter_reason"] == "cv match under 40"
          and lower["tier"] == "reading" and lower["filter_reason"] is None,
          "a floor lowered to 30 stops a 30 filtering a role, and the role is judged "
          "real on that floor as well")


def inbox_checks() -> None:
    pv = radar_common.prompt_version()
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "inbox.sqlite"
        mail = json.loads((SAMPLES / "01_linkedin_multi_role.json").read_text("utf-8"))
        with counted_reads() as reads:
            res = run_process_email(db, email_b64(mail["subject"], mail["body_text"]))
        check(res["code"] == 0 and res["json"]["new"] == 3,
              "the multi role alert stores three roles")
        check(len(reads) == 1,
              "the inbox run reads the thresholds once for the whole run, "
              f"however many roles it scores (read {len(reads)} times for 3 roles)")
        conn = radar_common.get_db(db)
        rows = [dict(r) for r in conn.execute("SELECT * FROM opportunities ORDER BY id")]
        check(all(r["score_basis"] == "card" and r["prompt_version"] == pv
                  and r["score_model"] == "mock"
                  and ISO.fullmatch(r["scored_at"] or "") for r in rows),
              "every stored role carries basis, prompt version, model and date")
        ok = True
        for r in rows:
            held = history(conn, r["id"])
            ok = ok and len(held) == 1 and held[0]["event"] == "scored" \
                and held[0]["writer"] == "process_email" \
                and held[0]["cv_match"] == r["cv_match"] \
                and held[0]["tier"] == r["tier"] \
                and held[0]["one_line_why"] == r["one_line_why"] \
                and held[0]["scored_at"] == r["scored_at"] \
                and held[0]["score_basis"] == "card" \
                and held[0]["prompt_version"] == pv \
                and held[0]["score_model"] == "mock" \
                and held[0]["cv_version"] == r["cv_version"] \
                and json.loads(held[0]["gates"]) == {
                    "sector": r["gate_sector"], "cv": r["gate_cv"],
                    "location": r["gate_location"], "rate": r["gate_rate"]} \
                and ISO.fullmatch(held[0]["recorded_at"] or "")
        check(ok, "each new score wrote exactly one history row holding its own values")
        again = run_process_email(db, email_b64(mail["subject"], mail["body_text"]))
        count = conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
        check(again["json"]["new"] == 0 and count == 3,
              "a duplicate alert scores nothing and writes no history")

        run_process_email(db, email_b64("One new role for you", UNSCORABLE))
        bad = conn.execute("SELECT * FROM opportunities WHERE company = 'Nebulon Health'"
                           ).fetchone()
        check(bad is not None and bad["tier"] is None and bad["score_basis"] is None
              and bad["prompt_version"] is None and bad["score_model"] is None
              and bad["scored_at"] is None and not history(conn, bad["id"]),
              "a role that could not be scored is stored with no record of a score "
              "and no history row")

        with captured_stderr():
            done = rescore.rescore_opportunities(conn, CFG, {})
        again = row_of(conn, bad["id"])
        held = history(conn, bad["id"])
        check(done == (1, 0) and again["tier"] is not None
              and again["score_basis"] == "card" and again["prompt_version"] == pv
              and again["score_model"] == "mock" and ISO.fullmatch(again["scored_at"] or "")
              and [h["event"] for h in held] == ["scored"]
              and held[0]["writer"] == "rescore",
              "rescoring a role that had no score records it, and there was "
              "nothing outgoing to keep")
        conn.close()


def rescore_checks() -> None:
    pv = radar_common.prompt_version()
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "rescore.sqlite")
        legacy = seed(conn, company="Veltrix Diagnostics",
                      title="Fractional Software Director, IVD platform",
                      location="Ghent, Belgium (Hybrid)", salary_rate="€950 per day",
                      cv_match=61, want_match=70, combined=66,
                      one_line_why="An old rubric's sentence.",
                      cv_version="cv-old.txt", score_basis="card",
                      prompt_version="pre-record", score_model="unknown")
        before = row_of(conn, legacy)
        with captured_stderr():
            fixed, failed, _ = rescore.rescore_stale_cv(conn, CFG, {}, cap=5)
        after = row_of(conn, legacy)
        held = history(conn, legacy)
        check((fixed, failed) == (1, 0) and after["cv_match"] == 93
              and after["prompt_version"] == pv and after["score_model"] == "mock"
              and after["score_basis"] == "card"
              and ISO.fullmatch(after["scored_at"] or ""),
              "a stale-CV re-score overwrites the score and records how the new one was made")
        check([h["event"] for h in held] == ["replaced", "scored"]
              and held[0]["writer"] == "rescore",
              "a re-score writes the outgoing values first, then the new score")
        old = held[0]
        check(old["cv_match"] == 61 and old["one_line_why"] == "An old rubric's sentence."
              and old["cv_version"] == "cv-old.txt" and old["tier"] is None
              and old["score_basis"] == "card" and old["prompt_version"] == "pre-record"
              and old["score_model"] == "unknown" and old["scored_at"] is None
              and json.loads(old["gates"]) == {"sector": None, "cv": None,
                                               "location": None, "rate": None},
              "the outgoing row holds the old values with their old date, basis, "
              "prompt version, model and CV version")
        new = held[1]
        check(new["cv_match"] == after["cv_match"] and new["tier"] == after["tier"]
              and new["scored_at"] == after["scored_at"]
              and new["prompt_version"] == pv and new["recorded_at"] >= old["recorded_at"],
              "the new score's history row matches the row it describes")
        check(before["cv_match"] == 61, "the row held 61 before, so the history is not the row")

        radar_common.archive_score(conn, legacy, "test")
        conn.commit()
        check(len(history(conn, legacy)) == 2,
              "keeping a score that is already the newest history row writes nothing")
        # Straight through the writer again, without changing the row first,
        # so the only thing that can differ is whether it is kept twice.
        with captured_stderr():
            rescore._rescore_rows(conn, CFG, {}, [conn.execute(
                "SELECT * FROM opportunities WHERE id = ?", (legacy,)).fetchone()])
        held = history(conn, legacy)
        check([h["event"] for h in held] == ["replaced", "scored", "scored"],
              "a second re-score does not keep the same score twice, only the new one is added")

        old_style = seed(conn, company="Caldora Medical", title="Head of Software Engineering",
                         location="Cambridge, England (On-site)", salary_rate="£95,000 per annum",
                         cv_match=80, want_match=80, combined=80, tier=None,
                         one_line_why="Additive era.", score_basis="card",
                         prompt_version="pre-record", score_model="unknown")
        with captured_stderr():
            done = rescore.rescore_opportunities(conn, CFG, {})
        held = history(conn, old_style)
        check(done == (1, 0) and [h["event"] for h in held] == ["replaced", "scored"]
              and held[0]["cv_match"] == 80 and held[0]["tier"] is None
              and held[0]["one_line_why"] == "Additive era.",
              "an additive-era row re-scored through the unscored list has its old score kept")

        batch = [seed(conn, company=f"Batch {n}", title=f"Fractional Software Director {n}",
                      location="Ghent, Belgium (Hybrid)", cv_match=50, want_match=50,
                      combined=50, one_line_why="Old.", score_basis="card",
                      prompt_version="pre-record", score_model="unknown")
                 for n in range(3)]
        rows = [conn.execute("SELECT * FROM opportunities WHERE id = ?", (i,)).fetchone()
                for i in batch]
        with counted_reads() as reads, captured_stderr():
            rescore._rescore_rows(conn, CFG, {}, rows)
        check(len(reads) == 1 and all(len(history(conn, i)) == 2 for i in batch),
              "a re-score batch reads the thresholds once, however many rows it holds "
              f"(read {len(reads)} times for 3 rows)")
        count = conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
        usage: dict = {}
        with captured_stderr():
            res = rescore.regate_backlog(conn, CFG, usage, cap=5, dry_run=True)
        check(res["dry_run"] and conn.execute("SELECT COUNT(*) FROM score_history"
                                              ).fetchone()[0] == count,
              "a dry run of the regate backlog writes no history")
        regated = seed(conn, company="Regate Co", title="Head of Software Engineering",
                       location="Ghent, Belgium (Hybrid)", cv_match=66, want_match=66,
                       combined=66, one_line_why="Back catalogue.", score_basis="card",
                       prompt_version="pre-record", score_model="unknown")
        with captured_stderr():
            res = rescore.regate_backlog(conn, CFG, usage, cap=5, dry_run=False)
        held = history(conn, regated)
        check(res["rescored"] == 1 and [h["event"] for h in held] == ["replaced", "scored"]
              and held[0]["cv_match"] == 66 and held[0]["one_line_why"] == "Back catalogue."
              and row_of(conn, regated)["tier"] is not None,
              "re-scoring the back catalogue keeps each outgoing score too")
        conn.close()


def concurrency_checks() -> None:
    """What is kept is what is replaced, even with another writer about."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "lock.sqlite"
        conn = radar_common.get_db(path)
        oid = seed(conn, **scored_row())
        rival = sqlite3.connect(path, timeout=0.2)
        rival.row_factory = sqlite3.Row
        outcome: dict = {}

        def sneak(statement):
            # The moment the history row is about to be written, a rival
            # tries to change the very score being kept.
            if statement.startswith("INSERT INTO score_history") and "tried" not in outcome:
                outcome["tried"] = True
                try:
                    rival.execute("UPDATE opportunities SET cv_match = 11, tier = 'filtered'"
                                  " WHERE id = ?", (oid,))
                    rival.commit()
                    outcome["rival"] = "wrote"
                except sqlite3.OperationalError:
                    rival.rollback()
                    outcome["rival"] = "blocked"

        conn.set_trace_callback(sneak)
        radar_common.archive_score(conn, oid, "test")
        conn.set_trace_callback(None)
        conn.execute("UPDATE opportunities SET cv_match = 50 WHERE id = ?", (oid,))
        conn.commit()
        held = history(conn, oid)
        check(outcome.get("rival") == "blocked" and len(held) == 1
              and held[0]["cv_match"] == 93 and held[0]["tier"] == "top",
              "a rival cannot change a score between it being read and being kept, "
              f"so the history holds the 93 that was replaced (rival {outcome.get('rival')})")
        rival.close()
        conn.close()


# Every function in scripts/ that writes to the opportunities table, and what it
# is. A score writer must keep the history. The others touch no score column.
SCORE_WRITERS = {
    ("scripts/process_email.py", "insert_opportunity"),
    ("scripts/rescore.py", "_rescore_rows"),
    ("scripts/regate_rates.py", "regate"),
    ("scripts/check_jobs.py", "apply_page"),
    ("scripts/prepare_application.py", "_score"),    # a score against the full description
}
OTHER_WRITERS = {
    ("scripts/backfill_pay.py", "main"),             # pay columns, filled once
    ("scripts/build_digest.py", "commit_token"),     # status after a digest
    ("scripts/check_jobs.py", "expire_stale"),       # expired_at
    ("scripts/check_jobs.py", "run_checks"),         # last_checked_at, closed_at
    ("scripts/engagement.py", "backfill"),           # the badge, empty cells only
    ("scripts/enrich_company.py", "enrich_company"), # region
    ("scripts/prepare_application.py", "_fetch_and_store"),  # description_text only
    ("scripts/process_email.py", "main"),            # sightings and revival
    ("scripts/radar_common.py", "_migrate"),         # backfills and the labels
    ("scripts/radar_common.py", "recompute_regions"),
    ("scripts/serve_dashboard.py", "set_acknowledged"),
    ("scripts/serve_dashboard.py", "set_applied"),    # applied_at, applied_cv
    ("scripts/serve_dashboard.py", "set_lifecycle"),
    ("scripts/touch.py", "cmd_mark.flip_opportunity"),
}


def _functions(tree):
    """Every function in a module with its dotted name, nested ones included."""
    def visit(node, qualified):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name if qualified is None else f"{qualified}.{child.name}"
                yield name, child
                yield from visit(child, name)
            else:
                yield from visit(child, qualified)
    yield from visit(tree, None)


def _own_strings(function):
    """The string constants in a function, less those of functions inside it."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
        stack.extend(ast.iter_child_nodes(node))


def opportunity_writers() -> dict:
    """Each function in scripts/ that carries SQL writing to opportunities.

    It reads the SQL text the code carries, and the function it finds is the
    innermost one. A statement built so that no piece of it begins UPDATE
    opportunities would not be seen, which is why the score writers are also
    named above and checked for the call.
    """
    pattern = re.compile(
        r"(UPDATE|INSERT( OR \w+)? INTO|DELETE FROM|REPLACE INTO) opportunities\b",
        re.IGNORECASE)
    found: dict = {}
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name, function in _functions(ast.parse(text)):
            if any(pattern.match(" ".join(s.split())) for s in _own_strings(function)):
                found[(f"scripts/{path.name}", name)] = ast.get_source_segment(text, function) or ""
    return found


def writers_checks() -> None:
    writers = opportunity_writers()
    known = SCORE_WRITERS | OTHER_WRITERS
    check(set(writers) == known,
          "every function that writes to the opportunities table is one this runner "
          "has classified, so a new writer cannot skip the history unnoticed "
          f"(unclassified {sorted(set(writers) - known)}, gone {sorted(known - set(writers))})")
    check(all("archive_score" in (writers.get(w) or "") or "record_score" in (writers.get(w) or "")
              for w in SCORE_WRITERS),
          "and every score writer calls into the history")
    check(all("archive_score" not in (writers.get(w) or "") and "record_score" not in (writers.get(w) or "")
              for w in OTHER_WRITERS if w in writers),
          "while the writers that touch no score do not pretend to")


def history_failure_checks() -> None:
    real = radar_common.record_score

    def broken(*_a, **_k):
        raise RuntimeError("the history is broken")

    radar_common.record_score = broken
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "broken.sqlite"
            mail = json.loads((SAMPLES / "02_reed_wrong_rate.json").read_text("utf-8"))
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = process_email.main(["--mock", "--db", str(db), "--b64",
                                           email_b64(mail["subject"], mail["body_text"])])
            conn = radar_common.get_db(db)
            row = conn.execute("SELECT * FROM opportunities").fetchone()
            check(code == 0 and row is not None and row["tier"] is not None
                  and row["score_basis"] == "card"
                  and "history row for a new score was not written" in err.getvalue()
                  and not conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0],
                  "a history row that cannot be written costs the inbox nothing, the advert "
                  "and its score are stored and a warning says so")
            conn.close()
    finally:
        radar_common.record_score = real


def regate_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "regate.sqlite")
        moves = seed(conn, **scored_row(pay_min=700.0, pay_max=700.0, gate_rate=1,
                                        salary_rate="£700 per day"))
        aux, _ = regate_rates.judge_row(scored_row(title="Steady role"), FLOORS, CFG)
        stays = seed(conn, **{**scored_row(title="Steady role", role_key="k2"), **aux})
        legacy = seed(conn, **scored_row(title="Legacy label", role_key="k3",
                                         pay_min=600.0, pay_max=600.0,
                                         prompt_version="pre-record",
                                         score_model="unknown", scored_at=None))
        retired = seed(conn, **scored_row(title="Closed", role_key="k4",
                                          pay_min=600.0, pay_max=600.0,
                                          closed_at="2026-11-02T00:00:00Z"))
        snapshot = {i: row_of(conn, i) for i in (moves, stays, legacy, retired)}

        dry = regate_rates.regate(conn, CFG, FLOORS, write=False)
        check(dry["mode"] == "dry-run" and dry["rows_changed"] == 2
              and dry["history_rows_written"] == 0
              and dry["tier_moves"] == {"top to question": 2}
              and not conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
              and all(row_of(conn, i) == snapshot[i] for i in snapshot),
              "a regate dry run reports the moves and changes nothing, history included")

        res = regate_rates.regate(conn, CFG, FLOORS, write=True)
        check(res["rows_changed"] == 2 and res["history_rows_written"] == 2
              and res["thresholds"] == {"pass": 70, "floor": 40},
              "writing a regate keeps the outgoing score of each row it changes")
        h = history(conn, moves)
        check(len(h) == 1 and h[0]["event"] == "replaced" and h[0]["writer"] == "regate_rates"
              and h[0]["tier"] == "top" and h[0]["cv_match"] == 93
              and json.loads(h[0]["gates"])["rate"] == 1
              and h[0]["prompt_version"] == "abcd1234"
              and h[0]["score_model"] == "claude-sonnet-4-6"
              and h[0]["scored_at"] == "2026-11-05T10:00:00Z"
              and h[0]["score_basis"] == "card",
              "the history row holds the tier and gates it replaced, with the "
              "score's own date, basis, prompt version and model")
        now = row_of(conn, moves)
        check(now["tier"] == "question" and now["gate_rate"] == 0
              and now["scored_at"] == "2026-11-05T10:00:00Z"
              and now["prompt_version"] == "abcd1234",
              "the row is re-tiered, and the record of who scored it is untouched")
        old = history(conn, legacy)
        check(len(old) == 1 and old[0]["prompt_version"] == "pre-record"
              and old[0]["scored_at"] is None and old[0]["score_model"] == "unknown",
              "a score made before the record gets its old label in the history")
        check(not history(conn, stays) and not history(conn, retired)
              and row_of(conn, stays) == snapshot[stays]
              and row_of(conn, retired) == snapshot[retired],
              "a row regate does not change gets no history, and a retired row is left alone")

        again = regate_rates.regate(conn, CFG, FLOORS, write=True)
        check(again["rows_changed"] == 0 and again["history_rows_written"] == 0
              and len(history(conn, moves)) == 1,
              "regating again changes nothing and keeps nothing")

        weak = seed(conn, **scored_row(title="Thirty five", role_key="k6", cv_match=35,
                                       gate_cv=0, gate_sector=0, tier="filtered",
                                       failed_gates='["sector", "cv"]',
                                       filter_reason="cv match under 40", gate_rate=1,
                                       pay_min=None, pay_max=None, pay_currency=None,
                                       pay_period=None))
        held_back = regate_rates.regate(conn, CFG, FLOORS, write=False)
        eased = regate_rates.regate(conn, CFG, FLOORS, write=False,
                                    thresholds={"pass": 70, "floor": 30})
        check(held_back["tier_moves"] == {} and eased["tier_moves"] == {"filtered to reading": 1},
              "a floor lowered to 30 lets a 35 with a failed sector back to Reading on a "
              "regate, so the floor reaches is_real there too")
        db_file = Path(tmp) / "regate.sqlite"
        conn.commit()
        with counted_reads() as reads, contextlib.redirect_stdout(io.StringIO()):
            regate_rates.main(["--db", str(db_file)])
        # The command leaves its connection to be closed when it is collected,
        # and Windows will not delete a database file while one is open.
        gc.collect()
        check(len(reads) == 1,
              f"the regate command reads the thresholds once (read {len(reads)} times)")
        conn.execute("DELETE FROM opportunities WHERE id = ?", (weak,))
        conn.commit()
        low = seed(conn, **scored_row(title="Fifty five", role_key="k5", cv_match=55,
                                      gate_cv=0, tier="reading", failed_gates='["cv"]',
                                      gate_rate=1, pay_min=None, pay_max=None,
                                      pay_currency=None, pay_period=None))
        quiet = regate_rates.regate(conn, CFG, FLOORS, write=False)
        loud = regate_rates.regate(conn, CFG, FLOORS, write=False,
                                   thresholds={"pass": 70, "floor": 60})
        check(row_of(conn, low)["tier"] == "reading" and quiet["rows_changed"] == 0
              and loud["tier_moves"] == {"reading to filtered": 1},
              "the floor from the preferences reaches a regate, so 55 filters under 60")
        regate_rates.regate(conn, CFG, FLOORS, write=True,
                            thresholds={"pass": 70, "floor": 60})
        row = row_of(conn, low)
        held = history(conn, low)
        check(row["tier"] == "filtered" and row["filter_reason"] == "cv match under 60"
              and len(held) == 1 and held[0]["tier"] == "reading",
              "the stored reason names the floor used, and the reading tier was kept")
        conn.close()


def page_checker_checks() -> None:
    page = {"description": "A contract role.", "employment_type": None,
            "pay": {"currency": "GBP", "period": "day", "min": 600.0, "max": 600.0,
                    "text": "£600 per day"},
            "pay_from": "top card"}
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "page.sqlite")
        oid = seed(conn, **scored_row(pay_min=None, pay_max=None, pay_currency=None,
                                      pay_period=None, gate_rate=None, salary_rate=""))
        row = row_of(conn, oid)
        out = check_jobs.apply_page(conn, row, page, FLOORS, CFG, write=False,
                                    thresholds={"pass": 70, "floor": 40})
        check(out["tier"] and out["tier"]["to"] == "question"
              and not history(conn, oid) and row_of(conn, oid) == row,
              "a page check that is not written would re-tier the row and keeps nothing")
        check_jobs.apply_page(conn, row, page, FLOORS, CFG, write=True,
                              thresholds={"pass": 70, "floor": 40})
        conn.commit()
        now = row_of(conn, oid)
        held = history(conn, oid)
        check(now["tier"] == "question" and now["pay_source"] == "page"
              and len(held) == 1 and held[0]["event"] == "replaced"
              and held[0]["writer"] == "check_jobs" and held[0]["tier"] == "top"
              and json.loads(held[0]["gates"])["rate"] is None
              and held[0]["scored_at"] == "2026-11-05T10:00:00Z"
              and held[0]["prompt_version"] == "abcd1234",
              "the page checker's rate re-tier keeps the outgoing score before it overwrites it")
        low = seed(conn, **scored_row(title="Fifty five", role_key="k8", cv_match=55,
                                      pay_min=None, pay_max=None, pay_currency=None,
                                      pay_period=None, gate_rate=None, salary_rate=""))
        check_jobs.apply_page(conn, row_of(conn, low), page, FLOORS, CFG, write=True,
                              thresholds={"pass": 70, "floor": 60})
        conn.commit()
        sixty = row_of(conn, low)
        check(sixty["tier"] == "filtered" and sixty["filter_reason"] == "cv match under 60"
              and len(history(conn, low)) == 1 and history(conn, low)[0]["tier"] == "top",
              "the page checker's re-tier judges the CV number against the floor it is given")
        quiet = seed(conn, **scored_row(title="No figure", role_key="k9", pay_min=None,
                                        pay_max=None, pay_currency=None, pay_period=None,
                                        gate_rate=None, salary_rate=""))
        check_jobs.apply_page(conn, row_of(conn, quiet),
                              {"description": "Nothing about pay.", "employment_type": None,
                               "pay": None, "pay_from": None},
                              FLOORS, CFG, write=True,
                              thresholds={"pass": 70, "floor": 40})
        conn.commit()
        check(not history(conn, quiet) and row_of(conn, quiet)["tier"] == "top",
              "a page that changes no score keeps nothing")
        conn.close()


def page_loop_checks() -> None:
    """The real page-check loop, with only the fetch stubbed."""
    fixtures = REPO_ROOT / "test" / "fixtures" / "linkedin"
    jid = "4482760631"
    url = f"https://www.linkedin.com/jobs/view/{jid}/"
    page = (fixtures / "live.html").read_text(encoding="utf-8").replace(
        '<div class="top-card-layout__cta-container">',
        '<div class="salary compensation__salary">£600 a day</div>'
        '<div class="top-card-layout__cta-container">', 1)
    real = check_jobs.fetch_job_page
    check_jobs.fetch_job_page = lambda _url: (200, url, page)
    try:
        for write in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                conn = radar_common.get_db(Path(tmp) / "loop.sqlite")
                oid = seed(conn, **scored_row(
                    title="Principal Medical Device Engineer", source_url=url,
                    pay_min=None, pay_max=None, pay_currency=None,
                    pay_period=None, gate_rate=None, salary_rate=""))
                before = row_of(conn, oid)
                with counted_reads() as reads:
                    res = check_jobs.run_checks(conn, cap=5, write=write,
                                                pause=lambda: None,
                                                wait=lambda s: None)
                after = row_of(conn, oid)
                check(len(reads) == 1,
                      "the page-check loop reads the thresholds once and hands them to "
                      f"every page it re-tiers (read {len(reads)} times)")
                held = history(conn, oid)
                if write:
                    check(res["pages_read"] == 1 and res["tier_moves"] == {"top to question": 1}
                          and after["tier"] == "question" and after["last_checked_at"]
                          and len(held) == 1 and held[0]["event"] == "replaced"
                          and held[0]["writer"] == "check_jobs" and held[0]["tier"] == "top"
                          and held[0]["scored_at"] == "2026-11-05T10:00:00Z",
                          "the live page-check loop re-tiers a role from the page's pay and "
                          "keeps the score it replaced, in the same run that stamps the check")
                else:
                    check(res["pages_read"] == 1 and after == before and not held,
                          "the page-check loop in dry run reads the page and changes and "
                          "keeps nothing")
                conn.close()
    finally:
        check_jobs.fetch_job_page = real


def truncation_checks() -> None:
    calls: list[str] = []
    queue: list[tuple[str, str]] = []
    extract_stop = ["end_turn"]

    def fake(model, system, user, max_tokens=1024, mock_fn=None):
        calls.append(user)
        if user.startswith("Extract every job opportunity"):
            return mocks.mock_extractor(user), {
                "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0,
                "mode": "mock", "model": "mock", "stop_reason": extract_stop[0]}
        text, stop = queue.pop(0)
        return text, {"input_tokens": 10, "output_tokens": max_tokens,
                      "cache_read_tokens": 0, "mode": "mock", "model": "mock-m",
                      "stop_reason": stop}

    real = radar_common.claude_call
    radar_common.claude_call = fake
    try:
        cut = '{"company": "X", "role_title": "Y", "cv_ma'
        whole = json.dumps({"ok": True})
        # Both replies cut off.
        queue[:] = [(cut, "max_tokens"), (cut, "max_tokens")]
        del calls[:]
        with captured_stderr() as buf:
            parsed, usage, note = score_item._call_for_json("m", "s", "u", None, refuse_cut_off=True)
        check(parsed is None and note == score_item.CUT_OFF_NOTE and len(calls) == 2
              and "cut off at max_tokens" in buf.getvalue()
              and usage["stop_reason"] == "max_tokens" and usage["model"] == "mock-m",
              "two replies cut off at max_tokens are a failed score, logged, with the "
              "stop reason and model on the usage")
        # A cut off reply that happens to be valid JSON is still not used.
        queue[:] = [(whole + " and then it ran on", "max_tokens"),
                    (whole + " and then it ran on", "max_tokens")]
        with captured_stderr():
            parsed, _, note = score_item._call_for_json("m", "s", "u", None, refuse_cut_off=True)
        check(parsed is None and note == score_item.CUT_OFF_NOTE,
              "a cut off reply is never used, even one that would parse")
        # Cut off once, then finished, so the retry stands.
        queue[:] = [(cut, "max_tokens"), (whole, "end_turn")]
        del calls[:]
        with captured_stderr():
            parsed, usage, note = score_item._call_for_json("m", "s", "u", None, refuse_cut_off=True)
        check(parsed == {"ok": True} and note is None and len(calls) == 2
              and usage["stop_reason"] == "end_turn",
              "a retry that finishes is a good score, as it was before")
        # Finished first time, so one call.
        queue[:] = [(whole, "end_turn")]
        del calls[:]
        parsed, _, note = score_item._call_for_json("m", "s", "u", None, refuse_cut_off=True)
        check(parsed == {"ok": True} and note is None and len(calls) == 1,
              "a reply that finishes costs one call")
        # Not JSON at all keeps its old note.
        queue[:] = [("no json here", "end_turn"), ("still none", "end_turn")]
        parsed, _, note = score_item._call_for_json("m", "s", "u", None, refuse_cut_off=True)
        check(parsed is None and note == score_item.INVALID_JSON_NOTE,
              "an unfinished-looking reply that is simply not JSON keeps its old note")
        # The note is about the last attempt. Cut off once and then finished
        # with something that is not JSON is a bad reply, not a cut off one.
        queue[:] = [(cut, "max_tokens"), ("no json here", "end_turn")]
        with captured_stderr():
            parsed, _, note = score_item._call_for_json(
                "m", "s", "u", None, refuse_cut_off=True)
        check(parsed is None and note == score_item.INVALID_JSON_NOTE,
              "the note describes the last attempt, not an earlier one")
        # The log line names the advert it was about.
        queue[:] = [(cut, "max_tokens"), (cut, "max_tokens")]
        with captured_stderr() as buf:
            score_item._call_for_json("m", "s", "u", None, refuse_cut_off=True,
                                      label="Veltrix Diagnostics, Fractional Director")
        check("Veltrix Diagnostics, Fractional Director" in buf.getvalue(),
              "the log line says which advert the cut off reply was for")
        # Only a score refuses a cut off reply. Anything else that shares the
        # helper uses a reply that parses, as it always did, and logs nothing.
        queue[:] = [(whole, "max_tokens")]
        del calls[:]
        with captured_stderr() as buf:
            parsed, _, note = score_item._call_for_json("m", "s", "u", None)
        check(parsed == {"ok": True} and note is None and len(calls) == 1
              and not buf.getvalue(),
              "without the strict rule a reply that parses is used, as it was before")

        # A cut off first score stores the role unscored, with no record.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cut.sqlite"
            queue[:] = [(cut, "max_tokens")] * 8
            mail = json.loads((SAMPLES / "02_reed_wrong_rate.json").read_text("utf-8"))
            res = run_process_email(db, email_b64(mail["subject"], mail["body_text"]))
            conn = radar_common.get_db(db)
            stored = [dict(r) for r in conn.execute("SELECT * FROM opportunities")]
            check(res["json"]["new"] == 1 and len(stored) == 1
                  and stored[0]["tier"] is None and stored[0]["score_basis"] is None
                  and stored[0]["prompt_version"] is None and stored[0]["scored_at"] is None
                  and "cut off" in (stored[0]["notes"] or "")
                  and "cut off" in res["json"]["items"][0].get("note", "")
                  and not conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0],
                  "a first score cut off at max_tokens stores the advert unscored with the "
                  "reason, no record of a score and no history")
            conn.close()

        # A re-score cut off keeps the previous score exactly.
        with tempfile.TemporaryDirectory() as tmp:
            conn = radar_common.get_db(Path(tmp) / "keep.sqlite")
            oid = seed(conn, **scored_row(combined=80, want_match=80,
                                          cv_version="cv-old.txt"))
            kept = row_of(conn, oid)
            queue[:] = [(cut, "max_tokens")] * 4
            with captured_stderr() as buf:
                fixed, failed, _ = rescore.rescore_stale_cv(conn, CFG, {}, cap=5)
            check((fixed, failed) == (0, 1) and row_of(conn, oid) == kept
                  and not history(conn, oid)
                  and "previous score is kept" in buf.getvalue()
                  and "cut off" in buf.getvalue(),
                  "a re-score cut off at max_tokens leaves the row exactly as it was, "
                  "writes no history, and says why")
            legacy = seed(conn, company="Old Co", title="Old role", cv_match=70,
                          want_match=70, combined=70, one_line_why="Old.",
                          score_basis="card", prompt_version="pre-record",
                          score_model="unknown")
            frozen = row_of(conn, legacy)
            queue[:] = [(cut, "max_tokens")] * 4
            with captured_stderr():
                done = rescore.rescore_opportunities(conn, CFG, {})
            check(done == (0, 1) and row_of(conn, legacy) == frozen
                  and not history(conn, legacy),
                  "an additive-era row whose re-score is cut off keeps its old score")
            # A row that never had a score says so, it does not claim to keep one.
            bare = seed(conn, company="Bare Co", title="Never scored")
            queue[:] = [(cut, "max_tokens")] * 4
            with captured_stderr() as buf:
                rescore.rescore_opportunities(conn, CFG, {})
            said = [ln for ln in buf.getvalue().splitlines() if f"id {bare} " in ln]
            kept_said = [ln for ln in buf.getvalue().splitlines() if f"id {legacy} " in ln]
            check(bool(said) and "still has no score" in said[0]
                  and "previous score" not in said[0]
                  and bool(kept_said) and "previous score is kept" in kept_said[0],
                  "the log says a row has no score to keep, and says it kept one where it did")
            conn.close()

        # The extractor shares the helper and keeps its old behaviour. A reply
        # of its own that stopped at max_tokens but parses is still used.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "extract.sqlite"
            extract_stop[0] = "max_tokens"
            try:
                queue[:] = [(cut, "end_turn")] * 8
                mail = json.loads((SAMPLES / "02_reed_wrong_rate.json").read_text("utf-8"))
                res = run_process_email(db, email_b64(mail["subject"], mail["body_text"]))
            finally:
                extract_stop[0] = "end_turn"
            check(res["json"]["new"] == 1 and "extract_failed" not in res["json"],
                  "an extraction that stopped at max_tokens but parses is still used, "
                  "only scores refuse a cut off reply")
    finally:
        radar_common.claude_call = real


def history_semantics_checks() -> None:
    """What the history must keep, beyond the writers calling it."""
    defaults = {"pass": 70, "floor": 40}
    page = {"description": "A contract role.", "employment_type": None,
            "pay": {"currency": "GBP", "period": "day", "min": 600.0, "max": 600.0,
                    "text": "£600 per day"},
            "pay_from": "top card"}
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "semantics.sqlite")

        # The archive is part of the caller's transaction, not a commit of its own.
        oid = seed(conn, **scored_row(role_key="atomic"))
        before = row_of(conn, oid)
        wrote = radar_common.archive_score(conn, oid, "test")
        open_after = conn.in_transaction
        conn.rollback()
        check(wrote and open_after and not history(conn, oid) and row_of(conn, oid) == before,
              "the archive leaves the caller's transaction open, so rolling the caller "
              "back takes the history row with it")

        # Each of the twelve things the history keeps counts as a different
        # score. The last two arrived with Prepare application. The history
        # calls the per-ask working "working", the row calls it score_working.
        variations = {
            "scored_at": "2026-11-06T10:00:00Z", "score_basis": "spec",
            "prompt_version": "ffff0000", "score_model": "another-model",
            "cv_version": "cv-other.txt", "cv_match": 94, "tier": "question",
            "filter_reason": "something else", "one_line_why": "A different sentence.",
            "gate_location": 0, "description_hash": "0123456789ab",
            "working": '{"v": 1, "asks": []}',
        }
        on_row = {"working": "score_working"}
        check(set(variations) - {"gate_location"} | {"gates"} == set(radar_common._HISTORY_FIELDS),
              "the variations below cover every field the history keeps")
        missed = []
        for column, value in variations.items():
            # A card score holds no description version and no working, so
            # these two only count as part of a score made on a full description.
            spec_only = ({"score_basis": "spec"}
                         if column in ("description_hash", "working") else {})
            oid = seed(conn, **scored_row(role_key=f"v-{column}", **spec_only))
            radar_common.archive_score(conn, oid, "test")
            radar_common.archive_score(conn, oid, "test")
            conn.commit()
            if len(history(conn, oid)) != 1:
                missed.append(f"{column} kept twice")
            conn.execute(f"UPDATE opportunities SET {on_row.get(column, column)} = ? "
                         "WHERE id = ?", (value, oid))
            conn.commit()
            radar_common.archive_score(conn, oid, "test")
            conn.commit()
            if len(history(conn, oid)) != 2:
                missed.append(f"{column} change not kept")
        check(not missed,
              f"a score is kept again when any one of its twelve fields differs ({missed})")

        # A filtered row keeps its reason.
        weak = seed(conn, **scored_row(role_key="weak", cv_match=30, gate_cv=0, tier="filtered",
                                       failed_gates='["cv"]',
                                       filter_reason="cv match under 40"))
        radar_common.archive_score(conn, weak, "test")
        conn.commit()
        check(history(conn, weak)[0]["filter_reason"] == "cv match under 40",
              "the reason a row was Filtered is kept with the score that carried it")

        # Whatever row factory the connection has, the history holds values.
        odd = sqlite3.connect(Path(tmp) / "semantics.sqlite")
        odd.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
        keyed = seed(conn, **scored_row(role_key="keyed"))
        radar_common.archive_score(odd, keyed, "test")
        odd.commit()
        odd.close()
        check(history(conn, keyed)[0]["tier"] == "top" and history(conn, keyed)[0]["cv_match"] == 93,
              "the history keeps the values of a score and not the names of its columns, "
              "whatever row factory the connection has")

        # A rival stored this advert first, so the insert is ignored and the
        # connection's last inserted row is someone else's.
        theirs = seed(conn, **scored_row(role_key="theirs", url_hash="rival-hash"))
        mine = seed(conn, **scored_row(role_key="mine"))
        scored = {"company": "Dup Co", "role_title": "Dup role", "location": "Ghent",
                  "source_url": "https://example.invalid/dup", "thread_type": "inbound",
                  "cv_match": 80, "want_match": None, "combined": None,
                  "one_line_why": "Fits.", "red_flags": [], "suggested_action": "",
                  "act_by": "", "tier": "top", "score_basis": "card",
                  "prompt_version": "abcd1234", "score_model": "mock",
                  "scored_at": "2026-11-09T10:00:00Z"}
        pay = {"pay_currency": None, "pay_period": None, "pay_min": None,
               "pay_max": None, "day_rate": None, "rate_band": None}
        opp = {"company": "Dup Co", "title": "Dup role", "location": "Ghent",
               "salary_rate": "", "source_url": "https://example.invalid/dup"}
        got = process_email.insert_opportunity(conn, "rival-hash", "2026-11-09T10:00:00Z",
                                               "email-other", opp, scored, None, pay)
        conn.commit()
        check(got is None and not history(conn, mine) and not history(conn, theirs),
              "an insert another process won writes no history, and does not write one "
              "against whichever row this connection inserted last")

        # The page checker keeps a gate that flips even when the tier does not.
        flip = seed(conn, **scored_row(role_key="flip", cv_match=55, gate_cv=0, tier="reading",
                                       failed_gates='["cv"]', pay_min=None, pay_max=None,
                                       pay_currency=None, pay_period=None, gate_rate=None,
                                       salary_rate=""))
        out = check_jobs.apply_page(conn, row_of(conn, flip), page, FLOORS, CFG, write=True,
                                    thresholds=defaults)
        conn.commit()
        held = history(conn, flip)
        check(out["tier"]["from"] == out["tier"]["to"] == "reading"
              and row_of(conn, flip)["tier"] == "reading"
              and row_of(conn, flip)["gate_rate"] == 0
              and len(held) == 1 and json.loads(held[0]["gates"])["rate"] is None,
              "a page check that changes the rate gate but not the tier still keeps the "
              "score it replaced")
        conn.close()

    # One role through every writer in turn. Each state it held is kept once,
    # in order, whatever history it already had.
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "chain.sqlite")
        oid = seed(conn, **scored_row(combined=80, want_match=80, pay_min=None, pay_max=None,
                                      pay_currency=None, pay_period=None, gate_rate=None,
                                      salary_rate=""))
        radar_common.record_score(conn, oid, "process_email")
        conn.commit()
        # A floor of 95 filters the 93, and the state it replaced is the score
        # already in the history, so nothing is kept twice.
        regate_rates.regate(conn, CFG, FLOORS, write=True,
                            thresholds={"pass": 100, "floor": 95})
        # Back at 40 it replaces the Filtered state, which is new to the history.
        regate_rates.regate(conn, CFG, FLOORS, write=True, thresholds=defaults)
        # The page checker finds £600 a day and replaces the Top state.
        check_jobs.apply_page(conn, row_of(conn, oid), page, FLOORS, CFG, write=True,
                              thresholds=defaults)
        conn.commit()
        with captured_stderr():
            fixed, failed, _ = rescore.rescore_stale_cv(conn, CFG, {}, cap=5)
        held = history(conn, oid)
        seen = [(h["event"], h["writer"], h["tier"]) for h in held]
        check(seen[:4] == [("scored", "process_email", "top"),
                           ("replaced", "regate_rates", "filtered"),
                           ("replaced", "check_jobs", "top"),
                           ("replaced", "rescore", "question")]
              and len(held) == 5 and held[4]["event"] == "scored"
              and held[4]["writer"] == "rescore" and (fixed, failed) == (1, 0),
              "a role taken through two regates, the page checker and a re-score keeps "
              f"each state once, in order ({seen})")
        check(held[3]["cv_version"] == "cv-2026-10.txt"
              and json.loads(held[3]["gates"]) == {"sector": 1, "cv": 1, "location": 1, "rate": 0}
              and held[1]["filter_reason"] == "cv match under 95",
              "and what each one replaced carries the CV version, gates and reason it had")
        conn.close()


def config_driven_checks() -> None:
    """Wherever the thresholds are taken, they are read from the config when
    the caller has not passed them, and an error halfway through reads as none."""
    opp_cv_30 = {"company": "Acme Ltd", "title": "Test Analyst",
                 "location": "Ghent", "salary_rate": "", "source_url": ""}
    opp_cv_76 = {"company": "Defence Systems Ltd", "title": "Programme Lead",
                 "location": "Ghent", "salary_rate": "", "source_url": ""}
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {**CFG, **prefs_config(
            tmp, "day_rate_floor_gbp: 750\nsalary_floor_gbp: 100000\n"
                 "cv_pass_score: 80\ncv_floor_score: 25\n")}
        with captured_stderr():
            blocks = score_item.build_scorer_system(CFG)
        low, _, _ = score_item.score_opportunity(opp_cv_30, blocks, cfg,
                                                 mock_fn=mocks.mock_scorer)
        mid, _, _ = score_item.score_opportunity(opp_cv_76, blocks, cfg,
                                                 mock_fn=mocks.mock_scorer)
        check(low["cv_match"] == 30 and low["tier"] != "filtered"
              and low["filter_reason"] is None
              and mid["cv_match"] == 76 and mid["gate_cv"] == 0,
              "a score reads the floor and the pass mark from the config it is given, "
              "a 30 is not Filtered under a floor of 25 and a 76 fails a pass mark of 80")
        row = scored_row(cv_match=30, gate_cv=0, gate_sector=0, tier="filtered",
                         failed_gates='["sector", "cv"]',
                         filter_reason="cv match under 40")
        new, _ = regate_rates.judge_row(row, FLOORS, cfg)
        check(new["tier"] == "reading" and new["filter_reason"] is None,
              "judge_row reads the floor from the config it is given")
        conn = radar_common.get_db(Path(tmp) / "driven.sqlite")
        seed(conn, **row)
        res = regate_rates.regate(conn, cfg, FLOORS, write=False)
        check(res["thresholds"] == {"pass": 80, "floor": 25}
              and res["tier_moves"] == {"filtered to reading": 1},
              "and so does a regate")
        conn.close()
        gc.collect()

        # An error halfway through the file is no reason to keep half of it.
        class Flaky:
            def __init__(self, real):
                self.real, self.calls = real, 0

            def __getattr__(self, name):
                return getattr(self.real, name)

            def search(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("halfway")
                return self.real.search(*args, **kwargs)

        saved_re, radar_common.re = radar_common.re, Flaky(re)
        try:
            got, said = thresholds_of(prefs_config(
                tmp, "cv_pass_score: 66\ncv_floor_score: 36\n", "half.md"))
        finally:
            radar_common.re = saved_re
        check(got == {"pass": 70, "floor": 40} and said.count("WARNING.") == 1
              and "RuntimeError" in said,
              "an error halfway through the file gives 70 and 40 and not half of each")


def stale_gate_checks() -> None:
    """A changed pass mark does not reach stored gates, and a regate says so."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "stale.sqlite")
        aux, _ = regate_rates.judge_row(scored_row(cv_match=72), FLOORS, CFG)
        seed(conn, **{**scored_row(cv_match=72, role_key="seventy-two"), **aux})
        blank, _ = regate_rates.judge_row(scored_row(cv_match=None, gate_cv=None),
                                          FLOORS, CFG)
        seed(conn, **{**scored_row(cv_match=None, gate_cv=None, role_key="no-number"),
                      **blank})
        steady = regate_rates.regate(conn, CFG, FLOORS, write=False)
        raised = regate_rates.regate(conn, CFG, FLOORS, write=False,
                                     thresholds={"pass": 75, "floor": 40})
        check(steady["cv_gate_stale"] == 0 and raised["cv_gate_stale"] == 1
              and raised["tier_moves"] == {},
              "after the pass mark is raised past a stored 72 a regate counts the gate "
              "it has not reached and moves no tier")
        current = radar_common.prompt_version()
        check(steady.get("prompt_version") == current,
              "a regate says which prompt version is current")
        conn.commit()
        shown = []
        for flags in (["--stale-cv"], []):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), captured_stderr():
                rescore.main(["--mock", "--db", str(Path(tmp) / "stale.sqlite"), *flags])
            shown.append(json.loads(out.getvalue().strip().splitlines()[-1]).get(
                "prompt_version"))
        check(shown == [current, current],
              "and so does a re-score, whichever way it is run")
        conn.close()
        gc.collect()


def live_call_checks() -> None:
    """The branch of claude_call that talks to the SDK, with the SDK faked.

    Nothing leaves the machine. A stand in for the anthropic module answers,
    so no key is used and no call is made.
    """
    import types
    seen: dict = {}

    class FakeMessages:
        def __init__(self, reply):
            self.reply = reply

        def create(self, **kwargs):
            seen.clear()
            seen.update(kwargs)
            return self.reply

    def fake_module(reply):
        class FakeClient:
            def __init__(self, api_key=None):
                self.messages = FakeMessages(reply)
        return types.SimpleNamespace(Anthropic=FakeClient)

    body = [types.SimpleNamespace(type="text", text="{}")]
    usage = types.SimpleNamespace(input_tokens=5, output_tokens=1024,
                                  cache_read_input_tokens=2)
    saved = (sys.modules.get("anthropic"), os.environ.get("RADAR_MOCK"),
             os.environ.get("ANTHROPIC_API_KEY"))
    os.environ["RADAR_MOCK"] = "0"
    os.environ["ANTHROPIC_API_KEY"] = "not-a-real-key"
    try:
        sys.modules["anthropic"] = fake_module(types.SimpleNamespace(
            content=body, usage=usage, model="claude-test-1", stop_reason="max_tokens"))
        text, got = radar_common.claude_call("claude-asked-for", [{"type": "text",
                                                                   "text": "s"}], "u")
        check(got["stop_reason"] == "max_tokens" and got["model"] == "claude-test-1"
              and got["mode"] == "live" and got["output_tokens"] == 1024
              and radar_common.reply_was_cut_off(got) and text == "{}",
              "the live branch reports the stop reason and the model that answered")
        check(seen["temperature"] == 0 and seen["max_tokens"] == 1024
              and seen["model"] == "claude-asked-for"
              and score_item.MAX_TOKENS == 1024,
              "and still sends temperature 0, and MAX_TOKENS is the 1024 it was")
        sys.modules["anthropic"] = fake_module(types.SimpleNamespace(
            content=body, usage=usage, stop_reason=None))
        _, got = radar_common.claude_call("claude-asked-for", "s", "u")
        check(got["model"] == "claude-asked-for" and got["stop_reason"] is None
              and not radar_common.reply_was_cut_off(got),
              "a reply with no model falls back to the one asked for, and no stop "
              "reason is not a cut off")
    finally:
        for name, value in (("RADAR_MOCK", saved[1]), ("ANTHROPIC_API_KEY", saved[2])):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if saved[0] is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = saved[0]


def fallback_note_checks() -> None:
    """A missing threshold line is in the run record, and the run goes on."""
    mail = json.loads((SAMPLES / "01_linkedin_multi_role.json").read_text("utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        prefs = Path(tmp) / "prefs.md"
        prefs.write_text("day_rate_floor_gbp: 750\nsalary_floor_gbp: 100000\n",
                         encoding="utf-8")
        radar_common.load_config = lambda: {**_real_load_config(), "prefs_file": str(prefs)}
        try:
            fresh_warnings()
            lacking = run_process_email(Path(tmp) / "lacking.sqlite",
                                        email_b64(mail["subject"], mail["body_text"]))
        finally:
            radar_common.load_config = _template_config
        fresh_warnings()
        whole = run_process_email(Path(tmp) / "whole.sqlite",
                                  email_b64(mail["subject"], mail["body_text"]))
        notes = {}
        for name in ("lacking", "whole"):
            conn = radar_common.get_db(Path(tmp) / f"{name}.sqlite")
            notes[name] = conn.execute(
                "SELECT note FROM runs WHERE workflow = 'inbox'").fetchone()[0]
            conn.close()
        check(lacking["code"] == 0 and lacking["json"]["new"] == 3,
              "an inbox run with both threshold lines missing still stores every role")
        check("CV threshold line missing or unreadable" in notes["lacking"]
              and "CV threshold" not in notes["whole"],
              "and its run record says so, where the warning on stderr is never kept")


def migration_checks() -> None:
    if sqlite3.sqlite_version_info < (3, 35, 0):
        note("this SQLite is older than 3.35, so it cannot drop a column to "
             "build a database in the old shape, and the migration was not checked")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "existing.sqlite"
        conn = radar_common.get_db(base)
        ids = {
            "scored": seed(conn, **scored_row(score_basis=None, prompt_version=None,
                                              score_model=None, scored_at=None)),
            "additive": seed(conn, company="Old", title="Additive", cv_match=60,
                             want_match=70, combined=65, one_line_why="Old era."),
            "unscored": seed(conn, company="Nebulon", title="Unscorable"),
            "only_cv": seed(conn, company="A1", title="Only a CV number", cv_match=60),
            "only_want": seed(conn, company="A2", title="Only a want number", want_match=60),
            "only_combined": seed(conn, company="A3", title="Only a combined", combined=60),
            "only_tier": seed(conn, company="A4", title="Only a tier", tier="reading"),
            "only_why": seed(conn, company="A5", title="Only a sentence",
                             one_line_why="Written."),
            "closed": seed(conn, **scored_row(title="Closed", role_key="c",
                                              score_basis=None, prompt_version=None,
                                              score_model=None, scored_at=None,
                                              closed_at="2026-11-01T00:00:00Z")),
        }
        conn.close()
        old_shaped(base)
        raw = sqlite3.connect(base)
        raw.row_factory = sqlite3.Row
        before = {k: dict(raw.execute("SELECT * FROM opportunities WHERE id = ?",
                                      (v,)).fetchone()) for k, v in ids.items()}
        columns = {r["name"] for r in raw.execute("PRAGMA table_info(opportunities)")}
        has_table = raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'score_history'"
                                ).fetchone()
        raw.close()
        check("score_basis" not in columns and "scored_at" not in columns
              and has_table is None,
              "the fixture really is shaped like a database from before this change")

        copy = Path(tmp) / "copy.sqlite"
        shutil.copyfile(base, copy)
        conn = radar_common.get_db(copy)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(opportunities)")}
        check({"score_basis", "prompt_version", "score_model", "scored_at"} <= columns,
              "the first open adds the four columns to an existing database")
        check(conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0] == 0,
              "and creates an empty score_history")
        labels = {k: (row_of(conn, v)["score_basis"], row_of(conn, v)["prompt_version"],
                      row_of(conn, v)["score_model"], row_of(conn, v)["scored_at"])
                  for k, v in ids.items()}
        card = ("card", "pre-record", "unknown", None)
        scored_names = [k for k in ids if k != "unscored"]
        check(all(labels[k] == card for k in scored_names),
              "every row that was scored is labelled card, pre-record, unknown, no date, "
              "however little of the score it holds (" + ", ".join(scored_names) + ")")
        check(labels["unscored"] == (None, None, None, None),
              "a row that was never scored is left with nothing to label")
        same = all({k: v for k, v in row_of(conn, ids[name]).items()
                    if k in before[name]} == before[name] for name in ids)
        check(same, "nothing that was already in the database changed")

        snapshot = [dict(r) for r in conn.execute("SELECT * FROM opportunities ORDER BY id")]
        conn.close()
        conn = radar_common.get_db(copy)
        again = [dict(r) for r in conn.execute("SELECT * FROM opportunities ORDER BY id")]
        check(again == snapshot, "opening it again changes nothing")

        recorded = seed(conn, **scored_row(title="Scored after", role_key="after"))
        late = seed(conn, company="Late", title="Scored by an old process", cv_match=50,
                    one_line_why="Written without the record.")
        conn.close()
        conn = radar_common.get_db(copy)
        check(row_of(conn, recorded)["prompt_version"] == "abcd1234"
              and row_of(conn, recorded)["score_model"] == "claude-sonnet-4-6",
              "a score already recorded is never relabelled")
        check(row_of(conn, late)["prompt_version"] == "pre-record",
              "a score an old copy of the code writes during the deploy is labelled "
              "the next time the database is opened")
        check(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' "
                           "AND name = 'idx_score_history_opportunity'").fetchone()
              is not None,
              "the history is indexed by the role it belongs to")
        conn.close()

        # An inbox run holds the write lock for the whole of an email. A database
        # that is already migrated must open without asking for it.
        holder = sqlite3.connect(copy, timeout=1)
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            radar_common.get_db(copy).close()
            waited = time.monotonic() - started
        except sqlite3.OperationalError as err:
            waited = f"failed after {time.monotonic() - started:.1f}s ({err})"
        finally:
            holder.rollback()
            holder.close()
        check(isinstance(waited, float) and waited < 2.0,
              "opening a database that is already migrated takes no write lock, so it "
              f"does not wait for an inbox run that holds it ({waited})")

        # Only a rival having added the column is forgiven.
        class Failing:
            def __init__(self, real):
                self.real = real

            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE opportunities ADD COLUMN score_basis"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        broken = Path(tmp) / "broken.sqlite"
        shutil.copyfile(base, broken)
        mine = sqlite3.connect(broken)
        mine.row_factory = sqlite3.Row
        try:
            radar_common._migrate(Failing(mine))
            raised = False
        except sqlite3.OperationalError as err:
            raised = "disk I/O" in str(err)
        finally:
            mine.close()
        check(raised, "an ALTER that fails for any reason but a rival having added the "
                      "column is still an error")

        # The race, forced. Another process adds the column in the moment
        # between this one looking at the table and altering it.
        class Rival:
            def __init__(self, real, other):
                self.real, self.other, self.fired = real, other, False

            def execute(self, sql, *args):
                if (not self.fired
                        and sql.startswith("ALTER TABLE opportunities ADD COLUMN score_basis")):
                    self.fired = True
                    self.other.execute(sql)
                    self.other.commit()
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        raced = Path(tmp) / "forced.sqlite"
        shutil.copyfile(base, raced)
        mine = sqlite3.connect(raced)
        mine.row_factory = sqlite3.Row
        theirs = sqlite3.connect(raced)
        try:
            rival = Rival(mine, theirs)
            radar_common._migrate(rival)
            mine.commit()
            columns = {r["name"] for r in mine.execute("PRAGMA table_info(opportunities)")}
            check(rival.fired and {"score_basis", "scored_at"} <= columns,
                  "a column a rival process added a moment earlier is not an error, "
                  "the open carries on and finishes the migration")
        except sqlite3.OperationalError as err:
            check(False, f"a column a rival process added first stopped the open ({err})")
        finally:
            mine.close()
            theirs.close()

        # Several processes first-opening the same upgraded database at once.
        errors: list[str] = []
        for round_ in range(12):
            race = Path(tmp) / f"race{round_}.sqlite"
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
        check(not errors,
              "six processes opening an old database at the same moment all succeed, "
              f"twelve times over ({errors[:2]})")
        conn = radar_common.get_db(Path(tmp) / "race0.sqlite")
        check(conn.execute("SELECT COUNT(*) FROM opportunities WHERE score_basis = 'card'"
                           ).fetchone()[0] == len(scored_names),
              "and the racing opens label each scored row once")
        conn.close()


def page_checks() -> None:
    config = CFG
    base = {"id": 7, "company": "Veltrix Diagnostics", "title": "Fractional Director",
            "location": "Ghent", "tier": "top", "cv_match": 93, "failed_gates": "[]",
            "one_line_why": "Fits.", "first_seen": "2026-11-01T09:00:00Z",
            "last_seen": "2026-11-09T09:00:00Z", "rate_band": "above",
            "source_url": "https://example.invalid/a"}

    def render(row, show=True):
        return build_dashboard.render_opportunity(
            {**base, **row}, {"serve": True}, 1, show_basis=show)

    html = render({"score_basis": "card", "scored_at": "2026-11-08T10:15:00Z"})
    check("Scored on the advert card, 8 Nov." in html,
          "the expanded Jobs row says Scored on the advert card, 8 Nov")
    html = render({"score_basis": "spec", "scored_at": "2026-11-08T10:15:00Z"})
    check("Scored on the full job spec, 8 Nov." in html,
          "a score made on the full spec says so")
    html = render({"score_basis": "card", "scored_at": None})
    check("Scored on the advert card, date not recorded." in html,
          "a score made before the record was kept says its date was not recorded")
    html = render({"score_basis": None, "scored_at": None, "tier": None})
    check("Scored on" not in html, "a role that was never scored has no such line")
    html = render({"score_basis": "card", "scored_at": "2026-11-08T10:15:00Z"}, show=False)
    check("Scored on" not in html,
          "without the Jobs argument the same row says nothing, so no other page changes")
    html = render({"score_basis": "<b>x</b>", "scored_at": "2026-11-08T10:15:00Z"})
    check("<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html,
          "the basis is escaped before it reaches the page")
    check(html.count('class="row-sub">Scored on') == 1,
          "the line is one line, once")
    html = render({"score_basis": "card", "scored_at": "2026-11-08T10:15:00Z",
                   "engagement_note": "fixed term", "engagement_type": "contract",
                   "red_flags": '["Rate not stated"]'})
    check(html.index("Red flags") < html.index("Scored on"),
          "the line follows the labelled facts instead of sitting among them")

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "page.sqlite")
        seed(conn, **scored_row(title="Page test role", role_key="pt",
                                scored_at="2026-11-08T10:15:00Z"))
        conn.close()
        read = sqlite3.connect(Path(tmp) / "page.sqlite")
        read.row_factory = sqlite3.Row
        data = build_dashboard.collect(read, config)
        # One data dict through the Jobs page and then the archive, the way a
        # caller that reuses it would, so a switch left behind would show.
        jobs_page = build_dashboard.render_jobs_page(data, config, "t")
        archive = build_dashboard.render_page(data, config, "t",
                                              Path(tmp) / "archive.html", serve=True)
        read.close()
        check("Scored on the advert card, 8 Nov." in jobs_page,
              "the Jobs page carries the line, read from the stored columns")
        check("Scored on" not in archive,
              "the archive renders the same role after the Jobs page and carries no "
              "such line")


def regate_proof() -> None:
    """The brief's proof. Every sample email, then a regate dry run."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "proof.sqlite"
        for path in sorted(SAMPLES.glob("*.json")):
            mail = json.loads(path.read_text(encoding="utf-8"))
            run_process_email(db, email_b64(mail["subject"], mail["body_text"]))
        run_process_email(db, email_b64("One new role for you", UNSCORABLE))
        conn = radar_common.get_db(db)
        with captured_stderr():
            rescore.rescore_opportunities(conn, CFG, {})
        before = [dict(r) for r in conn.execute("SELECT * FROM opportunities ORDER BY id")]
        res = regate_rates.regate(conn, CFG, radar_common.read_rate_floors(CFG),
                                  write=False)
        after = [dict(r) for r in conn.execute("SELECT * FROM opportunities ORDER BY id")]
        tiers = {}
        for r in before:
            tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
        check(res["tier_moves_total"] == 0 and res["rows_changed"] == 0
              and res["thresholds"] == {"pass": 70, "floor": 40} and before == after,
              f"a regate dry run at 70 and 40 moves {res['tier_moves_total']} tiers "
              f"over {res['open_rows']} open rows of test data ({tiers})")
        written = regate_rates.regate(conn, CFG, radar_common.read_rate_floors(CFG),
                                      write=True)
        check(written["rows_changed"] == 0 and written["history_rows_written"] == 0,
              "and writing that regate would change nothing either")
        conn.close()


def template_checks() -> None:
    doc = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check("score_history" in doc and "cv_pass_score" in doc,
          "the architecture notes describe the history table and the two settings")
    check("radar_common.prompt_version()" in doc and "cv_gate_stale" in doc,
          "and say how to read the version of the prompt as it stands, and what "
          "cv_gate_stale counts")
    # The model reads the preferences file, so the template says no more
    # about the thresholds than the two lines and what they are.
    body = TEMPLATE.read_text(encoding="utf-8")
    section = body.split("## Scoring thresholds.")[1].split("\n## ")[0]
    lines = [ln for ln in section.splitlines() if ln.strip()]
    check("filter" not in section.lower() and len(lines) <= 4
          and "cv_pass_score: 70" in lines and "cv_floor_score: 40" in lines,
          "the template's thresholds section holds the two lines and a sentence on "
          "what they are, nothing that tells the model what to do with them")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()
    for section in (prompt_version_checks, thresholds_checks, tier_checks,
                    score_record_checks, inbox_checks, rescore_checks,
                    writers_checks, history_failure_checks, concurrency_checks, regate_checks,
                    page_checker_checks, page_loop_checks, truncation_checks,
                    history_semantics_checks, config_driven_checks, stale_gate_checks,
                    live_call_checks, fallback_note_checks,
                    migration_checks, page_checks, regate_proof,
                    template_checks):
        section()
    print()
    if failures:
        print(f"{len(failures)} RECORD-KEEPING CHECK(S) FAILED")
        return 1
    print("ALL RECORD-KEEPING CHECKS PASSED"
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
