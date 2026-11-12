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


# A score made against the role's full description. Nothing here makes one,
# and nothing here replaces one: every pass below scores the advert card.
NOT_SPEC_SQL = ("COALESCE(score_basis, '') <> '"
                + radar_common.SCORE_BASIS_SPEC + "'")


def _now_spec(conn, opportunity_id) -> bool:
    return conn.execute("SELECT 1 FROM opportunities WHERE id = ? AND NOT "
                        + NOT_SPEC_SQL, (opportunity_id,)).fetchone() is not None


def _rescore_rows(conn, config, usage_total, rows) -> tuple[int, int]:
    if not rows:
        return 0, 0
    _, score_fn = scoring.get_mock_fns()
    system_blocks = scoring.build_scorer_system(config)
    cv_version = scoring.get_cv_version(config)
    thresholds = radar_common.read_score_thresholds(config)
    fixed = failed = 0
    for row in rows:
        # A role applied for while this pass was running keeps the score it
        # was applied with, and no tokens are spent on it.
        if radar_common.role_is_applied(conn, row["id"]):
            continue
        opp = {
            "company": row["company"] or "",
            "title": row["title"] or "",
            "location": row["location"] or "",
            "salary_rate": row["salary_rate"] or "",
            "source_url": row["source_url"] or "",
            # The stored pay columns ride along so the code gate judges
            # the figure against its floor, exactly as on first arrival.
            "pay_currency": row["pay_currency"], "pay_period": row["pay_period"],
            "pay_min": row["pay_min"], "pay_max": row["pay_max"],
            "rescore": True,
        }
        scored, usage, note = scoring.score_opportunity(
            opp, system_blocks, config, mock_fn=score_fn,
            thresholds=thresholds)
        radar_common.add_usage(usage_total, usage)
        # Phase three. A row is scored when it has a tier. Gating on
        # combined here marked every row failed and left tier null, so the
        # backlog never shrank and a live run would have looped for ever
        # spending money on the same hundred rows.
        #
        # A failed score, including a reply cut off at max_tokens, writes
        # nothing. The row keeps whatever score it had, no history row is
        # made and no record of a score is stamped on it. The log says why.
        if scored is None or scored.get("tier") is None:
            failed += 1
            held = any(row[c] is not None for c in (
                "cv_match", "want_match", "combined", "tier", "one_line_why"))
            print(f"rescore: id {row['id']} was not rescored, "
                  f"{note or 'no tier came back'}. "
                  + ("Its previous score is kept." if held
                     else "It still has no score."), file=sys.stderr)
            continue
        # Applied for while the model was answering. The write lock first,
        # then look again, so that nothing can be applied for between this
        # look and the commit below. The role keeps the score it was applied
        # with, and nothing is written or kept for it. The same look keeps a
        # score made on the full description in the meantime, by a Prepare
        # application click, from being replaced by this card score.
        radar_common.begin_write(conn)
        if (radar_common.role_is_applied(conn, row["id"])
                or _now_spec(conn, row["id"])):
            conn.rollback()
            continue
        gate_cols = ("gate_sector", "gate_sector_note", "gate_cv", "gate_cv_note",
                     "gate_location", "gate_location_note", "gate_rate",
                     "gate_rate_note", "tier", "failed_gates", "question_text",
                     "filter_reason", "rate_stated", "rate_value", "rate_basis",
                     "ir35", "location_class", "engagement_type", "engagement_note")
        # How the new score was made, stored beside it.
        record_cols = ("score_basis", "prompt_version", "score_model", "scored_at")
        # The score being replaced goes to the history first, in the same
        # transaction, then the new one is written and recorded too.
        radar_common.archive_score(conn, row["id"], "rescore")
        conn.execute(
            "UPDATE opportunities SET cv_match = ?, one_line_why = ?,"
            " red_flags = ?, suggested_action = ?, act_by = ?, thread_type = ?,"
            " notes = NULL, cv_version = ?, "
            + ", ".join(f"{c} = ?" for c in gate_cols + record_cols)
            + " WHERE id = ?",
            (scored["cv_match"], scored["one_line_why"],
             json.dumps(scored["red_flags"]), scored["suggested_action"],
             scored["act_by"], scored["thread_type"], cv_version)
            + tuple(scored.get(c) for c in gate_cols + record_cols)
            + (row["id"],))
        radar_common.record_score(conn, row["id"], "rescore")
        # Commit per row. Python's sqlite3 opens an implicit write
        # transaction at the first UPDATE, and holding it across the next
        # live model call would starve every other writer past the busy
        # timeout. One row, one commit, the lock is held for microseconds.
        conn.commit()
        fixed += 1
    return fixed, failed


def rescore_opportunities(conn, config, usage_total) -> tuple[int, int]:
    # Acknowledged rows are dismissed, re-scoring them would spend tokens
    # on something a human already waved past.
    # Phase three. An unscored row is one with no tier, not one with no
    # combined, because combined is history now and nothing new writes it.
    rows = conn.execute(
        "SELECT * FROM opportunities WHERE tier IS NULL"
        " AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL"
        " AND expired_at IS NULL AND " + radar_common.NOT_APPLIED_SQL
    ).fetchall()
    return _rescore_rows(conn, config, usage_total, rows)


def regate_backlog(conn, config, usage_total, cap: int, dry_run: bool = False):
    """Re-score the back catalogue against the four gates.

    The whole point of phase three is that the additive score guaranteed its
    own bar could never be cleared, so every row scored under it needs
    judging again. Capped per run like every other model call here, and it
    refuses to touch a row a human already acknowledged.

    dry_run scores nothing and writes nothing. It reports what would change,
    which is the review gate the spec asks for, because 211 rows of the
    stronger model is a real bill and a tiering change nobody has eyeballed
    is a bad thing to spend it on.
    """
    rows = conn.execute(
        "SELECT * FROM opportunities WHERE tier IS NULL"
        " AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND " + radar_common.NOT_APPLIED_SQL +
        " ORDER BY id LIMIT ?", (cap,)).fetchall()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE tier IS NULL"
        " AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL"
        " AND expired_at IS NULL AND " + radar_common.NOT_APPLIED_SQL
    ).fetchone()[0] - len(rows)
    if dry_run:
        return {"examined": len(rows), "rescored": 0, "failed": 0,
                "remaining": max(0, remaining + len(rows)), "dry_run": True,
                "would_examine": [dict(r) for r in rows]}
    fixed, failed = _rescore_rows(conn, config, usage_total, rows)
    return {"examined": len(rows), "rescored": fixed, "failed": failed,
            "remaining": max(0, remaining), "dry_run": False}


def rescore_stale_cv(conn, config, usage_total, cap: int) -> tuple[int, int, int]:
    """Re-score scored rows whose stamp predates the active CV.

    The stretch goal from the 29 July brief. Unacknowledged rows only,
    capped per run, so a CV change never triggers an unbounded spend. The
    caller confirms before invoking, and the cap plus the runs-table
    logging are the cost guardrails.

    A role scored against its full description is left alone, neither picked
    nor counted, because this pass scores the advert card and would quietly
    swap a full description's score for a card's.
    """
    current = scoring.get_cv_version(config)
    rows = conn.execute(
        "SELECT * FROM opportunities WHERE combined IS NOT NULL"
        " AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND " + radar_common.NOT_APPLIED_SQL + " AND " + NOT_SPEC_SQL +
        " AND COALESCE(cv_version, '') <> ?"
        " ORDER BY combined DESC LIMIT ?", (current, cap)).fetchall()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE combined IS NOT NULL"
        " AND acknowledged_at IS NULL AND COALESCE(cv_version, '') <> ?"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND " + radar_common.NOT_APPLIED_SQL + " AND " + NOT_SPEC_SQL,
        (current,)).fetchone()[0] - len(rows)
    fixed, failed = _rescore_rows(conn, config, usage_total, rows)
    return fixed, failed, max(0, remaining)


def rescore_signals(conn, config, usage_total) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT * FROM signals WHERE relevance IS NULL"
        " AND acknowledged_at IS NULL").fetchall()
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
    parser.add_argument("--regate", action="store_true",
                        help="re-score the back catalogue against the four gates")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --regate, report what would change and spend nothing")
    parser.add_argument("--stale-cv", action="store_true",
                        help="instead re-score scored rows whose stamp "
                             "predates the active CV, capped")
    def positive_cap(value: str) -> int:
        cap = int(value)
        if cap < 1:
            # SQLite reads a negative LIMIT as no limit at all, which
            # would turn the cost cap inside out.
            raise argparse.ArgumentTypeError("--cap must be at least 1")
        return cap

    parser.add_argument("--cap", type=positive_cap, default=25,
                        help="most rows per --stale-cv run, default 25")
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
    mode = "mock" if radar_common.mock_mode_active() else "live"

    if args.regate:
        res = regate_backlog(conn, config, usage_total, args.cap, args.dry_run)
        if res["dry_run"]:
            # Nothing scored, nothing written, nothing logged to runs. A dry
            # run that left a trace would be a run.
            rows = res.pop("would_examine")
            conn.close()
            if not args.quiet:
                print(json.dumps({**res, "sample": [
                    {"id": r["id"], "company": r["company"], "title": r["title"],
                     "old_combined": r["combined"], "old_cv": r["cv_match"],
                     "salary_rate": r["salary_rate"], "location": r["location"]}
                    for r in rows[:5]]}))
            return 0
        radar_common.log_run(conn, "rescore", mode=mode,
                             items_in=res["examined"], items_new=res["rescored"],
                             model=config.get("claude_model_score"),
                             usage=usage_total,
                             note=f"regate, {res['remaining']} still ungated")
        conn.close()
        if not args.quiet:
            print(json.dumps({**res, "prompt_version": radar_common.prompt_version()}))
        return 0

    if args.stale_cv:
        fixed, failed, remaining = rescore_stale_cv(conn, config, usage_total,
                                                    args.cap)
        radar_common.log_run(conn, "rescore", mode=mode,
                             items_in=fixed + failed, items_new=fixed,
                             model=config.get("claude_model_score"),
                             usage=usage_total,
                             note=f"stale-cv pass, {remaining} still stale")
        conn.close()
        if not args.quiet:
            print(json.dumps({"stale_rescored": fixed,
                              "still_failing": failed,
                              "remaining_stale": remaining,
                              "cv_version": scoring.get_cv_version(config),
                              "prompt_version": radar_common.prompt_version()}))
        return 0

    opp_fixed, opp_failed = rescore_opportunities(conn, config, usage_total)
    sig_fixed, sig_failed = rescore_signals(conn, config, usage_total)
    still = opp_failed + sig_failed

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
                          "still_failing": still,
                          "prompt_version": radar_common.prompt_version()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
