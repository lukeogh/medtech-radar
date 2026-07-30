#!/usr/bin/env python
"""Backfill structured pay onto rows that predate the rate band columns.

Rows already in the database carry their pay as prose in salary_rate and
nothing else. This script runs the fast-model pay extractor over each one,
converts and bands in code exactly like the live pipeline, and writes the
result onto the row. Stored why text is left alone, old prose in old rows
is fine.

Scope and caps, per the 29 July brief:
- only rows not yet banded (rate_band IS NULL),
- only rows not acknowledged and not retired,
- at most --cap rows per run, default 100,
- fast model only, one call per row, no scoring calls.

Idempotent. A banded row never matches the selection again, so re-runs walk
forward through whatever remains and a completed backfill does nothing.

  python scripts/backfill_pay.py --mock --db test/x.sqlite   # rehearsal
  python scripts/backfill_pay.py                             # one live pass
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
import process_email
import score_item

MAX_TOKENS = 256  # four small fields, anything longer is the model rambling


def get_mock_fn():
    """The deterministic pay parser from test/mocks.py, mock mode only."""
    if not radar_common.mock_mode_active():
        return None
    test_dir = str(radar_common.REPO_ROOT / "test")
    if test_dir not in sys.path:
        sys.path.insert(0, test_dir)
    import mocks

    def mock_pay(user_content: str) -> str:
        payload = radar_common.extract_json(user_content)
        return json.dumps(mocks._pay_fields(payload.get("salary_rate") or ""))

    return mock_pay


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Band stored rows that predate the pay columns.")
    parser.add_argument("--cap", type=int, default=100,
                        help="most rows examined in one run, default 100")
    parser.add_argument("--mock", action="store_true",
                        help="force RADAR_MOCK, deterministic offline mocks")
    parser.add_argument("--quiet", action="store_true", help="no stdout JSON")
    parser.add_argument("--db", help="database path override for tests")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    radar_common.load_env()
    config = radar_common.load_config()
    floor = radar_common.read_rate_floor(config)  # loud failure before any call

    conn = radar_common.get_db(Path(args.db).resolve() if args.db else None)
    rows = conn.execute(
        "SELECT id, title, company, salary_rate FROM opportunities"
        " WHERE rate_band IS NULL AND acknowledged_at IS NULL"
        " AND status NOT IN ('actioned','dead')"
        " ORDER BY first_seen DESC LIMIT ?", (args.cap,)).fetchall()

    prompt = (radar_common.REPO_ROOT / "prompts" / "pay-extractor.md"
              ).read_text(encoding="utf-8")
    mock_fn = get_mock_fn()
    usage_total: dict = {}
    banded = unstated = failed = 0

    for row in rows:
        # No pay text stored means nothing to extract. Band it unstated
        # without spending a token on an empty string.
        if not (row["salary_rate"] or "").strip():
            parsed = {"pay_currency": "", "pay_period": "",
                      "pay_min": None, "pay_max": None}
        else:
            user = ("Extract structured pay from this advert's stored text.\n\n"
                    + json.dumps({"title": row["title"] or "",
                                  "company": row["company"] or "",
                                  "salary_rate": row["salary_rate"]},
                                 ensure_ascii=False))
            parsed, usage, note = score_item._call_for_json(
                config["claude_model_extract"], prompt, user, mock_fn)
            radar_common.add_usage(usage_total, usage)
            if parsed is None:
                failed += 1
                continue

        pay = process_email.pay_columns(parsed, config, floor)
        conn.execute(
            "UPDATE opportunities SET pay_currency = ?, pay_period = ?,"
            " pay_min = ?, pay_max = ?, day_rate = ?, rate_band = ?"
            " WHERE id = ?",
            (pay["pay_currency"], pay["pay_period"], pay["pay_min"],
             pay["pay_max"], pay["day_rate"], pay["rate_band"], row["id"]))
        if pay["rate_band"] == "unstated":
            unstated += 1
        else:
            banded += 1

    conn.commit()
    mode = "mock" if radar_common.mock_mode_active() else "live"
    radar_common.log_run(conn, "backfill", mode=mode, items_in=len(rows),
                         items_new=banded + unstated,
                         model=config.get("claude_model_extract"),
                         usage=usage_total,
                         note=f"{failed} failed extraction" if failed
                         else "pay backfill")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM opportunities WHERE rate_band IS NULL"
        " AND acknowledged_at IS NULL AND status NOT IN ('actioned','dead')"
    ).fetchone()[0]
    conn.close()

    if not args.quiet:
        print(json.dumps({"examined": len(rows), "banded": banded,
                          "unstated": unstated, "failed": failed,
                          "remaining": remaining}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
