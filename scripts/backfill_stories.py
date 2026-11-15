#!/usr/bin/env python
"""Fill the Insights news page's story fields on signals stored before them.

Two passes, in this order:

1. Events, free. Every signal gets an event_key, worked out in order of
   arrival with the same rule the watcher now uses at store time
   (radar_common.event_key_for: same company, within seven days, similar
   headline). A key a human split off on the story page ("solo-") is kept.
   Re-running gives the same keys.

2. Types and card summaries. The hiring signals the code wrote itself get
   their type in code, free: job-advert is a buying window, qa-tripwire a
   first hire. Every other signal first seen in the last --days days (90 by
   default) that has no story_type yet is read again by the signal scorer,
   prompts/signal-scorer.md, from the headline and the source words the row
   already holds. Only its type, headline and card summary are kept. The
   relevance, the why line and everything else stay exactly as they were.
   This pass costs one scoring call per row, so it is a dry run unless
   --live is given: the dry run says how many rows it would read and does
   nothing else.

Idempotent. A row with a story_type never matches pass 2 again.

  python scripts/backfill_stories.py --mock --db test/x.sqlite   # rehearsal
  python scripts/backfill_stories.py                             # events, then a dry run of pass 2
  python scripts/backfill_stories.py --live                      # events, then the paid pass
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402
import check_signals  # noqa: E402

CODE_TYPES = {"job-advert": "buying-window", "qa-tripwire": "first-hire"}


def group_events(conn) -> int:
    """Pass 1. Returns how many rows changed key."""
    changed = 0
    rows = conn.execute("SELECT id, event_key FROM signals"
                        " ORDER BY first_seen, id").fetchall()
    for row in rows:
        if row["event_key"] and str(row["event_key"]).startswith("solo-"):
            continue
        before = row["event_key"]
        conn.execute("UPDATE signals SET event_key = NULL WHERE id = ?", (row["id"],))
        after = rc.assign_event_key(conn, row["id"])
        if after != before:
            changed += 1
    conn.commit()
    return changed


def type_hiring_rows(conn) -> int:
    n = 0
    for source, kind in CODE_TYPES.items():
        n += conn.execute("UPDATE signals SET story_type = ? WHERE source_id = ?"
                          " AND story_type IS NULL", (kind, source)).rowcount
    conn.commit()
    return n


def rows_to_read(conn, days: int) -> list:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(
        "SELECT id, source_id, headline, summary, source_url, first_seen FROM signals"
        " WHERE story_type IS NULL AND relevance IS NOT NULL AND first_seen >= ?"
        " ORDER BY first_seen DESC, id DESC", (since,)).fetchall()


def read_types(conn, rows: list, config: dict, mock_fn, cap: int) -> dict:
    rubric = check_signals.RUBRIC_PATH.read_text(encoding="utf-8")
    system_blocks = [{"type": "text", "text": rubric, "cache_control": {"type": "ephemeral"}}]
    names = {s.get("id"): s.get("name", "") for s in check_signals.load_watchlist()}
    out = {"read": 0, "failed": 0, "usage": {}}
    for row in rows[:cap]:
        item = {"source_id": row["source_id"] or "", "source_name": names.get(row["source_id"], ""),
                "headline": row["headline"] or "", "url": row["source_url"] or "",
                "date": (row["first_seen"] or "")[:10], "text": row["summary"] or ""}
        parsed, usage = check_signals.score_item(item, system_blocks, config, mock_fn)
        rc.add_usage(out["usage"], usage)
        if not parsed:
            out["failed"] += 1
            continue
        conn.execute(
            "UPDATE signals SET story_type = ?, card_summary = ?,"
            " headline = COALESCE(NULLIF(?, ''), headline) WHERE id = ?",
            (rc.clean_story_type(parsed.get("type")), rc.clean_card_summary(parsed.get("card_summary")),
             rc.clean_headline(parsed.get("headline")), row["id"]))
        conn.commit()
        out["read"] += 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="database path (default: the live one)")
    ap.add_argument("--days", type=int, default=90, help="how far back pass 2 reads (default 90)")
    ap.add_argument("--cap", type=int, default=400, help="most rows pass 2 reads in one run (default 400)")
    ap.add_argument("--live", action="store_true", help="make the scoring calls in pass 2")
    ap.add_argument("--mock", action="store_true", help="pass 2 with test/mocks_signals.py, no API calls")
    args = ap.parse_args(argv)
    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    mock_fn = None
    if rc.mock_mode_active():
        sys.path.insert(0, str(REPO_ROOT / "test"))
        import mocks_signals
        mock_fn = mocks_signals.mock_signal_scorer
    config = rc.load_config()
    conn = rc.get_db(Path(args.db)) if args.db else rc.get_db()
    try:
        moved = group_events(conn)
        events = conn.execute("SELECT COUNT(DISTINCT COALESCE(event_key, 'e' || id)) FROM signals").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        print(f"Events: {total} signals make {events} stories; {moved} rows took a new key.")
        hired = type_hiring_rows(conn)
        print(f"Hiring signals typed in code: {hired}.")
        rows = rows_to_read(conn, args.days)
        todo = min(len(rows), args.cap)
        if not (args.live or mock_fn):
            print(f"Dry run: {len(rows)} signals from the last {args.days} days have no type; "
                  f"--live would read {todo} of them, one scoring call each. Nothing written.")
            return 0
        result = read_types(conn, rows, config, mock_fn, args.cap)
        rc.log_run(conn, "backfill", mode="mock" if mock_fn else "live", items_in=todo,
                   items_new=result["read"], model=config.get("claude_model_score"),
                   usage=result["usage"],
                   note=f"story types: {result['read']} read, {result['failed']} unreadable")
        print(f"Types and card summaries: {result['read']} read, {result['failed']} unreadable"
              f"{', ' + str(len(rows) - todo) + ' left for the next run' if len(rows) > todo else ''}.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
