#!/usr/bin/env python3
"""Touch tracker CLI for MedTech Radar.

Logs outreach touches to the touches table in db/radar.sqlite so the playbook
rule "log every touch" costs one command and the Monday digest can list
threads awaiting action.

Usage examples:

  python scripts/touch.py add "Cantilex Dx" --channel comment \
      --note "congrats comment on seed post" \
      --next "connection note to CTO" --next-date 2026-07-21
  python scripts/touch.py list
  python scripts/touch.py list --company Cantilex
  python scripts/touch.py pending
  python scripts/touch.py mark "Cantilex Dx" --as actioned
  python scripts/touch.py mark --opportunity 12 --as dead

pending shows the most recent touch per company where a next action is set.
Logging a newer touch for the same company supersedes its pending entry, so
finishing an action means doing it and logging it.

mark retires a thread so the digest's ageing section stops nagging about it.
Since the 29 July brief, retirement for opportunities is acknowledgement.
mark stamps acknowledged_at on matching opportunity rows, the same mark the
dashboard's Acknowledge button makes, so the two mechanisms are one. The
--as word is kept for signals, which still flip to actioned or dead, the
only two statuses a human may set there. new and digested stay
machine-owned everywhere. Match by exact company name, case does not
matter, or by a single row with --opportunity ID or --signal ID. It
refuses when nothing matches.

--db PATH points at another database file. Tests use a throwaway one so the
live db/radar.sqlite is never polluted.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import radar_common  # noqa: E402  (import after sys.path tweak)

CHANNELS = ("comment", "connection-note", "engagement", "artefact", "other")


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _shorten(text: str | None, width: int) -> str:
    text = _clean(text)
    if not text:
        return "-"
    return text if len(text) <= width else text[: width - 3] + "..."


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def cmd_add(args, conn) -> int:
    company = _clean(args.company)
    if not company:
        print("Company name is empty.", file=sys.stderr)
        return 2
    if args.next_date:
        try:
            datetime.strptime(args.next_date, "%Y-%m-%d")
        except ValueError:
            print("--next-date must be YYYY-MM-DD, for example 2026-09-01.",
                  file=sys.stderr)
            return 2
        if not _clean(args.next):
            print("--next-date needs --next describing the action.",
                  file=sys.stderr)
            return 2
    conn.execute(
        "INSERT INTO touches (company, touched_at, channel, note,"
        " next_action, next_action_date) VALUES (?,?,?,?,?,?)",
        (company, radar_common.now_iso(), args.channel,
         _clean(args.note) or None, _clean(args.next) or None,
         args.next_date or None),
    )
    conn.commit()
    print(f"Logged {args.channel} touch for {company}.")
    if _clean(args.next):
        when = f" by {args.next_date}" if args.next_date else ""
        print(f"Next action{when}. {_clean(args.next)}")
    return 0


def cmd_list(args, conn) -> int:
    sql = ("SELECT id, company, touched_at, channel, note,"
           " next_action, next_action_date FROM touches")
    params: list = []
    if args.company:
        sql += " WHERE company LIKE ?"
        params.append(f"%{args.company}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        if args.company:
            print(f"No touches match company '{args.company}'.")
        else:
            print("No touches logged yet.")
        return 0
    table = []
    for r in rows:
        nxt = "-"
        if r["next_action"]:
            nxt = _shorten(r["next_action"], 26)
            if r["next_action_date"]:
                nxt += f" (by {r['next_action_date']})"
        table.append([
            str(r["id"]),
            (r["touched_at"] or "")[:10],
            _shorten(r["company"], 24),
            r["channel"] or "-",
            _shorten(r["note"], 36),
            nxt,
        ])
    _print_table(["id", "date", "company", "channel", "note", "next"], table)
    return 0


def cmd_pending(args, conn) -> int:
    rows = conn.execute(
        "SELECT t.company, t.touched_at, t.channel, t.next_action,"
        " t.next_action_date"
        " FROM touches t"
        " JOIN (SELECT company, MAX(id) AS max_id FROM touches"
        "       GROUP BY company) latest ON t.id = latest.max_id"
        " WHERE t.next_action IS NOT NULL AND TRIM(t.next_action) <> ''"
        " ORDER BY (t.next_action_date IS NULL), t.next_action_date, t.company"
    ).fetchall()
    if not rows:
        print("Nothing pending. Every thread is quiet.")
        return 0
    today = _today()
    table = []
    due_count = 0
    for r in rows:
        due = r["next_action_date"] or "-"
        if r["next_action_date"] and r["next_action_date"] <= today:
            due_count += 1
        table.append([
            _shorten(r["company"], 24),
            (r["touched_at"] or "")[:10],
            r["channel"] or "-",
            _shorten(r["next_action"], 40),
            due,
        ])
    _print_table(["company", "last touch", "channel", "next action", "by"],
                 table)
    if due_count == 1:
        print("\n1 action is due or overdue.")
    elif due_count > 1:
        print(f"\n{due_count} actions are due or overdue.")
    return 0


def cmd_mark(args, conn) -> int:
    chosen = [x for x in (args.company, args.opportunity, args.signal)
              if x not in (None, "")]
    if len(chosen) != 1:
        print("Give exactly one target. A company name, or --opportunity ID,"
              " or --signal ID.", file=sys.stderr)
        return 2
    status = args.as_status
    now = radar_common.now_iso()
    changed: list[str] = []

    def flip_opportunity(where: str, params: tuple) -> None:
        rows = conn.execute(
            f"SELECT id, company, title, acknowledged_at FROM opportunities"
            f" WHERE {where}", params).fetchall()
        for r in rows:
            if r["acknowledged_at"]:
                changed.append(f"opportunity {r['id']}. {r['company']}. "
                               f"{r['title']}. Already acknowledged.")
                continue
            conn.execute("UPDATE opportunities SET acknowledged_at = ?"
                         " WHERE id = ?", (now, r["id"]))
            changed.append(f"opportunity {r['id']}. {r['company']}. "
                           f"{r['title']}. Acknowledged, out of sight, "
                           "still deduped.")

    def flip_signal(where: str, params: tuple) -> None:
        rows = conn.execute(
            f"SELECT id, company, headline, status FROM signals WHERE {where}",
            params).fetchall()
        for r in rows:
            conn.execute("UPDATE signals SET status = ? WHERE id = ?",
                         (status, r["id"]))
            changed.append(f"signal {r['id']}. {r['company']}. "
                           f"{r['headline'] or ''} {r['status']} to {status}.")

    if args.opportunity:
        flip_opportunity("id = ?", (args.opportunity,))
    elif args.signal:
        flip_signal("id = ?", (args.signal,))
    else:
        company = _clean(args.company)
        flip_opportunity("LOWER(company) = LOWER(?)", (company,))
        flip_signal("LOWER(company) = LOWER(?)", (company,))

    if not changed:
        print("Nothing matched, nothing changed. Check the exact company name"
              " with list, or pass --opportunity or --signal with an id.",
              file=sys.stderr)
        return 2
    conn.commit()
    for line in changed:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="touch.py",
        description="Log and review outreach touches for MedTech Radar. "
                    "Touches live in the touches table of db/radar.sqlite.",
    )
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="database file (default db/radar.sqlite)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="log a touch")
    p_add.add_argument("company", help="company name, quoted if it has spaces")
    p_add.add_argument("--channel", required=True, choices=CHANNELS,
                       help="how the touch happened")
    p_add.add_argument("--note", default=None,
                       help="what was said or done, briefly")
    p_add.add_argument("--next", default=None, metavar="ACTION",
                       help="the next planned action for this thread")
    p_add.add_argument("--next-date", default=None, metavar="YYYY-MM-DD",
                       help="when the next action is due")

    p_list = sub.add_parser("list", help="show logged touches, newest first")
    p_list.add_argument("--company", default=None,
                        help="filter by company name substring")
    p_list.add_argument("--limit", type=int, default=30,
                        help="maximum rows to show (default 30)")

    p_pending = sub.add_parser("pending", help="show threads awaiting action")

    p_mark = sub.add_parser(
        "mark", help="retire a thread, mark its rows actioned or dead")
    p_mark.add_argument("company", nargs="?", default=None,
                        help="exact company name, case does not matter")
    p_mark.add_argument("--as", dest="as_status", required=True,
                        choices=("actioned", "dead"),
                        help="the status to set, humans own these two only")
    p_mark.add_argument("--opportunity", type=int, default=None, metavar="ID",
                        help="mark one opportunity row by id instead")
    p_mark.add_argument("--signal", type=int, default=None, metavar="ID",
                        help="mark one signal row by id instead")

    # --db is also accepted after the subcommand. SUPPRESS keeps the
    # subparser from clobbering a value given before it.
    for p in (p_add, p_list, p_pending, p_mark):
        p.add_argument("--db", default=argparse.SUPPRESS, metavar="PATH",
                       help="database file (default db/radar.sqlite)")

    args = parser.parse_args(argv)
    conn = radar_common.get_db(Path(args.db) if args.db else None)
    try:
        if args.command == "add":
            return cmd_add(args, conn)
        if args.command == "list":
            return cmd_list(args, conn)
        if args.command == "mark":
            return cmd_mark(args, conn)
        return cmd_pending(args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
