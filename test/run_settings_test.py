#!/usr/bin/env python
"""Checks for the Settings page, /settings, and the CV page in rose (owner, 18 Nov).

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
    chips = re.findall(r'<section class="st-sec" aria-labelledby="(\w+)-h"><header class="sec-head">'
                       r'<span class="gchip g-apps" aria-hidden="true"><svg [^>]*>(.*?)</svg>', page)
    check(dict(chips) == {"scoring": page_frame.ICONS["target"], "place": page_frame.ICONS["pin"],
                          "words": page_frame.ICONS["tag"], "you": page_frame.ICONS["person"]}
          and len({v for _k, v in chips}) == 4,
          "each section's chip has its own icon from the shared set: a target, a map pin, a tag, a person")
    scoring = page[page.index('<legend>Pay floors</legend>'):page.index('<legend>CV match</legend>')]
    check(scoring.rstrip().endswith('</fieldset>\n<hr class="rule">\n<fieldset class="group">')
          and ".rule{margin:16px 0;border:0;border-top:1px solid var(--border-subtle)}" in sp.SETTINGS_CSS
          and ".group + .group" not in sp.SETTINGS_CSS,
          "CV match's hairline is above its heading, 16px either side, not beside it")
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
    template = (REPO_ROOT / "config" / "preferences.template.md").read_text(encoding="utf-8")
    shipped = re.search(r"^target_title:\s*(.+)$", template, re.M).group(1)
    check("," in shipped
          and t_row.count('<li class="chip-v">') == (1 if title.strip() else 0)
          and (not title.strip() or f'<li class="chip-v">{page_frame.esc(title)}</li>' in t_row),
          "the title is one value by design (the template's own has a comma in it), so it is one "
          "chip, never split")
    check(k_row.count('<li class="chip-v">') == len(sp.keyword_list(keywords)),
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
    empty = sp.chips_row("keywords", "Keywords", [], "Edit keywords")
    check('<ul class="chips" aria-labelledby="tk-keywords"><li class="chips-none">None yet</li></ul>' in empty
          and ".chips-none{font-size:14px;line-height:20px;color:var(--text-2)}" in sp.SETTINGS_CSS
          and "chips-none" not in sp.chips_row("keywords", "Keywords", ["IVD"], "Edit keywords"),
          "an empty row says None yet, muted at 14px, where the chips would be")
    check(f'data-max="{rc.PREF_LINE_MAX}"' in dlg and rc.PREF_LINE_MAX == 200
          and "[:PREF_LINE_MAX]" in (REPO_ROOT / "scripts" / "radar_common.py").read_text(encoding="utf-8")
          and '<p class="chips-limit" id="chips-limit" aria-live="polite" hidden>That\'s the limit. '
              'Remove one to add another.</p>' in dlg
          and dlg.index('id="chips-box"') < dlg.index('id="chips-limit"')
          and "var MAX = parseInt(dlg.getAttribute('data-max'), 10) || 0;" in js
          and "function fits(list) { return !MAX || list.join(', ').length <= MAX; }" in js
          and "if (fits(values.concat([w]))) values.push(w); else held.push(w);" in js
          and "if (input.value.trim() && !add()) { input.focus(); return; }" in js,
          "the dialog takes the line's limit from the code, stops adding at it with one line under "
          "the box, and never saves past it quietly")


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


def cv_page_checks() -> None:
    """The CV page in rose (owner, 18 Nov)."""
    import cv_screen
    page = cv_screen.render_page(CFG, None, None).decode("utf-8")
    css = cv_screen.CV_SCREEN_CSS
    bar = page[page.index('<header class="appbar">'):page.index("</header>")]
    check('<a href="/cv" data-group="cv" aria-current="page">CV</a>' in bar
          and '.appbar a[data-group="cv"]{--nav-line:var(--group-cv)}' in page_frame.FRAME_CSS,
          "the menu's CV is underlined in rose on the CV page")
    head = page[page.index('<header class="cv-head">'):page.index("</header>", page.index('<header class="cv-head">'))]
    check('<span class="gchip g-cv"' in head and "<h1>CV</h1>" in head
          and '<span class="pill pill-ok">The scorer reads this version</span>' in head
          and 'id="pages-pill"' in head
          and re.search(r'<button type="button" class="btn btn-rose" id="tb-save" data-cv-act="master" '
                        r'aria-keyshortcuts="Control\+S"( hidden)?>Save version</button>', head)
          # Solid rose in every state it shows in: declared after the plain .btn
          # rule, which used to win and draw it as text, and rose when disabled.
          and ".btn.btn-rose,.cvp a.btn.btn-rose{background:#e88fb4;border-color:#e88fb4;color:#2a0f1c" in css
          and css.index(".btn.btn-rose,") > css.index(".btn{display:inline-flex")
          and css.index(".btn.btn-rose,") > css.index(".btn:disabled{")
          and ".btn.btn-rose:disabled,.btn.btn-rose[aria-disabled=\"true\"]{background:#e88fb4" in css
          and ".pill.is-over{" in css and "var(--caution)" in css.split(".pill.is-over{", 1)[1].split("}", 1)[0],
          "the title row: rose chip, CV, the green pill, the pages pill (amber over the limit) and "
          "Save version as the one solid button, rose, at the right")
    check("pill.textContent = plural(pages.length, 'page') + ', limit is 2';" in cv_screen.CV_SCREEN_JS,
          "the pages pill reads like 4 pages, limit is 2, as the copy is edited")
    import score_item
    real = score_item.load_cv_text
    try:
        score_item.load_cv_text = lambda config: ("Alex Rennie\nGuildford\n\nPROFILE\nA leader.\n", "x")
        unlaid = cv_screen.render_page(CFG, None, None).decode("utf-8")
        score_item.load_cv_text = lambda config: ("# Alex Rennie\nA leader.\n", "x")
        laid = cv_screen.render_page(CFG, None, None).decode("utf-8")
    finally:
        score_item.load_cv_text = real
    line = ('<p class="cv-caution" role="note">Not saved in Radar\'s layout yet. Prepare application '
            "can't make a copy until it is.</p>")
    check(unlaid.count('class="cv-caution"') == 1 and line in unlaid
          and "This version was not written by this editor" not in unlaid
          and 'class="cv-caution"' not in laid and 'id="cv-editor"' in laid
          and 'notice-text">The master CV could not be read' not in laid
          and "cv-banner" not in laid.split("<main", 1)[1],
          "one caution line replaces the not-in-layout notice, only in that state")
    save = r'<button type="button" class="btn btn-rose" id="tb-save" data-cv-act="master" aria-keyshortcuts="Control\+S"%s>Save version</button>'
    js = cv_screen.CV_SCREEN_JS
    check(re.search(save % "", unlaid) and re.search(save % " hidden", laid)
          and "&quot;laidOut&quot;: false" in unlaid and "&quot;laidOut&quot;: true" in laid
          and "function needsLayout(st) { return !D.laidOut && !D.viewing && st.editable !== false; }" in js
          and "saveBtn.hidden = !(st.edited || needsLayout(st)) || failed;" in js
          and "} else if (needsLayout(st)) {\n        setState('warn', ICON.dot + '<span>Not in Radar\\'s layout</span>');" in js,
          "a master not in the layout shows Save version, solid, with no edit, and the status "
          "reads Not in Radar's layout, not Nothing to save; a laid-out master still waits for an edit")
    tb = page[page.index('id="cv-toolbar"'):page.index("</div>", page.index('id="cv-toolbar"'))]
    order = [w for w in ("Add section", "Rearrange", "Design", "Line numbers", 'aria-label="Undo"',
                         'aria-label="Redo"', 'id="save-state"', 'id="over-note"') if w in tb]
    check(len(order) == 8 and [tb.index(w) for w in order] == sorted(tb.index(w) for w in order)
          and "phone-only" not in tb and "Ctrl S" not in tb and "<kbd" not in page
          and "over.innerHTML = '<a class=\"link\" href=\"#page-3\" id=\"go-page-3\">Go to page 3</a>';"
              in cv_screen.CV_SCREEN_JS,
          "the toolbar: Add section, Rearrange, Design, Line numbers, undo, redo, the save status "
          "and Go to page N when over length, with no Ctrl S label")
    rail = page[page.index('<aside class="cv-rail"'):page.index("</aside>")]
    check(re.findall(r'id="(\w+)-h">([^<]+)</h2>', rail) == [("versions", "Versions"), ("import", "Import")]
          and re.search(r'id="versions-h">Versions</h2><span class="sec-count">\d+</span>', rail)
          and rail.count('class="card g-cv glow-faint cv-card"') == 2 and rail.count('<span class="gchip g-cv"') == 2
          and '<p class="help">Word, Markdown, text or PDF, up to 5 MB</p>' in rail
          and "Choose a file" in rail,
          "the rail: Versions with its count and Import with Choose a file and its one line, rose chips and glows")
    check("@media (min-width:1024px){.cv-layout{grid-template-columns:minmax(0,1fr) 360px}}" in css
          and "@media (min-width:1800px){.cv-layout{grid-template-columns:minmax(0,1fr) 400px}}" in css
          and "grid-template-areas" not in css,
          "the sheet and a 360px rail from 1024px, 400px from 1800px, stacked below; no three columns")
    words = text(page)
    for gone in ("Edit it in place, section by section", "Master CV", "Keys on this page",
                 "On a phone the CV is for reading", 'role="tab"', "Scoring preferences",
                 "Bring the master in from a file", "Saving makes it the active master",
                 "A citation on a Prepare page follows", "tracking systems read it"):
        if gone in words or gone in page:
            check(False, f"the CV page has no {gone!r}")
            break
    else:
        check(True, "gone: the sentence under the title, the Master CV panel, the keys panel, the "
                    "phone note, the Preferences tab and the import's and dialogs' explanations")


def main() -> int:
    ap = argparse.ArgumentParser(description="Settings page checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    page = sp.render_page(CFG, None, None).decode("utf-8")
    for section in (lambda: page_checks(page), lambda: chips_checks(page), script_checks, route_checks,
                    cv_page_checks):
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
