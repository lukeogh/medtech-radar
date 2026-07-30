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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import radar_common

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

        digest_data = build_digest.collect(read, config)
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
                    f"http://127.0.0.1:{port}/", timeout=10) as resp:
                page = resp.read().decode("utf-8")
            check(resp.status == 200
                  and f'data-unack="{ids["Seenit Ltd"]}"' in page,
                  "the served page re-renders with the row acknowledged")
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
