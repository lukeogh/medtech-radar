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

    print("\nDoctrine checks. First contact carries no pitch, in every mode.")
    import re as _re
    step = (perfect_row.get("playbook_step") or "")
    do_line = next((line for line in payload.splitlines()
                    if line.lower().startswith("do today.")), "")
    # The playbook's own peer gesture is approved wording, never a pitch,
    # so it is stripped before the scan and explicitly allowed.
    allowed_phrase = "offer to compare notes"
    step_scan = step.lower().replace(allowed_phrase, "")
    payload_scan = payload.lower().replace(allowed_phrase, "")
    # Pitch-shaped patterns only. A bare "offer" would flunk the approved
    # gesture above, which the live model is entitled to use any day.
    banned_terms = ("gap assessment", "fixed-fee", "fixed fee", "7,500",
                    "offer the", "offer a", "offer my", "my services",
                    "happy to help with")
    for term in banned_terms:
        check(term not in step_scan,
              f"playbook_step carries no '{term}'")
        check(term not in payload_scan,
              f"payload carries no '{term}'")
    for label, textv in (("playbook_step", step), ("payload Do today line", do_line)):
        check("£" not in textv, f"{label} carries no pound sign")
        check(not _re.search(r"[£€$]\s?\d", textv),
              f"{label} carries no priced amount")

    # Doctrine, extended to the drafts. These are words that will be sent
    # to a founder under Luke's name, so they are held to the same rules as
    # the playbook step and then some. Runs in mock and live alike.
    print("\nDraft doctrine checks. Words a human will send, held to the playbook.")
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import draft_outreach
    drafted = conn.execute(
        "SELECT company, draft_comment, draft_note, draft_source FROM signals"
        " WHERE draft_comment IS NOT NULL OR draft_note IS NOT NULL").fetchall()
    check(True, f"{len(drafted)} signal(s) carried drafts")
    for d in drafted:
        who = d["company"] or "unknown"
        problems = draft_outreach.doctrine_problems(
            d["draft_comment"] or "", d["draft_note"] or "")
        check(not problems, f"{who} drafts obey the doctrine ({'; '.join(problems) or 'clean'})")
        if d["draft_note"]:
            check(len(d["draft_note"]) <= 300,
                  f"{who} connection note is inside the 300 character limit "
                  f"(got {len(d['draft_note'])})")
        if d["draft_comment"]:
            low = d["draft_comment"].lower()
            check("62304" not in low and "13485" not in low,
                  f"{who} comment names no standards, they belong in the note")
            check("?" not in d["draft_comment"],
                  f"{who} comment asks nothing, the first touch has no ask")
        check(d["draft_source"] in ("article", "headline"),
              f"{who} drafts record what they were written from "
              f"(got {d['draft_source']!r})")

    # The guard itself must catch what it claims to catch.
    for bad_comment, bad_note, why in (
            ("Congratulations. Happy to help with the regulatory side.", "", "an offer"),
            ("", "My rate is £700 per day if useful.", "a price"),
            ("Congratulations. See https://example.com for my services.", "", "a link and a service"),
            ("", "x" * 340, "an over-long note")):
        check(bool(draft_outreach.doctrine_problems(bad_comment, bad_note)),
              f"the doctrine guard catches {why}")

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

    print("\nArticle enrichment checks. Diff items score from article text,"
          " stubbed, no network.")
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import check_signals as cs
    listing = {"phase": 1}
    L1 = '<html><body><a href="/news/a1">Alpha raises seed</a></body></html>'
    L2 = ('<html><body><a href="/news/a2">Beta opens diagnostics lab</a>'
          '<a href="/news/a1">Alpha raises seed</a></body></html>')
    L3 = ('<html><body><a href="/news/a3">Gamma enters accelerator</a>'
          '<a href="/news/a2">Beta opens diagnostics lab</a>'
          '<a href="/news/a1">Alpha raises seed</a></body></html>')
    ARTICLE = ('<html><body><p>Beta Diagnostics has raised money to build a '
               'photonic IVD platform and has no software team yet.</p>'
               '</body></html>')

    real_http_get = cs.rc.http_get

    def fake_http_get(url, config=None, etag=None, last_modified=None):
        if url == "https://watch.example/news":
            return 200, {}, {1: L1, 2: L2, 3: L3}[listing["phase"]]
        if url == "https://watch.example/news/a2":
            return 200, {}, ARTICLE
        return 0, {"error": "stubbed away"}, ""

    try:
        cs.rc.http_get = fake_http_get
        stub_conn = rc.get_db((db.parent / "test_enrich.sqlite").resolve())
        src = {"id": "stub-src", "name": "Stub source",
               "url": "https://watch.example/news", "method": "diff",
               "check_interval_hours": 6, "status": "live"}
        cfg = rc.load_config()
        notes: list[str] = []

        items = cs.check_diff(src, cs._get_state(stub_conn, "stub-src"),
                              cfg, stub_conn, notes)
        check(items == [], "first run baselines without scoring")

        listing["phase"] = 2
        items = cs.check_diff(src, cs._get_state(stub_conn, "stub-src"),
                              cfg, stub_conn, notes)
        check(len(items) == 1 and "no software team yet" in items[0]["text"],
              "a fresh diff item carries the fetched article text")

        listing["phase"] = 3
        items = cs.check_diff(src, cs._get_state(stub_conn, "stub-src"),
                              cfg, stub_conn, notes)
        check(len(items) == 1
              and items[0]["text"] == "Gamma enters accelerator",
              "a failed article fetch falls back to the anchor text")
        check(any("article fetch failed" in n for n in notes),
              "the fallback is recorded in the run notes")
        stub_conn.close()
        (db.parent / "test_enrich.sqlite").unlink(missing_ok=True)
    finally:
        cs.rc.http_get = real_http_get

    print("\nPush resilience checks. A failed push delays, never loses,"
          " stubbed, nothing sent.")
    os.environ["RADAR_MOCK"] = "1"
    sys.path.insert(0, str(TEST_DIR))
    import mocks_signals as ms
    real_push = cs.rc.push_ntfy

    def throwing_push(*a, **k):
        raise OSError("stubbed ntfy outage")

    push_calls = []

    def working_push(config, title, message, **k):
        push_calls.append((title, message))
        return "sent:stub"

    try:
        push_db = (db.parent / "test_push.sqlite").resolve()
        if push_db.exists():
            push_db.unlink()
        push_conn = rc.get_db(push_db)
        cfg = rc.load_config()
        rubric = cs.RUBRIC_PATH.read_text(encoding="utf-8")
        blocks = [{"type": "text", "text": rubric}]
        item = cs.parse_announcement_file(SAMPLES["perfect"])
        push_result = {"mode": "push", "sources_checked": 0, "items_in": 0,
                       "items_new": 0, "duplicates": 0, "pushed": 0,
                       "payloads": [], "notes": [], "usage": {}}

        cs.rc.push_ntfy = throwing_push
        cs.process_item(item, push_conn, cfg, blocks,
                        ms.mock_signal_scorer, True, push_result)
        row = push_conn.execute("SELECT pushed, relevance FROM signals"
                                ).fetchone()
        check(row is not None and row["pushed"] == 0,
              "a failed live push leaves pushed at zero")
        check(push_result["pushed"] == 0
              and any("push failed" in n for n in push_result["notes"]),
              "the failure lands in the run notes and the run carries on")

        cs.rc.push_ntfy = working_push
        cs.catch_up_pushes(push_conn, cfg, push_result)
        row = push_conn.execute("SELECT pushed FROM signals").fetchone()
        check(row["pushed"] == 1 and len(push_calls) == 1,
              "the catch-up sweep pushes the delayed signal once")
        cs.catch_up_pushes(push_conn, cfg, push_result)
        check(len(push_calls) == 1,
              "a pushed signal is not pushed again by the next sweep")
        push_conn.close()
        push_db.unlink(missing_ok=True)
    finally:
        cs.rc.push_ntfy = real_push
        os.environ.pop("RADAR_MOCK", None)

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
