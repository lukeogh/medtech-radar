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


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def run_process(cli_args: list[str], stdin_text: str | None = None):
    return subprocess.run([sys.executable, str(PROCESS)] + cli_args,
                          input=stdin_text, capture_output=True,
                          text=True, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="throwaway test database, deleted at start")
    args = parser.parse_args()

    db = Path(args.db)
    if db.exists():
        db.unlink()

    radar_common.load_env()
    mock = not os.environ.get("ANTHROPIC_API_KEY")
    if mock:
        print("=" * 74)
        print("MOCK MODE - no API key found - rerun after filling .env for live validation")
        print("=" * 74)

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
    run_rows = conn.execute("SELECT COUNT(*) AS c FROM runs"
                            " WHERE workflow = 'inbox'").fetchone()["c"]
    check(run_rows == len(good), "every run logged to the runs table")
    conn.close()

    # Idempotency and the stdin path in one pass. Re-feed the first email.
    stdin_text = samples[0].read_text(encoding="utf-8")
    proc = run_process(["--db", str(db)] + (["--mock"] if mock else []),
                       stdin_text=stdin_text)
    ok = proc.returncode == 0
    check(ok, "stdin path exits 0")
    if ok:
        rerun = json.loads(proc.stdout)
        check(rerun["new"] == 0 and rerun["duplicates"] == rerun["processed"],
              "re-run is idempotent, every item reported as a duplicate")

    print()
    if failures:
        print(f"{len(failures)} INBOX CHECK(S) FAILED")
        return 1
    print("ALL INBOX CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
