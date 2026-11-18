#!/usr/bin/env python
"""Checks for the fix-up (owner, 18 Nov): the Watchlist's one-press add, Save
version in solid rose, the experience bank's section chips, the Add a role
dialog's fixed heading and footer, the 13px count pill and version file line,
the Title limit on Settings, and the market panel's euro rate.

  python test/run_fixup_test.py

No model calls and no network: a throwaway database in a temporary folder.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import add_role_page  # noqa: E402
import build_dashboard  # noqa: E402
import cv_screen  # noqa: E402
import experience_page  # noqa: E402
import page_frame  # noqa: E402
import radar_common as rc  # noqa: E402
import settings_page  # noqa: E402
import watchlist  # noqa: E402
import watchlist_page  # noqa: E402

failures: list[str] = []
NOW = "2026-11-18T12:00:00Z"


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def watchlist_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = rc.get_db(Path(tmp) / "w.sqlite")
        for i in range(7):
            conn.execute("INSERT INTO companies (norm_name, display_name, first_seen, city, country)"
                         " VALUES (?, ?, ?, ?, ?)", (f"qbd group {i}", f"QbD Group {i}", NOW,
                                                     "Ghent" if i == 0 else None, "Belgium" if i == 0 else None))
        cid = conn.execute("SELECT id FROM companies WHERE norm_name = 'qbd group 0'").fetchone()[0]
        for n, key in enumerate(("a", "a", "b")):
            conn.execute("INSERT INTO opportunities (url_hash, first_seen, title, company, company_id, role_key)"
                         " VALUES (?, ?, 'QA lead', 'QbD Group 0', ?, ?)", (f"h{n}", NOW, cid, key))
        conn.commit()
        found = watchlist.find_names(conn, "qbd")
        check(len(found) == 5, f"at most five matches ({len(found)})")
        first = next(f for f in found if f["id"] == cid)
        check(first["roles"] == "2 roles" and "Ghent" in first["place"]
              and all(f["roles"] == "" for f in found if f["id"] != cid),
              "each match has its place where known and N roles where it has any, one per repost chain")
        check(watchlist.find_names(conn, "q") == [], "nothing is looked up under two characters")
        out = watchlist.add_by_name(conn, name="Elemed", anyway=True, now=NOW)
        conn.commit()
        check(out.get("ok") and out.get("name") == "Elemed", "a name Radar has not seen is added in one step")
        again = watchlist.add_by_name(conn, name="ELEMED", anyway=True, now=NOW)
        check(again.get("already") and again["id"] == out["id"],
              "a name already on the Watchlist, whatever its case, is not added twice: its record opens")
        n = conn.execute("SELECT COUNT(*) FROM companies WHERE norm_name = 'elemed'").fetchone()[0]
        check(n == 1, "and no second company is made")
        conn.close()
    js = watchlist_page.WATCHLIST_SCRIPT
    block = js[js.index("// ---- add a company by name"):js.index("// ---- suggestions")]
    check("anyway: true" in block and "showNotFound" not in block and "Add ' + esc(name) + ' anyway" not in block
          and "statusBox" not in block, "Add or Enter adds in one step: no second press and no caution box")
    check("Up and Down" not in block and "Enter or Add looks again" not in block
          and "Check the spelling" not in block, "both explanatory lines are gone, and the key hint with them")
    check("matches.length ? '' : 'New to Radar'" in block, "with nothing matching, one muted line: New to Radar")
    check("text: 'Added ' + r.name + '.'" in block, "after adding, the existing kind of line: Added QbD Group.")
    check("if (r.ok && r.already) { openRecord(r.id); return; }" in block
          and "if (m && m.watched) { openRecord(m.id); return; }" in block,
          "a company already watched opens its record instead")
    check("[m.place, m.roles].filter(Boolean)" in block, "a match shows its name, place and roles")


def cv_checks() -> None:
    css = cv_screen.CV_SCREEN_CSS
    base = css.index(".btn{display:inline-flex")
    rose = css.index(".btn.btn-rose,.cvp a.btn.btn-rose{background:#e88fb4;border-color:#e88fb4;color:#2a0f1c")
    check(rose > base and rose > css.index(".btn:disabled{"),
          "Save version's rose comes after the plain button rule, so it is never drawn as text")
    check(".btn.btn-rose:disabled,.btn.btn-rose[aria-disabled=\"true\"]{background:#e88fb4;border-color:#e88fb4;color:#2a0f1c" in css,
          "and it stays solid rose while disabled")
    check(".vrow .vfile{--dv-space:8px;color:var(--text-3);font:400 13px/18px" in css,
          "the version file line is 13px")


def experience_checks() -> None:
    html = experience_page.lists_html([])
    for key, icon, title in (("draft", "review", "To review"), ("backed", "backed", "Backed"), ("parked", "parked", "Parked")):
        head = re.search(rf'<section class="xp-sec" aria-labelledby="xp-{key}-h"><header class="sec-head">(.*?)</header>',
                         html, re.S)
        want = page_frame.chip(icon, "cv")
        if not (head and want in head.group(1) and f">{title}</h2>" in head.group(1)):
            check(False, f"{title} has its rose chip")
            return
    icons = {experience_page.SECTION_ICONS[k] for k in ("draft", "backed", "parked")}
    check(len(icons) == 3 and icons <= set(page_frame.ICONS),
          "To review, Backed and Parked each have a rose chip with its own icon, in the shared set")


def dialog_checks() -> None:
    html = add_role_page.dialog_html()
    order = [html.index(c) for c in ('class="ar-top"', 'class="ar-body"', 'class="ar-foot"')]
    check(order == sorted(order) and html.index('id="ar-link"') > order[1] and html.index('id="ar-about"') < order[2],
          "the dialog is a heading, a body holding every field, and the footer")
    css = add_role_page.CSS
    check(".ar-body{" in css and "overflow-y:auto" in css.split(".ar-body{", 1)[1].split("}", 1)[0]
          and "min-height:0" in css.split(".ar-body{", 1)[1].split("}", 1)[0]
          and "flex:none" in css.split(".ar-top{", 1)[1].split("}", 1)[0]
          and "flex:none" in css.split(".ar-foot{", 1)[1].split("}", 1)[0]
          and ".ar-dlg[open]{display:flex;flex-direction:column;overflow:hidden}" in css,
          "the heading and the footer stay put and the fields scroll between them, Add and Edit alike")


def size_checks() -> None:
    rule = re.search(r"\.sec-count\{[^}]*\}", page_frame.FRAME_CSS + page_frame.TITLES_CSS
                     + page_frame.GROUPS_CSS + Path(page_frame.__file__).read_text(encoding="utf-8")).group(0)
    check("font-size:13px" in rule and "12px" not in rule, "the shared count pill is 13px")
    others = [p.name for p in (REPO_ROOT / "scripts").glob("*.py")
              if re.search(r"\.sec-count\{[^}]*font-size:12px", p.read_text(encoding="utf-8"))]
    check(not others, f"and no page draws it at 12px ({others})")


def settings_checks() -> None:
    js = settings_page.CHIPS_JS
    check("limit: \"That's the limit. Shorten it to save.\"" in js
          and "if (t && !fits([t])) { limit(true); return false; }" in js
          and "if (!KINDS[key].many) limit(!fits([" in js
          and "if (input.value.trim() && !add()) { input.focus(); return; }" in js,
          "Title past the save's limit shows That's the limit. Shorten it to save. and Save does not run")
    check(f'data-max="{rc.PREF_LINE_MAX}"' in settings_page.DIALOG, "the limit is the save's own, PREF_LINE_MAX")


def euro_checks() -> None:
    cfg = {"eur_to_gbp": 0.85}
    check(rc.convert_to_day_rate("EUR", "day", 1000, 1000, cfg, money={"EUR": 0.9, "USD": None, "fee": 15}) == 900,
          "the market panel's day rate converts euros at the Settings rate when one is set")
    check(rc.convert_to_day_rate("EUR", "day", 1000, 1000, cfg, money={"EUR": None, "USD": None, "fee": 15}) == 850,
          "and at radar.yaml's when none is, as today")
    with tempfile.TemporaryDirectory() as tmp:
        conn = rc.get_db(Path(tmp) / "m.sqlite")
        recent = rc.now_iso()
        conn.execute("INSERT INTO opportunities (url_hash, first_seen, pay_currency, pay_period, pay_min, pay_max,"
                     " day_rate, rate_band) VALUES ('e', ?, 'EUR', 'day', 1000, 1000, 850, 'above')", (recent,))
        conn.commit()
        real = rc.read_money_prefs
        try:
            rc.read_money_prefs = lambda config=None: {"EUR": 0.9, "USD": None, "fee": 15}
            set_ = build_dashboard.rate_metrics(conn, 750)
            rc.read_money_prefs = lambda config=None: {"EUR": None, "USD": None, "fee": 15}
            unset = build_dashboard.rate_metrics(conn, 750)
        finally:
            rc.read_money_prefs = real
        stored = conn.execute("SELECT day_rate FROM opportunities").fetchone()[0]
        conn.close()
    check(set_["median"] == 900 and unset["median"] == 850 and stored == 850,
          "the panel draws euros at the Settings rate, or the stored figure with none set, and nothing stored changes")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fix-up checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    for section in (watchlist_checks, cv_checks, experience_checks, dialog_checks, size_checks,
                    settings_checks, euro_checks):
        try:
            section()
        except Exception as err:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"a section stopped on {type(err).__name__}: {err}")
    print()
    if failures:
        print(f"FAIL. {len(failures)} check(s) failed.")
        return 1
    print("PASS. Every fix-up check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
