#!/usr/bin/env python
"""Build the Monday digest for MedTech Radar from SQLite.

Contract (build conventions, Workstream 1):

- Selects the top and one-question tiers since the last digest. Inbound
  opportunities first, then Signals. Signals combine opportunities with
  thread_type signal and rows from the signals table, sorted by score.
- An ageing section lists adverts unknown for over a month, no sighting and
  no live check, plus signals above the bar for a fortnight, so nothing rots
  quietly.
- A threads awaiting action section reads the touches table, the latest touch
  per company with a next action set, same query as touch.py pending.
- Ends with one line of pipeline stats from the runs table, covering the
  inbox and signals workflows.
- Every build stores the exact set of included ids in the meta table under a
  one-use token and emits the token in its stdout JSON. Builds never touch
  statuses, the token record is their only write, so they can run repeatedly.
- Prints {"subject", "text", "html", "item_count", "ageing_count",
  "thread_count", "token", "send", "to"} JSON to stdout for the n8n nodes.
  The recipient comes from digest_to in config/radar.yaml.
- send is the gate the workflow obeys. It is true when inbound plus signals
  plus ageing plus threads is above zero, or always when
  digest_send_when_empty is true in config. A quiet week sends a short
  digest that says so, because silence must mean breakage and nothing else.
- --dry-run also writes test/last_digest.html and test/last_digest.txt.
- --commit-token TOKEN marks exactly the stored set digested, stamps
  meta.last_digest_ts with the build time so late arrivals fall into the
  next window, and clears the record. A token commits once. Unknown or
  reused tokens are rejected. Only n8n passes it, after a confirmed send.
- --send-when-empty and --no-send-when-empty override the config flag,
  used by the tests.
- --quiet suppresses stdout, used with --commit-token in the workflow.
- --db PATH for test isolation.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common

TEST_DIR = radar_common.REPO_ROOT / "test"
EPOCH = "1970-01-01T00:00:00Z"
AGEING_DAYS = 30          # a role unknown this long is ageing (J16)
SIGNAL_AGEING_DAYS = 14   # watcher signals keep their fortnight


def get_meta(conn, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def prune_pending(conn) -> None:
    """Drop pending-build tokens older than two weeks. Dry runs create them
    freely, so they need a broom."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'digest_pending_%'"):
        try:
            built = json.loads(row["value"]).get("built_at", "")
        except (ValueError, TypeError):
            built = ""
        if not built or built < cutoff:
            conn.execute("DELETE FROM meta WHERE key = ?", (row["key"],))


def commit_token(conn, token: str, quiet: bool) -> int:
    """Mark exactly the stored build set digested. One use per token."""
    key = f"digest_pending_{token}"
    raw = get_meta(conn, key)
    if not raw:
        print(f"Unknown or already committed digest token {token}. "
              "Nothing changed.", file=sys.stderr)
        return 2
    pending = json.loads(raw)
    now = radar_common.now_iso()
    for oid in pending.get("opp_ids", []):
        conn.execute("UPDATE opportunities SET status = 'digested',"
                     " status_changed_at = ? WHERE id = ? AND status = 'new'",
                     (now, oid))
    for sid in pending.get("sig_ids", []):
        conn.execute("UPDATE signals SET status = 'digested'"
                     " WHERE id = ? AND status = 'new'", (sid,))
    # Stamp the build time, not the commit time. An item that arrived between
    # build and send stays inside the next digest's window. Never move the
    # stamp backwards.
    current = get_meta(conn, "last_digest_ts") or EPOCH
    built_at = pending.get("built_at", now)
    set_meta(conn, "last_digest_ts", max(current, built_at))
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    conn.commit()
    count = len(pending.get("opp_ids", [])) + len(pending.get("sig_ids", []))
    radar_common.log_run(conn, "digest", mode="commit", items_in=count,
                         note=f"token committed {count} items")
    if not quiet:
        print(json.dumps({"token": token, "committed": count}))
    return 0


def fmt_date(iso: str) -> str:
    try:
        parsed = datetime.strptime((iso or "")[:10], "%Y-%m-%d")
    except ValueError:
        return iso or ""
    return f"{parsed.day} {parsed.strftime('%B %Y')}"


def parse_flags(raw) -> list[str]:
    try:
        flags = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        flags = []
    if not isinstance(flags, list):
        flags = [flags]
    return [str(flag) for flag in flags]


def sentence(text: str) -> str:
    text = (text or "").strip()
    if text and text[-1] not in ".?":
        text += "."
    return text


def nz(value) -> str:
    """None-safe text. Seeded or partially extracted rows can carry NULL in
    any text column, and a renderer must shrug, not crash the build."""
    return "" if value is None else str(value)


def esc(value) -> str:
    return html_mod.escape(nz(value))


def plural(count: int, singular: str, plural_form: str) -> str:
    return singular if count == 1 else plural_form


def collect(conn, config: dict) -> dict:
    threshold = int(config.get("score_threshold", 70))
    # The floor feeds the rate legend. The digest is a reader, so a missing
    # floor line degrades to an honest legend note while the write path
    # fails loudly at the same gap.
    try:
        floor = radar_common.read_rate_floor(config)
    except RuntimeError:
        floor = None
    last_ts = get_meta(conn, "last_digest_ts") or EPOCH
    age_cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=AGEING_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig_age_cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=SIGNAL_AGEING_DAYS)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Phase three. Tiers, not a combined bar. digest_min_want retired with
    # the additive score it was a second bar on top of.

    # A role marked applied is in none of these. A repost of one arrives as
    # a new row with status new and would otherwise head the next digest as
    # a fresh prospect, so every selection of open rows below leaves it out.
    inbound = [dict(r) for r in conn.execute(
        "SELECT * FROM opportunities WHERE status = 'new' AND first_seen > ?"
        " AND tier IN ('top','question') AND thread_type = 'inbound'"
        " AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND " + radar_common.NOT_APPLIED_SQL +
        " ORDER BY CASE tier WHEN 'top' THEN 0 ELSE 1 END, cv_match DESC",
        (last_ts,))]
    opp_signals = [dict(r) for r in conn.execute(
        "SELECT * FROM opportunities WHERE status = 'new' AND first_seen > ?"
        " AND tier IN ('top','question') AND thread_type = 'signal'"
        " AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND " + radar_common.NOT_APPLIED_SQL +
        " ORDER BY CASE tier WHEN 'top' THEN 0 ELSE 1 END, cv_match DESC",
        (last_ts,))]
    reading_count = conn.execute(
        "SELECT COUNT(*) n FROM opportunities WHERE status = 'new'"
        " AND first_seen > ? AND tier = 'reading' AND acknowledged_at IS NULL"
        " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND " + radar_common.NOT_APPLIED_SQL,
        (last_ts,)).fetchone()["n"]
    table_signals = [dict(r) for r in conn.execute(
        "SELECT * FROM signals WHERE status = 'new' AND first_seen > ?"
        " AND relevance IS NOT NULL AND relevance >= ?"
        " AND acknowledged_at IS NULL"
        " ORDER BY relevance DESC, first_seen", (last_ts, threshold))]

    signals = sorted(
        [{**o, "kind": "opportunity", "score": o["combined"]} for o in opp_signals]
        + [{**s, "kind": "signal", "score": s["relevance"]} for s in table_signals],
        key=lambda entry: -(entry["score"] or 0))

    used_opp_ids = {o["id"] for o in inbound} | {o["id"] for o in opp_signals}
    used_sig_ids = {s["id"] for s in table_signals}

    ageing = []
    # J16. An advert ages when it is unknown, no sighting and no live
    # check in a week, and first seen over a month ago, whatever its
    # tier bar the filtered roll-up. The combined bar is history.
    for row in conn.execute(
            "SELECT * FROM opportunities WHERE acknowledged_at IS NULL"
            " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
            " AND " + radar_common.NOT_APPLIED_SQL +
            " AND COALESCE(tier, '') <> 'filtered'"
            " AND first_seen <= ?"
            " ORDER BY first_seen", (age_cutoff,)):
        if row["id"] in used_opp_ids:
            continue
        if radar_common.life_state(dict(row)) != "unknown":
            continue
        ageing.append({**dict(row), "kind": "opportunity"})
    for row in conn.execute(
            "SELECT * FROM signals WHERE status IN ('new','digested')"
            " AND acknowledged_at IS NULL"
            " AND relevance IS NOT NULL AND relevance >= ? AND first_seen <= ?"
            " ORDER BY first_seen", (threshold, sig_age_cutoff)):
        if row["id"] not in used_sig_ids:
            ageing.append({**dict(row), "kind": "signal"})

    needs_review = [
        {"label": r["title"] or "Untitled role", "company": r["company"] or "",
         "note": r["notes"] or "scoring failed"}
        for r in conn.execute(
            # Needs review means ungated now. combined is history and is
            # null on every new row, so testing it here would have put the
            # whole week in the review pile for ever.
            "SELECT title, company, notes FROM opportunities"
            " WHERE tier IS NULL AND status = 'new' AND first_seen > ?"
            " AND acknowledged_at IS NULL"
            " AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL"
            " AND " + radar_common.NOT_APPLIED_SQL +
            " ORDER BY first_seen", (last_ts,))
    ] + [
        {"label": r["headline"] or "Signal", "company": r["company"] or "",
         "note": r["why"] or "scoring failed"}
        for r in conn.execute(
            "SELECT headline, company, why FROM signals"
            " WHERE relevance IS NULL AND status = 'new' AND first_seen > ?"
            " ORDER BY first_seen", (last_ts,))
    ]

    threads = [dict(r) for r in conn.execute(
        "SELECT t.company, t.touched_at, t.channel, t.next_action,"
        " t.next_action_date"
        " FROM touches t"
        " JOIN (SELECT company, MAX(id) AS max_id FROM touches"
        "       GROUP BY company) latest ON t.id = latest.max_id"
        " WHERE t.next_action IS NOT NULL AND TRIM(t.next_action) <> ''"
        " ORDER BY (t.next_action_date IS NULL), t.next_action_date, t.company")]

    stats = dict(conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN workflow = 'inbox' THEN 1 END), 0) AS runs,"
        " COALESCE(SUM(CASE WHEN workflow = 'inbox' THEN items_in END), 0) AS seen,"
        " COALESCE(SUM(CASE WHEN workflow = 'inbox' THEN items_new END), 0) AS new,"
        " COALESCE(SUM(CASE WHEN workflow = 'signals' THEN 1 END), 0) AS signal_runs,"
        " COALESCE(SUM(CASE WHEN workflow = 'signals' THEN items_new END), 0) AS signal_new,"
        " COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens"
        " FROM runs WHERE workflow IN ('inbox', 'signals') AND ts > ?",
        (last_ts,)).fetchone())
    stats["failed_emails"] = conn.execute(
        "SELECT COUNT(*) FROM email_attempts"
        " WHERE attempts >= 3 AND last_attempt > ?", (last_ts,)).fetchone()[0]

    return {"threshold": threshold, "floor": floor, "last_ts": last_ts,
            "inbound": inbound, "signals": signals, "ageing": ageing,
            "threads": threads, "needs_review": needs_review, "stats": stats,
            "reading_count": reading_count,
            "top_count": sum(1 for x in inbound + signals if x.get("tier") == "top"),
            "question_count": sum(1 for x in inbound + signals
                                  if x.get("tier") == "question")}


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Build the Monday digest from SQLite.")
    parser.add_argument("--dry-run", action="store_true",
                        help="also write test/last_digest.html and .txt previews")
    parser.add_argument("--commit-token", metavar="TOKEN",
                        help="mark exactly the stored build set digested, once")
    parser.add_argument("--send-when-empty", dest="send_when_empty",
                        action="store_true", default=None,
                        help="override config, send even with nothing to report")
    parser.add_argument("--no-send-when-empty", dest="send_when_empty",
                        action="store_false",
                        help="override config, hold the email on an empty week")
    parser.add_argument("--quiet", action="store_true", help="no stdout JSON")
    parser.add_argument("--db", help="database path override for tests")
    args = parser.parse_args(argv)

    if args.dry_run and args.commit_token:
        print("Choose one of --dry-run or --commit-token, not both. "
              "A dry run never commits.", file=sys.stderr)
        return 2

    radar_common.load_env()
    config = radar_common.load_config()
    conn = radar_common.get_db(Path(args.db).resolve() if args.db else None)

    if args.commit_token:
        code = commit_token(conn, args.commit_token, args.quiet)
        conn.close()
        return code

    data = collect(conn, config)
    now = radar_common.now_iso()
    today_label = fmt_date(now)
    # Phase three. The email shrinks to the counts and a link through to the
    # page, because mail clients cannot collapse and expand, Gmail included,
    # and a wall of roles in an inbox is a wall nobody reads. The verdict is
    # in the subject and the detail is one tap away.
    top_n = data.get("top_count", 0)
    q_n = data.get("question_count", 0)
    read_n = data.get("reading_count", 0)
    page_url = str(config.get("dashboard_url")
                   or "https://medtech-radar.keogh.cloud") + "/jobs"
    counts = [f"{top_n} top prospect" + ("" if top_n == 1 else "s"),
              f"{q_n} one question away",
              f"{read_n} for reading"]
    text = ("Radar, week to " + today_label + ".\n\n"
            + "\n".join("  " + c for c in counts)
            + f"\n\nThe roles are on the page, sorted and gated.\n{page_url}\n"
            + ("\nNothing cleared all four gates this week.\n" if not top_n else ""))
    html = (f'<div style="font:15px/1.5 -apple-system,Segoe UI,sans-serif">'
            f'<p>Radar, week to {esc(today_label)}.</p><ul>'
            + "".join(f"<li>{esc(c)}</li>" for c in counts)
            + f'</ul><p><a href="{esc(page_url)}">Open the roles</a>, sorted and '
              f'gated.</p>'
            + ("<p>Nothing cleared all four gates this week.</p>" if not top_n else "")
            + "</div>")
    item_count = len(data["inbound"]) + len(data["signals"])

    # The subject lands the verdict before the open, because most weeks the
    # subject is the whole email as far as a phone lock screen is concerned.
    def plural_n(n, word):
        return f"{n} {word}" + ("" if n == 1 else "s")

    if top_n:
        bits = [plural_n(top_n, "top prospect")]
    else:
        bits = ["Nothing exciting"]
    if q_n:
        bits.append(f"{q_n} one question away")
    if read_n:
        bits.append(f"{read_n} for reading")
    subject = "Radar. " + ", ".join(bits)
    if item_count == 0 and not read_n:
        subject = f"Radar. A quiet week, nothing new. {today_label}."

    # Record the exact built set under a one-use token. The commit step marks
    # this set and nothing else, so an item that arrives between build and
    # send cannot be marked digested unseen.
    token = (datetime.now(timezone.utc).strftime("dg%Y%m%d%H%M%S")
             + secrets.token_hex(3))
    pending = {
        "opp_ids": sorted({o["id"] for o in data["inbound"]}
                          | {s["id"] for s in data["signals"]
                             if s["kind"] == "opportunity"}),
        "sig_ids": sorted({s["id"] for s in data["signals"]
                           if s["kind"] == "signal"}),
        "built_at": now,
    }
    prune_pending(conn)
    set_meta(conn, f"digest_pending_{token}", json.dumps(pending))
    conn.commit()

    send_when_empty = (bool(config.get("digest_send_when_empty", True))
                       if args.send_when_empty is None else args.send_when_empty)
    content = (item_count + len(data["ageing"]) + len(data["threads"])
               + len(data["needs_review"]))
    send = bool(send_when_empty or content > 0)

    if args.dry_run:
        TEST_DIR.mkdir(parents=True, exist_ok=True)
        (TEST_DIR / "last_digest.html").write_text(html, encoding="utf-8")
        (TEST_DIR / "last_digest.txt").write_text(text, encoding="utf-8")

    mode = "dry-run" if args.dry_run else "build"
    radar_common.log_run(conn, "digest", mode=mode, items_in=item_count,
                         items_new=0,
                         note=(f"{len(data['inbound'])} inbound, "
                               f"{len(data['signals'])} signals, "
                               f"{len(data['ageing'])} ageing"))
    conn.close()

    if not args.quiet:
        print(json.dumps({"subject": subject, "text": text, "html": html,
                          "item_count": item_count,
                          "ageing_count": len(data["ageing"]),
                          "thread_count": len(data["threads"]),
                          "needs_review_count": len(data["needs_review"]),
                          "reading_count": data.get("reading_count", 0),
                          "token": token, "send": send,
                          "to": config.get("digest_to", "")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
