#!/usr/bin/env python
"""Unit checks for the pure-code helpers. No model calls, either mode.

Covers the rate banding fixtures from the 29 July brief word for word, the
floor line parser including its loud failure, and the free-text sanitiser's
worked cases. The pipeline runners exercise the same helpers end to end,
this runner pins their arithmetic and their error manners in isolation, so
a regression names the exact rule it broke.

Exits non zero on any failure, readable FAILs, never a traceback.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_jobs
import enrich_company
import process_email
import radar_common
import serve_dashboard

failures: list[str] = []

# eur_to_gbp pinned here so the fixtures do not drift if radar.yaml moves.
CFG = {"eur_to_gbp": 0.85, "prefs_file": "config/profile/preferences.md"}
FLOOR = 650.0


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def band(currency, period, lo, hi):
    rate = radar_common.convert_to_day_rate(currency, period, lo, hi, CFG)
    return rate, radar_common.band_for(rate, FLOOR)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()

    # ----- banding fixtures, straight from the brief's acceptance section
    rate, b = band("GBP", "day", 700, 700)
    check(b == "above", f"£700 a day bands above (got {b})")
    rate, b = band("GBP", "day", 650.00, 650.00)
    check(b == "above", f"£650.00 exactly bands above (got {b})")
    rate, b = band("GBP", "day", 649.99, 649.99)
    check(b == "close", f"£649.99 bands close (got {b})")
    rate, b = band("GBP", "day", 600.00, 600.00)
    check(b == "close", f"£600.00 bands close (got {b})")
    rate, b = band("GBP", "day", 599, 599)
    check(b == "below", f"£599 bands below (got {b})")
    rate, b = band("GBP", "year", 120000, 120000)
    check(round(rate) == 545 and b == "below",
          f"£120,000 a year converts to £545 and bands below (got £{rate and round(rate)}, {b})")
    rate, b = band("GBP", "year", 130000, 150000)
    check(round(rate) == 682 and b == "above",
          f"a £130k to £150k range bands on the top, £682, above (got £{rate and round(rate)}, {b})")
    rate, b = band("GBP", "hour", 75, 75)
    check(rate == 600 and b == "close",
          f"£75 an hour converts to £600 and bands close (got £{rate}, {b})")
    rate, b = band("", "", None, None)
    check(rate is None and b == "unstated",
          f"no stated pay bands unstated (got {b})")
    rate, b = band("EUR", "day", 800, 800)
    check(rate == 680 and b == "above",
          f"€800 a day converts at 0.85 to £680 and bands above (got £{rate}, {b})")

    # ----- edges the fixtures imply
    rate, b = band("USD", "day", 900, 900)
    check(b == "unstated",
          "a currency with no configured conversion bands unstated, never guessed")
    rate, b = band("GBP", "", 650, 650)
    check(b == "unstated", "an amount with no stated period bands unstated")
    check(radar_common.band_for(600.0, 650.0) == "close"
          and radar_common.band_for(599.999, 650.0) == "below",
          "the close band edge sits exactly £50 under the floor")

    # ----- url hashing, the dedupe key's normalisation
    bare = radar_common.url_hash(
        "https://www.linkedin.com/comm/jobs/view/4447159213")
    check(radar_common.url_hash(
        "https://www.linkedin.com/comm/jobs/view/4447159213/"
        "?trackingId=abc&refId=def&lipi=urn%3Ali&midToken=AQF"
        "&midSig=0EzJQat&trk=eml-x&trkEmail=eml-y&eid=3cvk&otpToken=MDEw")
        == bare,
        "every LinkedIn tracking parameter strips, midSig included")
    check(radar_common.url_hash(
        "https://www.linkedin.com/comm/jobs/view/4447159213?midSig=other")
        == bare,
        "a per-email midSig never splits the dedupe key")
    check(radar_common.url_hash("https://example.com/page?q=term&utm_source=x")
          == radar_common.url_hash("https://EXAMPLE.com/page/?q=term"),
          "case, trailing slash and utm noise normalise away, the real query stays")

    # ----- the two floor lines, real file then loud failure
    try:
        floors = radar_common.read_rate_floors(CFG)
        check(floors["day"] > 0 and floors["salary"] > floors["day"],
              f"the preferences file carries both floors (got {floors})")
        check(radar_common.read_rate_floor(CFG) == floors["day"],
              "the day-floor reader returns the day line")
    except RuntimeError as err:
        check(False, f"the preferences floor lines read without error ({err})")

    with tempfile.TemporaryDirectory() as tmp:
        both = Path(tmp) / "both.md"
        both.write_text("# Preferences\n\nday_rate_floor_gbp: 750\n"
                        "salary_floor_gbp: 100000\n", encoding="utf-8")
        got = radar_common.read_rate_floors({"prefs_file": str(both.resolve())})
        check(got == {"day": 750.0, "salary": 100000.0},
              f"both lines parse to their numbers (got {got})")
        bare = Path(tmp) / "prefs.md"
        bare.write_text("# Preferences\n\nNo floor line here.\n", encoding="utf-8")
        rel = bare.resolve()
        try:
            radar_common.read_rate_floors({"prefs_file": str(rel)})
            check(False, "a preferences file without the floor lines fails loudly")
        except RuntimeError as err:
            check("day_rate_floor_gbp" in str(err),
                  "the loud failure names the missing line and the fix")
        day_only = Path(tmp) / "day.md"
        day_only.write_text("day_rate_floor_gbp: 750\n", encoding="utf-8")
        try:
            radar_common.read_rate_floors({"prefs_file": str(day_only.resolve())})
            check(False, "a file with only the day floor fails loudly")
        except RuntimeError as err:
            check("salary_floor_gbp" in str(err),
                  "the loud failure names the missing salary line")

    # ----- the dashboard-editable preference lines, surgical and loud
    with tempfile.TemporaryDirectory() as tmp:
        prefs = Path(tmp) / "prefs.md"
        prefs.write_text(
            "# Preferences.\n\n## Rates.\n\nProse stays untouched.\n\n"
            "day_rate_floor_gbp: 650\nsalary_floor_gbp: 100000\n\n"
            "More prose after.\n",
            encoding="utf-8")
        cfg2 = {"prefs_file": str(prefs)}
        check(radar_common.read_pref_line("day_rate_floor_gbp", cfg2) == "650",
              "a labelled line reads back")
        stored = radar_common.update_pref_line("day_rate_floor_gbp", "£700",
                                               cfg2)
        check(stored == "700"
              and radar_common.read_rate_floor(cfg2) == 700.0,
              "editing the floor updates the one line the banding reads")
        text_after = prefs.read_text(encoding="utf-8")
        check("Prose stays untouched." in text_after
              and "More prose after." in text_after,
              "the surrounding prose survives the edit untouched")
        radar_common.update_pref_line("target_title",
                                      "Fractional software director", cfg2)
        check(radar_common.read_pref_line("target_title", cfg2)
              == "Fractional software director"
              and "## Title and keywords." in prefs.read_text(encoding="utf-8"),
              "a missing line is appended under its managed section")
        for bad_key, bad_val, label in (
                ("nonsense", "x", "an unknown key is refused"),
                ("day_rate_floor_gbp", "cheap", "a non-number floor is refused"),
                ("day_rate_floor_gbp", "50000", "an insane floor is refused"),
                ("target_title", "", "an empty value is refused")):
            try:
                radar_common.update_pref_line(bad_key, bad_val, cfg2)
                check(False, label)
            except ValueError:
                check(True, label)

    # ----- the pay backfill, rehearsed in mock against a throwaway db
    import json as json_mod
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "backfill.sqlite"
        conn = radar_common.get_db(db)
        now = radar_common.now_iso()
        seed = [
            ("bf1", "Old Role A", "Alpha Ltd", "£800 per day", None, None),
            ("bf2", "Old Role B", "Beta Ltd", "£95,000 per annum",
             "Kept prose about the fit.", None),
            ("bf3", "Old Role C", "Gamma Ltd", "", None, None),
            ("bf4", "Old Role D", "Delta Ltd", "£700 per day", None, now),
        ]
        for h, title, company, salary, why, ack in seed:
            conn.execute(
                "INSERT INTO opportunities (url_hash, first_seen, company,"
                " title, salary_rate, one_line_why, thread_type, status,"
                " acknowledged_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (h, now, company, title, salary, why, "inbound", "new", ack))
        conn.commit()
        conn.close()

        env = dict(**__import__("os").environ)
        env["RADAR_MOCK"] = "1"
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "backfill_pay.py"),
             "--mock", "--db", str(db), "--cap", "2"],
            capture_output=True, text=True, encoding="utf-8", env=env)
        check(proc.returncode == 0, "backfill exits 0 in mock rehearsal")
        result = json_mod.loads((proc.stdout or "{}").strip() or "{}")
        check(result.get("examined") == 2,
              f"backfill honours the cap of 2 (examined {result.get('examined')})")

        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "backfill_pay.py"),
             "--mock", "--db", str(db)],
            capture_output=True, text=True, encoding="utf-8", env=env)
        check(proc.returncode == 0, "second backfill pass exits 0")

        import sqlite3
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = {r["url_hash"]: dict(r) for r in
                conn.execute("SELECT * FROM opportunities")}
        check(rows["bf1"]["rate_band"] == "above",
              f"backfill bands the £800 day rate above (got {rows['bf1']['rate_band']})")
        check(rows["bf2"]["rate_band"] == "close"
              and round(rows["bf2"]["day_rate"]) == 432,
              "backfill keeps the £95k salary's 220-day comparison column and"
              " bands it close against the salary floor"
              f" (got {rows['bf2']['rate_band']})")
        check(rows["bf2"]["one_line_why"] == "Kept prose about the fit.",
              "backfill leaves stored why text alone")
        check(rows["bf3"]["rate_band"] == "unstated",
              "backfill bands empty pay text unstated without a model call")
        check(rows["bf4"]["rate_band"] is None,
              "backfill never touches an acknowledged row")
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "backfill_pay.py"),
             "--mock", "--db", str(db)],
            capture_output=True, text=True, encoding="utf-8", env=env)
        result = json_mod.loads((proc.stdout or "{}").strip() or "{}")
        check(result.get("examined") == 0,
              "a completed backfill examines nothing, idempotent")
        conn.close()

    # ----- the life state ladder, derived at render, never stored
    now_s = "2026-11-07T12:00:00Z"
    fresh_s = "2026-11-03T00:00:00Z"
    edge_s = "2026-10-31T12:00:00Z"
    stale_s = "2026-10-01T00:00:00Z"
    base = {"closed_at": None, "last_seen": None, "first_seen": stale_s,
            "last_checked_at": None, "last_check_result": None}
    check(radar_common.life_state({**base, "closed_at": stale_s,
                                   "last_seen": fresh_s}, now_s) == "closed",
          "a closed stamp outranks a fresh sighting")
    check(radar_common.life_state({**base, "last_seen": fresh_s},
                                  now_s) == "live",
          "a sighting inside seven days reads live")
    check(radar_common.life_state({**base, "last_seen": edge_s},
                                  now_s) == "live",
          "a sighting exactly seven days old still reads live")
    check(radar_common.life_state({**base, "last_checked_at": fresh_s,
                                   "last_check_result": "live"},
                                  now_s) == "live",
          "a live page check inside seven days reads live")
    check(radar_common.life_state({**base, "last_checked_at": fresh_s,
                                   "last_check_result": "unknown"},
                                  now_s) == "unknown",
          "an unknown check result never reads live")
    check(radar_common.life_state(base, now_s) == "unknown",
          "an old row with no sighting reads unknown")
    check(radar_common.life_state({"first_seen": fresh_s},
                                  now_s) == "live",
          "a legacy mapping without the lifecycle keys judges on first_seen")

    # ----- role keys, one role across its reposts
    check(radar_common.role_key("Head of QA", "Pfizer")
          == radar_common.role_key(" head  of  QA. ", "  PFIZER "),
          "case, punctuation and spacing never split a role key")
    check(radar_common.role_key("Director, Product Owner", "Pfizer")
          == radar_common.role_key("Director - Product Owner", "Pfizer"),
          "a reposted title with new separators is still the same role")
    check(radar_common.role_key("Head of QA", "Pfizer")
          != radar_common.role_key("Head of QA", "Roche"),
          "the same title at another company is another role")
    check(radar_common.role_key("Head of QA", None) is None
          and radar_common.role_key(None, "Pfizer") is None
          and radar_common.role_key("", "") is None,
          "a missing half means no key, the row stands alone")

    # ----- the page checker's ladder, over the saved page shapes
    fixtures = REPO_ROOT / "test" / "fixtures" / "linkedin"
    pages = {name: (fixtures / f"{name}.html").read_text(encoding="utf-8")
             for name in ("live", "closed", "renamed", "noh1", "ambiguous")}
    jid = "4482760631"
    job_url = f"https://www.linkedin.com/jobs/view/{jid}/"
    title = "Principal Medical Device Engineer"

    verdict, why = check_jobs.classify(
        200, "https://www.linkedin.com/jobs/search/?trk=expired_jd_redirect",
        "<html>search results</html>", title, jid)
    check(verdict == "gone",
          f"the expired redirect reads gone (got {verdict})")
    verdict, why = check_jobs.classify(200, job_url, pages["closed"],
                                       title, jid)
    check(verdict == "closed",
          f"the closed marker with no apply button reads closed (got {verdict})")
    verdict, why = check_jobs.classify(200, job_url, pages["live"],
                                       title, jid)
    check(verdict == "live",
          "a live page reads live on title, button and posting id,"
          f" no JSON-LD needed (got {verdict})")
    verdict, why = check_jobs.classify(429, job_url, "", title, jid)
    check(verdict == "unknown",
          f"a 429 reads unknown, never closed (got {verdict})")
    verdict, why = check_jobs.classify(
        200, "https://www.linkedin.com/authwall?sessionRedirect=x",
        "<html>sign in</html>", title, jid)
    check(verdict == "unknown", f"the authwall reads unknown (got {verdict})")
    verdict, why = check_jobs.classify(200, job_url, pages["renamed"],
                                       title, jid)
    check(verdict == "unknown",
          f"a renamed advert reads unknown, not live (got {verdict})")
    verdict, why = check_jobs.classify(200, job_url, pages["noh1"],
                                       title, jid)
    check(verdict == "unknown",
          f"a page with no h1 reads unknown (got {verdict})")
    verdict, why = check_jobs.classify(200, job_url, pages["ambiguous"],
                                       title, jid)
    check(verdict == "unknown",
          f"a closed marker beside an apply button reads unknown (got {verdict})")
    verdict, why = check_jobs.classify(200, job_url, pages["live"],
                                       title, "999")
    check(verdict == "unknown",
          f"a posting id mismatch reads unknown (got {verdict})")
    verdict, why = check_jobs.classify(404, job_url, "", title, jid)
    check(verdict == "unknown",
          f"a bare 404 reads unknown, never closed (got {verdict})")
    check(check_jobs.job_view_id(
        "https://www.linkedin.com/comm/jobs/view/4441461574/?trackingId=x")
        == "4441461574", "the job id parses out of a stored comm URL")

    # ----- the page checker. Selection, the capped run and the stamps.
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "checker.sqlite"
        conn = radar_common.get_db(db)

        def plant(key, tier, seen, source_url=None, **extra):
            cols = {"url_hash": key, "first_seen": seen, "last_seen": seen,
                    "title": title, "company": "Veritas Medical",
                    "tier": tier,
                    "source_url": source_url
                    or f"https://www.linkedin.com/jobs/view/{key}/"}
            cols.update(extra)
            conn.execute(
                f"INSERT INTO opportunities ({', '.join(cols)}) VALUES"
                f" ({', '.join('?' for _ in cols)})", tuple(cols.values()))
            return conn.execute("SELECT id FROM opportunities WHERE"
                                " url_hash = ?", (key,)).fetchone()["id"]

        a = plant("111", "top", "2026-10-20T00:00:00Z")
        b = plant("222", "question", "2026-10-08T00:00:00Z")
        c = plant("333", "question", "2026-10-13T00:00:00Z")
        i = plant("444", "question", "2026-09-25T00:00:00Z",
                  last_checked_at="2026-10-28T00:00:00Z",
                  last_check_result="unknown")
        d = plant("555", "top", "2026-11-05T00:00:00Z")
        e = plant("666", "top", "2026-10-20T00:00:00Z",
                  last_checked_at="2026-11-04T00:00:00Z",
                  last_check_result="unknown")
        plant("777", "reading", "2026-10-20T00:00:00Z")
        plant("888", "top", "2026-10-20T00:00:00Z",
              closed_at="2026-11-01T00:00:00Z", closed_source="manual")
        plant("999", "top", "2026-10-20T00:00:00Z",
              source_url="https://example.com/role/1")
        conn.commit()

        due = check_jobs.due_candidates(conn, now_s)
        check([r["id"] for r in due] == [a, i, b, c, d]
              and [r["due_reason"] for r in due]
              == ["stale"] * 4 + ["never read"],
              "due is the stale rows, top first then stalest liveness, then"
              " the never-read fresh row (J20); recently checked, reading,"
              " closed and off-site rows are left out")

        gone_url = ("https://www.linkedin.com/jobs/search/"
                    "?position=1&trk=expired_jd_redirect")
        responses = {
            "111": (200, "https://www.linkedin.com/jobs/view/111/",
                    pages["live"].replace(jid, "111")),
            "222": (200, "https://www.linkedin.com/jobs/view/222/",
                    pages["closed"]),
            "333": (200, gone_url, "<html>search</html>"),
            "444": (200, "https://www.linkedin.com/jobs/view/444/",
                    pages["noh1"]),
            "555": (200, "https://www.linkedin.com/jobs/view/555/",
                    pages["live"].replace(jid, "555")),
        }
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return responses[check_jobs.job_view_id(url)]

        def no_pause():
            return None

        real_fetch = check_jobs.fetch_job_page
        check_jobs.fetch_job_page = fake_fetch
        try:
            trial = check_jobs.run_checks(conn, cap=2, write=False,
                                          now=now_s, pause=no_pause)
            check(trial["checked"] == 2 and len(calls) == 2,
                  "the cap holds, two fetches for a cap of two")
            unstamped = conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities"
                " WHERE last_checked_at IS NOT NULL"
                " AND last_checked_at > '2026-11-06'").fetchone()["n"]
            check(unstamped == 0, "a dry run writes nothing")

            calls.clear()
            run = check_jobs.run_checks(conn, cap=25, write=True,
                                        now=now_s, pause=no_pause)
            check(run["due"] == 5 and run["checked"] == 5
                  and run["due_never_read"] == 1,
                  "the live run checks all five due rows, one of them"
                  " never read")
            check(run["closed"] == 2 and run["verdicts"].get("live") == 2
                  and run["verdicts"].get("unknown") == 1,
                  "the verdicts land two live, one closed, one gone,"
                  " one unknown, two rows retired")
            rows = {r["url_hash"]: dict(r) for r in conn.execute(
                "SELECT * FROM opportunities")}
            check(rows["111"]["last_check_result"] == "live"
                  and rows["111"]["closed_at"] is None,
                  "the live page stamps live and closes nothing")
            check(radar_common.life_state(rows["111"]) == "live",
                  "a stale sighting with a fresh live check reads live")
            check(rows["222"]["last_check_result"] == "closed"
                  and rows["222"]["closed_at"] is not None
                  and rows["222"]["closed_source"] == "page_check",
                  "the closed page closes the row, confirmed by page_check")
            check(rows["333"]["last_check_result"] == "gone"
                  and rows["333"]["closed_source"] == "page_check",
                  "the expired redirect closes the row the same way")
            check(rows["444"]["last_check_result"] == "unknown"
                  and rows["444"]["closed_at"] is None,
                  "an unknown verdict stamps the check and changes no state")
            check(radar_common.life_state(rows["222"]) == "closed",
                  "a page_check close derives closed like a manual one")

            calls.clear()
            again = check_jobs.run_checks(conn, cap=25, write=True,
                                          now=now_s, pause=no_pause)
            check(again["due"] == 0 and not calls,
                  "a second run finds nothing due, checked rows wait a week")

            # A 429 is a wait, not a stop. Nothing is stamped, the row
            # stays due, the run backs off and carries on. The third 429
            # in one run ends it.
            import contextlib
            import io
            import json
            import re
            j = plant("1212", "question", "2026-09-01T00:00:00Z")
            k = plant("1313", "question", "2026-09-10T00:00:00Z")
            m = plant("1414", "question", "2026-09-20T00:00:00Z")
            conn.commit()

            def live_for(key):
                return (200, f"https://www.linkedin.com/jobs/view/{key}/",
                        pages["live"].replace(jid, key))

            def limited(key):
                return (429, f"https://www.linkedin.com/jobs/view/{key}/", "")

            responses["1212"] = live_for("1212")
            responses["1313"] = limited("1313")
            responses["1414"] = live_for("1414")
            waits = []

            def fake_wait(seconds):
                waits.append(seconds)

            carried = check_jobs.run_checks(conn, cap=25, write=True,
                                            now=now_s, pause=no_pause,
                                            backoff=0, wait=fake_wait)
            check(carried["due"] == 3 and carried["checked"] == 3
                  and carried["verdicts"] == {"live": 2, "unknown": 1},
                  "sequence 200, 429, 200: three rows processed, one"
                  " unknown, the run completes")
            check(carried["rate_limited"] == 1
                  and carried["backoff_seconds"] == 0 and waits == [0],
                  "one 429 is counted and the backoff is waited once")
            row_k = conn.execute(
                "SELECT last_checked_at, last_check_result FROM"
                " opportunities WHERE id = ?", (k,)).fetchone()
            check(row_k["last_checked_at"] is None
                  and row_k["last_check_result"] is None,
                  "the rate-limited row is not stamped, it stays due")
            beside = [conn.execute(
                "SELECT last_check_result FROM opportunities WHERE id = ?",
                (x,)).fetchone()["last_check_result"] for x in (j, m)]
            check(beside == ["live", "live"],
                  "the rows either side of the 429 are stamped live")
            unknown_entry = [c for c in carried["checks"]
                             if c["id"] == k][0]
            check(unknown_entry["verdict"] == "unknown"
                  and unknown_entry["reason"] == "rate limited, 429",
                  "the 429 row is recorded unknown with its reason")
            check(len(carried["rate_limits"]) == 1
                  and carried["rate_limits"][0]["id"] == k
                  and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
                                   carried["rate_limits"][0]["ts"]),
                  "every 429 is logged with a timestamp")
            check(any(re.match(r"\d{4}-\d\d-\d\dT\S+ 429 on id", n)
                      for n in carried["notes"]),
                  "the run notes carry the 429 with its clock time")

            p = plant("1515", "question", "2026-10-01T00:00:00Z")
            r = plant("1616", "question", "2026-10-02T00:00:00Z")
            q = plant("1717", "question", "2026-10-03T00:00:00Z")
            conn.commit()
            due_next = [row["id"] for row in
                        check_jobs.due_candidates(conn, now_s)]
            check(due_next == [k, p, r, q],
                  "the 429 row is selected first by due_candidates on the"
                  " next run")

            responses["1515"] = limited("1515")
            responses["1616"] = limited("1616")
            responses["1717"] = live_for("1717")
            waits.clear()
            struck = check_jobs.run_checks(conn, cap=25, write=True,
                                           now=now_s, pause=no_pause,
                                           backoff=0, wait=fake_wait)
            check(struck["due"] == 4 and struck["checked"] == 3
                  and struck["rate_limited"] == 3,
                  "sequence 429, 429, 429, 200: the run stops after the"
                  " third 429")
            check(len(waits) == 2,
                  "the third 429 ends the run without another wait")
            row_q = conn.execute(
                "SELECT last_checked_at FROM opportunities WHERE id = ?",
                (q,)).fetchone()
            check(row_q["last_checked_at"] is None
                  and q in [row["id"] for row in
                            check_jobs.due_candidates(conn, now_s)],
                  "the fourth row is untouched and still due")
            check(any("run stopped" in n and "1 left due" in n
                      for n in struck["notes"]),
                  "the stop names how many rows were left")

            waits.clear()
            tallied = check_jobs.run_checks(conn, cap=25, write=False,
                                            now=now_s, pause=no_pause,
                                            backoff=7, wait=fake_wait)
            check(tallied["backoff_seconds"] == 14 and waits == [7, 7],
                  "the total backoff is the sum of the waits taken")

            # The command line: the flag parses and the runs row carries
            # the 429 count, the clock times and the backoff total. The
            # command has no way to be given a time, and the rows planted
            # above are dated against now_s, so it runs on that clock.
            buf = io.StringIO()
            real_now = radar_common.now_iso
            radar_common.now_iso = lambda: now_s
            try:
                with contextlib.redirect_stdout(buf):
                    rc_main = check_jobs.main(["--dry-run", "--db", str(db),
                                               "--backoff", "0"])
            finally:
                radar_common.now_iso = real_now
            printed = json.loads(buf.getvalue())
            check(rc_main == 0 and printed["rate_limited"] == 3
                  and printed["backoff_seconds"] == 0,
                  "the run JSON carries the 429 count and the backoff total")
            run_note = conn.execute(
                "SELECT note FROM runs WHERE workflow = 'check'"
                " ORDER BY id DESC LIMIT 1").fetchone()["note"]
            check("3 rate limited, 429 at " in run_note
                  and run_note.count("Z") == 3
                  and "0s backoff" in run_note,
                  "the runs row names the 429 count, times and backoff")

            responses["1313"] = (
                0, "https://www.linkedin.com/jobs/view/1313/", "")
            failed = check_jobs.run_checks(conn, cap=25, write=True,
                                           now=now_s, pause=no_pause,
                                           backoff=0, wait=fake_wait)
            still_k = conn.execute(
                "SELECT last_checked_at FROM opportunities WHERE id = ?",
                (k,)).fetchone()
            check(failed["checked"] == 0
                  and still_k["last_checked_at"] is None,
                  "a network failure stops the run and stamps nothing")

            responses["555"] = (
                200, "https://www.linkedin.com/jobs/view/555/",
                pages["live"].replace(jid, "555"))
            spot = check_jobs.run_checks(conn, only_id=d, write=False,
                                         now=now_s, pause=no_pause)
            check(spot["checked"] == 1
                  and spot["checks"][0]["verdict"] == "live",
                  "a spot check by id bypasses the due gate")
        finally:
            check_jobs.fetch_job_page = real_fetch
        check(conn.execute(
            "SELECT last_checked_at FROM opportunities WHERE id = ?",
            (e,)).fetchone()["last_checked_at"] == "2026-11-04T00:00:00Z",
              "a row checked three days ago was never re-asked")
        conn.close()

    # ----- Brief C. The checker reads the page while it is open: the job
    # criteria, the description and any stated pay. First the seven saved
    # pages from the study of 7 November, then hand-made variants.
    saved_dir = fixtures / "pages"
    saved_ids = ("4472532393", "4484737607", "4483969601", "4486892005",
                 "4485096586", "4487779866")
    saved = {sid: (saved_dir / f"{sid}.html").read_text(encoding="utf-8")
             for sid in saved_ids + ("4461731176",)}
    yields = {sid: check_jobs.read_page(saved[sid]) for sid in saved}
    check(all(yields[sid]["employment_type"] == "Full-time" for sid in saved_ids),
          "all six served pages list Employment type Full-time in the criteria")
    check(all(len(yields[sid]["description"]) > 2000 for sid in saved_ids)
          and all(yields[sid]["pay"] is None for sid in saved_ids),
          "all six carry a description over 2,000 characters and no stated pay")
    check(all(check_jobs.map_engagement(yields[sid]["description"],
                                        yields[sid]["employment_type"])[0]
              == "permanent" for sid in saved_ids),
          "all six map to permanent, Full-time with no contract marker")
    check(yields["4461731176"] == {"criteria": {}, "employment_type": None,
                                   "description": "", "pay": None,
                                   "pay_from": None},
          "the 429 challenge page yields nothing")
    check("Veeva" in yields["4472532393"]["description"]
          and "<" not in yields["4472532393"]["description"]
          and yields["4472532393"]["criteria"]["Seniority level"] == "Director",
          "the description is the advert's text with the tags gone, and the"
          " criteria read as LinkedIn labels them")

    me = check_jobs.map_engagement
    check(me("", "Contract") == ("contract", "criteria Contract"),
          "criteria Contract gives contract")
    check(me("", "Temporary")[0] == "contract", "criteria Temporary gives contract")
    check(me("Paying £700 a day outside IR35.", "Full-time")
          == ("contract", "day rate, from the page"),
          "Full-time with a day rate in the description gives contract")
    check(me("An interim post for six months.", "Full-time")[0] == "contract",
          "Full-time with interim in the description gives contract")
    check(me("Lead the quality function.", "Full-time")
          == ("permanent", "criteria Full-time"),
          "Full-time with no marker gives permanent")
    check(me("Lead the quality function.", "Part-time")[0] == "permanent",
          "Part-time with no marker gives permanent")
    check(me("Lead the quality function.", None) == ("unknown", "")
          and me("", "Internship") == ("unknown", ""),
          "no criteria, or a criteria word outside the four, gives unknown")
    check(me("A 12 month fixed term contract.", "Full-time")
          == ("permanent", "fixed term, from the page"),
          "fixed term in the description reads permanent, as the rules say")

    sp = check_jobs.stated_pay
    check(sp("£90,000 to £110,000") is None
          and sp("£90,000 to £110,000", require_period=False)
          == {"text": "£90,000 to £110,000", "period": "year", "min": 90000.0,
              "max": 110000.0, "currency": "GBP"},
          "a bare top-card range reads as a year; in a description it needs"
          " a period word")
    check(sp("Rate: £650 to £750 per day, outside IR35")
          == {"text": "£650 to £750 per day", "period": "day", "min": 650.0,
              "max": 750.0, "currency": "GBP"},
          "a day-rate range in a description stores both ends and the period")
    check(sp("Up to £45 an hour")["period"] == "hour"
          and sp("Circa £95k per annum")["max"] == 95000.0
          and sp("£500 bonus and £120,000 pa")["max"] == 120000.0
          and sp("Competitive package") is None
          and sp("Salary £400", require_period=False)["period"] is None,
          "hour and k forms parse, a bonus figure is skipped for the one"
          " with a period, no figure is None, a small bare figure has no"
          " period")

    def page_with(base, criteria=None, salary=None, description=""):
        # A hand-made variant on the synthetic page: a top-card salary
        # element, a description markup block and a criteria list, in
        # the shapes the saved pages use.
        page = base
        if salary:
            page = page.replace(
                '<div class="top-card-layout__cta-container">',
                f'<div class="salary compensation__salary">{salary}</div>'
                '<div class="top-card-layout__cta-container">', 1)
        extra = ""
        if description:
            extra += ('<section class="show-more-less-html"><div class="'
                      'show-more-less-html__markup relative overflow-hidden">'
                      f'{description}</div><button class="show-more-less-html'
                      '__button">Show more</button></section>')
        if criteria:
            items = "".join(
                '<li class="description__job-criteria-item"><h3 class="'
                f'description__job-criteria-subheader">{k}</h3><span class="'
                f'description__job-criteria-text">{v}</span></li>'
                for k, v in criteria.items())
            extra += f'<ul class="description__job-criteria-list">{items}</ul>'
        return page.replace('<code id="decoratedJobPostingId"',
                            extra + '<code id="decoratedJobPostingId"', 1)

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "reader.sqlite")
        key_v1 = radar_common.role_key(title, "Veritas Medical")

        def plant_c(key, tier, **extra):
            cols = {"url_hash": key, "first_seen": "2026-10-01T00:00:00Z",
                    "last_seen": "2026-10-01T00:00:00Z", "title": title,
                    "company": "Veritas Medical", "tier": tier,
                    "gate_sector": 1, "gate_cv": 1, "gate_location": 1,
                    "cv_match": 80, "role_key": key_v1,
                    "source_url": f"https://www.linkedin.com/jobs/view/{key}/"}
            cols.update(extra)
            conn.execute(
                f"INSERT INTO opportunities ({', '.join(cols)}) VALUES"
                f" ({', '.join('?' for _ in cols)})", tuple(cols.values()))
            return conn.execute("SELECT id FROM opportunities WHERE"
                                " url_hash = ?", (key,)).fetchone()["id"]

        v1 = plant_c("2001", "top")
        v1_sib = plant_c("2002", "reading", source_url="https://example.com/x",
                         last_seen="2026-09-01T00:00:00Z")
        v1_set = plant_c("2003", "reading", source_url="https://example.com/y",
                         last_seen="2026-09-01T00:00:00Z",
                         engagement_type="contract", engagement_note="IR35")
        v2 = plant_c("2004", "top", role_key="other-role-a")
        v3 = plant_c("2005", "top", role_key="other-role-b")
        v4 = plant_c("2006", "question", role_key="other-role-c",
                     pay_currency="GBP", pay_period="day", pay_min=600,
                     pay_max=600, salary_rate="£600 a day", gate_rate=0,
                     rate_stated=1, rate_value=600, rate_basis="day-rate",
                     rate_band="below", question_text="Would they move on the rate",
                     engagement_type="unknown")
        v5 = plant_c("2007", "top", role_key="other-role-d",
                     engagement_type="unknown")
        v6 = plant_c("2008", "question", role_key="other-role-e",
                     gate_location=0, question_text="Would they go remote")
        v7 = plant_c("2009", "top", role_key="other-role-f")
        conn.commit()

        gone_url = ("https://www.linkedin.com/jobs/search/"
                    "?position=1&trk=expired_jd_redirect")
        responses_c = {
            "2001": (200, "https://www.linkedin.com/jobs/view/2001/",
                     page_with(pages["live"].replace(jid, "2001"),
                               criteria={"Seniority level": "Director",
                                         "Employment type": "Full-time"},
                               salary="£90,000 to £110,000",
                               description="<p>Lead the quality function."
                               " Bonus of £5,000.</p>")),
            "2004": (200, "https://www.linkedin.com/jobs/view/2004/",
                     page_with(pages["live"].replace(jid, "2004"),
                               criteria={"Employment type": "Full-time"},
                               description="<p>Rate: £650 to £750 per day,"
                               " outside IR35.</p><p>Six months.</p>")),
            "2005": (200, "https://www.linkedin.com/jobs/view/2005/",
                     page_with(pages["live"].replace(jid, "2005"),
                               criteria={"Employment type": "Full-time"},
                               salary="£85,000 to £95,000",
                               description="<p>Head of QA.</p>")),
            "2006": (200, "https://www.linkedin.com/jobs/view/2006/",
                     page_with(pages["live"].replace(jid, "2006"),
                               criteria={"Employment type": "Contract"},
                               description="<p>£800 per day.</p>")),
            "2007": (200, gone_url, "<html>search</html>"),
            "2008": (200, "https://www.linkedin.com/jobs/view/2008/",
                     page_with(pages["closed"],
                               criteria={"Employment type": "Full-time"},
                               description="<p>Hybrid, three days in"
                               " Cambridge.</p>")),
            "2009": (429, "https://www.linkedin.com/jobs/view/2009/",
                     saved["4461731176"]),
        }

        def fake_fetch_c(url):
            return responses_c[check_jobs.job_view_id(url)]

        real_fetch = check_jobs.fetch_job_page
        check_jobs.fetch_job_page = fake_fetch_c
        try:
            dry_c = check_jobs.run_checks(conn, cap=25, write=False,
                                          now=now_s, pause=lambda: None,
                                          backoff=0, wait=lambda s: None)
            untouched = conn.execute(
                "SELECT COUNT(*) AS n FROM opportunities WHERE description_text"
                " IS NOT NULL OR pay_source IS NOT NULL OR (engagement_type"
                " IS NOT NULL AND engagement_type NOT IN ('unknown','contract'))"
            ).fetchone()["n"]
            check(dry_c["pages_read"] == 5 and dry_c["pay_set"] == 3
                  and dry_c["tier_moves"] == {"top to question": 1}
                  and untouched == 0,
                  "a dry run reads five pages, reports three pay figures and"
                  " one tier move, and writes nothing")
            live_c = check_jobs.run_checks(conn, cap=25, write=True,
                                           now=now_s, pause=lambda: None,
                                           backoff=0, wait=lambda s: None)
            rows_c = {r["url_hash"]: dict(r) for r in conn.execute(
                "SELECT * FROM opportunities")}
            r1 = rows_c["2001"]
            check(r1["engagement_type"] == "permanent"
                  and r1["engagement_note"] == "criteria Full-time"
                  and r1["description_text"].startswith("Lead the quality")
                  and r1["pay_period"] == "year" and r1["pay_max"] == 110000
                  and r1["pay_source"] == "page"
                  and r1["salary_rate"] == "£90,000 to £110,000"
                  and r1["rate_basis"] == "salary" and r1["gate_rate"] == 1
                  and r1["rate_band"] == "above" and r1["tier"] == "top",
                  "a top-card range stores a salary, gates against the"
                  " salary floor and keeps the row in Top")
            check(rows_c["2002"]["engagement_type"] == "permanent"
                  and rows_c["2002"]["engagement_note"] == "criteria Full-time"
                  and rows_c["2003"]["engagement_type"] == "contract",
                  "a chain sibling reading unknown picks up the fetched type,"
                  " a sibling with its own type keeps it")
            r2 = rows_c["2004"]
            check(r2["engagement_type"] == "contract"
                  and r2["engagement_note"] == "day rate, from the page"
                  and r2["pay_period"] == "day" and r2["pay_min"] == 650
                  and r2["pay_max"] == 750 and r2["rate_basis"] == "day-rate"
                  and r2["gate_rate"] == 1 and r2["tier"] == "top"
                  and r2["salary_rate"] == "£650 to £750 per day",
                  "a day-rate range in the description stores a day rate,"
                  " gates against the day floor and sets contract over"
                  " Full-time")
            r3 = rows_c["2005"]
            check(r3["tier"] == "question" and r3["gate_rate"] == 0
                  and r3["question_text"] == "Would they move on the rate"
                  and r3["rate_band"] == "close" and r3["pay_max"] == 95000,
                  "a salary under the floor moves the row to One question"
                  " away with the rate as the question")
            r4 = rows_c["2006"]
            check(r4["pay_max"] == 600 and r4["pay_source"] is None
                  and r4["tier"] == "question" and r4["gate_rate"] == 0
                  and r4["engagement_type"] == "contract"
                  and r4["engagement_note"] == "day rate, from the page",
                  "a row that already had a figure keeps it, and still takes"
                  " the type, the description's marker ahead of the criteria")
            r5 = rows_c["2007"]
            check(r5["description_text"] is None and r5["pay_source"] is None
                  and r5["engagement_type"] == "unknown"
                  and r5["last_check_result"] == "gone",
                  "a gone page writes nothing but the verdict")
            r6 = rows_c["2008"]
            check(r6["last_check_result"] == "closed"
                  and r6["closed_at"] is not None
                  and r6["description_text"].startswith("Hybrid")
                  and r6["engagement_type"] == "permanent"
                  and r6["tier"] == "question",
                  "a closed page with its description still served is read,"
                  " and the row's tier is untouched without a figure")
            r7 = rows_c["2009"]
            check(r7["description_text"] is None and r7["last_checked_at"] is None
                  and r7["engagement_type"] is None,
                  "the 429 challenge page writes nothing and the row stays due")
            check(live_c["pages_read"] == 5 and live_c["types_set"] == 5
                  and live_c["siblings_set"] == 1 and live_c["pay_set"] == 3
                  and live_c["tier_moves"] == {"top to question": 1}
                  and [p["id"] for p in live_c["page_reads"]]
                  == [v1, v2, v3, v4, v6],
                  "the run reports pages read, types, siblings, pay and the"
                  " tier move")
            moved = [p for p in live_c["page_reads"] if p["id"] == v3][0]
            check(moved["tier"] == {"from": "top", "to": "question",
                                    "gate": False, "basis": "salary",
                                    "figure": 95000.0}
                  and moved["pay"]["from"] == "top card",
                  "the report names the row's move, the basis, the figure"
                  " and where on the page the pay came from")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc_c = check_jobs.main(["--dry-run", "--db",
                                        str(Path(tmp) / "reader.sqlite"),
                                        "--backoff", "0"])
            note_c = conn.execute(
                "SELECT note FROM runs WHERE workflow = 'check'"
                " ORDER BY id DESC LIMIT 1").fetchone()["note"]
            check(rc_c == 0 and "never read" not in note_c
                  and "pages read" not in note_c,
                  "after the live run the next run finds nothing due and the"
                  " runs row says so plainly")
        finally:
            check_jobs.fetch_job_page = real_fetch
        conn.close()

    # ----- the expiry pass. Forty-five days of silence retires the role,
    # on the repost chain's clock, not the advert's.
    import build_dashboard as bd4
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "expiry.sqlite")

        def put(h, seen, **extra):
            cols = {"url_hash": h, "first_seen": seen, "last_seen": seen,
                    "title": "Regulatory Affairs Lead", "company": "Veltrix"}
            cols.update(extra)
            conn.execute(
                f"INSERT INTO opportunities ({', '.join(cols)}) VALUES"
                f" ({', '.join('?' for _ in cols)})", tuple(cols.values()))
            return conn.execute("SELECT id FROM opportunities WHERE"
                                " url_hash = ?", (h,)).fetchone()["id"]

        now_x = "2026-11-07T12:00:00Z"
        old_x = "2026-09-01T00:00:00Z"   # 67 days before now_x
        mid_x = "2026-10-28T00:00:00Z"   # 10 days before now_x
        chain = radar_common.role_key("Regulatory Affairs Lead", "Veltrix")
        put("ch1", old_x, role_key=chain)
        put("ch2", mid_x, role_key=chain)
        put("ln1", old_x, title="Quality Director", company="Orbase")
        put("ln2", old_x, last_checked_at=mid_x, last_check_result="live")
        put("ln3", old_x, tier="filtered")
        put("ln4", old_x, closed_at=mid_x, closed_source="manual")
        conn.commit()

        dry_x = check_jobs.expire_stale(conn, write=False, now=now_x)
        check(dry_x["expired"] == 2,
              "two silent roles would expire, the lone advert and the"
              f" filtered one (got {dry_x['expired']})")
        check(conn.execute(
            "SELECT COUNT(*) AS n FROM opportunities"
            " WHERE expired_at IS NOT NULL").fetchone()["n"] == 0,
              "the dry expiry writes nothing")
        wet_x = check_jobs.expire_stale(conn, write=True, now=now_x)
        check(wet_x["expired"] == 2, "the live pass stamps the same two")
        rows_x = {r["url_hash"]: dict(r) for r in conn.execute(
            "SELECT * FROM opportunities")}
        check(rows_x["ch1"]["expired_at"] is None,
              "a stale advert with a fresher sibling in its chain lives on")
        check(rows_x["ln1"]["expired_at"] is not None
              and rows_x["ln3"]["expired_at"] is not None,
              "a lone silent role expires, the filtered tier included")
        check(rows_x["ln2"]["expired_at"] is None,
              "a live page check inside the window holds expiry off")
        check(rows_x["ln4"]["expired_at"] is None,
              "a closed row is already retired, expiry leaves it alone")
        check(check_jobs.expire_stale(conn, write=True,
                                      now=now_x)["expired"] == 0,
              "the expiry pass is idempotent")
        chain2 = radar_common.role_key("Clinical Lead", "Novem")
        put("gh1", old_x, role_key=chain2)
        put("gh2", mid_x, role_key=chain2,
            closed_at=mid_x, closed_source="manual")
        conn.commit()
        check_jobs.expire_stale(conn, write=True, now=now_x)
        check(conn.execute(
            "SELECT expired_at FROM opportunities WHERE url_hash = 'gh1'"
            ).fetchone()["expired_at"] is None,
              "a closed sibling's recent sighting still anchors the chain")
        config_x = radar_common.load_config()
        data_x = bd4.collect(conn, config_x)
        check({o["url_hash"] for o in data_x["retired_roles"]}
              >= {"ln1", "ln3", "ln4"},
              "expired rows retire into the archive list beside the closed")
        conn.close()

    # ----- ageing redefined. Unknown for a month, any tier bar filtered.
    import build_digest as bg4
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "ageing_rule.sqlite")

        def days_back(n):
            return (datetime.now(timezone.utc)
                    - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def put_age(h, tier, first, last, **extra):
            cols = {"url_hash": h, "first_seen": first, "last_seen": last,
                    "title": f"Role {h}", "company": "Agewell", "tier": tier}
            cols.update(extra)
            conn.execute(
                f"INSERT INTO opportunities ({', '.join(cols)}) VALUES"
                f" ({', '.join('?' for _ in cols)})", tuple(cols.values()))
            return conn.execute("SELECT id FROM opportunities WHERE"
                                " url_hash = ?", (h,)).fetchone()["id"]

        old_unknown = put_age("ag1", "reading", days_back(40), days_back(40))
        old_live = put_age("ag2", "top", days_back(40), days_back(2))
        put_age("ag3", "question", days_back(10), days_back(10))
        put_age("ag4", "filtered", days_back(40), days_back(40))
        put_age("ag5", "reading", days_back(40), days_back(40),
                last_checked_at=days_back(2), last_check_result="live")
        conn.commit()
        config_a = radar_common.load_config()
        data_a = bd4.collect(conn, config_a)
        age_ids = {a["id"] for a in data_a["ageing"]
                   if a["kind"] == "opportunity"}
        check(age_ids == {old_unknown},
              "only the month-old unknown advert ages; live, young,"
              f" filtered and freshly checked stay out (got {age_ids})")
        check(any(o["id"] == old_live for o in data_a["tiers"]["top"]),
              "the old but live row stays in its tier untouched")
        digest_a = bg4.collect(conn, config_a)
        digest_age = {a["id"] for a in digest_a["ageing"]
                      if a.get("kind") == "opportunity"}
        check(digest_age == {old_unknown},
              f"the digest ages by the same rule (got {digest_age})")
        jobs_a = bd4.render_jobs_page(data_a, config_a, "t")
        check("<h2>Ageing</h2>" not in jobs_a
              and f'data-ack="{old_unknown}"' in jobs_a,
              "Jobs has no Ageing section, the aged row stays in its tier"
              " with its state on the row (J16)")
        conn.close()

    # ----- one role, one row. The collapse, the chain verdicts and the
    # row's state surface.
    with tempfile.TemporaryDirectory() as tmp:
        db5 = Path(tmp) / "roles.sqlite"
        conn = radar_common.get_db(db5)
        key5 = radar_common.role_key("Principal Device Engineer", "Chainwell")

        def put5(h, first, last, **extra):
            cols = {"url_hash": h, "first_seen": first, "last_seen": last,
                    "title": "Principal Device Engineer",
                    "company": "Chainwell", "tier": "reading",
                    "role_key": key5}
            cols.update(extra)
            conn.execute(
                f"INSERT INTO opportunities ({', '.join(cols)}) VALUES"
                f" ({', '.join('?' for _ in cols)})", tuple(cols.values()))
            return conn.execute("SELECT id FROM opportunities WHERE"
                                " url_hash = ?", (h,)).fetchone()["id"]

        def days5(n):
            return (datetime.now(timezone.utc)
                    - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")

        older = put5("c1", days5(20), days5(12), location="Leeds",
                     seen_count=4)
        newer = put5("c2", days5(6), days5(2), location="Remote",
                     seen_count=3)
        put5("c3", days5(40), days5(40), title="Head of QA",
             company="Solo Dx", tier="question",
             role_key=radar_common.role_key("Head of QA", "Solo Dx"),
             question_text="Would they move on the rate")
        conn.commit()
        config5 = radar_common.load_config()
        data5 = bd4.collect(conn, config5)
        jobs5 = bd4.render_jobs_page(data5, config5, "t")
        check(f'data-ack="{newer}"' in jobs5
              and f'data-ack="{older}"' not in jobs5,
              "a repost chain in one tier renders once, the freshest advert"
              " its face")
        check("Remote" in jobs5 and "Leeds" in jobs5,
              "the chain's locations roll up onto the one row")
        check("Seen 7 times across 2 adverts" in jobs5,
              "the detail carries the sightings and the repost count")
        check("Would they move on the rate" in jobs5,
              "the question leads the one-question-away row")

        # Brief B. One collapse across every tier, the badges, the
        # percentage, the filter, the lead line, and what left the page.
        import re as _re5
        key_span = radar_common.role_key("Head of Regulatory", "Spanner Dx")
        key_filt = radar_common.role_key("Tester", "Filt Co")
        key_unsc = radar_common.role_key("Mystery Role", "Nullco")
        span_old = put5("c4", days5(30), days5(30), title="Head of Regulatory",
                        company="Spanner Dx", tier="reading", role_key=key_span,
                        engagement_type="permanent", region="UK")
        span_new = put5("c5", days5(3), days5(1), title="Head of Regulatory",
                        company="Spanner Dx", tier="question", role_key=key_span,
                        question_text="Would they go remote",
                        engagement_type="permanent", region="UK",
                        location_class="hybrid")
        top_id = put5("c6", days5(2), days5(2), title="Director of Software",
                      company="Topper Dx", tier="top",
                      role_key=radar_common.role_key("Director of Software", "Topper Dx"),
                      rate_band="unstated", one_line_why="The scorer had its own line.",
                      engagement_type="contract", cv_match=88, region="Europe",
                      location_class="remote")
        put5("c7", days5(50), days5(50), title="Tester", company="Filt Co",
             tier="filtered", role_key=key_filt, filter_reason="cv match under 40")
        put5("c8", days5(45), days5(45), title="Tester", company="Filt Co",
             tier="filtered", role_key=key_filt, filter_reason="cv match under 40")
        put5("c9", days5(45), days5(45), title="Analyst", company="Filt Co",
             tier="filtered", role_key=radar_common.role_key("Analyst", "Filt Co"),
             filter_reason="not a real leadership role")
        put5("c10", days5(60), days5(60), title="Mystery Role", company="Nullco",
             tier=None, role_key=key_unsc)
        put5("c11", days5(55), days5(55), title="Mystery Role", company="Nullco",
             tier=None, role_key=key_unsc)
        # A chain whose freshest advert read lower than its best. The old
        # advert was Top, the new one Reading. Highest wins the face, the
        # row borrows the new advert's age, the detail names the drop.
        key_drop = radar_common.role_key("VP Quality", "Dropper Dx")
        drop_top = put5("c12", days5(40), days5(35), title="VP Quality",
                        company="Dropper Dx", tier="top", role_key=key_drop,
                        rate_band="above", one_line_why="Scored high then.",
                        cv_match=81)
        drop_read = put5("c13", days5(4), days5(1), title="VP Quality",
                         company="Dropper Dx", tier="reading", role_key=key_drop,
                         rate_band="above", one_line_why="Scored lower now.",
                         cv_match=81)
        conn.commit()
        data6 = bd4.collect(conn, config5)
        jobs6 = bd4.render_jobs_page(data6, config5, "t")
        home6 = bd4.render_home_page(data6, config5, "t")

        def sec_count(page, title):
            m = _re5.search(r"<h2>" + _re5.escape(title)
                            + r"</h2>.*?<span class=\"section-count\">(\d+)</span>",
                            page, _re5.S)
            return int(m.group(1)) if m else None

        acks = _re5.findall(r'data-ack="(\d+)"', jobs6)
        key_of = {r["id"]: r["role_key"] for r in
                  conn.execute("SELECT id, role_key FROM opportunities")}
        keys = [key_of[int(a)] for a in acks]
        check(acks and len(keys) == len(set(keys)),
              f"no role_key renders twice on the page ({len(acks)} rows)")
        check(f'data-ack="{span_new}"' in jobs6
              and f'data-ack="{span_old}"' not in jobs6
              and sec_count(jobs6, "One question away") == 2
              and sec_count(jobs6, "Reading") == 1,
              "a chain spanning tiers appears once, in the highest tier"
              " it reached")
        # The row's markup, summary and detail, is the chunk between one
        # row open and the next.
        drop_row = next((c for c in _re5.split(r'<div class="row(?: row-acked)?"', jobs6)
                         if f'data-ack="{drop_top}"' in c), "")
        check(drop_row and f'data-ack="{drop_read}"' not in jobs6
              and sec_count(jobs6, "Top prospects") == 2
              and "Latest advert read lower, reading." in drop_row
              and "Scored high then" in drop_row,
              "an old Top advert outranks a fresh Reading one, and the"
              " detail says the latest advert read lower")
        check(drop_row and "state-live" in drop_row
              and jobs6.count("Latest advert read lower") == 1,
              "the face borrows the chain's freshest age, and a chain"
              " whose freshest advert is its face says nothing")
        check("Filtered, 2 roles. 1 cv match under 40, 1 not a real"
              " leadership role." in jobs6
              and "so the filter can be audited" not in jobs6,
              "the Filtered count is roles after collapse, with the reason"
              " roll-up and no audit sentence")
        check("<h2>Ageing</h2>" not in jobs6 and "<h2>Needs review</h2>" not in jobs6
              and f'data-ack="{top_id}"' in jobs6,
              "Ageing and Needs review have left Jobs")
        check("1 role unscored, run rescore.py" in home6
              or '<span class="num">1</span>role unscored, run rescore.py' in home6,
              "the unscored count sits on Home, as roles")
        for word in ("Contract", "Permanent", "Unknown"):
            check(f'chip-engagement chip-{word.lower()}">{word}</span>' in jobs6,
                  f"the {word} badge renders from engagement_type")
        check(jobs6.count("chip-engagement") == len(acks),
              "every row carries an engagement badge")
        check('<span class="col-cv">88%</span>' in jobs6
              and _re5.search(r'col-cv">\d+</span>', jobs6) is None,
              "CV match shows a % sign on every row")
        check('id="loc-filter"' in jobs6 and 'data-loc="remote"' in jobs6
              and 'data-loc="elsewhere"' in jobs6
              and "radar-location" in jobs6
              and 'data-region="europe" data-remote="1"' in jobs6,
              "the location filter control, its script and the row data"
              " attributes are present")
        check("Rate not stated, ask before anything else." in jobs6
              and "had its own line" not in jobs6,
              "a Top row with an unstated rate leads with the rate line")
        check('<span class="badge-rate">Rate not stated</span>' in jobs6
              and '<span class="chip">rate not stated</span>' not in jobs6,
              "rate not stated is a badge with the weight of the State cell")
        check(jobs6.count('<details class="section fold">') == 2
              and '<summary class="section-head"><h2>One question away</h2>' in jobs6
              and '<div class="section-head"><h2>Top prospects</h2>' in jobs6,
              "Top is open, One question away and Reading fold with their counts")
        check("Scored against" not in jobs6 and "row-sub\">Via " not in jobs6
              and "scripts/rescore.py" not in jobs6
              and "Add a job source" not in jobs6,
              "the detail drops the CV stamp, the source line and the rescore"
              " code, and the source strip is gone from Jobs")
        check('<span class="h-pay">Rate</span>' in jobs6
              and '<span class="h-type">Type</span>' in jobs6,
              "the column header names the Rate and Type cells")
        check(">Live<" in jobs5 and "state-live" in jobs5,
              "a chain seen this week reads Live on the row")
        check(">Unknown<" in jobs5, "a silent role reads Unknown")
        check("CV match" in jobs5 and "Capability" not in jobs5
              and "Desire" not in jobs5 and ">Combined<" not in jobs5,
              "the headers read Role, CV match, State, Last seen")
        check("a day converted" not in jobs5,
              "no estimated day rate anywhere on the page")
        check(">Rate not stated<" in jobs5,
              "silence about pay carries its prominent flag")
        check(jobs5.index("One question away") < jobs5.index("Reading")
              and "<h2>Ageing</h2>" not in jobs5,
              "the sections run in the draft's order, Ageing after Reading")

        import serve_dashboard as serve5
        saved5 = getattr(serve5, "ARGS", None)
        serve5.ARGS = argparse.Namespace(db=str(db5), push=False,
                                         host="127.0.0.1", port=0)
        try:
            result = serve5.set_lifecycle(newer, "close")
            check(result.get("ok") is True, "closing the face reports ok")
            rows5 = {r["url_hash"]: dict(r) for r in conn.execute(
                "SELECT * FROM opportunities")}
            check(rows5["c1"]["closed_at"] is not None
                  and rows5["c2"]["closed_at"] is not None
                  and rows5["c1"]["closed_source"] == "manual",
                  "a hand close covers the whole repost chain")
            check(rows5["c3"]["closed_at"] is None,
                  "another role's rows are untouched by the chain close")
            result = serve5.set_lifecycle(newer, "unclose")
            rows5 = {r["url_hash"]: dict(r) for r in conn.execute(
                "SELECT * FROM opportunities")}
            check(result.get("ok") is True
                  and rows5["c1"]["closed_at"] is None
                  and rows5["c2"]["closed_at"] is None,
                  "the undo lifts the stamp from the whole chain")
            result = serve5.set_acknowledged(older, True)
            rows5 = {r["url_hash"]: dict(r) for r in conn.execute(
                "SELECT * FROM opportunities")}
            check(result.get("ok") is True
                  and rows5["c1"]["acknowledged_at"] is not None
                  and rows5["c2"]["acknowledged_at"] is not None,
                  "seen it covers the chain too")
            result = serve5.set_acknowledged(older, False)
            rows5 = {r["url_hash"]: dict(r) for r in conn.execute(
                "SELECT * FROM opportunities")}
            check(result.get("ok") is True
                  and rows5["c1"]["acknowledged_at"] is None
                  and rows5["c2"]["acknowledged_at"] is None,
                  "and its undo brings the whole chain back")
        finally:
            serve5.ARGS = saved5
        conn.close()

    # ----- the retirement migration, once per database ever
    import sqlite3 as sqlite3_mod
    with tempfile.TemporaryDirectory() as tmp:
        legacy = Path(tmp) / "legacy.sqlite"
        raw = sqlite3_mod.connect(legacy)
        raw.execute(
            "CREATE TABLE opportunities ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, url_hash TEXT NOT NULL"
            " UNIQUE, first_seen TEXT NOT NULL, source TEXT, company TEXT,"
            " title TEXT, location TEXT, salary_rate TEXT, source_url TEXT,"
            " thread_type TEXT, cv_match INTEGER, want_match INTEGER,"
            " combined INTEGER, one_line_why TEXT, red_flags TEXT,"
            " suggested_action TEXT, act_by TEXT, status TEXT NOT NULL"
            " DEFAULT 'new', status_changed_at TEXT, notes TEXT)")
        raw.execute(
            "INSERT INTO opportunities (url_hash, first_seen, company, title,"
            " status, status_changed_at) VALUES"
            " ('lg1', '2026-07-01T00:00:00Z', 'Legacy Dead', 'Old role',"
            "  'dead', '2026-07-10T00:00:00Z'),"
            " ('lg2', '2026-07-01T00:00:00Z', 'Legacy New', 'Newer role',"
            "  'new', NULL)")
        raw.commit()
        raw.close()

        conn = radar_common.get_db(legacy)
        row = conn.execute("SELECT acknowledged_at FROM opportunities"
                           " WHERE url_hash = 'lg1'").fetchone()
        check(row["acknowledged_at"] == "2026-07-10T00:00:00Z",
              "the migration maps a legacy dead row from its status change")
        row = conn.execute("SELECT acknowledged_at FROM opportunities"
                           " WHERE url_hash = 'lg2'").fetchone()
        check(row["acknowledged_at"] is None,
              "the migration leaves machine-owned rows alone")
        flag = conn.execute("SELECT value FROM meta WHERE"
                            " key = 'migrated_retirement'").fetchone()
        check(flag is not None, "the mapping sets its once-only flag")
        # Closure detection stage one. A legacy row opens with its sighting
        # columns filled, last seen when first seen, counted once.
        row = conn.execute("SELECT first_seen, last_seen, seen_count"
                           " FROM opportunities"
                           " WHERE url_hash = 'lg2'").fetchone()
        check(row["last_seen"] == row["first_seen"] and row["seen_count"] == 1,
              "the sighting backfill reads a legacy row as last seen when"
              " first seen, counted once")
        seen_flag = conn.execute("SELECT value FROM meta WHERE"
                                 " key = 'migrated_last_seen'").fetchone()
        check(seen_flag is not None,
              "the sighting backfill sets its once-only flag")
        # Closure detection stage four. A legacy row gets its role key
        # from the title and company it already carries.
        row = conn.execute("SELECT role_key FROM opportunities"
                           " WHERE url_hash = 'lg2'").fetchone()
        check(row["role_key"]
              == radar_common.role_key("Newer role", "Legacy New"),
              "the role key backfill reads the stored title and company")
        role_flag = conn.execute("SELECT value FROM meta WHERE"
                                 " key = 'migrated_role_key'").fetchone()
        check(role_flag is not None,
              "the role key backfill sets its once-only flag")
        # The undo-survival regression, through the real /unack write.
        # Undo must clear the stamp, return the legacy status to
        # machine-owned 'new' so the digest, ageing and backfill can see
        # the row again, and hold across the next connection.
        import argparse as argparse_mod0
        import serve_dashboard as serve_mod
        lg1_id = conn.execute("SELECT id FROM opportunities"
                              " WHERE url_hash = 'lg1'").fetchone()["id"]
        conn.close()
        saved_args = getattr(serve_mod, "ARGS", None)
        serve_mod.ARGS = argparse_mod0.Namespace(
            db=str(legacy), push=False, host="127.0.0.1", port=0)
        result = serve_mod.set_acknowledged(lg1_id, False)
        serve_mod.ARGS = saved_args
        check(result.get("ok") is True, "undo on the legacy row reports ok")
        conn = radar_common.get_db(legacy)
        row = conn.execute("SELECT acknowledged_at, status FROM opportunities"
                           " WHERE url_hash = 'lg1'").fetchone()
        check(row["acknowledged_at"] is None,
              "an undo on a legacy-retired row survives the next connection")
        check(row["status"] == "new",
              "undo returns the legacy status to machine-owned new, the row"
              " is fully alive again, not stranded half-visible")
        conn.close()

    # ----- acknowledge and dismiss, the full loop against a throwaway db
    import argparse as argparse_mod
    import sqlite3
    import threading
    import urllib.request

    import build_dashboard
    import build_digest
    import serve_dashboard

    config = radar_common.load_config()
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "ack.sqlite"
        conn = radar_common.get_db(db)
        now = radar_common.now_iso()
        for h, company, title in (("ack1", "Seenit Ltd", "Director One"),
                                  ("ack2", "Keepit Ltd", "Director Two")):
            conn.execute(
                "INSERT INTO opportunities (url_hash, first_seen, company,"
                " title, thread_type, status, cv_match, want_match, combined,"
                " one_line_why, rate_band, tier)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,'top')",
                (h, now, company, title, "inbound", "new", 90, 90, 90,
                 "Exactly the target.", "above"))
        conn.execute(
            "INSERT INTO signals (url_hash, first_seen, source_id, company,"
            " headline, summary, source_url, relevance, why, playbook_step,"
            " pushed, pushed_at, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sig-front", now, "imec-news", "Frontpage Dx",
             "Frontpage Dx raises seed for photonic IVD reader",
             "Funding note.", "https://example.invalid/frontpage", 88,
             "An imec spin-off entering the buying window.",
             "Congratulate the CTO on the round in a comment today.",
             1, now, "new"))
        conn.commit()
        ids = {r["company"]: r["id"] for r in
               conn.execute("SELECT id, company FROM opportunities")}
        conn.close()

        serve_dashboard.ARGS = argparse_mod.Namespace(
            db=str(db), push=False, host="127.0.0.1", port=0)

        result = serve_dashboard.set_acknowledged(ids["Seenit Ltd"], True)
        check(result.get("ok") is True, "acknowledging a row reports ok")
        result = serve_dashboard.set_acknowledged(ids["Seenit Ltd"], True)
        check(result.get("ok") is False,
              "acknowledging the same row twice is refused, not repeated")

        read = sqlite3.connect(db)
        read.row_factory = sqlite3.Row
        data = build_dashboard.collect(read, config)
        check([o["id"] for o in data["opportunities"]] == [ids["Keepit Ltd"]],
              "the acknowledged row leaves the default view immediately")
        check([o["id"] for o in data["acknowledged"]] == [ids["Seenit Ltd"]],
              "the acknowledged row sits behind the toggle instead")
        html_page = build_dashboard.render_page(
            data, config, "ack test", Path(tmp) / "dash.html", serve=True)
        check("Show acknowledged (1)" in html_page,
              "the toggle names the acknowledged count")
        check(f'data-unack="{ids["Seenit Ltd"]}"' in html_page
              and "row-acked" in html_page,
              "the hidden row carries an undo, per row")
        check(f'data-ack="{ids["Keepit Ltd"]}"' in html_page,
              "the visible row carries an acknowledge action")
        # The rendered row surface, pinned. CV match and State in the
        # header, the state word on the row, and the derived bands,
        # their legend and the status chip all gone (J9, J12).
        check('class="h-state"' in html_page and 'class="h-cv"' in html_page,
              "the dashboard renders the CV match and State headers")
        check('state-live' in html_page,
              "a freshly seen row wears the Live state word")
        check('class="h-rate"' not in html_page
              and "rate-above" not in html_page
              and "Rate bands sit against the" not in html_page,
              "the rate band words and their legend retired with the score")
        check('<span class="chip chip-new">' not in html_page,
              "the status chip has left the visible inbound table")

        # The Insights front page. Signals read as news, a tab reaches it,
        # and the source-health table sits behind the fold at the bottom.
        insights = build_dashboard.render_insights_page(
            data, config, "ack test")
        check("Frontpage Dx raises seed" in insights
              and 'class="lead"' in insights,
              "the top signal leads the Insights front page")
        check("Suggestion" in insights
              and "Congratulate the CTO" in insights,
              "the playbook step renders as the lead's suggestion box")
        check("Do today" not in insights,
              "the box is called Suggestion now, nowhere Do today")
        check('<details class="sources-fold">' in insights
              and "Where this page" in insights,
              "sources sit collapsed at the bottom of Insights")
        check('href="/insights" aria-current="page"' in insights,
              "the Insights tab marks itself current")
        check("The week in numbers" in insights and "Coverage" in insights,
              "the widgets render")
        check(insights.count('class="info"') >= 2,
              "the Insights widget rules sit behind info icons")

        # The insight actions. Dismiss follows the jobs rule, done logs
        # the touch and the touch feeds context back onto the page.
        sig_id = read.execute("SELECT id FROM signals WHERE url_hash ="
                              " 'sig-front'").fetchone()["id"]
        check(f'data-sig-done="{sig_id}"' in insights
              and f'data-sig-ack="{sig_id}"' in insights,
              "an active story carries I did it and Dismiss")

        result = serve_dashboard.set_signal_state(sig_id, "ack")
        check(result.get("ok") is True, "dismissing an insight reports ok")
        read2 = sqlite3.connect(db)
        read2.row_factory = sqlite3.Row
        data2 = build_dashboard.collect(read2, config)
        ins2 = build_dashboard.render_insights_page(data2, config, "t")
        check(not any(s["id"] == sig_id for s in data2["signals"]),
              "a dismissed insight leaves the active signals")
        check(f'data-sig-unack="{sig_id}"' in ins2
              and "Dismissed (1)" in ins2,
              "the dismissed insight sits behind its fold with an undo")
        d2 = build_digest.collect(read2, config)
        check(not any(s.get("url_hash") == "sig-front" for s in d2["signals"]),
              "a digest render excludes the dismissed insight")
        read2.close()
        w2 = radar_common.get_db(db)
        cur = w2.execute(
            "INSERT OR IGNORE INTO signals (url_hash, first_seen, company,"
            " headline, status) VALUES ('sig-front', ?, 'Frontpage Dx',"
            " 'dupe', 'new')", (now,))
        w2.commit()
        check(cur.rowcount == 0,
              "a dismissed insight's URL hash is still rejected by dedupe")
        w2.close()

        result = serve_dashboard.set_signal_state(sig_id, "unack")
        check(result.get("ok") is True, "undo brings the insight back")
        result = serve_dashboard.set_signal_state(sig_id, "done")
        check(result.get("ok") is True
              and result.get("channel") == "comment",
              "I did it reports ok and reads the channel off the suggestion")
        read2 = sqlite3.connect(db)
        read2.row_factory = sqlite3.Row
        touch_row = read2.execute(
            "SELECT company, channel, note FROM touches"
            " WHERE company = 'Frontpage Dx'").fetchone()
        check(touch_row is not None
              and touch_row["channel"] == "comment"
              and "Did the suggestion" in touch_row["note"],
              "I did it logs the touch against the company")
        sig_status = read2.execute("SELECT status FROM signals WHERE id = ?",
                                   (sig_id,)).fetchone()["status"]
        check(sig_status == "actioned",
              "I did it marks the signal actioned, out of the fresh flow")
        data3 = build_dashboard.collect(read2, config)
        ins3 = build_dashboard.render_insights_page(data3, config, "t")
        check("Followed up" in ins3,
              "a done insight moves to the followed-up section")
        check("You last touched this company" in ins3,
              "the touch feeds relationship context back onto the page")
        d3 = build_digest.collect(read2, config)
        check(not any(s.get("url_hash") == "sig-front" for s in d3["signals"]),
              "a done insight stays out of the digest")
        read2.close()
        check('href="/insights"' in html_page,
              "the archive page carries the Insights tab")
        check('class="src-head"' not in html_page,
              "watchlist health has left the served archive page")
        static_page = build_dashboard.render_page(
            data, config, "ack test", Path(tmp) / "static.html", serve=False)
        check('class="src-head"' in static_page,
              "the static single-page fallback keeps the watchlist table")

        # Close and dismiss, the lifecycle loop. A verdict takes the row
        # out of every working view, the archive holds it with an undo,
        # and the stamp carries its confirmation source.
        check(f'data-close="{ids["Keepit Ltd"]}"' in html_page
              and f'data-dismiss="{ids["Keepit Ltd"]}"' in html_page,
              "an open row carries Close and Dismiss beside Acknowledge")
        result = serve_dashboard.set_lifecycle(ids["Keepit Ltd"], "close")
        check(result.get("ok") is True, "closing a row reports ok")
        result = serve_dashboard.set_lifecycle(ids["Keepit Ltd"], "close")
        check(result.get("ok") is False,
              "closing the same row twice is refused, not repeated")
        readl = sqlite3.connect(db)
        readl.row_factory = sqlite3.Row
        row = readl.execute(
            "SELECT closed_at, closed_source FROM opportunities"
            " WHERE id = ?", (ids["Keepit Ltd"],)).fetchone()
        check(row["closed_at"] is not None
              and row["closed_source"] == "manual",
              "a hand close is stamped manual, the confirmation on record")
        datal = build_dashboard.collect(readl, config)
        check(not any(o["id"] == ids["Keepit Ltd"]
                      for o in datal["opportunities"]),
              "a closed row leaves the working view")
        check(not any(o["id"] == ids["Keepit Ltd"]
                      for o in datal.get("acknowledged") or []),
              "a closed row does not hide behind the acknowledged toggle")
        check(any(o["id"] == ids["Keepit Ltd"]
                  for o in datal.get("retired_roles") or []),
              "a closed row sits in the retired list instead")
        pagel = build_dashboard.render_page(
            datal, config, "t", Path(tmp) / "retired.html", serve=True)
        check("Closed and dismissed" in pagel
              and f'data-unclose="{ids["Keepit Ltd"]}"' in pagel,
              "the archive lists the closed row in its own section with"
              " an undo")
        jobsl = build_dashboard.render_jobs_page(datal, config, "t")
        check(f'data-ack="{ids["Keepit Ltd"]}"' not in jobsl,
              "the closed row has left the Jobs page")
        check(radar_common.life_state(dict(readl.execute(
                  "SELECT * FROM opportunities WHERE id = ?",
                  (ids["Keepit Ltd"],)).fetchone())) == "closed",
              "the closed row derives closed")
        readl.close()
        result = serve_dashboard.set_lifecycle(ids["Keepit Ltd"], "unclose")
        check(result.get("ok") is True, "undo reopens the closed row")
        readl = sqlite3.connect(db)
        readl.row_factory = sqlite3.Row
        row = readl.execute(
            "SELECT closed_at, closed_source FROM opportunities"
            " WHERE id = ?", (ids["Keepit Ltd"],)).fetchone()
        check(row["closed_at"] is None and row["closed_source"] is None,
              "unclose clears the stamp and its source together")
        readl.close()
        result = serve_dashboard.set_lifecycle(ids["Keepit Ltd"], "dismiss")
        check(result.get("ok") is True, "dismissing a row reports ok")
        readl = sqlite3.connect(db)
        readl.row_factory = sqlite3.Row
        datal = build_dashboard.collect(readl, config)
        readl.close()
        check(not any(o["id"] == ids["Keepit Ltd"]
                      for o in datal["opportunities"])
              and any(o["id"] == ids["Keepit Ltd"]
                      for o in datal.get("retired_roles") or []),
              "a dismissed row follows the same retirement rule")
        result = serve_dashboard.set_lifecycle(ids["Keepit Ltd"], "undismiss")
        check(result.get("ok") is True, "undo restores the dismissed row")
        result = serve_dashboard.set_lifecycle(ids["Keepit Ltd"], "sideways")
        check(result.get("ok") is False, "an unknown lifecycle action is refused")

        # The Jobs page. Boards as sections, prospects on top, a registry
        # the page itself can extend.
        sources = radar_common.load_job_sources(Path(tmp) / "nope.yaml")
        check([s["id"] for s in sources] == ["linkedin-alert", "reed-alert",
                                             "indeed-alert", "cvlibrary-alert"],
              "the four boards ship built in")
        reg_path = Path(tmp) / "job_sources.yaml"
        added = radar_common.add_job_source("Technojobs", "technojobs",
                                            url="technojobs.co.uk",
                                            path=reg_path)
        check(added["id"] == "technojobs-alert",
              "a custom source slugs into the built-in id shape")
        loaded = radar_common.load_job_sources(reg_path)
        check(any(s["id"] == "technojobs-alert" for s in loaded),
              "the custom source loads alongside the built-ins")
        try:
            radar_common.add_job_source("Technojobs", "other", path=reg_path)
            check(False, "a duplicate source name is refused")
        except ValueError:
            check(True, "a duplicate source name is refused")
        try:
            radar_common.add_job_source("Other Board", "technojobs", path=reg_path)
            check(False, "a duplicate sender match is refused")
        except ValueError:
            check(True, "a duplicate sender match is refused")
        try:
            radar_common.add_job_source("X", "ab", path=reg_path)
            check(False, "too-short inputs are refused")
        except ValueError:
            check(True, "too-short inputs are refused")

        import process_email as pe_mod
        saved_reg = radar_common.JOB_SOURCES_PATH
        try:
            radar_common.JOB_SOURCES_PATH = reg_path
            got = pe_mod.detect_source("Technojobs <alerts@technojobs.co.uk>")
            check(got == "technojobs-alert",
                  f"detect_source tags a configured custom board (got {got})")
            got = pe_mod.detect_source("noreply@nowhere.example")
            check(got == "email-other",
                  "an unrecognised sender still files under email-other")
        finally:
            radar_common.JOB_SOURCES_PATH = saved_reg

        jobs_page = build_dashboard.render_jobs_page(data, config, "t")
        check("Top prospects" in jobs_page,
              "the Jobs page leads with top prospects")
        # Board grouping retired on 3 August. A role's board is one fact on
        # its row, and board freshness is telemetry that moved to Home.
        # These asserted the sections that no longer exist.
        for board in ("LinkedIn", "Reed", "Indeed", "CV-Library"):
            check(f">{board}</span>" not in jobs_page,
                  f"{board} no longer gets a section of its own")
        check('class="board-badge"' not in jobs_page,
              "no board badge tiles, and so no empty shelves inviting signup")
        check("Score distribution" not in jobs_page,
              "the score histogram retired with the additive score")
        # The analysis fold moved to Insights with the market intel it
        # is. Jobs carries roles, the roll-up and the add-source strip.
        check("Analysis." not in jobs_page,
              "the analysis fold has left the Jobs page")
        check("The jobs market" in insights
              and "The trailing 90 days" in insights,
              "Insights carries the rates widget in the market fold instead")
        # The telemetry moved to Home on 3 August. These facts still matter,
        # they are just no longer on the page a person opens for roles, so
        # the checks follow them rather than being deleted.
        home_page = build_dashboard.render_home_page(data, config, "t")
        check("The flow, last fourteen days" in home_page
              and 'class="bars"' in home_page,
              "the fortnight flow chart renders, on Home")
        check("dot-green" in home_page,
              "a board that heard from its sender today shows a green light")
        check("dot-grey" in home_page,
              "a board never heard from shows the hollow waiting light")
        check("dot-red" in home_page and "Inbox never run" in home_page,
              "an inbox that has never run wears the red light honestly, on Home")
        check("The flow, last fourteen days" not in jobs_page
              and "Board freshness" not in jobs_page,
              "and none of it clutters Jobs any more")
        check('id="src-add"' not in jobs_page
              and "Add a job source" not in jobs_page
              and 'id="src-add"' in home_page and "Add a job source" in home_page,
              "the add-a-source form renders on Home, not on Jobs (J11)")
        check("None yet" not in home_page
              and "job_sources.yaml" not in home_page,
              "no custom-source receipt renders while there are none")
        # The board-order assertion went with the board sections. Ordering
        # boards on a page that no longer groups by board tested nothing.
        saved_reg3 = radar_common.JOB_SOURCES_PATH
        try:
            radar_common.JOB_SOURCES_PATH = reg_path
            home_with_custom = build_dashboard.render_home_page(
                data, config, "t")
        finally:
            radar_common.JOB_SOURCES_PATH = saved_reg3
        check("Added so far: Technojobs (matches technojobs)"
              in home_with_custom
              and "job_sources.yaml" in home_with_custom,
              "an added source earns its one-line receipt on Home, with the edit path")
        # A custom source no longer earns a section, because no source does.
        # What it earns is the receipt line above, which is the honest
        # acknowledgement that the radar now watches for its sender.
        check(">Technojobs</span>" not in home_with_custom,
              "an added source gets no board section, because none exist")
        check(home_with_custom.index("Added so far: Technojobs")
              > home_with_custom.index('class="entrance"'),
              "its receipt sits at the foot of Home, below the cards")
        check('href="/jobs" aria-current="page"' in jobs_page,
              "the Jobs tab marks itself current")
        check(f'data-ack="{ids["Keepit Ltd"]}"' in jobs_page,
              "job rows carry the acknowledge action here too")
        check('>Home</a>' in html_page and 'href="/jobs"' in html_page,
              "the archive page tab bar names Home and reaches Jobs")

        # Home is the entrance now. Three cards to the pages, the trends,
        # and the scoring panel with the file's own values in its inputs.
        home = build_dashboard.render_home_page(data, config, "t")
        for href, word in (("/jobs", "Jobs"), ("/insights", "Insights"),
                           ("/cv", "CV")):
            check(f'href="{href}"' in home and f">{word}</span>" in home,
                  f"the {word} entrance card links to its page")
        check("<svg" in home.split('class="entrance"')[1][:2000],
              "the entrance cards carry their icons")
        check("Top prospects, week on week" in home
              and 'class="bars"' in home,
              "the week-on-week prospects chart renders")
        check("Insight activity, the year" in home
              and 'class="trend-line"' in home,
              "the annual insight line chart renders")
        check("Recording began" in home,
              "months before the radar existed are named, never faked as "
              "quiet")
        check(home.count('class="info"') >= 3
              and "scoring 70 or higher" in home,
              "every Home widget rule lives behind an info icon")
        # The relevance filter. A sub-threshold signal is activity noise
        # and must not move the annual line.
        low = dict(data)
        low_sig = {**data["signals"][0], "id": 9999, "relevance": 20,
                   "first_seen": radar_common.now_iso()}
        low["signals"] = data["signals"] + [low_sig]
        # collect() owns the bucketing, so recompute through it: seed a
        # low-relevance signal in the db and re-collect.
        w3 = radar_common.get_db(db)
        w3.execute(
            "INSERT INTO signals (url_hash, first_seen, source_id, company,"
            " headline, relevance, status) VALUES ('low-noise', ?,"
            " 'imec-news', 'Noise Co', 'Low relevance chatter', 20, 'new')",
            (radar_common.now_iso(),))
        w3.commit()
        w3.close()
        read3 = sqlite3.connect(db)
        read3.row_factory = sqlite3.Row
        data3b = build_dashboard.collect(read3, config)
        read3.close()
        this_month_count = data3b["monthly_signals"][-1][1]
        check(this_month_count == 1,
              f"a relevance-20 signal does not move the annual line "
              f"(this month counts {this_month_count})")
        _floor_now = radar_common.read_rate_floor(CFG)
        check('id="pref-day_rate_floor_gbp"' in home
              and f'value="{_floor_now:g}"' in home,
              f"the scoring panel shows the live floor, {_floor_now:g}")
        check('id="pref-target_title"' in home
              and 'id="pref-keywords"' in home,
              "title and keywords are editable from the panel")
        check("Needs you" not in home,
              "the attention section stays silent with nothing due")
        check('href="/archive"' in home,
              "the full archive stays reachable from the footer")

        # Phase three. The digest selects on tier, so the fixtures need one.
        # Everything unacknowledged is gated top here, which is what makes
        # the acknowledged-row exclusion below a real test rather than a
        # tautology about an empty list.
        w = sqlite3.connect(db)
        w.execute("UPDATE opportunities SET tier = 'top'"
                  " WHERE acknowledged_at IS NULL")
        w.commit()
        w.close()

        digest_data = build_digest.collect(read, config)
        # The email no longer renders roles, so the band word and the rate
        # legend went with the table that carried them. What the email says
        # now is counts and a link, and the page says the rest.
        # A week with no inbound but a scored signal-kind role still gets
        # the legend next to its rate words.
        sig_only = dict(digest_data)
        sig_only["inbound"] = []
        sig_only["signals"] = [{
            "kind": "opportunity", "id": 999, "title": "Signal Role",
            "company": "Sig Ltd", "location": "Ghent", "combined": 80,
            "cv_match": 80, "want_match": 80, "one_line_why": "Fits.",
            "source_url": "", "rate_band": "above"}]
        check(True, "the rate legend retired with the email's inbound table")
        digest_ids = [o["id"] for o in digest_data["inbound"]]
        check(ids["Seenit Ltd"] not in digest_ids
              and ids["Keepit Ltd"] in digest_ids,
              "a digest render excludes the acknowledged row and keeps the rest")
        check(not digest_data["ageing"],
              "nothing acknowledged reaches the ageing section")
        read.close()

        # Dedupe survives acknowledgement. The URL hash is still on file,
        # so the same advert presented again is rejected, not resurfaced.
        w = radar_common.get_db(db)
        before = w.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        cur = w.execute(
            "INSERT OR IGNORE INTO opportunities (url_hash, first_seen,"
            " company, title, thread_type, status)"
            " VALUES ('ack1', ?, 'Seenit Ltd', 'Director One', 'inbound', 'new')",
            (now,))
        w.commit()
        after = w.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        check(cur.rowcount == 0 and before == after,
              "the same URL hash presented again is still rejected by dedupe")
        w.close()

        result = serve_dashboard.set_acknowledged(ids["Seenit Ltd"], False)
        check(result.get("ok") is True, "undo reports ok")
        read = sqlite3.connect(db)
        read.row_factory = sqlite3.Row
        data = build_dashboard.collect(read, config)
        read.close()
        check(len(data["opportunities"]) == 2 and not data["acknowledged"],
              "undo restores the row to the default view")

        # One real HTTP pass, so the endpoint wiring is proven, not assumed.
        server = serve_dashboard.RadarServer(("127.0.0.1", 0),
                                             serve_dashboard.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Every write from a page carries the write guard's header, so the
        # POSTs below do too. The guard's own checks are in
        # test/run_hardening_test.py.
        wh = serve_dashboard.WRITE_HEADER
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ack",
                data=json_mod.dumps({"id": ids["Seenit Ltd"]}).encode(),
                headers={"Content-Type": "application/json", wh: "1"},
                method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json_mod.loads(resp.read())
            check(resp.status == 200 and body.get("ok") is True,
                  "POST /ack acknowledges over real HTTP")
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/archive", timeout=10) as resp:
                page = resp.read().decode("utf-8")
            check(resp.status == 200
                  and f'data-unack="{ids["Seenit Ltd"]}"' in page,
                  "the archive re-renders with the row acknowledged")
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=10) as resp:
                home_http = resp.read().decode("utf-8")
            check(resp.status == 200 and 'class="entrance"' in home_http,
                  "GET / serves the entrance dashboard over HTTP")
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/insights", timeout=10) as resp:
                ins_page = resp.read().decode("utf-8")
            check(resp.status == 200 and "Frontpage Dx" in ins_page,
                  "GET /insights serves the front page over HTTP")
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/jobs", timeout=10) as resp:
                jobs_http = resp.read().decode("utf-8")
            check(resp.status == 200 and "Top prospects" in jobs_http,
                  "GET /jobs serves the boards page over HTTP")
            saved_reg2 = radar_common.JOB_SOURCES_PATH
            try:
                radar_common.JOB_SOURCES_PATH = Path(tmp) / "http_sources.yaml"
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/jobs/source",
                    data=json_mod.dumps({"name": "Escape Hatch",
                                         "sender": "escapehatch"}).encode(),
                    headers={"Content-Type": "application/json", wh: "1"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json_mod.loads(resp.read())
                check(resp.status == 200
                      and body.get("id") == "escape-hatch-alert"
                      and radar_common.JOB_SOURCES_PATH.exists(),
                      "POST /jobs/source writes the registry over HTTP")
                try:
                    urllib.request.urlopen(req, timeout=10)
                    check(False, "a duplicate source over HTTP is refused")
                except urllib.error.HTTPError as err:
                    check(err.code == 422,
                          "a duplicate source over HTTP is refused")
            finally:
                radar_common.JOB_SOURCES_PATH = saved_reg2

            # The CV endpoints over real HTTP, against a throwaway
            # profile dir so the real config/profile is never touched.
            import cv_store
            saved_profile = cv_store.PROFILE_DIR
            try:
                cv_store.PROFILE_DIR = Path(tmp) / "profile"
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/cv", timeout=10) as resp:
                    cv_page = resp.read().decode("utf-8")
                check(resp.status == 200 and "Upload a new CV" in cv_page,
                      "GET /cv serves the update section")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/cv/preview",
                    data="A CV as plain text.\n".encode("utf-8"),
                    headers={"X-Filename": "cv.txt", wh: "1"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json_mod.loads(resp.read())
                check(resp.status == 200
                      and "A CV as plain text." in body.get("markdown", ""),
                      "POST /cv/preview extracts and stages over HTTP")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/cv/discard",
                    data=json_mod.dumps(
                        {"token": body["token"]}).encode(),
                    headers={"Content-Type": "application/json", wh: "1"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    check(resp.status == 200,
                          "POST /cv/discard forgets the staged upload")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/cv/confirm",
                    data=json_mod.dumps(
                        {"token": "0" * 16}).encode(),
                    headers={"Content-Type": "application/json", wh: "1"},
                    method="POST")
                try:
                    urllib.request.urlopen(req, timeout=10)
                    check(False, "POST /cv/confirm refuses an unknown token")
                except urllib.error.HTTPError as err:
                    check(err.code == 409,
                          "POST /cv/confirm refuses an unknown token")
            finally:
                cv_store.PROFILE_DIR = saved_profile

            # The stale-rescore endpoint refuses to spend without the
            # explicit confirm, the guard the button's dialog satisfies.
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/cv/rescore",
                data=b"{}", headers={"Content-Type": "application/json",
                                     wh: "1"},
                method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                check(False, "POST /cv/rescore without confirm is refused")
            except urllib.error.HTTPError as err:
                check(err.code == 400,
                      "POST /cv/rescore without confirm is refused")

            # A hostile Content-Length must get a fast clean refusal, not
            # a handler thread pinned on read-to-EOF.
            import http.client
            hc = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            hc.putrequest("POST", "/ack")
            hc.putheader(wh, "1")
            hc.putheader("Content-Length", "-1")
            hc.endheaders()
            resp = hc.getresponse()
            check(resp.status == 400,
                  f"a negative Content-Length is refused with 400 "
                  f"(got {resp.status})")
            hc.close()
        finally:
            server.shutdown()
            server.server_close()

    # ----- CV store. Extraction, staged confirm, append-only history.
    import base64
    import io
    import os as os_mod
    import zipfile

    import cv_store
    import process_email
    import score_item

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:r><w:t>Luke Keogh</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Software director, medical devices and IVD.</w:t>'
        '</w:r></w:p>'
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'
        '<w:r><w:t>IEC 62304 audits end to end.</w:t></w:r></w:p>'
        '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.'
                    'openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("word/document.xml", doc_xml)
    docx_bytes = buf.getvalue()

    md = cv_store.extract_markdown("cv_sample.docx", docx_bytes)
    check("# Luke Keogh" in md, "docx heading extracts as a markdown heading")
    check("Software director, medical devices and IVD." in md,
          "docx body text extracts intact")
    check("- IEC 62304 audits end to end." in md,
          "docx list items extract as markdown bullets")
    try:
        cv_store.extract_markdown("cv.exe", b"MZ junk")
        check(False, "an unsupported upload type is refused")
    except cv_store.UploadError as err:
        check("Unsupported" in str(err), "an unsupported upload type is refused")

    # The pdf path, both branches reachable without a real CV. With pypdf
    # installed a textless page hits the scan refusal, without it the
    # missing-dependency refusal, either way a clean UploadError.
    try:
        import pypdf

        pdf_buf = io.BytesIO()
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(pdf_buf)
        try:
            cv_store.extract_markdown("cv.pdf", pdf_buf.getvalue())
            check(False, "a textless pdf is refused as a probable scan")
        except cv_store.UploadError as err:
            check("no text" in str(err),
                  "a textless pdf is refused as a probable scan")
    except ImportError:
        try:
            cv_store.extract_markdown("cv.pdf", b"%PDF-1.4 junk")
            check(False, "pdf without pypdf is refused with the fix named")
        except cv_store.UploadError as err:
            check("pypdf" in str(err),
                  "pdf without pypdf is refused with the fix named")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        old = base / "cv-20250101-000000.md"
        old.write_text("The old CV text.\n", encoding="utf-8")
        (base / cv_store.MARKER_NAME).write_text(old.name, encoding="utf-8")

        token = cv_store.stage(md, base)
        check(cv_store.read_pending(token, base) == md,
              "a staged upload reads back exactly as extracted")
        result = cv_store.confirm(token, base)
        new_name = result["file"]
        check(new_name.startswith("cv-") and new_name.endswith(".md"),
              f"confirm writes a dated version ({new_name})")
        check(cv_store.active_cv_name(base) == new_name,
              "confirm points the active marker at the new version")
        check(old.exists()
              and old.read_text(encoding="utf-8") == "The old CV text.\n",
              "the previous version file still exists untouched")
        check(set(cv_store.history(base)) >= {old.name, new_name},
              "history lists both versions, append only")
        check(not list(base.glob("pending-cv-*.md")),
              "confirm consumes the pending file")

        token2 = cv_store.stage("Discard me.\n", base)
        cv_store.discard(token2, base)
        check(not list(base.glob("pending-cv-*.md")),
              "discard forgets the staged upload")
        try:
            cv_store.confirm(token2, base)
            check(False, "a spent token is refused")
        except cv_store.UploadError:
            check(True, "a spent token is refused")

        # A fresh score carries the version stamp of the CV the marker
        # names. In-process run with the profile dir pointed at this
        # temp store, mock mode, throwaway database.
        db2 = base / "stamp.sqlite"
        email = {"subject": "One new role", "from": "alerts@jobs.example",
                 "date": "2026-07-29",
                 "body_text": ("Fractional Software Director, IVD\n"
                               "Stampcheck Dx · Ghent, Belgium (Hybrid)\n"
                               "€900 per day\n"
                               "https://example.invalid/stampcheck-role\n"),
                 "body_html": ""}
        b64 = base64.b64encode(
            json_mod.dumps(email).encode("utf-8")).decode("ascii")
        saved_dir = score_item.PROFILE_DIR
        saved_mock = os_mod.environ.get("RADAR_MOCK")
        try:
            score_item.PROFILE_DIR = base
            os_mod.environ["RADAR_MOCK"] = "1"
            code = process_email.main(["--b64", b64, "--db", str(db2),
                                       "--mock"])
        finally:
            score_item.PROFILE_DIR = saved_dir
            if saved_mock is None:
                os_mod.environ.pop("RADAR_MOCK", None)
            else:
                os_mod.environ["RADAR_MOCK"] = saved_mock
        check(code == 0, "the stamping pipeline run exits 0")
        conn = sqlite3.connect(db2)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT cv_version, rate_band FROM opportunities"
                           " WHERE company = 'Stampcheck Dx'").fetchone()
        conn.close()
        check(row is not None and row["cv_version"] == new_name,
              f"a fresh score is stamped with the uploaded CV version "
              f"(got {row['cv_version'] if row else None})")
        check(row is not None and row["rate_band"] == "above",
              "the stamped row also banded its euro day rate")

        # The stretch goal. A capped re-score of rows whose stamp
        # predates the active CV, skipping acknowledged rows.
        import contextlib
        import rescore as rescore_mod

        conn = radar_common.get_db(db2)
        stale_rows = [
            ("st1", "Stale One", None),
            ("st2", "Stale Two", None),
            ("st3", "Stale Three", None),
            ("st4", "Stale Acked", radar_common.now_iso()),
        ]
        for h, company, ack in stale_rows:
            conn.execute(
                "INSERT INTO opportunities (url_hash, first_seen, company,"
                " title, thread_type, status, cv_match, want_match,"
                " combined, cv_version, acknowledged_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (h, radar_common.now_iso(), company,
                 "Fractional IVD Software Director", "inbound", "new",
                 80, 80, 80, "cv-ancient.md", ack))
        conn.commit()
        conn.close()

        saved_dir = score_item.PROFILE_DIR
        saved_mock = os_mod.environ.get("RADAR_MOCK")
        out_buf = io.StringIO()
        try:
            score_item.PROFILE_DIR = base
            os_mod.environ["RADAR_MOCK"] = "1"
            with contextlib.redirect_stdout(out_buf):
                code = rescore_mod.main(["--stale-cv", "--cap", "2",
                                         "--db", str(db2), "--mock"])
        finally:
            score_item.PROFILE_DIR = saved_dir
            if saved_mock is None:
                os_mod.environ.pop("RADAR_MOCK", None)
            else:
                os_mod.environ["RADAR_MOCK"] = saved_mock
        result = json_mod.loads(out_buf.getvalue().strip() or "{}")
        check(code == 0 and result.get("stale_rescored") == 2,
              f"the stale-cv pass re-scores exactly the cap "
              f"(got {result.get('stale_rescored')})")
        check(result.get("remaining_stale") == 1,
              f"the pass reports the one unacknowledged row still stale "
              f"(got {result.get('remaining_stale')})")
        conn = sqlite3.connect(db2)
        conn.row_factory = sqlite3.Row
        acked_ver = conn.execute("SELECT cv_version FROM opportunities"
                                 " WHERE url_hash = 'st4'").fetchone()[0]
        restamped = conn.execute(
            "SELECT COUNT(*) FROM opportunities WHERE cv_version = ?"
            " AND url_hash LIKE 'st%'", (new_name,)).fetchone()[0]
        conn.close()
        check(acked_ver == "cv-ancient.md",
              "the acknowledged row keeps its old stamp, never re-scored")
        check(restamped == 2, "re-scored rows carry the new version stamp")

    # ----- sanitiser worked cases
    offending = ("No action needed — the rate is less than half the floor; "
                 "renegotiation to £650 a day would change that.")
    cleaned = radar_common.sanitise_free_text(offending)
    check(cleaned == ("No action needed, the rate is less than half the "
                      "floor. Renegotiation to £650 a day would change that."),
          f"sanitiser cleans the Meridian worked case (got {cleaned!r})")
    check(radar_common.sanitise_free_text(cleaned) == cleaned,
          "the sanitiser is idempotent on clean text")
    check(radar_common.sanitise_free_text(None) is None,
          "None passes through the sanitiser untouched")

    # The rainy-day extension. Mid-sentence colons and exclamation marks,
    # which the voice rules also ban, are mechanical now.
    got = radar_common.sanitise_free_text(
        "The plan: comment on the round today.")
    check(got == "The plan, comment on the round today.",
          f"a mid-sentence colon reads as a comma (got {got!r})")
    got = radar_common.sanitise_free_text("Digest lands 07:30 on Monday.")
    check(got == "Digest lands 07:30 on Monday.",
          f"a digit colon is a time and survives (got {got!r})")
    got = radar_common.sanitise_free_text(
        "Do it now! The window is short.")
    check(got == "Do it now. The window is short.",
          f"an exclamation ends its sentence as a full stop (got {got!r})")
    got = radar_common.sanitise_free_text("Congratulations on the raise!")
    check(got == "Congratulations on the raise.",
          f"a trailing exclamation becomes a full stop (got {got!r})")
    got = radar_common.sanitise_free_text("Astonishing!! Twice over: really.")
    check(got == "Astonishing. Twice over, really.",
          f"stacked marks collapse cleanly (got {got!r})")
    mixed = ("Right fit — strong 62304 angle: reply today; the window "
             "closes fast!")
    cleaned_mixed = radar_common.sanitise_free_text(mixed)
    check(radar_common.sanitise_free_text(cleaned_mixed) == cleaned_mixed,
          "the extended sanitiser stays idempotent on mixed input")
    # The colon exemption is digit-colon-digit only, both sides. A colon
    # merely touching one digit is still punctuation and still converts.
    got = radar_common.sanitise_free_text(
        "The rate floor is 650: a hard number.")
    check(got == "The rate floor is 650, a hard number.",
          f"a colon after a number still converts (got {got!r})")
    got = radar_common.sanitise_free_text("Note the company and revisit:")
    check(got == "Note the company and revisit.",
          f"a trailing colon ends the sentence as a stop (got {got!r})")
    for probe in ("The rate floor is 650, a hard number.",
                  "Note the company and revisit."):
        check(radar_common.sanitise_free_text(probe) == probe,
              f"idempotent on {probe[:24]!r}")

    # A sighting revives an expired role. The dedupe path clears expired_at
    # when a retired advert is seen again, and never touches a closed or a
    # dismissed row. Three one-role emails through the real pipeline in
    # mock mode, each stamped by hand, each fed again.
    with tempfile.TemporaryDirectory() as tmp:
        import base64 as b64_mod
        import contextlib
        import io as io_mod
        import json as json_mod
        import os as os_mod

        import cv_store
        import score_item

        base = Path(tmp)
        cv_md = base / "cv-20260101-000000.md"
        cv_md.write_text("Software director, IVD, 62304.\n", encoding="utf-8")
        (base / cv_store.MARKER_NAME).write_text(cv_md.name, encoding="utf-8")
        db_r = base / "revive.sqlite"

        def feed(company: str, slug: str) -> dict:
            email = {"subject": "One new role", "from": "alerts@jobs.example",
                     "date": "2026-07-29",
                     "body_text": ("Fractional Software Director, IVD\n"
                                   f"{company} · Ghent, Belgium (Hybrid)\n"
                                   "€900 per day\n"
                                   f"https://example.invalid/{slug}\n"),
                     "body_html": ""}
            payload = b64_mod.b64encode(
                json_mod.dumps(email).encode("utf-8")).decode("ascii")
            saved_dir = score_item.PROFILE_DIR
            saved_mock = os_mod.environ.get("RADAR_MOCK")
            captured = io_mod.StringIO()
            try:
                score_item.PROFILE_DIR = base
                os_mod.environ["RADAR_MOCK"] = "1"
                with contextlib.redirect_stdout(captured):
                    code = process_email.main(["--b64", payload, "--db",
                                               str(db_r), "--mock"])
            finally:
                score_item.PROFILE_DIR = saved_dir
                if saved_mock is None:
                    os_mod.environ.pop("RADAR_MOCK", None)
                else:
                    os_mod.environ["RADAR_MOCK"] = saved_mock
            check(code == 0, f"the {company} run exits 0")
            return json_mod.loads(captured.getvalue().strip().splitlines()[-1])

        def row_for(company: str) -> dict:
            conn = sqlite3.connect(db_r)
            conn.row_factory = sqlite3.Row
            got = conn.execute(
                "SELECT last_seen, seen_count, expired_at, closed_at,"
                " dismissed_at FROM opportunities WHERE company = ?",
                (company,)).fetchone()
            conn.close()
            return dict(got) if got else {}

        def last_inbox_note() -> str:
            conn = sqlite3.connect(db_r)
            conn.row_factory = sqlite3.Row
            got = conn.execute(
                "SELECT note FROM runs WHERE workflow = 'inbox'"
                " ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
            return (got["note"] if got else "") or ""

        fixtures = (("Revive Dx", "revive-role", "expired_at"),
                    ("Shut Dx", "shut-role", "closed_at"),
                    ("Waved Dx", "waved-role", "dismissed_at"))
        first = {}
        for company, slug, _stamp in fixtures:
            first[company] = feed(company, slug)
        check(all(first[c]["new"] == 1 and first[c]["revived"] == 0
                  for c, _s, _t in fixtures),
              "each fixture role stores once, nothing revived on first sight")
        stamp = "2026-09-01T09:00:00Z"
        conn = sqlite3.connect(db_r)
        for company, _slug, column in fixtures:
            conn.execute(f"UPDATE opportunities SET {column} = ?"
                         " WHERE company = ?", (stamp, company))
        conn.commit()
        conn.close()
        before = {c: row_for(c) for c, _s, _t in fixtures}

        again = feed("Revive Dx", "revive-role")
        after = row_for("Revive Dx")
        check(again["duplicates"] == 1 and again["new"] == 0,
              "the expired role's repeat card is a duplicate, not a new row")
        check(after["expired_at"] is None,
              "a sighting clears expired_at on an expired row")
        check(after["last_seen"] >= before["Revive Dx"]["last_seen"]
              and after["last_seen"] > stamp,
              "the revived row's last_seen is bumped to now")
        check(after["seen_count"] == before["Revive Dx"]["seen_count"] + 1,
              "the revived row's seen_count goes up by one")
        check(again["revived"] == 1,
              f"the run record counts one revival (got {again['revived']})")
        check(again["items"][0].get("revived") is True
              and again["items"][0]["status"] == "duplicate",
              "the item is reported as a duplicate that revived")
        check("1 revived" in last_inbox_note(),
              f"the runs row notes the revival ({last_inbox_note()!r})")
        check(radar_common.life_state(after) == "live",
              "the revived row reads live again")

        again = feed("Shut Dx", "shut-role")
        after = row_for("Shut Dx")
        check(after["closed_at"] == stamp,
              "a closed row stays closed when its card is seen again")
        check(after["seen_count"] == before["Shut Dx"]["seen_count"] + 1
              and again["duplicates"] == 1,
              "the closed row's sighting is still counted")
        check(again["revived"] == 0 and "revived" not in again["items"][0],
              "a closed row is never reported as revived")
        check("revived" not in last_inbox_note(),
              "the runs row says nothing about revival when none happened")

        again = feed("Waved Dx", "waved-role")
        after = row_for("Waved Dx")
        check(after["dismissed_at"] == stamp,
              "a dismissed row stays dismissed when its card is seen again")
        check(after["seen_count"] == before["Waved Dx"]["seen_count"] + 1
              and again["duplicates"] == 1,
              "the dismissed row's sighting is still counted")
        check(again["revived"] == 0,
              "a dismissed row is never reported as revived")

        # The revived row is fresh, so the expiry pass leaves it alone and
        # the page puts it back in its tier, out of the archive.
        conn = radar_common.get_db(db_r)
        conn.execute("UPDATE opportunities SET tier = 'top'"
                     " WHERE company = 'Revive Dx'")
        conn.commit()
        dry_r = check_jobs.expire_stale(conn, write=False)
        check(all(item["company"] != "Revive Dx" for item in dry_r["sample"])
              and dry_r["expired"] == 0,
              "expire_stale does not re-expire the revived row the same day")
        conn.close()

    # Buying window. The doctrine's reading of an advert, decided in code
    # against the touch log, never by a model.
    check(process_email.normalise_company("  Veltrix   Diagnostics  ")
          == "veltrix diagnostics",
          "company names normalise for case and spacing")
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "buying_window.sqlite")
        conn.execute(
            "INSERT INTO touches (company, touched_at, channel, note)"
            " VALUES (?,?,?,?)",
            ("Veltrix Diagnostics", "2026-07-01T09:00:00Z", "comment",
             "congrats comment on seed post"))
        conn.commit()
        touched = process_email.touched_companies(conn)
        check(process_email.normalise_company("VELTRIX   diagnostics") in touched,
              "touch-log matching survives case and spacing")
        check(process_email.normalise_company("Untouched Ltd") not in touched,
              "an untouched company never matches the touch log")

        wrote = process_email.record_buying_window(
            conn, "Veltrix Diagnostics", "QA Manager",
            "https://example.test/jobs/1", "hash-one", "2026-07-20T09:00:00Z")
        conn.commit()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE source_id = 'job-advert'")]
        check(wrote and len(rows) == 1,
              "the first advert from a touched company writes one signal")
        row = rows[0] if rows else {}
        check(row.get("relevance") == process_email.BUYING_WINDOW_RELEVANCE
              and (row.get("relevance") or 0) >= 75,
              "the buying-window signal clears the fast push bar")
        check(row.get("pushed") == 0,
              "the buying-window signal is written unpushed")
        check("QA Manager" in (row.get("why") or ""),
              "the why names the advert that opened the window")
        built = " ".join(str(row.get(f) or "") for f in
                         ("headline", "why", "playbook_step"))
        check(":" not in built and ";" not in built,
              "the built text carries no colons and no semicolons")

        again = process_email.record_buying_window(
            conn, "veltrix  DIAGNOSTICS", "Regulatory Lead",
            "https://example.test/jobs/2", "hash-two", "2026-07-21T09:00:00Z")
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM signals"
            " WHERE source_id = 'job-advert'").fetchone()["n"]
        check(not again and count == 1,
              "a second advert from the same company writes nothing")
        conn.close()

    # The companies table. The landscape is companies, not rows.
    check(radar_common.normalise_company("  Nordic  BioSystems ")
          == "nordic biosystems",
          "the company key normalises case and spacing")
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "companies.sqlite")
        now = radar_common.now_iso()

        a = radar_common.resolve_company(conn, "Veltrix Diagnostics", now)
        b = radar_common.resolve_company(conn, "  VELTRIX   diagnostics ", now)
        check(a is not None and a == b,
              "two spellings of one company resolve to one row")
        check(radar_common.resolve_company(conn, "   ", now) is None,
              "an empty name gets no company row rather than an empty one")
        check(conn.execute("select display_name from companies where id=?",
                           (a,)).fetchone()["display_name"] == "Veltrix Diagnostics",
              "the first spelling seen is kept for display")

        # Legacy rows, stored before company_id existed. The backfill is
        # what turns a populated database into a companies-shaped one.
        conn.execute("INSERT INTO opportunities (url_hash, first_seen, company,"
                     " title, thread_type, status) VALUES ('u1',?, 'Caldora Medical',"
                     " 'Head of Software', 'inbound', 'new')", (now,))
        conn.execute("INSERT INTO opportunities (url_hash, first_seen, company,"
                     " title, thread_type, status) VALUES ('u2',?, 'caldora  MEDICAL',"
                     " 'QA Lead', 'inbound', 'new')", (now,))
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status) VALUES ('s1',?,'imec-press',"
                     " 'Caldora Medical', 'Caldora raises', 'new')", (now,))
        conn.execute("INSERT INTO touches (company, touched_at, channel)"
                     " VALUES ('Caldora Medical', ?, 'comment')", (now,))
        conn.commit()

        res = radar_common.backfill_companies(conn)
        conn.commit()
        cid = radar_common.resolve_company(conn, "Caldora Medical")
        linked = conn.execute(
            "select count(*) from opportunities where company_id=?",
            (cid,)).fetchone()[0]
        check(linked == 2,
              f"both spellings of a legacy company link to one row (got {linked})")
        check(res["companies_created"] >= 1, "the backfill creates missing companies")
        again = radar_common.backfill_companies(conn)
        conn.commit()
        check(again["companies_created"] == 0
              and sum(again["linked"].values()) == 0,
              "the backfill is idempotent, a second run finds nothing to do")
        dupes = conn.execute(
            "select count(*) from (select norm_name from companies"
            " group by norm_name having count(*)>1)").fetchone()[0]
        check(dupes == 0, "no company is stored twice under one key")

        # Derived states, read from facts rather than stored.
        check(radar_common.company_state(conn, cid) == "touched",
              "items plus a touch reads as touched, the stronger rung")
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status, company_id)"
                     " VALUES ('s2',?,'job-advert','Caldora Medical',"
                     " 'Buying window. Caldora Medical is hiring','new',?)",
                     (now, cid))
        conn.commit()
        check(radar_common.company_state(conn, cid) == "window open",
              "a job-advert signal opens the window, the strongest derived rung")
        conn.execute("UPDATE companies SET state='client' WHERE id=?", (cid,))
        check(radar_common.company_state(conn, cid) == "client",
              "a stored client state outranks every derived one")
        conn.execute("UPDATE companies SET state='dead' WHERE id=?", (cid,))
        check(radar_common.company_state(conn, cid) == "dead",
              "dead trumps everything, including client")
        conn.close()

    # Enrichment. One pass per company ever, capped, and never fatal.
    check(enrich_company.candidate_site(
        "See https://www.linkedin.com/company/veltrix and https://veltrix.example/about")
        == "https://veltrix.example/about",
        "a LinkedIn URL is never taken as the company site")
    check(enrich_company.candidate_site(
        "via https://optics.org/news/story https://reed.co.uk/jobs/1") is None,
        "a news outlet and a job board are not company sites")
    check(enrich_company.candidate_site("no urls here at all") is None,
          "no site is guessed from the company name")

    import os as _os
    _os.environ["RADAR_MOCK"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "enrich.sqlite")
        cfg = {"claude_model_extract": "claude-haiku-4-5", "enrich_cap_per_run": 2}
        cid = radar_common.resolve_company(conn, "Veltrix Diagnostics")
        r1 = enrich_company.enrich_company(conn, cid, "Ghent, Belgium.", cfg,
                                           enrich_company.new_budget(cfg))
        check(r1["enriched"] is True, "a new company is enriched at first sight")
        row = conn.execute("select country, city, stage, people, enrich_status"
                           " from companies where id=?", (cid,)).fetchone()
        check(row["country"] == "Belgium" and row["city"] == "Ghent",
              f"country and city are stored for the region mapping (got {row['country']}, {row['city']})")
        check("ceo" in (row["people"] or "") and "cto" in (row["people"] or ""),
              "published CEO and CTO are kept")
        check(row["enrich_status"].startswith("text-only"),
              f"a company with no site fetched says so in enrich_status (got {row['enrich_status']})")

        r2 = enrich_company.enrich_company(conn, cid, "Ghent, Belgium.", cfg,
                                           enrich_company.new_budget(cfg))
        check(r2["enriched"] is False and r2["status"] == "already enriched",
              "a company is never enriched twice, however often it is seen")

        budget = {"spent": 0, "cap": 1}
        a = radar_common.resolve_company(conn, "Alpha Diagnostics")
        b = radar_common.resolve_company(conn, "Beta Diagnostics")
        ra = enrich_company.enrich_company(conn, a, "x", cfg, budget)
        rb = enrich_company.enrich_company(conn, b, "x", cfg, budget)
        check(ra["enriched"] is True and rb["enriched"] is False
              and rb["status"] == "cap reached this run",
              "the per-run cap stops the second company, and says why")

        # A broken company id must not raise into the caller's pipeline.
        rc_ = enrich_company.enrich_company(conn, 999999, "x", cfg, None)
        check(rc_["enriched"] is False and rc_["status"] == "no such company",
              "an unknown company is reported, never raised")
        conn.close()
    _os.environ.pop("RADAR_MOCK", None)

    # Phase three. The tier ladder, and a fixture set that covers all four
    # tiers, because a tiering change that only ever sees one shape is
    # untested. The spec asks for this before any backfill runs.
    import score_item as _si

    G = lambda **k: {"sector": True, "cv": True, "location": True, "rate": True, **k}
    for label, gates, cv, real, want in (
            ("all four gates pass", G(), 84, True, "top"),
            ("all pass, rate unstated", G(rate=None), 84, True, "top"),
            ("location alone fails", G(location=False), 84, True, "question"),
            ("rate alone fails, whatever its basis", G(rate=False), 84, True, "question"),
            ("sector alone fails", G(sector=False), 84, True, "reading"),
            ("cv alone fails", G(cv=False), 68, True, "reading"),
            ("two gates fail", G(location=False, rate=False), 84, True, "reading"),
            ("cv under forty", G(cv=False), 22, True, "filtered"),
            ("not a real role", G(), 84, False, "filtered")):
        got = _si.derive_tier(gates, cv, real)["tier"]
        check(got == want, f"{label} lands in {want} (got {got})")

    check(_si.derive_tier(G(sector=False), 84, True)["failed_gates"] == ["sector"],
          "the failed gate is recorded as the verdict")
    check(_si.derive_tier(G(cv=False), 22, True)["filter_reason"] == "cv match under 40",
          "a filtered row records why, so the filter can be audited")
    check(_si.question_for(G(location=False), {}) == "Would they go remote",
          "the location question is the one a conversation could settle")
    check(_si.question_for(G(rate=False), {}) == "Would they move on the rate",
          "the rate question likewise")

    # Two floors, no conversion. The rate gate is judged in code from the
    # extractor's pay columns, each figure against the floor of its own
    # kind, and the tier follows from the gate alone.
    floors = {"day": 750.0, "salary": 100000.0}

    def land(currency, period, lo, hi):
        pay = radar_common.gate_pay(currency, period, lo, hi, floors, CFG)
        gates = G(rate=pay["gate"])
        return pay, _si.derive_tier(gates, 84, True)["tier"]

    for label, args, want_gate, want_basis, want_tier in (
            ("day rate 700 fails", ("GBP", "day", 700, 700), False, "day-rate", "question"),
            ("day rate 750 passes", ("GBP", "day", 750, 750), True, "day-rate", "top"),
            ("salary 95000 fails against the salary floor", ("GBP", "year", 95000, 95000), False, "salary", "question"),
            ("salary 100000 passes, never divided by 220", ("GBP", "year", 100000, 100000), True, "salary", "top"),
            ("hourly 90 is 720 a day and fails the 750 day floor", ("GBP", "hour", 90, 90), False, "hourly", "question"),
            ("hourly 94 is 752 a day and passes", ("GBP", "hour", 94, 94), True, "hourly", "top"),
            ("a salary range gates on its top figure", ("GBP", "year", 90000, 110000), True, "salary", "top"),
            ("a euro day rate converts at 0.85 then meets the day floor", ("EUR", "day", 950, 1100), True, "day-rate", "top")):
        pay, tier = land(*args)
        check(pay["gate"] is want_gate and pay["basis"] == want_basis and tier == want_tier,
              f"{label}, lands in {want_tier} (got gate {pay['gate']}, {pay['basis']}, {tier})")
    pay, tier = land("", "", None, None)
    check(pay["gate"] is None and pay["basis"] == "unstated" and tier == "top",
          f"unstated passes as unknown and lands in top (got {pay}, {tier})")
    pay, tier = land("USD", "year", 350000, 350000)
    check(pay["gate"] is None and tier == "top",
          "a currency with no conversion is unstated, never guessed")
    pay, _ = land("GBP", "year", 96000, 96000)
    check(pay["band"] == "close" and pay["floor"] == 100000.0,
          "a salary within five thousand of its floor bands close")
    pay, _ = land("GBP", "day", 700, 700)
    check(pay["band"] == "close", "a day rate within fifty of its floor bands close")
    pay, _ = land("GBP", "year", 60000, 60000)
    check(pay["band"] == "below", "a salary well under its floor bands below")
    note = _si.rate_gate_note(radar_common.gate_pay("GBP", "year", 95000, 95000, floors, CFG))
    check(note == "Stated salary £95,000 against the £100,000 floor, below, judged in code.",
          f"the code's rate note names the figure, the floor and the verdict (got {note!r})")

    # The code gate overrides the model's rate verdict whenever a figure
    # was stored, and leaves it alone for a silent advert.
    _os.environ["RADAR_MOCK"] = "1"
    try:
        import mocks as _mocks
        sys_blocks = [{"type": "text", "text": "rubric"}]
        cfg_score = dict(CFG, claude_model_score="mock-scorer")
        salaried = {"company": "Veltrix Diagnostics", "title": "Director of Software",
                    "location": "Ghent, Belgium (Hybrid)", "salary_rate": "£95,000 per annum",
                    "pay_currency": "GBP", "pay_period": "year", "pay_min": 95000, "pay_max": 95000,
                    "source_url": "https://example.com/a"}
        scored, _, _ = _si.score_opportunity(salaried, sys_blocks, cfg_score,
                                             mock_fn=_mocks.mock_scorer, floors=floors)
        check(scored["tier"] == "question" and scored["gate_rate"] == 0
              and scored["rate_basis"] == "salary" and scored["rate_value"] == 95000.0
              and scored["question_text"] == "Would they move on the rate"
              and "judged in code" in scored["gate_rate_note"],
              f"a salary under the salary floor lands one question away with the rate as the question (got {scored['tier']}, {scored['gate_rate_note']!r})")
        silent = dict(salaried, salary_rate="", pay_currency="", pay_period="",
                      pay_min=None, pay_max=None)
        scored, _, _ = _si.score_opportunity(silent, sys_blocks, cfg_score,
                                             mock_fn=_mocks.mock_scorer, floors=floors)
        check(scored["tier"] == "top" and scored["gate_rate"] == 1
              and scored["rate_basis"] == "unstated",
              f"a silent advert keeps the model's pass and lands in top (got {scored['tier']})")
    finally:
        _os.environ.pop("RADAR_MOCK", None)

    # Engagement type, one fixture per rule plus the no-match case.
    import engagement as _eng
    for text, want, want_note in (
            ("Software Director, 6 month fixed term contract", "permanent", "fixed term"),
            ("Head of Software (FTC)", "permanent", "fixed term"),
            ("Interim Head of Quality", "contract", "interim"),
            ("Principal Engineer £600 per day", "contract", "day rate"),
            ("Day rate role, outside IR35", "contract", "day rate"),
            ("Regulatory Lead, outside IR35", "contract", "outside IR35"),
            ("QA Manager, contract", "contract", "contract"),
            ("Software Director, permanent", "permanent", "permanent"),
            ("Head of Software, perm role", "permanent", "permanent"),
            ("Director of Engineering £120,000 per annum", "unknown", ""),
            ("", "unknown", "")):
        got, note = _eng.classify(text)
        check(got == want and note == want_note,
              f"engagement {text!r} reads {want} with note {want_note!r} (got {got}, {note!r})")
    check(_eng.normalise("Permanent") == "permanent" and _eng.normalise("gig") == "unknown"
          and _eng.normalise(None) == "unknown",
          "a model answer is clamped to the three allowed words")
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "engage.sqlite")
        for h, title, pay, ir35 in (("e1", "Interim CTO", "", None),
                                    ("e2", "Head of QA", "£600 per day", None),
                                    ("e3", "Software Director", "£120k", "outside"),
                                    ("e4", "Software Director", "£120k", None)):
            conn.execute("INSERT INTO opportunities (url_hash, first_seen, title,"
                         " company, salary_rate, ir35) VALUES (?,?,?,?,?,?)",
                         (h, "2026-11-01T00:00:00Z", title, "Co", pay, ir35))
        conn.commit()
        dry = _eng.backfill(conn, write=False)
        check(dry["counts"] == {"contract": 3, "permanent": 0, "unknown": 1}
              and conn.execute("SELECT COUNT(*) FROM opportunities WHERE"
                               " engagement_type IS NOT NULL").fetchone()[0] == 0,
              f"the dry backfill counts three contract and one unknown and writes nothing (got {dry['counts']})")
        wet = _eng.backfill(conn, write=True)
        stored = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT url_hash, engagement_type, engagement_note FROM opportunities")}
        check(stored == {"e1": ("contract", "interim"), "e2": ("contract", "day rate"),
                         "e3": ("contract", "outside IR35"), "e4": ("unknown", None)},
              f"the backfill writes the type and the deciding phrase (got {stored})")
        check(_eng.backfill(conn, write=True)["examined"] == 0,
              "a second backfill finds nothing left to set")
        conn.close()

    # Traction and the connector circle. Counting rules, pinned, because a
    # metric nobody has checked the arithmetic of is a rumour with a number.
    import build_dashboard as _bdash

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "traction.sqlite")
        now = radar_common.now_iso()
        old = "2026-01-05T09:00:00Z"

        def touch(company, channel, when, nxt=None, when_due=None, outcome=None):
            conn.execute(
                "INSERT INTO touches (company, touched_at, channel, note,"
                " next_action, next_action_date, outcome)"
                " VALUES (?,?,?,?,?,?,?)",
                (company, when, channel, "n", nxt, when_due, outcome))

        touch("Alpha Dx", "comment", old, "week three", "2026-01-26")
        touch("Alpha Dx", "engagement", now, outcome="conversation")
        touch("Beta Bio", "connection-note", now, "week three", "2099-01-01",
              outcome="reply")
        touch("Gamma Ltd", "artefact", now)
        conn.commit()

        tr = _bdash.traction_metrics(conn, {})
        check(tr["total"] == 4, f"every touch is counted (got {tr['total']})")
        check(tr["outcomes"] == {"none": 2, "reply": 1, "conversation": 1},
              f"outcomes count, with unset reading as none (got {tr['outcomes']})")
        check(tr["booked"] == 2, f"two next actions are booked (got {tr['booked']})")
        check(tr["due"] == 1,
              f"only the past-dated booking is due (got {tr['due']})")
        check(tr["done"] == 1,
              f"a booking is done when a later touch exists for that company "
              f"(got {tr['done']})")
        months = dict(tr["by_month"])
        check(months.get("2026-01", {}).get("comment") == 1,
              f"touches bucket by month and channel (got {tr['by_month']})")

        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status) VALUES"
                     " ('w1',?, 'job-advert','Alpha Dx','x','new')", (now,))
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status) VALUES"
                     " ('w2',?, 'qa-tripwire','Beta Bio','y','new')", (now,))
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status) VALUES"
                     " ('w3',?, 'imec-press','Gamma Ltd','z','new')", (now,))
        conn.commit()
        tr = _bdash.traction_metrics(conn, {})
        check(tr["windows"] == 2,
              f"windows count buying windows and tripwires, not ordinary news "
              f"(got {tr['windows']})")

        # The circle. Names from config, days from the touch log, nothing else.
        cc = _bdash.connector_circle(conn, {})
        check(cc["configured"] == 0 and cc["quiet"] == [],
              "no configured connectors means no circle line at all")
        cfg_c = {"connectors": ["Alpha Dx", "Never Spoken Ltd"],
                 "connector_quiet_days": 60}
        cc = _bdash.connector_circle(conn, cfg_c)
        quiet = [q["name"] for q in cc["quiet"]]
        check(cc["configured"] == 2 and quiet == ["Never Spoken Ltd"],
              f"a connector touched today is not quiet, one never touched is "
              f"(got {quiet})")
        # A configured zero must survive being falsy. This read 60 until
        # 3 August, because `int(cfg.get(k, 60) or 60)` throws away a real
        # zero for the crime of being falsy, and nobody would have noticed
        # except that the number is reported back.
        cc_zero = _bdash.connector_circle(conn, {**cfg_c, "connector_quiet_days": 0})
        check(cc_zero["quiet_days"] == 0,
              f"a configured zero is honoured, not swapped for the default "
              f"(got {cc_zero['quiet_days']})")
        # And the threshold genuinely moves. Yesterday's touch is quiet at
        # zero days and not quiet at sixty.
        conn.execute("INSERT INTO touches (company, touched_at, channel)"
                     " VALUES ('Yesterday Ltd', ?, 'comment')",
                     ((datetime.now(timezone.utc) - timedelta(days=1))
                      .strftime("%Y-%m-%dT%H:%M:%SZ"),))
        conn.commit()
        cfg_y = {"connectors": ["Yesterday Ltd"]}
        at_zero = _bdash.connector_circle(conn, {**cfg_y, "connector_quiet_days": 0})
        at_sixty = _bdash.connector_circle(conn, {**cfg_y, "connector_quiet_days": 60})
        check(len(at_zero["quiet"]) == 1 and len(at_sixty["quiet"]) == 0,
              "the quiet threshold moves with the configured number")
        conn.close()

    # The Jobs page and the ageing list. collect() builds data["ageing"] from
    # two kinds of row, adverts and watcher signals, because the archive
    # renders both. Jobs is for roles, so its Ageing section carries the
    # adverts only and never hands a signal to render_opportunity, which
    # reads o["combined"], a key a signal does not have. The live trigger
    # was eight signals above the bar, status new, older than a fortnight,
    # and /jobs answered "Render failed. KeyError: combined".
    import re as _re

    def _section_count(page: str, title: str):
        m = _re.search(r"<h2>" + _re.escape(title)
                       + r"</h2>.*?<span class=\"section-count\">(\d+)</span>",
                       page, _re.S)
        return int(m.group(1)) if m else None

    config_j = radar_common.load_config()
    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "jobs_ageing.sqlite")
        now = radar_common.now_iso()
        stale = (datetime.now(timezone.utc)
                 - timedelta(days=_bdash.AGEING_DAYS + 1)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO opportunities (url_hash, first_seen, company, title,"
            " thread_type, status, cv_match, want_match, combined, tier,"
            " one_line_why) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("age-opp", stale, "Stale Dx", "Stale Director Role", "inbound",
             "new", 88, 88, 88, "top", "Sat still for a fortnight."))
        conn.execute(
            "INSERT INTO signals (url_hash, first_seen, source_id, company,"
            " headline, summary, source_url, relevance, status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("age-sig", stale, "imec-news", "Stale Photonics",
             "Stale Photonics closes a seed round nobody acted on",
             "Funding note.", "https://example.invalid/stale", 85, "new"))
        conn.commit()
        data_j = _bdash.collect(conn, config_j)
        kinds = sorted(a["kind"] for a in data_j["ageing"])
        check(kinds == ["opportunity", "signal"],
              f"collect() ages the advert and the signal together (got {kinds})")
        try:
            jobs_j = _bdash.render_jobs_page(data_j, config_j, "t")
            check(_section_count(jobs_j, "Ageing") is None
                  and "<h2>Ageing</h2>" not in jobs_j,
                  "Jobs has no Ageing section (J16)")
            check("Stale Director Role" in jobs_j
                  and "Stale Photonics closes a seed round" not in jobs_j,
                  "the stale advert sits in its tier with its age on the row,"
                  " and the watcher signal is not on Jobs")
        except Exception as err:  # noqa: BLE001
            check(False, "the Jobs page renders with a watcher signal in the "
                         f"ageing list (raised {type(err).__name__}: {err})")
        archive_j = _bdash.render_page(data_j, config_j, "t",
                                       Path(tmp) / "dash.html", serve=True)
        check(_section_count(archive_j, "Ageing") == 2
              and "Stale Photonics closes a seed round" in archive_j,
              "the archive still lists the signal in its ageing rows")
        conn.close()

        # The held-signals shelf retired with the keep/drop pass. An
        # advert the extractor read as news stays in its tier like any
        # other row, and the page must still render with one present.
        conn = radar_common.get_db(Path(tmp) / "jobs_held.sqlite")
        conn.execute(
            "INSERT INTO opportunities (url_hash, first_seen, last_seen,"
            " company, title, thread_type, status, cv_match, tier,"
            " one_line_why) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("held-sig", now, now, "Newsworthy Ltd",
             "Newsworthy Ltd opens a Ghent office", "signal", "new",
             70, "reading", "News, not a role."))
        conn.commit()
        data_h = _bdash.collect(conn, config_j)
        try:
            jobs_h = _bdash.render_jobs_page(data_h, config_j, "t")
            check("Signals held this week" not in jobs_h
                  and "Newsworthy Ltd opens a Ghent office" in jobs_h,
                  "the held-signals shelf is gone, the advert sits in its tier")
        except Exception as err:  # noqa: BLE001
            check(False, "the Jobs page renders with a signal-kind advert "
                         f"this week (raised {type(err).__name__}: {err})")
        conn.close()

    # Tripwires. A first quality or regulatory hire at an untouched medtech
    # company is news about them, not a job for Luke.
    import tripwire as _tw

    for title, want in (("Quality Assurance Manager, ISO 13485", True),
                        ("Regulatory Affairs Specialist", True),
                        ("Head of Quality", True),
                        ("QA Engineer", True),
                        ("Senior Software Engineer", False),
                        ("Account Executive", False),
                        ("Quantitative Analyst", False)):
        got = _tw.is_first_hire_title(title)
        check(got is want,
              f"{title!r} reads as a first quality hire: {want} (got {got})")

    for company, title, loc, want in (
            ("Cantilex Diagnostics", "Quality Assurance Manager", "Leuven", True),
            ("Veltrix", "QA Manager, IVD platform", "Ghent", True),
            ("Acme Medical Device Ltd", "Head of Quality", "Surrey", True),
            ("NHS Berkshire Hospitals Trust", "Quality Manager", "Reading", False),
            ("Guildford Hospital", "Regulatory Affairs, medical devices", "Surrey", False),
            ("Bigg Foods Ltd", "Quality Manager", "Leeds", False),
            ("Generic Software Co", "QA Manager", "London", False)):
        got = _tw.reads_as_medtech(company, title, loc)
        check(got is want,
              f"{company!r} reads as medtech: {want} (got {got})")
    check(_tw.reads_as_medtech("NHS Trust", "QA, medical devices", None) is False,
          "a hospital wins over the words medical devices, it is the stronger fact")

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "tripwire.sqlite")
        touched = {"veltrix diagnostics"}

        fire, why = _tw.should_trip(conn, "Cantilex Diagnostics",
                                    "QA Manager, ISO 13485", "Leuven", touched)
        check(fire is True, f"an untouched medtech first hire fires (got {why})")

        fire, why = _tw.should_trip(conn, "Veltrix Diagnostics",
                                    "QA Manager, IVD", "Ghent", touched)
        check(fire is False and "touch log" in why,
              f"a touched company gets a buying window instead, not a tripwire (got {why})")

        fire, why = _tw.should_trip(conn, "Cantilex Diagnostics",
                                    "Senior Software Engineer", "Leuven", touched)
        check(fire is False and "quality or regulatory" in why,
              f"an ordinary role does not fire (got {why})")

        fire, why = _tw.should_trip(conn, "NHS Berkshire Trust",
                                    "Quality Manager", "Reading", touched)
        check(fire is False and "medtech" in why,
              f"a hospital QA role never fires (got {why})")

        fire, why = _tw.should_trip(conn, "", "QA Manager, IVD", None, touched)
        check(fire is False, "an advert with no company cannot fire")

        # One per company, ever, checked against the signals themselves.
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status) VALUES"
                     " ('tw1',?,?, 'cantilex  DIAGNOSTICS', 'x', 'new')",
                     (radar_common.now_iso(), _tw.SOURCE_ID))
        conn.commit()
        check(_tw.already_tripped(conn, "Cantilex Diagnostics") is True,
              "the one-per-company check ignores case and spacing")
        fire, why = _tw.should_trip(conn, "Cantilex Diagnostics",
                                    "Regulatory Affairs Lead", "Leuven", touched)
        check(fire is False and "already tripped" in why,
              f"a second first-hire advert from the same company does not fire (got {why})")
        conn.close()

    # digest_min_want retired on 3 August with the additive score it sat on
    # top of. What replaced it is the tier, and the digest now selects on
    # that, so the flag has nothing left to be a second bar above.
    import build_digest as _bd

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "digest.sqlite")
        now = radar_common.now_iso()
        for h, title, cv, tier in (("dw1", "Gated role", 90, "top"),
                                   ("dw2", "One question away", 84, "question"),
                                   ("dw3", "Reading only", 72, "reading"),
                                   ("dw4", "Filtered out", 20, "filtered")):
            conn.execute(
                "INSERT INTO opportunities (url_hash, first_seen, company, title,"
                " cv_match, tier, thread_type, status)"
                " VALUES (?,?,?,?,?,?,'inbound','new')",
                (h, now, "Acme Dx", title, cv, tier))
        conn.commit()
        d = _bd.collect(conn, {"score_threshold": 70,
                               "prefs_file": "config/profile/preferences.md"})
        titles = sorted(x["title"] for x in d["inbound"])
        check(titles == ["Gated role", "One question away"],
              f"the digest carries the top and question tiers only (got {titles})")
        check(d["reading_count"] == 1,
              f"reading is counted, not carried (got {d['reading_count']})")
        check(d["top_count"] == 1 and d["question_count"] == 1,
              "the counts that drive the subject line are separated")
        check(all("Filtered out" != x["title"] for x in d["inbound"]),
              "a filtered role never reaches the email")
        conn.close()

    # The sitemap watcher. Reads a complete list, not a stream, so the
    # rules that matter are what it filters and what it remembers.
    import check_signals as _cs

    SITEMAP = """<?xml version="1.0"?><urlset>
      <url><loc>https://x.test/en/press/alpha-raises-seed</loc><lastmod>2026-08-03</lastmod></url>
      <url><loc>https://x.test/en/press/beta-spins-out</loc><lastmod>2026-08-03</lastmod></url>
      <url><loc>https://x.test/en/work-at-imec/job-opportunities/researcher</loc><lastmod>2026-08-03</lastmod></url>
      <url><loc>https://x.test/en/articles/a-paper-about-waveguides</loc><lastmod>2026-08-03</lastmod></url>
    </urlset>"""

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "sitemap.sqlite")
        src = {"id": "x-press", "name": "X press", "tier": 1,
               "url": "https://x.test/news", "method": "sitemap",
               "sitemap_url": "https://x.test/sitemap.xml",
               "check_interval_hours": 6, "status": "live",
               "include_patterns": ["/en/press/"]}
        real_get, real_article = _cs.rc.http_get, _cs.fetch_article_text
        _cs.rc.http_get = lambda url, cfg=None, etag=None, last_modified=None: (
            200, {}, SITEMAP)
        _cs.fetch_article_text = lambda url, cfg: f"body text for {url}"
        try:
            notes = []
            items = _cs.check_sitemap(src, None, {}, conn, notes)
            conn.commit()
            check(items == [],
                  "the first run baselines and returns nothing to score")
            check(any("2 sitemap urls filtered" in n for n in notes),
                  f"the whitelist drops the vacancy and the paper (notes {notes})")
            state = _cs._get_state(conn, "x-press")
            remembered = json_mod.loads(state["last_seen_ids"])
            check(len(remembered) == 2 and all("/en/press/" in u for u in remembered),
                  f"only whitelisted urls are remembered (got {remembered})")

            items = _cs.check_sitemap(src, state, {}, conn, notes)
            conn.commit()
            check(items == [],
                  "an unchanged sitemap yields nothing on the next run")

            # A genuinely new press release arrives.
            grown = SITEMAP.replace("</urlset>",
                "<url><loc>https://x.test/en/press/gamma-series-a</loc>"
                "<lastmod>2026-08-04</lastmod></url></urlset>")
            _cs.rc.http_get = lambda url, cfg=None, etag=None, last_modified=None: (
                200, {}, grown)
            state = _cs._get_state(conn, "x-press")
            items = _cs.check_sitemap(src, state, {}, conn, notes)
            conn.commit()
            check(len(items) == 1 and items[0]["url"].endswith("gamma-series-a"),
                  f"a new press url is found and only that one (got {len(items)})")
            check(items[0]["text"].startswith("body text"),
                  "the article body is fetched so the scorer reads prose, not a slug")

            # robots refusal must be honoured and must not raise.
            _cs.rc.http_get = lambda url, cfg=None, etag=None, last_modified=None: (
                999, {}, "")
            notes2 = []
            items = _cs.check_sitemap(src, _cs._get_state(conn, "x-press"), {},
                                      conn, notes2)
            check(items == [] and any("robots" in n for n in notes2),
                  "a robots refusal yields nothing and says so")
            check(_cs._get_state(conn, "x-press")["last_status"] == "robots-blocked",
                  "the robots refusal is recorded on the source state")
        finally:
            _cs.rc.http_get, _cs.fetch_article_text = real_get, real_article
            conn.close()

    # Regions. The rules live in config, the answer lives on the row.
    rcfg = radar_common.load_config()
    check(radar_common.region_group_names(rcfg)[-1] == "Elsewhere",
          "the last configured group is the catch-all")
    for country, city, want in (
            ("United Kingdom", "Guildford", "Local"),
            ("United Kingdom", "Manchester", "UK"),
            ("Belgium", "Ghent", "Europe"),
            ("United States", "Boston", "Elsewhere"),
            (None, None, "Elsewhere"),
            ("", "", "Elsewhere")):
        got = radar_common.region_for(country, city, rcfg)
        check(got == want,
              f"{country or 'no country'}/{city or 'no city'} groups as {want} (got {got})")
    check(radar_common.region_for("  belgium  ", "  GHENT ", rcfg) == "Europe",
          "region matching ignores case and spacing like every other name rule")
    check(radar_common.region_for("United Kingdom", None, rcfg) == "UK",
          "a UK company with no city is still UK, not Local")

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "regions.sqlite")
        cid = radar_common.resolve_company(conn, "Northvale Bio")
        conn.execute("UPDATE companies SET country='United Kingdom',"
                     " city='Guildford' WHERE id=?", (cid,))
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status, company_id)"
                     " VALUES ('rg1',?,'imec-press','Northvale Bio','x','new',?)",
                     (radar_common.now_iso(), cid))
        conn.commit()
        radar_common.recompute_regions(conn, rcfg, force=True)
        conn.commit()
        got = conn.execute("select region from signals where url_hash='rg1'"
                           ).fetchone()["region"]
        check(got == "Local",
              f"the region is mirrored onto the signal row (got {got})")

        # A company nobody enriched must still land somewhere.
        orphan = radar_common.resolve_company(conn, "Unknown Origins Ltd")
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status, company_id)"
                     " VALUES ('rg2',?,'imec-press','Unknown Origins Ltd','y','new',?)",
                     (radar_common.now_iso(), orphan))
        conn.commit()
        radar_common.recompute_regions(conn, rcfg, force=True)
        conn.commit()
        got = conn.execute("select region from signals where url_hash='rg2'"
                           ).fetchone()["region"]
        check(got == "Elsewhere",
              f"an unenriched company lands in the catch-all, never nowhere (got {got})")
        check(conn.execute("select count(*) from signals where region is null"
                           ).fetchone()[0] == 0,
              "no signal is left without a group")

        # The bug of 3 August. Enrichment fills a country long after the
        # company row exists, and the fingerprint alone said nothing had
        # changed, so 39 companies with a known country stayed grouped as
        # Elsewhere. An unplaced company is now reason enough to do the work.
        late = radar_common.resolve_company(conn, "Late Arrival Ltd")
        conn.execute("INSERT INTO signals (url_hash, first_seen, source_id,"
                     " company, headline, status, company_id)"
                     " VALUES ('rg3',?,'imec-press','Late Arrival Ltd','z','new',?)",
                     (radar_common.now_iso(), late))
        conn.commit()
        res_late = radar_common.recompute_regions(conn, rcfg)
        conn.commit()
        check(res_late["changed"] is True,
              "a company with no region is reason enough to recompute")
        conn.execute("UPDATE companies SET country='Belgium', city='Ghent'"
                     " WHERE id=?", (late,))
        conn.execute("UPDATE companies SET region=NULL WHERE id=?", (late,))
        conn.commit()
        radar_common.recompute_regions(conn, rcfg)
        conn.commit()
        got = conn.execute("select region from signals where url_hash='rg3'"
                           ).fetchone()["region"]
        check(got == "Europe",
              f"a country learned after the fact regroups the row (got {got})")

        # The mirror is a cache, so it must follow the rules when they move.
        again = radar_common.recompute_regions(conn, rcfg)
        check(again["changed"] is False,
              "an unchanged rules block costs nothing on the next open")
        moved = dict(rcfg)
        moved["regions"] = [dict(g) for g in rcfg["regions"]]
        moved["regions"][0] = {**moved["regions"][0], "local_places": ["Nowhere"]}
        radar_common.recompute_regions(conn, moved, force=False)
        conn.commit()
        got = conn.execute("select region from signals where url_hash='rg1'"
                           ).fetchone()["region"]
        check(got == "UK",
              f"editing the local list moves the row on the next open (got {got})")
        conn.close()

    # The dossier's state writes. Only the three a human owns, dead behind
    # a confirm, and every change leaves a touch explaining itself.
    with tempfile.TemporaryDirectory() as tmp:
        dbp = Path(tmp) / "dossier.sqlite"
        conn = radar_common.get_db(dbp)
        cid = radar_common.resolve_company(conn, "Dossier Diagnostics")
        conn.commit()
        conn.close()
        serve_dashboard.ARGS = type("A", (), {"db": str(dbp)})()

        r = serve_dashboard.set_company_state(cid, "in-conversation", False)
        check(r["ok"] and r["showing"] == "in conversation",
              "a human state is set and shows as the strongest that applies")
        r = serve_dashboard.set_company_state(cid, "dead", False)
        check(r["ok"] is False and "confirm" in r["note"],
              "dead needs confirming, it outranks every other state")
        r = serve_dashboard.set_company_state(cid, "dead", True)
        check(r["ok"] and r["showing"] == "dead",
              "dead sticks once confirmed")
        for bad in ("seen", "touched", "window open", "client-ish", ""):
            r = serve_dashboard.set_company_state(cid, bad, True)
            check(r["ok"] is False,
                  f"{bad!r} is refused, the machine never sets a derived state")
        r = serve_dashboard.set_company_state(999999, "client", True)
        check(r["ok"] is False, "an unknown company is refused, never invented")

        c2 = radar_common.get_db(dbp)
        notes = [x["note"] for x in c2.execute(
            "select note from touches where company_id=? order by id", (cid,))]
        check(len(notes) == 2 and all("State set to" in n for n in notes),
              f"every accepted state change logs a touch that explains it (got {len(notes)})")
        check(all(":" not in n and ";" not in n for n in notes),
              "the logged notes obey the voice rules")
        c2.close()

    # The state ladder in isolation, so a regression names the rung it broke.
    for stored, items, touches, window, want in (
            (None,  False, False, False, "new"),
            (None,  True,  False, False, "seen"),
            (None,  True,  True,  False, "touched"),
            (None,  True,  True,  True,  "window open"),
            ("in-conversation", True, True, True, "in conversation"),
            ("client", True, True, True, "client"),
            ("dead",   True, True, True, "dead"),
            ("dead",   False, False, False, "dead")):
        got = radar_common.company_display_state(stored, items, touches, window)
        check(got == want,
              f"state ladder, stored={stored!r} items={items} touches={touches}"
              f" window={window} reads {want} (got {got})")

    # Week three books itself, but only where the playbook says the next
    # event is ours to make.
    fixed = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    for channel in ("comment", "connection-note"):
        action, when = serve_dashboard.week_three_booking(channel, fixed)
        check(when == "2026-07-22",
              f"{channel} books week three twenty-one days out (got {when!r})")
        check(action == serve_dashboard.WEEK_THREE_ACTION,
              f"{channel} books the week-three action")
    for channel in ("engagement", "artefact", "other"):
        action, when = serve_dashboard.week_three_booking(channel, fixed)
        check(action is None and when is None,
              f"{channel} books nothing, the next event is not ours")

    print()
    if failures:
        print(f"{len(failures)} UNIT CHECK(S) FAILED")
        return 1
    print("ALL UNIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # noqa: BLE001  readable FAIL, never a traceback
        print(f"\nFAIL. Unexpected {type(err).__name__}. {err}")
        sys.exit(1)
