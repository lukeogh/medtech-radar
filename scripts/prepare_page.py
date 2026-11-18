#!/usr/bin/env python
"""Prepare application, the page: /prepare/<id>.

Built from design/prepare-application/final (design/round-1, e98c241) and the
decisions of 15 Nov in design/README.md, which win wherever they differ from
the frames:

- The page never spends on opening, however it is opened, the Jobs link
  included. Fetching and scoring the full description sit behind their own
  button, which states the cost first. A fetch that fails costs nothing, and a
  description already scored is not scored again (prepare_application).
- The CV sheet is light paper inside the dark page (page_frame .sheet-paper).
- Each ask carries the advert sentence it came from, kept only where it is
  found word for word in the stored description (score_item.normalise_asks);
  an ask whose sentence was not found shows without one.

The layout. A role head; then the rail (Score, Application, Rate line, Gates),
the job description and the work: the tailored CV's head, the bar of its
buttons, the gaps, and the sheet with its margin notes. At 1800px and over the
three sit side by side; from 1024px the rail is a band of two columns and the
description folds; on a phone it is one column, the gates and the description
folded, each note under its line and the bar at the foot.

The editor is cv_page's (the CV builder's). This page puts it inside the paper
sheet and lays its margin notes beside it, finding each line by the editor's
.cvl[data-n], numbered as cv_store.cv_lines. The notes are this page's own
layer: their marks, fold, selection and stale state are worked out by
PREPARE_SCRIPT from the lines on screen, and use none of the editor's ask
attributes, so the two never both act on one element.

What changes. Everything but the editor is drawn by parts(), one HTML string
per slot. A page opened draws them in place; the answer to a press (POST
/prepare here, and Rescore role, Save CV and Start again from master through
the editor) carries them as <template data-part> elements in its html, which
the editor puts into #prepare-body as it always has, and the page script moves
each into its slot. Drawing writes nothing and spends nothing.

The three scores: the card's, the full spec's (the role's own, which the Jobs
row shows) and this copy's. The copy's is stored on the copy and in the score
history marked tailored, and is never written to the opportunities row
(prepare_application._store_tailored); test/run_prepare_test.py holds that.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import applications  # noqa: E402
import build_dashboard as bd  # noqa: E402
import cv_doc  # noqa: E402
import cv_page  # noqa: E402
import cv_store  # noqa: E402
import jobs_page as jp  # noqa: E402
import page_frame  # noqa: E402
import prepare_application as prep  # noqa: E402
import radar_common as rc  # noqa: E402
import tailored_cv  # noqa: E402
import experience  # noqa: E402
import hand_roles  # noqa: E402
import tiers  # noqa: E402

esc = bd.esc

# What one press costs, said before it is pressed. One read of the advert's
# page and one scoring call: about 0.9 cent for the score, about 2 cents with
# the per-ask working and the quoted reasons (docs/cost-note.md), and about
# 0.6 cent more for the advert sentence of each ask, so about 2.6 cents, and
# about 0.25 cent more for the short reason of each partly or missing ask
# (about 150 output tokens for the five or so of a typical advert, and about
# 90 tokens more of prompt), so about 2.9 cents, still about 3. The reply is
# capped at score_item.SPEC_MAX_TOKENS, 10,240, about 15 cents.
COST = "About 3 cents, 1 call"
BOARD_URL = "/jobs?show=applied"

GATE_ROWS = (("sector", "S", "Sector"), ("cv", "C", "CV match"),
             ("location", "L", "Location"), ("rate", "R", "Rate"))
MARKS = {"met": ("Met", "met"), "partly": ("Partly", "partly"),
         "missing": ("Gap, not on the CV", "gap")}
SHORT = {"met": "met", "partly": "partly", "missing": "missing"}


# ------------------------------------------------------------------ words

def _uk(iso):
    """A stored UTC moment in UK time, or None."""
    text = str(iso or "").strip()
    moment = None
    for fmt, size in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16),
                      ("%Y-%m-%d", 10)):
        try:
            moment = datetime.strptime(text[:size], fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if moment is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return moment.astimezone(ZoneInfo("Europe/London"))
    except Exception:  # noqa: BLE001  no tz database: UTC, right all winter
        return moment


def day(iso) -> str:
    m = _uk(iso)
    return f"{m.day} {m:%b}" if m else "date not recorded"


def day_time(iso) -> str:
    m = _uk(iso)
    return f"{m.day} {m:%b} {m:%H:%M}" if m else "date not recorded"


def clock(iso) -> str:
    m = _uk(iso)
    return f"{m:%H:%M}" if m else ""


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _lines_words(nums) -> str:
    nums = [str(n) for n in nums]
    if not nums:
        return ""
    return ("line " if len(nums) == 1 else "lines ") + ", ".join(nums)


# ------------------------------------------------------------- the model

def model(conn, item_id: int, config: dict, notices=None) -> dict | None:
    """Everything the page draws, read from the database. None for no role."""
    raw = conn.execute("SELECT * FROM opportunities WHERE id = ?", (item_id,)).fetchone()
    if raw is None:
        return None
    row = dict(raw)
    applied = rc.role_is_applied(conn, item_id)
    is_open = prep.is_open(row)
    can_edit = is_open and not applied
    description = (row.get("description_text") or "").strip()
    working = prep.load_working(row)
    on_description = prep.scored_on_description(row)
    stale_description = False
    if not on_description:
        stale_description = bool(description) and working is not None
        working = None
    copy = tailored_cv.for_row(conn, row)
    shown_text = None
    standing = {"kind": "none", "record": None}
    if copy is not None:
        shown_text = (copy["saved_text"] if not can_edit and copy.get("saved_text")
                      else copy.get("cv_text") or "")
        shown_hash = cv_store.cv_hash(shown_text)
        standing = tailored_cv.standing(row, copy, shown_hash,
                                        tailored_cv.scored_master_hash(row, config))
        if not can_edit:
            standing = tailored_cv.sent_standing(conn, row, shown_hash, standing)
    master = None
    master_problem = None
    try:
        master = tailored_cv.master(config)
    except cv_doc.CvError as err:
        master_problem = str(err)
    unlaid = bool(master) and not master["canonical"]
    needs_copy = copy is None and can_edit and bool(master) and master["canonical"]
    if shown_text is None and can_edit and master:
        shown_text = master["stored"] if unlaid else master["text"]
    asks_from, asks_working = "master", working
    record = standing.get("record")
    if standing["kind"] in ("current", "earlier") and record:
        asks_from, asks_working = "tailored", record.get("working")
    asks = []
    if asks_working is not None:
        shown_lines = cv_store.cv_lines(shown_text) if shown_text is not None else None
        asks = cv_doc.place_asks([a for a in asks_working.get("asks") or []
                                  if isinstance(a, dict)],
                                 shown_lines, asks_working.get("long_lines"))
        for i, a in enumerate(asks, 1):
            a["num"] = i
    app = (applications.get(conn, applications.role_ref(row))
           if applications.has_table(conn) else None)
    card = _card_score(conn, row)
    try:
        import score_item
        master_version = score_item.get_cv_version(config)
    except Exception:  # noqa: BLE001  a missing file is not a failed page
        master_version = None
    design = cv_store.read_design()
    pages = (cv_doc.estimate_pages(cv_store.cv_lines(shown_text), design)
             if shown_text else [])
    company_known = bool(row.get("company_id")) and conn.execute(
        "SELECT 1 FROM companies WHERE id = ?", (row["company_id"],)).fetchone() is not None
    advert_text = "\n".join(t for t in (row.get("title") or "", description) if t)
    return {"row": row, "id": int(item_id), "applied": applied, "is_open": is_open,
            "experience": experience.matches_for_advert(conn, advert_text),
            "company_known": company_known,
            "design": design, "pages": pages,
            "can_edit": can_edit, "description": description, "working": working,
            "on_description": on_description, "stale_description": stale_description,
            "copy": copy, "standing": standing, "record": record,
            "shown_text": shown_text, "master": master, "master_problem": master_problem,
            "master_version": master_version, "unlaid": unlaid, "needs_copy": needs_copy,
            "asks_from": asks_from, "asks_working": asks_working, "asks": asks,
            "app": app, "card": card, "notices": list(notices or []),
            "pending": prep.pending(row, applied, config),
            "tier_move": _tier_move(conn, row, on_description),
            "moved_on": _moved_on(row, on_description, config),
            "linkedin": linkedin_url(conn, row), "config": config,
            "thresholds": _thresholds(config), "floors": _floors(config)}


# The CV page's Design, as its sheet sets it (cv_screen applyDesign): the
# spacing as a gap scale, the margins in millimetres as pixels at 96 dpi.
_SPACE = {"compact": 0.5, "standard": 1, "relaxed": 1.5}


def design_style(design: dict) -> str:
    """The sheet's custom properties for a design, the CV page's own."""
    d = cv_doc.clean_design(design)
    return (f"--cv-font:{d['font']};--cv-scale:{d['size'] / 10.5:g};"
            f"--cv-gap:{_SPACE.get(d['spacing'], 1):g};"
            f"--sheet-pad-side:{round(d['margins'] * 3.78)}px;"
            f"--sheet-pad-top:{round((d['margins'] - 2) * 3.78)}px")


def design_words(design: dict) -> str:
    d = cv_doc.clean_design(design)
    return f"{d['font']} {d['size']:g} pt, {d['spacing']} spacing, {d['margins']} mm margins"


def pages_words(pages: list, design: dict | None = None) -> str:
    """How many A4 pages the Word file would be, as the CV page estimates it,
    and the limit: "4 pages, limit is 2"."""
    del design
    n = len(pages)
    return f"{_plural(n, 'page')}, limit is 2" if n else ""


def linkedin_url(conn, row: dict) -> str | None:
    """The role's advert on LinkedIn (C11): this row's, or a repost's.

    The job page by its id, which is the link a person applies through. A row
    from another board has none of its own, so the newest advert of the same
    role that is on LinkedIn gives it, and a role with none gives None.
    """
    import check_jobs
    jid = check_jobs.job_view_id(row.get("source_url") or "")
    if not jid and row.get("role_key"):
        mate = conn.execute(
            "SELECT source_url FROM opportunities WHERE role_key = ? AND id <> ?"
            " AND source_url LIKE '%linkedin.com/%jobs/view/%'"
            " ORDER BY id DESC LIMIT 1", (row["role_key"], row["id"])).fetchone()
        jid = check_jobs.job_view_id(mate["source_url"] or "") if mate else None
    return f"https://www.linkedin.com/jobs/view/{jid}/" if jid else None


def _tier_move(conn, row: dict, on_description: bool) -> str | None:
    """The tier move the full description made, from the history, for as
    long as the score it came from is the score the row holds."""
    last = conn.execute(
        "SELECT tier, tier_from, scored_at FROM score_history"
        " WHERE opportunity_id = ? AND event = 'scored' AND writer = 'prepare'"
        " ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    if (last is None or not last["tier_from"] or not on_description
            or last["scored_at"] != row.get("scored_at")):
        return None
    words = prep.TIER_WORDS
    text = (f"The full description moved this role from "
            f"{words.get(last['tier_from'], last['tier_from'])} to "
            f"{words.get(last['tier'], last['tier'])}. Nothing was dismissed.")
    if last["tier"] == "filtered":
        text += (" The Jobs page counts a Filtered role instead of listing it, so it "
                 "will not appear there as a row. This page still opens.")
    return text


def _moved_on(row: dict, on_description: bool, config) -> list[str]:
    """What has moved on since a full description score, apart from the
    description, which is the same one."""
    if not on_description:
        return []
    out = []
    try:
        if row.get("prompt_version") != rc.prompt_version():
            out.append("the prompt has changed since")
        import score_item
        if row.get("cv_version") != score_item.get_cv_version(config):
            out.append("the CV has changed since")
    except Exception:  # noqa: BLE001  a missing file is not a failed page
        return []
    return out


def _thresholds(config) -> dict:
    try:
        return rc.read_score_thresholds(config)
    except Exception:  # noqa: BLE001
        return {"pass": 70, "floor": 40}


def _floors(config) -> dict:
    try:
        return rc.read_rate_floors(config)
    except Exception:  # noqa: BLE001  the page still opens without a floor line
        return {}


def _card_score(conn, row: dict) -> dict | None:
    """The card's score: the row's own when it is a card score, else the
    newest card score the history kept for this advert."""
    if str(row.get("score_basis") or "").lower() != rc.SCORE_BASIS_SPEC:
        if row.get("cv_match") is None:
            return None
        return {"cv_match": row.get("cv_match"), "scored_at": row.get("scored_at"),
                "cv_version": row.get("cv_version"), "replaced": False,
                "one_line_why": row.get("one_line_why")}
    found = conn.execute(
        "SELECT cv_match, scored_at, cv_version, one_line_why FROM score_history"
        " WHERE opportunity_id = ? AND event <> ? AND score_basis = ?"
        " AND cv_match IS NOT NULL ORDER BY id DESC LIMIT 1",
        (row["id"], rc.TAILORED_EVENT, rc.SCORE_BASIS_CARD)).fetchone()
    if found is None:
        return None
    return {**dict(found), "replaced": True}


# ------------------------------------------------------------ small parts

def _btn_cost(label: str, attrs: str = "", primary: bool = False) -> str:
    cls = "btn btn-primary" if primary else "btn"
    return (f'<button type="button" class="{cls}" {attrs}><span class="cost">{COST}:</span> '
            f'<span class="lbl">{esc(label)}</span></button>')


def _notice(kind: str, html_text: str, extra: str = "", attrs: str = "") -> str:
    return f'<div class="notice {kind}" {attrs}><p>{html_text}</p>{extra}</div>'


def _tag(text_html: str, cls: str = "") -> str:
    return f'<span class="tag{(" " + cls) if cls else ""}">{text_html}</span>'


def _gate_mark(state) -> str:
    """One gate's mark from its state (radar_common.gate_state). A rate the
    advert does not state has its own mark, never a pass or a fail."""
    if state == rc.PASSES:
        return '<span class="pass">Passes</span>'
    if state == rc.FAILS:
        return '<span class="fail">Fails</span>'
    if state == rc.NOT_STATED:
        return '<span class="none">? Not stated</span>'
    return '<span class="none">Not judged</span>'


def _copy_state(row: dict, record: dict, gate: str) -> str:
    """A tailored copy's gate state. The copy is scored on the same advert, so
    a gate the advert leaves silent is not stated on the copy too."""
    if rc.gate_state(row, gate) == rc.NOT_STATED:
        return rc.NOT_STATED
    value = (record.get("gates") or {}).get(gate)
    return rc.NOT_JUDGED if value is None else rc.PASSES if int(value) else rc.FAILS


def master_diff(master_text: str | None, copy_text: str | None) -> tuple[int, list[str]]:
    """How many lines of a copy differ from the master, and in what way:
    "master line 11 cut", "line 12 reworded", "line 29 added"."""
    if not master_text or copy_text is None:
        return 0, []
    a = cv_store.cv_lines(master_text)
    b = cv_store.cv_lines(copy_text)
    out, count = [], 0
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "delete":
            nums = [n for n in range(i1 + 1, i2 + 1) if a[n - 1].strip()]
            if nums:
                out.append(f"master {_lines_words(nums)} cut")
                count += len(nums)
        elif op == "insert":
            nums = [n for n in range(j1 + 1, j2 + 1) if b[n - 1].strip()]
            if nums:
                out.append(f"{_lines_words(nums)} added")
                count += len(nums)
        else:
            nums = [n for n in range(j1 + 1, j2 + 1) if b[n - 1].strip()]
            if nums:
                out.append(f"{_lines_words(nums)} reworded")
                count += max(len(nums), i2 - i1)
    return count, out


# ------------------------------------------------------------- the head

def _months(row: dict) -> str:
    found = re.search(r"\b(\d{1,2})[- ]months?\b", row.get("description_text") or "", re.I)
    return f", {found.group(1)} months" if found else ""


def _join(parts: list) -> str:
    return page_frame.divided(parts)


def advert_link(m: dict):
    """(the advert's address, the board's name): LinkedIn's job page where
    there is one, else the row's own link."""
    if m["linkedin"]:
        return m["linkedin"], "LinkedIn"
    row = m["row"]
    url = bd.safe_url(row.get("source_url"))
    if not url:
        return None, None
    return url, jp.BOARDS.get(row.get("source"), bd.via(row.get("source")))


def stages_html(m: dict) -> str:
    """Application: the five stages as dots and labels, those reached filled."""
    app = m["app"] or {}
    stage = app.get("stage")
    order = list(applications.OPEN_STAGES)
    reached = order.index(stage) if stage in order else -1
    items = []
    for i, s in enumerate(order):
        cls = "is-reached" if i <= reached else ""
        here = ' aria-current="step"' if s == stage else ""
        items.append(f'<li class="{cls}"{here}><span class="dot" aria-hidden="true"></span>'
                     f'{esc(applications.STAGE_WORDS[s])}'
                     + ('<span class="visually-hidden">, reached</span>' if i <= reached else '')
                     + '</li>')
    closed = ""
    if stage == "closed":
        reason = applications.REASON_WORDS.get(app.get("closed_reason"), "Closed")
        closed = f'<span class="stchip is-neu">Closed, {esc(reason.lower())}</span>'
    board = f"{BOARD_URL}#app-{app['id']}" if app.get("id") else BOARD_URL
    return (f'<div class="pa-app"><span class="pa-app-l" id="pa-app-l">Application</span>'
            f'<ol class="stages" aria-labelledby="pa-app-l">{"".join(items)}</ol>{closed}'
            f'<a class="plain pa-board" href="{esc(board)}">Open the applications board</a></div>'
            '<p class="pa-app-say" data-pa-app-say role="status" aria-live="polite"></p>')


def head_html(m: dict) -> str:
    """The header card: the role, its tier, the advert and Mark applied; its
    facts; the application's stages."""
    row = m["row"]
    title = row.get("title") or "Untitled role"
    tier = row.get("tier")
    pill = (tiers.pill_html(tier, jp.TIER_WORDS.get(tier), singular=True) if tier
            else '<span class="tier-pill">Not scored</span>')
    acts = []
    url, board = advert_link(m)
    if url:
        acts.append(f'<a class="plain" href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                    f'{esc(jp.open_advert_words(row) if hand_roles.is_hand(row) else "Open the advert on " + board)}'
                    '<span class="visually-hidden"> (opens in a new tab)'
                    '</span></a>')
    copy = m["copy"] or {}
    stage = (m.get("app") or {}).get("stage")
    if m["can_edit"]:
        saved_lines = cv_store.cv_lines(copy["saved_text"]) if copy.get("saved_text") else []
        same = bool(copy.get("saved_text")) and copy.get("saved_hash") == copy.get("cv_hash")
        data = {"name": copy.get("saved_name"), "at": clock(copy.get("saved_at")),
                "same": same, "lines": saved_lines}
        acts.append('<button type="button" class="btn btn-solid" id="pa-apply" data-pa-apply '
                    f'aria-haspopup="dialog" data-saved="{esc(json.dumps(data, ensure_ascii=False))}">'
                    'Mark applied</button>')
    elif m["applied"] and stage in (None, "applied"):
        acts.append('<button type="button" class="btn" id="pa-undo" data-pa-undo>Undo'
                    '<span class="visually-hidden"> Mark applied</span></button>')
    company = esc(row.get("company") or "Unknown company")
    if m.get("company_known"):
        company = f'<a class="plain" href="/watchlist/{int(row["company_id"])}">{company}</a>'
    eng = str(row.get("engagement_type") or "").lower()
    kind = ("Contract" + _months(row) if eng == "contract" else "Permanent" if eng == "permanent"
            else "")
    source = jp.BOARDS.get(row.get("source"), bd.via(row.get("source")))
    facts = [company, esc(row.get("location") or "Place not stated"), esc(kind), esc(source),
             f'First seen {esc(day(row["first_seen"]))}' if row.get("first_seen") else ""]
    if hand_roles.is_hand(row):
        # Said as Jobs says it: the client or Client not named, the type
        # chosen, and Added by you with the date.
        if not row.get("company"):
            facts[0] = "Client not named"
        facts[2] = esc(hand_roles.type_word(row))
        facts[4] = f'Added by you {esc(day(row["added_at"]))}'
    marked = ""
    if m["applied"]:
        name = row.get("applied_cv")
        at = row.get("applied_at") or (m.get("app") or {}).get("stage_at")
        marked = (f'<div class="notice ok" id="pa-marked" hidden tabindex="-1">'
                  f'<p><b>Marked applied, {esc(clock(at))}</b>'
                  + (f', with {esc(name)}.docx.' if name else ', with no CV recorded.') + '</p></div>')
    return (f'<div class="ph-top">{page_frame.chip("jobs", "jobs")}<h1>{esc(title)}</h1>{pill}'
            f'<div class="ph-acts">{"".join(acts)}</div></div>'
            f'<p class="ph-facts">{_join(facts)}</p>{stages_html(m)}{marked}')


def _floor_words(m: dict) -> str:
    floors = m["floors"] or {}
    eng = str(m["row"].get("engagement_type") or "").lower()
    if eng == "permanent" and floors.get("salary"):
        return f"Floor £{int(floors['salary']):,} a year"
    if floors.get("day"):
        return f"Floor £{int(floors['day']):,} a day"
    return ""


def _gate_tile(gate: str, name: str, state, body: str, line: str, attrs: str = "",
               line_cls: str = "gate-note", word: str | None = None, link: str = "") -> str:
    _mk, _st, mark_word = jp.STATE_MARKS[state]
    word = word or mark_word
    cls = jp.GATE_TONES.get(state, "is-neu")
    glow = " gate-glow" if state in (rc.NOT_STATED, rc.FAILS) else ""
    icon = (f'<span class="gate-chip {cls}" aria-hidden="true"><svg viewBox="0 0 20 20" width="18" '
            'height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true" focusable="false">{jp.GATE_ICONS[state]}</svg></span>')
    return (f'<div class="gate gate-{gate} {cls}{glow}" {attrs}>{icon}<span class="gate-name">{name}</span>'
            f'<span class="gate-status {cls}">{esc(word)}</span>{body}'
            f'<span class="{line_cls}">{line}{(" " + link) if link else ""}</span></div>')


def tiles_html(m: dict) -> str:
    """The four gates as tiles: Sector, CV match, Location, Rate."""
    row = m["row"]
    tiles = []
    for gate, name, _letter in jp.GATES:
        state = rc.gate_state(row, gate)
        if gate == "cv":
            spec = m["on_description"]
            score = row.get("cv_match") if spec else (m["card"] or {}).get("cv_match")
            num = (f'<span class="gate-num num">{int(score)}</span>' if score is not None
                   else '<span class="gate-num num none">Not scored</span>')
            pill = '<span class="src-pill">Full description</span>' if spec and score is not None else ""
            body = f'<span class="gate-fig">{num}{pill}</span>'
            line = f'Pass mark {int(m["thresholds"].get("pass", 70))}'
            tiles.append(_gate_tile(gate, name, state, body, esc(line), 'data-fig="spec"',
                                    "gate-note fig-sub"))
            continue
        line = (row.get(f"gate_{gate}_note") or "").strip()
        if gate == "rate":
            quote = (_rate_working(m) or {}).get("rate_quote")
            source = (_rate_working(m) or {}).get("rate_quote_from")
            if quote and source != "disagrees":
                line = f"“{quote}”"
            elif state == rc.NOT_STATED:
                line = _floor_words(m)
            word, line, link = jp.rate_tile_words(row, state, jp.STATE_MARKS[state][2], line)
            tiles.append(_gate_tile(gate, name, state, "", esc(line), word=word, link=link))
            continue
        tiles.append(_gate_tile(gate, name, state, "", esc(line)))
    return f'<div class="gate-tiles">{"".join(tiles)}</div>'


def score_html(m: dict) -> str:
    """The gate tiles, what a press said, and Why this score."""
    row = m["row"]
    record = m["record"]
    kind = m["standing"]["kind"]
    out = ['<h2 class="visually-hidden" id="score-h" tabindex="-1">Gates</h2>', tiles_html(m)]
    for n in m["notices"]:
        out.append(_press_notice(n))
    if not m["notices"]:
        out.append(_standing_notice(m))
    if m["tier_move"]:
        out.append(_notice("", esc(m["tier_move"].split(". ")[0].rstrip(".") + ".")))
    out.append('<p class="notice work" data-pa-working hidden></p>')
    spec = m["on_description"]
    master_reasons = bd.reasons_html(row.get("score_reasons"), row.get("scored_at"),
                                     bd.quotes_hold(row), row=row)
    tailored_reasons = (bd.reasons_html(record.get("reasons"), record.get("scored_at"))
                        if record and kind in ("current", "earlier") else "")
    if master_reasons or tailored_reasons:
        parts_ = []
        if master_reasons:
            parts_.append(f'<div class="score-master"><h3>{"Full description" if spec else "Advert card"}'
                          f'</h3>{master_reasons}</div>')
        if tailored_reasons:
            parts_.append(f'<div id="tailored-score"><h3>This copy</h3>{_moved_html(m)}'
                          f'{tailored_reasons}</div>')
        out.append('<details class="pa-why card g-jobs glow-faint"><summary>Why this score</summary>'
                   f'<div class="pa-why-in">{"".join(parts_)}</div></details>')
    return "".join(out)


# ------------------------------------------------------------- the score

def _made_html(rec: dict, spec: bool, tailored: bool = False) -> str:
    """How a score was made: on what, when, by which model and prompt."""
    if not rec or rec.get("cv_match") is None:
        return ""
    what = "the full description" if spec else "the advert card"
    cv = "" if tailored else f', CV {esc(rec.get("cv_version") or "unknown")}'
    return (f'<p class="small">Scored against {what}, {esc(day_time(rec.get("scored_at")))}, '
            f'by {esc(rec.get("score_model") or "an unknown model")}, prompt '
            f'{esc(rec.get("prompt_version") or "unknown")}{cv}.</p>')


def shown_copy_words(m: dict) -> str:
    """Which copy the page shows, in the words the score beside it uses: the
    copy being tailored, or, where nothing can change any more, the copy that
    was saved and sent, the one saved for a role that closed, or the copy as
    it was left when nothing was ever saved."""
    copy = m["copy"]
    if m["can_edit"] or copy is None:
        return "this copy"
    if copy.get("saved_text"):
        return "the copy saved and sent" if m["applied"] else "the copy saved for this role"
    return "the copy as it was left, never saved"


def _moved_html(m: dict) -> str:
    """What the copy's score moved, against the score before it."""
    record = m["record"]
    if not record or m["standing"]["kind"] not in ("current", "earlier"):
        return ""
    out = []
    before = record.get("before")
    if before:
        whose = "the master CV" if before.get("source") == "master" else "the tailored copy"
        tier = before.get("tier")
        out.append(f'<p class="small">Before this rescore, on {whose}: CV match '
                   f'{esc(before.get("cv_match"))}'
                   + (f', {esc(prep.TIER_WORDS.get(tier, tier))}' if tier else '') + '.</p>')
    changes = record.get("changes")
    if changes:
        items = []
        for c in changes:
            word = cv_doc.change_word(c)
            if word == "new":
                head = f'New this time, {esc(c.get("to"))}.'
            elif word == "gone":
                head = f'Not listed this time, was {esc(c.get("from"))}.'
            else:
                head = f'Now {esc(c.get("to"))}, was {esc(c.get("from"))}.'
            items.append(f'<li><b>{head}</b> {esc(c.get("ask"))}</li>')
        out.append(f'<p class="small">{_plural(len(changes), "ask")} changed.</p>'
                   '<ul class="moved">' + "".join(items) + '</ul>')
    elif changes is not None:
        out.append('<p class="small">No ask changed.</p>')
    return "".join(out)


def _press_notice(n: dict) -> str:
    kind = {"fetch": "bad", "score": "bad"}.get(n.get("kind"), "work")
    text = esc(n.get("text", ""))
    head, _sep, rest = text.partition(". ")
    if n.get("kind") in ("fetch", "score") and rest:
        text = f"<b>{head}.</b> {rest}"
    extra = ""
    if n.get("retry"):
        if n.get("free"):
            extra = ('<button type="button" class="btn" data-pa-fetch><span class="lbl">'
                     'Try again</span> <span class="cost">(no charge)</span></button>')
        else:
            extra = _btn_cost("Try again", "data-pa-fetch")
    return _notice(kind, text, extra, 'data-pa-press')


def _standing_notice(m: dict) -> str:
    """What the page says on opening, from what is stored, without a press."""
    row = m["row"]
    if m["applied"]:
        return _notice("", esc(prep.APPLIED_NOTE))
    if not m["is_open"]:
        return _notice("", esc(prep.NOT_OPEN_NOTE))
    if m["stale_description"]:
        return _notice("warn", "<b>The stored description has changed since this role "
                       "was scored.</b>")
    if not m["description"] and (blocked := prep.fetch_blocked_why(row)):
        return _notice("bad", esc(prep.fetch_failed_text(blocked)))
    if (m["description"] and prep.needs_score(row)
            and (cooling := prep.score_cooling_text(row, m["description"], m["config"]))):
        return _notice("bad", esc(cooling))
    return ""


# ------------------------------------------------------------- the gates

_FLOOR = re.compile(r"£\s?([0-9][0-9,]*(?:\.[0-9]+)?k?)\s+floor", re.I)


def judged_floor(note) -> str | None:
    """The floor a rate note says it was judged against, "£700", where the
    note names one (the code's note always does, the scorer's mostly does),
    else None."""
    found = _FLOOR.search(str(note or ""))
    return f"£{found.group(1)}" if found else None


def gate_whys(m: dict) -> dict:
    """For each gate, (the role's Why, this copy's Why): each from its own
    score, never one standing in for the other."""
    row = m["row"]
    record = m["record"] if m["standing"]["kind"] in ("current", "earlier") else None
    copy_notes = ((record or {}).get("notes") or {})
    out = {}
    for g, _l, _n in GATE_ROWS:
        if g == "cv":
            role = (f"{row.get('cv_match')} on the master CV" if m["on_description"]
                    else f"{row.get('cv_match')} on the card")
            copy = f"{record.get('cv_match')} on this copy" if record else None
        else:
            role = (row.get(f"gate_{g}_note") or "").strip() or None
            copy = (copy_notes.get(g) or "").strip() or None if record else None
        out[g] = (role, copy)
    return out


def gates_when_words(m: dict) -> str:
    """When and against what each column was judged: this copy's gates when
    the copy was scored, the role's as Radar holds them now, judged again in
    code when a floor changes, with the floor each rate note names."""
    record = m["record"]
    first = "full spec" if m["on_description"] else "card"
    whys = gate_whys(m)
    role_floor, copy_floor = judged_floor(whys["rate"][0]), judged_floor(whys["rate"][1])
    floor_now = (m["floors"] or {}).get("day")
    words = (f"This copy's gates were judged when it was scored, "
             f"{day_time(record.get('scored_at'))}"
             + (f", its rate against the {copy_floor} floor" if copy_floor else "")
             + ", and are not judged again. The " + first + " column is the role's "
             "gates as Radar holds them now, judged again in code when a floor changes"
             + (f", its rate against the {role_floor} floor" if role_floor else "") + ".")
    if floor_now:
        words += f" Your day-rate floor is now £{int(floor_now):,}."
    return words


# ------------------------------------------------------------- the rate

def _rate_working(m: dict):
    if m["working"] is not None:
        return m["working"]
    if m["asks_from"] == "tailored":
        return m["asks_working"]
    return None


def _sentence_end(text: str) -> str:
    """Words that end as a sentence does, so the next one can follow."""
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?" else text + "."


def range_bottom_words(m: dict) -> str:
    """Where the stored pay is a range whose bottom is under the floor it is
    judged against, the clause that says so: "its bottom, £650, is under your
    floor". Judged as the gate judges the top (radar_common.gate_pay), the
    same period, currency and floor, so an hourly bottom is times eight. Empty
    for a single figure, no figure, or a bottom that clears the floor."""
    row = m["row"]
    try:
        low, high = float(row.get("pay_min") or 0), float(row.get("pay_max") or 0)
    except (TypeError, ValueError):
        return ""
    if not (low > 0 and high > low) or not m["floors"]:
        return ""
    try:
        bottom = rc.gate_pay(row.get("pay_currency"), row.get("pay_period"), low, None,
                             m["floors"], m["config"], fee=hand_roles.fee_for(row))
    except Exception:  # noqa: BLE001  a floor line missing is no clause, not a failed page
        return ""
    if bottom.get("gate") is not False:
        return ""
    return f"its bottom, £{bottom['figure']:,.0f}, is under your floor"



# -------------------------------------------------------- the application

_HEADING = re.compile(r"^[A-Z][^.!?:;,]{0,58}$")


def advert_blocks(description: str) -> list[dict]:
    """The stored description as headings and paragraphs, one per stored line.

    The page checker stores one line per block of the advert (check_jobs._text),
    with no marks for a heading or a bullet. A short line that does not end
    like a sentence reads as a heading; a run of three or more lines under one
    heading, or lines with a bullet mark, read as a list of items."""
    out = []
    heading = None
    for line in (description or "").splitlines():
        text = line.strip()
        if not text:
            continue
        bullet = re.match(r"^[-*•]\s+(.*)$", text)
        if bullet:
            text = bullet.group(1)
        if not bullet and _HEADING.match(text) and len(text.split()) <= 6:
            heading = text
            out.append({"kind": "h", "text": text, "heading": text})
            continue
        out.append({"kind": "li" if bullet else "p", "text": text, "heading": heading})
    for b in out:
        if b["kind"] == "p":
            under = [o for o in out if o["kind"] != "h" and o["heading"] == b["heading"]]
            if len(under) >= 3 and all(len(o["text"]) <= 200 for o in under):
                b["kind"] = "li"
    return out


def where_in_advert(m: dict, sentence: str, whole: bool = True):
    """(where in the advert a sentence is in words, block index), or None:
    "What you will do, item 8", or "The role, sentence 5"."""
    if not sentence:
        return None
    blocks = m.setdefault("_blocks", advert_blocks(m["description"]))
    for i, b in enumerate(blocks):
        if b["kind"] == "h":
            continue
        found = rc.find_verbatim(sentence, b["text"], whole_words=whole)
        if not found:
            continue
        under = [j for j, o in enumerate(blocks) if o["kind"] != "h"
                 and o["heading"] == b["heading"]]
        k = under.index(i)
        if b["kind"] == "li":
            place = f"item {k + 1}"
        else:
            before = " ".join(blocks[j]["text"] for j in under[:k])
            at = _find_ci(b["text"], found)
            lead = b["text"][:at] if at > 0 else ""
            count = len(re.findall(r"[.!?](?=\s|$)", (before + " " + lead).strip())) + 1
            place = f"sentence {count}"
        return (f"{b['heading']}, {place}" if b["heading"] else place), i
    return None


def _marked(text: str, asks: list, rate: str | None) -> str:
    """One block of the advert with each ask's sentence underlined, and the
    pay line marked. A span that overlaps one already marked only adds its
    mark after it."""
    spans = []
    for a in asks:
        found = rc.find_verbatim(a["sentence"], text, whole_words=True)
        at = _find_ci(text, found) if found else -1
        if at >= 0:
            spans.append((at, at + _span_len(text, at, found), a))
    if rate:
        found = rc.find_verbatim(rate, text)
        at = _find_ci(text, found) if found else -1
        if at >= 0:
            spans.append((at, at + _span_len(text, at, found), None))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept, tails = [], {}
    for s in spans:
        if kept and s[0] < kept[-1][1]:
            if s[2] is not None:
                tails.setdefault(len(kept) - 1, []).append(s[2])
            continue
        kept.append(s)
    out, pos = [], 0
    for k, s in enumerate(kept):
        out.append(esc(text[pos:s[0]]))
        words = esc(text[s[0]:s[1]])
        if s[2] is None:
            out.append(f'<span class="rate" id="adv-rate" tabindex="-1">{words}</span>')
        else:
            a = s[2]
            st = MARKS.get(a["status"], MARKS["missing"])[1]
            extra = "".join(_tagmark(x) for x in tails.get(k, []))
            out.append(f'<span class="ask {st}" id="adv-a{a["num"]}" tabindex="-1">{words}</span>'
                       f'{_tagmark(a)}{extra}')
        pos = s[1]
    out.append(esc(text[pos:]))
    return "".join(out)


def _tagmark(a: dict) -> str:
    st = MARKS.get(a["status"], MARKS["missing"])[1]
    return (f' <span class="tagmark {st}">'
            f'<span class="visually-hidden">{SHORT.get(a["status"], "missing")}, ask</span>'
            f' A{a["num"]}</span>')


def _find_ci(text: str, found: str) -> int:
    """Where find_verbatim's answer starts in the unflattened text, or -1."""
    if not found:
        return -1
    hit = re.search(r"\s+".join(re.escape(w) for w in found.split(" ")), text, re.IGNORECASE)
    return hit.start() if hit else -1


def _span_len(text: str, at: int, found: str) -> int:
    hit = re.compile(r"\s+".join(re.escape(w) for w in found.split(" ")),
                     re.IGNORECASE).match(text, at)
    return (hit.end() - at) if hit else len(found)


# ---------------------------------------------------------- the CV's head

def _version_time(name):
    found = re.match(r"^cv-(\d{8})-(\d{6})(?:-\d+)?\.md$", name or "")
    if not found:
        return None
    try:
        return datetime.strptime(found.group(1) + found.group(2),
                                 "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def version_words(name, short: bool = False) -> str:
    """A master CV version as the CV page names it, by its number in the
    History and the date it was saved: "version 3 (13 Nov 09:00 UTC)". A name
    the History does not list keeps its date, or its name when it has none."""
    if not name:
        return "a version not recorded"
    try:
        names = cv_store.history()
    except OSError:
        names = []
    when = _version_time(name)
    stamp = f"{when.day} {when:%b} {when:%H:%M} UTC" if when else None
    if name in names:
        number = len(names) - names.index(name)
        return f"version {number}" if short else (
            f"version {number} ({stamp})" if stamp else f"version {number}")
    return f"the version of {stamp}" if stamp else name


def compare_url(name) -> str | None:
    """The CV page's comparison of a version with the active one
    (/cv?compare=, cv_screen.render_page), for a version its History lists."""
    try:
        names = cv_store.history()
        active = cv_store.active_cv_name()
    except OSError:
        return None
    if not name or name not in names or name == active:
        return None
    from urllib.parse import quote
    return f"/cv?compare={quote(name)}#history"


# ------------------------------------------------------------- the bar

def scored_words(m: dict) -> str:
    row = m["row"]
    if m["on_description"]:
        return "Scored on the full description"
    if str(row.get("score_basis") or "").strip() or row.get("cv_match") is not None:
        return "Scored on the advert card"
    return "Not scored yet"


def fetch_button(m: dict) -> str:
    """The one paid press, Score the full description, with its cost beside it.
    Only where the role is not scored on the full description and a fetch or
    score can run."""
    if m["on_description"] or not m["pending"] or m["notices"]:
        return ""
    label = ("Fetch and score the full description" if not m["description"]
             else "Score the new description" if m["stale_description"]
             else "Score the full description")
    return (f'<span class="fetch"><button type="button" class="btn" data-pa-fetch>'
            f'<span class="lbl">{esc(label)}</span></button>'
            f'<span class="cost">{COST}</span></span>')


def advert_html(m: dict) -> str:
    row = m["row"]
    desc = m["description"]
    out = [f'<div class="pa-strip"><span class="strip-t">{esc(scored_words(m))}</span>{fetch_button(m)}</div>']
    if not desc:
        out.append('<p class="none">Not stored yet.</p>')
        return "".join(out)
    asks = m["asks"]
    blocks = m.setdefault("_blocks", advert_blocks(desc))
    marks: dict = {}
    for a in asks:
        s = a.get("sentence")
        where = where_in_advert(m, s) if s else None
        if where:
            marks.setdefault(where[1], []).append(a)
    rate = (_rate_working(m) or {}).get("rate_quote")
    rate_at = where_in_advert(m, rate, whole=False) if rate else None
    company = row.get("company") or ""

    def rest_of(i: int, b: dict, under_rest: bool) -> bool:
        """Boilerplate, folded as on Jobs, unless an ask or the rate is marked in it."""
        if marks.get(i) or (rate_at and rate_at[1] == i):
            return False
        return under_rest or (b["kind"] != "h" and jp._rest_line(b["text"]))

    main, rest = [], []
    under_rest = False
    for i, b in enumerate(blocks):
        if b["kind"] == "h":
            under_rest = bool(jp._REST_HEADING.match(b["text"]) or jp._about_company(b["text"], company))
            (rest if under_rest else main).append((i, b))
            continue
        (rest if rest_of(i, b, under_rest) else main).append((i, b))

    def draw(items: list) -> str:
        body, in_list = [], False
        for i, b in items:
            if b["kind"] == "h":
                if in_list:
                    body.append("</ul>")
                    in_list = False
                body.append(f'<h3>{esc(b["text"])}</h3>')
                continue
            text = _marked(b["text"], marks.get(i, []), rate if rate_at and rate_at[1] == i else None)
            if b["kind"] == "li":
                if not in_list:
                    body.append("<ul>")
                    in_list = True
                body.append(f"<li>{text}</li>")
            else:
                if in_list:
                    body.append("</ul>")
                    in_list = False
                body.append(f"<p>{text}</p>")
        if in_list:
            body.append("</ul>")
        return "".join(body)

    folded = (f'<details class="advert-rest"><summary>Rest of the advert</summary>{draw(rest)}</details>'
              if rest else "")
    out.append(f'<div class="adv" id="adv-body">{draw(main)}{folded}</div>')
    return "".join(out)


def copy_label(m: dict) -> str:
    """The copy the score pill is for: the one being tailored, or, where
    nothing can change any more, the copy sent, the copy saved, or the copy
    as it was left unsaved."""
    copy = m["copy"] or {}
    if m["can_edit"]:
        return "This copy"
    if copy.get("saved_text"):
        return "Sent copy" if m["applied"] else "Saved copy"
    return "Unsaved copy"


def pages_pill(m: dict) -> str:
    n = len(m["pages"])
    if not n:
        return '<span class="pill pa-pages" id="pa-pages" hidden></span>'
    return (f'<span class="pill pa-pages{" is-over" if n > 2 else ""}" id="pa-pages">'
            f'{esc(pages_words(m["pages"]))}</span>')


def workhead_html(m: dict) -> str:
    """The Tailored CV's top strip: the asks, this copy's score, the pages;
    then what stands in the way of a copy, where something does."""
    row = m["row"]
    copy = m["copy"]
    asks = m["asks"]
    bits = []
    if asks:
        n = {"met": 0, "partly": 0, "missing": 0}
        for a in asks:
            if not a.get("stale"):
                n[a.get("status") if a.get("status") in n else "missing"] += 1
        stale = sum(1 for a in asks if a.get("stale"))
        sr = page_frame.SR_COMMA
        # Each count in a span of its own, which the script rewrites, inside
        # its fact, which carries the divider and the screen reader's comma.
        bits.append('<span class="tally dv" data-pa-tally><span class="dv-in">'
                    f'<b class="dv-i">{_plural(len(asks), "ask")}</b>'
                    f'<span class="dv-i">{sr}<span data-pa-count="met">{n["met"]} met</span></span>'
                    f'<span class="dv-i">{sr}<span data-pa-count="partly">{n["partly"]} partly</span></span>'
                    f'<span class="dv-i">{sr}<span data-pa-count="missing">{_plural(n["missing"], "gap")}'
                    '</span></span>'
                    f'<span class="dv-i"{"" if stale else " hidden"}>{sr}<span data-pa-count="stale">'
                    f'{str(stale) + " stale" if stale else ""}</span></span></span></span>')
    else:
        bits.append('<span class="tally">No asks yet.</span>')
    record = m["record"]
    if record and m["standing"]["kind"] in ("current", "earlier") and record.get("cv_match") is not None:
        bits.append(f'<span class="pill" data-fig="copy">{esc(copy_label(m))} <span class="num">'
                    f'{esc(record.get("cv_match"))}</span><span class="fig-sub" hidden></span></span>')
    bits.append(pages_pill(m))
    out = [f'<div class="pa-strip cv-strip">{"".join(bits)}</div>']
    if m["applied"]:
        name = row.get("applied_cv")
        out.append(f'<p class="notice">Marked applied{" with " + esc(name) if name else ", no CV saved"}.</p>')
    elif not m["is_open"]:
        out.append('<p class="notice">This role is not open.</p>')
    if m["master_problem"] and copy is None:
        out.append(f'<p class="notice bad">The master CV could not be read. {esc(m["master_problem"])}</p>')
    if copy is not None and m["can_edit"]:
        changed = bool(m["master_version"] and copy.get("master_version")
                       and m["master_version"] != copy.get("master_version"))
        if changed:
            was = copy.get("master_version")
            compare = compare_url(was)
            link = (f'<a class="plain" href="{esc(compare)}">Compare {esc(version_words(was, short=True))} '
                    'with the active version</a>' if compare else
                    '<a class="plain" href="/cv#history">Open the CV page&rsquo;s versions</a>')
            out.append('<div class="notice warn" data-pa-master-moved><p><b>The master CV has changed '
                       f'since this copy was made.</b> This copy is from {esc(version_words(was))}; the '
                       f'master is now {esc(version_words(m["master_version"]))}.</p><p class="row-btns">'
                       '<button type="button" class="btn" data-cv-act="reset" disabled>Start again from '
                       f'master</button>{link}</p></div>')
    if asks and m["can_edit"]:
        earlier = m["standing"]["kind"] == "earlier"
        out.append('<p class="notice warn" id="copy-edited-note"'
                   + (' data-pa-earlier="1">' if earlier else ' hidden>')
                   + 'Edited since this score. <b data-pa-counts></b></p>')
    return "".join(out)


def bar_html(m: dict) -> str:
    """The copy's other buttons: Rescore role, Download Word, Start again from
    master, and where it was saved. Save copy sits by the section's title."""
    row = m["row"]
    copy = m["copy"] or {}
    rid = m["id"]
    saved = bool(copy.get("saved_text"))
    dl_attrs = (f'href="/prepare/{rid}/cv.docx" download' if saved else
                f'href="/prepare/{rid}/cv.docx" aria-disabled="true" '
                'aria-describedby="pa-save-as" data-pa-nodownload')
    download = f'<a class="btn" id="pa-download" {dl_attrs}>Download Word</a>'
    editable = m["can_edit"] and not m["unlaid"] and m["shown_text"] is not None
    out = []
    if editable:
        if m["description"]:
            out.append('<button type="button" class="btn" data-cv-act="rescore" disabled>'
                       '<span class="lbl">Rescore role</span> '
                       f'<span class="cost">{COST}</span></button>')
        else:
            out.append('<button type="button" class="btn" disabled>Rescore role</button>')
        out.append(download)
        out.append('<button type="button" class="btn quiet" data-cv-act="reset" disabled>'
                   'Start again from master</button>')
        same = saved and copy.get("saved_hash") == copy.get("cv_hash")
        words = (f'Saved {esc(day_time(copy.get("saved_at")))} as <b>{esc(copy.get("saved_name"))}</b>.'
                 if saved else 'Not saved yet.')
        out.append(f'<p class="save-as" id="pa-save-as" data-saved-same="{1 if same else 0}">{words}</p>')
    elif saved:
        out.append(download)
        out.append(f'<p class="save-as" id="pa-save-as">Saved {esc(day_time(copy.get("saved_at")))} as '
                   f'<b>{esc(copy.get("saved_name"))}</b>.</p>')
    return "".join(out)


# ------------------------------------------------------------ the notes

def note_cites(a: dict) -> list[dict]:
    return [{"n": p.get("n"), "t": p.get("whole") or p.get("text"),
             "cut": bool(p.get("cut")), "now": p.get("now")}
            for p in a.get("placed") or [] if isinstance(p, dict)]


def _changes(m: dict) -> dict:
    if m["asks_from"] != "tailored" or not m["record"]:
        return {}
    return {c.get("ask"): c for c in (m["record"].get("changes") or [])
            if c.get("to") is not None}


def _cite_line(m: dict, a: dict) -> str:
    bits = []
    cites = note_cites(a)
    if cites:
        links = ", ".join(
            f'<a href="#cv-h" data-pa-goto="{esc(c["now"] or c["n"])}">{esc(c["now"] or c["n"])}</a>'
            + (f'<span class="when" data-pa-when="{esc(c["n"])}"> ({esc(c["n"])} when scored)</span>'
               if c["now"] and c["now"] != c["n"] else
               f'<span class="when" data-pa-when="{esc(c["n"])}" hidden></span>')
            for c in cites)
        bits.append(("CV line " if len(cites) == 1 else "CV lines ") + links)
    where = where_in_advert(m, a.get("sentence")) if a.get("sentence") else None
    if where:
        bits.append(f'<a href="#adv-a{a["num"]}" data-pa-show="adv-a{a["num"]}">advert, '
                    f'{esc(where[0])}</a>')
    return page_frame.divided(bits, "cite") if bits else ""


# Why an ask shows no advert sentence, by score_item's sentence_dropped. An
# ask stored before the sentences were asked for (prompt 422cdad4) has no
# sentence key at all, and says nothing about one.
SENTENCE_DROPPED = {
    "not_found": "The advert sentence was not found word for word, so none is quoted.",
    "empty": "The scorer gave no advert sentence for this ask.",
    "too_long": "The advert sentence the scorer gave was longer than 25 words, so none "
                "is quoted.",
    "not_text": "The advert sentence the scorer gave could not be read, so none is quoted.",
}


def _sentence_html(a: dict) -> str:
    """The ask's advert sentence, quoted; or the ask's own words, with the
    reason no sentence is quoted, only where one was asked for and dropped."""
    if a.get("sentence"):
        return f'<q>{esc(a["sentence"])}</q>'
    out = f'<span class="ask-words">{esc(a.get("ask"))}</span>'
    if "sentence" not in a and "sentence_dropped" not in a:
        return out
    words = SENTENCE_DROPPED.get(a.get("sentence_dropped"))
    return out + (f'<span class="sub" data-pa-nosentence>{esc(words)}</span>' if words else "")


def note_html(m: dict, a: dict, changes: dict, fold: bool = True) -> str:
    status = a.get("status") if a.get("status") in MARKS else "missing"
    word, st = MARKS[status]
    num = a["num"]
    stale = bool(a.get("stale"))
    cites = note_cites(a)
    found_now = [c["now"] for c in cites if c.get("now")]
    line = found_now[0] if found_now else (cites[0]["n"] if cites else None)
    change = changes.get(a.get("ask"))
    was = ""
    if change:
        was = ('<span class="was">new this time</span>' if change.get("from") is None
               else f'<span class="was">was {esc(SHORT.get(change["from"], change["from"]))}'
                    '</span>')
    lines_words = esc(_lines_words(found_now or [c["n"] for c in cites]))
    data = (f'data-note="{num}" data-status="{status}" data-line="{esc(line or "")}" '
            f'data-cites="{esc(json.dumps(cites, ensure_ascii=False))}"')
    body = [_sentence_html(a)]
    if status == "partly" and a.get("why"):
        body.append(f'<span class="gapwhy" data-pa-why>{esc(a["why"])}</span>')
    if stale:
        gone = ", ".join(str(c["n"]) for c in cites if not c.get("now"))
        body.append(f'<span class="gapwhy" data-pa-stale-words>CV line {esc(gone)} when '
                    'scored, since changed.</span>')
    body.append(_cite_line(m, a))
    if cites:
        body.append('<button type="button" class="btn-sm" data-pa-light aria-pressed="false">'
                    f'Show cited lines<span class="visually-hidden">, A{num}</span></button>')
    mark = (f'<span class="mark stale">! Stale, was {SHORT[status]}</span>' if stale
            else f'<span class="mark {st}">{word}</span>')
    head = f'{mark} <span class="id">A{num}</span> {was}'
    if status == "met" and not stale and fold:
        return (f'<li class="note st-met is-closed" id="note-a{num}" {data}>'
                f'<button type="button" class="note-toggle" aria-expanded="false" '
                f'aria-controls="note-a{num}-body">{head} <span class="lines">{lines_words}'
                '</span><span class="chev" aria-hidden="true">&#9656;</span></button>'
                f'<div class="note-body" id="note-a{num}-body" hidden>{"".join(body)}</div>'
                '</li>')
    cls = "st-stale" if stale else f"st-{st}"
    return (f'<li class="note {cls} is-open" id="note-a{num}" {data} tabindex="-1">'
            f'<div class="head">{head} <span class="lines">{lines_words}</span></div>'
            f'<div class="note-body">{"".join(body)}</div></li>')


def note_groups(m: dict) -> list[tuple[int, list]]:
    """The notes beside the sheet: every met or partly ask with a cited line,
    grouped under the first of its lines that is still in the copy (or the
    one it cited, when none is), in the order of the sheet."""
    groups: dict = {}
    for a in m["asks"]:
        if a.get("status") == "missing":
            continue
        cites = note_cites(a)
        if not cites:
            continue
        found = [c["now"] for c in cites if c.get("now")]
        groups.setdefault(int(found[0] if found else cites[0]["n"]), []).append(a)
    return sorted(groups.items())


def notes_html(m: dict) -> str:
    if not m["asks"]:
        if m["stale_description"]:
            return ('<p class="notes-empty">The notes from the earlier description are not '
                    'shown. Score the new description to set them beside the lines.</p>')
        return ('<p class="notes-empty">The notes appear here, beside the lines they cite, '
                'once the full description is scored.</p>')
    changes = _changes(m)
    return "".join(
        f'<ol class="notes" data-pa-group="{line}" aria-label="Notes beside CV line {line}">'
        + "".join(note_html(m, a, changes) for a in asks) + '</ol>'
        for line, asks in note_groups(m))


def gaps_html(m: dict) -> str:
    asks = m["asks"]
    gaps = [a for a in asks if a.get("status") == "missing"]
    loose = [a for a in asks if a.get("status") in ("met", "partly") and not note_cites(a)]
    if not gaps and not loose:
        return ""
    out = []
    if gaps:
        out.append(f'<h3>Gaps: {_plural(len(gaps), "ask")} no line on the CV evidences</h3><ol>')
        for a in gaps:
            sentence = a.get("sentence")
            quote = _sentence_html(a)
            why = (f'<span class="gapwhy" data-pa-why>{esc(a["why"])}</span>' if a.get("why")
                   else '<span class="gapwhy">No line on the CV evidences this ask.</span>')
            show = (f'<a class="btn-sm" href="#adv-a{a["num"]}" data-pa-show="adv-a{a["num"]}">'
                    f'Show it in the advert<span class="visually-hidden">, A{a["num"]}</span></a>'
                    if sentence and where_in_advert(m, sentence) else "")
            out.append(f'<li class="note st-gap is-open" id="note-a{a["num"]}" '
                       f'data-note="{a["num"]}" data-status="missing" data-cites="[]" '
                       'tabindex="-1"><div class="head"><span class="mark gap">Gap, '
                       f'not on the CV</span> <span class="id">A{a["num"]}</span></div>'
                       f'<div class="note-body">{quote}{why}{_cite_line(m, a)}{show}</div></li>')
        out.append('</ol>')
    if loose:
        out.append(f'<h3>{_plural(len(loose), "ask")} marked met or partly with no CV line '
                   'that exists</h3><ol>')
        changes = _changes(m)
        out += [note_html(m, a, changes, fold=False) for a in loose]
        out.append('</ol>')
    return "".join(out)


# ------------------------------------------------- from your experience

XP_SHOWN = 5   # rows before Show all


def experience_html(m: dict) -> str:
    """The backed entries of the experience bank with a tag the advert names,
    most matching tags first (experience.matches_for_advert). Read only:
    nothing goes into the CV from here. Empty when nothing matches."""
    hits = m.get("experience") or []
    if not hits:
        return ""
    rows = []
    for i, h in enumerate(hits):
        chips = "".join(f'<li class="chip-v">{esc(t)}</li>' for t in h["matched"])
        rows.append(f'<li class="xp-row"{" hidden data-xp-more" if i >= XP_SHOWN else ""}>'
                    f'<a class="plain xp-link" href="/cv/experience/{h["id"]}">{esc(h["title"])}</a>'
                    f'<ul class="chips" aria-label="Matched tags">{chips}</ul></li>')
    more = (f'<p class="xp-all"><button type="button" class="btn-text" data-xp-all>'
            f'Show all {len(hits)}</button></p>' if len(hits) > XP_SHOWN else "")
    return (f'<details class="pa-xp"><summary><span class="xp-sum">From your experience</span>'
            f'<span class="xp-n">{len(hits)}</span></summary>'
            f'<ol class="xp-rows">{"".join(rows)}</ol>{more}</details>')


# ------------------------------------------------------------ the parts

SLOTS = ("head", "score", "advert", "workhead", "bar", "experience", "gaps", "notes")


def parts(m: dict) -> dict:
    return {"head": head_html(m), "score": score_html(m), "advert": advert_html(m),
            "workhead": workhead_html(m), "bar": bar_html(m),
            "experience": experience_html(m), "gaps": gaps_html(m),
            "notes": notes_html(m)}


def render_prepare_inner(conn, item_id: int, config: dict, notices=None,
                         running: bool = False) -> str | None:
    """The page's parts as the answer to a press carries them, read only.

    Each part is a <template data-part> holding what goes into its slot; the
    page script moves them there. running is kept for old callers and does
    nothing: no page starts work on its own. None for no such role.
    """
    del running
    m = model(conn, item_id, config, notices)
    if m is None:
        return None
    return "".join(f'<template data-part="{k}">{v}</template>' for k, v in parts(m).items())


def editor_pane(m: dict) -> str:
    text = m["shown_text"]
    if text is None:
        return '<p class="small pa-nocv">No tailored CV was made for this role.</p>'
    copy = m["copy"]
    saved = None
    if copy is not None and copy.get("saved_text"):
        saved = {"name": copy.get("saved_name"), "at": copy.get("saved_at"),
                 "same": copy.get("saved_hash") == cv_store.cv_hash(text)}
    return cv_page.editor_html(text, {
        "mode": "tailored", "id": m["id"], "editable": m["can_edit"] and not m["unlaid"],
        "needsCopy": m["needs_copy"], "saved": saved,
        "hash": copy.get("cv_hash") if copy is not None else None})


def legacy_tokens() -> str:
    """The sizes the editor's stylesheet is written in (--space-*, --text-*
    and the rest), from the older dashboard stylesheet's own :root, without
    its light colours: the colours are the frame's, and the paper's."""
    root = bd.CSS[bd.CSS.index(":root{"):]
    root = root[:root.index("}")]
    keep = re.findall(r"(--(?:space|text|weight|radius|ease|dur|tracking|font-serif|page)"
                      r"[\w-]*\s*:[^;]+;)", root)
    return ":root{" + "".join(keep) + "}"


def render_prepare_page(conn, item_id: int, config: dict, db_label: str = "",
                        go: bool = False) -> str | None:
    """The Prepare application page, or None when there is no such role.

    Drawing it writes nothing and spends nothing, however it was opened: go,
    the Jobs link's old ?go=1, is taken and ignored, so an old bookmark cannot
    start a fetch. The editor's script makes one POST of its own, the first
    time the page of an open role opens, which makes the role's tailored copy
    from the master: a write that costs nothing, no model is asked anything.
    Every other write is a button press.
    """
    del go, db_label
    m = model(conn, item_id, config)
    if m is None:
        return None
    p = parts(m)
    row = m["row"]
    title = row.get("title") or "Untitled role"

    def slot(k, tag="div", cls="", extra=""):
        return f'<{tag} class="{cls}" data-slot="{k}" {extra}>{p[k]}</{tag}>'

    editable = m["can_edit"] and not m["unlaid"] and m["shown_text"] is not None
    save = ('<button type="button" class="btn btn-plain sec-link" data-cv-act="save" disabled>Save copy</button>'
            if editable else "")
    xp_slot = slot("experience", "div", "pa-xpslot" + ("" if p["experience"] else " is-empty"))
    if m["unlaid"] and m["copy"] is None:
        # No copy can be made until the master is in the editor's layout.
        cv_body = ('<p class="notice warn pa-unlaid">Save your master CV on the CV page to make a copy '
                   'for this role. <a class="plain" href="/cv">Open CV</a></p>' + xp_slot)
    else:
        cv_body = f"""{slot("workhead", "div", "work-head")}
{slot("bar", "div", "pa-bar" + ("" if p["bar"] else " is-empty"), 'role="group" aria-label="Tailored CV"')}
<p class="cv-note notice-line" id="cv-say"></p>
{slot("gaps", "section", "gaps" + ("" if p["gaps"] else " is-empty"), 'aria-label="Gaps"')}
{xp_slot}
<div class="sheet-wrap">
{slot("notes", "div", "pa-notes", 'aria-label="Margin notes"')}
<div class="gut" aria-hidden="true"></div>
<div class="sheet-paper pa-sheet" id="cv-pane" style="{design_style(m['design'])}" data-design="{esc(json.dumps(m['design']))}">{editor_pane(m)}</div>
</div>"""

    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prepare application, {esc(title)}, MedTech Radar</title>
<link rel="icon" href="{bd.FAVICON}">
<style>{page_frame.FRAME_CSS}{legacy_tokens()}{page_frame.GROUPS_CSS}{page_frame.TITLES_CSS}{tiers.PILL_CSS}{PREPARE_CSS}{cv_page.CV_CSS}{SHEET_CSS}</style>
</head>
<body>
{page_frame.appbar_html("jobs", rc.job_health(conn), "Prepare application")}
<main class="pa" id="main" data-id="{m['id']}" data-applied="{1 if m['applied'] else 0}" data-editable="{1 if m['can_edit'] else 0}">
<p class="pa-back"><a class="plain" href="/jobs">Back to Jobs</a></p>
{slot("head", "header", "card g-jobs glow-head pa-head")}
{slot("score", "section", "pa-gates", 'aria-labelledby="score-h"')}
<div class="pa-cols">
<section class="pa-col" aria-labelledby="adv-h">
<header class="sec-head">{page_frame.chip("list", "jobs")}<h2 class="sec-title" id="adv-h">Job description</h2></header>
<div class="card g-jobs glow-faint pa-card">{slot("advert", "div", "advert")}</div>
</section>
<section class="pa-col pa-cv" aria-labelledby="cv-h">
<header class="sec-head">{page_frame.chip("cv", "cv")}<h2 class="sec-title" id="cv-h">Tailored CV</h2>{save}</header>
<div class="card g-cv glow-faint pa-card pa-cvcard">
{cv_body}
</div>
</section>
</div>
<p class="visually-hidden" id="pa-live" role="status" aria-live="polite" aria-atomic="true"></p>
<div id="prepare-body" hidden></div>
<dialog class="pa-dialog card" id="pa-apply-dialog" aria-labelledby="pa-dialog-h" aria-describedby="pa-dialog-p">
<h2 id="pa-dialog-h">Mark applied</h2>
<p id="pa-dialog-p"></p>
<p class="row-btns"><button type="button" class="btn btn-solid" data-pa-dlg="save">Save this copy, then mark applied</button>
<button type="button" class="btn" data-pa-dlg="apply">Mark applied</button>
<button type="button" class="btn quiet" data-pa-dlg="cancel">Cancel</button></p>
</dialog>
<noscript><p class="notice bad">This page needs JavaScript to fetch, score and edit.</p></noscript>
</main>
<script>{bd.SCRIPT}{page_frame.FRAME_SCRIPT}{PREPARE_SCRIPT}{cv_page.CV_MODEL_JS}{cv_page.CV_EDITOR_JS}</script>
</body>
</html>"""


# ------------------------------------------------------------- the style

PREPARE_CSS = r"""
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--canvas);scroll-padding-top:104px}
body{margin:0;background:var(--canvas);color:var(--text-1);font:400 15px/24px var(--font-sans);-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a{color:var(--accent)}
a:hover{color:var(--accent-hover)}
.pa h1,.pa h2,.pa h3,.pa p,.pa ol,.pa ul,.pa dl,.pa dd{margin:0}
.pa ol,.pa ul{padding:0;list-style:none}
.pa input,.pa textarea{font:inherit}
:root{--note-w:440px;--gut:28px}
/* The shared content box, page_frame's --frame-max and --frame-pad. */
.pa{padding:16px var(--frame-pad) 48px;max-width:var(--frame-max);margin-inline:auto}
.pa h2{font:600 16px/24px var(--font-sans);color:var(--text-1)}
.pa h3{font:600 14px/20px var(--font-sans);color:var(--text-1)}
.pa .small{font-size:14px;line-height:20px;color:var(--text-2)}
.card{min-width:0;border:1px solid var(--border-subtle);border-radius:14px;background-color:var(--surface)}
a.plain{color:var(--text-1);text-decoration:underline;text-decoration-color:#5b6770;text-decoration-thickness:1px;text-underline-offset:3px}
a.plain:hover{color:var(--text-1);text-decoration-color:var(--text-1)}
.pa-back{margin-bottom:12px;font-size:14px;line-height:20px}
/* ---- the header card ---- */
.pa-head{padding:20px 24px}
.ph-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;min-width:0}
.ph-top h1{font:600 24px/32px var(--font-sans);color:var(--text-1);overflow-wrap:anywhere;min-width:0}
.ph-acts{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin-left:auto;font-size:14px;line-height:20px}
.ph-facts{display:flex;flex-wrap:wrap;align-items:center;row-gap:4px;margin-top:8px;font-size:14px;line-height:20px;color:var(--text-2)}
.pa-app{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin-top:16px;padding-top:16px;border-top:1px solid var(--border-subtle);font-size:14px;line-height:20px}
.pa-app-l{font-weight:600;color:var(--text-1)}
.stages{display:flex;flex-wrap:wrap;gap:8px 20px;color:var(--text-2)}
.stages li{display:inline-flex;align-items:center;gap:8px}
.stages .dot{width:10px;height:10px;border-radius:50%;border:1.5px solid var(--text-3)}
.stages li.is-reached{color:var(--text-1)}
.stages li.is-reached .dot{background:var(--group-jobs);border-color:var(--group-jobs)}
.stages li[aria-current]{font-weight:600}
.pa-board{margin-left:auto}
.pa-app-say{font-size:14px;line-height:20px;color:var(--text-2)}
.pa-app-say:empty{display:none}
.stchip{display:inline-flex;align-items:center;height:24px;padding:0 10px;border-radius:12px;font-size:13px;line-height:20px;font-weight:500;background:#2a3238;color:var(--text-1)}
/* ---- buttons ---- */
.btn{display:inline-flex;align-items:center;gap:8px;height:36px;padding:0 14px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface);color:var(--text-1);font:500 14px/20px var(--font-sans);text-decoration:none;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--row-hover);color:var(--text-1)}
.btn .cost{color:var(--text-2);font-weight:400}
/* The page's one solid button, Mark applied, in the Jobs purple. */
.btn-solid{background:#a79cf0;border-color:#a79cf0;color:#15122e;font-weight:600}
.btn-solid:hover{background:#bab1f4;border-color:#bab1f4;color:#15122e}
.btn-plain{background:transparent}
.btn[disabled],.btn[aria-disabled="true"]{background:transparent;border-color:var(--border-subtle);color:var(--text-3);cursor:not-allowed}
.btn[disabled] .cost,.btn[aria-disabled="true"] .cost{color:var(--text-3)}
.btn.quiet{border-color:transparent;background:transparent;color:var(--text-2);padding:0 8px}
.btn-sm{display:inline-flex;align-items:center;height:28px;padding:0 10px;border:1px solid var(--border-strong);border-radius:6px;background:transparent;color:var(--text-2);font:500 13px/20px var(--font-sans);cursor:pointer;text-decoration:none}
.btn-sm[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}
.link{color:var(--text-1);text-decoration:underline;text-decoration-color:#5b6770;text-underline-offset:3px}
/* ---- notices: a full border in the status colour, no edge bar ---- */
.notice{margin:12px 0 0;padding:10px 14px;border:1px solid var(--border-strong);border-radius:10px;background:var(--surface);font-size:14px;line-height:20px;color:var(--text-2)}
.notice:empty{display:none}
.notice b{color:var(--text-1)}
.notice.bad{border-color:var(--negative);background:var(--negative-tint)}
.notice.ok{border-color:var(--positive);background:var(--positive-tint)}
.notice.work{border-color:var(--accent);background:var(--accent-tint)}
.notice.warn{border-color:var(--caution);background:var(--caution-tint)}
.notice .btn{margin-top:8px}
.notice p + p{margin-top:4px}
#cv-say{margin:0;padding:8px 12px;border:1px solid var(--positive);border-radius:10px;background:var(--positive-tint);font-size:14px;line-height:20px;color:var(--text-1);min-height:0}
#cv-say:empty{display:none}
#cv-say.bad{border-color:var(--negative);background:var(--negative-tint);color:var(--text-1)}
.row-btns{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px}
/* ---- the four gates: one row, two by two under 900px, one card of rows under 600px ---- */
.pa-gates{margin-top:16px}
.gate-tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
@media (max-width:899.98px){.gate-tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
.gate{--st:232 237 240;display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-areas:"chip name" "chip status" "fig fig" "note note";column-gap:12px;align-content:start;min-width:0;padding:14px;border:1px solid var(--border-subtle);border-radius:12px;background-color:var(--surface)}
.gate.is-pos{--st:116 211 155}
.gate.is-cau{--st:240 196 106}
.gate.is-neg{--st:255 155 143}
.gate-glow{background-image:radial-gradient(150% 170% at 0% 0%,rgb(var(--st) / .28) 0%,rgb(var(--st) / .10) 45%,rgb(var(--st) / 0) 100%)}
.gate-chip{grid-area:chip;display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:8px;background:rgb(var(--st) / .16)}
.gate-chip.is-pos{color:var(--positive)}.gate-chip.is-neg{color:var(--negative)}.gate-chip.is-cau{color:var(--caution)}
.gate-chip.is-neu{color:var(--text-2)}
.gate-chip svg{display:block}
.gate-name{grid-area:name;font-size:15px;line-height:20px;font-weight:600;color:var(--text-1)}
.gate-status{grid-area:status;font-size:14px;line-height:20px;font-weight:500}
.gate-status.is-pos{color:var(--positive)}.gate-status.is-neg{color:var(--negative)}.gate-status.is-cau{color:var(--caution)}
.gate-status.is-neu{color:var(--text-2)}
.gate-fig{grid-area:fig;display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:10px}
.gate-num{font:600 28px/36px var(--font-sans);color:var(--text-1);font-variant-numeric:tabular-nums}
.gate-num.none{font-size:15px;line-height:36px;font-weight:500;color:var(--text-2)}
.src-pill,.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border:1px solid #2a3238;border-radius:12px;background:#1a2025;font-size:13px;line-height:20px;font-weight:500;color:var(--text-2);white-space:nowrap}
.pill .num{color:var(--text-1);font-weight:600}
.pill.is-over{border-color:rgba(240,196,106,.5);background:var(--caution-tint);color:var(--caution)}
.gate-note{grid-area:note;margin-top:10px;font-size:14px;line-height:20px;color:var(--text-2);text-wrap:pretty;overflow-wrap:anywhere}
@media (forced-colors:active){.gate-chip{border:1px solid CanvasText}}
/* Under 600px the four gates are one card of compact rows, hairlines between:
   icon and name at the left, the status at the right, the reason under. */
@media (max-width:599.98px){
.gate-tiles{display:block;border:1px solid var(--border-subtle);border-radius:12px;background-color:var(--surface);overflow:hidden}
.gate{grid-template-columns:auto minmax(0,1fr) auto auto;grid-template-areas:"chip name num status" "note note note note" "pill pill pill pill";column-gap:0;align-items:center;padding:12px 14px;border:0;border-radius:0;background-color:transparent}
.gate + .gate{border-top:1px solid var(--border-subtle)}
.gate-glow{background-image:none}
.gate-chip{margin-right:12px}
.gate-status{margin-left:12px;text-align:right}
.gate-fig{display:contents}
.gate-num{grid-area:num;margin-left:12px;font:600 17px/20px var(--font-sans)}
.gate-num.none{font-size:14px;line-height:20px}
.gate-fig .src-pill{grid-area:pill;justify-self:start;margin-top:8px}
.gate-note{margin-top:6px}
}
.pa-why{margin-top:12px;padding:0 16px;font-size:14px;line-height:20px;color:var(--text-2)}
.pa-why summary{display:flex;align-items:center;min-height:44px;cursor:pointer;color:var(--text-1);font-weight:600}
.pa-why-in{padding-bottom:12px}
.pa-why h3{margin:8px 0 4px}
.pa-why .reasons{display:grid;gap:4px;padding-left:18px;list-style:disc}
.pa-why .detail-label{display:none}
.pa-why .row-sub{display:block;font-size:13px;line-height:20px;color:var(--text-3)}
/* ---- two columns: the description and the tailored CV ---- */
.pa-cols{display:grid;grid-template-columns:minmax(0,1fr);gap:32px;margin-top:32px;align-items:start}
@media (min-width:1024px){.pa-cols{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
.pa-col{min-width:0}
.pa-card{padding:16px 20px 20px}
.pa-cv .sec-head .btn{height:32px}
.pa-strip{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding-bottom:12px;margin-bottom:12px;border-bottom:1px solid var(--border-subtle);font-size:14px;line-height:20px;color:var(--text-2)}
.strip-t{color:var(--text-1);font-weight:500}
.fetch{display:inline-flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin-left:auto}
.fetch .cost{font-size:13px;line-height:20px;color:var(--text-2)}
.tally{color:var(--text-2)}
.tally b{color:var(--text-1);font-weight:600}
.tally [data-pa-count="stale"]{color:var(--negative)}
.none{color:var(--text-2);font-size:14px;line-height:20px}
.work-head .notice{margin:0 0 12px}
.pa-bar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:12px;padding:8px;border:1px solid var(--border-subtle);border-radius:10px;background:var(--surface)}
.pa-bar.is-empty{display:none}
.save-as{flex-basis:100%;margin:0 4px;font-size:13px;line-height:20px;color:var(--text-3)}
.save-as b{color:var(--text-2);font-weight:500}
.gaps{margin-bottom:12px;padding:12px 16px;border:1px dashed var(--negative);border-radius:10px;background:var(--negative-tint)}
.gaps.is-empty{display:none}
/* From your experience, in rose: a fold above the sheet (owner, 18 Nov). */
.pa-xpslot{margin:0 0 16px}
.pa-xpslot.is-empty{display:none}
.pa-xp{border:1px solid rgb(var(--group-cv-rgb) / .40);border-radius:10px;background:rgb(var(--group-cv-rgb) / .06);padding:0 16px}
.pa-xp summary{display:flex;align-items:center;gap:10px;min-height:44px;cursor:pointer;color:var(--group-cv);font-size:15px;line-height:24px;font-weight:600}
.pa-xp .xp-n{display:inline-flex;align-items:center;height:22px;padding:0 8px;border:1px solid rgb(var(--group-cv-rgb) / .45);border-radius:11px;background:rgb(var(--group-cv-rgb) / .12);font-size:13px;line-height:20px;font-weight:500;color:var(--group-cv);font-variant-numeric:tabular-nums}
.pa-xp .xp-rows{display:flex;flex-direction:column;gap:12px;margin:0;padding:4px 0 16px;list-style:none}
.pa-xp .xp-row{display:flex;flex-direction:column;gap:6px;min-width:0}
.pa-xp .xp-link{font-size:15px;line-height:24px;font-weight:500;overflow-wrap:anywhere}
.pa-xp .chips{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.pa-xp .chip-v{padding:5px 12px;border-radius:8px;background:#232a30;color:var(--text-1);font-size:14px;line-height:20px;overflow-wrap:anywhere}
.pa-xp .xp-all{margin:-8px 0 12px}
.pa-xp .btn-text{min-height:40px;padding:0;border:0;background:none;color:var(--text-1);font:500 14px/20px var(--font-sans);text-decoration:underline;text-decoration-color:#5b6770;text-underline-offset:3px;cursor:pointer}
.gaps ol{display:grid;gap:12px;margin-top:8px}
.gaps ol + h3{margin-top:16px}
.pa-unlaid{margin:0}
/* ---- the sheet and its notes: beside it when the CV card is wide enough,
   under each line when it is not (the script reads the wrapper's display) ---- */
.pa-cvcard{container:pacv / inline-size}
.sheet-wrap{position:relative;display:grid;grid-template-columns:minmax(0,1fr) var(--gut) var(--note-w);align-items:start;--page-canvas:var(--canvas);--page-text-3:var(--text-3);--page-caution:var(--caution)}
.pa-sheet{grid-column:1;grid-row:1;border-radius:var(--radius-2);min-width:0}
.pa-sheet .cv-lines{margin:0}
.gut{grid-column:2;grid-row:1}
.pa-notes{grid-column:3;grid-row:1;position:relative;display:grid;gap:6px;align-content:start;min-width:0}
.pa-js .pa-notes{display:block;align-self:stretch}
.pa-js .pa-notes > .notes{position:absolute;left:0;right:0}
.notes{position:relative;display:grid;gap:6px;align-content:start;padding:0 0 4px}
.notes::before{content:"";position:absolute;left:calc(-1 * var(--gut));top:12px;width:calc(var(--gut) - 4px);border-top:1px solid var(--border-strong)}
.notes-empty{padding:8px 12px;border:1px dashed var(--border-strong);border-radius:8px;font-size:14px;line-height:20px;color:var(--text-2)}
@container pacv (max-width:999.98px){
  .sheet-wrap{display:block}
  .gut{display:none}
  .pa-notes{display:grid;gap:8px;margin-top:12px}
  .pa-js .pa-notes{position:absolute;inset:0;z-index:2;margin:0;pointer-events:none}
  .pa-js .pa-notes > *{pointer-events:auto}
  .pa-js .pa-notes > .notes{right:8px}
  .notes::before{display:none}
  .notes-empty{margin-top:12px}
  .pa-js .pa-notes > .notes-empty{display:none}
  .note,.note.is-closed{box-shadow:var(--shadow-overlay)}
  .note.is-closed{background:var(--canvas)}
}
/* A note: its status in words and a full border, never an edge bar. */
.note{padding:6px 10px 8px;border:1px solid var(--border-strong);border-radius:8px;background:var(--canvas);font-size:14px;line-height:20px}
.note.st-met{border-color:rgba(116,211,155,.45)}
.note.st-partly{border-color:rgba(240,196,106,.5)}
.note.st-gap{border-color:rgba(255,155,143,.55);border-style:dashed}
.note.st-stale{border-color:var(--negative);border-style:double;border-width:3px}
.note.is-closed{padding:0;background:transparent}
.note.is-selected{background:var(--row-selected);outline:1px solid var(--accent)}
.note .head{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline}
.note-toggle{display:flex;width:100%;align-items:baseline;gap:8px;padding:2px 10px;border:0;background:transparent;color:var(--text-2);font:400 14px/20px var(--font-sans);text-align:left;cursor:pointer}
.note-toggle:hover{background:var(--row-hover)}
.note-toggle .chev{margin-left:auto;color:var(--text-3)}
.note-toggle[aria-expanded="true"] .chev{transform:rotate(90deg)}
.note.is-open .note-toggle{padding:0}
.note.st-met.is-open{padding:6px 10px 8px;background:var(--canvas)}
.note.st-met.is-open.is-selected{background:var(--row-selected)}
.note-body{display:grid;gap:2px;margin-top:2px}
.note .id{font:500 13px/20px var(--font-mono);color:var(--text-3)}
.note .lines{color:var(--text-3)}
.mark{font-weight:600;white-space:nowrap}
.mark.met{color:var(--positive)}.mark.partly{color:var(--caution)}.mark.gap,.mark.stale{color:var(--negative)}
.note q{display:block;color:var(--text-1);quotes:"\201C" "\201D"}
.note .ask-words{display:block;color:var(--text-1)}
.note .cite{--dv-space:8px;font-size:13px;line-height:20px;color:var(--text-3)}
.note .cite a{color:var(--text-2)}
.note .gapwhy{display:block;color:var(--text-2)}
.note .when{color:var(--text-3)}
.note .was{display:inline-block;padding:0 4px;border:1px solid var(--border-strong);border-radius:var(--radius-2);font-size:13px;line-height:20px;color:var(--text-2)}
.note .btn-sm{justify-self:start;margin-top:4px}
.gaps .note{background:var(--canvas)}
/* ---- the advert ---- */
.adv h3{margin:16px 0 4px}
.adv p,.adv li{font-size:15px;line-height:24px;color:var(--text-2)}
.adv p + p{margin-top:8px}
.adv ul{display:grid;gap:4px;padding-left:18px;list-style:disc}
.adv .ask{color:var(--text-1);text-decoration-line:underline;text-decoration-thickness:2px;text-underline-offset:4px}
.adv .ask.met{text-decoration-style:solid;text-decoration-color:var(--positive)}
.adv .ask.partly{text-decoration-style:dashed;text-decoration-color:var(--caution)}
.adv .ask.gap{text-decoration-style:dotted;text-decoration-color:var(--negative)}
.adv .ask.sel,.adv .rate.sel{background:var(--row-selected)}
.adv .tagmark{font:500 13px/20px var(--font-mono);white-space:nowrap}
.adv .tagmark.met{color:var(--positive)}.adv .tagmark.partly{color:var(--caution)}.adv .tagmark.gap{color:var(--negative)}
.adv .rate{color:var(--text-1);background:var(--accent-tint);box-shadow:inset 0 -2px 0 var(--accent)}
.advert-rest{margin-top:16px;border-top:1px solid var(--border-subtle)}
.advert-rest summary{display:flex;align-items:center;min-height:44px;cursor:pointer;color:var(--text-1);font-size:14px;font-weight:500}
/* ---- Mark applied, a shared card ---- */
.pa-dialog{max-width:min(560px,calc(100vw - 32px));padding:24px;color:var(--text-1);box-shadow:var(--shadow-overlay)}
.pa-dialog::backdrop{background:rgba(0,0,0,.6)}
.pa-dialog h2{margin-bottom:8px;font-size:20px;line-height:28px}
.pa-dialog p + p{margin-top:16px}
.pa-dialog > p:not(.row-btns){font-size:14px;line-height:20px;color:var(--text-2)}
.pa-dialog > p:empty{display:none}
.pa-dialog .btn{height:auto;min-height:36px;white-space:normal}
@media (pointer:coarse){.pa .btn,.pa .btn-sm,.pa summary,.note-toggle{min-height:44px}.pa .btn-sm{height:auto}}
@media (max-width:767.98px){
  html{scroll-padding-top:16px;scroll-padding-bottom:136px}
  .pa-head{padding:16px}
  .ph-top h1{font-size:20px;line-height:28px;flex-basis:100%}
  .ph-acts{margin-left:0}
  .pa-board{margin-left:0}
  .pa-card{padding:12px 16px 16px}
  .fetch{margin-left:0}
  .pa-bar{top:auto;bottom:0;margin:0 -16px 12px;border-radius:0;border-width:1px 0}
  .pa-bar .btn{flex:1 1 0;justify-content:center;height:auto;min-height:52px;padding:4px 6px;white-space:normal;text-align:center}
  .pa-bar .quiet{display:none}
  .pa-bar .save-as{display:none}
  .btn{height:44px}
  .btn-sm{height:32px}
  .note-toggle{min-height:44px;align-items:center}
}
@media (forced-colors:active){.note,.pill,.src-pill{border:1px solid CanvasText}.cvl.pa-lit{outline:2px solid Highlight}}
"""

# The sheet: the editor inside the paper. Nothing here restyles the editor's
# lines; it gives a selected note's cited lines their tint.
SHEET_CSS = r"""
.pa-sheet .cvl.pa-lit{background:var(--sheet-accent-wash);box-shadow:inset 3px 0 0 var(--sheet-accent)}
/* The sheet as the CV page draws it (cv_screen .sheet): the CV page's Design,
   font, size, spacing and margins, set on #cv-pane as --cv-font, --cv-scale,
   --cv-gap and --sheet-pad-*, the numbers in the margin, the headings in the
   Word file's sizes. */
.pa-sheet{padding:var(--sheet-pad-top,68px) var(--sheet-pad-side,76px) 48px;font:400 calc(14px * var(--cv-scale,1))/1.43 var(--cv-font,Arial),"Helvetica Neue",Helvetica,sans-serif}
.pa-sheet .cvl{display:grid;grid-template-columns:1em minmax(0,1fr) auto;gap:0 4px;position:relative;padding:0;border-radius:0}
.pa-sheet .cvl-n{position:absolute;left:-56px;width:44px;text-align:right;font:400 11px/20px var(--font-mono);color:var(--sheet-ink-3)}
.pa-sheet .cvl-mark{grid-column:1;font:inherit;color:var(--sheet-ink-2)}
.pa-sheet .cvl-t{grid-column:2;font:inherit;line-height:inherit;color:var(--sheet-ink-1)}
.pa-sheet .cvl-ops{grid-column:3}
.pa-sheet .cvl-plain .cvl-mark,.pa-sheet .cvl-h1 .cvl-mark,.pa-sheet .cvl-h2 .cvl-mark,.pa-sheet .cvl-h3 .cvl-mark{display:none}
.pa-sheet .cvl-plain .cvl-t,.pa-sheet .cvl-h1 .cvl-t,.pa-sheet .cvl-h2 .cvl-t,.pa-sheet .cvl-h3 .cvl-t{grid-column:1 / 3}
.pa-sheet .cvl-h1 .cvl-t{font:700 calc(24px * var(--cv-scale,1))/1.33 var(--cv-font,Arial),sans-serif;letter-spacing:0;text-transform:none}
.pa-sheet .cvl-h1 .cvl-n{line-height:32px}
.pa-sheet .cvl-h2{margin-top:calc(10px * var(--cv-gap,1))}
.pa-sheet .cvl-h2 .cvl-t{font:700 calc(17px * var(--cv-scale,1))/1.4 var(--cv-font,Arial),sans-serif;letter-spacing:0;text-transform:none;color:var(--sheet-ink-1)}
.pa-sheet .cvl-h3{margin-top:calc(6px * var(--cv-gap,1))}
.pa-sheet .cvl-h3 .cvl-t{font-weight:700}
.pa-sheet .cvl-blank{min-height:0;height:calc(4px * var(--cv-gap,1))}
.pa-sheet .cvl-blank .cvl-n{display:none}
.pa-sheet .cv-head .cvl-plain .cvl-t{color:var(--sheet-ink-2)}
.pa-sheet textarea.cvl-t{padding:0 5px;margin:0 -6px;min-height:0;line-height:inherit}
/* A focused field on the paper shows the CV page's ring, which the editor's
   own textarea.cvl-t:focus{outline:none} would otherwise take away. */
.pa-sheet textarea.cvl-t:focus{outline:2px solid var(--sheet-focus);outline-offset:1px;border-color:var(--sheet-rule-strong);background:var(--sheet-surface)}
.pa-sheet .cvl-ops button:focus-visible,.pa-sheet .cv-add button:focus-visible{outline:2px solid var(--sheet-focus);outline-offset:1px}
.pa-sheet .cvl-ops button,.pa-sheet .cv-add button{min-height:24px;color:var(--sheet-ink-2);background:var(--sheet-surface);border-color:var(--sheet-rule-strong)}
/* The add chips show only beside the line being edited, as the frames draw:
   the page script marks the chips that belong to the focused line's run. */
.pa-sheet .cv-add{display:none;margin:2px 0 6px 1em}
.pa-sheet .cv-add.pa-add-on{display:flex}
/* The estimated A4 pages of the Word file (cv_doc.estimate_pages and the
   editor's RadarCv.pages), drawn as on the CV page. */
.pa-break{display:flex;align-items:center;gap:12px;margin:20px calc(-1 * var(--sheet-pad-side,76px)) 16px;padding:6px var(--sheet-pad-side,76px);background:var(--page-canvas);color:var(--page-text-3);font:400 12px/16px var(--font-sans)}
.pa-break.is-limit{color:var(--page-caution);font-weight:600}
.pa-break.is-limit::before,.pa-break.is-limit::after{content:"";flex:1;border-top:2px dashed currentColor}
.pa-sheet .cvl.pa-over::before{content:"";position:absolute;left:-62px;top:0;bottom:0;width:3px;background:var(--sheet-caution)}
.pa-pages{margin:8px 0 0;font-size:12px;line-height:16px;color:var(--text-3)}
.pa-pages.is-over{color:var(--caution);font-weight:600}
@media (pointer:coarse){
  /* Touch: a line's buttons are 44px and show only on the line being
     edited, as the frame draws, never on every line at once. */
  .pa-sheet .cvl-ops{display:none;opacity:1}
  .pa-sheet .cvl:focus-within .cvl-ops,.pa-sheet .cvl.pa-on .cvl-ops{display:flex}
  .pa-sheet .cvl-ops button,.pa-sheet .cv-add button{min-height:44px;min-width:44px}
}
@media (max-width:767.98px){
  .pa-sheet{padding:20px 14px 20px 40px}
  .pa-break{margin:16px -14px 12px -40px;padding:6px 14px 6px 40px}
  .pa-sheet .cvl{grid-template-columns:1em minmax(0,1fr)}
  .pa-sheet .cvl-n{left:-36px;width:28px;font-size:10px}
  .pa-sheet .cvl-ops{grid-column:1 / 3;justify-content:flex-end;display:none;opacity:1}
  .pa-sheet .cvl:focus-within .cvl-ops,.pa-sheet .cvl.pa-on .cvl-ops{display:flex}
  .pa-sheet .cvl.pa-over::before{left:-38px}
}
"""


# ------------------------------------------------------------ the script

PREPARE_SCRIPT = r"""
(function () {
  var main = document.getElementById('main');
  if (!main || !main.hasAttribute || !main.hasAttribute('data-id')) return;
  var id = Number(main.getAttribute('data-id'));
  var carrier = document.getElementById('prepare-body');
  var root = document.documentElement;
  if (root && root.classList) root.classList.add('pa-js');
  var busy = false;
  var pendingApply = false;
  var loaded = null;              // the lines on screen as the page took them

  function all(sel, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(sel));
  }
  // Out of its parent, in any document: one with remove, one with only
  // removeChild, and the test runners' small stand-in, which keeps a list.
  function detach(el) {
    var parent = el && el.parentNode;
    if (!parent) return;
    if (parent.removeChild) { parent.removeChild(el); return; }
    if (parent.children && parent.children.indexOf) {
      var i = parent.children.indexOf(el);
      if (i >= 0) parent.children.splice(i, 1);
      el.parentNode = null;
    }
  }
  function slot(name) { return document.querySelector('[data-slot="' + name + '"]'); }
  // The notes sit under their lines when the CV card is too narrow for them
  // beside the sheet: the stylesheet's container query says so by laying
  // the sheet's wrapper out as a block.
  function under() {
    var w = document.querySelector('.sheet-wrap');
    if (!w || !window.getComputedStyle) return false;
    return window.getComputedStyle(w).display === 'block';
  }

  // ------------------------------------------------- the lines on screen
  function lineEls() { return all('#cv-editor .cvl[data-n]'); }
  function lineText(el) {
    var t = el.querySelector('.cvl-t');
    if (!t) return '';
    return t.tagName === 'TEXTAREA' ? String(t.value) : String(t.textContent || '');
  }
  function keyOf(text) {
    if (window.RadarCv && window.RadarCv.key) return window.RadarCv.key(text);
    return String(text || '').replace(/^#{1,6}\s+/, '').replace(/^[-*•]\s+/, '')
      .split(/\s+/).filter(Boolean).join(' ');
  }
  function snapshot() {
    var out = [];
    lineEls().forEach(function (el) {
      out[Number(el.getAttribute('data-n')) - 1] = keyOf(lineText(el));
    });
    for (var i = 0; i < out.length; i++) if (out[i] === undefined) out[i] = '';
    return out;
  }
  function lineEl(n) {
    return document.querySelector('#cv-editor .cvl[data-n="' + n + '"]');
  }
  function find(cite, keys) {
    if (window.RadarCv && window.RadarCv.findLine) {
      return window.RadarCv.findLine({ n: cite.n, text: cite.t, cut: !!cite.cut }, keys);
    }
    var k = keyOf(cite.t);
    if (keys[cite.n - 1] === k) return cite.n;
    var i = keys.indexOf(k);
    return i >= 0 ? i + 1 : null;
  }
  function edited() {
    if (main.getAttribute('data-editable') !== '1') return false;
    if (window.RadarCvEditor && window.RadarCvEditor.edited) return !!window.RadarCvEditor.edited();
    return loaded !== null && JSON.stringify(snapshot()) !== JSON.stringify(loaded);
  }

  // --------------------------------- the notes: where their lines are now
  function cites(note) {
    try { return JSON.parse(note.getAttribute('data-cites') || '[]'); } catch (e) { return []; }
  }
  function refind() {
    var keys = snapshot();
    var counts = { met: 0, partly: 0, missing: 0, stale: 0 };
    all('[data-note]').forEach(function (note) {
      var status = note.getAttribute('data-status');
      var list = cites(note);
      var nows = [];
      var gone = [];
      list.forEach(function (c) {
        var now = keys.length ? find(c, keys) : c.n;
        if (now) nows.push(now); else gone.push(c.n);
      });
      var stale = (status === 'met' || status === 'partly') && list.length > 0 && gone.length > 0;
      if (list.length) note.setAttribute('data-line', String(nows.length ? nows[0] : list[0].n));
      note.setAttribute('data-now', nows.join(','));
      var lines = note.querySelector('.lines');
      if (lines && list.length) {
        var shown = nows.length ? nows : list.map(function (c) { return c.n; });
        lines.textContent = (shown.length === 1 ? 'line ' : 'lines ') + shown.join(', ');
      }
      all('[data-pa-goto]', note).forEach(function (a, i) {
        var now = list[i] ? find(list[i], keys) : null;
        var to = now || (list[i] ? list[i].n : null);
        if (to) { a.setAttribute('data-pa-goto', String(to)); a.textContent = String(to); }
        var sibs = a.parentNode ? Array.prototype.slice.call(a.parentNode.children || []) : [];
        var when = a.nextElementSibling || sibs[sibs.indexOf(a) + 1];
        if (when && when.hasAttribute && when.hasAttribute('data-pa-when')) {
          when.hidden = !now || String(now) === when.getAttribute('data-pa-when');
          when.textContent = when.hidden ? '' : ' (' + when.getAttribute('data-pa-when') + ' when scored)';
        }
      });
      var mark = note.querySelector('.mark');
      var was = note.getAttribute('data-stale') === '1' || (mark && mark.classList.contains('stale'));
      if (mark && stale !== was) {
        if (stale) {
          mark.className = 'mark stale';
          mark.textContent = '! Stale, was ' + status;
          note.classList.add('st-stale');
          var words = note.querySelector('[data-pa-stale-words]');
          if (!words) {
            words = document.createElement('span');
            words.className = 'gapwhy';
            words.setAttribute('data-pa-stale-words', '');
            var body = note.querySelector('.note-body');
            if (body) body.appendChild(words);
          }
          words.textContent = 'CV line ' + gone.join(', ') + ' when scored, since changed.';
        } else {
          mark.className = 'mark ' + (status === 'met' ? 'met' : 'partly');
          mark.textContent = status === 'met' ? 'Met' : 'Partly';
          note.classList.remove('st-stale');
          var w = note.querySelector('[data-pa-stale-words]');
          if (w) detach(w);
        }
      }
      note.setAttribute('data-stale', stale ? '1' : '0');
      if (stale) counts.stale += 1;
      else if (counts[status] !== undefined) counts[status] += 1;
    });
    var tally = document.querySelector('[data-pa-tally]');
    if (tally) {
      var put = function (k, words) {
        var el = tally.querySelector('[data-pa-count="' + k + '"]');
        if (el) el.textContent = words;
      };
      put('met', counts.met + ' met');
      put('partly', counts.partly + ' partly');
      put('missing', counts.missing + (counts.missing === 1 ? ' gap' : ' gaps'));
      put('stale', counts.stale + ' stale');
      var st = tally.querySelector('[data-pa-count="stale"]');
      if (st) st.parentNode.hidden = counts.stale === 0;
    }
    var band = document.getElementById('copy-edited-note');
    if (band) {
      band.hidden = !(edited() || band.getAttribute('data-pa-earlier') === '1');
      var c = band.querySelector('[data-pa-counts]');
      if (c) {
        c.textContent = counts.met + ' met, ' + counts.partly + ' partly, ' + counts.missing
          + (counts.missing === 1 ? ' gap' : ' gaps')
          + (counts.stale ? ', ' + counts.stale + ' stale' : '') + '.';
      }
    }
    regroup();
    return counts;
  }
  // A note sits beside the first line it cites that is still there, so a
  // line moved up or down takes its notes with it.
  function regroup() {
    var layer = slot('notes');
    if (!layer) return;
    var groups = {};
    all('ol.notes[data-pa-group]', layer).forEach(function (g) { groups[g.getAttribute('data-pa-group')] = g; });
    all('ol.notes[data-pa-group] > [data-note]', layer).forEach(function (note) {
      var line = note.getAttribute('data-line');
      if (!line || note.parentNode.getAttribute('data-pa-group') === line) return;
      var g = groups[line];
      if (!g) {
        g = document.createElement('ol');
        g.className = 'notes';
        g.setAttribute('data-pa-group', line);
        g.setAttribute('aria-label', 'Notes beside CV line ' + line);
        layer.appendChild(g);
        groups[line] = g;
      }
      var after = all('[data-note]', g).filter(function (o) {
        return Number(o.getAttribute('data-note')) > Number(note.getAttribute('data-note'));
      })[0];
      detach(note);
      if (after) g.insertBefore(note, after); else g.appendChild(note);
    });
    all('ol.notes[data-pa-group]', layer).forEach(function (g) {
      if (!g.querySelector('[data-note]')) detach(g);
    });
    all('ol.notes[data-pa-group]', layer).sort(function (a, b) {
      return Number(a.getAttribute('data-pa-group')) - Number(b.getAttribute('data-pa-group'));
    }).forEach(function (g) { detach(g); layer.appendChild(g); });
  }

  // ---------------------------------------------- the notes beside lines
  var laying = false;
  function layout() {
    var layer = slot('notes');
    var wrap = layer && layer.parentNode;
    if (!layer || !wrap || !wrap.getBoundingClientRect) return;
    laying = true;
    try {
      lineEls().forEach(function (el) { if (el.style) el.style.marginBottom = ''; });
      var groups = all('ol.notes[data-pa-group]', layer);
      var y = 0;
      if (under()) {
        var box = wrap.getBoundingClientRect();
        groups.forEach(function (g) {
          var el = lineEl(g.getAttribute('data-pa-group'));
          var t = el && el.querySelector('.cvl-t');
          var left = t ? t.getBoundingClientRect().left - box.left : 32;
          g.style.left = Math.max(0, left) + 'px';
          var h = g.offsetHeight || 0;
          if (el) {
            el.style.marginBottom = (h + 12) + 'px';
            var top = el.getBoundingClientRect().bottom - wrap.getBoundingClientRect().top + 4;
            top = Math.max(top, y);
            g.style.top = top + 'px';
            y = top + h + 8;
          } else {
            g.style.top = y + 'px';
            y += h + 8;
          }
        });
        layer.style.minHeight = '';
      } else {
        var colTop = layer.getBoundingClientRect().top;
        groups.forEach(function (g) {
          g.style.left = '';
          var el = lineEl(g.getAttribute('data-pa-group'));
          var top = el ? el.getBoundingClientRect().top - colTop : y;
          top = Math.max(top, y);
          g.style.top = top + 'px';
          y = top + (g.offsetHeight || 0) + 6;
        });
        layer.style.minHeight = y + 'px';
      }
    } finally { laying = false; }
  }
  var queued = false;
  function later(fn) {
    if (window.requestAnimationFrame) window.requestAnimationFrame(fn);
    else Promise.resolve().then(fn);
  }
  function soon() {
    if (queued) return;
    queued = true;
    later(function () { queued = false; refind(); paginate(); layout(); });
  }
  // An edit is followed at once: the marks and the counts change as it is
  // typed, and the notes are laid out again on the next frame.
  function now() { refind(); soon(); }

  // ------------------------------------------- the A4 pages of the sheet
  // Where the Word file's pages would break, estimated from the CV page's
  // Design as the CV page estimates it (RadarCv.pages, the twin of
  // cv_doc.estimate_pages), drawn again as the copy is edited.
  function sheetDesign() {
    var pane = document.getElementById('cv-pane');
    try { return JSON.parse((pane && pane.getAttribute('data-design')) || 'null'); } catch (e) { return null; }
  }
  function pagesWords(pages, d) {
    var n = pages.length;
    if (!n) return '';
    if (window.RadarCv && window.RadarCv.cleanDesign) d = window.RadarCv.cleanDesign(d);
    return n + (n === 1 ? ' page' : ' pages') + ', limit is 2';
  }
  function paginate() {
    all('#cv-editor .pa-break').forEach(detach);
    all('#cv-editor .cvl.pa-over').forEach(function (el) { el.classList.remove('pa-over'); });
    var api = window.RadarCvEditor;
    var C = window.RadarCv;
    if (!api || !api.lines || !C || !C.pages) return null;
    var pages = C.pages(api.lines(), sheetDesign());
    var total = pages.length;
    pages.forEach(function (pg) {
      if (pg.page === 1) return;
      var first = lineEl(pg.first);
      if (!first || !first.parentNode) return;
      var brk = document.createElement('div');
      brk.className = 'pa-break' + (pg.page === 3 ? ' is-limit' : '');
      brk.setAttribute('role', 'separator');
      var words = 'Page ' + (pg.page - 1) + ' of ' + total + ' ends. Page ' + pg.page + ' from line ' + pg.first;
      if (pg.page === 3) words = 'Over the two-page limit. ' + words;
      brk.setAttribute('aria-label', words);
      brk.textContent = words;
      first.parentNode.insertBefore(brk, first);
    });
    if (total > 2) {
      lineEls().forEach(function (r) {
        if (Number(r.getAttribute('data-n')) >= pages[2].first) r.classList.add('pa-over');
      });
    }
    var foot = document.getElementById('pa-pages');
    if (foot) {
      foot.textContent = pagesWords(pages, sheetDesign());
      foot.hidden = !total;
      foot.classList.toggle('is-over', total > 2);
    }
    return pages;
  }

  // ---------------------------------- the add chips beside the edited line
  // As the frames draw: a line's add chips (+ Line, + Bullet, + Entry) show
  // only while a line of their run is being edited, and on touch its own
  // buttons show only on that line (pa-on), never on every line at once.
  function siblingsAfter(el) {
    var parent = el && el.parentNode;
    if (!parent) return [];
    var kids = Array.prototype.slice.call(parent.children || []);
    return kids.slice(kids.indexOf(el) + 1);
  }
  function chipsOf(line) {
    var out = [];
    var rest = siblingsAfter(line);
    for (var i = 0; i < rest.length; i++) {
      var el = rest[i];
      var cls = el.classList;
      if (!cls) break;
      if (cls.contains('cv-add')) { out.push(el); continue; }
      if (out.length) break;
      if (cls.contains('pa-break')) continue;
      if (cls.contains('cvl') && (cls.contains('cvl-plain') || cls.contains('cvl-bullet'))) continue;
      break;
    }
    return out;
  }
  function editHere(line) {
    all('#cv-editor .cv-add.pa-add-on').forEach(function (c) { c.classList.remove('pa-add-on'); });
    all('#cv-editor .cvl.pa-on').forEach(function (c) { if (c !== line) c.classList.remove('pa-on'); });
    if (!line) return [];
    line.classList.add('pa-on');
    var chips = chipsOf(line);
    chips.forEach(function (c) { c.classList.add('pa-add-on'); });
    return chips;
  }
  document.addEventListener('focusin', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    if (t.closest('#cv-editor .cv-add')) return;
    var line = t.closest('#cv-editor .cvl');
    editHere(line && !line.classList.contains('cvl-blank') ? line : null);
  });

  // -------------------------------------------------- selecting a note
  function light(nums) {
    all('#cv-editor .cvl.pa-lit').forEach(function (el) { el.classList.remove('pa-lit'); });
    (nums || []).forEach(function (n) { var el = lineEl(n); if (el) el.classList.add('pa-lit'); });
  }
  function nowOf(note) {
    return String(note.getAttribute('data-now') || '').split(',').filter(Boolean);
  }
  function select(note) {
    all('[data-note].is-selected').forEach(function (n) { if (n !== note) n.classList.remove('is-selected'); });
    if (!note) { light([]); return; }
    note.classList.add('is-selected');
    light(nowOf(note));
  }
  function setOpen(note, open) {
    var btn = note.querySelector('.note-toggle');
    var body = note.querySelector('.note-body');
    if (!btn || !body) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    body.hidden = !open;
    note.classList.toggle('is-open', open);
    note.classList.toggle('is-closed', !open);
    soon();
  }
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    // From your experience: Show all N shows the rows past the first five.
    var xpAll = t.closest('[data-xp-all]');
    if (xpAll) {
      var fold = xpAll.closest('.pa-xp');
      Array.prototype.forEach.call(fold.querySelectorAll('[data-xp-more]'), function (r) { r.hidden = false; });
      var first = fold.querySelector('[data-xp-more] a');
      xpAll.parentNode.parentNode.removeChild(xpAll.parentNode);
      if (first) first.focus();
      return;
    }
    var toggle = t.closest('.note-toggle');
    if (toggle) {
      var note = toggle.closest('[data-note]');
      var open = note.getAttribute('data-by-click') !== '1';
      note.setAttribute('data-by-click', open ? '1' : '0');
      setOpen(note, open);
      select(note);
      return;
    }
    var lightBtn = t.closest('[data-pa-light]');
    if (lightBtn) {
      var n2 = lightBtn.closest('[data-note]');
      var on = lightBtn.getAttribute('aria-pressed') !== 'true';
      all('[data-pa-light][aria-pressed="true"]').forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
      lightBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on) {
        select(n2);
        var first = lineEl(nowOf(n2)[0]);
        if (first && first.scrollIntoView) first.scrollIntoView({ block: 'center' });
      } else select(null);
      return;
    }
    var go = t.closest('[data-pa-goto]');
    if (go) {
      e.preventDefault();
      var el = lineEl(go.getAttribute('data-pa-goto'));
      if (el) {
        if (el.scrollIntoView) el.scrollIntoView({ block: 'center' });
        var field = el.querySelector('textarea');
        if (field && field.focus) field.focus({ preventScroll: true });
        light([go.getAttribute('data-pa-goto')]);
      }
      return;
    }
    var show = t.closest('[data-pa-show]');
    if (show) { e.preventDefault(); showInAdvert(show.getAttribute('data-pa-show')); return; }
    var adv = t.closest('[data-pa-advert]');
    if (adv) { setAdvert(adv.getAttribute('aria-expanded') !== 'true'); return; }
    if (t.closest('[data-pa-nodownload]')) { e.preventDefault(); return; }
    var fetchBtn = t.closest('[data-pa-fetch]');
    if (fetchBtn) { start(fetchBtn); return; }
    var applyBtn = t.closest('[data-pa-apply]');
    if (applyBtn) { askApply(applyBtn); return; }
    var undoBtn = t.closest('[data-pa-undo]');
    if (undoBtn) { undo(undoBtn); return; }
    var dlg = t.closest('[data-pa-dlg]');
    if (dlg) { dialogChoice(dlg.getAttribute('data-pa-dlg')); return; }
    var rescore = t.closest('[data-cv-act="rescore"]');
    if (rescore && !rescore.disabled) { working('copy'); pressed = 'copy'; }
    var note3 = t.closest('[data-note]');
    if (note3) select(note3);
  });
  document.addEventListener('focusin', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var note = t.closest('[data-note]');
    if (note) {
      select(note);
      if (note.classList.contains('is-closed') && t.classList && t.classList.contains('note-toggle')) {
        setOpen(note, true);
      }
      return;
    }
    var line = t.closest('#cv-editor .cvl[data-n]');
    if (line) {
      var n = line.getAttribute('data-n');
      all('[data-note]').forEach(function (nt) {
        nt.classList.toggle('is-selected', nowOf(nt).indexOf(n) >= 0);
      });
    }
  });
  // A met note opened by focus folds again when focus leaves it, unless it
  // was opened by a click.
  document.addEventListener('focusout', function (e) {
    var t = e.target;
    var note = t && t.closest ? t.closest('[data-note]') : null;
    if (!note || !note.querySelector('.note-toggle')) return;
    var next = e.relatedTarget;
    if (next && note.contains && note.contains(next)) return;
    if (note.getAttribute('data-by-click') === '1') return;
    if (note.classList.contains('is-open')) setOpen(note, false);
  });

  // --------------------------------------------------- the description
  function setAdvert(open) {
    var box = slot('advert');
    if (!box) return;
    box.classList.toggle('is-open', open);
    var b = box.querySelector('[data-pa-advert]');
    if (b) {
      b.setAttribute('aria-expanded', open ? 'true' : 'false');
      b.textContent = open ? 'Hide the job description' : 'Show the job description';
    }
  }
  function showInAdvert(target) {
    setAdvert(true);
    var el = document.getElementById(target);
    if (!el) return;
    all('.adv .sel').forEach(function (s) { s.classList.remove('sel'); });
    el.classList.add('sel');
    if (el.scrollIntoView) el.scrollIntoView({ block: 'center' });
    if (el.focus) el.focus({ preventScroll: true });
  }

  // ------------------------------------------------ the parts after a press
  function distribute() {
    if (!carrier) return false;
    var parts = all('template[data-part]', carrier);
    if (!parts.length) return false;
    var enabled = all('[data-cv-act]').some(function (b) { return !b.disabled; });
    var advertOpen = !!(slot('advert') && slot('advert').classList.contains('is-open'));
    var gates = slot('gates');
    var gatesOpen = gates ? gates.hasAttribute('open') : true;
    parts.forEach(function (tpl) {
      var name = tpl.getAttribute('data-part');
      var s = slot(name);
      if (!s) return;
      var empty;
      if (tpl.content && typeof tpl.innerHTML === 'string') {
        empty = !tpl.innerHTML.trim();
        s.innerHTML = tpl.innerHTML;
      } else {
        // A document without template elements: the part's nodes move over.
        var kids = Array.prototype.slice.call(tpl.childNodes || tpl.children || []);
        empty = kids.length === 0;
        s.textContent = '';
        kids.forEach(function (k) { detach(k); s.appendChild(k); });
      }
      if ((name === 'bar' || name === 'gaps' || name === 'experience') && s.classList) s.classList.toggle('is-empty', empty);
    });
    carrier.innerHTML = '';
    all('[data-cv-act]').forEach(function (b) { b.disabled = !enabled; });
    setAdvert(advertOpen);
    if (gates && !gatesOpen) gates.removeAttribute('open');
    loaded = snapshot();
    busy = false;
    if (pendingApply) {
      var said = document.getElementById('pa-save-as');
      pendingApply = false;
      if (said && said.getAttribute('data-saved-same') === '1') apply(document.querySelector('[data-pa-apply]'));
    }
    now();
    if (pressed === 'copy') {
      // Rescore role's answer, put in by the editor, which says the outcome
      // in #cv-say: focus goes to the Score heading and the same words are
      // said, with the copy's figure.
      // After the editor has said its words, which it does once it has put
      // the parts in.
      pressed = null;
      Promise.resolve().then(function () {
        var copyFig = figure('copy');
        landed(((cvSay && cvSay.textContent) || 'Rescored.')
          + (copyFig ? ' This copy now scores ' + copyFig + '.' : ''));
      });
    }
    return true;
  }
  function take(html) {
    if (!carrier) return;
    carrier.innerHTML = html;
    distribute();
  }
  if (carrier && window.MutationObserver) {
    new window.MutationObserver(function () { distribute(); }).observe(carrier, { childList: true });
  }

  // ---------------------------------------- the one press that may spend
  function working(which) {
    var fig = document.querySelector('[data-fig="' + which + '"]');
    if (fig && !fig.hasAttribute('data-was')) {
      var num = fig.querySelector('.num');
      var sub = fig.querySelector('.fig-sub');
      fig.setAttribute('data-was', JSON.stringify({
        cls: num ? num.className : '', num: num ? num.textContent : '',
        sub: sub ? sub.textContent : '' }));
      if (num) { num.className = 'num none'; num.textContent = 'Working'; }
      if (sub) sub.textContent = which === 'spec' ? 'Fetching, then scoring once.' : 'Scoring this copy once.';
    }
    var say = document.querySelector('[data-pa-working]');
    if (say) {
      say.hidden = false;
      say.className = 'notice work';
      say.innerHTML = which === 'spec'
        ? '<b>Working.</b> Reading the advert, then scoring it.'
        : '<b>Rescoring this copy.</b>';
      announce(say.textContent);
    }
  }
  // The one status region, on the page from the start and outside every part
  // a press redraws, so what is written into it is read out.
  function announce(words) {
    var live = document.getElementById('pa-live');
    if (live && live.textContent !== words) live.textContent = words || '';
  }
  // After a paid press: focus goes to the Score heading and the outcome is
  // said, once the parts are in.
  var pressed = null;
  function figure(which) {
    var num = document.querySelector('[data-fig="' + which + '"] .num');
    var n = num ? String(num.textContent || '').trim() : '';
    return /^\d+$/.test(n) ? n : null;
  }
  function outcomeWords(j) {
    var told = (j.notices || []).map(function (n) { return n.text; }).filter(Boolean).join(' ');
    var spec = figure('spec');
    if (j.outcome === 'scored') {
      return 'Scored. The CV match on the full description is ' + (spec || 'not known') + '.' + (told ? ' ' + told : '');
    }
    if (j.outcome === 'reused') {
      return 'This description was already scored, so nothing was spent.'
        + (spec ? ' The CV match on the full description is ' + spec + '.' : '') + (told ? ' ' + told : '');
    }
    return told || j.note || 'Done.';
  }
  function landed(words) {
    var h = document.getElementById('score-h');
    if (h && h.focus) h.focus();
    announce(words);
  }
  function unwork() {
    all('[data-fig][data-was]').forEach(function (f) {
      var was = {};
      try { was = JSON.parse(f.getAttribute('data-was')); } catch (e) {}
      var num = f.querySelector('.num');
      var sub = f.querySelector('.fig-sub');
      if (num) { num.className = was.cls || num.className; num.textContent = was.num || ''; }
      if (sub) sub.textContent = was.sub || '';
      f.removeAttribute('data-was');
    });
    var say = document.querySelector('[data-pa-working]');
    if (say) say.hidden = true;
  }
  function start(btn) {
    if (busy) return;
    busy = true;
    var label = null;
    if (btn) {
      btn.disabled = true;
      label = { html: typeof btn.innerHTML === 'string' ? btn.innerHTML : null,
                text: btn.textContent };
      btn.textContent = 'Fetching and scoring…';
    }
    working('spec');
    fetch('/prepare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        busy = false;
        if (j && typeof j.html === 'string') { take(j.html); landed(outcomeWords(j)); return; }
        back(btn, label, (j && j.note) || 'That did not run.');
      })
      .catch(function () {
        busy = false;
        back(btn, label, 'No usable answer came back. The role may still be working. Reload this page in a minute to see what was stored.');
      });
  }
  function back(btn, label, words) {
    unwork();
    if (btn) {
      btn.disabled = false;
      if (label && label.html !== null) btn.innerHTML = label.html;
      else if (label) btn.textContent = label.text;
    }
    var say = document.querySelector('[data-pa-working]');
    if (say) { say.hidden = false; say.className = 'notice bad'; say.textContent = words; }
    announce(words);
  }
  // The editor's own words (#cv-say, seen) are said through #pa-live, the one
  // status region, so nothing is read out twice.
  var cvSay = document.getElementById('cv-say');
  if (cvSay && window.MutationObserver) {
    new window.MutationObserver(function () {
      if (String(cvSay.className || '').indexOf('bad') >= 0) {
        unwork();
        pendingApply = false;
        pressed = null;
      }
      if (!pressed && cvSay.textContent) announce(cvSay.textContent);
    }).observe(cvSay, { childList: true, characterData: true, subtree: true, attributes: true });
  }

  // --------------------------------------------------- Mark applied
  var dialog = document.getElementById('pa-apply-dialog');
  var opener = null;
  function savedData(btn) {
    try { return JSON.parse(btn.getAttribute('data-saved') || '{}'); } catch (e) { return {}; }
  }
  function changedLines(saved) {
    var now = snapshot();
    var was = (saved.lines || []).map(keyOf);
    var out = [];
    var n = Math.max(now.length, was.length);
    for (var i = 0; i < n; i++) if ((now[i] || '') !== (was[i] || '')) out.push(i + 1);
    return out;
  }
  function askApply(btn) {
    if (busy || !dialog) return;
    var saved = savedData(btn);
    var differs = !saved.name || !saved.same || edited();
    if (!differs) { apply(btn); return; }
    opener = btn;
    var h = document.getElementById('pa-dialog-h');
    var p = document.getElementById('pa-dialog-p');
    var bSave = dialog.querySelector('[data-pa-dlg="save"]');
    var bApply = dialog.querySelector('[data-pa-dlg="apply"]');
    if (!saved.name) {
      h.textContent = 'No CV is saved for this role';
      p.textContent = '';
      bApply.textContent = 'Mark applied with no CV recorded';
    } else {
      var lines = changedLines(saved);
      var what = lines.length === 1 ? 'Line ' + lines[0] + ' has changed'
        : lines.length > 1 && lines.length <= 4 ? 'Lines ' + lines.join(', ') + ' have changed'
        : lines.length > 4 ? lines.length + ' lines have changed' : 'The copy on screen has changed';
      h.textContent = 'The copy on screen is not the saved one';
      p.textContent = what + ' since you saved at ' + saved.at + '.';
      bApply.textContent = 'Mark applied with the ' + saved.at + ' copy';
    }
    bSave.textContent = 'Save this copy, then mark applied';
    var save = document.querySelector('[data-cv-act="save"]');
    bSave.hidden = !save || save.disabled;
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute('open', '');
    var first = bSave.hidden ? bApply : bSave;
    if (first.focus) first.focus();
  }
  function closeDialog() {
    if (dialog.close) dialog.close(); else dialog.removeAttribute('open');
    if (opener && opener.focus) opener.focus();
  }
  if (dialog) {
    dialog.addEventListener('cancel', function () {
      Promise.resolve().then(function () { if (opener && opener.focus) opener.focus(); });
    });
    dialog.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      var items = all('button', dialog).filter(function (b) { return !b.hidden; });
      if (!items.length) return;
      var i = items.indexOf(document.activeElement);
      if (e.shiftKey && i <= 0) { e.preventDefault(); items[items.length - 1].focus(); }
      else if (!e.shiftKey && i === items.length - 1) { e.preventDefault(); items[0].focus(); }
    });
  }
  function dialogChoice(which) {
    closeDialog();
    if (which === 'apply') apply(opener);
    else if (which === 'save') {
      var save = document.querySelector('[data-cv-act="save"]');
      if (!save || save.disabled) return;
      pendingApply = true;
      save.click();
    }
  }
  function appSay(words) {
    var el = document.querySelector('[data-pa-app-say]');
    if (el) el.textContent = words || '';
  }
  function leave(hash) {
    if (window.RadarCvEditor && window.RadarCvEditor.leaving) window.RadarCvEditor.leaving();
    try { history.replaceState(null, '', location.pathname + (hash || '')); } catch (e) {}
    location.reload();
  }
  function post(url, btn, doing, done) {
    if (busy) return;
    busy = true;
    if (btn) btn.disabled = true;
    appSay(doing);
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        busy = false;
        if (j && j.ok) { done(); return; }
        if (btn) btn.disabled = false;
        appSay((j && j.note) || 'That did not run.');
      })
      .catch(function () {
        busy = false;
        if (btn) btn.disabled = false;
        appSay('No usable answer came back. Reload the page to see what was stored.');
      });
  }
  function apply(btn) {
    post('/apply', btn, 'Marking the role applied.', function () { leave('#marked'); });
  }
  function undo(btn) {
    post('/unapply', btn, 'Taking the role back to Preparing.', function () { leave(''); });
  }

  // ------------------------------------------------------------- opening
  if (location.hash === '#marked' && main.getAttribute('data-applied') === '1') {
    var marked = document.getElementById('pa-marked');
    if (marked) { marked.hidden = false; if (marked.focus) marked.focus(); }
    try { history.replaceState(null, '', location.pathname); } catch (e) {}
  }
  var editorHost = document.getElementById('cv-editor');
  if (editorHost && window.MutationObserver) {
    new window.MutationObserver(function () { if (!laying) soon(); })
      .observe(editorHost, { childList: true });
  }
  document.addEventListener('input', function (e) {
    if (e.target && e.target.closest && e.target.closest('#cv-editor')) now();
  });
  // The editor draws its lines again after Enter, Backspace, a move or a
  // delete; the observer above sees that in a browser, and these see it too.
  ['keydown', 'click', 'focusout'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      if (e.target && e.target.closest && e.target.closest('#cv-editor')) {
        Promise.resolve().then(now);
      }
    });
  });
  window.addEventListener('resize', soon);
  window.addEventListener('load', function () { if (loaded === null) loaded = snapshot(); soon(); });
  // After the editor's own script, which runs after this one, has drawn.
  Promise.resolve().then(function () { if (loaded === null) loaded = snapshot(); now(); });
  window.RadarPrepare = { take: take, refind: refind, layout: layout, snapshot: snapshot,
                          edited: edited, paginate: paginate };
})();
"""
