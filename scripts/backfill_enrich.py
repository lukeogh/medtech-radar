#!/usr/bin/env python
"""Enrich the companies that were stored before enrichment existed.

Same shape as backfill_pay.py, and for the same reason. Run it repeatedly
until it reports "remaining": 0, or until only failures remain, which
deserve one look rather than another loop.

- at most --cap companies per run, default the enrich_cap_per_run value,
- never re-enriches a company that already carries enriched_at, so a
  second run costs nothing for work already done,
- enriches from the text of the item that introduced the company, the
  strongest evidence available without going looking,
- logs to the runs table like every other model call here.

Usage:  python scripts/backfill_enrich.py [--cap N] [--mock] [--db PATH]
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

import enrich_company
import radar_common


def introducing_text(conn, company_id: int) -> str:
    """The best evidence already stored about this company.

    A signal carries a headline and a summary written about the company. An
    advert carries a role title and a location, which says less. Prefer the
    signal, fall back to the advert, and send nothing invented.
    """
    r = conn.execute(
        "SELECT headline, summary, source_url FROM signals"
        " WHERE company_id = ? ORDER BY id LIMIT 1", (company_id,)).fetchone()
    if r:
        return ". ".join(x for x in (r["headline"], r["summary"],
                                     r["source_url"]) if x)
    r = conn.execute(
        "SELECT title, location, source_url FROM opportunities"
        " WHERE company_id = ? ORDER BY id LIMIT 1", (company_id,)).fetchone()
    if r:
        return ". ".join(x for x in (r["title"], r["location"],
                                     r["source_url"]) if x)
    return ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Enrich stored companies that predate enrichment.")
    parser.add_argument("--cap", type=int, default=None,
                        help="companies this run, default enrich_cap_per_run")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--db")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    radar_common.load_env()
    config = radar_common.load_config()
    conn = radar_common.get_db(Path(args.db).resolve() if args.db else None)

    budget = enrich_company.new_budget(config)
    if args.cap:
        budget["cap"] = max(1, args.cap)

    rows = conn.execute(
        "SELECT id FROM companies WHERE enriched_at IS NULL"
        " ORDER BY id LIMIT ?", (budget["cap"],)).fetchall()

    examined = enriched = failed = 0
    usage: dict = {}
    for r in rows:
        res = enrich_company.enrich_company(
            conn, r["id"], introducing_text(conn, r["id"]), config, budget)
        examined += 1
        radar_common.add_usage(usage, res.get("usage") or {})
        if res.get("enriched"):
            enriched += 1
        elif str(res.get("status") or "").startswith("failed"):
            failed += 1
    conn.commit()

    mode = "mock" if radar_common.mock_mode_active() else "live"
    radar_common.log_run(conn, "enrich", mode=mode, items_in=examined,
                         items_new=enriched,
                         model=config.get("claude_model_extract"),
                         usage=usage, note="backfill")
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM companies WHERE enriched_at IS NULL"
    ).fetchone()["n"]
    conn.close()
    print(json.dumps({"examined": examined, "enriched": enriched,
                      "failed": failed, "remaining": remaining}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
