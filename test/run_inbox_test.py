#!/usr/bin/env python
"""Acceptance test for the inbox pipeline. Workstream 1.

Pushes the five fabricated sample emails through scripts/process_email.py end
to end, exactly as n8n would call it, against a throwaway database. Asserts:

- all five emails process and print valid contract JSON,
- the multi role alert is split into individual opportunities,
- the deliberate duplicate is deduped (items_new < items_in, and the repeat
  email creates no new rows),
- scores are populated on every stored row with an honest spread,
- a re-run is idempotent.

With no ANTHROPIC_API_KEY the runner forces mock mode and says so loudly.
Exits non zero on any failure.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import radar_common

SAMPLES_DIR = REPO_ROOT / "test" / "sample_emails"
DEFAULT_DB = REPO_ROOT / "test" / "test_radar.sqlite"
PROCESS = REPO_ROOT / "scripts" / "process_email.py"

failures: list[str] = []

# Built once in main() from the decided mode. Children get exactly this
# environment, so a stale shell export cannot flip a run's mode.
CHILD_ENV: dict | None = None


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def run_process(cli_args: list[str], stdin_text: str | None = None, env=None):
    return subprocess.run([sys.executable, str(PROCESS)] + cli_args,
                          input=stdin_text, capture_output=True,
                          text=True, encoding="utf-8", env=env or CHILD_ENV)


class StepFailure(Exception):
    """A child step broke. Raised after a readable FAIL, never a traceback."""


def parse_step(proc, label: str) -> dict:
    """Returncode-checked JSON parse of a step's stdout.

    A non-zero exit or unparseable stdout prints the captured stderr,
    records a FAIL and aborts the runner cleanly.
    """
    if proc.returncode != 0:
        check(False, f"{label} exited {proc.returncode}, stderr follows")
        print((proc.stderr or "").strip() or "(no stderr captured)")
        raise StepFailure(label)
    raw = (proc.stdout or "").strip()
    start = raw.find("{")
    if start >= 0:
        try:
            return json.loads(raw[start:])
        except json.JSONDecodeError:
            pass
    check(False, f"{label} printed no parseable JSON, stderr follows")
    print((proc.stderr or "").strip() or "(no stderr captured)")
    raise StepFailure(label)


def mock_env() -> dict:
    """Environment for failure-injection steps, mock whatever the suite mode.

    The injected failures are deterministic mock behaviours. The live model
    rightly refuses to reproduce them, Haiku handles a malformed email
    gracefully, so these steps always run mocked. The plumbing they test,
    give_up counting and null-row surfacing, is mode independent.
    """
    env = dict(CHILD_ENV or os.environ)
    env["RADAR_MOCK"] = "1"
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="throwaway test database, deleted at start")
    args = parser.parse_args()

    db = Path(args.db)
    if db.exists():
        db.unlink()

    radar_common.load_env()
    forced = radar_common.mock_mode_active()
    mock = forced or not os.environ.get("ANTHROPIC_API_KEY")
    global CHILD_ENV
    CHILD_ENV = os.environ.copy()
    if mock:
        CHILD_ENV["RADAR_MOCK"] = "1"
        print("=" * 74)
        print("MOCK MODE - RADAR_MOCK set explicitly" if forced else
              "MOCK MODE - no API key found - rerun after filling .env for live validation")
        print("=" * 74)
    else:
        CHILD_ENV.pop("RADAR_MOCK", None)

    samples = sorted(SAMPLES_DIR.glob("*.json"))
    check(len(samples) == 5, f"five sample emails found ({len(samples)})")

    results = []
    for path in samples:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        cli = ["--b64", b64, "--db", str(db)] + (["--mock"] if mock else [])
        proc = run_process(cli)
        ok = proc.returncode == 0
        check(ok, f"{path.name} exits 0")
        if not ok:
            sys.stderr.write(proc.stderr or "")
            results.append(None)
            continue
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            check(False, f"{path.name} stdout is valid JSON")
            results.append(None)
            continue
        print(f"{path.name} -> {json.dumps(result)}")
        check(all(k in result for k in ("processed", "new", "duplicates", "items")),
              f"{path.name} result has the contract keys")
        results.append(result)

    good = [r for r in results if r]
    total_in = sum(r["processed"] for r in good)
    total_new = sum(r["new"] for r in good)
    check(total_in >= 7,
          f"multi role alert split into individual roles ({total_in} opportunities from 5 emails)")
    check(total_new < total_in,
          f"duplicate deduped, items_new {total_new} < items_in {total_in}")
    dup_result = results[-1] if results else None
    check(bool(dup_result) and dup_result["duplicates"] >= 1 and dup_result["new"] == 0,
          "the repeat email produced no new rows")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM opportunities").fetchall()
    check(len(rows) == total_new, f"db row count {len(rows)} matches items_new {total_new}")
    scored = [r for r in rows if r["cv_match"] is not None
              and r["want_match"] is not None and r["combined"] is not None]
    check(len(scored) == len(rows), "scores populated on every stored row")
    spread = sorted(r["combined"] for r in scored)
    check(bool(spread) and spread[-1] >= 70 and spread[0] < 50,
          f"honest score spread {spread}")
    veltrix = conn.execute("SELECT COUNT(*) AS c FROM opportunities"
                           " WHERE company LIKE 'Veltrix%'").fetchone()["c"]
    check(veltrix == 1, "the duplicated role is stored exactly once")
    # The sanitiser guarantee, unit style. A deliberately offending string,
    # built from today's live Meridian line, comes out clean and natural.
    offending = ("No action needed — the rate is less than half the floor; "
                 "renegotiation to £650 a day would change that.")
    cleaned = radar_common.sanitise_free_text(offending)
    check(cleaned == ("No action needed, the rate is less than half the "
                      "floor. Renegotiation to £650 a day would change that."),
          f"sanitiser cleans the Meridian worked case (got {cleaned!r})")
    check(radar_common.sanitise_free_text(cleaned) == cleaned,
          "the sanitiser is idempotent on clean text")

    # Voice doctrine. No semicolons or em dashes in any free text the scorer
    # wrote, whichever mode produced it.
    dirty = []
    for r in rows:
        for field in ("one_line_why", "suggested_action", "red_flags", "act_by"):
            value = r[field] or ""
            if ";" in value or "—" in value:
                dirty.append(f"{r['company']}.{field}")
    check(not dirty,
          "no semicolons or em dashes in scored free text"
          + (f" (violations {dirty})" if dirty else ""))
    run_rows = conn.execute("SELECT COUNT(*) AS c FROM runs"
                            " WHERE workflow = 'inbox'").fetchone()["c"]
    check(run_rows == len(good), "every run logged to the runs table")
    expected_mode = "mock" if mock else "live"
    mode_rows = conn.execute("SELECT DISTINCT mode FROM runs"
                             " WHERE workflow = 'inbox'").fetchall()
    check(bool(mode_rows) and all(r[0] == expected_mode for r in mode_rows),
          f"runs logged with mode {expected_mode}")
    conn.close()

    # The poison-message cap. A hopeless email costs three attempts, then
    # the script says give_up and the workflow shelves it.
    poison = json.dumps({
        "subject": "RADAR-POISON weekly job alert",
        "from": "alerts@jobs.example",
        "date": "2026-07-28",
        "body_text": "nothing the extractor can use",
        "body_html": "",
    })
    poison_b64 = base64.b64encode(poison.encode()).decode("ascii")
    for attempt in (1, 2, 3):
        proc = run_process(["--b64", poison_b64, "--db", str(db), "--mock"],
                           env=mock_env())
        check(proc.returncode == 0, f"poison attempt {attempt} exits 0")
        result = json.loads(proc.stdout)
        check(result.get("extract_failed") is True,
              f"poison attempt {attempt} reports extract_failed")
        check(result.get("attempts") == attempt,
              f"poison attempt {attempt} counts {attempt}")
        expected_give_up = attempt >= 3
        check(result.get("give_up") is expected_give_up,
              f"poison attempt {attempt} give_up is {expected_give_up}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT attempts FROM email_attempts").fetchone()
    check(row is not None and row["attempts"] == 3,
          "email_attempts holds three attempts for the poison email")
    poison_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM opportunities"
        " WHERE company = ''").fetchone()["c"]
    check(poison_rows == 0, "the poison email stored no junk rows")
    conn.close()

    # A scoring failure must not bury the row. It lands with null scores and
    # a note, and rescore.py clears it.
    unscorable = json.dumps({
        "subject": "One new role for you",
        "from": "alerts@jobs.example",
        "date": "2026-07-28",
        "body_text": ("RADAR-UNSCORABLE Director of Software\n"
                      "Nebulon Health · Ghent, Belgium\n"
                      "€900 per day\n"
                      "https://example.invalid/nebulon-unscorable\n"),
        "body_html": "",
    })
    unscorable_b64 = base64.b64encode(unscorable.encode()).decode("ascii")
    proc = run_process(["--b64", unscorable_b64, "--db", str(db), "--mock"],
                       env=mock_env())
    check(proc.returncode == 0, "unscorable email still exits 0")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT combined, notes FROM opportunities"
                       " WHERE company = 'Nebulon Health'").fetchone()
    check(row is not None and row["combined"] is None,
          "the unscorable row is stored with a null score")
    check(bool(row and row["notes"]), "the failure note is stored on the row")
    conn.close()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rescore.py"),
         "--db", str(db), "--mock"],
        capture_output=True, text=True, encoding="utf-8", env=mock_env())
    rescored = parse_step(proc, "rescore step")
    check("opportunities_rescored" in rescored,
          "rescore.py prints its JSON contract on stdout")
    check(rescored["opportunities_rescored"] == 1,
          "rescore.py reports one opportunity rescored")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT combined FROM opportunities"
                       " WHERE company = 'Nebulon Health'").fetchone()
    check(row["combined"] is not None, "the rescored row now carries a score")
    conn.close()

    # The fixture-CV guard. With mock off and no CV in config/profile/, the
    # scorer must refuse before any network attempt. No key is needed for
    # this check, and a bogus base URL makes any accidental live call fail
    # locally instead of reaching the API.
    real_cv = ((REPO_ROOT / "config" / "profile" / "cv.txt").exists()
               or (REPO_ROOT / "config" / "profile" / "cv.pdf").exists())
    if real_cv:
        print("skip  fixture-CV guard, a real CV is present in config/profile/")
    else:
        guard_env = dict(CHILD_ENV or os.environ)
        guard_env.pop("RADAR_MOCK", None)
        guard_env.pop("ANTHROPIC_API_KEY", None)
        guard_env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
        opp = json.dumps({"company": "Guard Test", "title": "Director",
                          "location": "Remote", "source_url": ""})
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "score_item.py")],
            input=opp, capture_output=True, text=True, encoding="utf-8",
            env=guard_env)
        check(proc.returncode != 0,
              "live scoring without a CV refuses to run")
        check("fixture" in (proc.stderr or "").lower()
              and "cv" in (proc.stderr or "").lower(),
              "the refusal names the fixture and the fix")

    # Idempotency and the stdin path in one pass. Re-feed the first email.
    stdin_text = samples[0].read_text(encoding="utf-8")
    proc = run_process(["--db", str(db)] + (["--mock"] if mock else []),
                       stdin_text=stdin_text)
    rerun = parse_step(proc, "stdin re-run")
    check(rerun["new"] == 0 and rerun["duplicates"] == rerun["processed"],
          "re-run is idempotent, every item reported as a duplicate")

    print()
    if failures:
        print(f"{len(failures)} INBOX CHECK(S) FAILED")
        return 1
    print("ALL INBOX CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StepFailure:
        print("\nFAIL. A step broke, its stderr is above.")
        sys.exit(1)
    except Exception as err:  # noqa: BLE001  readable FAIL, never a traceback
        print(f"\nFAIL. Unexpected {type(err).__name__}. {err}")
        sys.exit(1)
