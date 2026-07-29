#!/usr/bin/env python
"""Re-score the rows the scorer previously failed on.

An opportunity whose scoring came back unparseable is stored with a null
combined score, and an unparseable signal with a null relevance. Both land in
the digest's Needs review section and on the dashboard rather than
disappearing. This script clears that list. It re-scores every null row in
place and reports what it managed.

  python scripts/rescore.py
  python scripts/rescore.py --mock --db test/test_radar.sqlite

Rescoring never pushes to ntfy. A freshly scored signal simply appears in the
next digest like any other. Flags mirror the rest of the pipeline, --mock
forces RADAR_MOCK and the deterministic test mocks, --db isolates tests,
--quiet suppresses the stdout JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common
import score_item as scoring
import check_signals


def rescore_opportunities(conn, config, usage_total) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT * FROM opportunities WHERE combined IS NULL").fetchall()
    if not rows:
        return 0, 0
    _, score_fn = scoring.get_mock_fns()
    system_blocks = scoring.build_scorer_system(config)
    fixed = failed = 0
    for row in rows:
        opp = {
            "company": row["company"] or "",
            "title": row["title"] or "",
            "location": row["location"] or "",
            "salary_rate": row["salary_rate"] or "",
            "source_url": row["source_url"] or "",
            "rescore": True,
        }
        scored, usage, note = scoring.score_opportunity(
            opp, system_blocks, config, mock_fn=score_fn)
        radar_common.add_usage(usage_total, usage)
        if scored is None or scored.get("combined") is None:
            failed += 1
            continue
        conn.execute(
            "UPDATE opportunities SET cv_match = ?, want_match = ?,"
            " combined = ?, one_line_why = ?, red_flags = ?,"
            " suggested_action = ?, act_by = ?, thread_type = ?, notes = NULL"
            " WHERE id = ?",
            (scored["cv_match"], scored["want_match"], scored["combined"],
             scored["one_line_why"], json.dumps(scored["red_flags"]),
             scored["suggested_action"], scored["act_by"],
             scored["thread_type"], row["id"]))
        fixed += 1
    conn.commit()
    return fixed, failed


def rescore_signals(conn, config, usage_total) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT * FROM signals WHERE relevance IS NULL").fetchall()
    if not rows:
        return 0, 0
    mock_fn = None
    if radar_common.mock_mode_active():
        test_dir = str(radar_common.REPO_ROOT / "test")
        if test_dir not in sys.path:
            sys.path.insert(0, test_dir)
        import mocks_signals
        mock_fn = mocks_signals.mock_signal_scorer
    rubric = check_signals.RUBRIC_PATH.read_text(encoding="utf-8")
    system_blocks = [{"type": "text", "text": rubric,
                      "cache_control": {"type": "ephemeral"}}]
    fixed = failed = 0
    for row in rows:
        item = {
            "source_id": row["source_id"] or "rescore",
            "source_name": "rescore pass",
            "url": row["source_url"] or "",
            "headline": row["headline"] or "",
            "date": None,
            "text": row["summary"] or "",
        }
        parsed, usage = check_signals.score_item(item, system_blocks, config,
                                                 mock_fn)
        radar_common.add_usage(usage_total, usage)
        if parsed is None:
            failed += 1
            continue
        try:
            relevance = max(0, min(100, int(parsed.get("relevance", 0))))
        except (TypeError, ValueError):
            failed += 1
            continue
        conn.execute(
            "UPDATE signals SET company = ?, headline = ?, relevance = ?,"
            " why = ?, playbook_step = ? WHERE id = ?",
            (str(parsed.get("company") or row["company"] or "Unknown company"),
             str(parsed.get("headline") or row["headline"] or ""),
             relevance,
             radar_common.sanitise_free_text(
                 str(parsed.get("why") or "").strip()),
             radar_common.sanitise_free_text(
                 str(parsed.get("playbook_step") or "").strip()),
             row["id"]))
        fixed += 1
    conn.commit()
    return fixed, failed


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Re-score rows with null scores, in place.")
    parser.add_argument("--mock", action="store_true",
                        help="force RADAR_MOCK, deterministic offline mocks")
    parser.add_argument("--quiet", action="store_true", help="no stdout JSON")
    parser.add_argument("--db", help="database path override for tests")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    radar_common.load_env()
    config = radar_common.load_config()
    conn = radar_common.get_db(Path(args.db).resolve() if args.db else None)

    usage_total: dict = {}
    opp_fixed, opp_failed = rescore_opportunities(conn, config, usage_total)
    sig_fixed, sig_failed = rescore_signals(conn, config, usage_total)
    still = opp_failed + sig_failed

    mode = "mock" if radar_common.mock_mode_active() else "live"
    radar_common.log_run(conn, "rescore", mode=mode,
                         items_in=opp_fixed + sig_fixed + still,
                         items_new=opp_fixed + sig_fixed,
                         model=config.get("claude_model_score"),
                         usage=usage_total,
                         note=f"{still} still failing" if still else "clear")
    conn.close()

    if not args.quiet:
        print(json.dumps({"opportunities_rescored": opp_fixed,
                          "signals_rescored": sig_fixed,
                          "still_failing": still}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
