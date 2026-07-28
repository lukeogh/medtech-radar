"""Acceptance test for the signals pipeline. No network, no n8n.

Pushes the three fabricated announcements in test/sample_announcements/ through
scripts/check_signals.py with --inject against a throwaway database, then
checks:

1. ranking is correct (perfect > marginal > irrelevant, perfect >= 75,
   irrelevant < 40)
2. the perfect signal produced a dry-run ntfy payload in test/last_signal.txt
   carrying the company, what happened, why it matters and the playbook step
3. re-running the same injections duplicates nothing (idempotent)

Mode selection. An explicit RADAR_MOCK=1 in the environment forces mock mode.
Otherwise the runner goes live when ANTHROPIC_API_KEY is in env or .env and
mock when it is not. Whatever the runner decides, it builds the child process
environment explicitly so an inherited variable can never flip a child to the
other mode. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import radar_common as rc  # noqa: E402

CHECK_SIGNALS = REPO_ROOT / "scripts" / "check_signals.py"
PAYLOAD_PATH = TEST_DIR / "last_signal.txt"

SAMPLES = {
    "perfect": TEST_DIR / "sample_announcements" / "perfect_signal.md",
    "marginal": TEST_DIR / "sample_announcements" / "marginal.md",
    "irrelevant": TEST_DIR / "sample_announcements" / "irrelevant.md",
}
SAMPLE_URLS = {
    "perfect": "https://www.quellindx.example/news/seed-round-2026",
    "marginal": "https://www.bramert-medical.example/press/tuebingen-facility-2026",
    "irrelevant": "https://www.voltaneo.example/newsroom/aurel-x-launch",
}

failures: list[str] = []

# Built once in main() from the decided mode. Children get exactly this
# environment, so a stale shell export cannot flip a run's mode.
CHILD_ENV: dict | None = None


def check(condition: bool, label: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + label)
    if not condition:
        failures.append(label)


def run_inject(sample: Path, db: Path, mock: bool) -> dict:
    cmd = [sys.executable, str(CHECK_SIGNALS), "--inject", str(sample),
           "--dry-run", "--db", str(db)]
    if mock:
        cmd.append("--mock")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", cwd=str(REPO_ROOT), env=CHILD_ENV)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"check_signals.py failed on {sample.name} "
                         f"(exit {proc.returncode})")
    start = proc.stdout.find("{")
    return json.loads(proc.stdout[start:])


def main() -> int:
    ap = argparse.ArgumentParser(description="signals acceptance test")
    ap.add_argument("--db", metavar="PATH",
                    default=str(TEST_DIR / "test_radar.sqlite"),
                    help="throwaway test database, deleted at start")
    args = ap.parse_args()
    db = Path(args.db)

    # Clean slate. The test db and last payload never survive between runs.
    if db.exists():
        db.unlink()
    if PAYLOAD_PATH.exists():
        PAYLOAD_PATH.unlink()

    rc.load_env()
    forced = rc.mock_mode_active()
    mock = forced or not os.environ.get("ANTHROPIC_API_KEY")
    global CHILD_ENV
    CHILD_ENV = os.environ.copy()
    if mock:
        CHILD_ENV["RADAR_MOCK"] = "1"
        print("=" * 68)
        print("MOCK MODE - RADAR_MOCK set explicitly" if forced else
              "MOCK MODE - no API key found - rerun after filling .env "
              "for live validation")
        print("=" * 68)
    else:
        CHILD_ENV.pop("RADAR_MOCK", None)
        print("Live mode. ANTHROPIC_API_KEY found, scoring with the real model.")

    print("\nFirst pass. Injecting the three sample announcements.")
    results = {}
    for name in ("marginal", "irrelevant", "perfect"):
        results[name] = run_inject(SAMPLES[name], db, mock)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["source_url"]: dict(r) for r in
            conn.execute("SELECT * FROM signals")}

    print("\nRanking checks.")
    check(len(rows) == 3, f"three signal rows stored (got {len(rows)})")
    scores = {}
    for name, url in SAMPLE_URLS.items():
        row = rows.get(url)
        check(row is not None, f"{name} stored under its source URL")
        scores[name] = (row or {}).get("relevance") or 0
    print(f"        relevance: perfect={scores.get('perfect')} "
          f"marginal={scores.get('marginal')} "
          f"irrelevant={scores.get('irrelevant')}")
    check(scores["perfect"] > scores["marginal"] > scores["irrelevant"],
          "perfect > marginal > irrelevant")
    check(scores["perfect"] >= 75, "perfect signal at or above 75")
    check(scores["irrelevant"] < 40, "irrelevant below 40")

    print("\nPayload checks.")
    check(results["perfect"]["pushed"] == 1,
          "perfect signal produced exactly one dry-run push")
    check(results["marginal"]["pushed"] == 0, "marginal produced no push")
    check(results["irrelevant"]["pushed"] == 0, "irrelevant produced no push")
    check(PAYLOAD_PATH.exists(), "test/last_signal.txt written")
    payload = PAYLOAD_PATH.read_text(encoding="utf-8") if PAYLOAD_PATH.exists() else ""
    perfect_row = rows.get(SAMPLE_URLS["perfect"]) or {}
    company = perfect_row.get("company") or ""
    check("quellin" in company.lower(), "scorer named the right company")
    check(company in payload, "payload element 1, the company")
    check("seed round" in payload.lower(),
          "payload element 2, what happened")
    check("Why it matters." in payload, "payload element 3, why it matters")
    check("Do today." in payload, "payload element 4, the playbook step")
    check(SAMPLE_URLS["perfect"] in payload, "payload carries the source URL")
    check(payload.startswith("POST "),
          "payload rendered as a would-be POST, nothing sent")

    print("\nIdempotency checks. Injecting all three again.")
    rerun_dupes = 0
    for name in ("marginal", "irrelevant", "perfect"):
        rerun_dupes += run_inject(SAMPLES[name], db, mock)["duplicates"]
    count_after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    check(count_after == 3, f"still three rows after re-run (got {count_after})")
    check(rerun_dupes == 3, f"re-run reported three duplicates (got {rerun_dupes})")
    mode_rows = conn.execute(
        "SELECT DISTINCT mode FROM runs WHERE workflow='signals'").fetchall()
    expected_mode = "mock" if mock else "dry-run"
    check(all(r[0] == expected_mode for r in mode_rows),
          f"runs logged with mode {expected_mode}")
    conn.close()

    print()
    if failures:
        print(f"FAIL. {len(failures)} check(s) failed.")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS. Signals pipeline ranks, pushes and dedupes correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
