#!/usr/bin/env python
"""Acceptance test for the digest builder. Workstream 1.

Populates a throwaway database through the real inbox pipeline, seeds one old
flagged opportunity for the ageing section and one fresh row in the signals
table, then exercises scripts/build_digest.py in dry run, commit and rebuild
modes. Asserts:

- the digest JSON honours the contract, including the build token and the
  send flag,
- the Inbound section renders with the strong role, weak roles stay out,
- the Signals section reads the signals table,
- the ageing section lists the seeded old row and keeps nagging after commit,
- dry run never touches statuses, the token commit marks exactly the built
  set, a row arriving after the build stays new, a token commits once,
  unknown tokens are rejected,
- the send gate counts ageing and threads, an ageing-only week sends even
  with digest_send_when_empty off, a truly empty week holds with the flag
  off and sends a quiet-week digest with it on.

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
TOUCH = REPO_ROOT / "scripts" / "touch.py"

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
    digest = parse_step(proc, "dry-run build")
    check(all(k in digest for k in ("subject", "text", "html", "item_count",
                                    "token", "send", "ageing_count",
                                    "thread_count")),
          "digest JSON has the contract keys including token and send")
    check(digest["send"] is True, "send flag true with items to report")
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
    check("failed extraction" in text,
          "stats line reports the failed-email count")
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

    # A row arrives between build and send. The token commit must not touch it.
    conn = radar_common.get_db(db.resolve())
    conn.execute(
        "INSERT INTO opportunities (url_hash, first_seen, source, company, title,"
        " location, source_url, thread_type, cv_match, want_match, combined,"
        " one_line_why, red_flags, status, status_changed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (radar_common.url_hash("radar://seed/lateshift-fractional-director"),
         iso(now + timedelta(seconds=5)), "linkedin-alert", "Lateshift Labs",
         "Fractional Software Director, diagnostics",
         "Remote (Europe)", "https://example.invalid/lateshift", "inbound",
         86, 90, 88, "Arrived while the digest was in flight.", "[]", "new",
         iso(now + timedelta(seconds=5))))
    conn.commit()
    conn.close()

    # Commit pass, as n8n would run it after a confirmed send.
    token = digest["token"]
    proc = run_script(BUILD, ["--commit-token", token, "--quiet", "--db", str(db)])
    check(proc.returncode == 0, "build_digest --commit-token exits 0")
    check(proc.stdout.strip() == "", "--quiet prints nothing")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    status = conn.execute("SELECT status FROM opportunities"
                          " WHERE company LIKE 'Veltrix%'").fetchone()["status"]
    check(status == "digested", "commit marks the sent item digested")
    late = conn.execute("SELECT status FROM opportunities"
                        " WHERE company = 'Lateshift Labs'").fetchone()["status"]
    check(late == "new", "a row arriving after the build stays new")
    orvala = conn.execute("SELECT status FROM opportunities"
                          " WHERE company = 'Orvala Health'").fetchone()["status"]
    check(orvala == "new", "the ageing row keeps its status, it was not in the send")
    signal = conn.execute("SELECT status FROM signals"
                          " WHERE company = 'Lumivance Dx'").fetchone()["status"]
    check(signal == "digested", "the sent signal is marked digested")
    meta = conn.execute("SELECT value FROM meta"
                        " WHERE key = 'last_digest_ts'").fetchone()["value"]
    check(meta > iso(now - timedelta(days=1)), "last_digest_ts stamped by commit")
    conn.close()

    # A token commits once. Unknown tokens are refused.
    proc = run_script(BUILD, ["--commit-token", token, "--db", str(db)])
    check(proc.returncode != 0, "a reused token is rejected")
    proc = run_script(BUILD, ["--commit-token", "dg-bogus", "--db", str(db)])
    check(proc.returncode != 0, "an unknown token is rejected")

    # The late arrival lands in the next build, then the well runs dry.
    proc = run_script(BUILD, ["--db", str(db)])
    check(proc.returncode == 0, "post commit rebuild exits 0")
    digest2 = parse_step(proc, "post-commit rebuild")
    check(digest2["item_count"] == 1 and "Lateshift" in digest2["text"],
          "the between-build arrival appears in the next digest")
    check("Orvala" in digest2["text"],
          "ageing keeps nagging until the status moves")
    proc = run_script(BUILD, ["--commit-token", digest2["token"], "--quiet",
                              "--db", str(db)])
    check(proc.returncode == 0, "second token commits cleanly")
    proc = run_script(BUILD, ["--db", str(db)])
    digest3 = parse_step(proc, "third build")
    check(digest3["item_count"] == 0,
          "third digest is empty, nothing is double reported")

    # Send gate. Ageing alone forces a send even with the empty-week flag off.
    proc = run_script(BUILD, ["--no-send-when-empty", "--db", str(db)])
    gate = parse_step(proc, "ageing-only gate build")
    check(gate["item_count"] == 0 and gate["ageing_count"] > 0
          and gate["send"] is True,
          "an ageing-only week still sends with the flag off")

    # Retirement, which since the 29 July brief is acknowledgement for
    # opportunities. touch.py mark stamps acknowledged_at, the same mark
    # the dashboard button makes, and statuses stay machine-owned.
    proc = run_script(TOUCH, ["mark", "Nonexistent Corp", "--as", "dead",
                              "--db", str(db)])
    check(proc.returncode != 0, "marking an unknown company is refused")
    proc = run_script(TOUCH, ["mark", "orvala health", "--as", "actioned",
                              "--db", str(db)])
    check(proc.returncode == 0 and "Orvala" in proc.stdout,
          "mark by company acknowledges the row and says so, case insensitive")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    orvala_row = conn.execute("SELECT status, acknowledged_at FROM"
                              " opportunities WHERE company = 'Orvala Health'"
                              ).fetchone()
    check(bool(orvala_row["acknowledged_at"])
          and orvala_row["status"] in ("new", "digested"),
          "mark stamps acknowledged_at and leaves the status machine-owned")
    late_id = conn.execute("SELECT id FROM opportunities"
                           " WHERE company = 'Lateshift Labs'").fetchone()["id"]
    conn.close()
    proc = run_script(TOUCH, ["mark", "--opportunity", str(late_id),
                              "--as", "dead", "--db", str(db)])
    check(proc.returncode == 0, "mark by opportunity id works")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    late_ack = conn.execute("SELECT acknowledged_at FROM opportunities"
                            " WHERE id = ?", (late_id,)).fetchone()[0]
    check(bool(late_ack), "the id-marked row is acknowledged")
    conn.close()
    proc = run_script(BUILD, ["--db", str(db)])
    after_mark = parse_step(proc, "post-mark build")
    check("Orvala" not in after_mark["text"],
          "a marked item drops out of the ageing section")

    # Acknowledge everything, a truly empty week holds with the flag off
    # and sends a quiet-week digest with it on.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE opportunities SET acknowledged_at = ?"
                 " WHERE acknowledged_at IS NULL", (iso(now),))
    conn.execute("UPDATE signals SET status = 'dead'")
    conn.commit()
    conn.close()
    proc = run_script(BUILD, ["--no-send-when-empty", "--db", str(db)])
    empty_gate = parse_step(proc, "empty-week gate build")
    check(empty_gate["send"] is False,
          "a truly empty week holds the email with the flag off")
    proc = run_script(BUILD, ["--send-when-empty", "--db", str(db)])
    quiet_week = parse_step(proc, "quiet-week build")
    check(quiet_week["send"] is True, "the empty-week flag forces a send")
    check("quiet week" in quiet_week["subject"].lower(),
          "the quiet-week subject says so")
    check("Pipeline." in quiet_week["text"],
          "the quiet-week digest keeps the stats line")

    # Needs review. Null-scored rows surface in the digest instead of
    # vanishing, and rescore.py clears them.
    conn = radar_common.get_db(db.resolve())
    conn.execute(
        "INSERT INTO opportunities (url_hash, first_seen, source, company,"
        " title, thread_type, status, status_changed_at, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (radar_common.url_hash("radar://seed/norvik-null"),
         iso(now + timedelta(minutes=5)),
         "linkedin-alert", "Norvik Bio", "Head of Software, IVD",
         "inbound", "new", iso(now + timedelta(minutes=5)),
         "response was not valid JSON after one retry"))
    conn.execute(
        "INSERT INTO signals (url_hash, first_seen, source_id, company,"
        " headline, summary, source_url, why, status)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (radar_common.url_hash("radar://seed/aldra-null"),
         iso(now + timedelta(minutes=5)),
         "test-seed", "Aldra Photonics",
         "Aldra Photonics raises seed round for optical diagnostics",
         "Seed round for an optical diagnostics startup.",
         "https://example.invalid/aldra",
         "scorer returned unparseable output, review by hand", "new"))
    conn.commit()
    conn.close()
    proc = run_script(BUILD, ["--no-send-when-empty", "--db", str(db)])
    review = parse_step(proc, "needs-review build")
    check(review["needs_review_count"] == 2,
          "both null rows counted as needs review")
    check("Needs review." in review["text"]
          and "Norvik Bio" in review["text"]
          and "Aldra Photonics" in review["text"],
          "the Needs review section lists the null rows")
    check("rescore.py" in review["text"],
          "the Needs review footer names the clearing command")
    check(review["send"] is True,
          "needs review items alone force a send with the flag off")
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "rescore.py"),
         "--db", str(db)] + (["--mock"] if mock else []),
        capture_output=True, text=True, encoding="utf-8", env=CHILD_ENV)
    rescore_out = parse_step(proc, "rescore step")
    check("opportunities_rescored" in rescore_out
          and "signals_rescored" in rescore_out,
          "rescore.py prints its JSON contract on stdout")
    proc = run_script(BUILD, ["--db", str(db)])
    cleared = parse_step(proc, "post-rescore rebuild")
    check(cleared["needs_review_count"] == 0
          and "Needs review." not in cleared["text"],
          "rescore clears the Needs review section")

    # Mode bookkeeping.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
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

    print()
    if failures:
        print(f"{len(failures)} DIGEST CHECK(S) FAILED")
        return 1
    print("ALL DIGEST CHECKS PASSED")
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
