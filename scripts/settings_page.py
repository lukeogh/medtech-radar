#!/usr/bin/env python
"""The Settings page: /settings (owner, 18 Nov).

What the CV page's Preferences tab held, on a page of its own: the scoring
lines (the pay floors and the CV match marks, saved and re-sorted in code),
the location and sector words (saved, then the paid rescore offered with its
count and cost first), the title and keywords as chips edited in a dialog,
and the first name Home greets by. Every save goes through the routes it
always did (POST /cv/preferences, /cv/preferences/section, /profile/setting
and /cv/rescore), so nothing about what is stored or how a save is judged
changes. The page's script is the CV page's own setup, Rescore affected and
Preferences code (cv_screen.CV_SCREEN_JS, by its section markers), and the
dashboard's PREFS_SCRIPT for the lines, plus the chips' dialog here.

Keywords stay one comma-separated line. The title stays one value: the
scorer reads the whole preferences file as prose, and the template's own
title has a comma in it ("Fractional software director, medtech and IVD"),
so it is never split on commas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_dashboard  # noqa: E402
import cv_screen  # noqa: E402
import page_frame  # noqa: E402
import radar_common  # noqa: E402

esc = page_frame.esc


def keyword_list(raw: str | None) -> list[str]:
    """The keywords line as chips: split on commas, trimmed, empties and
    exact duplicates dropped, in their order."""
    out = []
    for part in str(raw or "").split(","):
        word = " ".join(part.split())
        if word and word not in out:
            out.append(word)
    return out


def _sec(key: str, title: str, body: str, icon: str = "settings") -> str:
    return (f'<section class="st-sec" aria-labelledby="{key}-h"><header class="sec-head">'
            f'{page_frame.chip(icon, "apps")}<h2 class="sec-title" id="{key}-h">{esc(title)}</h2>'
            f'</header><div class="card g-apps glow-faint st-card">{body}</div></section>')


def _money(key: str, label: str, value: str, after: str, lock: str) -> str:
    return (f'<div class="field"><label for="pref-{key}">{esc(label)}</label>'
            f'<span class="affixed"><span class="affix" aria-hidden="true">£</span>'
            f'<input class="input num" id="pref-{key}" name="{key}" inputmode="decimal" value="{value}" '
            f'data-was="{value}"{lock}><span class="affix">{esc(after)}</span></span></div>')


def _mark(key: str, label: str, value: str, lock: str) -> str:
    return (f'<div class="field"><label for="pref-{key}">{esc(label)}</label>'
            f'<input class="input short" id="pref-{key}" name="{key}" inputmode="numeric" value="{value}" '
            f'data-was="{value}"{lock}></div>')


def chips_row(key: str, label: str, values: list[str], edit: str) -> str:
    chips = "".join(f'<li class="chip-v">{esc(v)}</li>' for v in values)
    return (f'<div class="tk-row"><div class="tk-head"><h3 class="tk-l" id="tk-{key}">{esc(label)}</h3>'
            f'<button type="button" class="pen" data-chips-edit="{key}" aria-label="{esc(edit)}" '
            f'aria-haspopup="dialog">{PEN}</button></div>'
            f'<ul class="chips" aria-labelledby="tk-{key}">{chips}</ul></div>')


PEN = ('<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">'
       '<path d="M3 13l1-3 6.5-6.5 2 2L6 12z" stroke="currentColor" stroke-width="1.4" fill="none"/></svg>')
CLOSE = ('<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
         '<path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6"/></svg>')


def body_html(config: dict, prefs: dict) -> str:
    live = prefs["live"]
    lock = "" if live else " disabled"
    num = cv_screen._num
    missing = ("" if live else
               f'<p class="notice warn st-missing">No preferences file at {esc(prefs["path"])}, so '
               'nothing here can be saved.</p>')
    scoring = f"""<form class="prefs" id="prefs-form" novalidate>
<fieldset class="group"><legend>Pay floors</legend>
{_money('day_rate_floor_gbp', 'Contract day rate', num(prefs['day_rate_floor_gbp']), 'a day', lock)}
{_money('salary_floor_gbp', 'Permanent salary', num(prefs['salary_floor_gbp']), 'a year', lock)}
</fieldset>
<fieldset class="group"><legend>CV match</legend>
<div class="pair">{_mark('cv_pass_score', 'Pass mark', num(prefs['cv_pass_score']), lock)}
{_mark('cv_floor_score', 'Filtered under', num(prefs['cv_floor_score']), lock)}</div>
</fieldset>
<div class="form-foot"><button type="submit" class="btn btn-solid" id="prefs-save"{lock}>Save and re-sort</button></div>
<p class="say" id="prefs-say" aria-live="polite"></p>
</form>"""

    def prose(key: str, label: str) -> str:
        value = prefs.get(key) or ""
        return (f'<div class="field"><label for="pref-{key}">{esc(label)}</label>'
                f'<textarea class="input prose" id="pref-{key}" rows="4" data-was="{esc(value)}"{lock}>'
                f'{esc(value)}</textarea><p class="inrow"><button type="button" class="btn" '
                f'data-section-save="{key}"{lock}>Save</button></p></div>')
    sections = (f'{prose("location", "Location")}{prose("sector", "Sector")}'
                '<p class="line">Saving either one offers a paid rescore, with the cost shown first.</p>'
                '<p class="say" id="section-say" aria-live="polite"></p>'
                f'{cv_screen._rescore_blocks(None, None, {"state": "idle"})}')
    title = radar_common.read_pref_line("target_title", config) or ""
    keywords = radar_common.read_pref_line("keywords", config) or ""
    tk = (chips_row("target_title", "Title", [title] if title.strip() else [], "Edit title")
          + chips_row("keywords", "Keywords", keyword_list(keywords), "Edit keywords")
          + f'<input type="hidden" id="pref-target_title" value="{esc(title)}">'
          + f'<input type="hidden" id="pref-keywords" value="{esc(keywords)}">'
          + '<p class="say" id="chips-say" aria-live="polite"></p>')
    name = radar_common.read_first_name(config) or ""
    you = (f'<div class="field"><label for="pref-first_name">First name</label>'
           f'<span class="inrow"><input type="text" class="input" id="pref-first_name" value="{esc(name)}" '
           'autocomplete="given-name"><button type="button" class="btn" data-pref-save="first_name">'
           'Save</button></span></div><p class="say" id="pref-note" aria-live="polite"></p>')
    return (f'<header class="st-head">{page_frame.chip("settings", "apps")}<h1>Settings</h1></header>{missing}'
            f'<div class="st-cols"><div class="st-col">{_sec("scoring", "Scoring", scoring)}</div>'
            f'<div class="st-col">{_sec("place", "Location and sector", sections)}'
            f'{_sec("words", "Title and keywords", tk)}{_sec("you", "Your name", you)}</div></div>'
            + DIALOG)


DIALOG = f"""<dialog class="chips-dlg card" id="chips-dlg" aria-labelledby="chips-dlg-h">
<div class="dlg-top"><h2 id="chips-dlg-h">Edit keywords</h2>
<button type="button" class="dlg-x" id="chips-x" aria-label="Close">{CLOSE}</button></div>
<label class="dlg-l" id="chips-l" for="chips-in">Your keywords</label>
<div class="chip-box" id="chips-box"><ul class="pills" id="chips-pills" aria-labelledby="chips-l"></ul>
<input type="text" class="chip-in" id="chips-in" placeholder="Add a keyword" autocomplete="off"></div>
<p class="say" id="chips-dlg-say" aria-live="polite"></p>
<div class="dlg-acts"><button type="button" class="btn-text" id="chips-cancel">Cancel</button>
<button type="button" class="btn btn-solid" id="chips-save">Save</button></div>
</dialog>"""


SETTINGS_CSS = r"""
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--canvas)}
body{margin:0;background:var(--canvas);color:var(--text-1);font:400 15px/24px var(--font-sans);-webkit-font-smoothing:antialiased}
h1,h2,h3,p,ul,ol,fieldset,legend{margin:0;padding:0}
ul{list-style:none}
button,input,textarea{font:inherit;color:inherit}
.st{margin:0 auto;padding:0 var(--frame-pad) 64px;max-width:var(--frame-max)}
.st-head{display:flex;align-items:center;gap:12px;padding:24px 0 8px}
.st-head h1{font:600 24px/32px var(--font-sans)}
.card{min-width:0;border:1px solid var(--border-subtle);border-radius:14px;background-color:var(--surface)}
.st-cols{display:grid;grid-template-columns:minmax(0,1fr);gap:0 32px;align-items:start}
@media (min-width:1024px){.st-cols{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
.st-sec{margin-top:24px;min-width:0}
.st-card{padding:20px}
.field{display:flex;flex-direction:column;gap:6px;min-width:0}
.field + .field{margin-top:16px}
.field label,.group legend{font-size:14px;line-height:20px;font-weight:600;color:var(--text-1)}
.group{border:0;min-width:0}
.group + .group{margin-top:20px;padding-top:20px;border-top:1px solid var(--border-subtle)}
.group legend{margin-bottom:12px;font-size:15px}
.input{height:40px;padding:0 12px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface-sunken);color:var(--text-1);font-size:15px;line-height:24px;min-width:0}
.input.prose{height:auto;min-height:96px;padding:8px 12px;resize:vertical;width:100%}
.input.num{width:140px}
.input.short{width:96px}
.input[aria-invalid="true"]{border-color:var(--negative)}
.input.changed{border-color:var(--accent)}
.affixed,.inrow{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.affix{font-size:15px;color:var(--text-2)}
.pair{display:flex;flex-wrap:wrap;gap:16px 24px}
.pair .field + .field{margin-top:0}
.form-foot{margin-top:20px}
.btn{display:inline-flex;align-items:center;gap:8px;height:40px;padding:0 16px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface);color:var(--text-1);font:500 14px/20px var(--font-sans);cursor:pointer;white-space:nowrap;text-decoration:none}
.btn:hover{background:var(--row-hover)}
.btn-solid{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
.btn-solid:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn-secondary,.btn-ghost{background:transparent}
.btn-primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn[disabled]{opacity:.55;cursor:not-allowed}
.btn-text{height:40px;padding:0 12px;border:0;background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);text-decoration:underline;text-decoration-color:#5b6770;text-underline-offset:3px;cursor:pointer}
.line{margin-top:16px;font-size:14px;line-height:20px;color:var(--text-2)}
.say{margin-top:12px;font-size:14px;line-height:20px;color:var(--text-2)}
.say:empty{display:none}
.say.bad{color:var(--negative)}
.say .btn{margin:8px 8px 0 0}
.tag{display:inline-flex;align-items:center;height:24px;padding:0 8px;border-radius:12px;font-size:13px;line-height:20px}
.tag-caution{background:var(--caution-tint);color:var(--caution)}
/* The paid rescore's confirm and run, as on the CV page: a full border, no edge bar. */
.notice{margin-top:16px;padding:12px 16px;border:1px solid var(--border-strong);border-radius:10px;background:var(--surface-sunken);font-size:14px;line-height:20px;color:var(--text-2)}
.notice.warn,.notice-caution{border-color:var(--caution);background:var(--caution-tint)}
.notice-bad{border-color:var(--negative);background:var(--negative-tint)}
.notice-ok{border-color:var(--positive);background:var(--positive-tint)}
.notice-info{border-color:var(--accent);background:var(--accent-tint)}
.notice-body{display:flex;gap:10px}
.notice-mark{flex:none;margin-top:2px}
.notice-acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.notice strong{color:var(--text-1)}
.st-missing{margin-top:8px}
.bar{height:6px;margin-top:8px;border-radius:3px;background:var(--border-subtle);overflow:hidden}
.bar span{display:block;height:100%;background:var(--accent)}
/* Title and keywords as chips. */
.tk-row + .tk-row{margin-top:16px;padding-top:16px;border-top:1px solid var(--border-subtle)}
.tk-head{display:flex;align-items:center;gap:10px}
.tk-l{font-size:15px;line-height:20px;font-weight:600}
.pen{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;padding:0;border:1.5px solid var(--accent);border-radius:50%;background:transparent;color:var(--accent);cursor:pointer}
.pen:hover{background:var(--accent-tint)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.chip-v{padding:5px 12px;border-radius:8px;background:#232a30;color:var(--text-1);font-size:14px;line-height:20px;overflow-wrap:anywhere}
/* The chips' dialog, on Mark applied's pattern. */
.chips-dlg{width:min(560px,calc(100vw - 32px));max-width:none;padding:24px;color:var(--text-1);box-shadow:var(--shadow-overlay)}
.chips-dlg::backdrop{background:rgba(0,0,0,.6)}
.dlg-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px}
.dlg-top h2{font:600 26px/32px var(--font-sans)}
.dlg-x{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border:0;border-radius:8px;background:transparent;color:var(--text-2);cursor:pointer}
.dlg-x:hover{background:var(--row-hover);color:var(--text-1)}
.dlg-l{display:block;margin-bottom:6px;font-size:14px;line-height:20px;font-weight:600}
.chip-box{display:flex;flex-wrap:wrap;align-items:center;gap:8px;min-height:48px;padding:8px;border:1px solid var(--border-strong);border-radius:10px;background:var(--surface-sunken)}
.chip-box:focus-within{border-color:var(--accent)}
.pills{display:contents}
.pill-v{display:inline-flex;align-items:center;gap:4px;padding:4px 4px 4px 12px;border-radius:999px;background:#232a30;font-size:14px;line-height:20px;max-width:100%;overflow-wrap:anywhere}
.pill-x{display:inline-flex;align-items:center;justify-content:center;flex:none;width:24px;height:24px;padding:0;border:0;border-radius:50%;background:transparent;color:var(--text-2);cursor:pointer}
.pill-x:hover{background:var(--row-hover);color:var(--text-1)}
.chip-in{flex:1 1 140px;min-width:120px;height:32px;padding:0 4px;border:0;background:transparent;font-size:15px;outline:none}
.dlg-acts{display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-top:24px}
@media (pointer:coarse){.btn,.btn-text,.pen,.dlg-x,.pill-x{min-height:44px;min-width:44px}}
@media (max-width:767.98px){.chips-dlg{width:calc(100vw - 32px);margin:auto 16px;padding:20px}.dlg-top h2{font-size:22px;line-height:28px}}
"""


CHIPS_JS = r"""
(function () {
  var dlg = document.getElementById('chips-dlg');
  if (!dlg) return;
  var KINDS = {
    keywords: { head: 'Edit keywords', label: 'Your keywords', hint: 'Add a keyword', many: true },
    target_title: { head: 'Edit title', label: 'Your titles', hint: 'Add a title', many: false }
  };
  var pills = document.getElementById('chips-pills');
  var input = document.getElementById('chips-in');
  var say = document.getElementById('chips-dlg-say');
  var key = null, values = [], opener = null;
  function split(raw) {
    var out = [];
    String(raw || '').split(',').forEach(function (p) {
      var w = p.replace(/\s+/g, ' ').trim();
      if (w && out.indexOf(w) < 0) out.push(w);
    });
    return out;
  }
  function draw() {
    pills.textContent = '';
    values.forEach(function (v, i) {
      var li = document.createElement('li');
      li.className = 'pill-v';
      li.appendChild(document.createTextNode(v));
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'pill-x';
      x.setAttribute('aria-label', 'Remove ' + v);
      x.innerHTML = '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6"/></svg>';
      x.addEventListener('click', function () { values.splice(i, 1); draw(); input.focus(); });
      li.appendChild(x);
      pills.appendChild(li);
    });
  }
  // A keyword is added on Enter or a comma. A title holds commas of its own,
  // so it is added on Enter only, and replaces the one there.
  function add() {
    var typed = input.value;
    if (KINDS[key].many) {
      split(typed).forEach(function (w) { if (values.indexOf(w) < 0) values.push(w); });
    } else {
      var t = typed.replace(/\s+/g, ' ').trim();
      if (t) values = [t];
    }
    input.value = '';
    draw();
  }
  function open(k, from) {
    key = k; opener = from;
    var kind = KINDS[k];
    document.getElementById('chips-dlg-h').textContent = kind.head;
    document.getElementById('chips-l').textContent = kind.label;
    input.placeholder = kind.hint;
    var stored = document.getElementById('pref-' + k).value;
    values = kind.many ? split(stored) : (stored.trim() ? [stored.trim()] : []);
    input.value = '';
    say.textContent = '';
    draw();
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    input.focus();
  }
  function close() {
    if (dlg.close) dlg.close(); else dlg.removeAttribute('open');
    if (opener && opener.focus) opener.focus();
  }
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-chips-edit]') : null;
    if (b) open(b.getAttribute('data-chips-edit'), b);
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || (e.key === ',' && KINDS[key].many)) { e.preventDefault(); add(); }
    else if (e.key === 'Backspace' && !input.value && values.length) { values.pop(); draw(); }
  });
  document.getElementById('chips-x').addEventListener('click', close);
  document.getElementById('chips-cancel').addEventListener('click', close);
  dlg.addEventListener('cancel', function () {
    Promise.resolve().then(function () { if (opener && opener.focus) opener.focus(); });
  });
  document.getElementById('chips-save').addEventListener('click', function () {
    if (input.value.trim()) add();
    var btn = this;
    btn.disabled = true;
    fetch('/profile/setting', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: key, value: values.join(', ') })
    }).then(function (r) { return r.json(); }).then(function (j) {
      btn.disabled = false;
      if (j && j.ok === false) { say.textContent = j.note || 'That did not stick.'; say.className = 'say bad'; return; }
      location.reload();
    }).catch(function () { btn.disabled = false; say.textContent = 'The server did not answer.'; say.className = 'say bad'; });
  });
})();
"""


def settings_js() -> str:
    """The CV page's own setup, Rescore affected and Preferences code, by the
    section markers in cv_screen.CV_SCREEN_JS, closed as one function."""
    js = cv_screen.CV_SCREEN_JS
    head = js[:js.index("  // ------------------------------------------------------- the sheet")]
    rescore = js[js.index("  // ------------------------------------------------- Rescore affected"):
                 js.index("  // ------------------------------------------------- Preferences")]
    prefs = js[js.index("  // ------------------------------------------------- Preferences"):
               js.index("  // ------------------------------------------------- the Word import")]
    return head + rescore + prefs + "})();\n"


def render_page(config: dict, db_path: Path | None, jobs: dict | None = None) -> bytes:
    """The Settings page as bytes. Read only, like every page."""
    prefs = cv_screen.pref_values(config)
    conn = cv_screen._ro(db_path)
    try:
        open_scope = cv_screen.affected_summary(conn, config, "open")
    finally:
        if conn is not None:
            conn.close()
    data = {"activeNumber": 0, "stale": None, "open": open_scope, "run": cv_screen.rescore_status(),
            "prefs": {k: prefs.get(k) for k in radar_common.SCORING_PREFS}, "live": prefs["live"]}
    page = f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings, MedTech Radar</title>
<link rel="icon" href="{build_dashboard.FAVICON}">
<style>{page_frame.FRAME_CSS}{page_frame.GROUPS_CSS}{page_frame.TITLES_CSS}{SETTINGS_CSS}</style>
</head>
<body>
{page_frame.appbar_html("settings", jobs)}
<main class="st" id="main" data-page="{esc(json.dumps(data, ensure_ascii=False))}">
{body_html(config, prefs)}
<p class="visually-hidden" id="cv-live" aria-live="assertive"></p>
</main>
<script>{build_dashboard.SCRIPT}{page_frame.FRAME_SCRIPT}{settings_js()}{build_dashboard.PREFS_SCRIPT}{CHIPS_JS}</script>
</body>
</html>"""
    return page.encode("utf-8")
