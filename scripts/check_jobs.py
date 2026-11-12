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

While the page is open the checker reads it (Brief C). On a verdict of
live, or closed with the description still served, it stores the advert's
own description, maps the job criteria and the description's words to
contract, permanent or unknown, and where the alert card never carried a
figure, takes the page's stated pay and re-runs the rate gate and the
tier in code. Nothing is read off a gone, unknown or login-wall result.
Every word stored is the advert's, none is the checker's (J9).

Selection (J20): a top or question role with no live confirmation in
seven days, or whose page has never been read, is checked at source,
stale rows first, never-read rows after them, inside the same cap.

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
import engagement  # noqa: E402
import regate_rates  # noqa: E402

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

# The parts of a guest job page the checker reads (Brief C). Each is a
# class name LinkedIn has carried for years, and each is matched loosely
# so a reshuffled attribute order still reads. A page that lacks one
# simply yields nothing for it.
_CRITERIA = re.compile(
    r'description__job-criteria-subheader"[^>]*>\s*(.*?)\s*</h3>\s*'
    r'<span[^>]*>\s*(.*?)\s*</span>', re.DOTALL)
_MARKUP = re.compile(
    r'<div[^>]*class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)'
    r'(?:</div>\s*<button|</section>)', re.DOTALL)
_TOP_CARD = re.compile(
    r'<section[^>]*class="[^"]*top-card-layout[^"]*"[^>]*>.*?</section>',
    re.DOTALL)
_TOP_CARD_SALARY = re.compile(
    r'<(?:div|span)[^>]*class="[^"]*(?:compensation__salary|salary)[^"]*"'
    r'[^>]*>\s*([^<]+?)\s*<', re.DOTALL)
_BREAKS = re.compile(r"</(?:p|li|h\d|div|tr)>|<br\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
# A sterling figure, with an optional second figure after "to" or a
# dash, and an optional period token between them ("£90,000/yr -
# £110,000/yr"). "k" multiplies by a thousand.
_FIGURE = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MONEY = re.compile(
    r"£\s?(?P<lo>" + _FIGURE + r")\s?(?P<lok>k\b)?"
    r"(?:(?:\s*/\s?[a-z]+)?\s*(?:to|-|–|—)\s*£?\s?"
    r"(?P<hi>" + _FIGURE + r")\s?(?P<hik>k\b)?)?",
    re.IGNORECASE)
_PERIODS = (
    ("day", re.compile(
        r"\b(?:per|a|each)\s+day\b|/\s?day\b|\bp/?d\b|\bdaily\b"
        r"|\bday[\s-]?rate\b|/d\b", re.IGNORECASE)),
    ("hour", re.compile(
        r"\b(?:per|an)\s+hour\b|/\s?h(?:ou)?r\b|\bp/?h\b|\bhourly\b",
        re.IGNORECASE)),
    ("year", re.compile(
        r"\bper\s+(?:annum|year)\b|\ba\s+year\b|\bp\.\s?a\.?(?=\W|$)"
        r"|/\s?(?:yr|year|annum)\b|\bannual(?:ly)?\b|\bannum\b|\bpa\b",
        re.IGNORECASE)),
)
PAY_CONTEXT_CHARS = 40       # how far either side of a figure a period may sit
YEAR_FLOOR_GBP = 10_000      # a bare top-card figure this big can only be a year
DESCRIPTION_CAP = 20_000     # characters of description kept on the row


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


# ------------------------------------------------------------ reading the page

def _text(fragment: str) -> str:
    """Tags out, entities decoded, whitespace folded, one line per block."""
    text = _BREAKS.sub("\n", fragment or "")
    text = html.unescape(_TAGS.sub(" ", text))
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip()
             for line in text.split("\n")]
    return re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()


def _amount(figure: str, thousands: str | None) -> float:
    value = float(figure.replace(",", ""))
    return value * 1000 if thousands else value


def stated_pay(text: str, require_period: bool = True) -> dict | None:
    """The first sterling figure in text that says what period it is for.

    A range takes both ends. The period is the word beside the figure:
    per day, an hour, a year and their short forms, looked for within
    forty characters either side, stopping at the next sterling sign so
    one figure's period is never read as another's. With require_period
    False, for the top
    card, a bare figure of ten thousand or more reads as a year, because
    nothing else is that large; a smaller bare figure is kept verbatim
    with no period, so it is stored but never gated. Returns None when
    the text names no usable figure. The verbatim string is the advert's
    own words, figure and period together.
    """
    text = text or ""
    for match in _MONEY.finditer(text):
        lo = _amount(match.group("lo"), match.group("lok"))
        hi = (_amount(match.group("hi"), match.group("hik"))
              if match.group("hi") else lo)
        period = None
        start, end = match.start(), match.end()
        after = text[end:end + PAY_CONTEXT_CHARS].split("£")[0]
        before = text[max(0, start - PAY_CONTEXT_CHARS):start].split("£")[-1]
        for name, pattern in _PERIODS:
            hit = pattern.search(after)
            if hit:
                period, end = name, end + hit.end()
                break
        if period is None:
            for name, pattern in _PERIODS:
                hits = list(pattern.finditer(before))
                if hits:
                    period = name
                    start -= len(before) - hits[-1].start()
                    break
        if period is None:
            if require_period:
                continue
            if max(lo, hi) >= YEAR_FLOOR_GBP:
                period = "year"
        verbatim = re.sub(r"\s+", " ", text[start:end]).strip(" ,.;:")
        return {"text": verbatim, "period": period, "min": min(lo, hi),
                "max": max(lo, hi), "currency": "GBP"}
    return None


def read_page(body: str) -> dict:
    """What a served job page says, in its own words.

    criteria is the job criteria list as LinkedIn labels it, with
    employment_type lifted out. description is the show-more-less markup
    as text, capped. pay is the top card's salary element if there is
    one, otherwise the first figure in the description with a period
    beside it, and pay_from says which. A page without a part yields
    nothing for that part.
    """
    body = body or ""
    out = {"criteria": {}, "employment_type": None, "description": "",
           "pay": None, "pay_from": None}
    for head, value in _CRITERIA.findall(body):
        out["criteria"][_text(head)] = _text(value)
    out["employment_type"] = out["criteria"].get("Employment type")
    markup = _MARKUP.search(body)
    if markup:
        out["description"] = _text(markup.group(1))[:DESCRIPTION_CAP]
    card = _TOP_CARD.search(body)
    if card:
        salary = _TOP_CARD_SALARY.search(card.group(0))
        if salary:
            pay = stated_pay(_text(salary.group(1)), require_period=False)
            if pay:
                out["pay"], out["pay_from"] = pay, "top card"
    if out["pay"] is None and out["description"]:
        pay = stated_pay(out["description"])
        if pay:
            out["pay"], out["pay_from"] = pay, "description"
    return out


def map_engagement(description: str, employment_type: str | None) -> tuple[str, str]:
    """Contract, permanent or unknown from the page, with the reason.

    The description's own words go first, through the engagement rules:
    a contract marker there (day rate, interim, IR35, contract) wins,
    because LinkedIn lists most contracts as Full-time. Then the
    criteria: Contract or Temporary set contract, Full-time or Part-time
    set permanent. A fixed-term or permanent word in the description
    counts the same way. Anything else stays unknown.
    """
    value, note = engagement.classify(description or "")
    if value == "contract":
        return "contract", f"{note}, from the page"
    criteria = (employment_type or "").strip()
    word = criteria.lower()
    if word in ("contract", "temporary"):
        return "contract", f"criteria {criteria}"
    if value == "permanent":
        return "permanent", f"{note}, from the page"
    if word in ("full-time", "part-time"):
        return "permanent", f"criteria {criteria}"
    return "unknown", ""


def apply_page(conn, row: dict, page: dict, floors: dict,
               config: dict | None = None, write: bool = False,
               thresholds: dict | None = None) -> dict:
    """Store what the page said on the row and its silent chain siblings.

    The description is kept. An engagement type other than unknown is
    written to the fetched advert and to every advert sharing its
    role_key that still reads unknown, so the badge shows across the
    chain. Where the row stored no pay figure and the page gives one, the
    figure goes on the row with pay_source "page" and the rate gate and
    tier are derived again in code, no model call. A row that already
    had a figure keeps it. Returns what happened, or in a dry run what
    would have. A re-tier overwrites the score the row holds, so when it is
    written the outgoing score goes to score_history first, in the same
    transaction. thresholds is the CV pass mark and the Filtered floor.
    """
    out = {"id": row["id"], "engagement_type": "unknown", "engagement_note": "",
           "siblings": 0, "pay": None, "tier": None,
           "description_chars": len(page.get("description") or "")}
    sets: dict = {}
    if page.get("description"):
        sets["description_text"] = page["description"]
        # When the text was stored, beside it, so that a date shown for a
        # description is never older than the words (Prepare application).
        sets["description_fetched_at"] = rc.now_iso()
    value, note = map_engagement(page.get("description"), page.get("employment_type"))
    out["engagement_type"], out["engagement_note"] = value, note
    siblings: list[int] = []
    if value != "unknown":
        sets.update(engagement_type=value, engagement_note=note)
        if row.get("role_key"):
            siblings = [r["id"] for r in conn.execute(
                "SELECT id FROM opportunities WHERE role_key = ? AND id != ?"
                " AND (engagement_type IS NULL OR engagement_type = 'unknown')",
                (row["role_key"], row["id"]))]
    out["siblings"] = len(siblings)
    pay = page.get("pay")
    had_figure = any((row.get(col) or 0) > 0 for col in ("pay_min", "pay_max"))
    if pay and not had_figure:
        sets.update(pay_currency=pay["currency"], pay_period=pay["period"] or "",
                    pay_min=pay["min"], pay_max=pay["max"], pay_source="page")
        if not (row.get("salary_rate") or "").strip():
            sets["salary_rate"] = pay["text"]
        out["pay"] = {"text": pay["text"], "period": pay["period"],
                      "from": page.get("pay_from")}
        if row.get("tier"):
            judged = dict(row)
            judged.update(sets)
            new, verdict = regate_rates.judge_row(judged, floors, config,
                                                  thresholds)
            if verdict["gate"] is not None:
                sets.update(new)
                out["tier"] = {"from": row["tier"], "to": new["tier"],
                               "gate": verdict["gate"], "basis": verdict["basis"],
                               "figure": verdict["figure"]}
    if write and sets:
        # The write lock first, then look again. The page was read for a
        # while, and a role can be applied for in that time. An applied role
        # keeps what it has, and nothing is written for it or for its chain.
        rc.begin_write(conn)
        if rc.role_is_applied(conn, row["id"]):
            conn.rollback()
            out["left_alone"] = True
            return out
        if any(col in sets for col in regate_rates._GATE_COLS):
            rc.archive_score(conn, row["id"], "check_jobs")
        conn.execute(
            "UPDATE opportunities SET " + ", ".join(f"{c} = ?" for c in sets)
            + " WHERE id = ?", (*sets.values(), row["id"]))
        if siblings:
            conn.executemany(
                "UPDATE opportunities SET engagement_type = ?, engagement_note = ?"
                " WHERE id = ?", [(value, note, sid) for sid in siblings])
    return out


# ----------------------------------------------------------------- selection

def due_candidates(conn, now: str | None = None) -> list[dict]:
    """Top and question rows due a look at their page (J20).

    Two reasons, in this order. Stale: no liveness signal in seven days.
    A sighting or a live check inside the window keeps a row off this
    part of the list, and so does a check of any verdict, so an
    unanswered page is asked weekly rather than daily. Never read: the
    page has never been checked at all, however fresh the sighting, so
    its type and pay get read once. Within each reason Top rows come
    first, then the stalest liveness, so the cap spends itself where it
    matters. Each row carries its reason in due_reason. Rows without a
    LinkedIn job URL cannot be asked and are left out.
    """
    now = now or rc.now_iso()
    moment = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    recheck_cutoff = ((moment - timedelta(days=RECHECK_DAYS))
                      .strftime("%Y-%m-%dT%H:%M:%SZ"))
    rows = conn.execute(
        "SELECT * FROM opportunities"
        " WHERE acknowledged_at IS NULL AND closed_at IS NULL"
        " AND dismissed_at IS NULL AND expired_at IS NULL"
        " AND " + rc.NOT_APPLIED_SQL +
        " AND tier IN ('top', 'question')"
        " ORDER BY CASE tier WHEN 'top' THEN 0 ELSE 1 END,"
        " COALESCE(last_seen, first_seen)").fetchall()
    due = []
    never_read = []
    for raw in rows:
        row = dict(raw)
        if not job_view_id(row.get("source_url") or ""):
            continue
        if rc.life_state(row, now) == "unknown":
            if (row.get("last_checked_at") or "") > recheck_cutoff:
                continue
            row["due_reason"] = "stale"
            due.append(row)
        elif not row.get("last_checked_at"):
            row["due_reason"] = "never read"
            never_read.append(row)
    return due + never_read


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
        "due": 0, "due_never_read": 0, "checked": 0, "closed": 0,
        "rate_limited": 0, "backoff_seconds": 0, "rate_limits": [],
        "verdicts": {}, "checks": [], "notes": [],
        "pages_read": 0, "types_set": 0, "siblings_set": 0, "pay_set": 0,
        "tier_moves": {}, "page_reads": [],
    }
    floors = None
    thresholds = None
    config = None

    if only_id is not None:
        raw = conn.execute("SELECT * FROM opportunities WHERE id = ?",
                           (only_id,)).fetchone()
        if raw is None:
            result["notes"].append(f"no stored row with id {only_id}")
            return result
        if rc.role_is_applied(conn, only_id):
            # A spot check bypasses the due gate, not the rule that an
            # applied role is left alone. Nothing is fetched or stamped.
            result["notes"].append(
                f"id {only_id}: on a role marked applied, left alone")
            return result
        rows = [dict(raw)]
    else:
        rows = due_candidates(conn, now)
    result["due"] = len(rows)
    result["due_never_read"] = sum(
        1 for r in rows if r.get("due_reason") == "never read")
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
        if write and rc.role_is_applied(conn, row["id"]):
            # Marked applied after it fell due, while the page was being
            # fetched. Nothing is read off the page, stamped or closed.
            result["notes"].append(
                f"id {row['id']}: marked applied since it fell due, left alone")
            continue
        verdict, reason = classify(status, final_url, body,
                                   row.get("title"), jid)
        result["checked"] += 1
        result["verdicts"][verdict] = result["verdicts"].get(verdict, 0) + 1
        result["checks"].append({
            "id": row["id"], "title": row.get("title"), "status": status,
            "verdict": verdict, "reason": reason,
            "due": row.get("due_reason", "spot check"),
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
        # The page was served, so read it while it is open (Brief C). A
        # closed page still carries the description until LinkedIn drops
        # it, and the type and pay are as true of a closed advert as of a
        # live one. Gone, unknown and authwall results say nothing about
        # the advert and nothing is read off them.
        if verdict == "live" or (verdict == "closed" and _MARKUP.search(body)):
            if floors is None:
                config = rc.load_config()
                floors = rc.read_rate_floors(config)
                thresholds = rc.read_score_thresholds(config)
            outcome = apply_page(conn, row, read_page(body), floors, config,
                                 write=write, thresholds=thresholds)
            if outcome.get("left_alone"):
                # Applied for while the page was being read, so nothing was
                # written, and nothing more is stamped or closed for the row.
                result["notes"].append(
                    f"id {row['id']}: marked applied since it fell due, "
                    "left alone")
                continue
            result["pages_read"] += 1
            result["page_reads"].append(outcome)
            if outcome["engagement_type"] != "unknown":
                result["types_set"] += 1
                result["siblings_set"] += outcome["siblings"]
            if outcome["pay"]:
                result["pay_set"] += 1
            move = outcome["tier"]
            if move and move["from"] != move["to"]:
                key = f"{move['from']} to {move['to']}"
                result["tier_moves"][key] = result["tier_moves"].get(key, 0) + 1
        if write:
            # Both statements carry the condition that leaves an applied role
            # alone, for a click that lands after the look at the top of the
            # loop, and the stamp is not made for a role that was applied for.
            conn.execute(
                "UPDATE opportunities SET last_checked_at = ?,"
                " last_check_result = ? WHERE id = ? AND "
                + rc.NOT_APPLIED_SQL,
                (rc.now_iso(), verdict, row["id"]))
            if verdict in ("gone", "closed"):
                cur = conn.execute(
                    "UPDATE opportunities SET closed_at = ?,"
                    " closed_source = 'page_check'"
                    " WHERE id = ? AND closed_at IS NULL AND "
                    + rc.NOT_APPLIED_SQL,
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

    A role marked applied never expires, however long it has been quiet.
    It is waiting on a reply, not on a repost, so the pass leaves its
    whole chain alone.
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
            " AND dismissed_at IS NULL AND expired_at IS NULL"
            " AND " + rc.NOT_APPLIED_SQL):
        seen = rc.latest_liveness(row)
        if row["role_key"]:
            seen = max(seen, newest.get(row["role_key"], ""))
        if seen < cutoff:
            expired.append({"id": row["id"], "title": row["title"],
                            "company": row["company"], "newest": seen})
    if write and expired:
        stamp = rc.now_iso()
        ids = [item["id"] for item in expired]
        # Five hundred ids to a statement, each with the condition that leaves
        # an applied role alone, in case one was applied for after the rows
        # were chosen. The condition holds a subquery that SQLite works out
        # once per statement, so one statement per row would work it out once
        # per row while the write lock is held.
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            conn.execute(
                "UPDATE opportunities SET expired_at = ?"
                " WHERE expired_at IS NULL AND id IN ("
                + ",".join("?" * len(chunk)) + ") AND " + rc.NOT_APPLIED_SQL,
                (stamp, *chunk))
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
    if result["pages_read"]:
        note += (f", {result['pages_read']} pages read, {result['types_set']}"
                 f" types, {result['siblings_set']} siblings, {result['pay_set']}"
                 f" pay")
        if result["tier_moves"]:
            note += ", " + ", ".join(
                f"{n} {k}" for k, n in sorted(result["tier_moves"].items()))
    if result["due_never_read"]:
        note += f", {result['due_never_read']} never read"
    if result.get("expired"):
        note += f", {result['expired']} expired"
    rc.log_run(conn, "check", mode=result["mode"],
               items_in=result["checked"], items_new=result["closed"],
               note=note)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
