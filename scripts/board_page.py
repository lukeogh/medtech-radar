"""The applications board: the Jobs page's Applied view laid out as a Board.

Built from design/jobs/applied-board v2 (2560 and phone, design/round-1
branch). Six columns, Preparing to Closed, "Waiting on you" across the top, and
every move made with the Move to menu or a key, never by dragging. The page is
drawn from the dict build_dashboard.collect fills and never writes; its script
moves items through POST /application/move, sets next steps through POST
/application/next, and redraws the board from GET /jobs?show=applied&part=board.

What the store does and who moves what is in applications.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import applications as ap
import build_dashboard as bd
import jobs_page as jp

esc = bd.esc

COLUMN_NOTES = {
    "preparing": "A tailored CV is saved and the role is not applied yet.",
    "applied": ("You marked the role applied. After 21 days with no movement, "
                "Radar closes it as no reply."),
    "conversation": "The company or its recruiter replied. You move items here.",
    "interviewing": "An interview is booked or under way.",
    "offer": "The company made you an offer.",
    "closed": "Each item says why: no reply, turned down or withdrawn.",
}
EMPTY = {
    "preparing": "Nothing in preparation. Save a tailored CV on Prepare application and the role lands here.",
    "applied": "No applications waiting. Mark a role applied on Jobs or Prepare application and it lands here.",
    "conversation": "No conversations yet. Move an item here when the company or its recruiter replies.",
    "interviewing": "No interviews yet. Move an item here when one is booked.",
    "offer": ("No offers yet. An item lands here when you move it after an offer. "
              "Radar never moves one here."),
    "closed": "Nothing closed.",
}
# The column keys of the Move to menu, and the close keys with their reason.
COLUMN_KEYS = {"preparing": "1", "applied": "2", "conversation": "3",
               "interviewing": "4", "offer": "5"}
CLOSE_KEYS = {"no-reply": "n", "turned-down": "t", "withdrawn": "w"}
REASON_SHAPES = {"no-reply": "ic-clock", "turned-down": "st-neg", "withdrawn": "ic-back"}
SYMBOLS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
<symbol id="ic-clock" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.8" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4.6V8l2.4 1.6" fill="none" stroke="currentColor" stroke-width="1.5"/></symbol>
<symbol id="ic-back" viewBox="0 0 16 16"><path d="M6 4L2.8 7.2 6 10.4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M3.2 7.2h6.3a3.3 3.3 0 0 1 0 6.6H8" fill="none" stroke="currentColor" stroke-width="1.5"/></symbol>
<symbol id="ic-warn" viewBox="0 0 16 16"><path d="M8 2.2l6.2 11H1.8z" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 6.4v3.2M8 11.2v.4" stroke="currentColor" stroke-width="1.6"/></symbol>
</svg>"""
DOT = " · "
BOARD_KEYMAP = (("← ↑ → ↓", "Move between items"),
                ("Enter", "Open the role"), ("M", "Open Move to for the item"),
                ("2 to 5", "In Move to, move to that column"),
                ("N T W", "In Move to, close as no reply, turned down or withdrawn"),
                ("Z", "Undo the last move"), ("?", "Show this list"))


# ------------------------------------------------------------------ words

def _date(moment: datetime) -> str:
    return f"{moment:%a} {moment.day} {moment:%b}"


def due_words(due, now: datetime) -> str:
    """'due today', 'due tomorrow', 'by Mon 16 Nov', '2 days overdue'."""
    day = ap.parse(due)
    if day is None:
        return ""
    gap = (day.date() - now.date()).days
    if gap == 0:
        return "due today"
    if gap == 1:
        return "due tomorrow"
    if gap < 0:
        return f"{-gap} {'day' if gap == -1 else 'days'} overdue"
    return f"by {_date(day)}"


def interview_words(at) -> str:
    moment = ap.parse(at)
    if moment is None:
        return ""
    return f"Interview {_date(moment)}, {moment:%H:%M}"


def uk_time(moment: datetime) -> str:
    """HH:MM in UK time, which is what the page says its times are."""
    try:
        from zoneinfo import ZoneInfo
        moment = moment.astimezone(ZoneInfo("Europe/London"))
    except Exception:  # noqa: BLE001  no tz database: UTC, right all winter
        pass
    return f"{moment:%H:%M}"


def _days(n: int) -> str:
    return f"{n} {'day' if n == 1 else 'days'}"


# ------------------------------------------------------------------ model

def board_model(data: dict, now=None, loc: tuple = ()) -> dict:
    """The six columns, Waiting on you and the counts, from one collect().

    An applied role whose application has not been made yet (stamped by
    something other than Mark applied, until the next scheduled rule makes it)
    is drawn in Applied all the same, without a Move to button, so the board
    never leaves out an application.
    The counts are of every application; the columns hold the ones the
    location chips match, as the queue's do.
    """
    moment = ap._now(now)
    loaded = data.get("applications")
    apps = [dict(a) for a in (loaded or [])]
    refs = {a["role_ref"] for a in apps}
    for face in data.get("applied") or []:
        ref = ap.role_ref(face)
        if ref in refs:
            continue
        refs.add(ref)
        apps.append({"id": None, "role_ref": ref, "opportunity_id": face.get("id"),
                     "stage": "applied", "stage_at": face.get("applied_at"),
                     "stage_by": ap.RADAR, "moved_at": face.get("applied_at"),
                     "created_at": None, "title": face.get("title"),
                     "company": face.get("company"), "region": face.get("region"),
                     "location_class": face.get("location_class"),
                     "applied_on": face.get("applied_at"),
                     "applied_cv": face.get("applied_cv")})
    totals = {s: 0 for s in ap.STAGES}
    reasons = {r: 0 for r in ap.REASONS}
    columns: dict = {s: [] for s in ap.STAGES}
    for a in apps:
        totals[a["stage"]] += 1
        if a["stage"] == "closed" and a.get("closed_reason") in reasons:
            reasons[a["closed_reason"]] += 1
        if jp.loc_matches(a, loc):
            columns[a["stage"]].append(a)
    for stage, items in columns.items():
        when = "applied_on" if stage == "applied" else "stage_at"
        items.sort(key=lambda a: (str(a.get(when) or a.get("stage_at") or ""),
                                  a.get("id") or 0), reverse=True)
    waiting = [a for a in apps if a["stage"] in ap.OPEN_STAGES
               and (a.get("next_step") or a.get("interview_at"))
               and jp.loc_matches(a, loc)]
    waiting.sort(key=lambda a: (str(a.get("next_step_due") or a.get("interview_at") or "9999"),
                                a.get("id") or 0))
    return {"now": moment, "loc": tuple(loc or ()), "columns": columns,
            "totals": totals, "reasons": reasons, "waiting": waiting,
            "open": sum(totals[s] for s in ap.OPEN_STAGES),
            "ready": loaded is not None}


def open_count(data: dict) -> int:
    """The Jobs header's Applied count: Preparing to Offer, not Closed."""
    return board_model(data)["open"]


# ------------------------------------------------------------------ the board

def _role_href(a: dict) -> str:
    oid = a.get("opportunity_id")
    if oid is None:
        return "/jobs?show=applied&layout=list"
    view = "open" if a["stage"] == "preparing" else "applied"
    return jp.role_url(oid, view) + ("&layout=list" if view == "applied" else "")


def _cv_line(a: dict) -> str:
    if a["stage"] == "preparing":
        name = a.get("saved_name")
        return f"Saved {esc(name)}.docx" if name else "A tailored CV is saved."
    name = a.get("applied_cv")
    if name:
        return f"Sent {esc(name)}.docx"
    return "No tailored CV was saved for this application."


def _meta_line(a: dict) -> str:
    company = esc(a.get("company") or "Unknown company")
    if a["stage"] == "preparing":
        when = jp._day(a.get("saved_at") or a.get("stage_at"))
        return f"CV saved {esc(when)} · {company}"
    return f"Applied {esc(jp._day(a.get('applied_on') or a.get('stage_at')))} · {company}"


def item_html(a: dict, model: dict) -> str:
    now = model["now"]
    stage = a["stage"]
    app_id = a.get("id")
    title = a.get("title") or "Untitled role"
    ident = f"app-{app_id}" if app_id is not None else f"app-r{a.get('opportunity_id')}"
    parts = []
    if app_id is not None:
        move = (f'<button type="button" class="btn btn-secondary item-move" '
                f'aria-haspopup="menu" aria-expanded="false" '
                f'aria-label="Move to, for {esc(title)}" data-move="{app_id}">'
                f'Move to{jp._use("ic-down", 16)}</button>')
    else:
        move = ""
    parts.append(f'<div class="item-top"><h3 class="item-h"><a class="item-title link" '
                 f'id="{ident}-t" href="{esc(_role_href(a))}">{esc(title)}</a></h3>{move}</div>')
    parts.append(f'<p class="item-meta">{_meta_line(a)}</p>')
    parts.append(f'<p class="item-cv">{_cv_line(a)}</p>')
    if stage == "closed":
        reason = a.get("closed_reason")
        word = ap.REASON_WORDS.get(reason, "Closed")
        shape = REASON_SHAPES.get(reason, "st-neu")
        parts.append(f'<p class="item-reason"><span class="tag tag-neutral">'
                     f'{jp._use(shape)}{esc(word)}</span>')
        when = jp._day(a.get("stage_at"))
        if a.get("stage_by") == ap.RADAR and reason == "no-reply":
            parts.append(f' <span class="item-by">Moved here by Radar on {esc(when)} after '
                         f'{ap.CLOSE_DAYS} days with no movement.</span></p>')
            if app_id is not None:
                parts.append(f'<p class="item-acts"><button type="button" class="btn btn-secondary" '
                             f'data-move-to="applied" data-app="{app_id}" '
                             f'aria-label="Move {esc(title)} back to Applied">'
                             'Move back to Applied</button></p>')
        else:
            who = "you" if a.get("stage_by") == ap.OWNER else "Radar"
            parts.append(f' <span class="item-by">Closed by {who} on {esc(when)}.</span></p>')
    else:
        days = ap.days_between(a.get("stage_at"), now)
        moved_today = days == 0 and a.get("stage_by") == ap.OWNER
        moment = ap.parse(a.get("stage_at"))
        line = (f"Moved here today, {uk_time(moment)}" if moved_today and moment
                else f"{_days(days)} in {ap.STAGE_IN[stage]}")
        bits = [esc(line)]
        if a.get("interview_at"):
            bits.append(esc(interview_words(a["interview_at"])))
        if a.get("next_step"):
            bits.append(f'<span class="item-next">{esc(a["next_step"])}, '
                        f'{esc(due_words(a.get("next_step_due"), now))}</span>')
        parts.append('<p class="item-days">' + DOT.join(bits) + '</p>')
        if ap.warned(a, now):
            on = ap.close_on(a)
            when = ("today" if on and on.date() <= now.date() else
                    f"on {on.day} {on:%b}" if on else "soon")
            parts.append(f'<p class="item-warn">{jp._use("ic-warn", 16)}<span>No movement for '
                         f'{_days(ap.still_days(a, now))}. Radar closes it as no reply '
                         f'{esc(when)} unless it moves.</span></p>')
        if app_id is not None:
            parts.append(next_step_form(a))
    data = (f'data-app="{app_id}" data-stage="{stage}" '
            f'data-reason="{esc(a.get("closed_reason") or "")}" '
            f'data-title="{esc(title)}"' if app_id is not None else 'data-virtual="1"')
    return f'<li class="item" id="{ident}" {data}>{"".join(parts)}</li>'


def next_step_form(a: dict) -> str:
    app_id = a["id"]
    title = a.get("title") or "Untitled role"
    has = bool(a.get("next_step") or a.get("interview_at"))
    interview = str(a.get("interview_at") or "")[:16]
    return (f'<details class="item-step"><summary class="link">'
            f'{"Change the next step" if has else "Add a next step"}'
            f'<span class="visually-hidden"> for {esc(title)}</span></summary>'
            f'<form class="step-form" data-step-form="{app_id}">'
            f'<label>Next step<input type="text" name="text" maxlength="{ap.NEXT_STEP_MAX}" '
            f'value="{esc(a.get("next_step") or "")}" autocomplete="off"></label>'
            f'<label>Due<input type="date" name="due" value="{esc(a.get("next_step_due") or "")}"></label>'
            f'<label>Interview, if there is one<input type="datetime-local" name="interview" '
            f'value="{esc(interview)}"></label>'
            f'<p class="step-error" id="step-err-{app_id}" hidden></p>'
            '<p class="step-acts"><button type="submit" class="btn btn-primary">Save</button>'
            '<button type="button" class="btn btn-secondary" data-step-clear>Clear the step '
            'and the interview</button></p>'
            '</form></details>')


def column_html(stage: str, model: dict) -> str:
    items = model["columns"][stage]
    total = model["totals"][stage]
    shown = len(items)
    count = f"{shown} of {total}" if model["loc"] and shown != total else str(total)
    head = [f'<h2 class="col-title" id="colh-{stage}">{ap.STAGE_WORDS[stage]} '
            f'<span class="col-count">{count}</span></h2>']
    if stage in ("preparing", "applied"):
        head.append('<p class="col-by">Filled by Radar</p>')
    if stage == "closed" and total:
        roll = " · ".join(f"{n} {ap.REASON_WORDS[r].lower()}"
                               for r, n in model["reasons"].items() if n)
        head.append(f'<p class="col-roll">{esc(roll)}</p>')
    head.append(f'<p class="col-note">{esc(COLUMN_NOTES[stage])}</p>')
    body = ("".join(item_html(a, model) for a in items) if items else
            f'<li class="col-empty">{esc(EMPTY[stage] if not model["loc"] else "Nothing here matches the location chips.")}</li>')
    return (f'<section class="col" id="col-{stage}" data-stage="{stage}" '
            f'aria-labelledby="colh-{stage}"><div class="col-head">{"".join(head)}</div>'
            f'<ul class="col-items">{body}</ul></section>')


def waiting_html(model: dict) -> str:
    now = model["now"]
    items = model["waiting"]
    if not items:
        body = '<p class="wait-empty">Nothing is waiting on you. A next step you set, or Radar\'s Follow up, shows here.</p>'
    else:
        lis = []
        for a in items:
            company = a.get("company") or "Unknown company"
            ident = f"app-{a['id']}"
            if a.get("next_step"):
                text = f"{a['next_step']}, {company}, {due_words(a.get('next_step_due'), now)}"
                if a.get("interview_at"):
                    text += ", interview " + interview_words(a["interview_at"])[len("Interview "):]
            else:
                text = f"{interview_words(a['interview_at'])}, {company}"
            lis.append(f'<li><a class="link" href="#{ident}" data-goto="{ident}">{esc(text)}</a></li>')
        body = f'<ol class="wait-list">{"".join(lis)}</ol>'
    return (f'<section class="wait" aria-labelledby="wait-title"><h2 class="wait-title" id="wait-title">'
            f'Waiting on you <span class="col-count">{len(items)}</span></h2>{body}</section>')


def picker_html(model: dict) -> str:
    return ('<div class="picker" role="group" aria-label="Column shown">' + "".join(
        f'<button type="button" class="chip" data-pick="{s}" aria-pressed="false" '
        f'aria-controls="col-{s}">{ap.STAGE_WORDS[s]} '
        f'<span class="chip-count">{model["totals"][s]}</span></button>'
        for s in ap.STAGES) + '</div>')


def board_inner_html(model: dict) -> str:
    virtual = any(a.get("id") is None for items in model["columns"].values() for a in items)
    note = ("" if model["ready"] and not virtual else
            '<p class="board-note">Some applications are not on file yet. Radar files '
            'them the next time it writes to the database, within two hours; until then '
            'they are shown in Applied and cannot be moved.</p>')
    keys = ('<p class="board-keys">Keys: arrows move between items, Enter opens the role, '
            '<kbd class="kbd">M</kbd> opens Move to; in it <kbd class="kbd">2</kbd> to '
            '<kbd class="kbd">5</kbd> picks a column and <kbd class="kbd">N</kbd>, '
            '<kbd class="kbd">T</kbd> or <kbd class="kbd">W</kbd> closes as no reply, '
            'turned down or withdrawn; <kbd class="kbd">Z</kbd> undoes the last move.</p>')
    cols = "".join(column_html(s, model) for s in ap.STAGES)
    return (f'{note}{waiting_html(model)}{keys}{picker_html(model)}'
            f'<div class="cols">{cols}</div>')


def board_payload(data: dict, loc: tuple = ()) -> dict:
    model = board_model(data, loc=loc)
    return {"html": board_inner_html(model), "open": model["open"]}


def layout_switch_html(layout: str, loc: tuple) -> str:
    base = "/jobs?show=applied" + (("&loc=" + ",".join(loc)) if loc else "")
    links = (("list", "List", base + "&layout=list"), ("board", "Board", base))
    return ('<nav class="layout" aria-label="Layout">' + "".join(
        f'<a class="chip{" is-selected" if key == layout else ""}" href="{esc(href)}"'
        f'{" aria-current=" + chr(34) + "page" + chr(34) if key == layout else ""}>{word}</a>'
        for key, word, href in links) + '</nav>')


def keymap_html() -> str:
    return ('<dl class="keymap">' + "".join(
        f'<div><dt><kbd class="kbd">{esc(k)}</kbd></dt><dd>{esc(v)}</dd></div>'
        for k, v in BOARD_KEYMAP) + '</dl>')


# ------------------------------------------------------------------ styles

BOARD_CSS = """
.layout{display:inline-flex;gap:0}
.layout .chip{border-radius:0;text-decoration:none}
.layout .chip:first-child{border-radius:var(--radius-2) 0 0 var(--radius-2)}
.layout .chip:last-child{border-radius:0 var(--radius-2) var(--radius-2) 0;margin-left:-1px}
.content-board{display:block;overflow:auto;overscroll-behavior:contain;padding:0 0 64px}
.board-note{margin:12px 16px 0;padding:8px 12px;border:1px solid var(--caution);border-radius:var(--radius-2);color:var(--text-1);max-width:80ch}
.wait{padding:12px 16px;border-bottom:1px solid var(--border-subtle)}
.wait-title{display:flex;align-items:baseline;gap:8px;font-size:16px;line-height:24px;font-weight:600}
.wait-list{display:flex;flex-wrap:wrap;gap:4px 24px;margin-top:4px;padding-left:0;list-style:none}
.wait-list li{min-height:24px;display:flex;align-items:center}
.wait-empty{margin-top:4px;color:var(--text-2)}
.board-keys{padding:8px 16px;font-size:12px;line-height:20px;color:var(--text-2);border-bottom:1px solid var(--border-subtle)}
.board-keys .kbd{margin:0 2px}
.picker{display:none}
.cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));align-items:start}
.col{min-width:0;border-right:1px solid var(--border-subtle);border-bottom:1px solid var(--border-subtle)}
.col-head{padding:12px 16px 8px;border-bottom:1px solid var(--border-subtle)}
.col-title{display:flex;align-items:baseline;gap:8px;font-size:16px;line-height:24px;font-weight:600}
.col-count{font-size:12px;line-height:16px;font-weight:400;color:var(--text-3);font-variant-numeric:tabular-nums}
.col-by{margin-top:2px;font-size:12px;line-height:16px;font-weight:600;color:var(--accent)}
.col-roll{margin-top:2px;font-size:12px;line-height:16px;color:var(--text-2)}
.col-note{margin-top:4px;font-size:12px;line-height:16px;color:var(--text-2)}
.col-items{padding:0;list-style:none}
.col-empty{padding:12px 16px;color:var(--text-2)}
.item{display:flex;flex-direction:column;gap:4px;padding:12px 16px;border-bottom:1px solid var(--border-subtle);scroll-margin-block:56px}
.item:last-child{border-bottom:0}
.item:target,.item.is-flash{box-shadow:inset 3px 0 0 var(--accent)}
.item-top{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.item-h{min-width:0;font-size:14px;line-height:20px;font-weight:600}
.item-title{overflow-wrap:anywhere}
.item-move{flex:none;padding-right:8px}
.item-meta,.item-cv,.item-days,.item-by{font-size:12px;line-height:16px;color:var(--text-2)}
.item-cv{overflow-wrap:anywhere}
.item-next{color:var(--text-1);font-weight:600}
.item-warn{display:flex;gap:8px;align-items:flex-start;padding:6px 8px;border:1px solid var(--caution);border-radius:var(--radius-2);background:var(--caution-tint);color:var(--text-1);font-size:12px;line-height:16px}
.item-warn svg{flex:none;color:var(--caution)}
.item-reason{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.item-acts{margin-top:4px}
.item-step summary{display:inline-block;font-size:12px;line-height:24px;cursor:pointer}
.step-form{display:grid;gap:8px;margin-top:8px;padding:8px;border:1px solid var(--border-subtle);border-radius:var(--radius-2);background:var(--surface-sunken)}
.step-form label{display:grid;gap:4px;font-size:12px;line-height:16px;color:var(--text-2)}
.step-form input{height:32px;padding:0 8px;border:1px solid var(--border-strong);border-radius:var(--radius-2);background:var(--surface);color:var(--text-1);font-size:14px;min-width:0;width:100%;color-scheme:dark}
.step-acts{display:flex;flex-wrap:wrap;gap:8px}
.step-error{color:var(--text-1);font-size:12px;line-height:16px;padding-left:8px;border-left:3px solid var(--negative)}
.step-error::before{content:"Not saved: ";font-weight:600}
.step-form input[aria-invalid="true"]{border-color:var(--negative)}
.move-menu{position:fixed;z-index:20;min-width:240px;padding:4px 0;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay)}
.move-menu[hidden]{display:none}
.move-menu button{display:flex;align-items:center;justify-content:space-between;gap:16px;width:100%;min-height:32px;padding:6px 12px;border:0;background:transparent;color:var(--text-1);text-align:left;cursor:pointer}
.move-menu button:hover,.move-menu button:focus-visible{background:var(--row-hover)}
.move-menu button:focus-visible{outline-offset:-2px}
.move-menu .menu-sep{height:1px;margin:4px 0;background:var(--border-subtle)}
.move-menu .menu-label{display:inline-flex;align-items:center;gap:8px}
@media (min-width:1800px){.cols{grid-template-columns:repeat(6,minmax(0,1fr))}}
@media (max-width:1023.98px){
  .board-keys{display:none}
  .cols{grid-template-columns:minmax(0,1fr)}
  .board.is-picking .picker{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;padding:12px 16px;border-bottom:1px solid var(--border-subtle)}
  .board.is-picking .picker .chip{justify-content:center;height:44px;white-space:normal;text-align:center;line-height:16px}
  .board.is-picking .col{display:none;border-right:0}
  .board.is-picking .col.is-shown{display:block}
  .item-move,.item-acts .btn,.step-acts .btn{height:44px}
  .step-form input{height:44px}
  .move-menu button{min-height:44px}
}
@media (forced-colors:active){.item-warn,.board-note{border:1px solid CanvasText}.item:target,.item.is-flash{outline:2px solid Highlight}}
"""


# ------------------------------------------------------------------ the script

BOARD_SCRIPT = """
(function () {
  'use strict';
  var app = document.querySelector('.app[data-page="jobs"]');
  var board = document.getElementById('board');
  if (!app || !board) return;
  var state = JSON.parse(app.dataset.state);
  var loc = state.loc || [];
  var LOCS = ['local', 'uk', 'europe', 'elsewhere', 'remote'];
  var toast = document.getElementById('toast');
  var undoBtn = document.getElementById('undo-last');
  var keys = document.getElementById('keys');
  var menu = document.getElementById('move-menu');
  var WORDS = { preparing: 'Preparing', applied: 'Applied', conversation: 'In conversation',
                interviewing: 'Interviewing', offer: 'Offer', closed: 'Closed' };
  var REASONS = { 'no-reply': 'no reply', 'turned-down': 'turned down', withdrawn: 'withdrawn' };
  var COLUMN_KEYS = { '2': 'applied', '3': 'conversation', '4': 'interviewing', '5': 'offer' };
  var CLOSE_KEYS = { n: 'no-reply', t: 'turned-down', w: 'withdrawn' };
  var INVERSE = { ack: 'unack', dismiss: 'undismiss', close: 'unclose', apply: 'unapply', unack: 'ack' };
  var DONE = { ack: 'Acknowledged', dismiss: 'Dismissed', close: 'Closed', apply: 'Marked applied',
               unack: 'Back in the open roles', move: 'Moved' };
  var narrow = window.matchMedia('(max-width: 1023.98px)');
  var locQs = loc.length ? '&loc=' + loc.join(',') : '';

  function postJson(path, body) {
    return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () {
        return { ok: false, note: 'No usable answer came back, so that may not have saved. Reload to check.' };
      }); });
  }
  function say(text, bad, undo) {
    toast.textContent = '';
    toast.classList.toggle('is-bad', !!bad);
    var span = document.createElement('span');
    span.textContent = text;
    toast.appendChild(span);
    if (undo) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-secondary';
      b.textContent = 'Undo';
      b.addEventListener('click', undoLast);
      toast.appendChild(b);
    }
  }
  function phrase(stage, reason) {
    return stage === 'closed' ? 'Closed, ' + (REASONS[reason] || 'closed') : WORDS[stage];
  }

  // ---- the undo stack, shared with the List so Undo last reaches every action
  function stack() {
    try { return JSON.parse(sessionStorage.getItem('radar-jobs-undo') || '[]'); } catch (e) { return []; }
  }
  function setStack(s) {
    try { sessionStorage.setItem('radar-jobs-undo', JSON.stringify(s.slice(-20))); } catch (e) {}
    undoBtn.disabled = !s.length;
    var last = s[s.length - 1];
    undoBtn.setAttribute('aria-label', last ? 'Undo last: ' + (DONE[last.kind] || 'Done') + ', '
                         + last.title : 'Undo last, nothing to undo');
  }
  setStack(stack());

  // ---- redrawing
  function itemEl(id) { return document.getElementById('app-' + id); }
  function refresh(focusId) {
    return fetch('/jobs?show=applied&part=board' + locQs, { headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) {
        board.innerHTML = j.html;
        document.querySelectorAll('[data-applied-count]').forEach(function (c) { c.textContent = j.open; });
        setupPicker();
        if (focusId) {
          var el = itemEl(focusId);
          if (el && el.offsetParent !== null) {
            var link = el.querySelector('.item-title');
            if (link) link.focus();
            el.classList.add('is-flash');
            setTimeout(function () { el.classList.remove('is-flash'); }, 1600);
          } else {
            var shown = board.querySelector('.col.is-shown .col-title') || board.querySelector('.col-title');
            if (shown) { shown.tabIndex = -1; shown.focus(); }
          }
        } else if (document.activeElement === document.body || document.activeElement === undoBtn) {
          var first = board.querySelector('.col.is-shown .item-title') || board.querySelector('.item-title');
          if (first) first.focus();
        }
      }).catch(function () { location.reload(); });
  }

  // ---- moving
  var busy = false;
  function move(id, from, fromReason, to, reason, title, undoing) {
    if (busy) { say('Still saving the last move.'); return Promise.resolve('busy'); }
    busy = true;
    return postJson('/application/move', { id: id, to: to, reason: reason || null, from: from,
                                           undo: !!undoing })
      .then(function (j) {
        busy = false;
        if (!j || j.ok === false) {
          say((j && j.note) || 'That did not stick. Try again.', true);
          return 'refused';
        }
        if (!undoing) {
          var s = stack();
          s.push({ kind: 'move', id: id, title: title, from: from, fromReason: fromReason || null,
                   to: to, reason: reason || null });
          setStack(s);
        }
        say((undoing ? 'Moved back' : 'Moved') + ' \\u201c' + title + '\\u201d to ' + phrase(to, reason) + '.',
            false, !undoing);
        return refresh(id).then(function () { return 'ok'; });
      }, function () {
        busy = false;
        say('No usable answer came back, so that may not have saved. Reload to check.', true);
        return 'error';
      });
  }
  var undoing = false;
  function undoLast() {
    var s = stack();
    if (!s.length || undoing) return;
    var last = s.pop();
    var go;
    if (last.kind === 'move') {
      go = function () { return move(last.id, last.to, last.reason, last.from, last.fromReason, last.title, true); };
    } else {
      if (last.kind === 'apply' && !window.confirm('Undo this application? The day you applied is '
          + 'forgotten, and applying again records a new day.')) return;
      go = function () {
        return postJson('/' + INVERSE[last.kind], { id: last.id }).then(function (j) {
          if (!j || j.ok === false) { say((j && j.note) || 'That did not stick.', true); return 'refused'; }
          say('Undone: ' + last.title + '.');
          return refresh(null).then(function () { return 'ok'; });
        }, function () { say('No usable answer came back. Reload to check.', true); return 'error'; });
      };
    }
    undoing = true;
    undoBtn.disabled = true;
    go().then(function (outcome) {
      undoing = false;
      if (outcome === 'ok' || outcome === 'refused') setStack(s); else setStack(stack());
    });
  }
  undoBtn.addEventListener('click', undoLast);

  // ---- the Move to menu
  var menuFor = null;
  function closeMenu(focusBack) {
    if (menu.hidden) return;
    menu.hidden = true;
    if (menuFor) {
      menuFor.button.setAttribute('aria-expanded', 'false');
      if (focusBack) menuFor.button.focus();
    }
    menuFor = null;
  }
  function entry(label, key, to, reason) {
    var b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.tabIndex = -1;
    b.dataset.to = to;
    if (reason) b.dataset.reason = reason;
    var l = document.createElement('span');
    l.className = 'menu-label';
    l.textContent = label;
    var k = document.createElement('kbd');
    k.className = 'kbd';
    k.textContent = key.toUpperCase();
    b.appendChild(l);
    b.appendChild(k);
    b.setAttribute('aria-keyshortcuts', key.toUpperCase());
    return b;
  }
  function openMenu(button) {
    var li = button.closest('.item');
    if (!li) return;
    closeMenu(false);
    var stage = li.dataset.stage, reason = li.dataset.reason || null;
    menu.textContent = '';
    menu.setAttribute('aria-label', 'Move \\u201c' + li.dataset.title + '\\u201d to');
    Object.keys(COLUMN_KEYS).forEach(function (k) {
      var to = COLUMN_KEYS[k];
      if (to !== stage) menu.appendChild(entry(WORDS[to], k, to));
    });
    var sep = document.createElement('div');
    sep.className = 'menu-sep';
    sep.setAttribute('role', 'separator');
    menu.appendChild(sep);
    Object.keys(CLOSE_KEYS).forEach(function (k) {
      var r = CLOSE_KEYS[k];
      if (!(stage === 'closed' && reason === r)) menu.appendChild(entry('Closed, ' + REASONS[r], k, 'closed', r));
    });
    menuFor = { button: button, li: li };
    button.setAttribute('aria-expanded', 'true');
    menu.hidden = false;
    var box = button.getBoundingClientRect();
    var w = menu.offsetWidth, h = menu.offsetHeight;
    var left = Math.min(Math.max(8, box.right - w), window.innerWidth - w - 8);
    var top = box.bottom + 4 + h > window.innerHeight ? Math.max(8, box.top - h - 4) : box.bottom + 4;
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    var first = menu.querySelector('[role="menuitem"]');
    if (first) first.focus();
  }
  function pick(b) {
    var li = menuFor && menuFor.li;
    closeMenu(true);
    if (!li) return;
    move(Number(li.dataset.app), li.dataset.stage, li.dataset.reason || null, b.dataset.to,
         b.dataset.reason || null, li.dataset.title, false);
  }
  menu.addEventListener('click', function (e) {
    var b = e.target.closest('[role="menuitem"]');
    if (b) pick(b);
  });
  menu.addEventListener('keydown', function (e) {
    var items = Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]'));
    var i = items.indexOf(document.activeElement);
    var k = (e.key || '').toLowerCase();
    if (e.key === 'Escape') { e.preventDefault(); closeMenu(true); return; }
    if (e.key === 'Tab') { e.preventDefault(); closeMenu(true); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      var n = (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[n].focus();
      return;
    }
    if (e.key === 'Home' || e.key === 'End') { e.preventDefault(); items[e.key === 'Home' ? 0 : items.length - 1].focus(); return; }
    // The menu's own keys, as it shows them, work while it is open.
    var hit = items.filter(function (b) { return b.getAttribute('aria-keyshortcuts').toLowerCase() === k; })[0];
    if (hit && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); e.stopPropagation(); pick(hit); }
  });
  document.addEventListener('click', function (e) {
    if (!menu.hidden && !e.target.closest('#move-menu') && !e.target.closest('.item-move')) closeMenu(false);
  });
  window.addEventListener('resize', function () { closeMenu(false); });
  board.addEventListener('scroll', function () { closeMenu(false); }, true);
  document.getElementById('main').addEventListener('scroll', function () { closeMenu(false); });

  // ---- clicks on the board
  board.addEventListener('click', function (e) {
    var mv = e.target.closest('.item-move');
    if (mv) {
      e.preventDefault();
      if (menuFor && menuFor.button === mv) closeMenu(true); else openMenu(mv);
      return;
    }
    var back = e.target.closest('[data-move-to]');
    if (back) {
      var li = back.closest('.item');
      move(Number(li.dataset.app), li.dataset.stage, li.dataset.reason || null, back.dataset.moveTo,
           null, li.dataset.title, false);
      return;
    }
    var go = e.target.closest('[data-goto]');
    if (go) {
      var el = document.getElementById(go.dataset.goto);
      if (el) {
        e.preventDefault();
        showColumnOf(el);
        el.scrollIntoView({ block: 'center' });
        var link = el.querySelector('.item-title');
        if (link) link.focus({ preventScroll: true });
        history.replaceState(null, '', '#' + go.dataset.goto);
      }
      return;
    }
    var clear = e.target.closest('[data-step-clear]');
    if (clear) {
      var f = clear.closest('form');
      f.elements.text.value = '';
      f.elements.due.value = '';
      f.elements.interview.value = '';
      saveStep(f);
    }
  });
  function fieldAt(note) {
    if (/interview/i.test(note)) return 'interview';
    if (/due date/i.test(note) && !/^Say what/.test(note)) return 'due';
    return 'text';
  }
  function saveStep(form) {
    if (form.dataset.busy === '1') return;
    var id = Number(form.dataset.stepForm);
    var body = { id: id, text: form.elements.text.value, due: form.elements.due.value,
                 interview: form.elements.interview.value };
    var err = form.querySelector('.step-error');
    ['text', 'due', 'interview'].forEach(function (n) {
      form.elements[n].removeAttribute('aria-invalid');
      form.elements[n].removeAttribute('aria-describedby');
    });
    err.hidden = true;
    form.dataset.busy = '1';
    form.setAttribute('aria-busy', 'true');
    postJson('/application/next', body).then(function (j) {
      form.dataset.busy = '';
      form.removeAttribute('aria-busy');
      if (!j || j.ok === false) {
        var note = (j && j.note) || 'That did not save.';
        if (j && j.invalid) {
          var field = form.elements[fieldAt(note)];
          err.textContent = note;
          err.hidden = false;
          field.setAttribute('aria-invalid', 'true');
          field.setAttribute('aria-describedby', err.id);
          field.focus();
        }
        say(note, true);
        return;
      }
      say(j.unchanged ? 'Nothing changed.' : (j.next_step ? 'Next step saved.'
          : (j.interview ? 'Interview saved.' : 'Next step cleared.')));
      refresh(id);
    }, function () {
      form.dataset.busy = '';
      form.removeAttribute('aria-busy');
      say('No usable answer came back, so that may not have saved. Reload to check.', true);
    });
  }
  board.addEventListener('submit', function (e) {
    var f = e.target.closest('[data-step-form]');
    if (!f) return;
    e.preventDefault();
    saveStep(f);
  });

  // ---- phone: one column at a time
  function pickColumn(stage, focus) {
    board.querySelectorAll('.col').forEach(function (c) { c.classList.toggle('is-shown', c.dataset.stage === stage); });
    board.querySelectorAll('[data-pick]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.pick === stage));
      b.classList.toggle('is-selected', b.dataset.pick === stage);
    });
    try { sessionStorage.setItem('radar-board-column', stage); } catch (e) {}
    if (focus) { var h = document.getElementById('colh-' + stage); if (h) { h.tabIndex = -1; h.focus(); } }
  }
  function showColumnOf(el) {
    var col = el.closest('.col');
    if (col && narrow.matches) pickColumn(col.dataset.stage, false);
  }
  var columnFromAddress = false;
  function setupPicker() {
    board.classList.add('is-picking');
    var stage = null;
    try { stage = sessionStorage.getItem('radar-board-column'); } catch (e) {}
    if (!stage || !document.getElementById('col-' + stage)) stage = 'applied';
    // Home's column links name the column in the address, #col-<stage>, and
    // the board opens on it; after that the column picked stays picked.
    var named = !columnFromAddress && /^#col-([a-z]+)$/.exec(location.hash || '');
    columnFromAddress = true;
    if (named && document.getElementById('col-' + named[1])) stage = named[1];
    pickColumn(stage, false);
    if (location.hash) {
      var el = document.querySelector(location.hash.replace(/[^#a-z0-9-]/gi, ''));
      if (el && el.classList.contains('item')) showColumnOf(el);
    }
  }
  board.addEventListener('click', function (e) {
    var p = e.target.closest('[data-pick]');
    if (p) pickColumn(p.dataset.pick, true);
  });
  setupPicker();

  // ---- the Show menu
  var showBtn = document.getElementById('show-btn');
  var showList = document.getElementById('show-list');
  function setShow(open) { showList.hidden = !open; showBtn.setAttribute('aria-expanded', String(open)); }
  showBtn.addEventListener('click', function () { setShow(showList.hidden); });
  document.addEventListener('click', function (e) {
    if (!showList.hidden && !e.target.closest('.show-menu')) setShow(false);
  });
  // ---- the location chips: the board is drawn again for the chips chosen
  document.querySelectorAll('[data-loc]').forEach(function (b) {
    b.addEventListener('click', function () {
      var k = b.dataset.loc;
      var next = loc.indexOf(k) >= 0 ? loc.filter(function (x) { return x !== k; })
                                      : LOCS.filter(function (x) { return x === k || loc.indexOf(x) >= 0; });
      location.href = '/jobs?show=applied' + (next.length ? '&loc=' + next.join(',') : '');
    });
  });

  // ---- keys
  function typing(t) {
    return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  }
  function keysOn() {
    try { return localStorage.getItem('radar-jobs-keys') !== 'off'; } catch (e) { return true; }
  }
  var keysToggle = document.getElementById('keys-on');
  if (keysToggle) {
    keysToggle.checked = keysOn();
    keysToggle.addEventListener('change', function () {
      try { localStorage.setItem('radar-jobs-keys', keysToggle.checked ? 'on' : 'off'); } catch (e) {}
    });
  }
  function openKeys() {
    if (keys.open) return;
    if (keys.showModal) keys.showModal(); else keys.setAttribute('open', '');
  }
  var keysBtn = document.getElementById('keys-open');
  if (keysBtn) keysBtn.addEventListener('click', openKeys);
  function visibleItems(col) {
    return Array.prototype.slice.call(col.querySelectorAll('.item')).filter(function (li) {
      return li.offsetParent !== null;
    });
  }
  function arrow(key, from) {
    var li = from.closest('.item');
    var cols = Array.prototype.slice.call(board.querySelectorAll('.col')).filter(function (c) {
      return c.offsetParent !== null;
    });
    if (!li) {
      var first = board.querySelector('.col .item .item-title');
      if (first) first.focus();
      return;
    }
    var col = li.closest('.col');
    var items = visibleItems(col);
    var i = items.indexOf(li);
    var target = null;
    if (key === 'ArrowDown') target = items[i + 1];
    else if (key === 'ArrowUp') target = items[i - 1];
    else {
      var c = cols.indexOf(col);
      var step = key === 'ArrowRight' ? 1 : -1;
      for (var n = c + step; n >= 0 && n < cols.length && !target; n += step) {
        var other = visibleItems(cols[n]);
        if (other.length) target = other[Math.min(i, other.length - 1)];
      }
    }
    if (target) {
      var link = target.querySelector('.item-title');
      if (link) { link.focus({ preventScroll: true }); target.scrollIntoView({ block: 'nearest' }); }
    }
  }
  document.addEventListener('keydown', function (e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
    if (!menu.hidden) return;
    if (typing(e.target) || keys.open) return;
    if (e.key === 'Escape' && !showList.hidden) { setShow(false); showBtn.focus(); return; }
    var inItem = e.target.closest && e.target.closest('.item');
    var onItemTitle = inItem && e.target.classList.contains('item-title');
    if (/^Arrow/.test(e.key) && (onItemTitle || e.target === document.body || e.target === board)) {
      e.preventDefault();
      arrow(e.key, e.target);
      return;
    }
    if (!keysOn()) return;
    if (e.key === '?') { e.preventDefault(); openKeys(); return; }
    if (e.shiftKey) return;
    var k = (e.key || '').toLowerCase();
    if (k === 'z') { e.preventDefault(); undoLast(); return; }
    if (k === 'm' && inItem) {
      var btn = inItem.querySelector('.item-move');
      if (btn) { e.preventDefault(); openMenu(btn); }
    }
  });
  if (location.hash) {
    var target = document.querySelector(location.hash.replace(/[^#a-z0-9-]/gi, ''));
    if (target && target.classList.contains('item')) {
      var link = target.querySelector('.item-title');
      if (link) link.focus({ preventScroll: true });
    }
  }
})();
"""
