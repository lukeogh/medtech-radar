#!/usr/bin/env python
"""Process one job alert email end to end for MedTech Radar.

Contract (build conventions, Workstream 1):

- Input, one email as JSON {subject, from, date, body_text, body_html}.
  Either --b64 <base64 encoded JSON> (the n8n path, avoids shell quoting) or
  raw JSON on stdin.
- Splits the email into opportunities with the extractor prompt (haiku),
  dedupes on url_hash against SQLite BEFORE any scoring call, scores new items
  with the scorer prompt (sonnet, cached system block), writes rows.
- Output, one JSON object on stdout:
  {"processed": N, "new": N, "duplicates": N, "items": [...]}
  When extraction fails after its one retry the object also carries
  "extract_failed": true and a "note", and the radar-inbox workflow gate
  leaves the email unlabelled so it is not falsely marked processed.
- Flags. --mock forces RADAR_MOCK (deterministic mocks from test/mocks.py).
  --db PATH points at a throwaway database for test isolation.

Idempotent. Re-running the same email adds nothing and scores nothing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common
import score_item


def detect_source(from_field: str) -> str:
    sender = (from_field or "").lower()
    if "linkedin" in sender:
        return "linkedin-alert"
    if "reed" in sender:
        return "reed-alert"
    if "indeed" in sender:
        return "indeed-alert"
    if "cv-library" in sender or "cvlibrary" in sender:
        return "cvlibrary-alert"
    return "email-other"


def insert_opportunity(conn, h: str, now: str, source: str, opp: dict,
                       scored: dict | None, note: str | None) -> None:
    if scored is None:
        scored = {
            "company": opp.get("company", ""),
            "role_title": opp.get("title", ""),
            "location": opp.get("location", ""),
            "source_url": opp.get("source_url", ""),
            "thread_type": "inbound",
            "cv_match": None, "want_match": None, "combined": None,
            "one_line_why": None, "red_flags": [],
            "suggested_action": None, "act_by": None,
        }
    conn.execute(
        "INSERT OR IGNORE INTO opportunities"
        " (url_hash, first_seen, source, company, title, location, salary_rate,"
        "  source_url, thread_type, cv_match, want_match, combined, one_line_why,"
        "  red_flags, suggested_action, act_by, status, status_changed_at, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, now, source,
         scored["company"] or opp.get("company", ""),
         scored["role_title"] or opp.get("title", ""),
         scored["location"] or opp.get("location", ""),
         opp.get("salary_rate", ""),
         scored["source_url"] or opp.get("source_url", ""),
         scored["thread_type"],
         scored["cv_match"], scored["want_match"], scored["combined"],
         scored["one_line_why"], json.dumps(scored["red_flags"]),
         scored["suggested_action"], scored["act_by"],
         "new", now, note))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Process one job alert email into scored opportunities.")
    parser.add_argument("--b64", help="base64 encoded email JSON, else stdin")
    parser.add_argument("--mock", action="store_true",
                        help="force RADAR_MOCK, deterministic offline mocks")
    parser.add_argument("--db", help="database path override for tests")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    radar_common.load_env()
    config = radar_common.load_config()

    # utf-8-sig and lstrip cope with a BOM from Windows shells and editors.
    raw = (base64.b64decode(args.b64).decode("utf-8-sig") if args.b64
           else sys.stdin.read())
    email = json.loads(raw.lstrip("\ufeff"))

    conn = radar_common.get_db(Path(args.db).resolve() if args.db else None)
    extract_fn, score_fn = score_item.get_mock_fns()

    usage_total: dict = {}
    opps, usage, extract_note = score_item.extract_opportunities(
        email, config, mock_fn=extract_fn)
    radar_common.add_usage(usage_total, usage)

    source = detect_source(email.get("from", ""))
    now = radar_common.now_iso()
    system_blocks = None  # built lazily, only when something new needs scoring
    seen_hashes: set[str] = set()
    items, new_count, dup_count = [], 0, 0

    for opp in opps:
        url = (opp.get("source_url") or "").strip()
        key = url or f"radar://{source}/{opp.get('company', '')}/{opp.get('title', '')}"
        h = radar_common.url_hash(key)
        already = h in seen_hashes or conn.execute(
            "SELECT 1 FROM opportunities WHERE url_hash = ?", (h,)).fetchone()
        if already:
            dup_count += 1
            items.append({"company": opp.get("company", ""),
                          "title": opp.get("title", ""),
                          "status": "duplicate"})
            continue
        seen_hashes.add(h)

        if system_blocks is None:
            system_blocks = score_item.build_scorer_system(config)
        scored, s_usage, s_note = score_item.score_opportunity(
            opp, system_blocks, config, mock_fn=score_fn)
        radar_common.add_usage(usage_total, s_usage)

        insert_opportunity(conn, h, now, source, opp, scored, s_note)
        new_count += 1
        item = {"company": opp.get("company", ""),
                "title": opp.get("title", ""),
                "status": "new"}
        if scored is not None:
            item.update({
                "company": scored["company"],
                "title": scored["role_title"],
                "cv_match": scored["cv_match"],
                "want_match": scored["want_match"],
                "combined": scored["combined"],
                "one_line_why": scored["one_line_why"],
            })
        if s_note:
            item["note"] = s_note
        items.append(item)

    conn.commit()
    mode = "mock" if radar_common.mock_mode_active() else "live"
    note_parts = [source] + ([extract_note] if extract_note else [])
    radar_common.log_run(conn, "inbox", mode=mode,
                         items_in=len(opps), items_new=new_count,
                         model=config.get("claude_model_score"),
                         usage=usage_total, note=", ".join(note_parts))
    conn.close()

    out = {"processed": len(opps), "new": new_count,
           "duplicates": dup_count, "items": items}
    if extract_note:
        out["extract_failed"] = True
        out["note"] = extract_note
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
