"""The Watchlist page: the companies the owner watches in a table, the
selected company's record beside it, Radar's suggestions kept apart, and the
record as its own view on a phone.

Drawn from the Watchlist final, variation A, Table and record (design/round-1
branch, design/watchlist/final, 15 Nov 2026), with the decisions of that day:
Read again on the facts (its cost said first, a press only), people added by
hand beside the published chief executive and chief technology officer,
logging a contact adds the company, a next step needs a due date, and only
what came back can be changed on a past contact (no edit or delete).

The data is watchlist.build_model; this module only draws it. Every write is
a fetch POST with a JSON body (build_dashboard.SCRIPT adds the write header),
and a write that changes what the page shows reloads it with a one-line note
kept in sessionStorage, said in the polite live region after the reload, with
Undo where the design draws one. Dark only, FRAME_CSS tokens only, nothing
fetched from elsewhere.
"""

from __future__ import annotations

import json

import build_dashboard as bd
import page_frame
import tiers
import radar_common
import watchlist as wl

esc = page_frame.esc

# ================================================================ icons
# Shapes as well as words, so no tag rests on colour alone.

_SVG = 'fill="none" aria-hidden="true" focusable="false"'
ICON_OVERDUE = (f'<svg viewBox="0 0 16 16" width="14" height="14" {_SVG}>'
                '<path d="M8 1.8 15 14H1z" fill="currentColor"/>'
                '<path d="M8 6v3.6" stroke="var(--canvas)" stroke-width="1.7" stroke-linecap="round"/>'
                '<circle cx="8" cy="11.8" r="1" fill="var(--canvas)"/></svg>')
ICON_TODAY = (f'<svg viewBox="0 0 16 16" width="14" height="14" {_SVG}>'
              '<circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.6"/>'
              '<path d="M8 4.6V8l2.4 1.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>')
ICON_LATER = (f'<svg viewBox="0 0 16 16" width="12" height="12" {_SVG}>'
              '<circle cx="8" cy="8" r="4.6" stroke="currentColor" stroke-width="1.6"/></svg>')
ICON_QUIET = (f'<svg viewBox="0 0 16 16" width="12" height="12" {_SVG}>'
              '<path d="M3.5 8h9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>')
ICON_STAR = (f'<svg viewBox="0 0 16 16" width="14" height="14" {_SVG}>'
             '<path d="M8 1.5l1.8 4.3 4.7.4-3.6 3 1.1 4.6L8 11.4l-4 2.4 1.1-4.6-3.6-3 4.7-.4z" '
             'fill="currentColor"/></svg>')
ICON_COPY = (f'<svg class="ic-copy" viewBox="0 0 16 16" width="14" height="14" {_SVG}>'
             '<rect x="5" y="5" width="8.5" height="9" rx="1" stroke="currentColor" stroke-width="1.4"/>'
             '<path d="M3 11V3a1 1 0 0 1 1-1h6.5" stroke="currentColor" stroke-width="1.4"/></svg>')
ICON_TICK = (f'<svg class="ic-tick" viewBox="0 0 16 16" width="14" height="14" {_SVG}>'
             '<path d="M3 8.5l3.2 3L13 4.5" stroke="currentColor" stroke-width="1.8" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')
ICON_BACK = (f'<svg viewBox="0 0 16 16" width="16" height="16" {_SVG}>'
             '<path d="M10 3 5 8l5 5" stroke="currentColor" stroke-width="1.8" '
             'stroke-linecap="round" stroke-linejoin="round"/></svg>')
ICON_ERR = (f'<svg viewBox="0 0 16 16" width="14" height="14" {_SVG}>'
            '<path d="M8 1.8 15 14H1z" fill="currentColor"/>'
            '<path d="M8 6v3.6" stroke="var(--negative-tint)" stroke-width="1.7" stroke-linecap="round"/>'
            '<circle cx="8" cy="11.8" r="1" fill="var(--negative-tint)"/></svg>')
ICON_CHECK = (f'<svg viewBox="0 0 16 16" width="14" height="14" {_SVG}>'
              '<path d="M3 8.5l3.2 3L13 4.5" stroke="currentColor" stroke-width="1.8" '
              'stroke-linecap="round" stroke-linejoin="round"/></svg>')

TAG_ICON = {"overdue": ICON_OVERDUE, "today": ICON_TODAY, "upcoming": ICON_LATER,
            "nodate": ICON_QUIET, "closed": ICON_QUIET, "none": ICON_QUIET}
TAG_CLASS = {"overdue": "is-overdue", "today": "is-today", "upcoming": "is-later",
             "nodate": "is-quiet", "closed": "is-quiet", "none": "is-quiet"}

# The contact a thread was set with, as a noun: "when you logged the comment".
CHANNEL_NOUN = {"comment": "the comment", "connection-note": "the connection note",
                "engagement": "the engagement", "artefact": "the article you sent",
                "other": "the contact"}
# How a contact was made, after "by": "You contacted them on 2 Nov, by an engagement."
CHANNEL_BY = {"comment": "by a comment", "connection-note": "by a connection note",
              "engagement": "by an engagement", "artefact": "by sending an article",
              "other": ""}


# ================================================================ small words

def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def channel_words(ch) -> str:
    ch = str(ch or "")
    return wl.CHANNEL_WORDS.get(ch) or (ch.replace("-", " ").capitalize() if ch else "Contact")


def sentence(text) -> str:
    return bd.sentence(str(text or "").strip())


def _today(model: dict):
    return wl.uk(wl.moment_of(model.get("now"))).date()


def _days(n: int) -> str:
    return plural(abs(n), "day")


def tag_html(th: dict, long: bool = False) -> str:
    """The thread's tag: a shape and words. long is the record's, which adds
    how far off the date is; the table's carries the overdue date beside it."""
    state = th.get("state") or "none"
    days = int(th.get("days") or 0)
    due = th.get("due_date")
    tail = ""
    if state == "overdue":
        words = f"Overdue by {_days(days)}"
        if long:
            words += f", due {wl.day_words(due)}"
        else:
            tail = f' <span class="meta">due {esc(wl.day_words(due))}</span>'
    elif state == "today":
        words = "Due today"
    elif state == "upcoming":
        words = f"Due {wl.day_words(due, weekday=days < 7)}"
        if long:
            words += ", tomorrow" if days == 1 else f", in {_days(days)}"
    elif state == "nodate":
        words = "No date set"
    elif state == "closed":
        words = "No next step"
    else:
        words = "No contact yet"
    return (f'<span class="ttag {TAG_CLASS.get(state, "is-quiet")}">{TAG_ICON.get(state, ICON_QUIET)}'
            f'<span>{esc(words)}</span></span>{tail}')


def run_words(model: dict) -> str:
    ran = model.get("watcher_ran")
    if not ran:
        return "The news watcher has not run yet."
    when = wl.hm(ran)
    day = wl.uk_day(ran)
    if day and day != _today(model):
        when += f", {wl.day_words(ran, weekday=True)}"
    return f"Signals as of the watcher's run at {when}."


# ================================================================ header

def title_html(model: dict) -> str:
    """The title row (owner, 17 Nov): the teal chip with the building, the
    title, two count pills, and the add box at the right."""
    c = model["counts"]
    return ('<header class="pagehead"><div class="ph-title">'
            f'{page_frame.chip("threads", "apps")}<h1>Watchlist</h1>'
            f'<span class="sec-count">{esc(plural(c["watched"], "company", "companies"))}</span>'
            f'<span class="sec-count">{esc(plural(c["due"], "follow-up"))} due</span></div>'
            f'{addname_html()}</header>')


def addname_html() -> str:
    # The hint below the box is empty until the box has something to say.
    return (
        '<form class="addname" id="add-form" role="search" aria-label="Add a company by name" novalidate>'
        '<label class="visually-hidden" for="add-name">Add a company by name</label>'
        '<div class="row"><div class="combo">'
        '<input class="field" id="add-name" type="text" role="combobox" aria-autocomplete="list" '
        'aria-expanded="false" aria-controls="add-list" aria-describedby="add-hint" autocomplete="off" '
        'maxlength="120" placeholder="Add a company by name" value="">'
        '<ul class="listbox" id="add-list" role="listbox" aria-label="Companies Radar has seen" hidden></ul>'
        '</div><button type="submit" class="btn btn-secondary">Add</button></div>'
        '<p class="hint" id="add-hint"></p>'
        '<div id="add-status"></div></form>')


def arrivals_html(model: dict) -> str:
    out = []
    for a in model.get("arrivals") or []:
        story = a.get("story")
        how = "when you pressed Add to Watchlist on Insights"
        if story:
            how = (f'when you pressed Add to Watchlist on its story, '
                   f'<a href="{esc(story["href"])}">{esc(story["headline"])}</a>')
        out.append(
            f'<div class="arrived"><span class="ntag">{ICON_STAR}<span>Just added from Insights</span></span>'
            f'<span class="what"><b>{esc(a["name"])}</b> arrived at {esc(wl.hm(a.get("at")))}, {how}. '
            'It is in your companies below.</span>'
            f'<a class="btn btn-ghost" data-focus-record href="/watchlist?company={int(a["id"])}">'
            f'Open its record<span class="visually-hidden">: {esc(a["name"])}</span></a></div>')
    return "".join(out)


# ================================================================ suggestions

def suggestion_card(s: dict) -> str:
    """One suggestion (owner, 17 Nov): a wide card glowing amber, the company,
    the story's headline and the way to it, Add to Watchlist and Dismiss."""
    sid = int(s["id"])
    name = esc(s["name"])
    data = (f'data-id="{sid}" data-name="{name}" data-kind="{esc(s.get("kind") or "")}" '
            f'data-type="{esc((s.get("type_words") or s.get("kind_words") or "").lower())}"')
    head = f'<p class="sg-headline">{esc(s["headline"])}</p>' if s.get("headline") else ""
    words = "Open the role on Jobs" if s.get("is_advert") else "Open the story"
    link = (f'<p class="sg-link"><a class="link" href="{esc(s["href"])}">{words}'
            f'<span class="visually-hidden">: {name}</span></a></p>' if s.get("href") else "")
    return (
        f'<li class="sg card g-story glow-short" aria-labelledby="sg-{sid}"><div class="sg-main">'
        f'<h3 id="sg-{sid}">{name}</h3>{head}{link}</div>'
        f'<div class="acts"><button type="button" class="btn btn-teal" data-sg="accept" {data}>'
        f'Add to Watchlist<span class="visually-hidden">: {name}</span></button>'
        f'<button type="button" class="btn btn-secondary" data-sg="dismiss" {data}>'
        f'Dismiss<span class="visually-hidden">: {name}</span></button></div></li>')


def dismissed_html(model: dict) -> str:
    items = model.get("dismissed") or []
    if not items:
        return ""
    rows = []
    for s in items:
        name = esc(s["name"])
        when = wl.day_words(s.get("dismissed_at"))
        how = (f"Radar folded it after {wl.FOLD_DAYS} days unanswered, {when}"
               if s.get("dismissed_by") == "radar" else f"Dismissed {when}")
        where = f' <span class="meta">{esc(s["place"])}</span>' if s.get("place") else ""
        rows.append(
            f'<li><span><b>{name}</b>{where}<span class="meta d-how">{esc(s.get("type_words") or "")}'
            f' · {esc(how)}</span></span>'
            f'<button type="button" class="btn btn-ghost" data-sg="again" data-id="{int(s["id"])}" '
            f'data-name="{name}">Suggest again<span class="visually-hidden">: {name}</span></button></li>')
    return (f'<p class="dis-head"><button type="button" class="btn btn-ghost" id="dis-btn" '
            f'aria-expanded="false" aria-controls="dis-list">Dismissed in the last {wl.DISMISSED_DAYS} days '
            f'({len(items)})</button></p><ul class="dis-list" id="dis-list" hidden>{"".join(rows)}</ul>')


def suggestions_section(model: dict) -> str:
    """Suggested by Radar: always shown while any wait, under its amber title
    with a count; not drawn at all when none wait."""
    waiting = model.get("suggestions") or []
    if not waiting:
        return ""
    return (f'<section class="suggest" id="sg-panel" aria-labelledby="sg-h">'
            f'<header class="sec-head">{page_frame.chip("story", "story")}'
            f'<h2 class="sec-title" id="sg-h" tabindex="-1">Suggested by Radar</h2>'
            f'<span class="sec-count">{len(waiting)}</span></header>'
            f'<ul class="sg-list">{"".join(suggestion_card(s) for s in waiting)}</ul>'
            f'{dismissed_html(model)}</section>')


# ================================================================ the table

def story_kick(sig: dict) -> str:
    """A story's type in amber, a drawn rule, then its date."""
    return (f'<span class="kick"><span class="r-bit sk-type">{esc(sig["type_words"])}</span>'
            '<span class="visually-hidden">, </span>'
            f'<span class="r-bit">{esc(wl.day_words(sig["found"]))}</span></span>')


def followup_html(th: dict) -> str:
    """The follow-up: the next step and when it is due, or No contact yet; the
    last contact on a second line; Due today or Overdue as a pill."""
    state = th.get("state") or "none"
    due = th.get("due_date")
    pill = ""
    if state == "today":
        pill = f'<span class="ttag is-today">{ICON_TODAY}<span>Due today</span></span>'
    elif state == "overdue":
        pill = f'<span class="ttag is-overdue">{ICON_OVERDUE}<span>Overdue</span></span>'
    if th.get("next_step"):
        when = (f", due {wl.day_words(due)}" if due and state in ("upcoming", "today", "overdue")
                else ", no date set" if state == "nodate" else "")
        line = f'{esc(th["next_step"])}<span class="fu-when">{esc(when)}</span>'
    elif state == "none":
        line = "No contact yet"
    else:
        line = "No next step"
    last = (f'<p class="fu-last">Last contact {esc(wl.day_words(th["last_at"]))}</p>'
            if th.get("last_at") else "")
    return f'<p class="fu-step">{line}</p>{pill}{last}'


def company_card(r: dict, selected_id) -> str:
    """One company (owner, 17 Nov): a wide card glowing teal, more strongly with
    a follow-up due, laid out in columns: the company, its follow-up, its latest
    story, its roles on Jobs, and Log a contact."""
    rid = int(r["id"])
    th = r["thread"]
    due = (th.get("state") or "") in ("today", "overdue")
    cur = ' aria-current="true"' if rid == selected_id else ""
    place = f'<p class="co-where">{esc(r["place"])}</p>' if r.get("place") else ""
    sig = r.get("latest")
    story = (f'{story_kick(sig)}<p class="co-headline">{esc(sig["headline"])}</p>' if sig
             else '<p class="muted">None yet</p>')
    n = int(r.get("roles") or 0)
    roles = (f'<span class="role-pill">{n} open</span>' if n
             else '<span class="role-pill is-none">None</span>')
    cls = "co-card card g-apps glow-row" + (" is-due" if due else "") + (" is-selected" if cur else "")
    return (
        f'<li class="{cls}" data-row="{rid}"><div class="co-grid">'
        f'<div class="co-col co-name">{page_frame.chip("threads", "apps")}<div class="co-id">'
        f'<a class="co" data-row-link="{rid}" href="/watchlist?company={rid}"{cur}>{esc(r["name"])}</a>'
        f'{place}<span class="stag">{esc(r["state_words"])}</span></div></div>'
        f'<div class="co-col co-fu"><p class="co-label fu-label">Follow-up</p>{followup_html(th)}</div>'
        f'<div class="co-col co-story"><p class="co-label story-label">Latest story</p>{story}</div>'
        f'<div class="co-col co-roles"><p class="co-label roles-label">Roles on Jobs</p>{roles}</div>'
        f'<div class="co-col co-act"><a class="btn btn-secondary" data-focus-log href="/watchlist?company={rid}#log">'
        f'Log a contact<span class="visually-hidden">: {esc(r["name"])}</span></a></div></div></li>')


def table_html(model: dict) -> str:
    """Your companies: the teal title with a count, then one card a company,
    in the order they are kept."""
    rows = model["rows"]
    selected_id = (model.get("selected") or {}).get("id")
    head = (f'<header class="sec-head">{page_frame.chip("threads", "apps")}'
            '<h2 class="sec-title" id="wt-h">Your companies</h2>'
            f'<span class="sec-count">{len(rows)}</span></header>')
    if not rows:
        return (f'<section class="companies" aria-labelledby="wt-h">{head}'
                '<div class="card empty"><p>No companies on your Watchlist yet.</p></div></section>')
    return (f'<section class="companies" aria-labelledby="wt-h">{head}'
            f'<ul class="co-list">{"".join(company_card(r, selected_id) for r in rows)}</ul></section>')


# ================================================================ the record

ICON_LEFT = (f'<svg viewBox="0 0 16 16" width="16" height="16" {_SVG}>'
             '<path d="M10 3.5L5.5 8l4.5 4.5" stroke="currentColor" stroke-width="1.8"/></svg>')
ICON_RIGHT = (f'<svg viewBox="0 0 16 16" width="16" height="16" {_SVG}>'
              '<path d="M6 3.5L10.5 8 6 12.5" stroke="currentColor" stroke-width="1.8"/></svg>')


def rsec(lv: int, ident: str, title: str, body: str, icon: str, group: str = "apps",
         count: str = "", cls: str = "", sec_id: str = "") -> str:
    """One section of the record (owner, 17 Nov): its title outside its card, in
    Home's pattern, the chip in the section's group colour (the colour rule),
    then the card with a faint glow of that colour."""
    pill = f'<span class="sec-count">{esc(count)}</span>' if count else ""
    sid = f' id="{sec_id}"' if sec_id else ""
    return (f'<section class="rsec{" " + cls if cls else ""}"{sid} aria-labelledby="{ident}">'
            f'<header class="sec-head">{page_frame.chip(icon, group)}'
            f'<h{lv} class="sec-title" id="{ident}">{esc(title)}</h{lv}>{pill}</header>'
            f'<div class="card rcard g-{group} glow-faint">{body}</div></section>')


def _rows(pairs: list) -> str:
    return '<dl class="rrows">' + "".join(f'<div class="rrow"><dt>{esc(k)}</dt><dd>{v}</dd></div>'
                                          for k, v in pairs) + '</dl>'


def log_form(rec: dict, model: dict, lv: int) -> str:
    """The contact form, inside Follow-up, closed until Log a contact is
    pressed. Every field, choice and check as before; no sentences on how to
    fill it in."""
    today = rec.get("today") or _today(model).isoformat()
    channels = "".join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in model.get("channels") or wl.CHANNELS)
    outcomes = model.get("outcomes") or wl.OUTCOME_WORDS
    radios = "".join(
        f'<label><input type="radio" name="outcome" id="log-out-{esc(k)}" value="{esc(k)}"'
        f'{" checked" if k == "none" else ""}>{esc(v)}</label>' for k, v in outcomes.items())
    return (
        f'<div class="log-wrap" id="log" hidden><h{lv + 1} class="log-h" id="log-h">Log a contact</h{lv + 1}>'
        '<form class="logform" id="log-form" aria-labelledby="log-h" novalidate>'
        + _fld("log-date", "Date",
               f'<input class="field" type="date" id="log-date" name="date" value="{esc(today)}" max="{esc(today)}">')
        + _fld("log-channel", "How",
               f'<select class="field" id="log-channel" name="channel"><option value="">Choose one</option>'
               f'{channels}</select>')
        + _fld("log-note", "Note, optional",
               f'<textarea class="field" id="log-note" name="note" maxlength="{wl.NOTE_MAX}"></textarea>',
               full=True)
        + '<fieldset class="full" id="log-outcome"><legend>What came back</legend>'
          f'<div class="radios">{radios}</div>'
          f'<p class="err" id="log-outcome-err" hidden>{ICON_ERR}<span></span></p></fieldset>'
        + _fld("log-next", "Next step",
               f'<input class="field" type="text" id="log-next" name="next_step" maxlength="{wl.STEP_MAX}" '
               'placeholder="Follow up in a week">')
        + _fld("log-due", "Due date", '<input class="field" type="date" id="log-due" name="due">')
        + '<div class="full submit"><button type="submit" class="btn btn-teal">Log the contact</button>'
          '<button type="button" class="btn btn-ghost" id="log-cancel">Cancel</button>'
          f'<p class="err" id="log-err" hidden>{ICON_ERR}<span></span></p></div>'
        '</form></div>')


def followup_block(rec: dict, model: dict, lv: int) -> str:
    """Follow-up: the last contact, the next step and its due date as rows, or
    No contact yet; Move the date and Clear the next step while one is open;
    then the contact form, closed."""
    th = rec["thread"]
    state = th["state"]
    open_ = state in ("overdue", "today", "upcoming", "nodate")
    if state == "none":
        body = '<p class="rnone">No contact yet.</p>'
    else:
        pairs = []
        if th.get("last_at"):
            pairs.append(("Last contact", esc(f"{wl.day_words(th['last_at'])}, "
                                              f"{channel_words(th.get('last_channel')).lower()}")))
        pairs.append(("Next step", esc(th["next_step"]) if th.get("next_step") else "None"))
        if th.get("due_date") and open_:
            pill = ""
            if state == "today":
                pill = f' <span class="ttag is-today">{ICON_TODAY}<span>Due today</span></span>'
            elif state == "overdue":
                pill = f' <span class="ttag is-overdue">{ICON_OVERDUE}<span>Overdue</span></span>'
            pairs.append(("Due", esc(wl.day_words(th["due_date"])) + pill))
        elif state == "nodate":
            pairs.append(("Due", "No date set"))
        body = _rows(pairs)
    acts, move = [], ""
    if open_:
        move_words = "Set a date" if state == "nodate" else "Move the date"
        acts.append(f'<button type="button" class="btn btn-secondary" id="move-btn" aria-expanded="false" '
                    f'aria-controls="move-form">{move_words}</button>')
        acts.append('<button type="button" class="btn btn-ghost" id="clear-btn">Clear the next step</button>')
        move = (
            '<form class="inline-form" id="move-form" hidden novalidate>'
            '<div class="fld"><label for="move-due">New due date</label>'
            f'<input class="field" type="date" id="move-due" name="due" value="{esc(th.get("due_date") or "")}">'
            f'<p class="err" id="move-err" hidden>{ICON_ERR}<span></span></p></div>'
            '<div class="acts"><button type="submit" class="btn btn-secondary">Save the date</button>'
            '<button type="button" class="btn btn-ghost" id="move-cancel">Cancel</button></div></form>')
    acts_html = f'<div class="acts">{"".join(acts)}</div>' if acts else ""
    return rsec(lv, "th-h", "Follow-up", body + acts_html + move + log_form(rec, model, lv), "apps",
                sec_id="followup")


def drafts_block(rec: dict, lv: int) -> str:
    d = rec.get("drafts")
    if not d:
        return ""
    parts = []
    written = wl.day_words(d.get("written"))
    story = (f'<a href="{esc(d["href"])}">{esc(d["headline"])}</a>' if d.get("headline")
             else f'<a href="{esc(d["href"])}">its story</a>')
    for key, label, ident in (("comment", "Comment", "comment"), ("note", "Connection note", "note")):
        text = d.get(key) or ""
        if not text:
            continue
        parts.append(
            f'<div class="draft"><h{lv + 1} class="dh" id="d-{ident}">{label}</h{lv + 1}>'
            f'<p class="rmeta">Written {esc(written)}, for the story {story}.</p>'
            f'<p class="text" id="t-{ident}">{esc(text)}</p>'
            f'<div class="draft-foot"><button type="button" class="btn btn-secondary" data-copy="t-{ident}">'
            f'{ICON_COPY}{ICON_TICK}<span class="lbl">Copy the {label.lower()}</span></button>'
            f'<span class="rmeta">{len(text)} characters</span>'
            f'<span class="err copy-err" hidden>{ICON_ERR}<span>The browser would not copy. Select the text '
            'and copy it by hand.</span></span></div></div>')
    return rsec(lv, "dr-h", "Drafts to copy", "".join(parts), "story", "story", cls="drafts",
                sec_id="drafts")


def _fld(ident: str, label: str, control: str, full: bool = False) -> str:
    return (f'<div class="fld{" full" if full else ""}"><label for="{ident}">{esc(label)}</label>{control}'
            f'<p class="err" id="{ident}-err" hidden>{ICON_ERR}<span></span></p></div>')


def contacts_block(rec: dict, lv: int) -> str:
    contacts = rec.get("contacts") or []
    if not contacts:
        return rsec(lv, "ct-h", "Contacts", '<p class="rnone">None logged yet.</p>', "applications")
    items = []
    outcomes = wl.OUTCOME_WORDS
    for i, t in enumerate(contacts):
        tid = int(t["id"])
        day = wl.day_words(t.get("touched_at"))
        how = channel_words(t.get("channel"))
        what = f"{day} {how.lower()}"
        note = f'<p class="note">{esc(t["note"])}</p>' if t.get("note") else ""
        step = (t.get("next_action") or "").strip()
        set_line = ""
        if step:
            due = (f", due {wl.day_words(t['next_action_date'])}" if t.get("next_action_date") else ", no date")
            after = ""
            if i > 0:
                newer = contacts[i - 1]
                after = (f" Followed by the {wl.day_words(newer.get('touched_at'))} "
                         f"{channel_words(newer.get('channel')).lower()}.")
            set_line = f'<p class="set">{esc(f"Next step set: {step.rstrip(chr(46))}{due}.{after}")}</p>'
        out = str(t.get("outcome") or "none")
        opts = "".join(f'<option value="{esc(k)}"{" selected" if k == out else ""}>{esc(v)}</option>'
                       for k, v in outcomes.items())
        items.append(
            f'<li><span class="when">{esc(day)}</span><div><span class="how">{esc(how)}</span>{note}{set_line}'
            f'<p class="out">Outcome: <b>{esc(outcomes.get(out, out.capitalize()))}</b>'
            f'<button type="button" class="btn btn-ghost" data-out-open="{tid}" '
            f'aria-expanded="false" aria-controls="out-{tid}">Change<span class="visually-hidden"> what came back '
            f'from the {esc(what)}</span></button></p>'
            f'<form class="inline-form outform" id="out-{tid}" data-touch="{tid}" data-what="{esc(what)}" hidden>'
            f'<div class="fld"><label for="out-sel-{tid}">What came back</label>'
            f'<select class="field" id="out-sel-{tid}" name="outcome">{opts}</select></div>'
            '<div class="acts"><button type="submit" class="btn btn-secondary">Save</button>'
            f'<button type="button" class="btn btn-ghost" data-out-cancel="{tid}">Cancel</button></div>'
            f'<p class="err" id="out-err-{tid}" hidden>{ICON_ERR}<span></span></p></form></div></li>')
    return rsec(lv, "ct-h", "Contacts", f'<ul class="contacts">{"".join(items)}</ul>', "applications",
                count=str(len(contacts)))


def stories_block(rec: dict, lv: int) -> str:
    """Stories: each one's type in amber, a drawn rule, its date, then its
    headline as a link to where it goes, then why it matters."""
    sigs = rec.get("signals") or []
    if not sigs:
        return rsec(lv, "sg2-h", "Stories", '<p class="rnone">None yet.</p>', "story", "story")
    items = []
    for s in sigs:
        why = f'<p class="why">{esc(sentence(s["why"]))}</p>' if s.get("why") else ""
        items.append(f'<li>{story_kick(s)}<p class="ttl"><a href="{esc(s["href"])}">{esc(s["headline"])}</a></p>'
                     f'{why}</li>')
    return rsec(lv, "sg2-h", "Stories", f'<ul class="items">{"".join(items)}</ul>', "story", "story",
                count=str(len(sigs)))


def roles_block(rec: dict, lv: int) -> str:
    """Roles on Jobs: each a row, its title linked to the role on Jobs, its
    place and its tier's pill."""
    roles = rec.get("roles") or []
    if not roles:
        return rsec(lv, "rl-h", "Roles on Jobs", '<p class="rnone">No open roles on Jobs.</p>', "jobs", "jobs")
    items = []
    for r in roles:
        place = f'<span class="rl-where">{esc(r["location"])}</span>' if r.get("location") else ""
        items.append(f'<li class="rl"><span class="rl-main"><a class="rl-title" href="{esc(r["href"])}">'
                     f'{esc(r["title"])}</a>{place}</span>'
                     f'{tiers.pill_html(r.get("tier") or "", r["tier_words"])}</li>')
    return rsec(lv, "rl-h", "Roles on Jobs", f'<ul class="roles">{"".join(items)}</ul>', "jobs", "jobs",
                count=str(len(roles)))


def _source_words(source: str, at) -> str:
    if not source:
        return ""
    day = wl.day_words(at) if at else ""
    return f"{source}, read {day}" if day else source


def people_html(rec: dict) -> str:
    people = rec.get("people") or []
    published = [p for p in people if not p.get("hand")]
    hand = [p for p in people if p.get("hand")]
    rows = []
    if published:
        names = ", ".join(f'{p["name"]} ({p["title"]})' if p.get("title") else p["name"] for p in published)
        src = _source_words(published[0].get("source") or "", published[0].get("at"))
        rows.append(f'<li><span class="v">{esc(names)}</span>'
                    + (f'<span class="s">{esc(src)}</span>' if src else "") + '</li>')
    for p in hand:
        pid = int(p["id"])
        rows.append(
            f'<li class="hand"><span class="v">{esc(p["name"])}{", " + esc(p["title"]) if p.get("title") else ""}'
            f'</span><span class="s">Added by you, {esc(wl.day_words(p.get("at")))}</span>'
            f'<button type="button" class="btn btn-ghost" data-person-remove="{pid}" '
            f'data-name="{esc(p["name"])}">Remove<span class="visually-hidden"> {esc(p["name"])}</span></button></li>')
    return f'<ul class="people">{"".join(rows)}</ul>' if rows else ""


def facts_block(rec: dict, lv: int) -> str:
    """Facts: only those known, as rows, each with its source; one line names
    what is not known yet. Add a person and Read again, its cost and its
    confirmation as before."""
    rows, unknown = [], []
    for f in rec.get("facts") or []:
        if f.get("value"):
            src = _source_words(f.get("source") or "", f.get("at"))
            s = f'<span class="s">{esc(src)}</span>' if src else ""
            rows.append((f["label"], f'<span class="v">{esc(f["value"])}</span>{s}'))
        else:
            unknown.append(f["label"])
    people = people_html(rec)
    if people:
        rows.append(("People", people))
    else:
        unknown.append("People")
    site = bd.safe_url(rec.get("site_url"))
    if site:
        rows.append(("Website", f'<a href="{esc(site)}" rel="noopener noreferrer">'
                                f'{esc(site.split("//", 1)[-1].rstrip("/"))}</a>'))
    known = _rows(rows) if rows else ""
    missing = (f'<p class="rnone">Not known yet: {esc(", ".join(u.lower() if i else u for i, u in enumerate(unknown)))}.</p>'
               if unknown else "")
    words = ((rec.get("reread") or {}).get("words") or "")
    cost = esc(words) if words else '<span id="reread-words">working out the cost</span>'
    person_form = (
        '<form class="inline-form" id="person-form" hidden novalidate>'
        + _fld("person-name", "Name", f'<input class="field" type="text" id="person-name" maxlength="{wl.PERSON_MAX}">')
        + _fld("person-title", "Title", f'<input class="field" type="text" id="person-title" '
                                        f'maxlength="{wl.PERSON_MAX}" placeholder="Head of Quality">')
        + '<div class="acts"><button type="submit" class="btn btn-secondary">Add the person</button>'
          '<button type="button" class="btn btn-ghost" id="person-cancel">Cancel</button></div>'
          f'<p class="err" id="person-err" hidden>{ICON_ERR}<span></span></p></form>')
    reread = (
        '<div class="reread-confirm" id="reread-confirm" hidden>'
        f'<p>Read again fetches the company\'s own site once, politely and as its robots file allows, and makes '
        f'one model call: {cost}. What it finds replaces the facts above, each with its source and date.</p>'
        '<div class="acts"><button type="button" class="btn btn-primary" id="reread-go"'
        f'{"" if words else " disabled"}>Read again now<span class="visually-hidden">, {esc(words)}</span></button>'
        '<button type="button" class="btn btn-ghost" id="reread-cancel">Cancel</button></div>'
        '<p class="rmeta" id="reread-status" hidden></p></div>')
    acts = ('<div class="acts"><button type="button" class="btn btn-secondary" id="person-btn" '
            'aria-expanded="false" aria-controls="person-form">Add a person</button>'
            '<button type="button" class="btn btn-secondary" id="reread-btn" aria-expanded="false" '
            'aria-controls="reread-confirm">Read again</button></div>')
    return rsec(lv, "fa-h", "Facts", known + missing + acts + person_form + reread, "list")


def record_head(rec: dict, model: dict, view: str) -> str:
    """The header card (owner, 17 Nov): the teal glow, the building chip, the
    name, the place and since when it is watched, the state; Previous and Next
    company at the right, in the list's order; Log a contact, Set state and
    Remove from Watchlist."""
    name = esc(rec["name"])
    rows = {r["id"]: r for r in model.get("rows") or []}
    pos = model.get("position") or {}

    def href(other: int) -> str:
        return f"/watchlist/{int(other)}" if view == "record" else f"/watchlist?company={int(other)}"

    def step(key: str, label: str, icon: str) -> str:
        other = pos.get(key) if pos else None
        if other is None:
            return (f'<span class="btn btn-secondary prevnext is-disabled" role="link" aria-disabled="true" '
                    f'aria-label="{label}" title="{label}">{icon}</span>')
        oname = rows.get(other, {}).get("name", "")
        return (f'<a class="btn btn-secondary prevnext" data-focus-record href="{href(other)}" '
                f'aria-label="{label}, {esc(oname)}" title="{label}">{icon}</a>')
    nav = step("prev", "Previous company", ICON_LEFT) + step("next", "Next company", ICON_RIGHT)
    if view == "record":
        back = f'<a class="back" href="/watchlist">{ICON_BACK}Back to Watchlist</a>'
        title = f'<h1 class="rec-h" id="rec-h" tabindex="-1">{name}</h1>'
    else:
        back = ('<a class="back back-narrow" data-close-record href="/watchlist">'
                f'{ICON_BACK}Back to Watchlist</a>')
        title = f'<h2 class="rec-h" id="rec-h" tabindex="-1">{name}</h2>'
    watched = (f"On your Watchlist since {wl.day_words(rec.get('watched_at'))}" if rec.get("watched")
               else "Not on your Watchlist")
    facts = [x for x in (rec.get("place"), watched) if x]
    line = ('<span class="visually-hidden">, </span>').join(f'<span class="r-bit">{esc(x)}</span>' for x in facts)
    current = rec.get("stored_state") or ""
    items = "".join(
        f'<li role="none"><button type="button" role="menuitemradio" tabindex="-1" data-state="{esc(k)}" '
        f'aria-checked="{"true" if k == current else "false"}"><span class="chk">{ICON_CHECK}</span>{esc(v)}'
        '</button></li>' for k, v in (model.get("set_states") or wl.SET_STATES))
    menu = ('<div class="menu-wrap"><button type="button" class="btn btn-secondary" id="state-btn" '
            'aria-haspopup="menu" aria-expanded="false" aria-controls="state-menu">Set state</button>'
            f'<ul class="menu" id="state-menu" role="menu" aria-labelledby="state-btn" hidden>{items}</ul></div>')
    if rec.get("watched"):
        watch_btn = '<button type="button" class="btn btn-quiet" id="remove-btn">Remove from Watchlist</button>'
    else:
        watch_btn = '<button type="button" class="btn btn-secondary" id="watch-btn">Add to Watchlist</button>'
    log_btn = ('<button type="button" class="btn btn-teal" id="log-open" aria-expanded="false" '
               'aria-controls="log">Log a contact</button>')
    return (f'<header class="rec-head card g-apps glow-head">{back}<div class="rh-grid">'
            f'<span class="rh-chip">{page_frame.chip("threads", "apps")}</span>{title}'
            f'<div class="rh-nav">{nav}</div>'
            f'<div class="rh-meta"><p class="rh-facts">{line}</p><span class="stag">{esc(rec["state_words"])}</span></div>'
            f'</div><div class="rec-acts">{log_btn}{menu}{watch_btn}</div>'
            f'<div class="status rec-flash" id="rec-flash" tabindex="-1" hidden></div>'
            f'<p class="err" id="rec-err" hidden>{ICON_ERR}<span></span></p></header>')


def record_html(rec: dict, model: dict, view: str) -> str:
    """The record: a stack of cards, 24px apart, each section's title outside
    its card."""
    lv = 2 if view == "record" else 3
    cards = (record_head(rec, model, view) + followup_block(rec, model, lv) + contacts_block(rec, lv)
             + drafts_block(rec, lv) + stories_block(rec, lv) + roles_block(rec, lv) + facts_block(rec, lv))
    tag = "article" if view == "record" else "aside"
    return f'<{tag} class="record" aria-labelledby="rec-h">{cards}</{tag}>'


# ================================================================ the page

def _state(model: dict, view: str) -> dict:
    rows = model.get("rows") or []
    rec = model.get("selected")
    pos = model.get("position") or {}
    th = (rec or {}).get("thread") or {}
    return {
        "view": view,
        "selected": int(rec["id"]) if rec else None,
        "name": rec["name"] if rec else "",
        "watched": bool(rec and rec.get("watched")),
        "positioned": bool(pos), "prev": pos.get("prev"), "next": pos.get("next"),
        "first": rows[0]["id"] if rows else None, "last": rows[-1]["id"] if rows else None,
        "arrivals": [int(a["id"]) for a in model.get("arrivals") or []],
        "step": th.get("next_step") or "", "due": th.get("due_date") or "",
        "today": (rec or {}).get("today") or _today(model).isoformat(),
    }


def _document(title: str, jobs, body: str, state: dict) -> str:
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" href="{bd.FAVICON}">
<style>{page_frame.FRAME_CSS}{page_frame.GROUPS_CSS}{page_frame.TITLES_CSS}{tiers.PILL_CSS}{WATCHLIST_CSS}</style>
</head>
<body>
{page_frame.appbar_html("watchlist", jobs, "Watchlist")}
<main id="main" tabindex="-1" data-page="watchlist" data-state="{esc(json.dumps(state))}">
{body}
</main>
<p class="visually-hidden" id="wl-live" role="status" aria-live="polite"></p>
<script>{bd.SCRIPT}{page_frame.FRAME_SCRIPT}{WATCHLIST_SCRIPT}</script>
</body>
</html>"""


def render_page(model: dict, jobs: dict | None, view: str = "table") -> str:
    """The whole Watchlist document. view table is /watchlist (with the
    selected record beside the table); view record is /watchlist/<id>, the
    record as its own page."""
    state = _state(model, view)
    rec = model.get("selected")
    if view == "record" and rec:
        body = (f'<div class="page recpage">'
                f'<div class="status wl-flash" id="wl-flash" tabindex="-1" hidden></div>'
                f'{record_html(rec, model, "record")}</div>')
        return _document(f"{rec['name']}, Watchlist, MedTech Radar", jobs, body, state)
    missing = ""
    if model.get("missing"):
        missing = ('<div class="status is-warn wl-missing"><span class="what">That company is not on file. '
                   'It may have been merged with another or removed. Your companies are below.</span></div>')
    layout_cls = "layout has-record" if rec else "layout"
    record = record_html(rec, model, "table") if rec else ""
    body = (
        f'<div class="page">{title_html(model)}'
        f'<div class="status wl-flash" id="wl-flash" tabindex="-1" hidden></div>{missing}'
        f'{arrivals_html(model)}'
        f'<div class="{layout_cls}"><div class="main-col">{suggestions_section(model)}{table_html(model)}'
        f'</div>{record}</div></div>')
    title = f"{rec['name']}, Watchlist, MedTech Radar" if rec else "Watchlist, MedTech Radar"
    return _document(title, jobs, body, state)


# ================================================================ CSS
# From the frames (design/watchlist/final, the shared stylesheet), less the
# tokens FRAME_CSS already holds and the states board and notes, plus what
# the frames left to the build: the breakpoints that choose columns, the list
# below 1024, the folded suggestions, the menu, the inline forms and errors.

WATCHLIST_CSS = """
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--canvas)}
body{margin:0;background:var(--canvas);color:var(--text-1);font:400 15px/24px var(--font-sans);
  -webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
a:hover{color:var(--accent-hover)}
h1,h2,h3,h4,p,ol,ul,dl,dd{margin:0}
ol,ul{padding:0;list-style:none}
button,input,select,textarea{font:inherit}
main:focus{outline:none}
:root{--gutter:var(--frame-pad)}
/* The shared content box, page_frame's --frame-max and --frame-pad; a
   company's record keeps its 960px column inside it. */
.page{margin:0 auto;padding:0 var(--frame-pad) 64px;max-width:var(--frame-max)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:32px;padding:5px 12px;
  border:1px solid transparent;border-radius:var(--radius-2);font:500 14px/20px var(--font-sans);text-decoration:none;
  cursor:pointer;text-align:left}
.btn-primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn-primary:hover{color:var(--on-accent);background:var(--accent-hover)}
.btn-secondary{background:var(--surface);border-color:var(--border-strong);color:var(--text-1)}
.btn-secondary:hover{color:var(--text-1);background:var(--row-hover)}
.btn-ghost{background:transparent;color:var(--text-2);padding-left:8px;padding-right:8px}
.btn-ghost:hover{color:var(--text-1);text-decoration:underline;text-underline-offset:2px}
.btn[disabled]{cursor:default;color:var(--text-3);border-color:var(--border-subtle);background:transparent}
.meta{color:var(--text-3);font-size:13px;line-height:20px}
.muted{color:var(--text-2)}

/* The title row (owner, 17 Nov): the teal chip and title with two count
   pills, the add box at the right. */
.pagehead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:16px 32px;padding:24px 0 8px}
.ph-title{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;min-width:0}
.pagehead h1{font:600 20px/28px var(--font-sans)}
.addname{display:grid;gap:4px;min-width:0;width:min(100%,440px)}
.addname .row{display:flex;gap:8px;min-width:0}
.field{min-width:0;width:100%;min-height:36px;padding:7px 10px;border:1px solid var(--border-strong);border-radius:var(--radius-2);
  background:var(--surface-sunken);color:var(--text-1);font:400 15px/20px var(--font-sans)}
.field::placeholder{color:var(--text-3)}
.field.is-error{border-color:var(--negative)}
@media (pointer:coarse){.field{min-height:44px}}
.hint{color:var(--text-2);font-size:13px;line-height:20px}
.hint:empty{display:none}
.combo{position:relative;flex:1;min-width:0}
.listbox{position:absolute;z-index:15;left:0;right:0;top:100%;margin-top:4px;max-height:320px;overflow:auto;
  border:1px solid var(--border-strong);border-radius:var(--radius-2);background:var(--surface);box-shadow:var(--shadow-overlay)}
.listbox li{display:flex;flex-wrap:wrap;justify-content:space-between;gap:2px 12px;padding:8px 10px;border-bottom:1px solid var(--border-subtle);
  font-size:14px;line-height:20px;cursor:pointer}
.listbox li:last-child{border-bottom:0}
.listbox li:hover{background:var(--row-hover)}
.listbox li[aria-selected="true"]{background:var(--row-selected)}
.listbox .opt-name{color:var(--text-1);font-weight:600}
#add-status .status{margin-top:8px}
.arrived{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;margin-top:16px;padding:10px 12px;
  border:1px solid var(--accent);border-radius:var(--radius-2);background:var(--accent-tint);font-size:14px;line-height:20px}
.arrived .what{flex:1 1 320px;min-width:0}
.status{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;padding:10px 12px;border:1px solid var(--border-strong);
  border-radius:var(--radius-2);background:var(--surface);font-size:14px;line-height:20px}
.status:focus{outline:none}
.status .what{flex:1 1 260px;min-width:0}
.status.is-ok{border-color:var(--positive)}
.status.is-bad{border-color:var(--negative);background:var(--negative-tint)}
.status.is-warn{border-color:var(--caution);background:var(--caution-tint)}
.status svg{flex:none}
.status.is-ok svg{color:var(--positive)}
.status.is-bad svg{color:var(--negative)}
.wl-flash,.wl-missing{margin-top:16px}
.rec-flash{margin-top:12px}

/* layout: the list, and the record beside it from 1024 */
.layout{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;margin-top:24px;align-items:start}
.main-col{display:flex;flex-direction:column;gap:32px;min-width:0}
@media (min-width:1024px){
  .layout.has-record{grid-template-columns:minmax(0,1fr) 560px}
  .layout>.record{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto}
}
@media (min-width:1800px){.layout.has-record{grid-template-columns:minmax(0,1fr) clamp(720px,40vw,1000px);gap:32px}}
@media (max-width:1023.98px){.layout>.record{order:-1}}

/* cards on the page's ground: the site's ground and border, 14px corners,
   a glow of their group's colour over it. Text on them is the secondary
   colour at its quietest, never the muted one. */
.card{min-width:0;border:1px solid var(--border-subtle);border-radius:14px;background-color:var(--surface)}
.muted{color:var(--text-2)}
.link{color:var(--accent);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:2px}
.btn-teal{background:var(--group-apps);border-color:var(--group-apps);color:var(--on-accent)}
.btn-teal:hover{color:var(--on-accent);filter:brightness(1.08)}

/* Suggested by Radar: wide cards glowing amber */
.sg-list{display:flex;flex-direction:column;gap:12px}
.sg{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px 24px;padding:16px 20px}
.sg-main{flex:1 1 360px;min-width:0}
.sg h3{font:600 17px/24px var(--font-sans);overflow-wrap:anywhere}
.sg-headline{margin-top:2px;color:var(--text-2);font-size:14px;line-height:20px;max-width:80ch}
.sg-link{margin-top:4px;font-size:14px;line-height:20px}
.sg .acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.dis-head{margin-top:12px}
.dis-head .btn{padding-left:0}
.dis-list li{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;justify-content:space-between;padding:8px 0;
  border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.dis-list li>span{min-width:0;flex:1 1 260px}
.dis-list .d-how{display:block}

/* Your companies: one wide card a company, glowing teal, in columns: the
   company, its follow-up, its latest story, its roles on Jobs, Log a contact. */
.co-list{display:flex;flex-direction:column;gap:12px}
.co-card{container:cocard / inline-size;padding:16px 20px}
.co-card.is-selected{border-color:var(--group-apps)}
.co-grid{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1.3fr) minmax(0,1.5fr) minmax(0,.7fr) auto;gap:12px 24px;align-items:start}
.co-col{min-width:0}
.co-name{display:flex;gap:12px;align-items:flex-start}
.co-id{display:flex;flex-direction:column;align-items:flex-start;gap:4px;min-width:0}
.co{color:var(--text-1);font:600 16px/24px var(--font-sans);text-decoration:none;overflow-wrap:anywhere}
.co:hover{color:var(--text-1);text-decoration:underline;text-underline-offset:2px}
.co-where{color:var(--text-2);font-size:14px;line-height:20px}
.co-label{font:600 13px/20px var(--font-sans)}
.fu-label{color:var(--group-apps)}
.story-label{color:var(--text-2)}
.roles-label{color:var(--group-jobs)}
.fu-step{color:var(--text-1);font-size:14px;line-height:20px;overflow-wrap:anywhere}
.fu-when{color:var(--text-2)}
.fu-last{margin-top:2px;color:var(--text-2);font-size:14px;line-height:20px}
.co-fu .ttag{margin-top:6px}
.kick{display:flex;flex-wrap:wrap;align-items:center;font-size:13px;line-height:20px;color:var(--text-2)}
.kick .r-bit ~ .r-bit::before{content:"";display:inline-block;width:0;height:12px;margin:0 8px;border-left:1px solid var(--text-3);vertical-align:-1px}
.sk-type{color:var(--group-story);font-weight:600}
.co-headline{margin-top:2px;color:var(--text-2);font-size:14px;line-height:20px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.role-pill{display:inline-flex;align-items:center;height:24px;margin-top:4px;padding:0 10px;border-radius:12px;background:rgba(167,156,240,.18);color:#c9c2f8;font:600 13px/20px var(--font-sans);white-space:nowrap}
.role-pill.is-none{background:rgba(167,156,240,.10);color:var(--text-2);font-weight:500}
.co-act{align-self:center}
/* Narrower than 900px of card: two columns, the company and its follow-up,
   then the story and the roles, Log a contact last. */
@container cocard (max-width:899.98px){
  .co-grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
  .co-act{grid-column:1 / -1;align-self:start}
}
@container cocard (max-width:519.98px){.co-grid{grid-template-columns:minmax(0,1fr)}}
.empty{padding:20px}
.empty p{color:var(--text-2);font-size:14px;line-height:20px}

/* tags: a shape and a word, never colour alone */
.ttag,.ntag,.stag{display:inline-flex;align-items:center;gap:5px;padding:1px 7px;border:1px solid;border-radius:var(--radius-2);
  font:600 13px/18px var(--font-sans);white-space:nowrap}
.ttag.is-overdue{color:var(--negative);border-color:var(--negative);background:var(--negative-tint)}
.ttag.is-today{color:var(--caution);border-color:var(--caution);background:var(--caution-tint)}
.ttag.is-later{color:var(--text-1);border-color:var(--border-strong);background:var(--surface)}
.ttag.is-quiet{color:var(--text-3);border-color:var(--border-subtle);background:transparent;font-weight:500}
.ntag{color:var(--accent);border-color:var(--accent);background:var(--accent-tint)}
.stag{color:var(--text-2);border-color:var(--border-subtle);background:var(--surface-sunken);font-weight:500}

/* the record (owner, 17 Nov): a stack of cards 24px apart, each section's
   title outside its card; the header card glows teal. */
.record{display:flex;flex-direction:column;gap:24px;min-width:0;align-self:start}
.rec-head{container:rhead / inline-size;padding:16px 20px}
.rh-grid{display:grid;grid-template-columns:auto minmax(0,1fr) auto;grid-template-areas:"chip title nav" ". meta meta";column-gap:14px}
.rh-chip{grid-area:chip;padding-top:2px}
.rec-h{grid-area:title;align-self:center;font:600 24px/32px var(--font-sans);overflow-wrap:anywhere;text-wrap:balance}
.rec-h:focus{outline:none}
.rh-nav{grid-area:nav;display:flex;gap:8px;align-self:start}
.prevnext{width:32px;padding:0;justify-content:center}
.prevnext.is-disabled{color:var(--text-3);border-color:var(--border-subtle);cursor:default}
.rh-meta{grid-area:meta;display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;margin-top:4px}
.rh-facts{display:flex;flex-wrap:wrap;color:var(--text-2);font-size:14px;line-height:20px}
.rh-facts .r-bit ~ .r-bit::before{content:"";display:inline-block;width:0;height:14px;margin:0 8px;border-left:1px solid var(--text-3);vertical-align:-2px}
.rec-acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.btn-quiet{background:transparent;border-color:var(--border-subtle);color:var(--text-2)}
.btn-quiet:hover{color:var(--text-1);border-color:var(--border-strong)}
.back{display:inline-flex;align-items:center;gap:6px;min-height:44px;margin:-8px 0 4px;color:var(--accent);font-size:15px}
.back-narrow{display:none}
@media (max-width:1023.98px){.back-narrow{display:inline-flex}}
@container rhead (max-width:559.98px){.rh-grid{grid-template-columns:auto minmax(0,1fr);grid-template-areas:"chip title" "meta meta" "nav nav"}.rh-nav{margin-top:8px}}
.rsec{min-width:0}
.rcard{padding:16px 20px;font-size:14px;line-height:20px}
.rnone{color:var(--text-2)}
.rmeta{color:var(--text-2);font-size:13px;line-height:20px}
.rrows{display:grid;gap:0}
.rrow{display:grid;grid-template-columns:120px minmax(0,1fr);gap:2px 12px;padding:8px 0;border-top:1px solid rgba(232,237,240,.08)}
.rrow:first-child{border-top:0;padding-top:0}
.rrow dt{color:var(--text-2);font-weight:600}
.rrow dd{color:var(--text-1);overflow-wrap:anywhere}
.rrow dd .s{display:block;color:var(--text-2);font-size:13px}
.rcard .acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}
.acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.log-wrap{margin-top:16px;padding-top:16px;border-top:1px solid rgba(232,237,240,.08)}
.log-h{font:600 15px/24px var(--font-sans)}
.drafts .draft + .draft{margin-top:16px}
.drafts .dh{font:600 14px/20px var(--font-sans)}
.drafts .text{margin-top:4px;padding:12px;border:1px solid var(--border-subtle);border-radius:var(--radius-2);
  background:var(--canvas);color:var(--text-1);white-space:pre-wrap;overflow-wrap:anywhere}
.drafts .text.is-picked{background:var(--row-selected)}
.drafts .draft-foot{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;margin-top:8px}
.ic-tick{display:none}
.is-copied .ic-tick{display:inline}
.is-copied .ic-copy{display:none}
.logform{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px 16px;margin-top:12px}
.logform .full{grid-column:1/-1}
.fld{min-width:0}
.fld>label,.logform legend{display:block;color:var(--text-2);font-size:14px;line-height:20px;font-weight:500}
.fld>.field{display:block;margin-top:4px}
textarea.field{min-height:72px;resize:vertical}
select.field{padding-right:6px}
input[type=date].field{color-scheme:dark}
.logform fieldset{margin:0;padding:0;border:0;min-width:0}
.logform .radios{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:6px}
.logform .radios label{display:inline-flex;align-items:center;gap:6px;min-height:32px;color:var(--text-1);font-weight:400;font-size:14px}
.logform input[type=radio]{width:16px;height:16px;margin:0;accent-color:var(--accent)}
.err{display:flex;gap:6px;align-items:flex-start;margin-top:4px;color:var(--negative);font-size:13px;line-height:20px}
.err svg{flex:none;margin-top:3px}
.logform .submit{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
.logform .submit .err{flex-basis:100%}
@media (max-width:767.98px){.logform{grid-template-columns:minmax(0,1fr)}}
.inline-form{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:flex-end;margin-top:12px;padding:12px;
  border:1px solid var(--border-subtle);border-radius:var(--radius-2);background:var(--canvas)}
.inline-form .fld{flex:1 1 180px}
.inline-form>.err{flex-basis:100%}
.contacts li{display:grid;grid-template-columns:72px minmax(0,1fr);gap:2px 12px;padding:12px 0;border-top:1px solid rgba(232,237,240,.08)}
.contacts li:first-child{border-top:0;padding-top:0}
.contacts .when{color:var(--text-1);font-weight:600}
.contacts .how{color:var(--text-2);font-weight:600}
.contacts .note{color:var(--text-1);overflow-wrap:anywhere}
.contacts .set{color:var(--text-2)}
.contacts .out{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;color:var(--text-2)}
.contacts .out b{color:var(--text-1);font-weight:600}
.items li{padding:12px 0;border-top:1px solid rgba(232,237,240,.08)}
.items li:first-child{border-top:0;padding-top:0}
.items .ttl{margin-top:2px;font-weight:600;overflow-wrap:anywhere}
.items .ttl a{color:var(--text-1)}
.items .why{margin-top:2px;color:var(--text-2);max-width:80ch}
.roles li{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 16px;padding:10px 0;border-top:1px solid rgba(232,237,240,.08)}
.roles li:first-child{border-top:0;padding-top:0}
.rl-main{display:flex;flex-direction:column;min-width:0;flex:1 1 240px}
.rl-title{color:var(--text-1);font-weight:600;overflow-wrap:anywhere}
.rl-where{color:var(--text-2)}
.people li+li{margin-top:6px}
.people li.hand{display:flex;flex-wrap:wrap;gap:0 8px;align-items:baseline}
.people li.hand .s{flex-basis:100%;order:2}
.people .s{display:block;color:var(--text-2);font-size:13px}
.reread-confirm{margin-top:12px;padding:12px;border:1px solid var(--border-strong);border-radius:var(--radius-2);
  background:var(--canvas);color:var(--text-1)}
.reread-confirm .acts{margin-top:8px}
.reread-confirm #reread-status{margin-top:8px}

/* the Set state menu */
.menu-wrap{position:relative;display:inline-block}
.menu{position:absolute;z-index:10;top:calc(100% + 4px);left:0;min-width:220px;padding:4px 0;border:1px solid var(--border-strong);
  border-radius:var(--radius-2);background:var(--surface);box-shadow:var(--shadow-overlay)}
.menu button{display:flex;align-items:center;gap:8px;width:100%;min-height:32px;padding:6px 12px;border:0;background:transparent;
  color:var(--text-1);font:400 14px/20px var(--font-sans);text-align:left;cursor:pointer}
.menu button:hover,.menu button:focus{background:var(--row-selected)}
.menu button:focus-visible{outline-offset:-2px;box-shadow:none}
.menu .chk{display:inline-flex;width:14px;color:var(--accent)}
.menu [aria-checked="false"] .chk svg{visibility:hidden}

/* the record as its own view */
.recpage{max-width:960px;padding-top:8px}
.recpage .wl-flash{margin:8px 0}

/* touch: 44 pixels */
@media (pointer:coarse){.btn,.menu button,.logform .radios label{min-height:44px}}
"""


# ================================================================ script

WATCHLIST_SCRIPT = r"""
(function () {
  var main = document.querySelector('[data-page="watchlist"]');
  if (!main) return;
  var state = {};
  try { state = JSON.parse(main.getAttribute('data-state') || '{}'); } catch (e) { state = {}; }
  var FLASH = 'radar-watchlist-flash', FOCUS = 'radar-watchlist-focus';
  var SG = 'radar-watchlist-suggestions';
  var live = document.getElementById('wl-live');
  var ICON_OK = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true" focusable="false">'
    + '<circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.6"/>'
    + '<path d="M5.2 8.2l1.9 1.8 3.8-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var ICON_BAD = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true" focusable="false">'
    + '<path d="M8 1.8 15 14H1z" fill="currentColor"/></svg>';

  // ---- small helpers
  function say(text) {
    if (!live || !text) return;
    live.textContent = '';
    setTimeout(function () { live.textContent = text; }, 60);
  }
  function sget(key) { try { return window.sessionStorage.getItem(key); } catch (e) { return null; } }
  function sset(key, value) {
    try {
      if (value === null) window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key, value);
    } catch (e) {}
  }
  function typing(el) {
    if (!el || !el.tagName) return false;
    var t = el.tagName;
    return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || !!el.isContentEditable;
  }
  function visible(el) { return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)); }
  function wide() { return !!(window.matchMedia && window.matchMedia('(min-width:1024px)').matches); }
  function recordUrl(id) { return wide() ? '/watchlist?company=' + id : '/watchlist/' + id; }
  var UNREACHABLE = 'Radar could not be reached. Check the connection and try again.';
  function post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body || {}) })
      .then(function (r) {
        return r.json().catch(function () {
          return { ok: false, note: 'Radar answered in a way the page did not expect (' + r.status + '). Reload the page.' };
        });
      }, function () { return { ok: false, note: UNREACHABLE }; });
  }
  // Leave the page for url (or reload it), carrying a one-line note and
  // where focus should land, said after the load.
  function go(url, flash, focus) {
    if (flash) { flash.at = Date.now(); sset(FLASH, JSON.stringify(flash)); }
    if (focus) sset(FOCUS, focus);
    var here = location.pathname + location.search;
    if (!url || url === here) location.reload();
    else location.assign(url);
  }

  // ---- the one-line note: after a reload, or at once for a refusal
  function showNote(f) {
    var box = null;
    if (f.where === 'record') box = document.getElementById('rec-flash');
    if (!box) box = document.getElementById('wl-flash');
    if (!box) { say(f.text); return null; }
    box.innerHTML = '';
    box.className = 'status ' + (box.id === 'rec-flash' ? 'rec-flash' : 'wl-flash') + (f.tone ? ' is-' + f.tone : '');
    if (f.tone === 'ok') box.insertAdjacentHTML('beforeend', ICON_OK);
    if (f.tone === 'bad') box.insertAdjacentHTML('beforeend', ICON_BAD);
    var what = document.createElement('span');
    what.className = 'what';
    if (f.name) {
      var b = document.createElement('b');
      b.textContent = f.name;
      what.appendChild(b);
      what.appendChild(document.createTextNode(' ' + f.text));
    } else {
      what.textContent = f.text;
    }
    box.appendChild(what);
    var undo = null;
    if (f.undo) {
      undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'btn btn-secondary';
      undo.textContent = 'Undo';
      undo.addEventListener('click', function () {
        undo.disabled = true;
        post(f.undo.url, f.undo.body).then(function (r) {
          if (r && r.ok) {
            if (f.undo.sg) sset(SG, 'open');
            go(f.undo.go || null, { name: f.name, text: f.undo.done, tone: 'ok', where: f.where, focusNote: true },
               f.undo.focus || null);
          } else {
            undo.disabled = false;
            say((r && r.note) || 'The undo did not go through. Reload the page.');
          }
        });
      });
      box.appendChild(undo);
    }
    if (f.link) {
      var a = document.createElement('a');
      a.className = 'btn btn-secondary';
      a.href = f.link.href;
      a.textContent = f.link.text;
      box.appendChild(a);
    }
    box.hidden = false;
    say((f.name ? f.name + ' ' : '') + f.text);
    return undo || box;
  }
  function refusal(note, near) {
    var text = note || 'That did not go through. Reload the page and try again.';
    var err = near || null;
    if (err && err.classList && err.classList.contains('err')) {
      err.querySelector('span').textContent = text;
      err.hidden = false;
      say(text);
      return;
    }
    var el = showNote({ text: text, tone: 'bad' });
    if (el && el.focus) el.focus();
  }

  // ---- field errors, tied to their field
  function clearErrors(scope) {
    if (!scope) return;
    scope.querySelectorAll('[aria-invalid]').forEach(function (i) {
      i.removeAttribute('aria-invalid');
      i.classList.remove('is-error');
      var hint = i.getAttribute('data-hint');
      if (hint) i.setAttribute('aria-describedby', hint); else i.removeAttribute('aria-describedby');
    });
    scope.querySelectorAll('.err').forEach(function (e) { e.hidden = true; });
  }
  function fieldError(input, errEl, note) {
    if (!errEl) { refusal(note); return; }
    errEl.querySelector('span').textContent = note;
    errEl.hidden = false;
    if (input) {
      input.classList.add('is-error');
      input.setAttribute('aria-invalid', 'true');
      var hint = input.getAttribute('data-hint');
      input.setAttribute('aria-describedby', errEl.id + (hint ? ' ' + hint : ''));
      input.focus();
    }
    say(note);
  }
  function dayWords(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
    if (!m) return iso || '';
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return String(+m[3]) + ' ' + months[+m[2] - 1];
  }

  // ---- the contact form, inside Follow-up: closed until Log a contact
  function openLog(focus) {
    var wrap = document.getElementById('log'), btn = document.getElementById('log-open');
    if (!wrap) return;
    wrap.hidden = false;
    if (btn) btn.setAttribute('aria-expanded', 'true');
    if (focus) { var d = document.getElementById('log-date'); if (d) d.focus(); }
  }
  function closeLog() {
    var wrap = document.getElementById('log'), btn = document.getElementById('log-open');
    if (!wrap) return;
    wrap.hidden = true;
    if (btn) { btn.setAttribute('aria-expanded', 'false'); btn.focus(); }
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('#log-open')) {
      var wrap = document.getElementById('log');
      if (wrap && wrap.hidden) openLog(true); else closeLog();
    } else if (e.target.closest('#log-cancel')) closeLog();
  });
  if (location.hash === '#log') openLog(false);

  // ---- on load: the note, focus, and the arrival from Insights
  (function () {
    var raw = sget(FLASH);
    if (raw) {
      sset(FLASH, null);
      var f = null;
      try { f = JSON.parse(raw); } catch (e) { f = null; }
      if (f && f.text && (!f.at || Date.now() - f.at < 120000)) {
        var target = showNote(f);
        if ((f.undo || f.focusNote) && target && target.focus) { sset(FOCUS, null); target.focus(); }
      }
    }
    var want = sget(FOCUS);
    if (want) {
      sset(FOCUS, null);
      var el = null;
      if (want === 'record') el = document.getElementById('rec-h');
      else if (want === 'log') { openLog(); el = document.getElementById('log-date'); }
      else if (want.indexOf('row:') === 0) {
        var id = want.slice(4);
        el = document.querySelector('[data-row-link="' + id + '"]');
      }
      if (el && el.focus) el.focus();
    }
    if (state.arrivals && state.arrivals.length) {
      post('/watchlist/arrived', { company_ids: state.arrivals });
    }
  })();

  // ---- links that open or close the record carry focus with them
  document.addEventListener('click', function (e) {
    var a = e.target.closest('a');
    if (!a) return;
    if (a.hasAttribute('data-focus-log')) sset(FOCUS, 'log');
    else if (a.hasAttribute('data-focus-record') || a.hasAttribute('data-row-link')) sset(FOCUS, 'record');
    else if (a.hasAttribute('data-close-record') && state.selected) sset(FOCUS, 'row:' + state.selected);
    else if (a.hasAttribute('data-focus-log') && a.pathname === location.pathname
             && a.search === location.search) {
      // The card of the record already open: open its form here.
      e.preventDefault();
      openLog(true);
    }
  });

  // ---- add a company by name: a combobox
  var input = document.getElementById('add-name');
  if (input) {
    var list = document.getElementById('add-list');
    var form = document.getElementById('add-form');
    var hint = document.getElementById('add-hint');
    var addStatus = document.getElementById('add-status');
    var hintStart = hint.textContent;
    var matches = [], active = -1, timer = null, seq = 0;
    function closeList() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }
    function setActive(i) {
      var items = list.querySelectorAll('[role="option"]');
      if (!items.length) return;
      active = Math.max(0, Math.min(i, items.length - 1));
      items.forEach(function (li, n) { li.setAttribute('aria-selected', n === active ? 'true' : 'false'); });
      input.setAttribute('aria-activedescendant', items[active].id);
      if (items[active].scrollIntoView) items[active].scrollIntoView({ block: 'nearest' });
    }
    function renderList() {
      list.innerHTML = '';
      matches.forEach(function (m, i) {
        var li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.id = 'opt-' + m.id;
        li.setAttribute('aria-selected', 'false');
        var nm = document.createElement('span');
        nm.className = 'opt-name';
        nm.textContent = m.name;
        var meta = document.createElement('span');
        meta.className = 'meta';
        meta.textContent = [m.place, m.seen, m.watched ? 'on your Watchlist since ' + m.since : 'not on your Watchlist']
          .filter(Boolean).join(' · ');
        li.appendChild(nm);
        li.appendChild(meta);
        li.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
        li.addEventListener('click', function () { setActive(i); submitAdd(); });
        list.appendChild(li);
      });
      if (matches.length) {
        list.hidden = false;
        input.setAttribute('aria-expanded', 'true');
      } else {
        closeList();
      }
    }
    function clearStatus() { addStatus.innerHTML = ''; }
    function statusBox(tone, html) {
      clearStatus();
      var div = document.createElement('div');
      div.className = 'status' + (tone ? ' is-' + tone : '');
      div.setAttribute('role', 'status');
      div.innerHTML = html;
      addStatus.appendChild(div);
      return div;
    }
    function esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    }
    function showAlready(name, since, id) {
      closeList();
      hint.textContent = 'Nothing was added twice.';
      statusBox('', '<span class="what"><b>' + esc(name) + '</b> is already on your Watchlist'
        + (since ? ', since ' + esc(since) : '') + '.</span><a class="btn btn-secondary" data-focus-record href="'
        + esc(recordUrl(id)) + '">Open its record</a>');
      say(name + ' is already on your Watchlist' + (since ? ', since ' + since : '') + '. Nothing was added twice.');
    }
    function showNotFound(name) {
      closeList();
      hint.textContent = 'Nothing Radar has seen matches. Check the spelling, or add it anyway.';
      var box = statusBox('warn', '<span class="what">Radar has not seen <b>' + esc(name) + '</b>. Add it anyway '
        + 'and it joins your Watchlist with its facts Not known until a story or an advert names it.</span>'
        + '<button type="button" class="btn btn-secondary">Add ' + esc(name) + ' anyway</button>');
      var btn = box.querySelector('button');
      btn.addEventListener('click', function () { btn.disabled = true; addName({ name: name, anyway: true }); });
      say('Radar has not seen ' + name + '. Add it anyway, with the button after the field, or check the spelling.');
    }
    function addName(body) {
      post('/watchlist/add-name', body).then(function (r) {
        if (r.ok && r.already) { showAlready(r.name, r.since, r.id); return; }
        if (r.ok) {
          go(null, { name: r.name, tone: 'ok',
                     text: r.unknown ? 'is on your Watchlist, added by hand. Radar has not seen it, so its facts read '
                                       + 'Not known until a story or an advert names it.'
                                     : 'is on your Watchlist, added by hand. Its row is in the table.',
                     undo: { url: '/watchlist/remove', body: { company_id: r.id, undo_add: true, since: r.since },
                             done: 'is off your Watchlist again.' } });
          return;
        }
        if (r.not_found) { showNotFound(r.name || body.name); return; }
        if (r.choose && r.choose.length) {
          matches = r.choose;
          renderList();
          setActive(0);
          input.focus();
          hint.textContent = r.note || 'Choose one from the list.';
          say(hint.textContent);
          return;
        }
        hint.textContent = r.note || 'That did not go through. Try again.';
        say(hint.textContent);
      });
    }
    function submitAdd() {
      var m = (!list.hidden && active >= 0) ? matches[active] : null;
      var name = input.value.trim();
      if (!m && !name) { hint.textContent = "Type a company's name first."; say(hint.textContent); input.focus(); return; }
      closeList();
      if (m && m.watched) { showAlready(m.name, m.since, m.id); return; }
      addName(m ? { company_id: m.id } : { name: name });
    }
    input.addEventListener('input', function () {
      clearStatus();
      clearTimeout(timer);
      var q = input.value.trim();
      if (q.length < 2) { matches = []; closeList(); hint.textContent = hintStart; return; }
      timer = setTimeout(function () {
        var mine = ++seq;
        fetch('/watchlist/find?q=' + encodeURIComponent(q)).then(function (r) { return r.json(); })
          .then(function (j) {
            if (mine !== seq) return;
            matches = (j && j.matches) || [];
            renderList();
            if (matches.length) {
              hint.textContent = matches.length + ' found. Up and Down choose, Enter adds the chosen one.';
            } else {
              hint.textContent = 'Nothing Radar has seen matches yet. Enter or Add looks again and offers to add it anyway.';
            }
            say(hint.textContent);
          }).catch(function () { if (mine === seq) { hint.textContent = UNREACHABLE; say(UNREACHABLE); } });
      }, 250);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        if (!matches.length) return;
        e.preventDefault();
        if (list.hidden) { list.hidden = false; input.setAttribute('aria-expanded', 'true'); }
        setActive(active + 1);
      } else if (e.key === 'ArrowUp') {
        if (list.hidden || !matches.length) return;
        e.preventDefault();
        setActive(active - 1);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        submitAdd();
      } else if (e.key === 'Escape') {
        if (!list.hidden) { e.preventDefault(); closeList(); }
        else if (input.value) { e.preventDefault(); input.value = ''; matches = []; clearStatus(); hint.textContent = hintStart; }
      }
    });
    input.addEventListener('blur', function () { setTimeout(closeList, 150); });
    form.addEventListener('submit', function (e) { e.preventDefault(); submitAdd(); });
  }

  // ---- suggestions: answers, and Dismissed
  // A company suggested again finds the suggestions' title focused after the reload.
  var sgWant = sget(SG);
  if (sgWant) {
    sset(SG, null);
    var sgH = document.getElementById('sg-h');
    if (sgWant === 'focus' && sgH) sgH.focus();
  }
  var disBtn = document.getElementById('dis-btn');
  if (disBtn) disBtn.addEventListener('click', function () {
    var open = disBtn.getAttribute('aria-expanded') !== 'true';
    disBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.getElementById('dis-list').hidden = !open;
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-sg]');
    if (!b) return;
    var action = b.getAttribute('data-sg');
    var id = parseInt(b.getAttribute('data-id'), 10);
    var name = b.getAttribute('data-name') || '';
    var type = b.getAttribute('data-type') || 'signal';
    var kind = b.getAttribute('data-kind') || '';
    b.disabled = true;
    post('/watchlist/suggestion', { id: id, action: action }).then(function (r) {
      if (!r.ok) { b.disabled = false; refusal(r.note); return; }
      var undo = { url: '/watchlist/suggestion', body: { id: id, action: 'undo' }, sg: true };
      if (action === 'accept') {
        undo.done = 'is back in the suggestions, not on your Watchlist.';
        go(null, { name: name, tone: 'ok', undo: undo,
                   text: 'is on your Watchlist. It moved from the suggestions into your companies.' });
      } else if (action === 'dismiss') {
        undo.done = 'is back in the suggestions.';
        go(null, { name: name, undo: undo,
                   text: 'dismissed. ' + (kind === 'buying-window'
                        ? 'Radar will not suggest it again. It stays in Dismissed for 90 days, with Suggest again.'
                        : kind === 'funding'
                          ? 'Radar will not suggest it again on a funding round or a story. A buying window later '
                            + 'can bring it back once, marked as dismissed before.'
                          : 'Radar will not suggest it again on another story. A funding round or a buying window '
                            + 'later can bring it back once, marked as dismissed before.') });
      } else {
        sset(SG, 'focus');
        go(null, { name: name, tone: 'ok', text: 'is back in the suggestions.' });
      }
    });
  });

  // ---- the record
  var cid = state.selected;
  var recErr = document.getElementById('rec-err');
  function recNote(text, extra) {
    var f = { text: text, tone: 'ok', where: 'record' };
    if (extra) for (var k in extra) f[k] = extra[k];
    return f;
  }
  function toggle(btn, box, focusEl) {
    if (!btn || !box) return;
    btn.addEventListener('click', function () {
      var open = box.hidden;
      box.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open && focusEl) focusEl.focus();
    });
  }

  // Set state: a menu button. Arrows move, Enter sets, Esc closes.
  var stateBtn = document.getElementById('state-btn');
  var menu = document.getElementById('state-menu');
  if (stateBtn && menu) {
    var items = Array.prototype.slice.call(menu.querySelectorAll('[role="menuitemradio"]'));
    function openMenu(at) {
      menu.hidden = false;
      stateBtn.setAttribute('aria-expanded', 'true');
      var checked = items.filter(function (i) { return i.getAttribute('aria-checked') === 'true'; })[0];
      var target = at === 'last' ? items[items.length - 1] : (at === 'first' ? items[0] : (checked || items[0]));
      target.focus();
    }
    function closeMenu(back) {
      menu.hidden = true;
      stateBtn.setAttribute('aria-expanded', 'false');
      if (back) stateBtn.focus();
    }
    stateBtn.addEventListener('click', function () { if (menu.hidden) openMenu(); else closeMenu(true); });
    stateBtn.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); openMenu('first'); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); openMenu('last'); }
    });
    menu.addEventListener('keydown', function (e) {
      var i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === 'Home') { e.preventDefault(); items[0].focus(); }
      else if (e.key === 'End') { e.preventDefault(); items[items.length - 1].focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
      else if (e.key === 'Tab') { closeMenu(false); }
    });
    document.addEventListener('click', function (e) {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== stateBtn && !stateBtn.contains(e.target)) closeMenu(false);
    });
    items.forEach(function (item) {
      item.addEventListener('click', function () {
        var value = item.getAttribute('data-state');
        var words = item.textContent.trim();
        closeMenu(true);
        if (value === 'dead' && !window.confirm('Set ' + state.name + ' to Not pursuing? It outranks every '
            + 'other state, and only another state from this menu changes it.')) return;
        post('/company/state', { id: cid, state: value, confirm: true }).then(function (r) {
          if (!r.ok) { refusal(r.note, recErr); return; }
          go(null, recNote('The state is now ' + words + '.'), 'record');
        });
      });
    });
  }

  var removeBtn = document.getElementById('remove-btn');
  if (removeBtn) removeBtn.addEventListener('click', function () {
    removeBtn.disabled = true;
    post('/watchlist/remove', { company_id: cid }).then(function (r) {
      if (!r.ok) { removeBtn.disabled = false; refusal(r.note, recErr); return; }
      var next = state.view === 'table' ? (state.next || state.prev) : null;
      var url = state.view === 'table' ? (next ? '/watchlist?company=' + next : '/watchlist') : null;
      go(url, { name: state.name, where: state.view === 'table' ? 'page' : 'record',
                text: 'is off your Watchlist. Its contacts, signals, stories and roles stay on file, and Radar may '
                  + 'suggest it again after a new signal.',
                undo: { url: '/watchlist/restore', body: { company_id: cid }, done: 'is back on your Watchlist.' } });
    });
  });
  var watchBtn = document.getElementById('watch-btn');
  if (watchBtn) watchBtn.addEventListener('click', function () {
    watchBtn.disabled = true;
    post('/watchlist/add', { company_id: cid, via: 'name' }).then(function (r) {
      if (!r.ok) { watchBtn.disabled = false; refusal(r.note, recErr); return; }
      go(null, { name: state.name, tone: 'ok', where: 'record', text: 'is on your Watchlist.',
                 undo: r.already ? null : { url: '/watchlist/remove', body: { company_id: cid, undo_add: true,
                                                                              since: r.watched_at },
                         done: 'is off your Watchlist again.' } }, 'record');
    });
  });

  // Thread: Move the date, Clear the next step, each with Undo.
  var moveBtn = document.getElementById('move-btn');
  var moveForm = document.getElementById('move-form');
  var moveDue = document.getElementById('move-due');
  toggle(moveBtn, moveForm, moveDue);
  var moveCancel = document.getElementById('move-cancel');
  if (moveCancel) moveCancel.addEventListener('click', function () {
    moveForm.hidden = true; moveBtn.setAttribute('aria-expanded', 'false'); clearErrors(moveForm); moveBtn.focus();
  });
  function threadUndo(r) {
    var before = (r && r.before) || {};
    return { url: '/watchlist/thread', done: 'The next step and its date are back as they were.',
             body: { company_id: cid, next_step: before.next_step || '', due: before.due || '', restore: true } };
  }
  if (moveForm) moveForm.addEventListener('submit', function (e) {
    e.preventDefault();
    clearErrors(moveForm);
    var due = moveDue.value;
    if (!due) { fieldError(moveDue, document.getElementById('move-err'), 'Choose the new due date.'); return; }
    post('/watchlist/thread', { company_id: cid, next_step: state.step, due: due }).then(function (r) {
      if (!r.ok) { fieldError(moveDue, document.getElementById('move-err'), r.note || 'The date did not save.'); return; }
      go(null, recNote('The next step is now due ' + dayWords(due) + '.', { undo: threadUndo(r) }));
    });
  });
  var clearBtn = document.getElementById('clear-btn');
  if (clearBtn) clearBtn.addEventListener('click', function () {
    clearBtn.disabled = true;
    post('/watchlist/thread', { company_id: cid, next_step: '', due: '' }).then(function (r) {
      if (!r.ok) { clearBtn.disabled = false; refusal(r.note, recErr); return; }
      go(null, recNote('The next step is cleared, so the thread is closed.', { undo: threadUndo(r) }));
    });
  });

  // Drafts: Copy, and when the browser refuses, the text selected to copy by hand.
  function selectText(el) {
    try {
      var range = document.createRange();
      range.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return true;
    } catch (e) { return false; }
  }
  function legacyCopy(el) {
    if (!selectText(el)) return false;
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    if (ok) { try { window.getSelection().removeAllRanges(); } catch (e) {} }
    return ok;
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-copy]');
    if (!btn) return;
    var src = document.getElementById(btn.getAttribute('data-copy'));
    if (!src) return;
    var foot = btn.parentNode;
    var err = foot.querySelector('.copy-err');
    var lbl = btn.querySelector('.lbl');
    if (!btn.hasAttribute('data-label')) btn.setAttribute('data-label', lbl.textContent);
    function done() {
      if (err) err.hidden = true;
      src.classList.remove('is-picked');
      btn.classList.add('is-copied');
      lbl.textContent = 'Copied';
      say('Copied. Paste it where you post it and make it yours.');
      setTimeout(function () { btn.classList.remove('is-copied'); lbl.textContent = btn.getAttribute('data-label'); }, 2000);
    }
    function fail() {
      selectText(src);
      src.classList.add('is-picked');
      if (err) err.hidden = false;
      say('The browser would not copy. Select the text and copy it by hand.');
    }
    var text = src.textContent;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { if (legacyCopy(src)) done(); else fail(); });
    } else if (legacyCopy(src)) {
      done();
    } else {
      fail();
    }
  });

  // Log a contact: a next step needs a due date; empty closes the thread.
  var logForm = document.getElementById('log-form');
  if (logForm) logForm.addEventListener('submit', function (e) {
    e.preventDefault();
    clearErrors(logForm);
    var f = function (id) { return document.getElementById(id); };
    var checked = logForm.querySelector('input[name="outcome"]:checked');
    var body = { company_id: cid, date: f('log-date').value, channel: f('log-channel').value,
                 note: f('log-note').value, outcome: checked ? checked.value : 'none',
                 next_step: f('log-next').value.trim(), due: f('log-due').value };
    if (!body.channel) { fieldError(f('log-channel'), f('log-channel-err'), 'Choose how the contact was made from the list.'); return; }
    if (body.next_step && !body.due) {
      fieldError(f('log-due'), f('log-due-err'), 'Give the next step a due date, or leave the next step empty to close the thread.');
      return;
    }
    if (body.due && !body.next_step) { fieldError(f('log-next'), f('log-next-err'), 'Give a next step for that date, or clear the date.'); return; }
    var sendBtn = logForm.querySelector('button[type="submit"]');
    sendBtn.disabled = true;
    post('/touch/log', body).then(function (r) {
      sendBtn.disabled = false;
      if (!r.ok) {
        var map = { date: 'log-date', channel: 'log-channel', next_step: 'log-next', due: 'log-due', outcome: 'log-outcome' };
        var id = map[r.field];
        if (id === 'log-outcome') { fieldError(null, f('log-outcome-err'), r.note); f('log-out-none').focus(); return; }
        if (id) { fieldError(f(id), f(id + '-err'), r.note); return; }
        fieldError(null, f('log-err'), r.note || 'The contact did not save. Try again.');
        return;
      }
      var how = f('log-channel').selectedOptions[0].textContent.toLowerCase();
      var text = 'Contact logged: ' + (/^[aeiou]/.test(how) ? 'an ' : 'a ') + how + ' on ' + dayWords(body.date) + '. '
        + (body.next_step ? 'The next step is now ' + body.next_step.replace(/\.$/, '') + ', due '
                              + dayWords(body.due) + '.'
           : (r.thread && (r.thread.state === 'closed' || r.thread.state === 'none'))
             ? 'No next step, so the thread is closed.'
             : 'It was before the newest contact, so the thread still follows that one.');
      if (r.added) text += ' ' + (r.name || state.name) + ' joined your Watchlist.';
      go(null, recNote(text), 'record');
    });
  });

  // What came back from a past contact: Change, the one thing a past contact can change.
  document.addEventListener('click', function (e) {
    var open = e.target.closest('[data-out-open]');
    if (open) {
      var box = document.getElementById('out-' + open.getAttribute('data-out-open'));
      var show = box.hidden;
      box.hidden = !show;
      open.setAttribute('aria-expanded', show ? 'true' : 'false');
      if (show) box.querySelector('select').focus();
      return;
    }
    var cancel = e.target.closest('[data-out-cancel]');
    if (cancel) {
      var tid = cancel.getAttribute('data-out-cancel');
      document.getElementById('out-' + tid).hidden = true;
      var opener = document.querySelector('[data-out-open="' + tid + '"]');
      opener.setAttribute('aria-expanded', 'false');
      opener.focus();
    }
  });
  document.querySelectorAll('form.outform').forEach(function (of) {
    of.addEventListener('submit', function (e) {
      e.preventDefault();
      var tid = parseInt(of.getAttribute('data-touch'), 10);
      var sel = of.querySelector('select');
      var words = sel.selectedOptions[0].textContent;
      post('/touch/outcome', { id: tid, outcome: sel.value }).then(function (r) {
        if (!r.ok) { fieldError(sel, document.getElementById('out-err-' + tid), r.note || 'It did not save.'); return; }
        go(null, recNote('What came back from the ' + of.getAttribute('data-what') + ' is now ' + words + '.'), 'record');
      });
    });
  });

  // People: added by hand beside the published chief executive and CTO.
  var personBtn = document.getElementById('person-btn');
  var personForm = document.getElementById('person-form');
  toggle(personBtn, personForm, document.getElementById('person-name'));
  var personCancel = document.getElementById('person-cancel');
  if (personCancel) personCancel.addEventListener('click', function () {
    personForm.hidden = true; personBtn.setAttribute('aria-expanded', 'false'); clearErrors(personForm); personBtn.focus();
  });
  if (personForm) personForm.addEventListener('submit', function (e) {
    e.preventDefault();
    clearErrors(personForm);
    var n = document.getElementById('person-name'), t = document.getElementById('person-title');
    if (!n.value.trim()) { fieldError(n, document.getElementById('person-name-err'), "Give the person's name."); return; }
    if (!t.value.trim()) { fieldError(t, document.getElementById('person-title-err'), "Give the person's title, for example Head of Quality."); return; }
    post('/watchlist/person', { company_id: cid, name: n.value.trim(), title: t.value.trim() }).then(function (r) {
      if (!r.ok) {
        var which = r.field === 'title' ? t : (r.field === 'name' ? n : null);
        fieldError(which, document.getElementById(which ? which.id + '-err' : 'person-err'), r.note || 'It did not save.');
        return;
      }
      go(null, recNote('is added to the people at ' + state.name + ', as ' + r.title + '.',
        { name: r.name, undo: { url: '/watchlist/person/remove', body: { id: r.id }, done: 'is no longer listed.' } }), null);
    });
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-person-remove]');
    if (!b) return;
    var pid = parseInt(b.getAttribute('data-person-remove'), 10);
    var who = b.getAttribute('data-name') || 'The person';
    b.disabled = true;
    post('/watchlist/person/remove', { id: pid }).then(function (r) {
      if (!r.ok) { b.disabled = false; refusal(r.note, recErr); return; }
      go(null, recNote('is removed from the people at ' + state.name + '.',
        { name: who, undo: { url: '/watchlist/person/remove', body: { id: pid, undo: true }, done: 'is back.' } }));
    });
  });

  // Read again: the cost first, then a press of the confirm button spends it.
  var rrBtn = document.getElementById('reread-btn');
  var rrBox = document.getElementById('reread-confirm');
  var rrGo = document.getElementById('reread-go');
  var rrStatus = document.getElementById('reread-status');
  if (rrBtn && rrBox) {
    rrBtn.addEventListener('click', function () {
      var open = rrBox.hidden;
      rrBox.hidden = !open;
      rrBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (!open) return;
      var words = document.getElementById('reread-words');
      if (words) {
        // The cost was not drawn in the page: ask for it. This POST never
        // spends anything; only confirm does.
        post('/watchlist/reread', { company_id: cid }).then(function (r) {
          if (r && r.words) {
            words.textContent = r.words;
            words.removeAttribute('id');
            rrGo.disabled = false;
            say('Read again costs ' + r.words + '.');
            rrGo.focus();
          } else {
            rrStatus.hidden = false;
            rrStatus.textContent = (r && r.note) || 'The cost could not be worked out. Reload the page.';
            say(rrStatus.textContent);
          }
        });
      } else {
        rrGo.focus();
      }
    });
    document.getElementById('reread-cancel').addEventListener('click', function () {
      rrBox.hidden = true; rrBtn.setAttribute('aria-expanded', 'false'); rrBtn.focus();
    });
    rrGo.addEventListener('click', function () {
      rrGo.disabled = true;
      rrStatus.hidden = false;
      rrStatus.textContent = 'Reading the site and asking the model. This can take a minute.';
      say(rrStatus.textContent);
      post('/watchlist/reread', { company_id: cid, confirm: true }).then(function (r) {
        if (r && r.ok) {
          var cost = (typeof r.cents === 'number') ? ' It cost ' + r.cents + ' cents.' : '';
          go(null, recNote('The facts were read again' + (r.status ? ' (' + r.status + ')' : '') + '.' + cost), 'record');
          return;
        }
        rrGo.disabled = false;
        rrStatus.textContent = (r && r.note) || 'The facts could not be read again.';
        say(rrStatus.textContent);
      });
    });
  }
})();
"""
