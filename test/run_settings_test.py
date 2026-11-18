#!/usr/bin/env python
"""Checks for the Settings page, /settings (owner, 18 Nov).

What the CV page's Preferences tab held, on a page of its own: Scoring (the
pay floors and the CV match marks, Save and re-sort), Location and sector
(each with its Save and the one line on the paid rescore), Title and
keywords as chips edited in a dialog, and Your name. The routes, the field
names and the checks behind them are the Preferences tab's, unchanged; the
page's script is the CV page's own code for them, by its section markers.

  python test/run_settings_test.py
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import re
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import page_frame  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402
import settings_page as sp  # noqa: E402

CFG = rc.load_config()
failures: list[str] = []


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


@contextlib.contextmanager
def serving(db: Path):
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
             serve_dashboard._MIGRATED)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    serve_dashboard.GUARD = serve_dashboard.WriteGuard("enforce", serve_dashboard.LOOPBACK_NAMES)
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    serve_dashboard._MIGRATED = False
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
         serve_dashboard._MIGRATED) = saved


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def page_checks(page: str) -> None:
    bar = page[page.index('<header class="appbar">'):page.index("</header>")]
    check('<a href="/settings" aria-current="page">Settings</a>' in bar
          and 'href="/cv#settings"' not in bar,
          "the menu's Settings goes to /settings and marks it, in the accent underline")
    check('<span class="gchip g-apps"' in page and "<h1>Settings</h1>" in page,
          "the title row: the accent chip and Settings")
    left = page[page.index('<div class="st-cols">'):page.index('<div class="st-col"><section class="st-sec" aria-labelledby="place-h"')]
    right = page[page.index('aria-labelledby="place-h"'):page.index('<dialog')]
    titles = re.findall(r'<h2 class="sec-title" id="(\w+)-h">([^<]+)</h2>', page)
    check([t for _k, t in titles] == ["Scoring", "Location and sector", "Title and keywords", "Your name"]
          and 'id="scoring-h"' in left and all(f'id="{k}-h"' in right for k in ("place", "words", "you"))
          and "@media (min-width:1024px){.st-cols{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}" in sp.SETTINGS_CSS,
          "four sections, titled outside their cards: Scoring on the left, the other three on the right "
          "from 1024px, one column below")
    check(page.count('class="card g-apps glow-faint st-card"') == 4
          and page.count('<span class="gchip g-apps"') == 5,
          "each section's chip and card glow in the accent")
    check(re.search(r'<legend>Pay floors</legend>.*?<label for="pref-day_rate_floor_gbp">Contract day rate</label>'
                    r'<span class="affixed"><span class="affix" aria-hidden="true">£</span><input [^>]*id="pref-day_rate_floor_gbp"'
                    r'[^>]*><span class="affix">a day</span>.*?<label for="pref-salary_floor_gbp">Permanent salary</label>'
                    r'.*?<span class="affix">a year</span>', page, re.S)
          and re.search(r'<legend>CV match</legend>.*?Pass mark.*?Filtered under', page, re.S)
          and page.count('id="prefs-save"') == 1 and ">Save and re-sort</button>" in page,
          "Scoring: Pay floors with £ before and a day or a year after, CV match with Pass mark and "
          "Filtered under, and the one Save and re-sort")
    check(page.count('data-section-save="location"') == 1 and page.count('data-section-save="sector"') == 1
          and '<p class="line">Saving either one offers a paid rescore, with the cost shown first.</p>' in page
          and 'id="rescore-confirm"' in page and 'id="rescore-run"' in page,
          "Location and sector: each its own Save, the one line on the paid rescore, and the "
          "rescore's confirm and run as on the CV page")
    check('data-pref-save="first_name"' in page and 'id="pref-first_name"' in page
          and '<label for="pref-first_name">First name</label>' in page,
          "Your name: First name with Save")
    css = sp.SETTINGS_CSS
    check(".input{height:40px" in css and ".field label,.group legend{font-size:14px;line-height:20px" in css
          and ".field{display:flex;flex-direction:column;gap:6px" in css,
          "inputs 40px high with their labels at 14px above them")
    words = text(page)
    for gone in ("What a role is judged against", "Changing these changes how roles are scored",
                 "A day rate under this fails", "judged by the scorer from these words", "re-sorted in code",
                 "The scorer also reads the title", "Where you will work and travel",
                 "The sectors you want first", "comma separated", "for Home's welcome"):
        if gone in words:
            check(False, f"no explanatory text: {gone!r}")
            break
    else:
        check(True, "every explanatory paragraph of the old tab is gone; the paid-rescore line stays")
    check("·" not in words and "<kbd" not in page, "no middle dots and no key hints")


def chips_checks(page: str) -> None:
    check(sp.keyword_list(" IEC 62304, ISO 13485 ,,IVD, IEC 62304,  medical  devices ")
          == ["IEC 62304", "ISO 13485", "IVD", "medical devices"],
          "keywords split on commas, trimmed, empties and exact duplicates dropped, in order")
    title = rc.read_pref_line("target_title", CFG) or ""
    keywords = rc.read_pref_line("keywords", CFG) or ""
    t_row = re.search(r'<h3 class="tk-l" id="tk-target_title">Title</h3>.*?</ul>', page, re.S).group(0)
    k_row = re.search(r'<h3 class="tk-l" id="tk-keywords">Keywords</h3>.*?</ul>', page, re.S).group(0)
    check("," in title and t_row.count('<li class="chip-v">') == 1
          and f'<li class="chip-v">{page_frame.esc(title)}</li>' in t_row,
          "the title is one value by design (the template's has a comma in it), so it is one chip")
    check(k_row.count('<li class="chip-v">') == len(sp.keyword_list(keywords)) >= 2,
          "the keywords are a chip each")
    check('<button type="button" class="pen" data-chips-edit="target_title" aria-label="Edit title"' in t_row
          and '<button type="button" class="pen" data-chips-edit="keywords" aria-label="Edit keywords"' in k_row
          and ".pen{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;padding:0;"
              "border:1.5px solid var(--accent);border-radius:50%" in sp.SETTINGS_CSS
          and ".tk-l{font-size:15px;line-height:20px;font-weight:600}" in sp.SETTINGS_CSS,
          "each row's label at 15px/600 with a round 28px edit button, 1.5px accent ring, named Edit "
          "title or Edit keywords")
    check(".chip-v{padding:5px 12px;border-radius:8px;background:#232a30;color:var(--text-1);font-size:14px"
          in sp.SETTINGS_CSS and ".chips{display:flex;flex-wrap:wrap;gap:8px" in sp.SETTINGS_CSS
          and ".tk-row + .tk-row{margin-top:16px;padding-top:16px;border-top:1px solid var(--border-subtle)}"
          in sp.SETTINGS_CSS,
          "the chips wrap, #232a30, radius 8px, 5px 12px, 14px, 8px apart, with a hairline between the rows")
    dlg = page[page.index('<dialog class="chips-dlg card"'):page.index("</dialog>")]
    check('aria-label="Close"' in dlg and 'placeholder="Add a keyword"' in dlg
          and '<button type="button" class="btn-text" id="chips-cancel">Cancel</button>' in dlg
          and '<button type="button" class="btn btn-solid" id="chips-save">Save</button>' in dlg
          and ".dlg-top h2{font:600 26px/32px" in sp.SETTINGS_CSS
          and ".pill-v{display:inline-flex;align-items:center;gap:4px;padding:4px 4px 4px 12px;border-radius:999px"
              in sp.SETTINGS_CSS
          and ".chips-dlg{width:calc(100vw - 32px);margin:auto 16px" in sp.SETTINGS_CSS,
          "the dialog: a 26px/600 heading with a close button, the values as round pills in one "
          "bordered box ending in the input, Cancel as text and Save solid in the accent, the full "
          "width less 16px margins on a phone")
    js = sp.CHIPS_JS
    check("head: 'Edit keywords', label: 'Your keywords', hint: 'Add a keyword', many: true" in js
          and "head: 'Edit title', label: 'Your titles', hint: 'Add a title', many: false" in js
          and "x.setAttribute('aria-label', 'Remove ' + v);" in js
          and "e.key === 'Enter' || (e.key === ',' && KINDS[key].many)" in js
          and "body: JSON.stringify({ key: key, value: values.join(', ') })" in js
          and "fetch('/profile/setting'" in js,
          "Enter or a comma adds a keyword (a title, Enter only, replaces the one there), each pill "
          "has Remove <value>, and Save joins them with commas through the same route")
    close = js[js.index("function close()"):js.index("document.addEventListener('click'")]
    check("fetch" not in close and "getElementById('chips-cancel').addEventListener('click', close)" in js
          and "getElementById('chips-x').addEventListener('click', close)" in js,
          "Cancel and the close button discard the changes: they close without a save")
    check("Maximum" not in page, "no limit line: the code keeps no limit on keywords")


def script_checks() -> None:
    js = sp.settings_js()
    check(js.lstrip().startswith("(function () {") and js.rstrip().endswith("})();")
          and "// ------------------------------------------------- Rescore affected" in js
          and "// ------------------------------------------------- Preferences" in js
          and "// ------------------------------------------------- the Word import" not in js
          and "// ------------------------------------------------------- the sheet" not in js,
          "the page's script is the CV page's own setup, Rescore affected and Preferences code, "
          "closed as one function")
    check("post('/cv/preferences'" in js and "post('/cv/preferences/section'" in js
          and "post('/cv/rescore'" in js,
          "the saves go through the routes they always did")


def route_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "s.sqlite"
        rc.get_db(db).close()
        with serving(db) as port:
            own = {"Host": f"127.0.0.1:{port}"}
            status, ctype, body = call(port, "GET", "/settings", headers=own)
            check(status == 200 and "text/html" in ctype and b"<h1>Settings</h1>" in body,
                  "GET /settings serves the page")
            w = {**own, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
                 serve_dashboard.WRITE_HEADER: "1"}
            status, _c, b = call(port, "POST", "/cv/preferences",
                                 json.dumps({"values": {"cv_pass_score": "70"}, "dry_run": True}), w)
            check(status == 200 and "ok" in json.loads(b),
                  "POST /cv/preferences still answers, a dry run writing nothing")
        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
        finally:
            conn.close()
        check(n == 0, "and drawing the page or a dry run writes nothing")


def main() -> int:
    ap = argparse.ArgumentParser(description="Settings page checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    page = sp.render_page(CFG, None, None).decode("utf-8")
    for section in (lambda: page_checks(page), lambda: chips_checks(page), script_checks, route_checks):
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
    print("PASS. Every Settings check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
