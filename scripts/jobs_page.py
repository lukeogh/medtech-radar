"""The Jobs page: the triage queue, the role and its advert, side by side.

Built from the round-2 Jobs design (design/round-1 branch, design/jobs/round-2
and shared/shared_jobs-notes_1280_v3, 14 Nov 2026), on the shared frame in
page_frame.py. From 1800px the queue, the role and the advert are three
columns; from 1024px the advert is the last section of the role; under 1024px
the list and the role are separate screens, /jobs and /jobs/<id>.

Everything here reads the dict build_dashboard.collect fills, so the page and
its pieces render from one read of the database and nothing on this page ever
writes. The page's own script takes the writes through the guarded POST
routes serve_dashboard.py already has (/ack, /dismiss, /close, /apply and
their undos) and the one it adds (/read), and replaces the parts that change
from the fragments below, so an action never reloads the page.

Nothing is fetched or scored from here. An advert that has not been read says
so and offers Prepare application, which is the page that reads it.

The fragments, all GET and read only:
  /jobs/<id>?part=role   the role pane for one role, as JSON
  /jobs?part=queue       the queue and the counts, as JSON
  /jobs?part=reading     the Reading rows, which load when the group opens
  /jobs?part=stamp       the newest row id and how many open roles came after
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import radar_common
import build_dashboard as bd
import page_frame
import tiers

esc = bd.esc

VIEWS = ("open", "acknowledged", "filtered", "applied")
VIEW_WORDS = {"open": "Open roles", "acknowledged": "Acknowledged",
              "filtered": "Filtered", "applied": "Applied"}
OPEN_TIERS = tiers.OPEN_TIERS
GROUP_WORDS = {**tiers.NAMES, "filtered": "Filtered",
               "acknowledged": "Acknowledged", "applied": "Applied"}
TIER_WORDS = {**tiers.SINGULAR, "filtered": "Filtered"}
GATES = (("sector", "Sector", "S"), ("cv", "CV match", "C"),
         ("location", "Location", "L"), ("rate", "Rate", "R"))
BOARDS = {"linkedin-alert": "LinkedIn", "reed-alert": "Reed",
          "indeed-alert": "Indeed", "cvlibrary-alert": "CV-Library",
          "email-other": "Email alert"}
# An Unknown role says when it will expire once that is this close.
EXPIRY_WARN_DAYS = 7
# How long a role is on screen before it counts as opened. An action, Prepare
# application and Open the advert count at once.
READ_AFTER_MS = 2000
# The four keys the role's verdicts take, and their undo.
ACTIONS = (("ack", "Acknowledge", "a"), ("dismiss", "Dismiss", "d"),
           ("close", "Close role", "c"), ("apply", "Mark applied", "m"))


# ------------------------------------------------------------------ helpers

def role_key(o: dict) -> str:
    return o.get("role_key") or f"row-{o.get('id')}"


def board(source) -> str:
    return BOARDS.get(source or "", source or "Unknown source")


def expiry_days() -> int:
    try:
        import check_jobs
        return int(check_jobs.EXPIRY_DAYS)
    except Exception:  # noqa: BLE001  a constant, not worth a failed page
        return 45


def _parse(iso):
    try:
        return datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        try:
            return datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return None


def _day(iso) -> str:
    moment = _parse(iso)
    return f"{moment.day} {moment:%b}" if moment else ""


def _clean(text) -> str:
    return str(text or "").strip().rstrip(".").strip()


# The location chips. Local, UK, Europe and Elsewhere are where the company
# is (the region enrichment mirrors onto each row); Remote is the advert's own
# location class, as the location filter on the page before had it.
LOCATIONS = (("local", "Local"), ("uk", "UK"), ("europe", "Europe"),
             ("elsewhere", "Elsewhere"), ("remote", "Remote"))
LOCATION_KEYS = tuple(k for k, _w in LOCATIONS)


def parse_loc(raw) -> tuple:
    """The chips an address names, in the chips' own order, unknown ones
    dropped. "uk,remote" and "remote,uk" are the same selection."""
    asked = {p.strip().lower() for p in str(raw or "").split(",") if p.strip()}
    return tuple(k for k in LOCATION_KEYS if k in asked)


def loc_matches(o: dict, loc: tuple) -> bool:
    """A role shows under the chips when its region is one chosen, or Remote
    is chosen and its advert is remote. No chip chosen shows every role."""
    if not loc:
        return True
    region = str(o.get("region") or "").strip().lower()
    remote = str(o.get("location_class") or "").strip().lower() == "remote"
    return region in loc or ("remote" in loc and remote)


def view_query(view: str, loc: tuple = ()) -> str:
    params = []
    if view != "open":
        params.append(f"show={view}")
    if loc:
        params.append("loc=" + ",".join(loc))
    return ("?" + "&".join(params)) if params else ""


def role_url(item_id, view: str, loc: tuple = ()) -> str:
    return f"/jobs/{int(item_id)}{view_query(view, loc)}"


def _sorted(faces: list) -> list:
    """CV match high to low, then the freshest sign of life first."""
    out = sorted(faces, key=radar_common.latest_liveness, reverse=True)
    return sorted(out, key=lambda o: -(o["cv_match"] if o.get("cv_match")
                                       is not None else -1))


# -------------------------------------------------------------------- model

def build_model(data: dict, now: str | None = None, loc: tuple = ()) -> dict:
    """Every list the page draws, from one collect().

    The open roles are one collapse across every tier (one row per role, its
    face the highest-tier advert, build_dashboard.collapse_roles), sorted CV
    match first and then freshness. Acknowledged roles collapse the same way.
    Applied roles are the first stamp on each applied chain, newest first.
    A role is new until any advert on its chain has been read; with read
    tracking not yet migrated every role reads as read. loc is the location
    chips chosen: the lists drawn hold the roles they match, and the counts
    in the header stay those of every role.
    """
    now = now or radar_common.now_iso()
    read_keys = data.get("read_keys")
    # The first sighting of each chain, over every advert on it in any
    # state, so a role that came back after expiring keeps its first date.
    firsts: dict = {}
    for r in (data.get("arrivals") or []) + (data.get("retired_roles") or []):
        seen = r.get("first_seen") or ""
        key = role_key(r)
        if seen and (key not in firsts or seen < firsts[key]):
            firsts[key] = seen

    def faces_of(rows):
        out = []
        for face in bd.collapse_roles(rows or []):
            face = dict(face)
            face["_unread"] = (read_keys is not None
                               and role_key(face) not in read_keys)
            face["_first"] = firsts.get(role_key(face))
            out.append(face)
        return out

    tiers: dict = {t: [] for t in ("top", "question", "reading", "filtered")}
    unscored = []
    for face in faces_of(data.get("opportunities")):
        tiers.get(face.get("tier") or "", unscored).append(face)
    for tier in tiers:
        tiers[tier] = _sorted(tiers[tier])
    acknowledged = _sorted(faces_of(data.get("acknowledged")))
    applied = []
    import applications
    stages = {a["role_ref"]: a for a in data.get("applications") or []}
    for row in data.get("applied") or []:
        face = dict(row)
        face["_unread"] = False
        board_app = stages.get(applications.role_ref(face))
        face["_stage"] = board_app["stage"] if board_app else "applied"
        face["_reason"] = board_app.get("closed_reason") if board_app else None
        applied.append(face)
    filtered_reasons: dict = {}
    for o in tiers["filtered"]:
        reason = o.get("filter_reason") or "filtered"
        filtered_reasons[reason] = filtered_reasons.get(reason, 0) + 1
    loc = tuple(loc or ())
    all_lists = {
        "open": tiers["top"] + tiers["question"] + tiers["reading"],
        "acknowledged": acknowledged, "filtered": tiers["filtered"],
        "applied": applied,
    }
    totals = {"top": len(tiers["top"]), "question": len(tiers["question"]),
              "reading": len(tiers["reading"]), "filtered": len(tiers["filtered"]),
              "acknowledged": len(acknowledged), "applied": len(applied)}
    shown = {t: [o for o in rows if loc_matches(o, loc)] for t, rows in tiers.items()}
    acknowledged_shown = [o for o in acknowledged if loc_matches(o, loc)]
    applied_shown = [o for o in applied if loc_matches(o, loc)]
    model = {
        "now": now, "loc": loc, "tiers": shown, "totals": totals,
        "unscored": unscored,
        "acknowledged": acknowledged_shown, "applied": applied_shown,
        "filtered_reasons": filtered_reasons,
        "all_lists": all_lists,
        "lists": {
            "open": shown["top"] + shown["question"] + shown["reading"],
            "acknowledged": acknowledged_shown,
            "filtered": shown["filtered"],
            "applied": applied_shown,
        },
        "open_total": sum(len(v) for v in tiers.values()) + len(unscored),
        # The Applied count in the header and the Show menu: the board's
        # Preparing to Offer, not Closed.
        "applied_open": _applied_open(data),
        "new": sum(1 for t in OPEN_TIERS for o in tiers[t] if o["_unread"]),
        "stamp": max([int(o.get("id") or 0) for o in
                      (data.get("arrivals") or []) + (data.get("retired_roles") or [])]
                     or [0]),
    }
    return model


def _applied_open(data: dict) -> int:
    import board_page
    return board_page.open_count(data)


def find_role(data: dict, model: dict, item_id: int):
    """(the face of the role a row belongs to, the view it sits in), or
    (None, None) when there is no such row.

    Any row id finds its role, an old advert on the chain included, so a link
    or a bookmark never dead-ends. The face is the one the view's queue shows.
    A role that has left every view, closed, dismissed or expired, is drawn
    from its own chain and read only.
    """
    rows = list(data.get("arrivals") or []) + list(data.get("retired_roles") or [])
    row = next((r for r in rows if r.get("id") == item_id), None)
    if row is None:
        return None, None
    key = role_key(row)
    for view in ("open", "acknowledged", "applied", "filtered"):
        for face in model["all_lists"][view]:
            if role_key(face) == key:
                return face, view
    for face in model["unscored"]:
        if role_key(face) == key:
            return face, "open"
    retired = [r for r in data.get("retired_roles") or [] if role_key(r) == key]
    if retired:
        face = dict(bd.collapse_roles(retired)[0])
        face["_unread"] = False
        return face, "retired"
    face = dict(row)
    face["_unread"] = False
    return face, "open"


def chain_rows(data: dict, face: dict) -> list:
    """Every advert on the role's chain, in any state."""
    rows = list(data.get("arrivals") or []) + list(data.get("retired_roles") or [])
    key = role_key(face)
    return [r for r in rows if role_key(r) == key]


# ---------------------------------------------------------------- the queue

SYMBOLS = """<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
<symbol id="st-pos" viewBox="0 0 16 16"><path d="M3 8.5l3.2 3.2L13 4.8" fill="none" stroke="currentColor" stroke-width="1.8"/></symbol>
<symbol id="st-cau" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 2.4a5.6 5.6 0 0 0 0 11.2z" fill="currentColor"/></symbol>
<symbol id="st-neg" viewBox="0 0 16 16"><path d="M4.2 4.2l7.6 7.6m0-7.6l-7.6 7.6" stroke="currentColor" stroke-width="1.8"/></symbol>
<symbol id="st-neu" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6"/></symbol>
<symbol id="st-ask" viewBox="0 0 16 16"><path d="M5.4 5.9a2.6 2.6 0 1 1 3.9 2.3c-.9.5-1.3 1.1-1.3 2v.4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="8" cy="13.2" r="1.1" fill="currentColor"/></symbol>
<symbol id="ic-doc" viewBox="0 0 16 16"><path d="M4 1.8h5.2L12.2 4.8v9.4H4z" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M9 1.8v3.2h3.2M6 8h4.2M6 10.6h4.2" fill="none" stroke="currentColor" stroke-width="1.3"/></symbol>
<symbol id="ic-launch" viewBox="0 0 16 16"><path d="M7 3H3.6A1.6 1.6 0 0 0 2 4.6v7.8A1.6 1.6 0 0 0 3.6 14h7.8a1.6 1.6 0 0 0 1.6-1.6V9" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M9.5 2H14v4.5M14 2 7.5 8.5" fill="none" stroke="currentColor" stroke-width="1.3"/></symbol>
<symbol id="ic-down" viewBox="0 0 16 16"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5"/></symbol>
<symbol id="ic-right" viewBox="0 0 16 16"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5"/></symbol>
<symbol id="ic-left" viewBox="0 0 16 16"><path d="M10 4l-4 4 4 4" fill="none" stroke="currentColor" stroke-width="1.5"/></symbol>
</svg>"""


def _use(symbol: str, size: int = 12) -> str:
    return (f'<svg width="{size}" height="{size}" aria-hidden="true" '
            f'focusable="false"><use href="#{symbol}"></use></svg>')


# Each gate state's mark: its class, its symbol and its word on the role pane.
# Not stated is neutral in colour and has its own shape, a question mark, so it
# never rests on colour alone and never reads as a pass or a fail.
STATE_MARKS = {radar_common.PASSES: ("mk-pass", "st-pos", "Passes"),
               radar_common.FAILS: ("mk-fail", "st-neg", "Fails"),
               radar_common.NOT_STATED: ("mk-ns", "st-ask", "Not stated"),
               radar_common.NOT_JUDGED: ("mk-nj", "st-neu", "Not judged")}


def lifecycle_tag(o: dict, now: str) -> str:
    """The lifecycle chip for an Unknown role: its age, or when it will
    expire once that is close. A live role carries none."""
    state = radar_common.life_state(o, now)
    if state != "unknown":
        return ""
    newest = _parse(radar_common.latest_liveness(o))
    moment = _parse(now)
    if newest is None or moment is None:
        words = "Unknown"
    else:
        expires = newest + timedelta(days=expiry_days())
        days_left = (expires - moment).days
        if days_left <= EXPIRY_WARN_DAYS:
            words = f"Unknown, expires {expires.day} {expires:%b}"
        else:
            age = max(0, (moment - newest).days)
            words = f"Unknown, {age} {'day' if age == 1 else 'days'}"
    return f'<span class="tag tag-caution">{_use("st-cau")}{esc(words)}</span>'


def spec_tag(o: dict) -> str:
    if str(o.get("score_basis") or "").strip().lower() == radar_common.SCORE_BASIS_SPEC:
        return f'<span class="tag tag-neutral">{_use("ic-doc")}Full job spec</span>'
    return ""


def engagement_word(o: dict) -> str:
    word = str(o.get("engagement_type") or "").strip().lower()
    return word.capitalize() if word in ("contract", "permanent") else ""


def location_text(o: dict) -> str:
    chain = o.get("_chain") or {}
    locations = [str(x) for x in (chain.get("locations") or []) if x]
    if not locations and o.get("location"):
        locations = [str(o["location"])]
    if len(locations) > 3:
        return f"{locations[0]} and {len(locations) - 1} more locations"
    return " / ".join(locations)


def lead_line(o: dict) -> str:
    """The row's one line: the open question on a One question away role,
    about the gate that fails and nothing else, else the scorer's line. A Top
    prospect carries no question; its Rate mark says when the rate is the
    first thing to ask."""
    if o.get("tier") == "question":
        question = radar_common.question_kept(o.get("question_text"),
                                              radar_common.gates_of(o))
        if question:
            return bd.sentence(question)
    return bd.sentence(o.get("one_line_why") or "")


def pane_lead(o: dict) -> str:
    """The role pane's lead line: lead_line, except that code's own question
    on a One question away role is not shown. A question the model wrote and
    that is still stored is."""
    lead = lead_line(o)
    if o.get("tier") == "question":
        own = {bd.sentence(q) for q in radar_common.QUESTIONS.values()}
        if lead in own:
            return ""
    return lead


def pay_shown(o: dict) -> str:
    """The pay text the advert gives, when it states a figure, whatever the
    Rate mark says. Words with no figure, such as competitive, show
    nothing."""
    text = _clean(o.get("salary_rate"))
    return text if re.search(r"[0-9]", text) else ""


def state_tag(o: dict) -> str:
    """The row's state tag, only for an advert the advert checker has
    confirmed closed. Live and unknown carry none; State in the role pane
    says them."""
    if not o.get("closed_at") or o.get("closed_source") != "page_check":
        return ""
    return (f'<span class="tag tag-neutral">Closed '
            f'{esc(_day(o["closed_at"]))}</span>')


def first_seen(o: dict) -> str:
    """The first sighting of the role's repost chain."""
    chain = o.get("_chain") or {}
    return o.get("_first") or chain.get("first") or o.get("first_seen") or ""


def facts_bits(o: dict) -> list:
    """A role's facts, each escaped, in the order the row and the role pane
    say them: company, place, contract or permanent where known, pay only
    when the advert states a figure, and the source only when it is not
    LinkedIn."""
    bits = [esc(o.get("company") or "Unknown company")]
    place = location_text(o)
    if place:
        bits.append(esc(place))
    word = engagement_word(o)
    if word:
        bits.append(word)
    pay = pay_shown(o)
    if pay:
        bits.append(f'<span class="r-rate">{esc(pay)}</span>')
    source = board(o.get("source"))
    if source != BOARDS["linkedin-alert"]:
        bits.append(esc(source))
    return bits


def meta_html(bits: list) -> str:
    """The row's facts (each already escaped), set apart by a thin drawn
    rule with 8px either side (ROW_CSS, owner, 17 Nov), never a typed mark,
    with a comma between them for a screen reader."""
    comma = '<span class="visually-hidden">, </span>'
    return comma.join(f'<span class="r-bit">{b}</span>' for b in bits)


def row_html(o: dict, view: str, current_key: str | None, now: str,
             loc: tuple = ()) -> str:
    """One role in the queue: its title, the link that opens it, on up to two
    lines with New before it, then one line of facts (facts_bits) with the
    Full job spec tag and a Closed tag for an advert the checker confirmed
    closed (owner, 17 Nov). The Applied view's row keeps its own facts and
    its CV match."""
    oid = int(o["id"])
    key = role_key(o)
    current = key == current_key
    unread = bool(o.get("_unread"))
    classes = ["row", "row-queue"]
    if o.get("tier") == "question" and view == "open":
        classes.append("row-question")
    if unread:
        classes.append("is-unread")
    if current:
        classes.append("is-current")
    title = o.get("title") or "Untitled role"
    cv = (f'<span class="r-cv"><span class="visually-hidden">CV match </span>'
          f'{int(o["cv_match"])}%</span>' if o.get("cv_match") is not None
          else '<span class="r-cv"><span class="visually-hidden">CV match </span>'
               '&ndash;</span>')
    if view == "applied":
        when = f"Applied {_day(o.get('applied_at'))}"
        stage = o.get("_stage") or "applied"
        import applications
        where = (applications.STAGE_WORDS[stage] if stage != "closed" else
                 "Closed, " + applications.REASON_WORDS.get(o.get("_reason"), "closed").lower())
        # Where the application stands on the board, once it has moved on.
        meta_bits = [when, o.get("company") or "Unknown company",
                     where if stage != "applied" else None,
                     o.get("salary_rate"), board(o.get("source"))]
        meta = meta_html([esc(b) for b in meta_bits if b])
        return (f'<div class="{" ".join(classes)} row-applied" data-id="{oid}" '
                f'data-key="{esc(key)}">'
                f'<span class="r1"><a class="r-link" id="rt-{oid}" '
                f'href="{role_url(oid, view, loc)}"{" aria-current=" + chr(34) + "true" + chr(34) if current else ""} '
                f'aria-describedby="rm-{oid}"><span class="r-title">{esc(title)}</span></a>{cv}</span>'
                f'<span class="r2"><span class="r-meta" id="rm-{oid}">{meta}</span></span>'
                '</div>')
    meta_bits = facts_bits(o)
    tags = spec_tag(o) + state_tag(o)
    if view == "acknowledged" and o.get("acknowledged_at"):
        tags += f'<span class="tag tag-neutral">Acknowledged {esc(_day(o["acknowledged_at"]))}</span>'
    new = '<span class="r-new">New</span>' if unread else ""
    # The region and the remote flag stay on the row, as they were for the
    # location filter, so the design's location chips can be wired to them.
    region = str(o.get("region") or "").strip().lower()
    remote = "1" if str(o.get("location_class") or "").strip().lower() == "remote" else "0"
    return (
        f'<div class="{" ".join(classes)}" data-id="{oid}" data-key="{esc(key)}" '
        f'data-tier="{esc(o.get("tier") or "")}" data-region="{esc(region)}" '
        f'data-remote="{remote}">'
        f'<span class="r1"><a class="r-link" id="rt-{oid}" href="{role_url(oid, view, loc)}"'
        f'{" aria-current=" + chr(34) + "true" + chr(34) if current else ""} '
        f'aria-describedby="rm-{oid}">{new}'
        f'<span class="r-title">{esc(title)}</span></a></span>'
        f'<span class="r2"><span class="r-meta" id="rm-{oid}">{meta_html(meta_bits)}</span>{tags}</span>'
        '</div>')


def rows_html(faces: list, view: str, current_key, now: str, loc: tuple = ()) -> str:
    """The rows, in the order given."""
    return "".join(row_html(o, view, current_key, now, loc) for o in faces)


def group_html(gid: str, title: str, count, new: int, body: str,
               fold: str | None = None, lazy: bool = False) -> str:
    """One group: a full-width band, pinned while its rows scroll under it,
    with the group's name in a pill, its count and new count, and for a tier
    that folds, a chevron at the end; then its rows. A tier's pill shows and
    hides one line on what puts a role there. fold is None for a group that
    is always open, "open" or "closed" for one that folds."""
    new_html = (f'<span class="group-new" data-new="{new}">{new} new</span>'
                if new else '<span class="group-new" data-new="0"></span>')
    means = ""
    if gid in tiers.OPEN_TIERS:
        pill = tiers.pill_html(gid, title, toggle_id=f"group-{gid}-means")
        means = (f'<p class="group-means" id="group-{gid}-means" hidden>'
                 f'{esc(tiers.MEANINGS[gid])}</p>')
    else:
        pill = tiers.pill_html(gid, title)
    fold_html = ""
    hidden = ""
    if fold is not None:
        expanded = "true" if fold == "open" else "false"
        fold_html = (f'<button type="button" class="fold" aria-expanded="{expanded}" '
                     f'aria-controls="group-{gid}-rows" data-fold="{gid}" '
                     f'aria-label="{esc(title)}, show or hide">{_use("ic-right", 16)}</button>')
        hidden = "" if fold == "open" else " hidden"
    lazy_attr = ' data-lazy="1"' if lazy else ""
    return (f'<section class="group" aria-labelledby="group-{gid}" data-tier="{gid}">'
            f'<div class="group-head"><h2 class="group-title" id="group-{gid}">{pill}'
            f'<span class="group-count">{count}</span>{new_html}</h2>{fold_html}</div>'
            f'{means}<div class="group-rows" id="group-{gid}-rows"{hidden}{lazy_attr}>{body}</div>'
            '</section>')


def queue_inner_html(model: dict, view: str, current_key: str | None,
                     reading_open: bool = False) -> str:
    """Everything inside the queue for one view."""
    now = model["now"]
    tiers = model["tiers"]
    loc = model.get("loc") or ()
    totals = model.get("totals") or {}

    def count(group, shown):
        total = totals.get(group, shown)
        return f"{shown} of {total}" if loc and shown != total else shown

    def rows(faces, v):
        return rows_html(faces, v, current_key, now, loc)

    out = []
    if view == "open":
        if model["open_total"] == 0 and not model["acknowledged"] and not model["applied"]:
            out.append('<p class="empty-note">Nothing scored yet.</p>')
        top = tiers["top"]
        body = (rows(top, view) if top else
                '<p class="empty-note">Nothing to get excited about this week.</p>'
                if not loc else '<p class="empty-note">No Top prospect here.</p>')
        out.append(group_html("top", GROUP_WORDS["top"], count("top", len(top)),
                              sum(o["_unread"] for o in top), body, fold="open"))
        question = tiers["question"]
        body = (rows(question, view) if question else
                '<p class="empty-note">No role is one question away.</p>')
        out.append(group_html("question", GROUP_WORDS["question"],
                              count("question", len(question)),
                              sum(o["_unread"] for o in question), body, fold="open"))
        reading = tiers["reading"]
        if reading or (loc and totals.get("reading")):
            # Reading folds. Its rows are drawn now when it is open, which it
            # is when the selected role is in it, and otherwise load when it
            # is opened, so the page does not carry hundreds of rows unasked.
            show = reading_open or any(role_key(o) == current_key for o in reading)
            body = ((rows(reading, view) if reading else
                     '<p class="empty-note">No Reading role here.</p>') if show else "")
            out.append(group_html("reading", GROUP_WORDS["reading"], count("reading", len(reading)),
                                  sum(o["_unread"] for o in reading), body,
                                  fold="open" if show else "closed",
                                  lazy=not show))
        # Applied is always linked, at zero too, as the way to the view;
        # Acknowledged once there is something behind it.
        links = [("applied", "Applied", model["applied_open"])]
        if model["acknowledged"]:
            links.append(("acknowledged", "Acknowledged",
                          count("acknowledged", len(model["acknowledged"]))))
        for key, label, n in links:
            out.append(f'<p class="view-link"><a class="link" href="/jobs{view_query(key, loc)}">'
                       f'{label} <span class="group-count">{n}</span></a></p>')
        filtered = tiers["filtered"]
        if filtered:
            roll = ", ".join(f"{n} {r}" for r, n in sorted(
                model["filtered_reasons"].items(), key=lambda kv: -kv[1]))
            out.append(f'<p class="filtered">Filtered, {len(filtered)} '
                       f'{"role" if len(filtered) == 1 else "roles"}. {esc(roll)}. '
                       f'<a class="link" href="/jobs{view_query("filtered", loc)}">Show them</a></p>')
        if model["unscored"]:
            n = len(model["unscored"])
            out.append(f'<p class="filtered">{n} {"role" if n == 1 else "roles"} '
                       'not scored yet.</p>')
    else:
        faces = model["lists"][view]
        new = sum(1 for o in faces if o.get("_unread"))
        empty = {"applied": "No applications recorded yet.",
                 "acknowledged": "No acknowledged roles.",
                 "filtered": "No filtered roles."}[view]
        body = (rows(faces, view) if faces else
                f'<p class="empty-note">{empty}</p>')
        out.append(group_html(view, GROUP_WORDS[view], count(view, len(faces)), new, body))
        out.append(f'<p class="view-link"><a class="link" href="/jobs{view_query("open", loc)}">'
                   'Back to the open roles</a></p>')
    return "".join(out)


# ----------------------------------------------------------------- the role

def _quote_span(quote, text: str):
    """(start, end) of a quote in text, word for word, as radar_common
    .find_verbatim finds it (case and runs of white space aside, whole words
    at its ends), or None."""
    wanted = radar_common.flat_text(quote)
    if not wanted or not re.search(r"\w", wanted):
        return None
    pattern = r"\s+".join(re.escape(w) for w in wanted.split(" "))
    if re.match(r"\w", wanted):
        pattern = r"(?<!\w)" + pattern
    if re.search(r"\w\Z", wanted):
        pattern += r"(?!\w)"
    found = re.search(pattern, text, re.IGNORECASE)
    return (found.start(), found.end()) if found else None


def _marked(text: str, start: int, end: int, spans: list, used: set) -> str:
    """text[start:end] escaped, with each quote span that falls in it marked.
    A quote's first piece carries its id, so its evidence link lands on it."""
    out, pos = [], start
    for s, e, qid in spans:
        s2, e2 = max(s, start), min(e, end)
        if s2 >= e2:
            continue
        if s2 > pos:
            out.append(esc(text[pos:s2]))
        ident = "" if qid in used else f' id="{qid}"'
        used.add(qid)
        out.append(f"<mark{ident}>{esc(text[s2:e2])}</mark>")
        pos = e2
    if pos < end:
        out.append(esc(text[pos:end]))
    return "".join(out)


_BULLET = re.compile(r"^[ \t]*(?:[-*•·▪◦‣–]|\d{1,2}[.)])[ \t]+")


def advert_text_html(text: str, spans: list) -> str:
    """The stored description as paragraphs and lists, quotes marked.

    The description is stored as plain lines. A blank line ends a paragraph,
    a line that starts with a bullet or a number is a list item, and every
    other line keeps its own line inside its paragraph. Nothing is invented:
    no headings are guessed.
    """
    used: set = set()
    blocks: list = []
    offset = 0
    for line in text.split("\n"):
        start, end = offset, offset + len(line)
        offset = end + 1
        if line.endswith("\r"):
            end -= 1
            line = line[:-1]
        if not line.strip():
            blocks.append(("gap", None))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            item = (start + bullet.end(), end)
            if blocks and blocks[-1][0] == "ul":
                blocks[-1][1].append(item)
            else:
                blocks.append(("ul", [item]))
        else:
            lead = len(line) - len(line.lstrip())
            item = (start + lead, end)
            if blocks and blocks[-1][0] == "p":
                blocks[-1][1].append(item)
            else:
                blocks.append(("p", [item]))
    out = []
    for kind, items in blocks:
        if kind == "p":
            out.append("<p>" + "<br>".join(_marked(text, s, e, spans, used)
                                           for s, e in items) + "</p>")
        elif kind == "ul":
            out.append("<ul>" + "".join(f"<li>{_marked(text, s, e, spans, used)}</li>"
                                        for s, e in items) + "</ul>")
    return "".join(out)


def _spans_for(reasons: list, text: str, offset: int = 0) -> tuple[list, dict]:
    """The marked spans for the reasons' quotes in one text, and which reason
    each landed for. A quote that overlaps one already marked is not marked
    again, and its reason shows its quote without a link."""
    spans, hit = [], {}
    for i, r in enumerate(reasons, 1):
        if not r.get("quote"):
            continue
        found = _quote_span(r["quote"], text)
        if not found:
            continue
        s, e = found
        if any(s < e2 and s2 < e for s2, e2, _q in spans):
            continue
        spans.append((s + offset, e + offset, f"q-{i}"))
        hit[i] = f"q-{i}"
    spans.sort()
    return spans, hit


# Each gate state's colour on the role pane: passes green, fails red, not
# stated amber (the caution colour), not judged grey. The pane only; the
# row's marks are as they were.
GATE_TONES = {radar_common.PASSES: "is-pos", radar_common.FAILS: "is-neg",
              radar_common.NOT_STATED: "is-cau"}


def score_html(o: dict) -> str:
    """Score, its own section: the tier's pill, then the four gates, the CV
    figure inside the CV match gate. How the score was made stays in the
    database and is not said here (owner, 17 Nov)."""
    tier = o.get("tier")
    if tier:
        reason = (f'<span class="tier-reason">{esc(o["filter_reason"])}</span>'
                  if tier == "filtered" and o.get("filter_reason") else "")
        tier_line = (f'<p class="tier-line">{tiers.pill_html(tier, TIER_WORDS.get(tier), singular=True)}'
                     f'{reason}</p>')
    else:
        tier_line = '<p class="tier-line">Not scored yet.</p>'
    gates = []
    for gate, name, _letter in GATES:
        state = radar_common.gate_state(o, gate)
        _mk, symbol, word = STATE_MARKS[state]
        cls = GATE_TONES.get(state, "is-neu")
        note = o.get(f"gate_{gate}_note") or ""
        if state == radar_common.NOT_STATED:
            # A note an older score wrote may pass or fail it. Code's is said
            # here whatever is stored.
            note = radar_common.RATE_NOT_STATED_NOTE
        status = f'{_use(symbol)}{word}'
        if gate == "cv" and o.get("cv_match") is not None:
            # The figure shares the status's line, set apart by the drawn rule.
            status = (f'<span class="r-bit">{status}</span>'
                      '<span class="visually-hidden">, </span>'
                      f'<span class="r-bit gate-figure">{int(o["cv_match"])}%</span>')
        gates.append(f'<div class="gate gate-{gate}"><span class="gate-name">{name}</span>'
                     f'<span class="gate-status {cls}">{status}</span>'
                     f'<span class="gate-note">{esc(note)}</span></div>')
    return ('<section class="sec score" aria-labelledby="score-heading">'
            '<h3 class="sec-h" id="score-heading">Score</h3>'
            f'{tier_line}<div class="gate-strip">{"".join(gates)}</div>'
            '</section>')


def reasons_section(o: dict, reasons: list, hits: dict, quotes: bool) -> str:
    if not reasons:
        return ""
    items = []
    for i, r in enumerate(reasons, 1):
        quote = ""
        if r.get("quote") and quotes:
            words = f"&ldquo;{esc(r['quote'])}&rdquo;"
            if i in hits:
                quote = (f'<a class="evidence" id="reason-{i}-evidence" '
                         f'href="#{hits[i]}">{words}</a>')
            else:
                quote = f'<span class="evidence-text">{words}</span>'
        items.append(f'<li><span class="reason">{esc(bd.sentence(r["reason"]))}</span>'
                     f'{quote}</li>')
    notes = []
    if not quotes and any(r.get("quote") for r in reasons):
        notes.append('<p class="basis">The quotes are not shown, because the '
                     'stored description has changed since this score.</p>')
    if radar_common.reasons_gates_moved(
            radar_common.reasons_record(o.get("score_reasons"), o.get("scored_at")), o):
        notes.append('<p class="basis">The gates have been judged again in code '
                     'since these reasons were given.</p>')
    return ('<section class="sec" aria-labelledby="reasons-heading">'
            '<h3 class="sec-h" id="reasons-heading">Reasons</h3>'
            f'<ul class="reasons">{"".join(items)}</ul>{"".join(notes)}</section>')


def asks_section(o: dict, view: str) -> str:
    """The asks summary from the per-ask working of the score the role holds,
    and the way to see them. Read from the full job spec only, and not shown
    at all until there are asks (owner, 17 Nov)."""
    import prepare_application as prep
    working = prep.load_working(o)
    on_description = prep.scored_on_description(o)
    oid = int(o["id"])
    link = (f'<a class="link" id="asks-in-prepare" href="/prepare/{oid}">'
            'See them in Prepare application</a>')
    if working is not None and on_description:
        counts = working.get("counts") or {}
        met, partly, missing = (int(counts.get(k) or 0)
                                for k in ("met", "partly", "missing"))
        if not met + partly + missing:
            return ""
        text = f'{met} met, {partly} partly, {missing} missing. '
        body = f"<p>{text}{link}</p>"
    elif working is not None and (o.get("description_text") or "").strip():
        body = ('<p>The stored job spec has changed since this score, so its '
                f'asks are not counted here. {link}</p>')
    else:
        return ""
    return ('<section class="sec" aria-labelledby="asks-heading">'
            f'<h3 class="sec-h" id="asks-heading">Asks</h3>{body}</section>')


def _state_words(o: dict, view: str, now: str) -> str:
    if view == "applied" or o.get("applied_at"):
        return f"Applied {_day(o.get('applied_at'))}"
    if o.get("closed_at"):
        how = "the page check" if o.get("closed_source") == "page_check" else "hand"
        return f"Closed {_day(o['closed_at'])} by {how}"
    if o.get("dismissed_at"):
        return f"Dismissed {_day(o['dismissed_at'])}"
    if o.get("expired_at"):
        return f"Expired {_day(o['expired_at'])}"
    state = radar_common.life_state(o, now)
    newest = _day(radar_common.latest_liveness(o))
    if state == "live":
        words = f"Live, seen {newest}"
    else:
        words = f"Unknown, last seen {newest}" if newest else "Unknown"
        moment, latest = _parse(now), _parse(radar_common.latest_liveness(o))
        if moment and latest:
            expires = latest + timedelta(days=expiry_days())
            words += f", expires {expires.day} {expires:%b}"
    if o.get("acknowledged_at"):
        words += f", acknowledged {_day(o['acknowledged_at'])}"
    return words


def details_section(data: dict, o: dict, view: str, now: str) -> str:
    import prepare_application as prep
    facts = []
    rate = o.get("salary_rate") or "Not stated"
    ir35 = str(o.get("ir35") or "").strip().lower()
    if ir35 in ("inside", "outside") and o.get("salary_rate"):
        rate += f", {ir35} IR35"
    facts.append(("Rate as advertised", esc(rate)))
    working = prep.load_working(o)
    if working is not None and prep.scored_on_description(o):
        quote = working.get("rate_quote")
        source = working.get("rate_quote_from")
        if quote and source != "disagrees":
            how = ("Found by pattern matching in the job spec"
                   if source == "pattern" else "Quoted from the job spec")
            facts.append(("Rate line", f"{how}: &ldquo;{esc(quote)}&rdquo;"))
    facts.append(("State", esc(_state_words(o, view, now))))
    chain = chain_rows(data, o)
    if len(chain) > 1:
        sightings = sum(int(r.get("seen_count") or 1) for r in chain)
        first = min((r.get("first_seen") or "") for r in chain)
        text = (f"Seen {sightings} times across {len(chain)} adverts since "
                f"{_day(first)}. A verdict here covers the whole chain.")
        latest = (o.get("_chain") or {}).get("latest_tier")
        if latest:
            text += f" Latest advert read lower, {latest}."
        facts.append(("Repost chain", esc(text)))
    flags = []
    try:
        flags = [str(f) for f in json.loads(o.get("red_flags") or "[]") if str(f).strip()]
    except (TypeError, ValueError):
        flags = []
    facts.append(("Red flags", esc("; ".join(flags)) if flags else "None"))
    word = engagement_word(o) or "Not stated"
    if o.get("engagement_note"):
        word += f", {o['engagement_note']}"
    facts.append(("Type", esc(word)))
    facts.append(("Source", esc(board(o.get("source")))))
    rows = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in facts)
    return ('<section class="sec" aria-labelledby="details-heading">'
            '<h3 class="sec-h" id="details-heading">Details</h3>'
            f'<dl class="facts">{rows}</dl></section>')


def company_section(data: dict, o: dict) -> str:
    """The way to everything on file for the company, only when the company
    page exists."""
    cid = o.get("company_id")
    if not cid or int(cid) not in (data.get("company_ids") or set()):
        return ""
    name = o.get("company") or "this company"
    bits = []
    insights = sum(1 for s in (data.get("signals") or []) + (data.get("dismissed_signals") or [])
                   if s.get("company_id") == cid)
    if insights:
        bits.append(f"{insights} {'insight' if insights == 1 else 'insights'}")
    touch = (data.get("touch_map") or {}).get(str(o.get("company") or "").lower())
    if touch:
        bits.append(f"last touch {_day(touch.get('touched_at'))}")
    text = ", ".join(bits)
    meta = (f'<p class="company-meta">{esc(text[:1].upper() + text[1:])}</p>'
            if bits else "")
    return ('<section class="sec company" aria-label="Company">'
            f'<p><a class="link" id="company-on-file" href="/company/{int(cid)}">'
            f'Everything on file for {esc(name)}</a></p>{meta}</section>')


def advert_section(o: dict, reasons: list, quotes: bool, view: str,
                   can_prepare: bool) -> tuple[str, dict]:
    """The advert: the stored full description where there is one, marked
    where the reasons quote it; otherwise "Advert not read yet." beside
    Prepare application, the way to read it.
    Returns the section and which reasons' quotes were marked in it."""
    text = (o.get("description_text") or "").strip("\n")
    basis = str(o.get("score_basis") or "").strip().lower()
    oid = int(o["id"])
    if text.strip():
        spans, hits = ([], {})
        if quotes and basis == radar_common.SCORE_BASIS_SPEC:
            spans, hits = _spans_for(reasons, text)
        fetched = o.get("description_fetched_at")
        read = (f"Read from {board(o.get('source'))} on {_day(fetched)}" if fetched
                else "Stored by the page checker, date not recorded")
        body = f'<div class="advert-text">{advert_text_html(text, spans)}</div>'
        head = f'<span class="advert-read">{esc(read)}</span>'
    else:
        # Not read yet: those words beside the way to read it, and no more
        # (owner, 17 Nov). The card's quotes then show without a link.
        hits = {}
        prepare = (f'<a class="btn btn-secondary" data-prepare="{oid}" '
                   f'href="/prepare/{oid}">Prepare application</a>'
                   if can_prepare else "")
        body = (f'<div class="advert-unread">{prepare}'
                '<p>Advert not read yet.</p></div>')
        head = ""
    return ('<section class="advert" aria-labelledby="advert-heading" tabindex="-1" '
            'id="advert">'
            f'<div class="advert-head"><h3 class="sec-h" id="advert-heading">Advert</h3>'
            f'{head}</div><div class="advert-body">{body}</div></section>'), hits


def _btn(kind: str, label: str, key: str, oid: int, primary: bool = False) -> str:
    cls = "btn-primary" if primary else "btn-secondary"
    return (f'<button type="button" class="btn {cls}" data-act="{kind}" '
            f'data-id="{oid}">{label}</button>')


def actions_html(o: dict, view: str) -> str:
    oid = int(o["id"])
    prepare = (f'<a class="btn btn-primary" href="/prepare/{oid}" '
               f'data-prepare="{oid}">Prepare application</a>')
    if view == "applied":
        return ('<div class="actions actions-applied">'
                f'<button type="button" class="btn btn-secondary" data-act="unapply" '
                f'data-id="{oid}">Undo applied</button>'
                f'<p class="applied-note">Applied {esc(_day(o.get("applied_at")))}. '
                'Applied roles stay here and never expire. Undo asks first, then '
                'puts the role back in the open roles.</p></div>')
    if view == "retired":
        return ('<div class="actions"><p class="applied-note">This role is out of '
                'every view: ' + esc(_state_words(o, view, radar_common.now_iso()))
                + '. Its undo is on the <a class="link" href="/archive">archive '
                'page</a>.</p></div>')
    if view == "acknowledged":
        # Prepare application will not touch an acknowledged role, so the
        # way back to the open roles comes first.
        return ('<div class="actions actions-open">'
                + f'<button type="button" class="btn btn-secondary" data-act="unack" '
                  f'data-id="{oid}">Back to open roles</button>'
                + _btn("dismiss", "Dismiss", "d", oid)
                + _btn("close", "Close role", "c", oid) + '</div>')
    return ('<div class="actions actions-open">' + prepare
            + "".join(_btn(kind, label, key, oid) for kind, label, key in ACTIONS)
            + '</div>')


def nav_ids(model: dict, view: str, face: dict) -> tuple:
    """The previous and next role in the view's order, by row id."""
    lst = model["lists"].get(view) or []
    keys = [role_key(o) for o in lst]
    try:
        i = keys.index(role_key(face))
    except ValueError:
        return None, None
    prev_id = int(lst[i - 1]["id"]) if i > 0 else None
    next_id = int(lst[i + 1]["id"]) if i + 1 < len(lst) else None
    return prev_id, next_id


def role_inner_html(data: dict, model: dict, face: dict, view: str) -> str:
    """The role pane for one role: its header with the actions, then the lead
    line, Score, Reasons, Asks, Details, the company and the advert."""
    now = model["now"]
    oid = int(face["id"])
    title = face.get("title") or "Untitled role"
    reasons = radar_common.score_reasons(face.get("score_reasons"), face.get("scored_at"))
    quotes = bd.quotes_hold(face)
    # Prepare application works on open roles only, Filtered ones included.
    open_view = view in ("open", "filtered")
    advert, hits = advert_section(face, reasons, quotes, view,
                                  can_prepare=open_view)
    # The row's facts, said the same way, the company a link to its page
    # where there is one.
    meta = facts_bits(face)
    cid = face.get("company_id")
    if cid and int(cid) in (data.get("company_ids") or set()):
        meta[0] = (f'<a class="link" id="detail-company" href="/company/{int(cid)}">'
                   f'{meta[0]}</a>')
    tags = spec_tag(face)
    if view == "applied":
        tags += (f'<span class="tag tag-positive">{_use("st-pos")}Applied '
                 f'{esc(_day(face.get("applied_at")))}</span>')
    link = bd.safe_url(face.get("source_url"))
    open_advert = (f'<a class="btn btn-ghost open-advert" href="{esc(link)}" '
                   'target="_blank" rel="noopener noreferrer" '
                   f'data-open="{oid}">{_use("ic-launch", 16)}'
                   'Open the advert<span class="visually-hidden"> (opens in a new tab)'
                   '</span></a>'
                   if link else "")
    prev_id, next_id = nav_ids(model, view, face)

    def step(target, label, rel, arrow):
        # An arrow, named in words for a screen reader and on hover.
        if target is None:
            return (f'<span class="btn btn-secondary prevnext is-disabled" role="link" '
                    f'aria-disabled="true" aria-label="{label}" title="{label}">'
                    f'{_use(arrow, 16)}</span>')
        return (f'<a class="btn btn-secondary prevnext" href="{role_url(target, view, model["loc"])}" '
                f'rel="{rel}" data-step="{rel}" data-id="{target}" aria-label="{label}" '
                f'title="{label}">{_use(arrow, 16)}</a>')

    back = (f'<a class="link back" href="/jobs{view_query(view if view != "retired" else "open", model["loc"])}" '
            'data-back>Back to Jobs</a>')
    head = (f'<header class="detail-head">{back}<div class="dh-grid">'
            f'<h2 class="dh-title" id="role-title" tabindex="-1">{esc(title)}</h2>'
            f'<div class="dh-meta"><p class="dh-facts"><span class="dh-facts-in">'
            f'{meta_html(meta)}</span></p>{tags}</div>'
            f'<div class="dh-nav">{open_advert}'
            f'{step(prev_id, "Previous job", "prev", "ic-left")}{step(next_id, "Next job", "next", "ic-right")}'
            f'</div></div>{actions_html(face, view)}</header>')
    lead = pane_lead(face)
    body = [f'<p class="lead-line">{esc(lead)}</p>' if lead else "",
            score_html(face),
            reasons_section(face, reasons, hits, quotes),
            asks_section(face, view),
            details_section(data, face, view, now),
            company_section(data, face)]
    return (f'{head}<div class="role-scroll"><div class="detail-body">'
            f'{"".join(body)}</div>{advert}</div>')


def no_selection_html(view: str, note: str = "") -> str:
    """The role pane with no role chosen: those words and no more, or why the
    address named no role."""
    return f'<div class="no-selection"><p>{esc(note or "No role chosen.")}</p></div>'


# ------------------------------------------------------------------ the page

def role_payload(data: dict, model: dict, item_id: int, view: str) -> dict | None:
    """The role pane as JSON for the page script, or None for no such row."""
    face, where = find_role(data, model, item_id)
    if face is None:
        return None
    # The pane is drawn for the view the role is in, whatever the page shows,
    # so its actions are the ones that role can take.
    shown = where
    return {"id": int(face["id"]), "key": role_key(face),
            "tier": face.get("tier") or "", "view": shown,
            "title": face.get("title") or "Untitled role",
            "unread": bool(face.get("_unread")),
            "url": role_url(face["id"], view, model["loc"]),
            "html": role_inner_html(data, model, face, shown)}


def queue_payload(model: dict, view: str, current_key: str | None,
                  reading_open: bool) -> dict:
    return {"html": queue_inner_html(model, view, current_key, reading_open),
            "stamp": model["stamp"], "new": model["new"]}


def reading_payload(model: dict, current_key: str | None) -> dict:
    return {"html": rows_html(model["tiers"]["reading"], "open", current_key,
                              model["now"], model["loc"])}


def show_menu_html(model: dict, view: str) -> str:
    totals = model["totals"]
    counts = {"open": model["open_total"] - totals["filtered"] - len(model["unscored"]),
              "acknowledged": totals["acknowledged"],
              "filtered": totals["filtered"],
              "applied": model["applied_open"]}
    items = "".join(
        f'<li><a href="/jobs{view_query(v, model["loc"])}"'
        f'{" aria-current=" + chr(34) + "page" + chr(34) if v == view else ""}>'
        f'{VIEW_WORDS[v]} <span class="chip-count"'
        f'{" data-applied-count" if v == "applied" else ""}>{counts[v]}</span></a></li>'
        for v in VIEWS)
    return ('<div class="show-menu">'
            '<button type="button" class="chip chip-facet'
            f'{" is-selected" if view != "open" else ""}" id="show-btn" '
            'aria-expanded="false" aria-controls="show-list">'
            f'Show: {VIEW_WORDS[view]}'
            + (f' <span class="chip-count" data-applied-count>{counts["applied"]}</span>'
               if view == "applied" else "")
            + f'{_use("ic-down", 16)}</button>'
            f'<ul class="show-list" id="show-list" hidden>{items}</ul></div>')


def chips_html(loc: tuple) -> str:
    """The location chips, each a toggle. Choosing none shows every role."""
    return ('<div class="chips" role="group" aria-label="Location">' + "".join(
        f'<button type="button" class="chip{" is-selected" if key in loc else ""}" '
        f'data-loc="{key}" aria-pressed="{"true" if key in loc else "false"}">{word}</button>'
        for key, word in LOCATIONS) + '</div>')


def render(data: dict, config: dict, db_label: str, selected: int | None = None,
           view: str = "open", reading: bool = False, loc: tuple = (),
           layout: str = "board") -> tuple[int, str]:
    """(status, the whole page). 404 when the address names no row. reading
    draws the Reading rows now rather than when the group is opened. layout is
    for the Applied view, board (the default) or list; a role named in the
    address is always shown in the list, beside its role pane."""
    if view not in VIEWS:
        view = "open"
    loc = parse_loc(",".join(loc)) if loc else ()
    if view == "applied" and selected is None and layout != "list":
        return 200, render_board(data, config, loc)
    layout = "list"
    model = build_model(data, loc=loc)
    status = 200
    face, where = (find_role(data, model, selected) if selected is not None
                   else (None, None))
    if selected is not None and face is None:
        status = 404
    shown = where
    current_key = role_key(face) if face is not None else None
    queue = queue_inner_html(model, view, current_key, reading)
    if face is not None:
        role_cls = "role role-detail"
        role = role_inner_html(data, model, face, shown)
        label = 'aria-labelledby="role-title"'
        title = f"{face.get('title') or 'Untitled role'}, Jobs, MedTech Radar"
    else:
        role_cls = "role role-empty"
        note = (f"There is no role {int(selected)}. It may have been removed from "
                "the database." if status == 404 else "")
        role = no_selection_html(view, note)
        label = 'aria-label="Role"'
        title = "Jobs, MedTech Radar"
    state = {"view": view, "id": int(face["id"]) if face is not None else None,
             "key": current_key, "tier": (face or {}).get("tier") or "",
             "unread": bool((face or {}).get("_unread")),
             "title": (face or {}).get("title") or "",
             "readAfter": READ_AFTER_MS, "stamp": model["stamp"], "loc": list(loc),
             "layout": layout}
    state_json = esc(json.dumps(state))
    page = f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" href="{bd.FAVICON}">
<style>{page_frame.FRAME_CSS}{JOBS_CSS}</style>
</head>
<body>
<div class="app" data-page="jobs" data-show="{view}" data-view="{"role" if face is not None else "list"}" data-state="{state_json}">
{SYMBOLS}
{page_frame.appbar_html("jobs", bd.job_verdicts(data), "Jobs")}
<div class="pagehead">
<div class="ph-title"><h1>Jobs</h1></div>
<div class="ph-tools">
<button type="button" class="btn btn-secondary list-toggle" id="list-toggle" aria-expanded="true" aria-controls="queue">Hide the list</button>
<div class="arrivals" role="status"><button type="button" class="btn btn-secondary" id="arrivals" hidden></button></div>
{chips_html(loc)}
{show_menu_html(model, view)}
{layout_switch(view, loc)}
<button type="button" class="btn btn-ghost undo-last" id="undo-last" disabled>Undo last</button>
</div>
</div>
<main class="content" id="main">
<section class="queue" id="queue" aria-label="Roles">{queue}</section>
<section class="{role_cls}" id="role" {label}>{role}</section>
</main>
<p class="toast" id="toast" role="status" aria-live="polite"></p>
<template id="no-selection">{no_selection_html(view)}</template>
</div>
<script>{bd.SCRIPT}{page_frame.FRAME_SCRIPT}{JOBS_SCRIPT}</script>
</body>
</html>"""
    return status, page


def layout_switch(view: str, loc: tuple, layout: str = "list") -> str:
    if view != "applied":
        return ""
    import board_page
    return board_page.layout_switch_html(layout, loc)


def render_board(data: dict, config: dict, loc: tuple = ()) -> str:
    """The Applied view as a Board: the same header, the board for the queue
    and the role, and its own script. See board_page.py."""
    import board_page
    model = build_model(data, loc=loc)
    bmodel = board_page.board_model(data, loc=loc)
    state = {"view": "applied", "layout": "board", "loc": list(loc)}
    state_json = esc(json.dumps(state))
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Applications board, Jobs, MedTech Radar</title>
<link rel="icon" href="{bd.FAVICON}">
<style>{page_frame.FRAME_CSS}{JOBS_CSS}{board_page.BOARD_CSS}</style>
</head>
<body>
<div class="app" data-page="jobs" data-show="applied" data-layout="board" data-state="{state_json}">
{SYMBOLS}{board_page.SYMBOLS}
{page_frame.appbar_html("jobs", bd.job_verdicts(data), "Jobs")}
<div class="pagehead">
<div class="ph-title"><h1>Jobs</h1></div>
<div class="ph-tools">
{chips_html(loc)}
{show_menu_html(model, "applied")}
{board_page.layout_switch_html("board", loc)}
<button type="button" class="btn btn-ghost undo-last" id="undo-last" disabled>Undo last</button>
</div>
</div>
<main class="content content-board" id="main">
<section class="board" id="board" aria-label="Applications board">{board_page.board_inner_html(bmodel)}</section>
</main>
<div class="move-menu" id="move-menu" role="menu" hidden></div>
<p class="toast" id="toast" role="status" aria-live="polite"></p>
</div>
<script>{bd.SCRIPT}{page_frame.FRAME_SCRIPT}{board_page.BOARD_SCRIPT}</script>
</body>
</html>"""


# --------------------------------------------------------------- the styles

# The queue row's own rules, shared with Home, whose role items are this row
# (owner, 17 Nov): ROW_CSS is the row, ROW_NARROW_CSS its narrow queue. Home
# names a container "queue" around each row so the narrow rules reach it.
ROW_CSS = """.row{position:relative;display:grid;grid-template-rows:auto 16px;row-gap:4px;min-height:56px;padding:8px 16px 9px;border-bottom:1px solid var(--border-subtle);background:var(--surface);color:var(--text-1);transition:background-color var(--dur) ease-out}
.row:hover{background:var(--row-hover)}
.row.is-current{background:var(--row-selected)}
.r1,.r2{display:flex;min-width:0}
.r1{align-items:flex-start;gap:8px}
.r-link{display:flex;align-items:baseline;gap:8px;flex:1 1 auto;min-width:0;color:var(--text-1);text-decoration:none}
.r-link::after{content:"";position:absolute;inset:0;z-index:0}
.r-link:focus-visible{outline:none;box-shadow:none}
.r-link:focus-visible::after{outline:2px solid var(--focus);outline-offset:-5px}
.r-link:hover .r-title{text-decoration:underline;text-underline-offset:2px}
.r-new{flex:none;font-size:12px;line-height:16px;font-weight:600;color:var(--text-1)}
/* The title takes the row's width and a second line before it is cut. */
.r-title{flex:1 1 auto;min-width:0;overflow:hidden;white-space:normal;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow-wrap:anywhere;font-weight:500}
.is-unread .r-title{font-weight:600}
.r-cv{flex:none;width:40px;text-align:right;font-weight:500;font-variant-numeric:tabular-nums}
.r2{align-items:center;gap:8px}
/* Positioned, so the screen reader's commas (visually-hidden, absolutely
   placed) are clipped with the line when it is cut, and never widen the page;
   clicks pass through it to the row's link, as they did before it was positioned. */
.r-meta{position:relative;pointer-events:none;flex:0 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:12px;line-height:16px;color:var(--text-3)}
/* A thin rule between two facts, drawn, not typed: the second fact's own
   mark, so none comes before the first or after the last, and the line,
   which never wraps, can only start with its first fact. A border, so it
   keeps a system colour in forced colours. */
.r-bit ~ .r-bit::before{content:"";display:inline-block;width:0;height:12px;margin:0 8px;border-left:1px solid var(--text-3);vertical-align:-1px}
.r2 .tag{margin:-2px 0}
.row-applied{grid-template-rows:auto 16px;row-gap:4px;min-height:56px}
"""

# The row is a title and one line of facts at every width now, so the narrow
# queue needs no rules of its own; Home still includes this, empty.
ROW_NARROW_CSS = ""

JOBS_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{background:var(--surface)}
html body{margin:0;background:var(--surface);color:var(--text-1);font-family:var(--font-sans);font-size:14px;line-height:20px;font-weight:400;letter-spacing:0;-webkit-font-smoothing:antialiased}
h1,h2,h3,h4,p,ul,ol,dl,dd{margin:0}
ul,ol{list-style:none;padding:0}
button,input{font:inherit;color:inherit;letter-spacing:0}
a{color:inherit}
.mono{font-family:var(--font-mono);white-space:nowrap}
.seg-nowrap{white-space:nowrap}
.app{display:grid;grid-template-rows:48px auto minmax(0,1fr);height:100vh;height:100dvh;overflow:hidden;background:var(--surface)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:32px;padding:0 12px;border:1px solid transparent;border-radius:var(--radius-2);font-size:14px;line-height:20px;font-weight:500;white-space:nowrap;text-decoration:none;cursor:pointer;transition:background-color var(--dur) ease-out,color var(--dur) ease-out,border-color var(--dur) ease-out}
.btn-primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn-secondary{background:var(--surface);border-color:var(--border-strong);color:var(--text-1)}
.btn-secondary:hover{background:var(--row-hover)}
.btn-ghost{background:transparent;color:var(--text-2)}
.btn-ghost:hover{color:var(--text-1);text-decoration:underline;text-underline-offset:2px}
.btn.is-disabled,.btn:disabled{color:var(--text-3);border-color:var(--border-subtle);cursor:default;text-decoration:none}
.btn[aria-busy="true"]{cursor:progress}
.prevnext{width:32px;padding:0}
.link{color:var(--accent);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:2px;transition:color var(--dur) ease-out}
.link:hover{color:var(--accent-hover);text-decoration-thickness:2px}
.with-icon{display:inline-flex;align-items:center;gap:8px}
.chip{display:inline-flex;align-items:center;gap:4px;height:32px;padding:0 12px;border:1px solid var(--border-strong);border-radius:var(--radius-2);background:transparent;color:var(--text-2);font-size:12px;line-height:16px;font-weight:600;white-space:nowrap;cursor:pointer}
.chip:hover{color:var(--text-1)}
.chip-count{color:var(--text-3);font-weight:400;font-variant-numeric:tabular-nums}
.chip.is-selected{background:var(--accent-tint);border-color:var(--accent);color:var(--accent)}
.chip-facet{padding-right:8px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip.is-selected:hover{color:var(--accent-hover)}
.tag{display:inline-flex;align-items:center;gap:4px;flex:none;height:20px;padding:0 8px;border-radius:var(--radius-2);font-size:12px;line-height:16px;font-weight:600;white-space:nowrap}
.tag-neutral{background:var(--neutral-tint);color:var(--neutral)}
.tag-caution{background:var(--caution-tint);color:var(--caution)}
.tag-positive{background:var(--positive-tint);color:var(--positive)}
.is-pos{color:var(--positive)}
.is-neg{color:var(--negative)}
.is-neu{color:var(--neutral)}
.pagehead{display:flex;flex-wrap:wrap;align-items:center;gap:8px 24px;min-height:56px;padding:12px 16px;background:var(--canvas);border-bottom:1px solid var(--border-subtle)}
.ph-title{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 12px;min-width:0}
.ph-title h1{font-size:20px;line-height:28px;font-weight:600}
.ph-tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-left:auto}
.arrivals:has(#arrivals[hidden]){display:none}
.show-menu{position:relative}
.show-list{position:absolute;right:0;top:36px;z-index:12;min-width:220px;padding:4px 0;background:var(--surface);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay)}
.show-list a{display:flex;justify-content:space-between;gap:16px;padding:8px 12px;color:var(--text-1);text-decoration:none}
.show-list a:hover{background:var(--row-hover)}
.show-list a[aria-current="page"]{font-weight:600;background:var(--accent-tint)}
.undo-last{margin-left:4px}
.content{display:grid;grid-template-columns:minmax(0,1fr);min-width:0;min-height:0;background:var(--surface)}
.queue,.role{min-width:0;min-height:0;overflow:auto;overscroll-behavior:contain;background:var(--surface)}
.queue{container:queue / inline-size;scroll-padding-top:48px}
.queue .link,.queue .fold,.role .link,.role .evidence,.role .btn{scroll-margin-block:8px}
.empty-note{padding:12px 16px;color:var(--text-2);border-bottom:1px solid var(--border-subtle)}
/* A tier's band: full width, lightly filled, pinned while its rows scroll
   under it; the name in its pill, the count and new count, the chevron last. */
.group-head{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:8px;height:48px;padding:0 16px;background:var(--surface-sunken);border-bottom:1px solid var(--border-subtle)}
.group-title{display:flex;align-items:center;gap:12px;min-width:0;font-size:14px;line-height:20px;font-weight:400;white-space:nowrap}
.group-count{font-size:14px;line-height:20px;color:var(--text-2);font-variant-numeric:tabular-nums}
.group-new{font-size:14px;line-height:20px;color:var(--text-2)}
.group-new:empty{display:none}
.group-new::before{content:"";display:inline-block;width:0;height:12px;margin:0 12px 0 0;border-left:1px solid var(--text-3);vertical-align:-1px}
.group-means{padding:8px 16px 8px 32px;max-width:72ch;color:var(--text-2);border-bottom:1px solid var(--border-subtle)}
.group-rows .row{padding-left:32px}
.fold{display:inline-flex;align-items:center;justify-content:center;flex:none;width:32px;height:32px;margin:0 -8px 0 auto;border:0;border-radius:var(--radius-2);background:transparent;color:var(--text-2);cursor:pointer}
.fold:hover{background:var(--row-hover);color:var(--text-1)}
.fold svg{transition:transform var(--dur) ease-out}
.fold[aria-expanded="true"] svg{transform:rotate(90deg)}
""" + ROW_CSS + tiers.PILL_CSS + """
.view-link{display:flex;align-items:center;min-height:40px;padding:0 16px;border-bottom:1px solid var(--border-subtle);font-weight:600}
.view-link .group-count{margin-left:4px}
.filtered{padding:12px 16px 12px;font-size:12px;line-height:16px;color:var(--text-3)}
.detail-head{position:sticky;top:0;z-index:3;container:dhead / inline-size;padding:12px 16px;background:var(--surface);border-bottom:1px solid var(--border-subtle)}
.back{display:none}
.dh-grid{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"title nav" "meta meta";column-gap:16px}
.dh-title{grid-area:title;align-self:center;font-size:22px;line-height:28px;font-weight:600;text-wrap:balance}
.dh-title:focus{outline:none}
.dh-nav{grid-area:nav;display:flex;align-items:center;gap:8px}
.dh-meta{grid-area:meta;display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;min-height:20px;margin-top:4px}
/* The row's facts beneath the title, set apart by the same drawn rule. They
   may wrap here: every fact carries the rule before it, and the line is
   pulled left by the rule's width and clipped, so no line starts with one. */
.dh-facts{flex:0 1 auto;min-width:0;overflow:hidden;font-size:14px;line-height:20px;color:var(--text-2)}
.dh-facts-in{position:relative;display:flex;flex-wrap:wrap;margin-left:-17px}
.dh-facts .r-bit{min-width:0}
.dh-facts .r-bit::before{content:"";display:inline-block;width:0;height:14px;margin:0 8px;border-left:1px solid var(--text-3);vertical-align:-2px}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px}
.applied-note{flex:1 1 32ch;min-width:0;max-width:80ch;margin-left:4px;color:var(--text-2);text-wrap:pretty}
@container dhead (max-width:759.98px){
  .dh-grid{grid-template-columns:minmax(0,1fr);grid-template-areas:"title" "meta" "nav"}
  .dh-nav{margin-top:8px;flex-wrap:wrap}
  .dh-nav .open-advert{margin:0 auto 0 -12px}
}
@container dhead (max-width:679.98px){
  .actions-open{display:grid;grid-template-columns:repeat(4,max-content)}
  .actions-open .btn-primary{grid-column:1 / -1;justify-self:start}
}
.detail-body{display:flex;flex-direction:column;gap:24px;padding:16px 16px 24px}
/* Sections set apart by a line inset from the pane's edges. */
.sec ~ .sec{padding-top:24px;border-top:1px solid var(--border-subtle)}
.sec-h{margin-bottom:8px;font-size:16px;line-height:24px;font-weight:600}
.lead-line{max-width:80ch;text-wrap:pretty}
.tier-line{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px}
.tier-reason{color:var(--text-2)}
.score{container-type:inline-size}
.gate-strip{display:grid;grid-template-columns:88px 120px minmax(0,1fr);gap:8px 12px;align-items:start;max-width:960px;margin-top:8px}
.gate{display:contents}
.gate-name,.gate-status{font-size:12px;line-height:16px;font-weight:600}
.gate-name{color:var(--text-2)}
.gate-status{display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.gate-status > .r-bit:first-child{display:inline-flex;align-items:center;gap:4px}
.gate-figure{color:var(--text-1);font-variant-numeric:tabular-nums}
.is-cau{color:var(--caution)}
.gate-note{font-size:12px;line-height:16px;color:var(--text-3);text-wrap:pretty}
@container (max-width:479.98px){.gate-strip{grid-template-columns:72px minmax(0,1fr);row-gap:2px}.gate-note{grid-column:2;margin-bottom:8px}}
@container (min-width:720px){.gate-strip{grid-template-columns:repeat(4,minmax(0,1fr));gap:0 24px}.gate{display:flex;flex-direction:column;gap:2px}}
.basis{margin-top:2px;font-size:12px;line-height:16px;color:var(--text-3)}
.reasons{display:flex;flex-direction:column;gap:12px}
.reasons li{display:flex;flex-direction:column;align-items:flex-start;gap:2px;max-width:80ch}
.evidence,.evidence-text{font-size:12px;line-height:16px;color:var(--text-2)}
.evidence{text-decoration:underline;text-decoration-color:var(--accent);text-decoration-thickness:1px;text-underline-offset:2px}
.evidence:hover{text-decoration-thickness:2px}
.facts{display:grid;grid-template-columns:160px minmax(0,1fr);gap:8px 16px;align-items:baseline}
.facts dt{font-size:12px;line-height:16px;font-weight:600;color:var(--text-2)}
.facts dd{max-width:80ch}
.company-meta{margin-top:2px;font-size:12px;line-height:16px;color:var(--text-3)}
.advert{position:relative;background:var(--surface)}
.advert::before{content:"";position:absolute;top:0;left:16px;right:16px;border-top:1px solid var(--border-subtle)}
.advert:focus{outline:none}
.advert-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 12px;padding:24px 16px 0;background:var(--surface)}
.advert-head .sec-h{margin-bottom:0}
.advert-read{font-size:12px;line-height:16px;color:var(--text-3)}
.advert-body{padding:12px 16px 32px}
.advert-text{max-width:72ch;font-size:15px;line-height:24px}
.advert-text p,.advert-text ul{margin-bottom:12px}
.advert-text ul{padding-left:20px;list-style:disc}
.advert-text li{margin-bottom:4px}
.advert-text li::marker{color:var(--text-3)}
mark{background:transparent;color:inherit;text-decoration:underline;text-decoration-color:var(--accent);text-decoration-thickness:1px;text-underline-offset:3px}
mark:target{background:var(--accent-tint);text-decoration-thickness:2px}
.advert-unread{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
.advert-unread p{color:var(--text-2)}
.no-selection{padding:12px 16px 24px}
.no-selection > p{max-width:72ch;color:var(--text-2)}
.toast{position:fixed;left:50%;bottom:16px;z-index:30;transform:translateX(-50%);display:flex;align-items:center;gap:12px;max-width:min(560px,calc(100vw - 32px));padding:8px 8px 8px 16px;background:var(--surface-sunken);border:1px solid var(--border-strong);border-radius:var(--radius-4);box-shadow:var(--shadow-overlay)}
.toast:empty{display:none}
.toast.is-bad{border-color:var(--negative)}
.toast.is-bad::before{content:"!";display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:var(--negative);color:var(--on-accent);font-weight:700;font-size:12px}
@media (max-width:1799.98px){.ph-tools{flex-basis:100%;margin-left:0}}
""" + ROW_NARROW_CSS + """@media (min-width:1024px){
  .content{grid-template-columns:400px minmax(0,1fr)}
  .queue{border-right:1px solid var(--border-subtle)}
}
@media (min-width:768px) and (max-width:1799.98px){
  .role-detail{display:grid;grid-template-rows:auto minmax(0,1fr);overflow:clip}
  .role-scroll{min-width:0;min-height:0;overflow:auto;overscroll-behavior:contain;scroll-padding-block:8px}
}
@media (max-height:499px) and (min-width:768px) and (max-width:1799.98px){
  .role-detail{display:block;overflow:auto}
  .role-scroll{overflow:visible}
}
@media (min-width:1280px){.content{grid-template-columns:440px minmax(0,1fr)}}
/* Hide the list: the role pane takes the full width. */
@media (min-width:1024px){.app.list-off .content{grid-template-columns:minmax(0,1fr)}.app.list-off .queue{display:none}}
@media (min-width:1800px){
  .content{grid-template-columns:clamp(440px,24vw,640px) minmax(min(640px,100vw - 760px - clamp(440px,24vw,640px)),1fr) 760px;grid-template-rows:auto minmax(0,1fr)}
  .queue{grid-column:1;grid-row:1 / span 2}
  .role,.role-scroll{display:contents}
  .detail-head{grid-column:2;grid-row:1;position:relative}
  .detail-body{grid-column:2;grid-row:2;min-height:0;overflow:auto;overscroll-behavior:contain;scroll-padding-block:8px}
  .advert{grid-column:3;grid-row:1 / span 2;min-height:0;overflow:auto;overscroll-behavior:contain;scroll-padding-top:40px;scroll-padding-bottom:8px;border-left:1px solid var(--border-subtle)}
  .advert::before{content:none}
  .advert-head{position:sticky;top:0;z-index:2;align-items:center;height:40px;padding:0 16px;border-bottom:1px solid var(--border-subtle)}
  .advert-body{padding-top:16px}
  .no-selection{grid-column:2 / span 2;grid-row:1 / span 2;overflow:auto}
  .app.list-off .content{grid-template-columns:0 minmax(0,1fr) 760px}
}
@media (max-width:1023.98px){
  .list-toggle{display:none}
  .app[data-view="role"] .queue{display:none}
  .app[data-view="role"] .pagehead{display:none}
  .app[data-view="list"] .role{display:none}
  .back{display:inline-flex;align-items:center;margin-bottom:8px}
  .prevnext{display:none}
}
@media (max-width:767.98px){
  html{scroll-padding-top:44px}
  .app{grid-template-rows:48px auto auto;height:auto;min-height:100vh;overflow:visible}
  .queue,.role{overflow:visible;min-height:auto}
  .detail-head{position:static}
  .undo-last{margin-left:0;padding-inline:0}
  .ph-title h1{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);clip-path:inset(50%);white-space:nowrap}
  .btn,.chip{height:44px}
  .fold{width:44px;height:44px;margin-right:-12px}
  .view-link,.back{min-height:44px}
  .actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
  .actions .btn-primary,.applied-note{grid-column:1 / -1;justify-self:stretch}
  .applied-note{margin-left:0}
  .facts{grid-template-columns:112px minmax(0,1fr)}
  .company .link,.with-icon{display:inline-flex;align-items:center;min-height:44px}
  .evidence{position:relative;display:inline-block;padding:6px 0}
  .show-list{left:0;right:auto}
  .chips{display:contents}
}
@media (pointer:coarse){
  .btn,.chip{height:44px}
  .fold{width:44px;height:44px}
  .queue{scroll-padding-top:48px}
}
@media (max-width:767.98px),(pointer:coarse){.fold:focus-visible{outline-offset:-4px;box-shadow:none}}
@media (max-height:499px){.group-head,.detail-head,.advert-head{position:static}}
@media (forced-colors:active){.row.is-current{outline:2px solid Highlight;outline-offset:-2px}.tag{border:1px solid CanvasText}}
"""


# --------------------------------------------------------------- the script

JOBS_SCRIPT = """
(function () {
  var app = document.querySelector('.app[data-page="jobs"]');
  if (!app) return;
  var state = JSON.parse(app.dataset.state);
  var view = state.view;
  var loc = state.loc || [];
  var LOCS = ['local', 'uk', 'europe', 'elsewhere', 'remote'];
  var qs = '', amp = '';
  // The view and the location chips travel in every address the page makes,
  // so a reload, a role's link and the back button keep both.
  function setQuery() {
    var params = [];
    if (view !== 'open') params.push('show=' + view);
    if (loc.length) params.push('loc=' + loc.join(','));
    if (view === 'applied') params.push('layout=list');
    qs = params.length ? '?' + params.join('&') : '';
    amp = params.length ? '&' + params.join('&') : '';
  }
  setQuery();
  var queue = document.getElementById('queue');
  var role = document.getElementById('role');
  var toast = document.getElementById('toast');
  var undoBtn = document.getElementById('undo-last');
  var arrivals = document.getElementById('arrivals');
  var current = state.id ? { id: state.id, key: state.key, tier: state.tier,
                             unread: state.unread, title: state.title } : null;
  var stamp = state.stamp;
  var seq = 0, readTimer = null, listScroll = 0;
  var INVERSE = { ack: 'unack', dismiss: 'undismiss', close: 'unclose', apply: 'unapply', unack: 'ack' };
  var DONE = { ack: 'Acknowledged', dismiss: 'Dismissed', close: 'Closed', apply: 'Marked applied',
               unack: 'Back in the open roles', undismiss: 'Dismiss undone', unclose: 'Close undone',
               unapply: 'Application undone', move: 'Moved' };
  var narrow = window.matchMedia('(max-width: 1023.98px)');

  function json(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  function post(kind, id) {
    return fetch('/' + kind, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                               body: JSON.stringify({ id: id }), keepalive: kind === 'read' })
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

  // ---- the undo stack, kept for the tab so a reload keeps it
  function stack() {
    try { return JSON.parse(sessionStorage.getItem('radar-jobs-undo') || '[]'); } catch (e) { return []; }
  }
  function setStack(s) {
    try { sessionStorage.setItem('radar-jobs-undo', JSON.stringify(s.slice(-20))); } catch (e) {}
    undoBtn.disabled = !s.length;
    undoBtn.textContent = s.length ? 'Undo last' : 'Undo last';
    undoBtn.setAttribute('aria-label', s.length ? 'Undo last: ' + DONE[s[s.length - 1].kind]
                         + ', ' + s[s.length - 1].title : 'Undo last, nothing to undo');
  }
  setStack(stack());

  // ---- the queue
  function rowFor(key) {
    var rows = queue.querySelectorAll('.row');
    for (var i = 0; i < rows.length; i++) if (rows[i].dataset.key === key) return rows[i];
    return null;
  }
  function markCurrent(key, focus) {
    queue.querySelectorAll('.row').forEach(function (row) {
      var on = !!key && row.dataset.key === key;
      row.classList.toggle('is-current', on);
      var link = row.querySelector('.r-link');
      if (link) { if (on) link.setAttribute('aria-current', 'true'); else link.removeAttribute('aria-current'); }
      if (on) {
        if (focus && link) link.focus({ preventScroll: true });
        if (!narrow.matches) row.scrollIntoView({ block: 'nearest' });
      }
    });
  }
  // ---- the tiers fold, and this browser remembers each one's choice
  var FOLD_KEY = { top: 'radar-jobs-fold-top', question: 'radar-jobs-fold-question',
                   reading: 'radar-reading-open' };
  function foldRows(tier) { return document.getElementById('group-' + tier + '-rows'); }
  function readingRows() { return foldRows('reading'); }
  function setReading(open) { return setFold('reading', open); }
  function setFold(tier, open, remember) {
    var rows = foldRows(tier);
    if (!rows) return Promise.resolve();
    var btn = queue.querySelector('[data-fold="' + tier + '"]');
    if (remember !== false && FOLD_KEY[tier]) {
      try { localStorage.setItem(FOLD_KEY[tier], open ? '1' : '0'); } catch (e) {}
    }
    if (btn) btn.setAttribute('aria-expanded', String(open));
    if (!open) { rows.hidden = true; return Promise.resolve(); }
    var load = Promise.resolve();
    if (rows.dataset.lazy === '1') {
      load = json('/jobs?part=reading' + amp + (current ? '&current=' + current.id : '')).then(function (j) {
        rows.innerHTML = j.html;
        rows.dataset.lazy = '0';
      });
    }
    return load.then(function () {
      rows.hidden = false;
      if (current) markCurrent(current.key, false);
    });
  }
  // The page draws Top prospects and One question away open and Reading
  // closed. A choice this browser remembers wins, except that the tier
  // holding the open role is never closed under it.
  function applyFolds() {
    Object.keys(FOLD_KEY).forEach(function (tier) {
      var rows = foldRows(tier);
      if (!rows) return;
      var saved = null;
      try { saved = localStorage.getItem(FOLD_KEY[tier]); } catch (e) {}
      if (saved === null) return;
      var open = saved === '1';
      if (!open && current && current.tier === tier && view === 'open') return;
      if (open === rows.hidden) setFold(tier, open, false);
    });
  }
  function refreshQueue() {
    var top = queue.scrollTop;
    var open = !!readingRows() && !readingRows().hidden;
    return json('/jobs?part=queue' + amp + '&reading=' + (open ? 1 : 0)
                + (current ? '&current=' + current.id : '')).then(function (j) {
      queue.innerHTML = j.html;
      applyFolds();
      queue.scrollTop = top;
      stamp = j.stamp;
      arrivals.hidden = true;
      if (current) markCurrent(current.key, false);
    });
  }

  // ---- a role opened counts as read after a moment on screen
  function clearNew(info) {
    var row = rowFor(info.key);
    if (row) {
      row.classList.remove('is-unread');
      var n = row.querySelector('.r-new');
      if (n) n.remove();
    }
    var group = queue.querySelector('.group[data-tier="' + (view === 'open' ? info.tier : view) + '"] .group-new');
    if (group) {
      var left = Math.max(0, Number(group.dataset.new || 0) - 1);
      group.dataset.new = left;
      group.textContent = left ? left + ' new' : '';
    }
  }
  function markRead(info) {
    if (!info || !info.unread || info.reading) return;
    info.reading = true;
    post('read', info.id).then(function (j) {
      info.reading = false;
      if (j && j.ok) { info.unread = false; clearNew(info); }
    }, function () { info.reading = false; });
  }
  function startRead(info) {
    clearTimeout(readTimer);
    if (!info || !info.unread) return;
    readTimer = setTimeout(function () {
      if (current && current.id === info.id) markRead(info);
    }, state.readAfter);
  }

  // ---- the role pane
  function showRole(j, opts) {
    role.className = 'role role-detail';
    role.removeAttribute('aria-label');
    role.setAttribute('aria-labelledby', 'role-title');
    role.innerHTML = j.html;
    role.scrollTop = 0;
    var body = role.querySelector('.role-scroll');
    if (body) body.scrollTop = 0;
    var detail = role.querySelector('.detail-body');
    if (detail) detail.scrollTop = 0;
    var adv = role.querySelector('.advert');
    if (adv) adv.scrollTop = 0;
    current = { id: j.id, key: j.key, tier: j.tier, unread: j.unread, title: j.title };
    if (narrow.matches && app.dataset.view !== 'role') listScroll = window.scrollY;
    app.dataset.view = 'role';
    document.title = j.title + ', Jobs, MedTech Radar';
    var row = rowFor(j.key);
    var ready = (!row && view === 'open' && j.tier === 'reading') ? setReading(true) : Promise.resolve();
    ready.then(function () { markCurrent(j.key, !!opts.focusRow && !narrow.matches); });
    if (narrow.matches) {
      // One screen at a time: the role replaces the list, so focus goes to
      // its heading, whatever opened it.
      window.scrollTo(0, 0);
      var h = document.getElementById('role-title');
      if (h) h.focus({ preventScroll: true });
    }
    startRead(current);
  }
  function select(id, opts) {
    opts = opts || {};
    var mine = ++seq;
    return json('/jobs/' + id + '?part=role' + amp).then(function (j) {
      if (mine !== seq) return;
      showRole(j, opts);
      if (opts.push !== false) history.pushState({ id: j.id }, '', j.url);
    }).catch(function () {
      if (mine === seq) location.href = '/jobs/' + id + qs;
    });
  }
  function showList(push) {
    seq++;
    clearTimeout(readTimer);
    var tpl = document.getElementById('no-selection');
    role.className = 'role role-empty';
    role.removeAttribute('aria-labelledby');
    role.setAttribute('aria-label', 'Role');
    role.innerHTML = tpl.innerHTML;
    var was = current;
    current = null;
    app.dataset.view = 'list';
    document.title = 'Jobs, MedTech Radar';
    if (push) history.pushState({}, '', '/jobs' + qs);
    markCurrent(null);
    if (narrow.matches) {
      window.scrollTo(0, listScroll);
      var row = was ? rowFor(was.key) : null;
      var link = row ? row.querySelector('.r-link') : null;
      if (link) link.focus({ preventScroll: true });
    }
  }
  function locFromAddress() {
    var m = location.search.match(/[?&]loc=([^&]*)/);
    var asked = m ? decodeURIComponent(m[1]).split(',') : [];
    return LOCS.filter(function (k) { return asked.indexOf(k) >= 0; });
  }
  function showChips() {
    document.querySelectorAll('[data-loc]').forEach(function (b) {
      var on = loc.indexOf(b.dataset.loc) >= 0;
      b.setAttribute('aria-pressed', String(on));
      b.classList.toggle('is-selected', on);
    });
  }
  function applyLoc(next, push) {
    loc = next;
    setQuery();
    showChips();
    // The Show menu's views keep the chips too.
    document.querySelectorAll('#show-list a').forEach(function (a) {
      var m = a.getAttribute('href').match(/show=([a-z]+)/);
      var params = m ? ['show=' + m[1]] : [];
      if (loc.length) params.push('loc=' + loc.join(','));
      a.setAttribute('href', '/jobs' + (params.length ? '?' + params.join('&') : ''));
    });
    var path = current ? '/jobs/' + current.id : '/jobs';
    if (push) history.pushState(current ? { id: current.id } : {}, '', path + qs + location.hash);
    return refreshQueue().then(function () {
      // The role stays open; its Previous and Next follow the chips.
      if (current) return select(current.id, { push: false });
    });
  }
  document.querySelectorAll('[data-loc]').forEach(function (b) {
    b.addEventListener('click', function () {
      var k = b.dataset.loc;
      var next = loc.indexOf(k) >= 0 ? loc.filter(function (x) { return x !== k; })
                                      : LOCS.filter(function (x) { return x === k || loc.indexOf(x) >= 0; });
      applyLoc(next, true);
    });
  });
  window.addEventListener('popstate', function () {
    var asked = locFromAddress();
    if (asked.join(',') !== loc.join(',')) {
      // Back or forward across a change of chips: the list follows.
      var m0 = location.pathname.match(/^\\/jobs\\/(\\d+)/);
      if (!m0 && current) showList(false);
      applyLoc(asked, false).then(function () {
        if (m0 && (!current || current.id !== Number(m0[1]))) select(Number(m0[1]), { push: false });
      });
      return;
    }
    // A quote's link to the advert, or the skip link, moves only the hash,
    // and the browser reports that as a step in history too. The role on
    // screen is still the one the address names, so nothing is redrawn.
    var m = location.pathname.match(/^\\/jobs\\/(\\d+)/);
    if (m) {
      if (current && current.id === Number(m[1])) return;
      select(Number(m[1]), { push: false });
    } else if (current) {
      showList(false);
    }
  });

  // ---- actions
  function nextAfter() {
    var n = role.querySelector('[data-step="next"]') || role.querySelector('[data-step="prev"]');
    return n ? Number(n.dataset.id) : null;
  }
  function act(kind, id, undoing) {
    // Undo on an application forgets the day it was applied, and marking it
    // applied again records a new day, so it asks first.
    if (kind === 'unapply' && !window.confirm('Undo this application? The day you applied is '
        + 'forgotten, and applying again records a new day.')) return Promise.resolve('cancelled');
    var title = current && current.id === id ? current.title : (undoing && undoing.title) || '';
    var after = nextAfter();
    var buttons = role.querySelectorAll('[data-act]');
    buttons.forEach(function (b) { b.disabled = true; b.setAttribute('aria-busy', 'true'); });
    return post(kind, id).then(function (j) {
      buttons.forEach(function (b) { b.disabled = false; b.removeAttribute('aria-busy'); });
      if (!j || j.ok === false) {
        // The server answered and said no: the role is not in the state
        // that action expects, so trying it again cannot help.
        say((j && j.note) || 'That did not stick. Try again.', true);
        return 'refused';
      }
      if (!undoing && INVERSE[kind]) {
        var s = stack();
        s.push({ kind: kind, id: id, title: title });
        setStack(s);
      }
      say(DONE[kind] + (title ? ': ' + title : '') + '.', false, !undoing && !!INVERSE[kind]);
      return refreshQueue().then(function () {
        if (current && rowFor(current.key) && current.id === id) return select(id, { push: false });
        if (undoing) {
          var back = rowFor(keyOf(id));
          if (back) return select(Number(back.dataset.id), { focusRow: true });
          return select(id);
        }
        var target = after !== null && queue.querySelector('.row[data-id="' + after + '"]') ? after : null;
        if (target === null) {
          var first = queue.querySelector('.row');
          target = first ? Number(first.dataset.id) : null;
        }
        if (target === null) { showList(true); return; }
        return select(target, { focusRow: true, focusTitle: false });
      }).then(function () { return 'ok'; }, function () { return 'ok'; });
    }, function () {
      buttons.forEach(function (b) { b.disabled = false; b.removeAttribute('aria-busy'); });
      say('No usable answer came back, so that may not have saved. Reload to check.', true);
      return 'error';
    });
  }
  function keyOf(id) {
    var row = queue.querySelector('.row[data-id="' + id + '"]');
    return row ? row.dataset.key : '';
  }
  var undoing = false;
  function undoLast() {
    var s = stack();
    if (!s.length || undoing) return;
    undoing = true;
    undoBtn.disabled = true;
    var last = s.pop();
    // A move made on the board is undone here too, by moving it back.
    var undo = last.kind === 'move'
      ? fetch('/application/move', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ id: last.id, to: last.from, reason: last.fromReason || null,
                                     from: last.to, undo: true }) })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (!j || j.ok === false) { say((j && j.note) || 'That did not stick.', true); return 'refused'; }
            say('Moved back \\u201c' + last.title + '\\u201d.');
            return refreshQueue().then(function () { return 'ok'; });
          }, function () { say('No usable answer came back. Reload to check.', true); return 'error'; })
      : act(INVERSE[last.kind], last.id, last);
    undo.then(function (outcome) {
      undoing = false;
      // The entry leaves the stack once its undo has stuck, or once the
      // server has refused it, as it would every time after. A network
      // failure or a cancelled question keeps it, to try again.
      if (outcome === 'ok' || outcome === 'refused') setStack(s);
      else setStack(stack());
    });
  }
  undoBtn.addEventListener('click', undoLast);

  // ---- clicks
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest('a');
    var actBtn = e.target.closest('[data-act]');
    if (actBtn && role.contains(actBtn)) {
      e.preventDefault();
      act(actBtn.dataset.act, Number(actBtn.dataset.id), false);
      return;
    }
    if (a && a.matches('.r-link')) {
      e.preventDefault();
      var row = a.closest('.row');
      select(Number(row.dataset.id), { focusRow: true });
      return;
    }
    if (a && a.matches('[data-step]')) {
      e.preventDefault();
      select(Number(a.dataset.id), { focusRow: true, focusTitle: false });
      return;
    }
    if (a && a.matches('[data-back]')) {
      e.preventDefault();
      showList(true);
      return;
    }
    if (a && (a.matches('[data-prepare]') || a.matches('[data-open]')) && current) {
      markRead(current);
    }
    var fold = e.target.closest('[data-fold]');
    if (fold) {
      setFold(fold.dataset.fold, fold.getAttribute('aria-expanded') !== 'true');
      return;
    }
    // A tier's pill shows and hides its one line on what puts a role there.
    var pill = e.target.closest('[data-pill]');
    if (pill) {
      var on = pill.getAttribute('aria-expanded') !== 'true';
      var means = document.getElementById(pill.getAttribute('aria-controls'));
      pill.setAttribute('aria-expanded', String(on));
      if (means) means.hidden = !on;
    }
  });

  // ---- Hide the list: the role pane takes the full width
  var listBtn = document.getElementById('list-toggle');
  if (listBtn) listBtn.addEventListener('click', function () {
    var off = !app.classList.contains('list-off');
    app.classList.toggle('list-off', off);
    listBtn.setAttribute('aria-expanded', String(!off));
    listBtn.textContent = off ? 'Show the list' : 'Hide the list';
  });

  // ---- the Show menu
  var showBtn = document.getElementById('show-btn');
  var showList_ = document.getElementById('show-list');
  function setShow(open) {
    showList_.hidden = !open;
    showBtn.setAttribute('aria-expanded', String(open));
  }
  showBtn.addEventListener('click', function () { setShow(showList_.hidden); });
  document.addEventListener('click', function (e) {
    if (!showList_.hidden && !e.target.closest('.show-menu')) setShow(false);
  });

  // ---- Escape closes the Show menu; the page has no other keys of its own
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !showList_.hidden) { setShow(false); showBtn.focus(); }
  });
  // ---- new roles: a notice in place of the old fifteen-minute reload
  function checkArrivals() {
    if (document.hidden) return;
    json('/jobs?part=stamp&since=' + stamp).then(function (j) {
      if (j.new > 0) {
        arrivals.textContent = j.new + (j.new === 1 ? ' new role has' : ' new roles have')
          + ' arrived. Load ' + (j.new === 1 ? 'it' : 'them');
        arrivals.hidden = false;
      }
    }).catch(function () {});
  }
  arrivals.addEventListener('click', function () {
    refreshQueue().then(function () {
      say('The list is up to date.');
      // The notice has gone, so focus goes to the top of the list.
      var first = queue.querySelector('.r-link');
      if (first) first.focus();
    });
  });
  setInterval(checkArrivals, 120000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) checkArrivals(); });

  // ---- on load
  applyFolds();
  if (current) {
    history.replaceState({ id: current.id }, '', location.pathname + location.search + location.hash);
    markCurrent(current.key, false);
    startRead(current);
  }
})();
"""
