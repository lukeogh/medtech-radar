#!/usr/bin/env python
"""The experience bank's pages (owner, 18 Nov): /cv/experience, the entries in
three sections, and /cv/experience/<id> (or /new), one entry's form.

Both are in the CV group, rose, and the menu marks CV. The data and every rule
are experience.py's; these pages only draw, read only like every page. Their
writes are the three POSTs serve_dashboard routes to experience.py:
/cv/experience/save (create, edit, and a status move in the same write),
/cv/experience/delete and /cv/experience/paste. No model is asked anything
and nothing is fetched from another site.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_dashboard  # noqa: E402
import experience as xp  # noqa: E402
import page_frame  # noqa: E402

esc = page_frame.esc

SECTIONS = (("draft", "To review", "Nothing to review."),
            ("backed", "Backed", "Nothing backed yet."),
            ("parked", "Parked", "Nothing parked."))
STATUS_WORD = dict(xp.STATUSES)

CLOSE = ('<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false">'
         '<path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6"/></svg>')


def _ro(db_path: Path | None):
    """A read-only connection, or None when there is no database yet."""
    if db_path is None or not Path(db_path).exists():
        return None
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def stage_chip(stage: str) -> str:
    return f'<span class="stage st-{esc(stage)}">{esc(xp.STAGE_WORD.get(stage, "Drafted"))}</span>'


def tag_chips(tags: list, cls: str = "chips") -> str:
    if not tags:
        return ""
    return f'<ul class="{cls}">' + "".join(f'<li class="chip-v">{esc(t)}</li>' for t in tags) + "</ul>"


# ------------------------------------------------------------------ the list

def card_html(e: dict) -> str:
    facts = page_frame.divided([esc(e.get("place") or ""), esc(e.get("period") or "")], "xp-facts")
    has_facts = bool((e.get("place") or "").strip() or (e.get("period") or "").strip())
    return (f'<li><a class="card g-cv glow-faint xp-card" href="/cv/experience/{e["id"]}">'
            f'<span class="xp-main"><span class="xp-t">{esc(e["title"])}</span>'
            f'{facts if has_facts else ""}{tag_chips(e["tag_list"])}</span>'
            f'<span class="xp-side">{stage_chip(e["stage"])}'
            f'<span class="xp-ev">{e["evidence_count"]} evidence</span></span></a></li>')


def counts_html(entries: list) -> str:
    backed = sum(1 for e in entries if e["status"] == "backed")
    review = sum(1 for e in entries if e["status"] == "draft")
    return (f'<span class="pill" id="xp-n-backed">{backed} backed</span>'
            f'<span class="pill" id="xp-n-review">{review} to review</span>')


def lists_html(entries: list) -> str:
    out = []
    for key, title, empty in SECTIONS:
        mine = [e for e in entries if e["status"] == key]
        body = (f'<ul class="xp-list">{"".join(card_html(e) for e in mine)}</ul>' if mine
                else f'<p class="xp-none">{esc(empty)}</p>')
        out.append(f'<section class="xp-sec" aria-labelledby="xp-{key}-h"><header class="sec-head">'
                   f'<h2 class="sec-title" id="xp-{key}-h">{esc(title)}</h2>'
                   f'<span class="sec-count">{len(mine)}</span></header>{body}</section>')
    return "".join(out)


PASTE_DIALOG = f"""<dialog class="xp-dlg card" id="xp-paste-dlg" aria-labelledby="xp-paste-h">
<div class="dlg-top"><h2 id="xp-paste-h">Paste entries</h2>
<button type="button" class="dlg-x" id="xp-paste-x" aria-label="Close">{CLOSE}</button></div>
<label class="dlg-l" for="xp-paste-in">Entries</label>
<textarea class="input xp-paste-in" id="xp-paste-in" rows="14" spellcheck="false" maxlength="{xp.PASTE_MAX}"></textarea>
<p class="say" id="xp-paste-say" aria-live="polite"></p>
<div class="dlg-acts"><button type="button" class="btn-text" id="xp-paste-cancel">Cancel</button>
<button type="button" class="btn btn-rose" id="xp-paste-go">Add as drafts</button></div>
</dialog>"""


def list_body(entries: list) -> str:
    return (f'<header class="xp-head">{page_frame.chip("cv", "cv")}<h1>Experience</h1>'
            f'<span class="xp-pills" id="xp-pills">{counts_html(entries)}</span>'
            '<span class="xp-acts"><button type="button" class="btn" id="xp-paste-open" '
            'aria-haspopup="dialog">Paste entries</button>'
            '<a class="btn btn-rose" href="/cv/experience/new">Add entry</a></span></header>'
            f'{page_frame.cv_switch("experience")}'
            '<div class="xp-added" id="xp-added" role="status" hidden></div>'
            f'<div id="xp-lists">{lists_html(entries)}</div>{PASTE_DIALOG}')


# ------------------------------------------------------------------ one entry

def _input(key: str, label: str, value: str, cap: int, area: int = 0) -> str:
    if area:
        ctl = (f'<textarea class="input prose" id="xp-{key}" name="{key}" rows="{area}" '
               f'maxlength="{cap}">{esc(value)}</textarea>')
    else:
        ctl = (f'<input type="text" class="input" id="xp-{key}" name="{key}" value="{esc(value)}" '
               f'maxlength="{cap}" autocomplete="off">')
    return f'<div class="field"><label for="xp-{key}">{esc(label)}</label>{ctl}</div>'


def _level_select(level: str) -> str:
    return "".join(f'<option value="{k}"{" selected" if k == level else ""}>{esc(w)}</option>'
                   for k, w in xp.LEVELS)


def evidence_row(ev: dict | None = None) -> str:
    ev = ev or {"name": "", "location": "", "shows": "", "level": "describe_only"}
    caps = xp.CAPS

    def box(key: str, label: str) -> str:
        return (f'<label class="ev-f"><span class="ev-l">{esc(label)}</span>'
                f'<input type="text" class="input" data-ev="{key}" value="{esc(ev.get(key) or "")}" '
                f'maxlength="{caps["ev_" + key]}" autocomplete="off"></label>')
    return (f'<li class="ev-row">{box("name", "Name")}{box("location", "Where it lives")}'
            f'{box("shows", "What it shows")}<label class="ev-f ev-lv"><span class="ev-l">Level</span>'
            f'<select class="input" data-ev="level">{_level_select(ev.get("level") or "describe_only")}</select></label>'
            '<button type="button" class="btn-text ev-x" data-ev-remove>Remove</button></li>')


def _status_buttons(e: dict) -> str:
    status = e["status"]
    why = xp.backing_problem(e["stage"], e["evidence_count"], e.get("vouch"))
    out = []
    if status != "backed":
        out.append(f'<button type="button" class="btn" data-xp-status="backed" id="xp-back"'
                   f' aria-describedby="xp-why"{" disabled" if why else ""}>Mark backed</button>'
                   f'<span class="xp-why" id="xp-why">{esc(why or "")}</span>')
    if status != "parked":
        out.append('<button type="button" class="btn" data-xp-status="parked">Park</button>')
    if status != "draft":
        out.append('<button type="button" class="btn" data-xp-status="draft">Back to review</button>')
    return "".join(out)


DELETE_DIALOG = f"""<dialog class="xp-dlg xp-del-dlg card" id="xp-del-dlg" aria-labelledby="xp-del-h">
<div class="dlg-top"><h2 id="xp-del-h">Delete this entry and its evidence?</h2>
<button type="button" class="dlg-x" id="xp-del-x" aria-label="Close">{CLOSE}</button></div>
<p class="say" id="xp-del-say" aria-live="polite"></p>
<div class="dlg-acts"><button type="button" class="btn-text" id="xp-del-cancel">Cancel</button>
<button type="button" class="btn btn-danger" id="xp-del-go">Delete</button></div>
</dialog>"""


def entry_body(e: dict | None) -> str:
    new = e is None
    e = e or {"id": None, "title": "", "did": "", "result": "", "period": "", "place": "",
              "tag_list": [], "stage": "drafted", "vouch": "", "questions": "", "status": "draft",
              "evidence": [], "evidence_count": 0}
    caps = xp.CAPS
    title = "New entry" if new else e["title"]
    status_pill = "" if new else f'<span class="pill st-{e["status"]}">{esc(STATUS_WORD[e["status"]])}</span>'
    stages = "".join(
        f'<label class="seg"><input type="radio" name="stage" value="{k}"'
        f'{" checked" if k == e["stage"] else ""}><span>{esc(w)}</span></label>'
        for k, w in xp.STAGES)
    tags = ", ".join(e["tag_list"])
    fields = (_input("title", "Title", e["title"], caps["title"])
              + _input("did", "What I did", e.get("did") or "", caps["did"], 5)
              + _input("result", "Result", e.get("result") or "", caps["result"], 3)
              + '<div class="pair">' + _input("period", "When", e.get("period") or "", caps["period"])
              + _input("place", "Where", e.get("place") or "", caps["place"]) + '</div>'
              + '<div class="field"><label for="xp-tags">Tags</label>'
              f'<input type="text" class="input" id="xp-tags" name="tags" value="{esc(tags)}" autocomplete="off">'
              f'<span id="xp-tag-chips">{tag_chips(e["tag_list"])}</span></div>'
              + f'<fieldset class="field segs"><legend>Stage</legend><span class="seg-row">{stages}</span></fieldset>'
              + _input("vouch", "Who can vouch", e.get("vouch") or "", caps["vouch"])
              + _input("questions", "Questions", e.get("questions") or "", caps["questions"], 3))
    evidence = (f'<section class="xp-evsec" aria-labelledby="xp-ev-h"><h2 class="xp-h2" id="xp-ev-h">Evidence</h2>'
                f'<ul class="ev-list" id="xp-ev-list">{"".join(evidence_row(v) for v in e["evidence"])}</ul>'
                '<p class="ev-add"><button type="button" class="btn" id="xp-ev-add">Add evidence</button></p>'
                f'<template id="xp-ev-tpl">{evidence_row()}</template></section>')
    foot = ('<div class="xp-foot"><button type="submit" class="btn btn-rose" id="xp-save">Save</button>'
            + ("" if new else _status_buttons(e)
               + '<button type="button" class="btn-text xp-del" id="xp-del-open" aria-haspopup="dialog">Delete</button>')
            + '</div><p class="say" id="xp-say" aria-live="polite"></p>')
    data = {"id": e["id"], "status": e["status"]}
    return (f'<p class="xp-backlink"><a class="plain" href="/cv/experience">Back to Experience</a></p>'
            f'<header class="xp-head">{page_frame.chip("cv", "cv")}<h1 id="xp-h1">{esc(title)}</h1>{status_pill}</header>'
            f'<form class="card g-cv glow-faint xp-form" id="xp-form" novalidate '
            f'data-entry="{esc(json.dumps(data))}" aria-labelledby="xp-h1">{fields}{evidence}{foot}</form>'
            + ("" if new else DELETE_DIALOG))


# ------------------------------------------------------------------ the page

def _page(title: str, body: str, jobs: dict | None) -> bytes:
    page = f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}, MedTech Radar</title>
<link rel="icon" href="{build_dashboard.FAVICON}">
<style>{page_frame.FRAME_CSS}{page_frame.GROUPS_CSS}{page_frame.TITLES_CSS}{page_frame.SWITCH_CSS}{XP_CSS}</style>
</head>
<body>
{page_frame.appbar_html("cv", jobs)}
<main class="xp" id="main">
{body}
</main>
<script>{build_dashboard.SCRIPT}{page_frame.FRAME_SCRIPT}{XP_JS}</script>
</body>
</html>"""
    return page.encode("utf-8")


def render_list(db_path: Path | None, jobs: dict | None = None) -> bytes:
    conn = _ro(db_path)
    try:
        entries = xp.list_entries(conn) if conn is not None else []
    finally:
        if conn is not None:
            conn.close()
    return _page("Experience", list_body(entries), jobs)


def render_entry(db_path: Path | None, entry_id: int | None, jobs: dict | None = None) -> bytes | None:
    """One entry's page, the empty form for entry_id None, or None for no such entry."""
    entry = None
    if entry_id is not None:
        conn = _ro(db_path)
        try:
            entry = xp.get_entry(conn, entry_id) if conn is not None else None
        finally:
            if conn is not None:
                conn.close()
        if entry is None:
            return None
    return _page(entry["title"] if entry else "New entry", entry_body(entry), jobs)


# ------------------------------------------------------------------ style

XP_CSS = r"""
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--canvas)}
body{margin:0;background:var(--canvas);color:var(--text-1);font:400 15px/24px var(--font-sans);-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
h1,h2,h3,p,ul,ol,fieldset,legend{margin:0;padding:0}
ul{list-style:none}
button,input,textarea,select{font:inherit;color:inherit}
.xp{margin:0 auto;padding:24px var(--frame-pad) 64px;max-width:var(--frame-max)}
.xp a.plain{color:var(--accent);text-underline-offset:3px}
.xp a.plain:hover{color:var(--accent-hover)}
/* The title row: the rose chip, the page, its count pills, and the two buttons at the right. */
.xp-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:0 0 16px}
.xp-head h1{font:600 24px/32px var(--font-sans);min-width:0;overflow-wrap:anywhere}
.xp-pills{display:inline-flex;flex-wrap:wrap;gap:8px}
.xp-acts{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto}
.pill{display:inline-flex;align-items:center;height:24px;padding:0 10px;border:1px solid #2a3238;border-radius:12px;background:#1a2025;font-size:13px;line-height:20px;font-weight:500;color:var(--text-2);white-space:nowrap}
.pill.st-backed{border-color:rgba(116,211,155,.45);background:var(--positive-tint);color:var(--positive)}
.pill.st-parked{color:var(--text-2)}
.pill.st-draft{border-color:rgb(var(--group-cv-rgb) / .45);background:rgb(var(--group-cv-rgb) / .12);color:var(--group-cv)}
.sec-count{font-size:13px;line-height:20px}
.card{min-width:0;border:1px solid var(--border-subtle);border-radius:14px;background-color:var(--surface)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:40px;padding:0 16px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface);color:var(--text-1);font:500 14px/20px var(--font-sans);cursor:pointer;white-space:nowrap;text-decoration:none}
.btn:hover{background:var(--row-hover)}
.btn[disabled]{opacity:.55;cursor:not-allowed}
.btn-rose,.xp a.btn-rose{background:#e88fb4;border-color:#e88fb4;color:#2a0f1c;font-weight:600}
.btn-rose:hover,.xp a.btn-rose:hover{background:#efa9c6;border-color:#efa9c6;color:#2a0f1c}
.btn-danger{background:var(--negative);border-color:var(--negative);color:#2a0d0a;font-weight:600}
.btn-text{height:40px;padding:0 12px;border:0;background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);text-decoration:underline;text-decoration-color:#5b6770;text-underline-offset:3px;cursor:pointer}
.say{margin-top:12px;font-size:14px;line-height:20px;color:var(--text-2)}
.say:empty{display:none}
.say.bad{color:var(--negative)}
/* The three sections, titles outside their cards. */
.xp-sec{margin-top:28px;min-width:0}
.xp-list{display:flex;flex-direction:column;gap:12px}
.xp-none{font-size:14px;line-height:20px;color:var(--text-2)}
.xp-card{display:flex;align-items:flex-start;gap:16px;padding:16px 20px;color:var(--text-1);text-decoration:none}
.xp-card:hover{border-color:var(--border-strong)}
.xp-main{display:flex;flex-direction:column;gap:6px;flex:1 1 auto;min-width:0}
.xp-t{font-size:16px;line-height:24px;font-weight:600;overflow-wrap:anywhere}
.xp-facts{font-size:14px;line-height:20px;color:var(--text-2)}
.xp-side{display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex:none}
.xp-ev{font-size:14px;line-height:20px;color:var(--text-2);white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.chip-v{padding:5px 12px;border-radius:8px;background:#232a30;color:var(--text-1);font-size:14px;line-height:20px;overflow-wrap:anywhere}
.stage{display:inline-flex;align-items:center;height:24px;padding:0 10px;border-radius:12px;border:1px solid;font-size:13px;line-height:20px;font-weight:500;white-space:nowrap}
.st-delivered{border-color:rgba(116,211,155,.45);background:var(--positive-tint);color:var(--positive)}
.st-drafted{border-color:rgba(240,196,106,.5);background:var(--caution-tint);color:var(--caution)}
.st-discussed{border-color:#2a3238;background:var(--neutral-tint);color:var(--neutral)}
/* What a paste added and what it could not read. */
.xp-added{margin-bottom:8px;padding:12px 16px;border:1px solid rgb(var(--group-cv-rgb) / .5);border-radius:10px;background:rgb(var(--group-cv-rgb) / .08);font-size:14px;line-height:20px}
.xp-added ul{margin-top:8px;display:flex;flex-direction:column;gap:8px}
.xp-added .fl{display:block;font-weight:600;overflow-wrap:anywhere}
.xp-added .why{display:block;color:var(--text-2)}
/* The dialogs, on Settings' pattern. */
.xp-dlg{width:min(720px,calc(100vw - 32px));max-width:none;padding:24px;color:var(--text-1);box-shadow:var(--shadow-overlay)}
.xp-del-dlg{width:min(480px,calc(100vw - 32px))}
.xp-dlg::backdrop{background:rgba(0,0,0,.6)}
.dlg-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:16px}
.dlg-top h2{font:600 22px/28px var(--font-sans)}
.dlg-x{display:inline-flex;align-items:center;justify-content:center;flex:none;width:36px;height:36px;border:0;border-radius:8px;background:transparent;color:var(--text-2);cursor:pointer}
.dlg-x:hover{background:var(--row-hover);color:var(--text-1)}
.dlg-l{display:block;margin-bottom:6px;font-size:14px;line-height:20px;font-weight:600}
.dlg-acts{display:flex;justify-content:flex-end;align-items:center;flex-wrap:wrap;gap:8px;margin-top:20px}
.input{width:100%;height:40px;padding:0 12px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface-sunken);color:var(--text-1);font-size:15px;line-height:24px;min-width:0}
.input.prose,.xp-paste-in{height:auto;padding:8px 12px;resize:vertical}
.xp-paste-in{min-height:280px;font:400 14px/20px var(--font-mono)}
select.input{padding-right:8px}
/* One entry. */
.xp-backlink{margin:0 0 12px;font-size:14px;line-height:20px}
.xp-form{padding:24px;max-width:960px}
.field{display:flex;flex-direction:column;gap:6px;min-width:0;border:0}
.field + .field,.pair + .field,.field + .pair{margin-top:16px}
.field label,.field legend{font-size:14px;line-height:20px;font-weight:600;color:var(--text-1)}
.field legend{margin-bottom:6px}
.pair{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
.pair .field + .field{margin-top:0}
.seg-row{display:flex;flex-wrap:wrap;gap:8px}
.seg{position:relative;display:inline-flex}
.seg input{position:absolute;opacity:0;width:1px;height:1px}
.seg span{display:inline-flex;align-items:center;height:36px;padding:0 14px;border:1px solid var(--border-strong);border-radius:8px;font-size:14px;line-height:20px;cursor:pointer}
.seg input:checked + span{border-color:var(--group-cv);background:rgb(var(--group-cv-rgb) / .14);color:var(--text-1);font-weight:600}
.seg input:focus-visible + span{outline:2px solid var(--focus);outline-offset:2px}
.xp-evsec{margin-top:24px;padding-top:20px;border-top:1px solid var(--border-subtle)}
.xp-h2{font-size:17px;line-height:24px;font-weight:600;margin-bottom:12px}
.ev-list{display:flex;flex-direction:column;gap:12px}
.ev-row{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) minmax(0,1.4fr) minmax(0,.9fr) auto;gap:12px;align-items:end;padding:12px;border:1px solid var(--border-subtle);border-radius:10px;background:var(--surface-sunken)}
.ev-row .input{background:var(--surface)}
.ev-f{display:flex;flex-direction:column;gap:4px;min-width:0}
.ev-l{font-size:13px;line-height:20px;font-weight:600;color:var(--text-2)}
.ev-add{margin-top:12px}
.xp-foot{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-top:24px;padding-top:20px;border-top:1px solid var(--border-subtle)}
.xp-why{font-size:14px;line-height:20px;color:var(--text-2)}
.xp-why:empty{display:none}
.xp-del{margin-left:auto;color:var(--negative);text-decoration-color:var(--negative)}
@media (max-width:1023.98px){.ev-row{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.ev-row .ev-x{justify-self:start}}
@media (max-width:767.98px){
  .xp-head h1{flex:1 1 auto}
  .xp-acts{flex-basis:100%;margin-left:0}
  .xp-card{flex-direction:column;gap:10px;padding:16px}
  .xp-side{flex-direction:row;align-items:center;gap:12px}
  .xp-form{padding:16px}
  .pair,.ev-row{grid-template-columns:minmax(0,1fr)}
  .xp-dlg{width:calc(100vw - 32px);margin:auto 16px;padding:20px}
  .xp-del{margin-left:0}
}
@media (pointer:coarse){.btn,.btn-text,.dlg-x{min-height:44px;min-width:44px}.seg span{height:44px}}
"""


XP_JS = r"""
(function () {
  function $(id) { return document.getElementById(id); }
  function post(path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return { ok: false }; }); });
  }
  function bad(el, text) { el.textContent = text; el.className = 'say bad'; }
  function openDlg(d) { if (d.showModal) d.showModal(); else d.setAttribute('open', ''); }
  function closeDlg(d) { if (d.close) d.close(); else d.removeAttribute('open'); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }

  // ------------------------------------------------ the list: Paste entries
  var pdlg = $('xp-paste-dlg');
  if (pdlg) {
    var opener = $('xp-paste-open'), input = $('xp-paste-in'), psay = $('xp-paste-say');
    var go = $('xp-paste-go'), result = $('xp-added');
    function pclose() { closeDlg(pdlg); opener.focus(); }
    opener.addEventListener('click', function () { psay.textContent = ''; openDlg(pdlg); input.focus(); });
    $('xp-paste-x').addEventListener('click', pclose);
    $('xp-paste-cancel').addEventListener('click', pclose);
    function show(j) {
      result.textContent = '';
      var head = document.createElement('p');
      head.textContent = j.added ? plural(j.added, 'entry', 'entries') + ' added as drafts.' : 'No entries added.';
      result.appendChild(head);
      if (j.refused && j.refused.length) {
        var p = document.createElement('p');
        p.textContent = plural(j.refused.length, 'block') + ' not read';
        p.style.marginTop = '8px';
        result.appendChild(p);
        var ul = document.createElement('ul');
        j.refused.forEach(function (r) {
          var li = document.createElement('li');
          var a = document.createElement('span'); a.className = 'fl'; a.textContent = r.first_line || 'An empty block';
          var b = document.createElement('span'); b.className = 'why'; b.textContent = r.why;
          li.appendChild(a); li.appendChild(b); ul.appendChild(li);
        });
        result.appendChild(ul);
      }
      result.hidden = false;
    }
    go.addEventListener('click', function () {
      if (!input.value.trim()) { input.focus(); return; }
      go.disabled = true;
      post('/cv/experience/paste', { text: input.value }).then(function (j) {
        go.disabled = false;
        if (!j || !j.ok) { bad(psay, (j && j.note) || 'That did not stick.'); return; }
        input.value = '';
        return fetch('/cv/experience').then(function (r) { return r.text(); }).then(function (html) {
          var doc = new DOMParser().parseFromString(html, 'text/html');
          ['xp-lists', 'xp-pills'].forEach(function (id) {
            var fresh = doc.getElementById(id);
            if (fresh && $(id)) $(id).innerHTML = fresh.innerHTML;
          });
          closeDlg(pdlg);
          show(j);
          opener.focus();
        });
      }).catch(function () { go.disabled = false; bad(psay, 'The server did not answer.'); });
    });
  }

  // ------------------------------------------------ one entry
  var form = $('xp-form');
  if (!form) return;
  var entry = JSON.parse(form.getAttribute('data-entry'));
  var say = $('xp-say'), list = $('xp-ev-list'), tpl = $('xp-ev-tpl');
  var backBtn = $('xp-back'), why = $('xp-why');
  var dirty = false;
  function val(id) { return $(id).value; }
  function tags() {
    var out = [];
    val('xp-tags').split(',').forEach(function (t) {
      t = t.replace(/\s+/g, ' ').trim();
      if (t && out.map(function (x) { return x.toLowerCase(); }).indexOf(t.toLowerCase()) < 0) out.push(t);
    });
    return out;
  }
  function evidence() {
    var out = [];
    Array.prototype.forEach.call(list.querySelectorAll('.ev-row'), function (row) {
      var ev = {};
      Array.prototype.forEach.call(row.querySelectorAll('[data-ev]'), function (el) {
        ev[el.getAttribute('data-ev')] = el.value;
      });
      if ((ev.name || '').trim() || (ev.location || '').trim() || (ev.shows || '').trim()) out.push(ev);
    });
    return out;
  }
  function stage() {
    var c = form.querySelector('input[name="stage"]:checked');
    return c ? c.value : 'drafted';
  }
  function body(status) {
    var b = { title: val('xp-title'), did: val('xp-did'), result: val('xp-result'),
              period: val('xp-period'), place: val('xp-place'), tags: tags(), stage: stage(),
              vouch: val('xp-vouch'), questions: val('xp-questions'), evidence: evidence() };
    if (entry.id !== null) b.id = entry.id;
    if (status) b.status = status;
    return b;
  }
  // The rule, as the server holds it: evidence or a named voucher, and not discussed.
  function rule() {
    if (stage() === 'discussed') return "Discussed work can't be backed.";
    var vouch = val('xp-vouch').trim();
    if (/^(none|nobody named)\.?$/i.test(vouch)) vouch = '';
    if (!evidence().length && !vouch) return 'Add evidence or someone who can vouch.';
    return '';
  }
  function update() {
    if (!backBtn) return;
    var w = rule();
    backBtn.disabled = !!w;
    why.textContent = w;
  }
  function drawTags() {
    var box = $('xp-tag-chips'), t = tags();
    box.textContent = '';
    if (!t.length) return;
    var ul = document.createElement('ul');
    ul.className = 'chips';
    t.forEach(function (x) { var li = document.createElement('li'); li.className = 'chip-v'; li.textContent = x; ul.appendChild(li); });
    box.appendChild(ul);
  }
  form.addEventListener('input', function (e) {
    dirty = true;
    if (e.target && e.target.id === 'xp-tags') drawTags();
    update();
  });
  form.addEventListener('change', function () { dirty = true; update(); });
  window.addEventListener('beforeunload', function (e) { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
  $('xp-ev-add').addEventListener('click', function () {
    var row = tpl.content.firstElementChild.cloneNode(true);
    list.appendChild(row);
    row.querySelector('[data-ev="name"]').focus();
  });
  list.addEventListener('click', function (e) {
    var x = e.target.closest ? e.target.closest('[data-ev-remove]') : null;
    if (!x) return;
    var row = x.closest('.ev-row');
    var next = row.nextElementSibling || row.previousElementSibling;
    row.parentNode.removeChild(row);
    dirty = true;
    update();
    (next ? next.querySelector('[data-ev="name"]') : $('xp-ev-add')).focus();
  });
  var busy = false;
  function save(status) {
    if (busy) return;
    if (!val('xp-title').trim()) { bad(say, 'Add a title.'); $('xp-title').focus(); return; }
    busy = true;
    say.textContent = '';
    post('/cv/experience/save', body(status)).then(function (j) {
      busy = false;
      if (!j || !j.ok) { bad(say, (j && j.note) || 'That did not stick.'); return; }
      dirty = false;
      if (j.created) { location.href = '/cv/experience/' + j.id; return; }
      if (status) { location.reload(); return; }
      say.className = 'say';
      say.textContent = 'Saved.';
      $('xp-h1').textContent = val('xp-title').trim();
    }).catch(function () { busy = false; bad(say, 'The server did not answer.'); });
  }
  form.addEventListener('submit', function (e) { e.preventDefault(); save(null); });
  Array.prototype.forEach.call(form.querySelectorAll('[data-xp-status]'), function (b) {
    b.addEventListener('click', function () { save(b.getAttribute('data-xp-status')); });
  });
  update();

  var ddlg = $('xp-del-dlg');
  if (ddlg) {
    var dOpen = $('xp-del-open'), dsay = $('xp-del-say');
    function dclose() { closeDlg(ddlg); dOpen.focus(); }
    dOpen.addEventListener('click', function () { dsay.textContent = ''; openDlg(ddlg); $('xp-del-cancel').focus(); });
    $('xp-del-x').addEventListener('click', dclose);
    $('xp-del-cancel').addEventListener('click', dclose);
    $('xp-del-go').addEventListener('click', function () {
      var btn = this;
      btn.disabled = true;
      post('/cv/experience/delete', { id: entry.id }).then(function (j) {
        btn.disabled = false;
        if (!j || !j.ok) { bad(dsay, (j && j.note) || 'That did not stick.'); return; }
        dirty = false;
        location.href = '/cv/experience';
      }).catch(function () { btn.disabled = false; bad(dsay, 'The server did not answer.'); });
    });
  }
})();
"""
