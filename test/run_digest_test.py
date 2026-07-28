#!/usr/bin/env python
"""Acceptance test for the digest builder. Workstream 1.

Populates a throwaway database through the real inbox pipeline, seeds one old
flagged opportunity for the ageing section and one fresh row in the signals
table, then exercises scripts/build_digest.py in dry run, commit and rebuild
modes. Asserts:

- the digest JSON honours the contract {subject, text, html, item_count},
- the Inbound section renders with the strong role, weak roles stay out,
- the Signals section reads the signals table,
- the ageing section lists the seeded old row and keeps nagging after commit,
- dry run mutates nothing, commit marks items digested and stamps
  meta.last_digest_ts, and the next build is empty (idempotent).

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import radar_common

SAMPLES_DIR = REPO_ROOT / "test" / "sample_emails"
DEFAULT_DB = REPO_ROOT / "test" / "test_radar.sqlite"
PROCESS = REPO_ROOT / "scripts" / "process_email.py"
BUILD = REPO_ROOT / "scripts" / "build_digest.py"

failures: list[str] = []

# Built once in main() from the decided mode. Children get exactly this
# environment, so a stale shell export cannot flip a run's mode.
CHILD_ENV: dict | None = None


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def run_script(script: Path, cli_args: list[str]):
    return subprocess.run([sys.executable, str(script)] + cli_args,
                          capture_output=True, text=True, encoding="utf-8",
                          env=CHILD_ENV)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # Populate through the real pipeline.
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        cli = ["--b64", b64, "--db", str(db)] + (["--mock"] if mock else [])
        proc = run_script(PROCESS, cli)
        check(proc.returncode == 0, f"{path.name} processed into the test db")
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr or "")

    # Seed rows the digest logic needs to prove itself.
    now = datetime.now(timezone.utc)
    old = iso(now - timedelta(days=30))
    recent = iso(now - timedelta(days=2))
    conn = radar_common.get_db(db.resolve())
    conn.execute(
        "INSERT INTO opportunities (url_hash, first_seen, source, company, title,"
        " location, source_url, thread_type, cv_match, want_match, combined,"
        " one_line_why, red_flags, status, status_changed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (radar_common.url_hash("radar://seed/orvala-interim-director"), old,
         "linkedin-alert", "Orvala Health", "Interim Software Director",
         "Antwerp, Belgium (Hybrid)", "https://example.invalid/orvala",
         "inbound", 84, 80, 82,
         "Solid fit that has sat unanswered for a month.", "[]", "new", old))
    conn.execute(
        "INSERT INTO signals (url_hash, first_seen, source_id, company, headline,"
        " summary, source_url, relevance, why, playbook_step, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (radar_common.url_hash("radar://seed/lumivance-seed-round"), recent,
         "test-seed", "Lumivance Dx",
         "Lumivance Dx raises an 8m euro seed round for photonic IVD",
         "Seed round for a photonic diagnostics platform.",
         "https://example.invalid/lumivance", 81,
         "Photonic IVD at seed stage, squarely inside the signal definition.",
         "Same day comment and a short connection note to the CTO.", "new"))
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('last_digest_ts', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (iso(now - timedelta(days=21)),))
    conn.commit()
    conn.close()

    # Dry run build.
    proc = run_script(BUILD, ["--dry-run", "--db", str(db)])
    check(proc.returncode == 0, "build_digest --dry-run exits 0")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or "")
        print(f"\n{len(failures)} DIGEST CHECK(S) FAILED")
        return 1
    digest = json.loads(proc.stdout)
    check(all(k in digest for k in ("subject", "text", "html", "item_count")),
          "digest JSON has the contract keys")
    print(f"digest -> {json.dumps({k: digest[k] for k in ('subject', 'item_count')})}")
    text = digest["text"]
    check(digest["item_count"] >= 2,
          f"item_count {digest['item_count']} covers inbound plus signal")
    check("Inbound." in text, "Inbound section renders")
    check("Veltrix Diagnostics" in text, "the strong inbound role is in the digest")
    check("Signals." in text, "Signals section renders")
    check("Lumivance" in text, "the signals table feeds the Signals section")
    check("Ageing." in text, "Ageing section renders")
    check("Orvala" in text, "the seeded old row appears in the ageing section")
    check("Pipeline." in text, "pipeline stats line present")
    check("Caldora" not in text, "below threshold roles stay out of the digest")
    check("Meridian" not in text, "the wrong rate role stays below the bar")

    html_file = REPO_ROOT / "test" / "last_digest.html"
    txt_file = REPO_ROOT / "test" / "last_digest.txt"
    check(html_file.exists() and html_file.stat().st_size > 0,
          "test/last_digest.html written")
    check(txt_file.exists() and txt_file.stat().st_size > 0,
          "test/last_digest.txt written")
    check("Veltrix" in html_file.read_text(encoding="utf-8"),
          "html preview contains the inbound item")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    status = conn.execute("SELECT status FROM opportunities"
                          " WHERE company LIKE 'Veltrix%'").fetchone()["status"]
    check(status == "new", "dry run leaves statuses untouched")
    conn.close()

    # Commit pass, as n8n would run it after a successful send.
    proc = run_script(BUILD, ["--commit", "--quiet", "--db", str(db)])
    check(proc.returncode == 0, "build_digest --commit --quiet exits 0")
    check(proc.stdout.strip() == "", "--quiet prints nothing")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    status = conn.execute("SELECT status FROM opportunities"
                          " WHERE company LIKE 'Veltrix%'").fetchone()["status"]
    check(status == "digested", "commit marks the sent item digested")
    orvala = conn.execute("SELECT status FROM opportunities"
                          " WHERE company = 'Orvala Health'").fetchone()["status"]
    check(orvala == "new", "the ageing row keeps its status, it was not in the send")
    signal = conn.execute("SELECT status FROM signals"
                          " WHERE company = 'Lumivance Dx'").fetchone()["status"]
    check(signal == "digested", "the sent signal is marked digested")
    meta = conn.execute("SELECT value FROM meta"
                        " WHERE key = 'last_digest_ts'").fetchone()["value"]
    check(meta > iso(now - timedelta(days=1)), "last_digest_ts stamped by commit")
    expected_mode = "mock" if mock else "live"
    seed_modes = conn.execute("SELECT DISTINCT mode FROM runs"
                              " WHERE workflow = 'inbox'").fetchall()
    check(bool(seed_modes) and all(r[0] == expected_mode for r in seed_modes),
          f"seeding runs logged with mode {expected_mode}")
    digest_modes = {r[0] for r in conn.execute(
        "SELECT DISTINCT mode FROM runs WHERE workflow = 'digest'")}
    check(bool(digest_modes) and digest_modes <= {"dry-run", "build", "commit"},
          "digest runs logged with build modes only")
    conn.close()

    # A rebuild after commit must be empty, and ageing must keep nagging.
    proc = run_script(BUILD, ["--db", str(db)])
    check(proc.returncode == 0, "post commit rebuild exits 0")
    digest2 = json.loads(proc.stdout)
    check(digest2["item_count"] == 0,
          "second digest is empty, nothing is double reported")
    check("Orvala" in digest2["text"],
          "ageing keeps nagging until the status moves")

    print()
    if failures:
        print(f"{len(failures)} DIGEST CHECK(S) FAILED")
        return 1
    print("ALL DIGEST CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
