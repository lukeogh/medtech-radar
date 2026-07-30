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
