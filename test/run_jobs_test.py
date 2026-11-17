#!/usr/bin/env python
"""Checks for the round-2 Jobs page and the frame every page sits in.

1. New means unread. read_at is a nullable column added through _MIGRATIONS;
   every row on file when it arrives is stamped read once, and only later
   rows are new. A role is read when any advert on its chain is, so a repost
   of a read role arrives read. POST /read stamps the chain, and so do the
   verdicts; their undos do not make a role new again.
2. The queue: Top prospects, One question away, Reading, each sorted by CV
   match and then freshness, with ten-point CV bands; the row's parts, the
   gate marks with their names and tooltips, the New word and its counts, the
   Full job spec tag and the lifecycle chip. Filtered and acknowledged roles
   are hidden and reachable; Applied has its own view.
3. The role pane: the actions with their keys, Score with the four gates,
   the provenance line, the reasons with their quotes marked in the advert or
   the card, the asks summary, the details, and the company only when its
   page exists. The advert, or its not-read state with Prepare application.
4. The fragments the page script uses, read only, and the routes through
   the real server and its write guard. Rendering writes nothing and never
   fetches or scores.
5. The frame: the pages are dark only, and the bar marks Jobs. The bar itself,
   on every page, is checked in test/run_header_test.py.

No network beyond a loopback server this runner starts, no model, no n8n.
Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(TEST_DIR))

import build_dashboard  # noqa: E402
import jobs_page  # noqa: E402

# Between two of a row's facts: a comma for a screen reader, space on screen.
BIT = '<span class="visually-hidden">, </span><span class="r-bit">'
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
CFG = rc.load_config()
NOW = datetime.now(timezone.utc)
SPEC_TEXT = ("About us\n\nWe make infusion pumps in Guildford.\n\nThe role\n\n"
             "You will own the IEC 62304 software lifecycle for our pump firmware. "
             "You will report to the VP Quality.\n\n"
             "What you will do\n\n- Act as internal auditor under ISO 13485.\n"
             "- Lead a team of six engineers.\n\nDay rate £700–£750, outside IR35.")


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(days: float) -> str:
    return iso(NOW - timedelta(days=days))


_n = 0


def put(conn, title="Head of Software", company="Alpha Medical", **cols) -> int:
    global _n
    _n += 1
    row = {"url_hash": f"jobs-{_n}", "first_seen": ago(3), "last_seen": ago(1),
           "source": "linkedin-alert", "title": title, "company": company,
           "location": "Guildford", "salary_rate": "£720/day", "tier": "top",
           "cv_match": 80, "failed_gates": "[]", "one_line_why": "A fit.",
           "gate_sector": 1, "gate_sector_note": "infusion pumps",
           "gate_cv": 1, "gate_cv_note": "80% clears the 70% pass mark",
           "gate_location": 1, "gate_location_note": "Guildford",
           "gate_rate": 1, "gate_rate_note": "£720/day clears the £700 floor",
           "rate_band": "above", "engagement_type": "contract",
           # The figure on file, as the extractor stores it. A row with none
           # would read Rate not stated, whatever gate_rate says.
           "pay_currency": "GBP", "pay_period": "day", "pay_min": 720.0,
           "pay_max": 720.0, "rate_stated": 1,
           "source_url": f"https://www.linkedin.com/jobs/view/{9000000 + _n}/",
           "role_key": rc.role_key(title, company), "red_flags": "[]"}
    row.update(cols)
    conn.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES"
                 f" ({', '.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()
    return conn.execute("SELECT id FROM opportunities WHERE url_hash = ?",
                        (row["url_hash"],)).fetchone()[0]


@contextlib.contextmanager
def pointed_at(db):
    saved = serve_dashboard.ARGS
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False,
                                              host="127.0.0.1", port=0)
    try:
        yield
    finally:
        serve_dashboard.ARGS = saved


def collect(db) -> dict:
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return build_dashboard.collect(conn, CFG)
    finally:
        conn.close()


def page(db, selected=None, view="open", reading=True) -> str:
    return build_dashboard.render_jobs_page(collect(db), CFG, "t", selected, view, reading)


def pane(db, item_id, view="open") -> dict:
    with pointed_at(db):
        query = {"part": ["role"]}
        if view != "open":
            query["show"] = [view]
        status, body, _t = serve_dashboard.render_jobs(item_id, query)
    return json.loads(body) if status == 200 else {}


def rows_of(html: str) -> list[int]:
    return [int(x) for x in re.findall(r'<div class="row [^"]*" data-id="(\d+)"', html)]


def row_html(html: str, item_id: int) -> str:
    m = re.search(r'<div class="row [^"]*" data-id="' + str(item_id) + r'".*?</div>', html, re.S)
    return m.group(0) if m else ""


def dump(db) -> list:
    conn = sqlite3.connect(db)
    try:
        return [tuple(r) for r in conn.execute("SELECT * FROM opportunities ORDER BY id")]
    finally:
        conn.close()


# ------------------------------------------------------------------ the migration

def migration_checks(tmp: Path) -> None:
    db = tmp / "old.sqlite"
    conn = rc.get_db(db)
    a = put(conn, "Role A", "Old Co")
    b = put(conn, "Role B", "Old Co")
    conn.close()
    if sqlite3.sqlite_version_info < (3, 35, 0):
        print("NOTE  SQLite older than 3.35 cannot drop a column; the switch-on "
              "replay is skipped")
        return
    # The deployed shape: the column and its flag not there yet.
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE opportunities DROP COLUMN read_at")
    raw.execute("DELETE FROM meta WHERE key = 'migrated_read_at'")
    raw.commit()
    raw.close()
    data = collect(db)
    check(data["read_keys"] is None and jobs_page.build_model(data)["new"] == 0,
          "before the migration a read-only page reads every role as read")
    conn = rc.get_db(db)        # the first writer to open it migrates
    stamped = conn.execute("SELECT COUNT(*) FROM opportunities WHERE read_at IS NOT NULL").fetchone()[0]
    flag = conn.execute("SELECT value FROM meta WHERE key = 'migrated_read_at'").fetchone()
    check(stamped == 2 and flag is not None,
          "the first open adds read_at and stamps every role on file read, once, behind its flag")
    c = put(conn, "Role C", "New Co")
    conn.close()
    conn = rc.get_db(db)        # a later open does not stamp again
    left = conn.execute("SELECT read_at FROM opportunities WHERE id = ?", (c,)).fetchone()[0]
    conn.close()
    model = jobs_page.build_model(collect(db))
    check(left is None and model["new"] == 1,
          "a role that arrives after the switch-on is new, and stays new across later opens")
    check(all(o["_unread"] is False for o in model["tiers"]["top"] if o["id"] in (a, b)),
          "and the roles from before it are not")


# ------------------------------------------------------------------ read state

def read_checks(tmp: Path) -> None:
    db = tmp / "read.sqlite"
    conn = rc.get_db(db)
    first = put(conn, "Head of Quality", "Chain Co", first_seen=ago(9), last_seen=ago(8))
    conn.close()
    with pointed_at(db):
        out = serve_dashboard.set_read(first)
        again = serve_dashboard.set_read(first)
        missing = serve_dashboard.set_read(999999)
    check(out == {"ok": True, "id": first, "stamped": 1}
          and again == {"ok": True, "id": first, "stamped": 0}
          and missing["ok"] is False,
          "POST /read's function stamps the role once, is a success when it is read "
          "already, and refuses a row that does not exist")
    conn = rc.get_db(db)
    repost = put(conn, "Head of Quality", "Chain Co", first_seen=ago(1), last_seen=ago(1))
    conn.close()
    model = jobs_page.build_model(collect(db))
    face = model["tiers"]["top"][0]
    check(face["id"] == repost and face["_unread"] is False and model["new"] == 0,
          "a repost of a read role arrives read: the chain shares the stamp")
    # The verdicts read a role too, and their undos leave it read.
    conn = rc.get_db(db)
    fresh = {name: put(conn, f"Verdict {name}", "Verdict Co") for name in
             ("ack", "close", "dismiss", "apply")}
    conn.close()
    with pointed_at(db):
        results = [serve_dashboard.set_acknowledged(fresh["ack"], True),
                   serve_dashboard.set_lifecycle(fresh["close"], "close"),
                   serve_dashboard.set_lifecycle(fresh["dismiss"], "dismiss"),
                   serve_dashboard.set_applied(fresh["apply"], True)]
        undos = [serve_dashboard.set_acknowledged(fresh["ack"], False),
                 serve_dashboard.set_lifecycle(fresh["close"], "unclose"),
                 serve_dashboard.set_lifecycle(fresh["dismiss"], "undismiss"),
                 serve_dashboard.set_applied(fresh["apply"], False)]
    conn = sqlite3.connect(db)
    stamps = [conn.execute("SELECT read_at FROM opportunities WHERE id = ?", (i,)).fetchone()[0]
              for i in fresh.values()]
    conn.close()
    check(all(r["ok"] for r in results + undos) and all(stamps),
          "Acknowledge, Close role, Dismiss and Mark applied each read the role, and "
          "their undos leave it read")
    # Reading an applied role changes nothing else about it.
    conn = rc.get_db(db)
    applied = put(conn, "Applied Lead", "Kept Co", applied_at=ago(2))
    before = conn.execute("SELECT applied_at, acknowledged_at, closed_at, dismissed_at"
                          " FROM opportunities WHERE id = ?", (applied,)).fetchone()
    conn.close()
    with pointed_at(db):
        r = serve_dashboard.set_read(applied)
    conn = sqlite3.connect(db)
    after = conn.execute("SELECT applied_at, acknowledged_at, closed_at, dismissed_at"
                         " FROM opportunities WHERE id = ?", (applied,)).fetchone()
    conn.close()
    check(r["ok"] and tuple(before) == tuple(after),
          "reading an applied role leaves its stamps as they were")


# ------------------------------------------------------------------ the queue

def queue_checks(tmp: Path) -> None:
    db = tmp / "queue.sqlite"
    conn = rc.get_db(db)
    t_low_new = put(conn, "Top Low Fresh", "A Co", cv_match=72, last_seen=ago(0.5))
    t_high = put(conn, "Top High", "B Co", cv_match=91, last_seen=ago(5))
    t_low_old = put(conn, "Top Low Old", "C Co", cv_match=72, last_seen=ago(4),
                    score_basis="spec", scored_at=ago(1),
                    description_text=SPEC_TEXT, description_fetched_at=ago(1),
                    description_hash=rc.description_version(SPEC_TEXT))
    q = put(conn, "Question Role", "D Co", tier="question", cv_match=78, gate_rate=0,
            gate_rate_note="£650/day is under the £700 floor", failed_gates='["rate"]',
            question_text="Would they move on the rate? £650/day is £50 under the floor",
            salary_rate="£650/day", pay_min=650.0, pay_max=650.0)
    r = put(conn, "Reading Role", "E Co", tier="reading", cv_match=50, gate_cv=0,
            gate_location=None, gate_location_note="Bristol, no pattern given")
    f = put(conn, "Filtered Role", "F Co", tier="filtered", cv_match=20,
            filter_reason="cv match under 40")
    ack = put(conn, "Seen Role", "G Co", acknowledged_at=ago(1))
    old_unknown = put(conn, "Quiet Role", "H Co", cv_match=70, first_seen=ago(40),
                      last_seen=ago(40))
    ageing = put(conn, "Ageing Role", "I Co", cv_match=70, first_seen=ago(20),
                 last_seen=ago(12))
    # A chain that came back after expiring keeps its first date.
    put(conn, "Revived Role", "J Co", cv_match=74, first_seen=ago(90), last_seen=ago(80),
        expired_at=ago(30))
    revived = put(conn, "Revived Role", "J Co", cv_match=74, first_seen=ago(1), last_seen=ago(1))
    conn.execute("UPDATE opportunities SET read_at = ? WHERE id IN (?, ?)",
                 (ago(1), t_high, t_low_old))
    conn.commit()
    conn.close()

    html = page(db)
    order = rows_of(html.split('id="group-top"')[1].split('id="group-question"')[0])
    check(order[:4] == [t_high, revived, t_low_new, t_low_old],
          "Top prospects run CV match high to low, then the freshest sign of life first "
          f"({order})")
    top_part = html.split('id="group-top"')[1].split('id="group-question"')[0]
    check('<div class="band">CV match 90–100</div>' in top_part
          and '<div class="band">CV match 70–79</div>' in top_part
          and top_part.index("CV match 90–100") < top_part.index("CV match 70–79"),
          "a divider opens each ten-point CV band inside a tier")
    check(html.index('id="group-top"') < html.index('id="group-question"')
          < html.index('id="group-reading"'),
          "the tiers run Top prospects, One question away, Reading")
    check(f'data-id="{f}"' not in html and f'data-id="{ack}"' not in html
          and '<a class="link" href="/jobs?show=filtered">Show them</a>' in html
          and '<a class="link" href="/jobs?show=acknowledged">Acknowledged' in html,
          "Filtered and acknowledged roles are hidden from the open roles and linked")
    filtered = page(db, view="filtered")
    acknowledged = page(db, view="acknowledged")
    check(f'data-id="{f}"' in filtered and f'data-id="{ack}"' in acknowledged
          and f'data-id="{t_high}"' not in filtered,
          "and each has a view of its own that lists them")
    folded = page(db, reading=False)
    check(f'data-id="{r}"' not in folded
          and 'id="group-reading-rows" hidden data-lazy="1"' in folded
          and 'aria-controls="group-reading-rows" data-fold="reading"' in folded,
          "Reading folds, and its rows load when it is opened")
    selected_reading = page(db, selected=r, reading=False)
    check(row_html(selected_reading, r) != "",
          "but a role opened from Reading has Reading open around it")

    row = row_html(html, t_low_old)
    check(">Top Low Old</span></a>" in row
          and 'class="r-cv"' not in row and "72%" not in row
          and "First seen " in row
          and '<span class="r-bit">C Co</span>' + BIT + 'Guildford</span>' + BIT in row
          and '<span class="r-rate">£720/day</span>' in row
          and row.endswith("</div>") and BIT + "Contract</span>" in row
          and "·" not in row and "|" not in re.sub(r"<[^>]+>", "", row)
          and "LinkedIn" not in row and "A fit." in row,
          "a row carries the title, the first-seen date, the company, place, pay "
          "as stated, contract or permanent and the reason, and no CV figure and "
          "no LinkedIn; no middle dot and no typed bar between its facts, and a "
          "comma between them for a screen reader")
    rule = re.search(r"\.r-bit ~ \.r-bit::before\{([^}]*)\}", jobs_page.ROW_CSS)
    rule = rule.group(1) if rule else ""
    check('content:""' in rule and "border-left:1px solid var(--text-3)" in rule
          and "margin:0 8px" in rule and "width:0" in rule
          and ".r-bit::before" not in jobs_page.ROW_CSS.replace(".r-bit ~ .r-bit::before", "")
          and ".r-bit::after" not in jobs_page.ROW_CSS,
          "between two facts a drawn 1px rule in the muted text colour, 8px either side, "
          "and none before the first fact or after the last")
    meta = jobs_page.ROW_CSS[jobs_page.ROW_CSS.index(".r-meta{"):].split("}", 1)[0]
    check("white-space:nowrap" in meta and "text-overflow:ellipsis" in meta,
          "the facts' line never wraps, it is cut, so it always starts with the first fact")
    check("position:relative" in meta and "overflow:hidden" in meta and "pointer-events:none" in meta,
          "the line is the screen reader commas' positioning box, so a cut line clips them and "
          "never widens the page, and a click on it still reaches the row's link")
    # Only what differs between roles: a source other than LinkedIn, pay only
    # when the advert gives a figure.
    face = dict(next(o for o in jobs_page.build_model(collect(db))["lists"]["open"]
                     if o["id"] == t_low_old))
    other = jobs_page.row_html({**face, "source": "email-other",
                                "salary_rate": "Competitive"}, "open", None, ago(0))
    check(BIT + "Contract</span>" + BIT + "Email alert</span>" in other
          and 'class="r-rate"' not in other and "Competitive" not in other
          and "Rate not stated" not in other.replace("Rate not stated, ", ""),
          "a source other than LinkedIn is named, and pay words with no figure show nothing")
    check(jobs_page.pay_shown({"salary_rate": "up to £200k"}) == "up to £200k"
          and jobs_page.pay_shown({"salary_rate": "Competitive salary"}) == ""
          and jobs_page.pay_shown({"salary_rate": None}) == "",
          "pay shows whenever the advert's text holds a figure, whatever the Rate mark says")
    check(".r-title{flex:1 1 auto;min-width:0;overflow:hidden;white-space:normal;"
          "display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;" in html
          and ".r-title{white-space:normal" not in html,
          "a title wraps to two lines before it is cut, at every width")
    check("Full job spec</span>" in row and "Full job spec</span>" not in row_html(html, t_high),
          "only a score made on the full job spec carries the Full job spec tag")
    qrow = row_html(html, q)
    check('id="rl-' in qrow and ">£650/day is under the £700 floor.</span>" in qrow
          and "Would they" not in qrow and "A fit." not in qrow and "row-question" in qrow,
          "a One question away row leads with the failing gate's stored reason, "
          "not the question")
    qface = {"tier": "question", "gate_sector": 1, "gate_cv": 1, "gate_location": 0,
             "gate_rate": None, "rate_stated": 0, "one_line_why": "A fit.",
             "gate_location_note": "Location fails, Karlsruhe is on site.",
             "question_text": "Would they go remote"}
    rate_face = {**qface, "gate_location": 1, "gate_rate": 0, "rate_stated": 1,
                 "pay_currency": "GBP", "pay_period": "year", "pay_max": 53474,
                 "gate_rate_note": "Stated salary £53,474 against the £100,000 floor,"
                                   " below, judged in code."}
    check(jobs_page.row_lead(qface) == "Karlsruhe is on site."
          and jobs_page.row_lead(rate_face)
          == "Stated salary £53,474 against the £100,000 floor, below."
          and jobs_page.row_lead({**qface, "gate_location_note": None}) == "",
          "the reason loses its verdict words and judged in code, and no stored "
          "reason shows nothing")
    check(jobs_page.pane_lead(qface) == ""
          and jobs_page.pane_lead({**qface, "question_text": "Is German fluency a "
                                   "hard requirement?"}) == "Is German fluency a hard requirement?"
          and jobs_page.lead_line(qface) == "Would they go remote.",
          "the pane drops code's own question and keeps one the model wrote; Home's "
          "lead line is unchanged")
    marks = re.findall(r'<span class="mk (mk-[a-z]+)" role="img" aria-label="([^"]+)" '
                       r'tabindex="(-?\d)">.*?<span class="mk-tip" aria-hidden="true">([^<]+)</span>',
                       qrow, re.S)
    check([m[0] for m in marks] == ["mk-pass", "mk-pass", "mk-pass", "mk-fail"]
          and marks[3][1] == "Rate fails, £650/day is under the £700 floor"
          and all(m[1] == m[3] for m in marks),
          "the four gate marks are named in words, and each tooltip is the same words")
    rrow = row_html(html, r)
    check("Location not judged, Bristol, no pattern given" in rrow
          and 'class="mk mk-nj"' in rrow,
          "a gate not judged is named so, with its own mark")
    check(all(m[2] == "-1" for m in marks)
          and html.count('<abbr class="gh-key" title="Sector">S</abbr>') == 3,
          "an unselected row's marks wait out of the tab order, and each open group "
          "names the marks' gates in its header")
    opened = page(db, selected=q)
    check(all(t == "0" for t in re.findall(r'tabindex="(-?\d)"', row_html(opened, q))
              if t != "-1") and 'tabindex="0"' in row_html(opened, q),
          "the selected row's marks are in the tab order, so each tooltip shows on focus")
    check(".mk:hover .mk-tip,.mk:focus-visible .mk-tip{display:block}" in html
          and ".row:focus-visible .r-tips" not in html and "r-tips" not in html,
          "a mark's tooltip shows on hover or focus of that mark alone")

    new_row = row_html(html, t_low_new)
    check('<span class="r-new">New</span>' in new_row and "is-unread" in new_row
          and '<span class="r-new">' not in row_html(html, t_high),
          "a role nobody has opened says New, and a read one does not")
    model = jobs_page.build_model(collect(db))
    check(f'<span data-count="new" data-n="{model["new"]}">{model["new"]} new</span>' in html
          and re.search(r'id="group-top">Top prospects<span class="group-count">\d+ '
                        r'<span class="group-new" data-new="\d+">· \d+ new</span>', html),
          "the header and each group count the new roles")
    check(re.search(r'<span data-count="open">\d+ open</span> · <span data-count="top">\d+ '
                    r'Top prospects</span> · <span data-count="question">1 one question away'
                    r'</span> · <span data-count="reading">1 reading</span> · '
                    r'<span data-count="filtered">1 filtered</span>', html),
          "the header counts the open roles and each tier")

    quiet = row_html(html, old_unknown)
    old_days = row_html(html, ageing)
    expiry = datetime.strptime(ago(40)[:10], "%Y-%m-%d") + timedelta(days=jobs_page.expiry_days())
    check("Unknown" not in quiet and "Unknown" not in old_days
          and 'class="tag tag-caution"' not in html and quiet and old_days,
          "an Unknown role carries no chip on its row, close to expiry or not")
    check(f"Unknown · last seen" in pane(db, old_unknown).get("html", "")
          and f"expires {expiry.day} {expiry:%b}" in pane(db, old_unknown).get("html", ""),
          "and the role pane's State still says Unknown and when it expires")
    check(jobs_page.state_tag({"closed_at": ago(1), "closed_source": "page_check"})
          .startswith('<span class="tag tag-neutral">Closed ')
          and jobs_page.state_tag({"closed_at": ago(1), "closed_source": "hand"}) == ""
          and jobs_page.state_tag({}) == "",
          "a row's only state tag is Closed, for an advert the checker confirmed closed")
    first = datetime.strptime(ago(90)[:10], "%Y-%m-%d")
    check(f"First seen {first.day} {first:%b}" in row_html(html, revived),
          "the first-seen date is the chain's first sighting, an expired advert's included")

    applied_db = tmp / "applied.sqlite"
    conn = rc.get_db(applied_db)
    a = put(conn, "Applied Role", "K Co", applied_at=ago(2))
    o = put(conn, "Open Role", "L Co")
    conn.close()
    open_view = page(applied_db)
    applied_view = page(applied_db, view="applied")
    check(f'data-id="{a}"' not in open_view and f'data-id="{a}"' in applied_view
          and f'data-id="{o}"' not in applied_view
          and '<span class="r-bit">Applied ' + jobs_page._day(ago(2)) + '</span>' + BIT + "K Co" in applied_view,
          "Applied roles have their own view, with the date applied, and stay out of "
          "the open roles")


# ------------------------------------------------------------------ the role pane

def pane_checks(tmp: Path) -> None:
    db = tmp / "pane.sqlite"
    conn = rc.get_db(db)
    company = rc.resolve_company(conn, "Spec Co")
    scored = ago(1)
    spec_reasons = json.dumps({"v": rc.REASONS_VERSION, "scored_at": scored, "basis": "spec",
                               "dropped": 0, "reasons": [
                                   {"reason": "Owns the lifecycle",
                                    "quote": "You will own the IEC 62304 software lifecycle"},
                                   {"reason": "Audit lead",
                                    "quote": "Act as internal auditor under ISO 13485."},
                                   {"reason": "A reason with no quote", "quote": None}]})
    working = json.dumps({"scored_at": scored, "asks": [{"ask": "x", "status": "met", "lines": []}],
                          "counts": {"met": 3, "partly": 2, "missing": 1},
                          "rate_quote": "Day rate £700–£750, outside IR35.",
                          "rate_quote_from": "scorer"})
    spec = put(conn, "Spec Role", "Spec Co", company_id=company, score_basis="spec",
               scored_at=scored, score_model="claude-sonnet-4-6", prompt_version="97f11cce",
               cv_version="cv-20261029-091500.md", description_text=SPEC_TEXT,
               description_fetched_at=ago(1),
               description_hash=rc.description_version(SPEC_TEXT),
               score_reasons=spec_reasons, score_working=working, ir35="outside",
               red_flags='["No pay band for the permanent option"]')
    put(conn, "Spec Role", "Spec Co", company_id=company, first_seen=ago(30),
        last_seen=ago(20), seen_count=2)
    card_reasons = json.dumps({"v": rc.REASONS_VERSION, "scored_at": scored, "basis": "card",
                               "dropped": 0, "reasons": [
                                   {"reason": "The title says so", "quote": "IVD analyser"},
                                   {"reason": "Three days in Cambridge",
                                    "quote": "Cambridge, 3 days on site"}]})
    card = put(conn, "Director, IVD analyser", "Card Co", location="Cambridge, 3 days on site",
               score_basis="card", scored_at=scored, score_model="claude-sonnet-4-6",
               prompt_version="97f11cce", score_reasons=card_reasons, company_id=9999)
    old = put(conn, "Old Role", "Old Co", score_basis="card", prompt_version="pre-record",
              score_model="unknown", scored_at=None)
    conn.close()
    before = dump(db)

    p = pane(db, spec)
    html = p.get("html", "")
    check(p.get("id") == spec and p.get("view") == "open"
          and '<h2 class="dh-title" id="role-title" tabindex="-1">Spec Role</h2>' in html,
          "the role pane is drawn for the role the address names")
    for kind, label, key in (("ack", "Acknowledge", "A"), ("dismiss", "Dismiss", "D"),
                             ("close", "Close role", "C"), ("apply", "Mark applied", "M")):
        check(f'data-act="{kind}" data-id="{spec}" data-shortcut="{key.lower()}" '
              f'aria-keyshortcuts="{key}">{label}<kbd class="kbd" aria-hidden="true">{key}</kbd>'
              in html, f"the {label} action carries its key, {key}")
    check(f'href="/prepare/{spec}" data-prepare="{spec}" data-shortcut="p" '
          'aria-keyshortcuts="P">Prepare application<kbd' in html
          and 'data-shortcut="o" data-open=' in html and "Open the advert" in html
          and ('aria-keyshortcuts="K">Previous' in html
               or 'aria-disabled="true">Previous<kbd class="kbd" aria-hidden="true">K' in html)
          and ('aria-keyshortcuts="J">Next' in html
               or 'aria-disabled="true">Next<kbd class="kbd" aria-hidden="true">J' in html),
          "Prepare application P, Open the advert O, Previous K and Next J carry theirs")
    gates = re.findall(r'<span class="gate-name">([^<]+)</span><span class="gate-status '
                       r'(is-[a-z]+)">.*?(Passes|Fails|Not judged)</span>'
                       r'<span class="gate-note">([^<]*)</span>', html)
    check([g[0] for g in gates] == ["Sector", "CV match", "Location", "Rate"]
          and all(g[2] == "Passes" for g in gates) and gates[0][3] == "infusion pumps",
          "the score block shows the four gates with their notes, in words")
    basis = re.sub(r"<[^>]+>", "", re.search(r'<p class="basis">(.*?)</p>', html).group(1))
    check(basis == (f"Scored on the full job spec · {jobs_page._day(scored)} · "
                    "claude-sonnet-4-6 · prompt 97f11cce · CV cv-20261029-091500.md"),
          f"the provenance line gives basis, date, model, prompt and CV version ({basis})")
    old_basis = re.sub(r"<[^>]+>", "", re.search(r'<p class="basis">(.*?)</p>',
                                                 pane(db, old)["html"]).group(1))
    check(old_basis == "Scored on the advert card · date not recorded · model unknown · "
                       "prompt pre-record",
          "and says what was not recorded for a score from before the record")
    check('<a class="evidence" id="reason-1-evidence" href="#q-1">' in html
          and '<mark id="q-1">You will own the IEC 62304 software lifecycle</mark>' in html
          and '<mark id="q-2">Act as internal auditor under ISO 13485.</mark>' in html
          and "A reason with no quote.</span></li>" in html,
          "each reason's quote links to the words it rests on, marked in the advert, "
          "and a reason without a quote stands alone")
    check("3 met · 2 partly · 1 missing. " in html
          and f'<a class="link" id="asks-in-prepare" href="/prepare/{spec}">' in html,
          "the asks summary counts the asks and links to Prepare application")
    facts = dict(re.findall(r"<dt>([^<]+)</dt><dd>(.*?)</dd>", html))
    check(facts.get("Rate as advertised") == "£720/day, outside IR35"
          and "Day rate £700–£750, outside IR35." in facts.get("Rate line", "")
          and facts.get("State", "").startswith("Live · seen ")
          and facts.get("Repost chain", "").startswith("Seen 3 times across 2 adverts since ")
          and facts.get("Red flags") == "No pay band for the permanent option"
          and facts.get("Source") == "LinkedIn",
          "the details give the rate as advertised, the rate line, the state, the repost "
          "chain, the red flags and the source")
    check(f'href="/company/{company}"' in html and "Everything on file for Spec Co" in html,
          "a company with a page is linked")
    card_html = pane(db, card)["html"]
    check("/company/" not in card_html and "Everything on file" not in card_html,
          "a company with no page is not linked")
    check("Advert not read yet. Only the advert card is on file." in card_html
          and '<mark id="q-1">IVD analyser</mark>' in card_html
          and '<mark id="q-2">Cambridge, 3 days on site</mark>' in card_html
          and f'href="/prepare/{card}">Prepare application</a>' in card_html
          and "No asks yet. Asks are read from the full job spec." in card_html,
          "an advert never read shows the card the score read, its quotes marked, "
          "and Prepare application as the way to read it")
    check(dump(db) == before, "drawing the panes wrote nothing")


# ------------------------------------------------------------------ fragments and routes

@contextlib.contextmanager
def serving(db):
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    serve_dashboard.GUARD = serve_dashboard.WriteGuard("enforce", serve_dashboard.LOOPBACK_NAMES)
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD,
         serve_dashboard.Handler.log_message) = saved


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def route_checks(tmp: Path) -> None:
    db = tmp / "routes.sqlite"
    conn = rc.get_db(db)
    one = put(conn, "Route Role", "M Co")
    put(conn, "Second Role", "N Co", tier="reading", cv_match=50)
    conn.close()
    calls = []
    import prepare_application
    import score_item
    saved = (prepare_application.prepare, score_item.score_opportunity)
    prepare_application.prepare = lambda *a, **k: calls.append("prepare")
    score_item.score_opportunity = lambda *a, **k: calls.append("score")
    try:
        before = dump(db)
        with serving(db) as port:
            host = {"Host": f"127.0.0.1:{port}"}
            s_page, t_page, b_page = call(port, "GET", "/jobs", headers=host)
            s_role, t_role, b_role = call(port, "GET", f"/jobs/{one}", headers=host)
            s_frag, _t, b_frag = call(port, "GET", f"/jobs/{one}?part=role", headers=host)
            s_q, _t, b_q = call(port, "GET", f"/jobs?part=queue&current={one}", headers=host)
            s_r, _t, b_r = call(port, "GET", "/jobs?part=reading", headers=host)
            s_st, _t, b_st = call(port, "GET", "/jobs?part=stamp&since=0", headers=host)
            s_404, _t, _b = call(port, "GET", "/jobs/999999", headers=host)
            s_bad, _t, _b = call(port, "GET", "/jobs/abc", headers=host)
            s_none, _t, _b = call(port, "GET", "/jobs/999999?part=role", headers=host)
            after_reads = dump(db)
            own = f"127.0.0.1:{port}"
            write = {"Host": own, "Origin": f"http://{own}", "X-Radar-Request": "1",
                     "Content-Type": "application/json"}
            s_read, _t, b_read = call(port, "POST", "/read", json.dumps({"id": one}), write)
            s_noheader, _t, _b = call(port, "POST", "/read", json.dumps({"id": one}),
                                      {k: v for k, v in write.items() if k != "X-Radar-Request"})
            s_bool, _t, _b = call(port, "POST", "/read", json.dumps({"id": True}), write)
            s_foreign, _t, _b = call(port, "POST", "/read", json.dumps({"id": one}),
                                     {**write, "Origin": "https://evil.example"})
    finally:
        prepare_application.prepare, score_item.score_opportunity = saved
    check(s_page == 200 and "text/html" in t_page and s_role == 200
          and b'data-view="role"' in b_role and b'data-view="list"' in b_page,
          "GET /jobs is the list and GET /jobs/<id> keeps the role in the address")
    frag = json.loads(b_frag) if s_frag == 200 else {}
    check(frag.get("id") == one and frag.get("url") == f"/jobs/{one}"
          and "role-title" in frag.get("html", ""),
          "the role fragment is the pane as JSON, with the address to keep")
    q = json.loads(b_q) if s_q == 200 else {}
    check("is-current" in q.get("html", "") and "data-count" in q.get("counts", "")
          and isinstance(q.get("stamp"), int),
          "the queue fragment has the rows, the selected one marked, the counts and the stamp")
    check(s_r == 200 and "Second Role" in json.loads(b_r)["html"],
          "the Reading fragment has the Reading rows")
    check(s_st == 200 and json.loads(b_st)["new"] == 2,
          "the stamp fragment counts the open roles that came after a page was drawn")
    check(s_404 == 404 and s_bad == 404 and s_none == 404,
          "an address that names no role is a 404, the page or the fragment")
    check(after_reads == before and not calls,
          "none of the GETs wrote a row, fetched an advert or scored one")
    check(s_read == 200 and json.loads(b_read)["stamped"] == 1,
          "POST /read through the guard stamps the role")
    check(s_noheader == 403 and s_foreign == 403 and s_bool == 400,
          "and the write guard refuses it without the header or from another origin, "
          "and an id that is not an integer is a 400")

    # The notice counts roles, not adverts: a repost of a role on file is not new.
    conn = rc.get_db(db)
    stamp = conn.execute("SELECT MAX(id) FROM opportunities").fetchone()[0]
    put(conn, "Route Role", "M Co", first_seen=ago(0), last_seen=ago(0))
    put(conn, "Filtered Arrival", "O Co", tier="filtered", cv_match=10)
    put(conn, "Fresh Arrival", "P Co")
    conn.close()
    with pointed_at(db):
        got = serve_dashboard.jobs_new_since(stamp)
    check(got["new"] == 1 and got["stamp"] == stamp + 3,
          "the notice counts one new role: not the repost of a role on file, nor a "
          f"Filtered one ({got})")


# ------------------------------------------------------------------ the page and the frame

def page_checks(tmp: Path) -> None:
    db = tmp / "frame.sqlite"
    conn = rc.get_db(db)
    put(conn)
    conn.execute("INSERT INTO runs (ts, workflow, mode, outcome) VALUES (?, 'inbox', 'live', 'ok')",
                 (ago(0.05),))
    conn.commit()
    conn.close()
    html = page(db)
    check("location.reload()" not in html and "900000" not in html
          and 'id="arrivals"' in html and "part=stamp" in html,
          "the Jobs page does not reload itself; a notice says when new roles arrive")
    check(not re.search(r'<script[^>]+src=|<link[^>]+stylesheet|@import|fonts\.googleapis|'
                        r'cdn\.tailwindcss|iconify|https://[^"\s]*\.(js|css|woff2?)', html),
          "no external script, stylesheet, font or icon set: plain HTML, CSS and script")
    check("function typing(t)" in html
          and "if (typing(e.target) || keys.open) return;" in html
          and "e.ctrlKey || e.metaKey || e.altKey" in html,
          "the shortcuts stand down while a field has the keys, and with a modifier")
    check('<dialog class="keys" id="keys" aria-labelledby="keys-title">' in html
          and "if (e.key === '?')" in html
          and all(f'<kbd class="kbd">{k}</kbd>' in html for k in "JKPADCMO?"),
          "? opens the list of shortcuts, which names every key")
    check('<input type="checkbox" id="keys-on" checked> Single-key shortcuts on' in html
          and "if (!keysOn()) return;" in html,
          "the list of keys can turn the single-key shortcuts off (WCAG 2.1.4)")
    state = re.search(r'<div class="app" data-page="jobs"[^>]* data-state="([^"]+)"', html)
    import html as htmllib
    check(state is not None and json.loads(htmllib.unescape(state.group(1)))["view"] == "open"
          and '<script type="application/json"' not in html,
          "the page's state is a data attribute, so every script block on the page is script")
    check("if (current && current.id === Number(m[1])) return;" in html
          and ".mk.tip-off .mk-tip{display:none}" in html,
          "a quote's link moves only the hash and redraws nothing, and Escape hides a tooltip")
    check('<html lang="en-GB">' in html
          and '<main class="content" id="main">' in html
          and 'role="status" aria-live="polite"' in html,
          "the page has its language, a main landmark and a live region")
    # The header's own checks (its skip link, its light, the page it marks on
    # every page) are in test/run_header_test.py; this is the Jobs page's one.
    check('<a href="/jobs" aria-current="page">Jobs</a>' in html,
          "the top bar marks Jobs as the page it is on")
    with pointed_at(db):
        pages = {name: serve_dashboard.render(name).decode("utf-8")
                 for name in ("home", "insights", "archive")}
        pages["cv"] = serve_dashboard.render_cv_page().decode("utf-8")
        pages["jobs"] = serve_dashboard.render_jobs(None, {})[1].decode("utf-8")
    def unsheeted(text: str) -> str:
        """The page's CSS without the CV sheet's paper, the one approved light block."""
        return re.sub(r"\.sheet-paper\{\s*color-scheme:light", ".sheet-paper{", text)
    for name, text in pages.items():
        check("data-appearance-set" not in text and "radar-appearance" not in text
              and re.search(r"<html [^>]*data-appearance", text) is None
              and unsheeted(text).rfind("color-scheme:dark") > unsheeted(text).rfind("color-scheme:light")
              and text.count("color-scheme:light") - unsheeted(text).count("color-scheme:light") <= 1,
              f"the {name} page is dark only, with no light or auto switch; the CV sheet's "
              "paper is the one light block")


def location_checks(tmp: Path) -> None:
    db = tmp / "loc.sqlite"
    conn = rc.get_db(db)
    local = put(conn, "Local Role", "Q Co", region="Local")
    uk = put(conn, "UK Role", "R Co", region="UK", tier="question", gate_rate=0,
             failed_gates='["rate"]', question_text="Would they move")
    europe = put(conn, "Europe Role", "S Co", region="Europe")
    remote = put(conn, "Remote Role", "T Co", region="Elsewhere", location_class="remote")
    conn.close()
    data = collect(db)

    def shown(loc, view="open"):
        html = build_dashboard.render_jobs_page(data, CFG, "t", None, view, True, loc)
        return set(rows_of(html)), html

    every, html = shown(())
    check(every == {local, uk, europe, remote}
          and all(f'data-loc="{k}" aria-pressed="false">' in html
                  for k in ("local", "uk", "europe", "elsewhere", "remote"))
          and '<div class="chips" role="group" aria-label="Location">' in html,
          "the five location chips are there, none chosen, and every role shows")
    one, html = shown(("uk",))
    check(one == {uk} and 'data-loc="uk" aria-pressed="true">UK</button>' in html
          and f'href="/jobs/{uk}?loc=uk"' in html,
          "UK shows the UK role alone, the chip is pressed, and a role's link keeps it")
    two, html = shown(("europe", "remote"))
    check(two == {europe, remote}
          and re.search(r'id="group-top">Top prospects<span class="group-count">2 of 3', html)
          and '<span data-count="top">3 Top prospects</span>' in html,
          "two chips show either, a group counts what it shows of its whole, and the "
          "header still counts every role")
    check(jobs_page.parse_loc("remote, UK,bogus") == ("uk", "remote")
          and jobs_page.view_query("applied", ("uk",)) == "?show=applied&loc=uk",
          "the address takes the chips in any order, drops unknown ones, and the views keep them")
    with pointed_at(db):
        status, body, _t = serve_dashboard.render_jobs(None, {"part": ["queue"], "loc": ["remote"]})
        page_status, page_body, _t = serve_dashboard.render_jobs(None, {"loc": ["local"]})
        pane_status, pane_body, _t = serve_dashboard.render_jobs(local, {"part": ["role"], "loc": ["local"]})
    check(status == 200 and set(rows_of(json.loads(body)["html"])) == {remote}
          and page_status == 200 and set(rows_of(page_body.decode())) == {local}
          and json.loads(pane_body)["url"] == f"/jobs/{local}?loc=local",
          "the served page and its fragments read the chips from the address")


def main() -> int:
    ap = argparse.ArgumentParser(description="Jobs page checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    os.environ["RADAR_MOCK"] = "1"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        for section in (migration_checks, read_checks, queue_checks, pane_checks,
                        route_checks, page_checks, location_checks):
            try:
                section(tmp_path)
            except Exception as err:  # noqa: BLE001  one section stopping is a failure
                import traceback
                traceback.print_exc()
                check(False, f"{section.__name__} stopped on {type(err).__name__}: {err}")
    print()
    if failures:
        print(f"FAIL. {len(failures)} check(s) failed.")
        for label in failures:
            print("  - " + label)
        return 1
    print("PASS. Every Jobs page check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
