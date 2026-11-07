#!/usr/bin/env python
"""Process one job alert email end to end for MedTech Radar.

Contract (build conventions, Workstream 1):

- Input, one email as JSON {subject, from, date, body_text, body_html}.
  Either --b64 <base64 encoded JSON> (the n8n path, avoids shell quoting) or
  raw JSON on stdin.
- Splits the email into opportunities with the extractor prompt (haiku),
  dedupes on url_hash against SQLite BEFORE any scoring call, scores new items
  with the scorer prompt (sonnet, cached system block), writes rows.
- Output, one JSON object on stdout:
  {"processed": N, "new": N, "duplicates": N, "items": [...]}
  When extraction fails after its one retry the object also carries
  "extract_failed": true, a "note", the running "attempts" count for this
  email and "give_up": true once attempts reach three. The radar-inbox
  workflow leaves a failed email unlabelled for a retry, and shelves it
  under the radar-failed label when give_up is set, so a poison message
  costs at most three attempts. A successful extraction clears the count.
- Flags. --mock forces RADAR_MOCK (deterministic mocks from test/mocks.py).
  --db PATH points at a throwaway database for test isolation.

Idempotent. Re-running the same email adds nothing and scores nothing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import enrich_company
import radar_common
import score_item
import tripwire

# The buying-window signal is written by code, not scored by a model, so it
# needs a relevance of its own. 90 sits deliberately above the fast signal
# threshold, which is 75 in radar.yaml, because the doctrine treats the
# first QA, regulatory or software hire at a touched company as the moment
# worth interrupting the day for. Nothing here pushes. The catch-up sweep in
# check_signals.py collects any new unacknowledged signal at or above the
# fast bar within 24 hours of first_seen on the next armed run, so arming
# semantics stay exactly where they were.
BUYING_WINDOW_RELEVANCE = 90

# Step 4 of playbook/announcement-day.md, compressed to one instruction.
BUYING_WINDOW_STEP = (
    "Send the compliance-cost article, or the gap assessment one-pager if "
    "the article is not published yet. The note stays short, and the paid "
    "assessment earns one line at the very end, never earlier."
)


# The rule moved to radar_common in phase two, because it is now the
# natural key of the companies table and three writers need the same
# answer. Re-exported here so callers and tests that knew it by this name
# keep working.
normalise_company = radar_common.normalise_company


def touched_companies(conn) -> set[str]:
    """Every company in the touch log, normalised, as a set.

    Read once per run rather than per advert. The log is small and the
    inbox loop is hot.
    """
    return {normalise_company(r["company"])
            for r in conn.execute("SELECT company FROM touches")
            if normalise_company(r["company"])}


def record_buying_window(conn, company: str, title: str, url: str,
                         h: str, now: str, company_id=None) -> bool:
    """Write the buying-window insight for one advert. True if written.

    One per company, ever. A company hiring three QA people in a week is
    one buying window, not three, and the second advert must not re-open a
    conversation the first already started. The check is on existing
    job-advert signals rather than on a flag somewhere, so it survives a
    database restored from backup.

    Idempotent on reruns through the advert's own url_hash and INSERT OR
    IGNORE, matching how opportunities dedupe.
    """
    key = normalise_company(company)
    if not key:
        return False
    seen = {normalise_company(r["company"]) for r in conn.execute(
        "SELECT company FROM signals WHERE source_id = 'job-advert'")}
    if key in seen:
        return False
    headline = radar_common.sanitise_free_text(
        f"Buying window. {company} is hiring")
    why = radar_common.sanitise_free_text(
        f"They advertised {title or 'a role'}, and the playbook reads the "
        "first QA, regulatory or software hire at a company already touched "
        "as the week the standards stop being abstract.")
    conn.execute(
        "INSERT OR IGNORE INTO signals"
        " (url_hash, first_seen, source_id, company, headline, summary,"
        "  source_url, relevance, why, playbook_step, pushed, status,"
        "  company_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,0,'new',?)",
        (h, now, "job-advert", company, headline, None, url or "",
         BUYING_WINDOW_RELEVANCE, why,
         radar_common.sanitise_free_text(BUYING_WINDOW_STEP),
         company_id))
    return True


def fire_tripwire(conn, company: str, title: str, url: str, h: str,
                  now: str, opp: dict, config: dict, company_id) -> dict:
    """Score a first-hire advert with the signal rubric and store it.

    The signal scorer, not the job scorer, and no fixed relevance. An
    untouched company deserves the rubric's judgement, and a number chosen
    in advance would be the code deciding what the rubric is for.

    Never raises. The advert is already stored and scored as an
    opportunity by the time this runs, and a tripwire that fails must not
    cost the row it was derived from.
    """
    out = {"fired": False, "note": None, "usage": {}}
    try:
        import check_signals

        # A distinct hash, because the same advert is legitimately both an
        # opportunity and a signal and they must not dedupe against each
        # other in the signals table.
        sig_hash = radar_common.url_hash(f"radar-tripwire://{h}")
        if conn.execute("SELECT 1 FROM signals WHERE url_hash = ?",
                        (sig_hash,)).fetchone():
            out["note"] = "already stored"
            return out

        rubric = check_signals.RUBRIC_PATH.read_text(encoding="utf-8")
        system_blocks = [{"type": "text", "text": rubric,
                          "cache_control": {"type": "ephemeral"}}]
        mock_fn = None
        if radar_common.mock_mode_active():
            sys.path.insert(0, str(SCRIPT_DIR.parent / "test"))
            import mocks_signals
            mock_fn = mocks_signals.mock_signal_scorer

        where = str(opp.get("location") or "").strip()
        item = {
            "source_id": tripwire.SOURCE_ID,
            "source_name": "First hire tripwire",
            "url": url or f"radar-tripwire://{h}",
            "headline": f"{company} is hiring for {title}",
            "date": None,
            "text": (f"{company} advertised the role {title}"
                     + (f" in {where}" if where else "")
                     + ". A first quality or regulatory hire is the point a "
                       "medical device company starts needing IEC 62304 and "
                       "ISO 13485 in practice rather than in principle."),
        }
        parsed, usage = check_signals.score_item(item, system_blocks, config,
                                                 mock_fn)
        radar_common.add_usage(out["usage"], usage)
        if parsed is None:
            out["note"] = "signal scorer returned unparseable output"
            return out

        try:
            relevance = max(0, min(100, int(parsed.get("relevance", 0))))
        except (TypeError, ValueError):
            relevance = 0
        conn.execute(
            "INSERT OR IGNORE INTO signals"
            " (url_hash, first_seen, source_id, company, headline, summary,"
            "  source_url, relevance, why, playbook_step, pushed, status,"
            "  company_id, region)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,'new',?,?)",
            # The advert names the company outright, so that is what is
            # stored. The scorer is reading a headline this code wrote and
            # its guess at the company cannot be better than the fact.
            (sig_hash, now, tripwire.SOURCE_ID, company,
             radar_common.sanitise_free_text(
                 str(parsed.get("headline") or item["headline"])),
             item["text"][:500], url or "", relevance,
             radar_common.sanitise_free_text(str(parsed.get("why") or "")),
             radar_common.sanitise_free_text(str(parsed.get("playbook_step") or "")),
             company_id,
             conn.execute("SELECT region FROM companies WHERE id=?",
                          (company_id,)).fetchone()["region"]
             if company_id else None))
        out["fired"] = True
        out["relevance"] = relevance
        return out
    except Exception as err:                       # noqa: BLE001
        out["note"] = f"failed, {type(err).__name__}"
        return out


def detect_source(from_field: str) -> str:
    """Tag the email by its board, built-ins and configured customs alike.

    The registry lives in radar_common, extended through the Jobs page.
    cvlibrary keeps its historical spelling tolerance, and anything
    unrecognised still gets extracted and scored under email-other, the
    tag is routing, never a gate.
    """
    sender = (from_field or "").lower()
    for source in radar_common.load_job_sources():
        if source["sender_contains"] in sender:
            return source["id"]
    if "cvlibrary" in sender:
        return "cvlibrary-alert"
    return "email-other"


def pay_columns(opp: dict, config: dict, floor: float) -> dict:
    """Structured pay for one extracted opportunity, judged in code.

    The extractor reports what the advert said, currency, period and the
    stated amounts. Conversion and banding happen here, deterministically,
    so the same advert always lands in the same band.
    """
    def num(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    currency = str(opp.get("pay_currency") or "").strip().upper()
    period = str(opp.get("pay_period") or "").strip().lower()
    pay_min, pay_max = num(opp.get("pay_min")), num(opp.get("pay_max"))
    day_rate = radar_common.convert_to_day_rate(
        currency, period, pay_min, pay_max, config)
    return {"pay_currency": currency, "pay_period": period,
            "pay_min": pay_min, "pay_max": pay_max, "day_rate": day_rate,
            "rate_band": radar_common.band_for(day_rate, floor)}


def insert_opportunity(conn, h: str, now: str, source: str, opp: dict,
                       scored: dict | None, note: str | None,
                       pay: dict, cv_version: str | None = None,
                       buying_window: int = 0,
                       company_id: int | None = None,
                       region: str | None = None) -> None:
    if scored is None:
        scored = {
            "company": opp.get("company", ""),
            "role_title": opp.get("title", ""),
            "location": opp.get("location", ""),
            "source_url": opp.get("source_url", ""),
            "thread_type": "inbound",
            "cv_match": None, "want_match": None, "combined": None,
            "one_line_why": None, "red_flags": [],
            "suggested_action": None, "act_by": None,
        }
    # A row that could not be scored has no gates and no tier. Nulls are the
    # honest answer, and the page reads a missing tier as needs review.
    conn.execute(
        "INSERT OR IGNORE INTO opportunities"
        " (url_hash, first_seen, source, company, title, location, salary_rate,"
        "  source_url, thread_type, cv_match, want_match, combined, one_line_why,"
        "  red_flags, suggested_action, act_by, status, status_changed_at, notes,"
        "  pay_currency, pay_period, pay_min, pay_max, day_rate, rate_band,"
        "  cv_version, buying_window, company_id, region,"
        "  gate_sector, gate_sector_note, gate_cv, gate_cv_note, gate_location, gate_location_note, gate_rate, gate_rate_note, tier, failed_gates, question_text, filter_reason, rate_stated, rate_value, rate_basis, ir35, location_class)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, now, source,
         scored["company"] or opp.get("company", ""),
         scored["role_title"] or opp.get("title", ""),
         scored["location"] or opp.get("location", ""),
         opp.get("salary_rate", ""),
         scored["source_url"] or opp.get("source_url", ""),
         scored["thread_type"],
         scored["cv_match"], scored["want_match"], scored["combined"],
         scored["one_line_why"], json.dumps(scored["red_flags"]),
         scored["suggested_action"], scored["act_by"],
         "new", now, note,
         pay["pay_currency"], pay["pay_period"], pay["pay_min"],
         pay["pay_max"], pay["day_rate"], pay["rate_band"], cv_version,
         buying_window, company_id, region,
         scored.get("gate_sector"), scored.get("gate_sector_note"), scored.get("gate_cv"), scored.get("gate_cv_note"), scored.get("gate_location"), scored.get("gate_location_note"), scored.get("gate_rate"), scored.get("gate_rate_note"), scored.get("tier"), scored.get("failed_gates"), scored.get("question_text"), scored.get("filter_reason"), scored.get("rate_stated"), scored.get("rate_value"), scored.get("rate_basis"), scored.get("ir35"), scored.get("location_class")))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Process one job alert email into scored opportunities.")
    parser.add_argument("--b64", help="base64 encoded email JSON, else stdin")
    parser.add_argument("--mock", action="store_true",
                        help="force RADAR_MOCK, deterministic offline mocks")
    parser.add_argument("--db", help="database path override for tests")
    args = parser.parse_args(argv)

    if args.mock:
        os.environ["RADAR_MOCK"] = "1"
    radar_common.load_env()
    config = radar_common.load_config()
    # Refuse to run a live pipeline that would score against the fixture CV.
    # This fires before the extraction call, so no tokens are spent either.
    score_item.assert_profile_ready(config)
    # The rate floor fails loudly here, before any token is spent, when the
    # preferences file has lost its machine-readable line.
    floor = radar_common.read_rate_floor(config)
    # Every stored score is stamped with the CV version that shaped it, so
    # a score that predates a CV change is obvious at a glance.
    cv_version = score_item.get_cv_version(config)

    # utf-8-sig and lstrip cope with a BOM from Windows shells and editors.
    raw = (base64.b64decode(args.b64).decode("utf-8-sig") if args.b64
           else sys.stdin.read())
    email = json.loads(raw.lstrip("\ufeff"))

    conn = radar_common.get_db(Path(args.db).resolve() if args.db else None)
    extract_fn, score_fn = score_item.get_mock_fns()

    usage_total: dict = {}
    opps, usage, extract_note = score_item.extract_opportunities(
        email, config, mock_fn=extract_fn)
    radar_common.add_usage(usage_total, usage)

    # Poison-message cap. Count extraction failures per email so the
    # workflow can shelve a hopeless one instead of retrying forever.
    email_key = radar_common.url_hash(
        "radar-email://" + "|".join(str(email.get(k, ""))
                                    for k in ("from", "subject", "date")))
    attempts = 0
    if extract_note:
        conn.execute(
            "INSERT INTO email_attempts (email_hash, attempts, last_attempt,"
            " subject) VALUES (?, 1, ?, ?)"
            " ON CONFLICT(email_hash) DO UPDATE SET"
            " attempts = attempts + 1, last_attempt = excluded.last_attempt",
            (email_key, radar_common.now_iso(),
             str(email.get("subject", ""))[:200]))
        attempts = conn.execute(
            "SELECT attempts FROM email_attempts WHERE email_hash = ?",
            (email_key,)).fetchone()["attempts"]
    else:
        conn.execute("DELETE FROM email_attempts WHERE email_hash = ?",
                     (email_key,))

    source = detect_source(email.get("from", ""))
    now = radar_common.now_iso()
    # The touch log, read once. Every advert is checked against it, so the
    # doctrine's reading of a company survives whatever the job scorer makes
    # of the role itself.
    touched = touched_companies(conn)
    # Enrichment is capped for the whole run, not per item, so one busy
    # email cannot spend the day's budget. Reaching the cap is not an
    # error, the rest wait for backfill_enrich.py or the next run.
    enrich_budget = enrich_company.new_budget(config)
    enrich_usage: dict = {}
    enriched_count = 0
    tripwire_usage: dict = {}
    tripwire_count = 0
    notes_extra: list = []
    system_blocks = None  # built lazily, only when something new needs scoring
    seen_hashes: set[str] = set()
    items, new_count, dup_count = [], 0, 0

    for opp in opps:
        url = (opp.get("source_url") or "").strip()
        key = url or f"radar://{source}/{opp.get('company', '')}/{opp.get('title', '')}"
        h = radar_common.url_hash(key)
        already = h in seen_hashes or conn.execute(
            "SELECT 1 FROM opportunities WHERE url_hash = ?", (h,)).fetchone()
        if already:
            dup_count += 1
            items.append({"company": opp.get("company", ""),
                          "title": opp.get("title", ""),
                          "status": "duplicate"})
            continue
        seen_hashes.add(h)

        if system_blocks is None:
            system_blocks = score_item.build_scorer_system(config)
        scored, s_usage, s_note = score_item.score_opportunity(
            opp, system_blocks, config, mock_fn=score_fn)
        radar_common.add_usage(usage_total, s_usage)

        # The doctrine's reading of this advert, taken before anything is
        # stored and deliberately independent of the score. scored is None
        # when scoring failed, and a failed score must not cost us the
        # buying window, so every field falls back to the extraction.
        company = (str((scored or {}).get("company") or "").strip()
                   or str(opp.get("company", "")).strip())
        role_title = (str((scored or {}).get("role_title") or "").strip()
                      or str(opp.get("title", "")).strip())
        advert_url = (str((scored or {}).get("source_url") or "").strip()
                      or str(opp.get("source_url", "")).strip())
        is_window = normalise_company(company) in touched
        item_tripwire = False

        # The company row is resolved before the item is stored, so every
        # row lands already pointing at the company it belongs to and the
        # backfill only ever has legacy rows to find.
        company_id = radar_common.resolve_company(conn, company, now)
        # First sight of a company earns one description, ever. It runs
        # after the row is resolved and before nothing at all, because a
        # failure here must not cost the advert. enrich_company never
        # raises for that reason.
        if company_id is not None:
            er = enrich_company.enrich_company(
                conn, company_id,
                f"{role_title}. {opp.get('location','')}. {advert_url}",
                config, enrich_budget)
            radar_common.add_usage(enrich_usage, er.get("usage") or {})
            if er.get("enriched"):
                enriched_count += 1

        # The region is mirrored onto the row at store time so grouping the
        # page is a column read. Enrichment may have just filled the
        # country, so this is read after it rather than before.
        region = None
        if company_id is not None:
            co = conn.execute("SELECT country, city FROM companies WHERE id=?",
                              (company_id,)).fetchone()
            region = radar_common.region_for(co["country"], co["city"], config) if co else None

        insert_opportunity(conn, h, now, source, opp, scored, s_note,
                           pay_columns(opp, config, floor), cv_version,
                           buying_window=int(is_window),
                           company_id=company_id, region=region)
        if is_window:
            record_buying_window(conn, company, role_title, advert_url, h, now,
                                 company_id=company_id)
        else:
            # Not a touched company, so no buying window. If this is a first
            # quality or regulatory hire at a medtech firm it is still news
            # about them, and the signal rubric should judge it rather than
            # the job scorer deciding Luke does not want to be a QA manager.
            fire, why_not = tripwire.should_trip(
                conn, company, role_title, opp.get("location"), touched)
            if fire:
                tw = fire_tripwire(conn, company, role_title, advert_url, h,
                                   now, opp, config, company_id)
                radar_common.add_usage(tripwire_usage, tw.get("usage") or {})
                if tw.get("fired"):
                    tripwire_count += 1
                    item_tripwire = True
                elif tw.get("note"):
                    notes_extra.append(f"tripwire for {company}: {tw['note']}")
        new_count += 1
        item = {"company": opp.get("company", ""),
                "title": opp.get("title", ""),
                "status": "new"}
        if scored is not None:
            item.update({
                "company": scored["company"],
                "title": scored["role_title"],
                "cv_match": scored["cv_match"],
                "want_match": scored["want_match"],
                "combined": scored["combined"],
                "one_line_why": scored["one_line_why"],
            })
        if is_window:
            item["buying_window"] = True
        if item_tripwire:
            item["tripwire"] = True
        if s_note:
            item["note"] = s_note
        items.append(item)

    conn.commit()
    mode = "mock" if radar_common.mock_mode_active() else "live"
    note_parts = [source] + ([extract_note] if extract_note else []) + notes_extra
    # Enrichment is its own line in the runs table. Folding its tokens into
    # the inbox row would hide a cost that behaves differently, one pass per
    # company ever rather than one per advert.
    if tripwire_usage or tripwire_count:
        radar_common.log_run(conn, "tripwire", mode=mode,
                             items_in=tripwire_count, items_new=tripwire_count,
                             model=config.get("claude_model_score"),
                             usage=tripwire_usage,
                             note="first QA or regulatory hire, untouched company")
    if enrich_usage or enriched_count:
        radar_common.log_run(conn, "enrich", mode=mode,
                             items_in=enriched_count, items_new=enriched_count,
                             model=config.get("claude_model_extract"),
                             usage=enrich_usage,
                             note=f"first sight, cap {enrich_budget['cap']}")
    radar_common.log_run(conn, "inbox", mode=mode,
                         items_in=len(opps), items_new=new_count,
                         model=config.get("claude_model_score"),
                         usage=usage_total, note=", ".join(note_parts))
    conn.close()

    out = {"processed": len(opps), "new": new_count,
           "duplicates": dup_count, "items": items}
    if extract_note:
        out["extract_failed"] = True
        out["note"] = extract_note
        out["attempts"] = attempts
        out["give_up"] = attempts >= 3
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
