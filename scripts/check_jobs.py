"""Radar page checker.

Asks the public job page itself whether a top or question role is still
accepting applications. A row with no liveness signal in seven days, no
sighting and no live check, gets one polite anonymous fetch and a verdict
from agent B's ladder: gone, closed, live or unknown. Only a documented
closed marker ever closes a role. Unknown changes nothing and is asked
again a week later.

The daily run ends with the expiry pass: any role whose repost chain has
shown no sign of life in forty-five days retires as expired, every tier
including filtered. A repost by the same company under the same title
resets the chain's clock.

Safety posture:
- Dry run is the default. Fetches happen and the classifications print,
  nothing is written. Live stamps need an explicit --live.
- Polite fetching. Sequential, 8 to 12 seconds apart, 20 second timeout,
  no in-run retries, hard cap per run. A 429 is a wait, not a stop: the
  row is not stamped and stays due, the run backs off ten minutes and
  carries on with the next row. The third 429 in one run ends it and
  the rest of the batch waits for tomorrow. The study saw the limit
  lift within seconds, so one 429 should never cost a day.
- Honest verdicts. Body text, JSON-LD and the bare HTTP status each
  mislead on their own, the study showed it, so none of them decides
  anything alone. The default verdict is unknown.

Flags:
  --dry-run   fetch and classify, write nothing (also the default)
  --live      write the stamps, close on a confirmed marker
  --cap N     fetches per run, default 25
  --id N      spot-check one stored row, bypasses the due gate
  --db PATH   alternate database for test isolation
  --backoff S seconds to wait after a 429 before the next row, default 600
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402

CAP_DEFAULT = 25
RECHECK_DAYS = 7                 # an answered page is not asked again inside a week
EXPIRY_DAYS = 45                 # no sign of life this long retires the role (J17)
SPACING_SECONDS = (8.0, 12.0)    # agent B hit one 429 in ten at five seconds
BACKOFF_SECONDS = 600            # the wait after a 429, a run of 25 stays under half an hour
MAX_429_PER_RUN = 3              # the third 429 ends the run
FETCH_TIMEOUT = 20
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
             " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_JOB_VIEW = re.compile(r"/jobs/view/(\d+)")
_AUTHWALL_WORDS = ("authwall", "login", "checkpoint")
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_DECORATED_ID = re.compile(r"decoratedJobPostingId\D{0,64}(\d+)")


# ------------------------------------------------------------------ fetching

def job_view_id(url: str) -> str | None:
    """The numeric LinkedIn job id in a stored advert URL, or None."""
    match = _JOB_VIEW.search(url or "")
    return match.group(1) if match else None


def fetch_job_page(url: str) -> tuple[int, str, str]:
    """One anonymous fetch of a public job page.

    Returns (status, final_url, body). Redirects are followed because the
    final URL is part of the evidence, the expired redirect carries its
    marker in the query. Chrome UA and identity encoding are the recipe
    the study validated. Status 0 means the network failed, which is an
    answer about this machine, not about the page.
    """
    if rc.mock_mode_active():
        raise RuntimeError(
            "RADAR_MOCK=1 but a real fetch was requested. Tests stub"
            " fetch_job_page, they never touch the network.")
    req = urllib.request.Request(url, headers={
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            body = resp.read(3_000_000).decode("utf-8", "replace")
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as err:
        return err.code, getattr(err, "url", None) or url, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, url, ""


def _pause() -> None:
    """The gap between fetches. Skipped in mock mode, tests never wait."""
    if rc.mock_mode_active():
        return
    time.sleep(random.uniform(*SPACING_SECONDS))


def _backoff(seconds: float) -> None:
    """The wait after a 429. Skipped in mock mode, tests never wait."""
    if rc.mock_mode_active() or seconds <= 0:
        return
    time.sleep(seconds)


def _log(line: str) -> None:
    """One line to stderr at the moment it happens, so a long run shows
    its 429s in the n8n execution log as they land, not at the end."""
    print(f"check_jobs: {line}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- classifying

def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _first_h1(body: str) -> str:
    match = _H1.search(body)
    if not match:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))


def classify(status: int, final_url: str, body: str,
             title: str | None, job_id: str) -> tuple[str, str]:
    """The ladder, in order, from the ten-page study of 7 November.

    Returns (verdict, reason). gone and closed are the only verdicts that
    retire a role, and each needs its documented marker. live needs the
    title, the apply button and the posting id to agree. Everything else
    is unknown, so a changed page shape on LinkedIn's side shows up as a
    spike in unknowns, never as silent wrong closes.
    """
    query = urllib.parse.urlsplit(final_url or "").query
    if "expired_jd_redirect" in query:
        return "gone", "redirected away with trk=expired_jd_redirect"
    if status == 429:
        return "unknown", "rate limited, 429"
    lowered = (final_url or "").lower()
    if any(word in lowered for word in _AUTHWALL_WORDS):
        return "unknown", "the fetch hit the authwall"
    if status != 200 or not body:
        return "unknown", f"status {status} with no readable page"
    if not _JOB_VIEW.search(urllib.parse.urlsplit(final_url).path):
        return "unknown", "redirected off the job page without the marker"
    if "closed-job" in body:
        if "apply-button" not in body:
            return "closed", "closed-job marker and no apply button"
        return "unknown", "closed-job marker beside an apply button, contradictory"
    h1 = _first_h1(body)
    if not h1:
        return "unknown", "no h1 on the page"
    decorated = _DECORATED_ID.search(body)
    if (_collapse(h1) == _collapse(title or "")
            and "apply-button" in body
            and decorated is not None and decorated.group(1) == job_id):
        return "live", "title, apply button and posting id all agree"
    return "unknown", "the page matched neither the live nor the closed shape"


# ----------------------------------------------------------------- selection

def due_candidates(conn, now: str | None = None) -> list[dict]:
    """Top and question rows with no liveness signal in seven days.

    A sighting or a live check inside the window keeps a row off the
    list, and so does a check of any verdict, so an unanswered page is
    asked weekly rather than daily. Top rows come first, then the
    stalest liveness, so the cap spends itself where it matters. Rows
    without a LinkedIn job URL cannot be asked and are left out.
    """
    now = now or rc.now_iso()
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    recheck_cutoff = ((moment - timedelta(days=RECHECK_DAYS))
                      .strftime("%Y-%m-%dT%H:%M:%SZ"))
    rows = conn.execute(
        "SELECT * FROM opportunities"
        " WHERE acknowledged_at IS NULL AND closed_at IS NULL"
        " AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND tier IN ('top', 'question')"
        " ORDER BY CASE tier WHEN 'top' THEN 0 ELSE 1 END,"
        " COALESCE(last_seen, first_seen)").fetchall()
    due = []
    for raw in rows:
        row = dict(raw)
        if rc.life_state(row, now) != "unknown":
            continue
        if (row.get("last_checked_at") or "") > recheck_cutoff:
            continue
        if not job_view_id(row.get("source_url") or ""):
            continue
        due.append(row)
    return due


# ----------------------------------------------------------------- the run

def run_checks(conn, cap: int = CAP_DEFAULT, write: bool = False,
               only_id: int | None = None, now: str | None = None,
               pause=None, backoff: float = BACKOFF_SECONDS,
               wait=None) -> dict:
    """Fetch, classify and (in live mode) stamp one capped batch.

    backoff is the wait in seconds after a 429, wait is the function that
    performs it. Tests pass a recorder and zero.
    """
    now = now or rc.now_iso()
    pause = pause or _pause
    wait = wait or _backoff
    result = {
        "mode": "live" if write else "dry-run",
        "due": 0, "checked": 0, "closed": 0,
        "rate_limited": 0, "backoff_seconds": 0, "rate_limits": [],
        "verdicts": {}, "checks": [], "notes": [],
    }

    if only_id is not None:
        raw = conn.execute("SELECT * FROM opportunities WHERE id = ?",
                           (only_id,)).fetchone()
        if raw is None:
            result["notes"].append(f"no stored row with id {only_id}")
            return result
        rows = [dict(raw)]
    else:
        rows = due_candidates(conn, now)
    result["due"] = len(rows)
    batch = len(rows[:max(cap, 0)])

    for index, row in enumerate(rows[:batch]):
        if index:
            pause()
        jid = job_view_id(row.get("source_url") or "")
        if not jid:
            result["notes"].append(
                f"id {row['id']}: no LinkedIn job id in its URL, skipped")
            continue
        status, final_url, body = fetch_job_page(
            f"https://www.linkedin.com/jobs/view/{jid}/")
        if status == 0:
            result["notes"].append(
                f"id {row['id']}: network failure, run stopped, row left due")
            break
        verdict, reason = classify(status, final_url, body,
                                   row.get("title"), jid)
        result["checked"] += 1
        result["verdicts"][verdict] = result["verdicts"].get(verdict, 0) + 1
        result["checks"].append({
            "id": row["id"], "title": row.get("title"), "status": status,
            "verdict": verdict, "reason": reason,
        })
        if status == 429:
            # A 429 says nothing about the page, so nothing is stamped and
            # the row stays due, at the head of tomorrow's batch. The run
            # waits out the limit and moves on to the next row. The same
            # URL is never asked twice in one run. A third 429 means the
            # limit is holding, so the run ends and the rest waits.
            stamp = rc.now_iso()
            result["rate_limited"] += 1
            entry = {"ts": stamp, "id": row["id"], "fetch": index + 1,
                     "waited": 0}
            result["rate_limits"].append(entry)
            if result["rate_limited"] >= MAX_429_PER_RUN:
                line = (f"{stamp}: 429 on id {row['id']}, fetch {index + 1}"
                        f" of {batch}, the third this run, run stopped,"
                        f" {batch - index - 1} left due")
                result["notes"].append(line)
                _log(line)
                break
            entry["waited"] = backoff
            result["backoff_seconds"] += backoff
            line = (f"{stamp}: 429 on id {row['id']}, fetch {index + 1}"
                    f" of {batch}, waiting {backoff:g}s, row left due")
            result["notes"].append(line)
            _log(line)
            wait(backoff)
            continue
        if write:
            conn.execute(
                "UPDATE opportunities SET last_checked_at = ?,"
                " last_check_result = ? WHERE id = ?",
                (rc.now_iso(), verdict, row["id"]))
            if verdict in ("gone", "closed"):
                cur = conn.execute(
                    "UPDATE opportunities SET closed_at = ?,"
                    " closed_source = 'page_check'"
                    " WHERE id = ? AND closed_at IS NULL",
                    (rc.now_iso(), row["id"]))
                if cur.rowcount:
                    result["closed"] += 1
            conn.commit()
        elif verdict in ("gone", "closed"):
            result["closed"] += 1
    return result


# ------------------------------------------------------------------- expiry

def expire_stale(conn, write: bool = False, now: str | None = None) -> dict:
    """Retire every role with no sign of life in forty-five days.

    The clock runs on the repost chain, not the advert: the newest
    sighting or live check across every row sharing a role_key, retired
    siblings included, because their history is still history. A repost
    resets the whole chain, so a role that keeps being re-advertised
    never expires, while a genuinely stale one goes quietly, every tier
    including filtered. A row without a key stands alone on its own
    clock. Expiry is retirement, not closure, nothing here claims the
    page said anything.
    """
    now = now or rc.now_iso()
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    cutoff = ((moment - timedelta(days=EXPIRY_DAYS))
              .strftime("%Y-%m-%dT%H:%M:%SZ"))
    newest: dict[str, str] = {}
    for row in conn.execute(
            "SELECT role_key, last_seen, first_seen, last_checked_at,"
            " last_check_result FROM opportunities"
            " WHERE role_key IS NOT NULL"):
        seen = rc.latest_liveness(row)
        if seen > newest.get(row["role_key"], ""):
            newest[row["role_key"]] = seen
    expired = []
    for row in conn.execute(
            "SELECT id, title, company, role_key, last_seen, first_seen,"
            " last_checked_at, last_check_result FROM opportunities"
            " WHERE acknowledged_at IS NULL AND closed_at IS NULL"
            " AND dismissed_at IS NULL AND expired_at IS NULL"):
        seen = rc.latest_liveness(row)
        if row["role_key"]:
            seen = max(seen, newest.get(row["role_key"], ""))
        if seen < cutoff:
            expired.append({"id": row["id"], "title": row["title"],
                            "company": row["company"], "newest": seen})
    if write and expired:
        stamp = rc.now_iso()
        conn.executemany(
            "UPDATE opportunities SET expired_at = ?"
            " WHERE id = ? AND expired_at IS NULL",
            [(stamp, item["id"]) for item in expired])
        conn.commit()
    return {"expired": len(expired), "sample": expired[:10]}


# ----------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and classify, write nothing (default)")
    ap.add_argument("--live", action="store_true",
                    help="write the stamps, close on a confirmed marker")
    ap.add_argument("--cap", type=int, default=CAP_DEFAULT,
                    help=f"fetches per run, default {CAP_DEFAULT}")
    ap.add_argument("--id", type=int, metavar="N",
                    help="spot-check one stored row, bypasses the due gate")
    ap.add_argument("--db", metavar="PATH", help="alternate database path")
    ap.add_argument("--backoff", type=float, default=BACKOFF_SECONDS,
                    metavar="S",
                    help="seconds to wait after a 429 before the next row,"
                         f" default {BACKOFF_SECONDS}")
    args = ap.parse_args(argv)

    if args.dry_run and args.live:
        print("Choose one of --dry-run or --live, not both.", file=sys.stderr)
        return 2
    write = args.live
    if not write and not args.dry_run:
        print("No mode flag given, defaulting to dry run.", file=sys.stderr)

    conn = rc.get_db(Path(args.db)) if args.db else rc.get_db()
    result = run_checks(conn, cap=args.cap, write=write, only_id=args.id,
                        backoff=args.backoff)
    if args.id is None:
        # The expiry pass rides on the daily run, never on a spot check.
        expiry = expire_stale(conn, write=write)
        result["expired"] = expiry["expired"]
        result["expired_sample"] = expiry["sample"]

    verdicts = ", ".join(f"{v} {k}" for k, v in sorted(
        result["verdicts"].items(), key=lambda kv: -kv[1]))
    note = f"{result['due']} due, {result['checked']} checked"
    if verdicts:
        note += f", {verdicts}"
    if result["rate_limited"]:
        # The clock times of every 429 ride in the runs row, so a week of
        # rows shows whether the limit has a pattern.
        stamps = ", ".join(e["ts"][11:] for e in result["rate_limits"])
        note += (f", {result['rate_limited']} rate limited, 429 at {stamps},"
                 f" {result['backoff_seconds']:g}s backoff")
    if result.get("expired"):
        note += f", {result['expired']} expired"
    rc.log_run(conn, "check", mode=result["mode"],
               items_in=result["checked"], items_new=result["closed"],
               note=note)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
