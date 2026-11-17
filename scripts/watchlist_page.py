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


def arrival_tag(arrival: dict | None) -> str:
    if not arrival:
        return ""
    where = "Insights" if arrival.get("via") == "insights" else "a suggestion"
    return (f'<span class="ntag">{ICON_STAR}<span>Added from {where} at '
            f'{esc(wl.hm(arrival.get("at")))}</span></span>')


def last_contact_words(th: dict) -> str:
    if not th.get("last_at"):
        return ""
    return f"Last contact {wl.day_words(th['last_at'])}, {channel_words(th.get('last_channel')).lower()}"


def signal_kick(sig: dict | None) -> str:
    if not sig:
        return ""
    return (f'<span class="sig-type">{esc(sig["type_words"])} · <span class="rel">{int(sig["relevance"] or 0)}'
            f'</span> · found {esc(wl.day_words(sig["found"]))}</span>')


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

def tally_html(model: dict) -> str:
    c = model["counts"]
    sep = '<span class="sep" aria-hidden="true">·</span>'
    parts = [f'<b>{esc(plural(c["watched"], "company", "companies"))}</b> on your Watchlist']
    if c["watched"]:
        bits = []
        if c["today"]:
            bits.append(f'{c["today"]} today')
        if c["overdue"]:
            bits.append(f'{c["overdue"]} overdue')
        due = f'<b>{esc(plural(c["due"], "thread"))} due</b>'
        if bits:
            due += f' ({esc(", ".join(bits))})'
        parts.append(due)
    parts.append(f'<b>{esc(plural(c["waiting"], "suggestion"))}</b> waiting')
    now = wl.uk(wl.moment_of(model.get("now")))
    parts.append(f'As of {now:%H:%M}, {now:%A} {now.day} {wl.MONTHS[now.month - 1]}. Times are UK time.')
    return sep.join(parts)


def addname_html() -> str:
    return (
        '<form class="addname" id="add-form" role="search" aria-label="Add a company by name" novalidate>'
        '<label for="add-name">Add a company by name <span class="meta keyhint" style="font-weight:400">'
        '<kbd class="kbd">/</kbd></span></label>'
        '<div class="row"><div class="combo">'
        '<input class="field" id="add-name" type="text" role="combobox" aria-autocomplete="list" '
        'aria-expanded="false" aria-controls="add-list" aria-describedby="add-hint" autocomplete="off" '
        'maxlength="120" aria-keyshortcuts="/" placeholder="For example, Okara Diagnostics" value="">'
        '<ul class="listbox" id="add-list" role="listbox" aria-label="Companies Radar has seen" hidden></ul>'
        '</div><button type="submit" class="btn btn-secondary">Add</button></div>'
        '<p class="hint" id="add-hint">Radar looks the name up among the companies it has seen. '
        'One it has not seen can still be added.</p>'
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

def _suggestion_sentence(s: dict) -> str:
    bits = []
    lead = s.get("summary") or s.get("headline") or ""
    if s.get("is_advert"):
        if lead:
            bits.append(sentence(f"Advertised: {lead}"))
    elif lead:
        bits.append(sentence(lead))
        if s.get("relevance") is not None:
            bits.append(f"Relevance {int(s['relevance'])}.")
    last = s.get("last_contact")
    if last:
        by = CHANNEL_BY.get(str(last.get("channel") or ""), "")
        bits.append(f"You contacted them on {wl.day_words(last.get('touched_at'))}"
                    + (f", {by}." if by else "."))
    return " ".join(bits)


def _source_line(s: dict) -> str:
    found = wl.day_words(s.get("found"))
    what = "Read from a job advert" if s.get("is_advert") else "An Insights story"
    link = (f' · <a href="{esc(s["href"])}">{esc(s["link_words"])}</a>' if s.get("href") else "")
    return f'<p class="meta src">{esc(what)}{", found " + esc(found) if found else ""}{link}</p>'


def suggestion_card(s: dict) -> str:
    sid = int(s["id"])
    name = esc(s["name"])
    came = '<span class="stag">Dismissed before</span>' if s.get("came_back") else ""
    where = f' <span class="where">{esc(s["place"])}</span>' if s.get("place") else ""
    data = (f'data-id="{sid}" data-name="{name}" data-kind="{esc(s.get("kind") or "")}" '
            f'data-type="{esc((s.get("type_words") or s.get("kind_words") or "").lower())}"')
    return (
        f'<li class="sg" aria-labelledby="sg-{sid}"><div>'
        f'<p class="kick"><span class="type">{esc(s.get("type_words") or s.get("kind_words"))}</span>'
        f'<span>Suggested by Radar</span>{came}</p>'
        f'<h3 id="sg-{sid}">{name}{where}</h3>'
        f'<p>{esc(_suggestion_sentence(s))}</p>{_source_line(s)}</div>'
        f'<div class="acts"><button type="button" class="btn btn-secondary" data-sg="accept" {data}>'
        f'Add to Watchlist<span class="visually-hidden">: {name}</span></button>'
        f'<button type="button" class="btn btn-ghost" data-sg="dismiss" {data}>'
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


def _name_list(names: list) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def suggestions_strip(model: dict) -> str:
    waiting = model.get("suggestions") or []
    dismissed = model.get("dismissed") or []
    if not waiting and not dismissed:
        return ""
    if waiting:
        lead = (f'<b>{esc(plural(len(waiting), "suggestion"))} waiting</b> from Radar: '
                f'{esc(_name_list([s["name"] for s in waiting]))}. None joins your Watchlist until you add it.')
        extra = f" the {plural(len(waiting), 'suggestion')}"
    else:
        lead = (f'<b>No suggestions waiting</b> from Radar. {esc(plural(len(dismissed), "company", "companies"))} '
                f'dismissed in the last {wl.DISMISSED_DAYS} days.')
        extra = " the suggestions and what was dismissed"
    return (f'<div class="sg-strip" role="region" aria-label="Suggestions from Radar"><p>{lead}</p>'
            f'<button type="button" class="btn btn-secondary" id="sg-show" aria-expanded="false" '
            f'aria-controls="sg-panel"><span class="sg-word">Show</span>'
            f'<span class="visually-hidden">{esc(extra)}</span></button></div>')


def suggestions_section(model: dict, folded: bool) -> str:
    waiting = model.get("suggestions") or []
    dismissed = model.get("dismissed") or []
    if not waiting and not dismissed and folded:
        return ""
    count = (f"{len(waiting)} waiting. None is on your Watchlist until you add it." if waiting
             else "None waiting.")
    ran = wl.hm(model.get("watcher_ran")) if model.get("watcher_ran") else ""
    note = (f"From buying windows (a first quality or software hire at a company you have contacted), "
            f"funding rounds at {wl.FUNDING_BAR} or more and Insights stories at {wl.SUGGEST_BAR} or more, "
            f"for companies you do not watch. "
            f"Checked after each watcher run" + (f", the last at {ran}." if ran else "."))
    body = (f'<ul class="sg-list">{"".join(suggestion_card(s) for s in waiting)}</ul>' if waiting
            else '<p class="sec-note">Radar has nothing to suggest just now. It looks again after the next '
                 'watcher run.</p>')
    cls = "suggest after is-folded" if folded else "suggest after"
    hide = ('<button type="button" class="btn btn-ghost sg-hide" id="sg-hide" aria-controls="sg-panel">'
            'Hide the suggestions</button>') if folded else ""
    return (f'<section class="{cls}" id="sg-panel" aria-labelledby="sg-h">'
            f'<div class="sec-head"><h2 id="sg-h" tabindex="-1">Suggested by Radar</h2>'
            f'<span class="count">{esc(count)}</span>{hide}</div>'
            f'<p class="sec-note">{esc(note)}</p>{body}{dismissed_html(model)}</section>')


# ================================================================ the table

def _row_cells(r: dict, selected_id) -> str:
    rid = int(r["id"])
    th = r["thread"]
    sel = rid == selected_id
    cur = ' aria-current="true"' if sel else ""
    place = f'<span class="co-where">{esc(r["place"])}</span>' if r.get("place") else ""
    arr = arrival_tag(r.get("arrival"))
    arr = f'<span class="arr">{arr}</span>' if arr else ""
    step = f'<span class="next">{esc(th["next_step"])}</span>' if th.get("next_step") else ""
    lc = last_contact_words(th)
    lc_inline = f'<span class="meta lc-inline">{esc(lc)}</span>' if lc else ""
    if th.get("last_at"):
        last = (f'{esc(wl.day_words(th["last_at"]))}'
                f'<span class="co-where">{esc(channel_words(th.get("last_channel")))}</span>')
    else:
        last = '<span class="meta">None yet</span>'
    sig = r.get("latest")
    sig_html = (f'{signal_kick(sig)}<span class="sig-h">{esc(sig["headline"])}</span>' if sig
                else '<span class="meta">None yet</span>')
    roles = str(int(r["roles"])) if r.get("roles") else '<span class="meta">None</span>'
    return (
        f'<tr{" class=" + chr(34) + "is-selected" + chr(34) if sel else ""} data-row="{rid}">'
        f'<th scope="row"><a class="co" data-row-link="{rid}" href="/watchlist?company={rid}"{cur}>'
        f'{esc(r["name"])}</a>{place}{arr}</th>'
        f'<td>{tag_html(th)}{step}{lc_inline}</td>'
        f'<td class="c-wide">{last}</td>'
        f'<td>{sig_html}</td>'
        f'<td class="c-wide num">{roles}</td>'
        f'<td class="c-wide"><span class="stag">{esc(r["state_words"])}</span></td></tr>')


def _plist_row(r: dict, selected_id) -> str:
    rid = int(r["id"])
    th = r["thread"]
    cur = ' aria-current="true"' if rid == selected_id else ""
    where = f'<span class="where">{esc(r["place"])}</span>' if r.get("place") else ""
    step = f'<span>{esc(th["next_step"])}</span>' if th.get("next_step") else ""
    lc = last_contact_words(th)
    sig = r.get("latest")
    sig_line = (f'<span class="sg-line">{esc(sig["type_words"])} · {int(sig["relevance"] or 0)} · found '
                f'{esc(wl.day_words(sig["found"]))}</span>' if sig else "")
    return (f'<li><a class="prow" href="/watchlist/{rid}"{cur}><span class="nm">{esc(r["name"])}{where}</span>'
            f'{arrival_tag(r.get("arrival"))}<span class="th">{tag_html(th)}</span>{step}'
            f'{"<span class=" + chr(34) + "sg-line" + chr(34) + ">" + esc(lc) + "</span>" if lc else ""}'
            f'{sig_line}</a></li>')


KEYS_HTML = (
    '<div class="keys" role="group" aria-label="Keys">'
    '<span><kbd class="kbd">J</kbd> <kbd class="kbd">K</kbd> next, previous</span>'
    '<span><kbd class="kbd">Enter</kbd> open the record</span>'
    '<span><kbd class="kbd">Esc</kbd> back to the row</span>'
    '<span><kbd class="kbd">L</kbd> log a contact</span>'
    '<span><kbd class="kbd">/</kbd> add by name</span>'
    '<button type="button" class="switch" id="keys-switch" role="switch" aria-checked="true">'
    'Single-key shortcuts: <span class="sw-word">On</span><span class="knob" aria-hidden="true"></span>'
    '</button></div>')


def table_html(model: dict) -> str:
    rows = model["rows"]
    selected_id = (model.get("selected") or {}).get("id")
    count = f'{len(rows)} watched'
    order = ("Due first: overdue, then today. Then the next due dates, then threads with no date, then "
             "companies with no next step, newest signal first. " + run_words(model))
    head = (f'<div class="sec-head"><h2 id="wt-h">Your companies</h2><span class="count">{esc(count)}</span></div>')
    if not rows:
        return (f'<section aria-labelledby="wt-h">{head}<div class="empty" style="margin-top:12px">'
                '<h3>Your Watchlist is empty</h3><p>Choose the companies you want to follow. Their signals, '
                'stories and roles gather here, with every contact you log and your next step.</p><ol>'
                '<li><span>Add one by name in the field above.</span></li>'
                '<li><span>Add a suggestion below. Radar suggests, you choose.</span></li>'
                '<li><span>Press Add to Watchlist on any Insights story. It saves straight away.</span></li>'
                '</ol></div></section>')
    body = "".join(_row_cells(r, selected_id) for r in rows)
    plist = "".join(_plist_row(r, selected_id) for r in rows)
    return (
        f'<section aria-labelledby="wt-h">{head}'
        f'<table class="wt" aria-labelledby="wt-h"><caption>{esc(order)}</caption>'
        '<thead><tr><th scope="col" class="w-co">Company</th><th scope="col" class="w-th">Thread</th>'
        '<th scope="col" class="c-wide w-lc">Last contact</th>'
        '<th scope="col" class="w-sig">Latest signal or story</th>'
        '<th scope="col" class="c-wide w-rl num">Roles on Jobs</th>'
        '<th scope="col" class="c-wide w-st">State</th></tr></thead>'
        f'<tbody>{body}</tbody></table>{KEYS_HTML}'
        '<p class="sec-note plist-note">Due first: overdue, then today, then the next due dates, then the rest, '
        'newest signal first. Each opens its record.</p>'
        f'<ul class="plist">{plist}</ul></section>')


# ================================================================ the record

def _h(level: int, ident: str, text: str, count: str = "") -> str:
    tail = f' <span class="count">{esc(count)}</span>' if count else ""
    return f'<h{level} class="rb-h" id="{ident}">{esc(text)}{tail}</h{level}>'


def _watched_line(rec: dict) -> str:
    if not rec.get("watched"):
        return ("Not on your Watchlist. Add it here, or log a contact below and it joins your Watchlist.")
    since = wl.day_words(rec.get("watched_at"))
    via = rec.get("via_words") or ""
    line = f"On your Watchlist since {since}" + (f", added {via}" if via else "")
    return line + "."


def _thread_from(rec: dict, th: dict) -> str:
    state = th["state"]
    name = rec["name"]
    set_day = wl.day_words(th.get("set_at") or th.get("last_at"))
    noun = CHANNEL_NOUN.get(str(th.get("last_channel") or ""), "the contact")
    counts = rec.get("watched")
    if state == "none":
        return (f"Nothing is logged with {name} yet. Log a contact below when you first reach out, "
                "with a next step and its date.")
    if state == "closed":
        what = noun.replace('the ', '', 1)
        lead = (f"The last contact, on {set_day}," if what == "contact"
                else f"The last contact, {what} on {set_day},")
        return (f"{lead} was logged without a next step, "
                "so the thread closed. Log a contact with a next step to open it again.")
    base = f"The next step you set on {set_day}, when you logged {noun}."
    off = "" if counts else " It does not count on Home while the company is off your Watchlist."
    if state == "nodate":
        return (base + " It was set before a next step needed a date, so it never falls due. "
                "Set a date to have it count." + off)
    if state == "upcoming":
        when = wl.day_words(th.get("due_date"), weekday=True)
        return (base + f" Nothing is due today: it joins threads due on Home on {when}." if counts
                else base + off)
    if state == "today":
        return (base + " It counts in threads due on Home today, until you log a contact, move the date "
                "or clear the step." if counts else base + off)
    return (base + " It counts in threads due on Home until you log a contact, move the date or clear the "
            "step." if counts else base + off)


def thread_block(rec: dict, lv: int) -> str:
    th = rec["thread"]
    state = th["state"]
    open_ = state in ("overdue", "today", "upcoming", "nodate")
    box_cls = {"overdue": "is-overdue", "today": "", "upcoming": "is-later"}.get(state, "is-quiet")
    if state in ("closed", "none"):
        step = '<p class="step" style="font-weight:500;color:var(--text-2)">Nothing waits on you.</p>'
    else:
        step = f'<p class="step">{esc(th["next_step"])}</p>'
    primary = "btn-primary" if th.get("due") else "btn-secondary"
    acts = [f'<a class="btn {primary}" href="#log" data-log-focus aria-keyshortcuts="L">Log a contact '
            '<kbd class="kbd">L</kbd></a>']
    if open_:
        move_words = "Set a date" if state == "nodate" else "Move the date"
        acts.append(f'<button type="button" class="btn btn-secondary" id="move-btn" aria-expanded="false" '
                    f'aria-controls="move-form">{move_words}</button>')
        acts.append('<button type="button" class="btn btn-ghost" id="clear-btn">Clear the next step</button>')
    d = rec.get("drafts")
    if d and open_:
        which = "connection note" if d.get("note") else "comment"
        acts.append(f'<a class="btn btn-ghost" href="#drafts">The {which} draft is below</a>')
    move = ""
    if open_:
        move = (
            '<form class="inline-form" id="move-form" hidden novalidate>'
            '<div class="fld"><label for="move-due">New due date</label>'
            f'<input class="field" type="date" id="move-due" name="due" value="{esc(th.get("due_date") or "")}" '
            'aria-describedby="move-hint" data-hint="move-hint">'
            '<span class="hint" id="move-hint">The same next step, on a new date.</span>'
            f'<p class="err" id="move-err" hidden>{ICON_ERR}<span></span></p></div>'
            '<div class="acts"><button type="submit" class="btn btn-secondary">Save the date</button>'
            '<button type="button" class="btn btn-ghost" id="move-cancel">Cancel</button></div></form>')
    return (
        f'<section class="rblock" aria-labelledby="th-h">{_h(lv, "th-h", "Thread")}'
        '<p class="lede">Your next step with the company and when it is due, from the newest contact.</p>'
        f'<div class="thread-box {box_cls}">{tag_html(th, long=True)}{step}'
        f'<p class="meta from">{esc(_thread_from(rec, th))}</p>'
        f'<div class="acts">{"".join(acts)}</div>{move}</div></section>')


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
            f'<p class="meta">Written {esc(written)}, for the story {story}.</p>'
            f'<p class="text" id="t-{ident}">{esc(text)}</p>'
            f'<div class="draft-foot"><button type="button" class="btn btn-secondary" data-copy="t-{ident}">'
            f'{ICON_COPY}{ICON_TICK}<span class="lbl">Copy the {label.lower()}</span></button>'
            f'<span class="meta">{len(text)} characters</span>'
            f'<span class="err copy-err" hidden>{ICON_ERR}<span>The browser would not copy. Select the text '
            'and copy it by hand.</span></span></div></div>')
    return (f'<section class="rblock drafts" id="drafts" aria-labelledby="dr-h">{_h(lv, "dr-h", "Drafts to copy")}'
            '<p class="lede">Copy one and make it yours where you post it. Nothing is ever posted or sent by '
            f'Radar. The drafts stay on the story on Insights too.</p>{"".join(parts)}</section>')


def _fld(ident: str, label: str, control: str, hint: str = "", full: bool = False) -> str:
    hint_html = f'<span class="hint" id="{ident}-h">{esc(hint)}</span>' if hint else ""
    return (f'<div class="fld{" full" if full else ""}"><label for="{ident}">{esc(label)}</label>{control}'
            f'{hint_html}<p class="err" id="{ident}-err" hidden>{ICON_ERR}<span></span></p></div>')


def log_block(rec: dict, model: dict, lv: int) -> str:
    today = rec.get("today") or _today(model).isoformat()
    name = rec["name"]
    channels = "".join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in model.get("channels") or wl.CHANNELS)
    outcomes = model.get("outcomes") or wl.OUTCOME_WORDS
    radios = "".join(
        f'<label><input type="radio" name="outcome" id="log-out-{esc(k)}" value="{esc(k)}"'
        f'{" checked" if k == "none" else ""}>{esc(v)}</label>' for k, v in outcomes.items())
    lede = f"One contact with {name}, by you. It joins the contacts below, and its next step becomes the thread."
    if not rec.get("watched"):
        lede += f" Logging it adds {name} to your Watchlist."
    return (
        f'<section class="rblock" id="log" aria-labelledby="log-h">{_h(lv, "log-h", "Log a contact")}'
        f'<p class="lede">{esc(lede)}</p>'
        '<form class="logform" id="log-form" aria-labelledby="log-h" novalidate>'
        + _fld("log-date", "Date",
               f'<input class="field" type="date" id="log-date" name="date" value="{esc(today)}" max="{esc(today)}" '
               'aria-describedby="log-date-h" data-hint="log-date-h">', "Today unless you say otherwise.")
        + _fld("log-channel", "How",
               f'<select class="field" id="log-channel" name="channel"><option value="">Choose one</option>'
               f'{channels}</select>')
        + _fld("log-note", "Note, optional",
               f'<textarea class="field" id="log-note" name="note" maxlength="{wl.NOTE_MAX}"></textarea>',
               full=True)
        + '<fieldset class="full" id="log-outcome"><legend>What came back</legend>'
          f'<div class="radios">{radios}</div>'
          '<p class="hint">You can change it later, when a reply arrives.</p>'
          f'<p class="err" id="log-outcome-err" hidden>{ICON_ERR}<span></span></p></fieldset>'
        + _fld("log-next", "Next step",
               f'<input class="field" type="text" id="log-next" name="next_step" maxlength="{wl.STEP_MAX}" '
               'placeholder="Follow up in a week" aria-describedby="log-next-h" data-hint="log-next-h">',
               "Leave it empty and the thread closes.")
        + _fld("log-due", "Due date",
               '<input class="field" type="date" id="log-due" name="due" aria-describedby="log-due-h" '
               'data-hint="log-due-h">', "Needed with a next step. Today and earlier count as due on Home.")
        + '<div class="full submit"><button type="submit" class="btn btn-primary">Log the contact</button>'
          '<span class="meta">You can change what came back later, from the contact\'s row.</span>'
          f'<p class="err" id="log-err" hidden>{ICON_ERR}<span></span></p></div>'
        '</form></section>')


def contacts_block(rec: dict, lv: int) -> str:
    contacts = rec.get("contacts") or []
    if not contacts:
        return (f'<section class="rblock" aria-labelledby="ct-h">{_h(lv, "ct-h", "Contacts", "None logged yet")}'
                '<p class="lede">Each contact you log appears here, newest first, with what came back.</p></section>')
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
            f'<p class="out">Outcome: <b style="color:var(--text-1);font-weight:600">'
            f'{esc(outcomes.get(out, out.capitalize()))}</b>'
            f'<button type="button" class="btn btn-ghost" style="padding:0 8px" data-out-open="{tid}" '
            f'aria-expanded="false" aria-controls="out-{tid}">Change<span class="visually-hidden"> what came back '
            f'from the {esc(what)}</span></button></p>'
            f'<form class="inline-form outform" id="out-{tid}" data-touch="{tid}" data-what="{esc(what)}" hidden>'
            f'<div class="fld"><label for="out-sel-{tid}">What came back</label>'
            f'<select class="field" id="out-sel-{tid}" name="outcome">{opts}</select></div>'
            '<div class="acts"><button type="submit" class="btn btn-secondary">Save</button>'
            f'<button type="button" class="btn btn-ghost" data-out-cancel="{tid}">Cancel</button></div>'
            f'<p class="err" id="out-err-{tid}" hidden>{ICON_ERR}<span></span></p></form></div></li>')
    return (f'<section class="rblock" aria-labelledby="ct-h">'
            f'{_h(lv, "ct-h", "Contacts", plural(len(contacts), "logged", "logged") + ", newest first")}'
            f'<ul class="contacts" style="margin-top:4px">{"".join(items)}</ul></section>')


def _report_words(n: int) -> str:
    names = {1: "One report", 2: "Two reports", 3: "Three reports", 4: "Four reports", 5: "Five reports"}
    return names.get(n, f"{n} reports")


def signals_block(rec: dict, lv: int) -> str:
    sigs = rec.get("signals") or []
    if not sigs:
        return (f'<section class="rblock" aria-labelledby="sg2-h">{_h(lv, "sg2-h", "Signals and stories")}'
                f'<p class="lede">None yet. Stories and signals that name {esc(rec["name"])} gather here.</p>'
                '</section>')
    drafts_from = (rec.get("drafts") or {}).get("signal_id")
    items = []
    for s in sigs:
        why = []
        if s.get("why"):
            why.append(sentence(s["why"]))
        if s.get("is_advert"):
            why.append("It came from a job advert, so it opens on Jobs: hiring is not shown on Insights.")
        else:
            why.append(f"{_report_words(int(s.get('reports') or 1))}, on Insights.")
        if s.get("from_here"):
            why.append("You added the company from this story.")
        if drafts_from and s["id"] == drafts_from:
            why.append("The drafts above are from it.")
        items.append(
            f'<li><p class="kick"><b>{esc(s["type_words"])}</b><span>Relevance {int(s["relevance"] or 0)}</span>'
            f'<span>found {esc(wl.day_words(s["found"]))}</span></p>'
            f'<span class="ttl"><a href="{esc(s["href"])}">{esc(s["headline"])}</a></span>'
            f'<p class="why">{esc(" ".join(why))}</p></li>')
    return (f'<section class="rblock" aria-labelledby="sg2-h">'
            f'{_h(lv, "sg2-h", "Signals and stories", str(len(sigs)) + ", newest first")}'
            f'<ul class="items" style="margin-top:4px">{"".join(items)}</ul></section>')


def roles_block(rec: dict, lv: int) -> str:
    roles = rec.get("roles") or []
    if not roles:
        return (f'<section class="rblock" aria-labelledby="rl-h">{_h(lv, "rl-h", "Roles on Jobs")}'
                '<p class="lede">No open roles on Jobs.</p></section>')
    items = []
    for r in roles:
        kick = f'<b>{esc(r["tier_words"])}</b>'
        if r.get("score") is not None:
            basis = ("full spec" if str(r.get("basis") or "").lower() == radar_common.SCORE_BASIS_SPEC
                     else "from the advert card")
            kick += f'<span>{int(r["score"])}, {basis}</span>'
        bits = [x for x in (str(r.get("engagement") or "").replace("-", " ").capitalize(), r.get("location"),
                            r.get("pay")) if x]
        if r.get("first_seen"):
            bits.append(f"first seen {wl.day_words(r['first_seen'])}")
        line = " · ".join(bits)
        if r.get("window"):
            line += (". " if line else "") + "A buying window."
        items.append(
            f'<li><p class="kick">{kick}</p><span class="ttl"><a href="{esc(r["href"])}">{esc(r["title"])}</a></span>'
            + (f'<p class="why">{esc(line)}</p>' if line else "")
            + f'<p class="meta"><a href="{esc(r["prepare"])}">Prepare application'
              f'<span class="visually-hidden">: {esc(r["title"])}</span></a></p></li>')
    return (f'<section class="rblock" aria-labelledby="rl-h">'
            f'{_h(lv, "rl-h", "Roles on Jobs", str(len(roles)) + " open")}'
            f'<ul class="items" style="margin-top:4px">{"".join(items)}</ul></section>')


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
            f'<button type="button" class="btn btn-ghost" style="padding:0 8px" data-person-remove="{pid}" '
            f'data-name="{esc(p["name"])}">Remove<span class="visually-hidden"> {esc(p["name"])}</span></button></li>')
    if not rows:
        rows.append('<li><span class="v nk">Not known</span></li>')
    return f'<ul class="people">{"".join(rows)}</ul>'


def facts_block(rec: dict, lv: int) -> str:
    rows = []
    for f in rec.get("facts") or []:
        if f.get("value"):
            v = f'<span class="v">{esc(f["value"])}</span>'
            src = _source_words(f.get("source") or "", f.get("at"))
            s = f'<span class="s">{esc(src)}</span>' if src else ""
        else:
            v, s = '<span class="v nk">Not known</span>', ""
        rows.append(f'<div class="fact"><dt>{esc(f["label"])}</dt><dd>{v}{s}</dd></div>')
    rows.append(f'<div class="fact"><dt>People</dt><dd>{people_html(rec)}</dd></div>')
    name = rec["name"]
    site = bd.safe_url(rec.get("site_url"))
    site_html = ""
    if site:
        site_html = (f' The company\'s own site: <a href="{esc(site)}" rel="noopener noreferrer">'
                     f'{esc(site.split("//", 1)[-1].rstrip("/"))}</a>.')
    if rec.get("enriched_at"):
        foot = (f"Read by Radar on {esc(wl.day_words(rec['enriched_at']))}.{site_html} A fact no source stated "
                "reads Not known. Only a published chief executive and chief technology officer are kept from "
                "what Radar reads; add anyone else by hand.")
    else:
        foot = (f"Radar has not read the facts for {esc(name)} yet. It reads them once a story or an advert "
                f"names the company, or when you press Read again.{site_html}")
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
        '<div class="reread">'
        '<button type="button" class="btn btn-secondary" id="reread-btn" aria-expanded="false" '
        'aria-controls="reread-confirm">Read again</button>'
        '<div class="reread-confirm" id="reread-confirm" hidden>'
        f'<p>Read again fetches the company\'s own site once, politely and as its robots file allows, and makes '
        f'one model call: {cost}. What it finds replaces the facts above, each with its source and date.</p>'
        '<div class="acts"><button type="button" class="btn btn-primary" id="reread-go"'
        f'{"" if words else " disabled"}>Read again now<span class="visually-hidden">, {esc(words)}</span></button>'
        '<button type="button" class="btn btn-ghost" id="reread-cancel">Cancel</button></div>'
        '<p class="meta" id="reread-status" hidden></p></div></div>')
    return (
        f'<section class="rblock" aria-labelledby="fa-h">{_h(lv, "fa-h", "Facts as published")}'
        f'<dl class="facts">{"".join(rows)}</dl>'
        '<div class="acts person-acts"><button type="button" class="btn btn-ghost" id="person-btn" '
        'aria-expanded="false" aria-controls="person-form">Add a person by hand</button></div>'
        f'{person_form}<p class="meta facts-foot">{foot}</p>{reread}</section>')


def record_head(rec: dict, model: dict, view: str) -> str:
    rid = int(rec["id"])
    name = esc(rec["name"])
    rows = {r["id"]: r for r in model.get("rows") or []}
    if view == "record":
        top = (f'<div class="rec-top"><a class="back" href="/watchlist">{ICON_BACK}Back to Watchlist</a></div>'
               f'<h1 class="rec-h" id="rec-h" tabindex="-1">{name}</h1>')
    else:
        pos = model.get("position")
        if pos:
            where = f'Record {pos["index"]} of {pos["total"]}'
            walk = []
            for key, label, k in (("prev", "Previous", "K"), ("next", "Next", "J")):
                other = pos.get(key)
                if other is not None:
                    oname = rows.get(other, {}).get("name", "")
                    walk.append(f'<a class="btn btn-secondary" data-focus-record href="/watchlist?company={int(other)}" '
                                f'aria-label="{label} company, {esc(oname)}" aria-keyshortcuts="{k}">{label} '
                                f'<kbd class="kbd">{k}</kbd></a>')
                else:
                    walk.append(f'<button type="button" class="btn btn-secondary" disabled>{label} '
                                f'<kbd class="kbd">{k}</kbd></button>')
        else:
            where = "Not on your Watchlist"
            walk = []
        walk.append(f'<a class="btn btn-ghost" data-close-record href="/watchlist" aria-label="Close the record" '
                    'aria-keyshortcuts="Escape">Close <kbd class="kbd">Esc</kbd></a>')
        top = (f'<div class="rec-top"><p class="meta">{esc(where)}</p><div class="rec-walk">{"".join(walk)}</div></div>'
               f'<h2 class="rec-h" id="rec-h" tabindex="-1">{name}</h2>')
    stage = ""
    for f in rec.get("facts") or []:
        if f["label"] == "Stage" and f.get("value"):
            stage = f["value"]
    line = " · ".join(x for x in (stage, rec.get("place") or "Place not known") if x)
    story = rec.get("from_story")
    watched = esc(_watched_line(rec))
    if rec.get("watched") and story and rec.get("via") in ("insights", "suggestion"):
        watched = watched[:-1] + (f', from the story <a href="/insights/story/{int(story["id"])}">'
                                  f'{esc(story["headline"])}</a>.')
    current = rec.get("stored_state") or ""
    items = "".join(
        f'<li role="none"><button type="button" role="menuitemradio" tabindex="-1" data-state="{esc(k)}" '
        f'aria-checked="{"true" if k == current else "false"}"><span class="chk">{ICON_CHECK}</span>{esc(v)}'
        '</button></li>' for k, v in (model.get("set_states") or wl.SET_STATES))
    menu = ('<div class="menu-wrap"><button type="button" class="btn btn-secondary" id="state-btn" '
            'aria-haspopup="menu" aria-expanded="false" aria-controls="state-menu">Set state</button>'
            f'<ul class="menu" id="state-menu" role="menu" aria-labelledby="state-btn" hidden>{items}</ul></div>')
    if rec.get("watched"):
        watch_btn = '<button type="button" class="btn btn-ghost" id="remove-btn">Remove from Watchlist</button>'
    else:
        watch_btn = '<button type="button" class="btn btn-secondary" id="watch-btn">Add to Watchlist</button>'
    return (f'{top}<p class="rec-line"><span>{esc(line)}</span><span class="stag">{esc(rec["state_words"])}</span></p>'
            f'<p class="rec-line"><span>{watched}</span></p>'
            f'<div class="rec-acts">{menu}{watch_btn}</div>'
            f'<div class="status rec-flash" id="rec-flash" tabindex="-1" hidden></div>'
            f'<p class="err" id="rec-err" hidden>{ICON_ERR}<span></span></p>')


def record_html(rec: dict, model: dict, view: str) -> str:
    lv = 2 if view == "record" else 3
    work = (thread_block(rec, lv) + drafts_block(rec, lv) + log_block(rec, model, lv) + contacts_block(rec, lv))
    ref = signals_block(rec, lv) + roles_block(rec, lv) + facts_block(rec, lv)
    head = record_head(rec, model, view)
    if view == "record":
        return (f'<article class="record" aria-labelledby="rec-h">{head}'
                f'<div class="rec-cols"><div class="col">{work}</div><div class="col">{ref}</div></div></article>')
    return (f'<aside class="record" aria-labelledby="rec-h">{head}'
            f'<div class="rec-cols"><div class="col">{work}</div><div class="col">{ref}</div></div></aside>')


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
<style>{page_frame.FRAME_CSS}{WATCHLIST_CSS}</style>
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
        return _document(f"{rec['name']} · Watchlist · MedTech Radar", jobs, body, state)
    missing = ""
    if model.get("missing"):
        missing = ('<div class="status is-warn wl-missing"><span class="what">That company is not on file. '
                   'It may have been merged with another or removed. Your companies are below.</span></div>')
    rows = model.get("rows") or []
    folded = bool(rows)
    layout_cls = "layout has-record" if rec else "layout"
    record = record_html(rec, model, "table") if rec else ""
    strip = suggestions_strip(model) if folded else ""
    body = (
        '<div class="page"><header class="pagehead"><div><h1>Watchlist</h1>'
        f'<p class="tally">{tally_html(model)}</p></div>{addname_html()}</header>'
        f'<div class="status wl-flash" id="wl-flash" tabindex="-1" hidden></div>{missing}'
        f'{arrivals_html(model)}'
        f'<div class="{layout_cls}"><div class="main-col">{strip}{table_html(model)}'
        f'{suggestions_section(model, folded)}</div>{record}</div></div>')
    title = f"{rec['name']} · Watchlist · MedTech Radar" if rec else "Watchlist · MedTech Radar"
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
:root{--gutter:16px}
@media (min-width:768px){:root{--gutter:24px}}
@media (min-width:1920px){:root{--gutter:48px}}
.page{margin:0 auto;padding:0 var(--gutter) 64px;max-width:2560px}
.kbd{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;padding:0 5px;
  border:1px solid var(--border-strong);border-radius:var(--radius-2);background:var(--surface-sunken);
  color:var(--text-2);font:500 12px/16px var(--font-mono)}
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
.btn .kbd{margin-left:2px}
.meta{color:var(--text-3);font-size:13px;line-height:20px}
.muted{color:var(--text-2)}

/* masthead */
.pagehead{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:16px 32px;
  padding:24px 0 16px;border-bottom:1px solid var(--border-subtle)}
.pagehead h1{font:600 20px/28px var(--font-sans)}
.tally{margin-top:4px;color:var(--text-2);font-size:14px;line-height:20px;max-width:90ch}
.tally b{color:var(--text-1);font-weight:600}
.tally .sep{color:var(--text-3);padding:0 6px}
.addname{display:grid;gap:4px;min-width:0;width:min(100%,440px)}
.addname label{font:600 14px/20px var(--font-sans)}
.addname .row{display:flex;gap:8px;min-width:0}
.field{min-width:0;width:100%;min-height:36px;padding:7px 10px;border:1px solid var(--border-strong);border-radius:var(--radius-2);
  background:var(--surface-sunken);color:var(--text-1);font:400 15px/20px var(--font-sans)}
.field::placeholder{color:var(--text-3)}
.field.is-error{border-color:var(--negative)}
@media (pointer:coarse){.field{min-height:44px}}
.hint{color:var(--text-3);font-size:13px;line-height:20px}
.combo{position:relative;flex:1;min-width:0}
.listbox{position:absolute;z-index:15;left:0;right:0;top:100%;margin-top:4px;max-height:320px;overflow:auto;
  border:1px solid var(--border-strong);border-radius:var(--radius-2);background:var(--surface);box-shadow:var(--shadow-overlay)}
.listbox li{display:flex;flex-wrap:wrap;justify-content:space-between;gap:2px 12px;padding:8px 10px;border-bottom:1px solid var(--border-subtle);
  font-size:14px;line-height:20px;cursor:pointer}
.listbox li:last-child{border-bottom:0}
.listbox li:hover{background:var(--row-hover)}
.listbox li[aria-selected="true"]{background:var(--row-selected);box-shadow:inset 3px 0 0 var(--accent)}
.listbox .opt-name{color:var(--text-1);font-weight:600}
#add-status .status{margin-top:8px}
.arrived{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;margin-top:16px;padding:10px 12px;
  border:1px solid var(--accent);border-left-width:4px;border-radius:var(--radius-2);background:var(--accent-tint);font-size:14px;line-height:20px}
.arrived .what{flex:1 1 320px;min-width:0}
.status{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;padding:10px 12px;border:1px solid var(--border-strong);
  border-left-width:4px;border-radius:var(--radius-2);background:var(--surface);font-size:14px;line-height:20px}
.status:focus{outline:none}
.status .what{flex:1 1 260px;min-width:0}
.status.is-ok{border-left-color:var(--positive)}
.status.is-bad{border-color:var(--negative);background:var(--negative-tint)}
.status.is-warn{border-color:var(--caution);background:var(--caution-tint)}
.status svg{flex:none}
.status.is-ok svg{color:var(--positive)}
.status.is-bad svg{color:var(--negative)}
.wl-flash,.wl-missing{margin-top:16px}
.rec-flash{margin-top:12px}

/* layout: the table, and the record beside it from 1024 */
.layout{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;margin-top:24px;align-items:start}
.main-col{min-width:0}
@media (min-width:1024px){
  .layout.has-record{grid-template-columns:minmax(0,1fr) 560px}
  .layout>.record{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto}
}
@media (min-width:1800px){.layout.has-record{grid-template-columns:minmax(0,1fr) clamp(720px,40vw,1000px);gap:32px}}
@media (max-width:1023.98px){.layout>.record{order:-1}}
.sg-strip{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin-bottom:24px;padding:10px 12px;
  border:1px dashed var(--border-strong);border-radius:var(--radius-4);font-size:14px;line-height:20px}
.sg-strip p{flex:1 1 280px;min-width:0;color:var(--text-2)}
.sg-strip b{color:var(--text-1)}
@media (min-width:1920px){.sg-strip{display:none}}
.switch{display:inline-flex;align-items:center;gap:8px;min-height:32px;padding:0 8px;border:1px solid var(--border-strong);
  border-radius:var(--radius-2);background:var(--surface);color:var(--text-1);font:500 13px/20px var(--font-sans);cursor:pointer}
.switch .knob{position:relative;width:28px;height:16px;border-radius:8px;background:var(--accent)}
.switch .knob::after{content:"";position:absolute;top:2px;right:2px;width:12px;height:12px;border-radius:50%;background:var(--on-accent)}
.switch[aria-checked="false"] .knob{background:var(--surface-sunken);box-shadow:inset 0 0 0 1px var(--border-strong)}
.switch[aria-checked="false"] .knob::after{right:auto;left:2px;background:var(--text-3)}
.sec-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px;padding-top:12px;border-top:3px solid var(--text-1)}
.sec-head h2{font:600 18px/24px var(--font-sans)}
.sec-head h2:focus{outline:none}
.sec-head .count{color:var(--text-3);font-size:14px;line-height:20px}
.sec-note{margin-top:4px;color:var(--text-3);font-size:13px;line-height:20px;max-width:100ch}

/* suggestions: kept apart, dashed, never in the table; folded below 1920 */
.suggest{margin-bottom:32px}
.suggest.after{margin-top:40px;margin-bottom:0}
.sg-hide{display:none;margin-left:auto}
@media (max-width:1919.98px){
  .suggest.is-folded:not(.is-open){display:none}
  .suggest.is-folded .sg-hide{display:inline-flex}
}
.sg-list{display:grid;grid-template-columns:minmax(0,1fr);gap:8px;margin-top:12px}
@media (min-width:1920px){.sg-list{grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}}
.sg{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 16px;align-items:start;padding:12px;
  border:1px dashed var(--border-strong);border-radius:var(--radius-4);background:var(--canvas)}
@media (min-width:1920px){.sg{grid-template-columns:minmax(0,1fr)}.sg .acts{margin-top:8px}}
@media (max-width:767.98px){.sg{grid-template-columns:minmax(0,1fr)}.sg .acts{margin-top:8px}.page .kbd,.keyhint{display:none}}
.sg .kick{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;color:var(--text-3);font-size:12px;line-height:16px}
.sg .kick .type{color:var(--text-2);font-weight:600}
.sg h3{margin-top:2px;font:600 16px/24px var(--font-sans);overflow-wrap:anywhere}
.sg h3 .where{font-weight:400;color:var(--text-3);font-size:14px}
.sg p{color:var(--text-2);font-size:14px;line-height:20px}
.sg .src{margin-top:2px}
.sg .acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.dis-head{margin-top:12px}
.dis-head .btn{padding-left:0}
.dis-list li{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;justify-content:space-between;padding:8px 0;
  border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.dis-list li>span{min-width:0;flex:1 1 260px}
.dis-list .d-how{display:block}

/* the watched table: three columns from 1024, six from 1800; a list below 1024 */
.wt{width:100%;margin-top:8px;border-collapse:collapse;table-layout:fixed;font-size:14px;line-height:20px}
.wt caption{caption-side:top;text-align:left;color:var(--text-3);font-size:13px;line-height:20px;padding:4px 0 8px}
.wt thead th{padding:8px;border-bottom:1px solid var(--border-strong);color:var(--text-2);font:600 12px/16px var(--font-sans);text-align:left;vertical-align:bottom}
.wt tbody th{font:inherit;font-weight:400;text-align:left;padding:12px 8px;border-bottom:1px solid var(--border-subtle);vertical-align:top;overflow-wrap:anywhere}
.wt td{padding:12px 8px;border-bottom:1px solid var(--border-subtle);vertical-align:top;overflow-wrap:anywhere}
.wt tr.is-selected>*{background:var(--row-selected)}
.wt tr.is-selected>:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.wt tbody tr:not(.is-selected):hover>*{background:var(--row-hover)}
.wt .co{color:var(--text-1);font-weight:600;text-decoration:none}
.wt .co:hover{text-decoration:underline}
.wt .co-where{display:block;color:var(--text-3);font-size:13px}
.wt .arr{display:block;margin-top:4px}
.wt .next{display:block;margin-top:4px;color:var(--text-1)}
.wt .lc-inline{display:block;margin-top:2px}
.wt .sig-type{display:block;color:var(--text-3);font-size:12px;line-height:16px}
.wt .sig-h{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;color:var(--text-2)}
.wt .rel{font-weight:600;color:var(--text-1)}
.wt .num{text-align:right}
.wt .c-wide{display:none}
.wt .w-co{width:30%}.wt .w-th{width:38%}.wt .w-sig{width:32%}
@media (min-width:1800px){
  .wt .c-wide{display:table-cell}
  .wt .lc-inline{display:none}
  .wt .w-co{width:20%}.wt .w-th{width:27%}.wt .w-lc{width:12%}.wt .w-sig{width:25%}.wt .w-rl{width:7%}.wt .w-st{width:9%}
}
.keys{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;margin-top:12px;color:var(--text-3);font-size:13px;line-height:20px}
.keys>span{display:inline-flex;align-items:center;gap:6px}
.plist,.plist-note{display:none}
@media (max-width:1023.98px){
  .wt,.keys{display:none}
  .plist,.plist-note{display:block}
}

/* tags: a shape and a word, never colour alone */
.ttag,.ntag,.stag{display:inline-flex;align-items:center;gap:5px;padding:1px 7px;border:1px solid;border-radius:var(--radius-2);
  font:600 12px/18px var(--font-sans);white-space:nowrap}
.ttag.is-overdue{color:var(--negative);border-color:var(--negative);background:var(--negative-tint)}
.ttag.is-today{color:var(--caution);border-color:var(--caution);background:var(--caution-tint)}
.ttag.is-later{color:var(--text-1);border-color:var(--border-strong);background:var(--surface)}
.ttag.is-quiet{color:var(--text-3);border-color:var(--border-subtle);background:transparent;font-weight:500}
.ntag{color:var(--accent);border-color:var(--accent);background:var(--accent-tint)}
.stag{color:var(--text-2);border-color:var(--border-subtle);background:var(--surface-sunken);font-weight:500}

/* the record */
.record{min-width:0;padding:20px 24px 24px;border:1px solid var(--border-subtle);border-top:3px solid var(--accent);
  border-radius:var(--radius-2);background:var(--surface);align-self:start}
.rec-top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 16px}
.rec-walk{display:flex;gap:8px;flex-wrap:wrap}
.record .rec-h{margin-top:12px;font:600 24px/32px var(--font-sans);overflow-wrap:anywhere}
.rec-line{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:center;margin-top:4px;color:var(--text-2);font-size:14px;line-height:20px}
.rec-acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.rblock{margin-top:24px;padding-top:16px;border-top:1px solid var(--border-subtle)}
.rblock .rb-h{font:600 16px/24px var(--font-sans)}
.rblock .rb-h .count{font-weight:400;color:var(--text-3);font-size:14px}
.rblock .lede{margin-top:2px;color:var(--text-3);font-size:13px;line-height:20px}
.rec-cols{display:block}
@media (min-width:1800px){
  .layout .rec-cols{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:0 32px}
}
.thread-box{margin-top:12px;padding:12px;border:1px solid var(--caution);border-left-width:4px;border-radius:var(--radius-2);background:var(--canvas)}
.thread-box.is-overdue{border-color:var(--negative)}
.thread-box.is-later,.thread-box.is-quiet{border-color:var(--border-strong)}
.thread-box .step{margin-top:8px;font:600 16px/24px var(--font-sans);overflow-wrap:anywhere}
.thread-box .from{margin-top:4px}
.thread-box .acts,.acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.thread-box .acts{margin-top:12px}
.drafts .draft{margin-top:12px}
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
.fld>.hint{display:block;margin-top:4px}
textarea.field{min-height:72px;resize:vertical}
select.field{padding-right:6px}
input[type=date].field{color-scheme:dark}
.logform fieldset{margin:0;padding:0;border:0;min-width:0}
.logform .radios{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:6px}
.logform .radios label{display:inline-flex;align-items:center;gap:6px;min-height:32px;color:var(--text-1);font-weight:400;font-size:14px}
.logform input[type=radio]{width:16px;height:16px;margin:0;accent-color:var(--accent)}
.logform .hint{margin-top:4px}
.err{display:flex;gap:6px;align-items:flex-start;margin-top:4px;color:var(--negative);font-size:13px;line-height:20px}
.err svg{flex:none;margin-top:3px}
.logform .submit{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
.logform .submit .err{flex-basis:100%}
@media (max-width:767.98px){.logform{grid-template-columns:minmax(0,1fr)}}
.inline-form{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:flex-end;margin-top:12px;padding:12px;
  border:1px solid var(--border-subtle);border-radius:var(--radius-2);background:var(--canvas)}
.inline-form .fld{flex:1 1 180px}
.inline-form>.err{flex-basis:100%}
.contacts li{display:grid;grid-template-columns:72px minmax(0,1fr);gap:2px 12px;padding:12px 0;border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.contacts li:first-child{border-top:0}
.contacts .when{color:var(--text-1);font-weight:600}
.contacts .how{color:var(--text-2);font-weight:600}
.contacts .note{color:var(--text-1);overflow-wrap:anywhere}
.contacts .set{color:var(--text-3)}
.contacts .out{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;color:var(--text-3)}
.items li{padding:12px 0;border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.items li:first-child{border-top:0}
.items .kick{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;color:var(--text-3);font-size:12px;line-height:16px}
.items .kick b{color:var(--text-2);font-weight:600}
.items .ttl{display:block;margin-top:2px;font-weight:600;overflow-wrap:anywhere}
.items .ttl a{color:var(--text-1)}
.items .why{margin-top:2px;color:var(--text-2)}
.facts{margin-top:8px}
.facts .fact{display:grid;grid-template-columns:88px minmax(0,1fr);gap:2px 12px;padding:8px 0;border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.facts .fact:first-child{border-top:0}
.facts dt{color:var(--text-3)}
.facts dd .v{color:var(--text-1);overflow-wrap:anywhere}
.facts dd .v.nk{color:var(--text-3)}
.facts dd .s{display:block;color:var(--text-3);font-size:13px}
.people li+li{margin-top:6px}
.people li.hand{display:flex;flex-wrap:wrap;gap:0 8px;align-items:baseline}
.people li.hand .s{flex-basis:100%;order:2}
.person-acts{margin-top:4px}
.person-acts .btn{padding-left:0}
.facts-foot{margin-top:8px}
.reread{margin-top:12px}
.reread-confirm{margin-top:8px;padding:12px;border:1px solid var(--border-strong);border-radius:var(--radius-2);
  background:var(--canvas);font-size:14px;line-height:20px;color:var(--text-1)}
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

/* the phone list and the record as its own view */
.plist{margin-top:8px}
.plist li{border-bottom:1px solid var(--border-subtle)}
.prow{display:grid;gap:4px;padding:12px 0;color:var(--text-1);text-decoration:none;font-size:14px;line-height:20px;min-height:44px}
.prow:hover{color:var(--text-1)}
.prow[aria-current="true"]{box-shadow:inset 3px 0 0 var(--accent);padding-left:10px;background:var(--row-selected)}
.prow .nm{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline;font:600 16px/24px var(--font-sans)}
.prow .nm .where{font-weight:400;font-size:13px;color:var(--text-3)}
.prow .th{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center}
.prow .ntag{justify-self:start}
.prow .sg-line{color:var(--text-3);font-size:13px;line-height:20px}
.back{display:inline-flex;align-items:center;gap:6px;min-height:44px;color:var(--accent);font-size:15px}
.recpage{max-width:960px;padding-top:8px}
.recpage .wl-flash{margin:8px 0}
@media (max-width:767.98px){.recpage .record{padding:16px;border-left:0;border-right:0;border-radius:0;margin:0 calc(-1 * var(--gutter))}}
.empty{padding:24px;border:1px dashed var(--border-strong);border-radius:var(--radius-4)}
.empty h3{font:600 18px/24px var(--font-sans)}
.empty p{margin-top:4px;color:var(--text-2);font-size:14px;line-height:20px;max-width:70ch}
.empty ol{margin-top:12px;display:grid;gap:8px;counter-reset:e}
.empty ol li{display:grid;grid-template-columns:24px minmax(0,1fr);gap:8px;font-size:14px;line-height:20px;color:var(--text-1)}
.empty ol li::before{counter-increment:e;content:counter(e);display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border:1px solid var(--border-strong);border-radius:50%;color:var(--text-2);font-size:12px}

/* touch: 44 pixels */
@media (pointer:coarse){.btn,.switch,.menu button,.logform .radios label{min-height:44px}}
"""


# ================================================================ script

WATCHLIST_SCRIPT = r"""
(function () {
  var main = document.querySelector('[data-page="watchlist"]');
  if (!main) return;
  var state = {};
  try { state = JSON.parse(main.getAttribute('data-state') || '{}'); } catch (e) { state = {}; }
  var FLASH = 'radar-watchlist-flash', FOCUS = 'radar-watchlist-focus', KEYS = 'radar-watchlist-keys';
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
  function keysOn() { try { return window.localStorage.getItem(KEYS) !== 'off'; } catch (e) { return true; } }
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
      else if (want === 'log') el = document.getElementById('log-date');
      else if (want.indexOf('row:') === 0) {
        var id = want.slice(4);
        el = document.querySelector('[data-row-link="' + id + '"]');
        if (!visible(el)) el = document.querySelector('.plist a[href="/watchlist/' + id + '"]');
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
    if (a.hasAttribute('data-focus-record') || a.hasAttribute('data-row-link')) sset(FOCUS, 'record');
    else if (a.hasAttribute('data-close-record') && state.selected) sset(FOCUS, 'row:' + state.selected);
    else if (a.hasAttribute('data-log-focus')) {
      var first = document.getElementById('log-date');
      if (first) { e.preventDefault(); first.focus(); }
    }
  });

  // ---- single-key shortcuts and their switch
  var sw = document.getElementById('keys-switch');
  function paintSwitch() {
    var on = keysOn();
    document.querySelectorAll('[aria-keyshortcuts], [data-keys-off]').forEach(function (n) {
      var k = n.getAttribute('aria-keyshortcuts') || n.getAttribute('data-keys-off');
      if (k === 'Escape') return;
      if (on) { n.setAttribute('aria-keyshortcuts', k); n.removeAttribute('data-keys-off'); }
      else { n.setAttribute('data-keys-off', k); n.removeAttribute('aria-keyshortcuts'); }
    });
    if (!sw) return;
    sw.setAttribute('aria-checked', on ? 'true' : 'false');
    sw.querySelector('.sw-word').textContent = on ? 'On' : 'Off';
  }
  paintSwitch();
  if (sw) sw.addEventListener('click', function () {
    var on = !keysOn();
    try { window.localStorage.setItem(KEYS, on ? 'on' : 'off'); } catch (e) {}
    paintSwitch();
    say(on ? 'Single-key shortcuts are on.' : 'Single-key shortcuts are off. Every one is still a button or link.');
  });
  function step(dir) {
    var id = null;
    if (state.selected && state.positioned) {
      id = dir > 0 ? state.next : state.prev;
      if (id === null || id === undefined) { say(dir > 0 ? 'This is the last company.' : 'This is the first company.'); return; }
    } else {
      id = dir > 0 ? state.first : state.last;
    }
    if (id === null || id === undefined) return;
    go(recordUrl(id), null, 'record');
  }
  document.addEventListener('keydown', function (e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
    if (typing(e.target)) return;
    if (document.querySelector('.appbar.menu-open')) return;
    var key = e.key || '';
    // Esc and Enter are not single-character keys, so the switch leaves them on.
    if (state.view === 'table' && key === 'Escape' && state.selected) {
      e.preventDefault();
      go('/watchlist', null, 'row:' + state.selected);
      return;
    }
    if (state.view === 'table' && key === 'Enter' && state.selected) {
      var t0 = e.target;
      if (t0 === document.body || t0 === main || (t0 && t0.tagName === 'TR')) {
        var h0 = document.getElementById('rec-h');
        if (h0) { e.preventDefault(); h0.focus(); }
      }
      return;
    }
    if (!keysOn()) return;
    if (key === '/') {
      var add = document.getElementById('add-name');
      if (add) { e.preventDefault(); add.focus(); }
      return;
    }
    if (e.shiftKey) return;
    var k = key.toLowerCase();
    if (k === 'l') {
      var first = document.getElementById('log-date');
      if (first) { e.preventDefault(); first.focus(); }
      return;
    }
    if (state.view !== 'table') return;
    if (k === 'j' || k === 'k') { e.preventDefault(); step(k === 'j' ? 1 : -1); return; }
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

  // ---- suggestions: Show and Hide below 1920, answers, and Dismissed
  var panel = document.getElementById('sg-panel');
  var showBtn = document.getElementById('sg-show');
  function setPanel(open, focusTo) {
    if (!panel) return;
    panel.classList.toggle('is-open', open);
    if (showBtn) {
      showBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      showBtn.querySelector('.sg-word').textContent = open ? 'Hide' : 'Show';
    }
    if (focusTo && focusTo.focus) focusTo.focus();
  }
  if (showBtn) showBtn.addEventListener('click', function () {
    var open = showBtn.getAttribute('aria-expanded') !== 'true';
    setPanel(open, open ? document.getElementById('sg-h') : null);
    if (open) {
      var h = document.getElementById('sg-h');
      if (h && h.scrollIntoView) h.scrollIntoView({ block: 'start' });
    }
  });
  // A suggestion answered with the panel open finds it open again after the reload.
  var sgWant = sget(SG);
  if (sgWant) {
    sset(SG, null);
    setPanel(true, sgWant === 'focus' ? document.getElementById('sg-h') : null);
  }
  var hideBtn = document.getElementById('sg-hide');
  if (hideBtn) hideBtn.addEventListener('click', function () { setPanel(false, showBtn); });
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
      if (panel && panel.classList.contains('is-open')) sset(SG, 'open');
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
