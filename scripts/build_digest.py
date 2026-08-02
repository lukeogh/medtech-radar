#!/usr/bin/env python
"""Build the Monday digest for MedTech Radar from SQLite.

Contract (build conventions, Workstream 1):

- Selects everything at or above score_threshold since the last digest.
  Inbound opportunities first, then Signals. Signals combine opportunities
  with thread_type signal and rows from the signals table, sorted by score.
- An ageing section lists flagged items older than two weeks whose status has
  not moved (status new or digested), so nothing rots quietly.
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
AGEING_DAYS = 14


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


RATE_WORDS = {"above": "Above", "close": "Close", "below": "Below",
              "unstated": "Unstated"}


def rate_word(row: dict) -> str:
    return RATE_WORDS.get((row.get("rate_band") or "unstated").lower(),
                          "Unstated")


def rate_legend(floor) -> str:
    if floor is None:
        return ("Rate bands need the day_rate_floor_gbp line in the "
                "preferences file, and it is missing.")
    f = f"£{round(floor):,}"
    return (f"Rate bands. Above meets the {f} a day floor, Close is under "
            "by up to £50, Below is under by more, ranges band on their "
            "top, the best case.")


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

    inbound = [dict(r) for r in conn.execute(
        "SELECT * FROM opportunities WHERE status = 'new' AND first_seen > ?"
        " AND combined IS NOT NULL AND combined >= ? AND thread_type = 'inbound'"
        " AND acknowledged_at IS NULL"
        " ORDER BY combined DESC, first_seen", (last_ts, threshold))]
    opp_signals = [dict(r) for r in conn.execute(
        "SELECT * FROM opportunities WHERE status = 'new' AND first_seen > ?"
        " AND combined IS NOT NULL AND combined >= ? AND thread_type = 'signal'"
        " AND acknowledged_at IS NULL"
        " ORDER BY combined DESC, first_seen", (last_ts, threshold))]
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
    for row in conn.execute(
            "SELECT * FROM opportunities WHERE status IN ('new','digested')"
            " AND acknowledged_at IS NULL"
            " AND combined IS NOT NULL AND combined >= ?"
            " AND COALESCE(status_changed_at, first_seen) <= ?"
            " ORDER BY COALESCE(status_changed_at, first_seen)",
            (threshold, age_cutoff)):
        if row["id"] not in used_opp_ids:
            ageing.append({**dict(row), "kind": "opportunity"})
    for row in conn.execute(
            "SELECT * FROM signals WHERE status IN ('new','digested')"
            " AND acknowledged_at IS NULL"
            " AND relevance IS NOT NULL AND relevance >= ? AND first_seen <= ?"
            " ORDER BY first_seen", (threshold, age_cutoff)):
        if row["id"] not in used_sig_ids:
            ageing.append({**dict(row), "kind": "signal"})

    needs_review = [
        {"label": r["title"] or "Untitled role", "company": r["company"] or "",
         "note": r["notes"] or "scoring failed"}
        for r in conn.execute(
            "SELECT title, company, notes FROM opportunities"
            " WHERE combined IS NULL AND status = 'new' AND first_seen > ?"
            " AND acknowledged_at IS NULL"
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
            "threads": threads, "needs_review": needs_review, "stats": stats}


def stats_line(stats: dict) -> str:
    dups = max(0, stats["seen"] - stats["new"])
    return (f"Pipeline. {stats['runs']} inbox {plural(stats['runs'], 'run', 'runs')}, "
            f"{stats['seen']} {plural(stats['seen'], 'opportunity', 'opportunities')} "
            f"seen, {stats['new']} new, "
            f"{dups} {plural(dups, 'duplicate', 'duplicates')} skipped, "
            f"{stats['signal_runs']} signal {plural(stats['signal_runs'], 'check', 'checks')}, "
            f"{stats['signal_new']} scored, "
            f"{stats['failed_emails']} "
            f"{plural(stats['failed_emails'], 'email', 'emails')} failed extraction, "
            f"{stats['tokens']} tokens since the last digest.")


def render_text(data: dict, today_label: str) -> str:
    inbound, signals, ageing = data["inbound"], data["signals"], data["ageing"]
    lines = [f"MedTech Radar. Weekly digest for {today_label}.", ""]

    if inbound:
        lines.append(f"Inbound. {len(inbound)} at or above {data['threshold']}.")
    else:
        lines.append("Inbound. Nothing cleared the bar since the last digest.")
    for i, o in enumerate(inbound, 1):
        lines.append("")
        lines.append(f"{i}. {nz(o['title'])}. {nz(o['company'])}. {nz(o['location'])}.")
        lines.append(f"   Scores. Combined {o['combined']}. CV {o['cv_match']}. "
                     f"Want {o['want_match']}. Rate {rate_word(o)}.")
        if o["one_line_why"]:
            lines.append(f"   {sentence(o['one_line_why'])}")
        flags = parse_flags(o["red_flags"])
        lines.append("   Red flags. "
                     + (" ".join(sentence(f) for f in flags) if flags else "None noted."))
        action = sentence(o["suggested_action"]) or "No action suggested."
        act_by = f" Act by {fmt_date(o['act_by'])}." if o["act_by"] else ""
        lines.append(f"   Next step. {action}{act_by}")
        if o["source_url"]:
            lines.append(f"   {o['source_url']}")
    if inbound:
        lines.append("")
        lines.append(rate_legend(data["floor"]))

    lines.append("")
    if signals:
        lines.append(f"Signals. {len(signals)} worth a look.")
    else:
        lines.append("Signals. Quiet since the last digest.")
    for i, s in enumerate(signals, 1):
        lines.append("")
        if s["kind"] == "signal":
            lines.append(f"{i}. {sentence(s['headline'] or s['company'])}")
            lines.append(f"   Relevance {s['score']}. {nz(s['company'])}.")
            if s.get("why"):
                lines.append(f"   {sentence(s['why'])}")
            if s.get("playbook_step"):
                lines.append(f"   Playbook step. {sentence(s['playbook_step'])}")
        else:
            lines.append(f"{i}. {nz(s['title'])}. {nz(s['company'])}. {nz(s['location'])}.")
            lines.append(f"   Scores. Combined {s['combined']}. CV {s['cv_match']}. "
                         f"Want {s['want_match']}. Rate {rate_word(s)}.")
            if s["one_line_why"]:
                lines.append(f"   {sentence(s['one_line_why'])}")
        if s.get("source_url"):
            lines.append(f"   {s['source_url']}")
    # Rate words appear on opportunity-kind signal entries too, so a week
    # with no inbound but a scored role in Signals still gets its legend.
    if not inbound and any(s["kind"] == "opportunity" for s in signals):
        lines.append("")
        lines.append(rate_legend(data["floor"]))

    lines.append("")
    lines.append("Ageing. Flagged items older than two weeks with no movement.")
    if ageing:
        for a in ageing:
            label = (f"{a['title']}, {a['company']}" if a["kind"] == "opportunity"
                     else (a["headline"] or a["company"]))
            lines.append(f"- {label}. First seen {fmt_date(a['first_seen'])}. "
                         f"Status {a['status']}.")
    else:
        lines.append("- Nothing sitting idle.")

    lines.append("")
    lines.append("Threads awaiting action. The latest touch per company, from the tracker.")
    if data["threads"]:
        for t in data["threads"]:
            due = f" By {fmt_date(t['next_action_date'])}." if t["next_action_date"] else ""
            lines.append(f"- {t['company']}. {sentence(t['next_action'])}{due} "
                         f"Last touch {fmt_date(t['touched_at'])} "
                         f"via {t['channel'] or 'other'}.")
    else:
        lines.append("- Nothing waiting. Every thread is quiet.")

    if data["needs_review"]:
        lines.append("")
        lines.append("Needs review. The scorer could not score these, so a"
                     " human look is due.")
        for n in data["needs_review"]:
            lines.append(f"- {n['label']}, {n['company']}. {sentence(n['note'])}")
        lines.append("Clear this list with python scripts/rescore.py once the"
                     " cause is fixed.")

    lines.append("")
    lines.append(stats_line(data["stats"]))
    return "\n".join(lines) + "\n"


def render_html(data: dict, today_label: str) -> str:
    inbound, signals, ageing = data["inbound"], data["signals"], data["ageing"]
    parts = ["<div style=\"font-family:Georgia,serif;max-width:640px\">",
             f"<h1 style=\"font-size:20px\">MedTech Radar. Weekly digest for {esc(today_label)}.</h1>"]

    parts.append("<h2 style=\"font-size:16px\">Inbound</h2>")
    if inbound:
        parts.append("<ol>")
        for o in inbound:
            flags = parse_flags(o["red_flags"])
            flag_text = " ".join(sentence(f) for f in flags) if flags else "None noted."
            action = sentence(o["suggested_action"]) or "No action suggested."
            act_by = f" Act by {esc(fmt_date(o['act_by']))}." if o["act_by"] else ""
            link = (f"<br><a href=\"{esc(o['source_url'])}\">View the advert</a>"
                    if o["source_url"] else "")
            parts.append(
                f"<li><strong>{esc(o['title'])}</strong>. {esc(o['company'])}. "
                f"{esc(o['location'])}.<br>"
                f"Scores. Combined {o['combined']}. CV {o['cv_match']}. Want {o['want_match']}. "
                f"Rate {esc(rate_word(o))}.<br>"
                f"<em>{esc(sentence(o['one_line_why']))}</em><br>"
                f"Red flags. {esc(flag_text)}<br>"
                f"Next step. {esc(action)}{act_by}{link}</li>")
        parts.append("</ol>")
        parts.append(f"<p style=\"color:#666;font-size:12px\">"
                     f"{esc(rate_legend(data['floor']))}</p>")
    else:
        parts.append("<p>Nothing cleared the bar since the last digest.</p>")

    parts.append("<h2 style=\"font-size:16px\">Signals</h2>")
    if signals:
        parts.append("<ol>")
        for s in signals:
            link = (f"<br><a href=\"{esc(s['source_url'])}\">Source</a>"
                    if s.get("source_url") else "")
            if s["kind"] == "signal":
                why = f"<br><em>{esc(sentence(s['why']))}</em>" if s.get("why") else ""
                step = (f"<br>Playbook step. {esc(sentence(s['playbook_step']))}"
                        if s.get("playbook_step") else "")
                # The group name as a plain prefix, nothing more. The
                # digest is a list to read on a phone, not a page to browse,
                # so geography earns three words and no headings.
                where = f"{esc(s.get('region'))}. " if s.get("region") else ""
                parts.append(
                    f"<li><strong>{where}{esc(s['headline'] or s['company'])}</strong><br>"
                    f"Relevance {s['score']}. {esc(s['company'])}.{why}{step}{link}</li>")
            else:
                parts.append(
                    f"<li><strong>{esc(s['title'])}</strong>. {esc(s['company'])}. "
                    f"{esc(s['location'])}.<br>"
                    f"Scores. Combined {s['combined']}. CV {s['cv_match']}. "
                    f"Want {s['want_match']}. Rate {esc(rate_word(s))}.<br>"
                    f"<em>{esc(sentence(s['one_line_why']))}</em>{link}</li>")
        parts.append("</ol>")
        if not inbound and any(s["kind"] == "opportunity" for s in signals):
            parts.append(f"<p style=\"color:#666;font-size:12px\">"
                         f"{esc(rate_legend(data['floor']))}</p>")
    else:
        parts.append("<p>Quiet since the last digest.</p>")

    parts.append("<h2 style=\"font-size:16px\">Ageing</h2>")
    if ageing:
        parts.append("<ul>")
        for a in ageing:
            label = (f"{a['title']}, {a['company']}" if a["kind"] == "opportunity"
                     else (a["headline"] or a["company"]))
            parts.append(f"<li>{esc(label)}. First seen {esc(fmt_date(a['first_seen']))}. "
                         f"Status {a['status']}.</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>Nothing sitting idle.</p>")

    parts.append("<h2 style=\"font-size:16px\">Threads awaiting action</h2>")
    if data["threads"]:
        parts.append("<ul>")
        for t in data["threads"]:
            due = (f" By {esc(fmt_date(t['next_action_date']))}."
                   if t["next_action_date"] else "")
            parts.append(f"<li>{esc(t['company'])}. {esc(sentence(t['next_action']))}{due} "
                         f"Last touch {esc(fmt_date(t['touched_at']))} "
                         f"via {esc(t['channel'] or 'other')}.</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>Nothing waiting. Every thread is quiet.</p>")

    if data["needs_review"]:
        parts.append("<h2 style=\"font-size:16px\">Needs review</h2>")
        parts.append("<p>The scorer could not score these, so a human look"
                     " is due.</p>")
        parts.append("<ul>")
        for n in data["needs_review"]:
            parts.append(f"<li>{esc(n['label'])}, {esc(n['company'])}. "
                         f"{esc(sentence(n['note']))}</li>")
        parts.append("</ul>")
        parts.append("<p>Clear this list with <code>python scripts/rescore.py"
                     "</code> once the cause is fixed.</p>")

    parts.append(f"<p style=\"color:#666\">{esc(stats_line(data['stats']))}</p>")
    parts.append("</div>")
    return "\n".join(parts)


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
    text = render_text(data, today_label)
    html = render_html(data, today_label)
    item_count = len(data["inbound"]) + len(data["signals"])
    n_sig = len(data["signals"])
    if item_count == 0:
        subject = f"Radar digest. A quiet week, nothing new. {today_label}."
    else:
        subject = (f"Radar digest. {len(data['inbound'])} inbound, "
                   f"{n_sig} {plural(n_sig, 'signal', 'signals')}. {today_label}.")

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
                          "token": token, "send": send,
                          "to": config.get("digest_to", "")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
