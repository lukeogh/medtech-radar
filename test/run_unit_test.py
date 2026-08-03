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

    # ----- the floor line, real file then loud failure
    try:
        floor = radar_common.read_rate_floor(CFG)
        check(floor == 650.0, f"the preferences floor line reads 650 (got {floor})")
    except RuntimeError as err:
        check(False, f"the preferences floor line reads without error ({err})")

    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "prefs.md"
        bare.write_text("# Preferences\n\nNo floor line here.\n", encoding="utf-8")
        rel = bare.resolve()
        try:
            radar_common.read_rate_floor({"prefs_file": str(rel)})
            check(False, "a preferences file without the floor line fails loudly")
        except RuntimeError as err:
            check("day_rate_floor_gbp" in str(err),
                  "the loud failure names the missing line and the fix")

    # ----- the dashboard-editable preference lines, surgical and loud
    with tempfile.TemporaryDirectory() as tmp:
        prefs = Path(tmp) / "prefs.md"
        prefs.write_text(
            "# Preferences.\n\n## Rates.\n\nProse stays untouched.\n\n"
            "day_rate_floor_gbp: 650\n\nMore prose after.\n",
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
        check(rows["bf2"]["rate_band"] == "below"
              and round(rows["bf2"]["day_rate"]) == 432,
              "backfill converts the £95k salary and bands it below")
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
                " one_line_why, rate_band)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
        # The rendered rate surface, pinned. Column header, band word on
        # the row, legend under the panel, and the status chip is gone
        # from the inbound table.
        check('class="h-rate"' in html_page,
              "the dashboard renders the Rate column header")
        check("rate-above" in html_page,
              "the dashboard renders the band word on the row")
        check("Rate bands sit against the" in html_page,
              "the dashboard renders the rate legend under the table")
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
        # Deaths by gate needs gated rows to have anything to say, and this
        # fixture predates the gates, so its absence here is correct.
        check(jobs_page.index("Top prospects") < jobs_page.index("Analysis."),
              "the page opens on the tiers, analysis sits below them")
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
        check('id="src-add"' in jobs_page and "Add a job source" in jobs_page,
              "the add-a-source form renders")
        check("None yet" not in jobs_page
              and "job_sources.yaml" not in jobs_page,
              "no custom-source receipt renders while there are none")
        # The board-order assertion went with the board sections. Ordering
        # boards on a page that no longer groups by board tested nothing.
        saved_reg3 = radar_common.JOB_SOURCES_PATH
        try:
            radar_common.JOB_SOURCES_PATH = reg_path
            jobs_with_custom = build_dashboard.render_jobs_page(
                data, config, "t")
        finally:
            radar_common.JOB_SOURCES_PATH = saved_reg3
        check("Added so far: Technojobs (matches technojobs)"
              in jobs_with_custom
              and "job_sources.yaml" in jobs_with_custom,
              "an added source earns its one-line receipt, with the edit path")
        # A custom source no longer earns a section, because no source does.
        # What it earns is the receipt line above, which is the honest
        # acknowledgement that the radar now watches for its sender.
        check(">Technojobs</span>" not in jobs_with_custom,
              "an added source gets no board section, because none exist")
        check(jobs_with_custom.index("Added so far: Technojobs")
              > jobs_with_custom.index("Top prospects"),
              "its receipt sits at the foot of the page, below the tiers")
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
        check('id="pref-day_rate_floor_gbp"' in home
              and 'value="650"' in home,
              "the scoring panel shows the live floor")
        check('id="pref-target_title"' in home
              and 'id="pref-keywords"' in home,
              "title and keywords are editable from the panel")
        check("Needs you" not in home,
              "the attention section stays silent with nothing due")
        check('href="/archive"' in home,
              "the full archive stays reachable from the footer")

        digest_data = build_digest.collect(read, config)
        digest_text = build_digest.render_text(digest_data, "unit day")
        check("Rate Above." in digest_text,
              "the digest renders the band word on the item line")
        check("Rate bands. Above meets the" in digest_text,
              "the digest renders the rate legend under the inbound table")
        # A week with no inbound but a scored signal-kind role still gets
        # the legend next to its rate words.
        sig_only = dict(digest_data)
        sig_only["inbound"] = []
        sig_only["signals"] = [{
            "kind": "opportunity", "id": 999, "title": "Signal Role",
            "company": "Sig Ltd", "location": "Ghent", "combined": 80,
            "cv_match": 80, "want_match": 80, "one_line_why": "Fits.",
            "source_url": "", "rate_band": "above"}]
        sig_text = build_digest.render_text(sig_only, "unit day")
        check("Rate bands. Above meets the" in sig_text,
              "a signals-only digest still carries the legend")
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
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ack",
                data=json_mod.dumps({"id": ids["Seenit Ltd"]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
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
                    headers={"Content-Type": "application/json"},
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
                    headers={"X-Filename": "cv.txt"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = json_mod.loads(resp.read())
                check(resp.status == 200
                      and "A CV as plain text." in body.get("markdown", ""),
                      "POST /cv/preview extracts and stages over HTTP")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/cv/discard",
                    data=json_mod.dumps(
                        {"token": body["token"]}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    check(resp.status == 200,
                          "POST /cv/discard forgets the staged upload")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/cv/confirm",
                    data=json_mod.dumps(
                        {"token": "0" * 16}).encode(),
                    headers={"Content-Type": "application/json"},
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
                data=b"{}", headers={"Content-Type": "application/json"},
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
    for label, gates, basis, cv, real, want in (
            ("all four gates pass", G(), "", 84, True, "top"),
            ("all pass, rate unstated", G(), "", 84, True, "top"),
            ("location alone fails", G(location=False), "", 84, True, "question"),
            ("stated day rate alone fails", G(rate=False), "day-rate", 84, True, "question"),
            ("rate from a salary alone fails", G(rate=False), "converted-salary", 84, True, "reading"),
            ("sector alone fails", G(sector=False), "", 84, True, "reading"),
            ("cv alone fails", G(cv=False), "", 68, True, "reading"),
            ("two gates fail", G(location=False, rate=False), "", 84, True, "reading"),
            ("cv under forty", G(cv=False), "", 22, True, "filtered"),
            ("not a real role", G(), "", 84, False, "filtered")):
        got = _si.derive_tier(gates, basis, cv, real)["tier"]
        check(got == want, f"{label} lands in {want} (got {got})")

    check(_si.derive_tier(G(sector=False), "", 84, True)["failed_gates"] == ["sector"],
          "the failed gate is recorded as the verdict")
    check(_si.derive_tier(G(cv=False), "", 22, True)["filter_reason"] == "cv match under 40",
          "a filtered row records why, so the filter can be audited")
    check(_si.question_for(G(location=False), {}) == "Would they go remote",
          "the location question is the one a conversation could settle")
    check(_si.question_for(G(rate=False), {}) == "Would they move on the rate",
          "the rate question likewise")

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

    # digest_min_want. A second bar on the desire half alone, off by default,
    # and off must mean the digest is exactly what it was.
    import build_digest as _bd

    with tempfile.TemporaryDirectory() as tmp:
        conn = radar_common.get_db(Path(tmp) / "digest.sqlite")
        now = radar_common.now_iso()
        # Two roles over the combined bar. One is wanted, one is not, which
        # is the case the flag exists for, a role the CV can clearly do and
        # the preferences plainly do not want.
        for h, title, cv, want, comb in (("dw1", "Wanted role", 90, 90, 90),
                                         ("dw2", "Capable but unwanted", 95, 20, 75)):
            conn.execute(
                "INSERT INTO opportunities (url_hash, first_seen, company, title,"
                " cv_match, want_match, combined, thread_type, status)"
                " VALUES (?,?,?,?,?,?,?,'inbound','new')",
                (h, now, "Acme Dx", title, cv, want, comb))
        conn.commit()

        base_cfg = {"score_threshold": 70, "prefs_file": "config/profile/preferences.md"}
        off = _bd.collect(conn, dict(base_cfg))
        off_zero = _bd.collect(conn, {**base_cfg, "digest_min_want": 0})
        on = _bd.collect(conn, {**base_cfg, "digest_min_want": 50})

        titles = lambda d: sorted(x["title"] for x in d["inbound"])
        check(titles(off) == ["Capable but unwanted", "Wanted role"],
              f"with the flag absent both roles are in the digest (got {titles(off)})")
        check(titles(off_zero) == titles(off),
              "an explicit zero behaves exactly as the flag being absent")
        check(titles(on) == ["Wanted role"],
              f"with the flag at 50 the unwanted role drops out (got {titles(on)})")
        check(len(on["inbound"]) == 1 and on["inbound"][0]["want_match"] >= 50,
              "what survives the flag clears the want bar it names")
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
