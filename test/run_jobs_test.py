#!/usr/bin/env python
"""Checks for the round-2 Jobs page and the frame every page sits in.

1. New means unread. read_at is a nullable column added through _MIGRATIONS;
   every row on file when it arrives is stamped read once, and only later
   rows are new. A role is read when any advert on its chain is, so a repost
   of a read role arrives read. POST /read stamps the chain, and so do the
   verdicts; their undos do not make a role new again.
2. The queue: Top prospects, One question away, Reading, each sorted by CV
   match and then freshness, each under its band with its pill, count and
   chevron (owner, 17 Nov); the row is a title and one line of facts, with
   the New word, the Full job spec tag and the Closed tag. Filtered and
   acknowledged roles are hidden and reachable; Applied has its own view.
   No single-key shortcut, key hint or list of keys anywhere on the page.
3. The role pane (owner, 17 Nov): a larger title over the row's facts, the
   actions, Score with the tier's pill and the four gates (Not stated amber,
   the CV figure inside its gate, no record line), the reasons with their
   quotes marked in the advert, the asks summary only once there are asks,
   the details, and the company only when its page exists. The advert, or
   "Advert not read yet." beside Prepare application, and no middle dot.
   The advert's layout: headings, lists and paragraphs worked out from the
   stored plain text, its words unchanged, its boilerplate under a closed
   "Rest of the advert", quotes still marked across the blocks.
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
    check('class="band"' not in html and "CV match 90–100" not in html
          and "CV match 70–79" not in html,
          "no CV match band headings inside a tier; the order is the same")
    import tiers
    closed_reading = page(db, reading=False)
    for tier, opened in (("top", True), ("question", True), ("reading", False)):
        head = re.search(rf'<div class="group-head"><h2 class="group-title" id="group-{tier}">'
                         rf'<button type="button" class="tier-pill t-{tier}" data-pill '
                         rf'aria-expanded="false" aria-controls="group-{tier}-means">'
                         rf'{re.escape(tiers.NAMES[tier])}</button><span class="group-count">\d+</span>'
                         rf'<span class="group-new" data-new="\d+">(\d+ new)?</span></h2>'
                         rf'<button type="button" class="fold" aria-expanded="{str(opened).lower()}" '
                         rf'aria-controls="group-{tier}-rows" data-fold="{tier}"', closed_reading)
        means = (f'<p class="group-means" id="group-{tier}-means" hidden>'
                 f'{tiers.MEANINGS[tier]}</p>')
        check(head is not None and means in html,
              f"{tiers.NAMES[tier]} has its band: the name in a pill that shows its one line "
              f"when tapped (hidden until then), the count, the new count and a chevron, "
              f"{'open' if opened else 'closed'} to start")
    check("--tier-top:var(--group-jobs);--tier-top-ink:#15122e;" in tiers.PILL_CSS
          and "--tier-question:rgba(167,156,240,0.18);--tier-question-ink:#c9c2f8;"
              "--tier-question-rule:rgba(167,156,240,0.45);" in tiers.PILL_CSS
          and "--tier-reading:#2a3238;--tier-reading-ink:var(--text-1);" in tiers.PILL_CSS
          and all(tiers.COLOURS[t] not in ("var(--positive)", "var(--negative)", "var(--caution)",
                                           "var(--accent)", "var(--group-apps)")
                  for t in tiers.COLOURS)
          and (".tier-pill.t-question{background:var(--tier-question);color:var(--tier-question-ink);"
               "border-color:var(--tier-question-rule)}") in html
          and ".group-head{position:sticky;top:0" in html
          and "background:var(--surface-sunken)" in html.split(".group-head{", 1)[1].split("}", 1)[0]
          and ".group-rows .row{padding-left:32px}" in html,
          "colour is in the pill only, in the Jobs group's purple (Top prospects filled, One "
          "question away a tint with a rule, Reading grey) and none in green, red, amber or "
          "teal; the band is lightly filled and pinned, and its rows are indented")
    check("FOLD_KEY" in html and "localStorage.setItem(FOLD_KEY[tier]" in html
          and "applyFolds();" in html,
          "every tier opens and closes with its chevron, and the browser remembers the choice")
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
    check(">Top Low Old</span></a></span>" in row
          and 'class="r-cv"' not in row and "72%" not in row
          and "First seen" not in row and "r-first" not in row
          and '<span class="r-bit">C Co</span>' + BIT + 'Guildford</span>' + BIT
              + 'Contract</span>' + BIT + '<span class="r-rate">£720/day</span></span>' in row
          and row.endswith("</div>") and 'class="r3"' not in row and "r-lead" not in row
          and 'class="mk ' not in row and "r-marks" not in row
          and "·" not in row and "|" not in re.sub(r"<[^>]+>", "", row)
          and "LinkedIn" not in row and "A fit." not in row,
          "a row is the title, then one line of facts: company, place, contract or "
          "permanent, pay as stated, and no CV figure, no LinkedIn, no marks, no date and "
          "no lead line; no middle dot and no typed bar between its facts, and a comma "
          "between them for a screen reader")
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
    check('id="rl-' not in qrow and "under the £700 floor" not in qrow
          and "Would they" not in qrow and "A fit." not in qrow and "£650/day" in qrow,
          "a One question away row is its title and facts too, with no reason and no question")
    qface = {"tier": "question", "gate_sector": 1, "gate_cv": 1, "gate_location": 0,
             "gate_rate": None, "rate_stated": 0, "one_line_why": "A fit.",
             "gate_location_note": "Location fails, Karlsruhe is on site.",
             "question_text": "Would they go remote"}
    rate_face = {**qface, "gate_location": 1, "gate_rate": 0, "rate_stated": 1,
                 "pay_currency": "GBP", "pay_period": "year", "pay_max": 53474,
                 "gate_rate_note": "Stated salary £53,474 against the £100,000 floor,"
                                   " below, judged in code."}
    check(jobs_page.pane_lead(qface) == ""
          and jobs_page.pane_lead({**qface, "question_text": "Is German fluency a "
                                   "hard requirement?"}) == "Is German fluency a hard requirement?"
          and jobs_page.lead_line(qface) == "Would they go remote.",
          "the pane drops code's own question and keeps one the model wrote; Home's "
          "lead line is unchanged")
    check('class="mk ' not in html and "gh-key" not in html and "mk-tip" not in html
          and ".mk{" not in html,
          "no gate marks on any row and no S C L R legend in any band")
    check(".row.is-current{background:var(--row-selected)}" in html
          and "border-left:3px" not in html and "inset 3px 0 0" not in html,
          "the open row is marked by its fill, and nothing on the page has a coloured bar "
          "down its left edge")

    new_row = row_html(html, t_low_new)
    check('<span class="r-new">New</span>' in new_row and "is-unread" in new_row
          and '<span class="r-new">' not in row_html(html, t_high),
          "a role nobody has opened says New, and a read one does not")
    model = jobs_page.build_model(collect(db))
    top_new = sum(1 for o in model["tiers"]["top"] if o["_unread"])
    check(re.search(r'Top prospects</button><span class="group-count">\d+</span>'
                    rf'<span class="group-new" data-new="{top_new}">{top_new} new</span>', html)
          and ".group-new::before{" in html,
          "each band counts its new roles beside its count, set apart by the drawn divider")
    check('id="counts"' not in html and "data-count=" not in html
          and "Scores are from the advert card" not in html,
          "no line of counts under the page title, and no line on what the scores are from")

    quiet = row_html(html, old_unknown)
    old_days = row_html(html, ageing)
    expiry = datetime.strptime(ago(40)[:10], "%Y-%m-%d") + timedelta(days=jobs_page.expiry_days())
    check("Unknown" not in quiet and "Unknown" not in old_days
          and 'class="tag tag-caution"' not in html and quiet and old_days,
          "an Unknown role carries no chip on its row, close to expiry or not")
    check(f"Unknown, last seen" in pane(db, old_unknown).get("html", "")
          and f"expires {expiry.day} {expiry:%b}" in pane(db, old_unknown).get("html", ""),
          "and the role pane's State still says Unknown and when it expires")
    check(jobs_page.state_tag({"closed_at": ago(1), "closed_source": "page_check"})
          .startswith('<span class="tag tag-neutral">Closed ')
          and jobs_page.state_tag({"closed_at": ago(1), "closed_source": "hand"}) == ""
          and jobs_page.state_tag({}) == "",
          "a row's only state tag is Closed, for an advert the checker confirmed closed")
    rface = next(o for o in jobs_page.build_model(collect(db))["lists"]["open"] if o["id"] == revived)
    check(jobs_page.first_seen(rface)[:10] == ago(90)[:10] and "First seen" not in row_html(html, revived),
          "the first sighting is the chain's, an expired advert's included, and the row "
          "does not show it")

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
    for kind, label in (("ack", "Acknowledge"), ("dismiss", "Dismiss"),
                        ("close", "Close role"), ("apply", "Mark applied")):
        check(f'data-act="{kind}" data-id="{spec}">{label}</button>' in html,
              f"the {label} action is there, with no key hint")
    check(f'href="/prepare/{spec}" data-prepare="{spec}">Prepare application</a>' in html
          and "Open the advert" in html and "<kbd" not in html
          and 'aria-label="Previous job"' in html and 'aria-label="Next job"' in html
          and "data-shortcut" not in html and "aria-keyshortcuts" not in html,
          "Prepare application and Open the advert are there, and no button carries a key")
    gates = re.findall(r'<span class="gate-name">([^<]+)</span><span class="gate-status '
                       r'(is-[a-z]+)">(.*?)</span><span class="gate-note">([^<]*)</span>', html)
    words = [re.sub(r"<[^>]+>", "", g[2]) for g in gates]
    check([g[0] for g in gates] == ["Sector", "CV match", "Location", "Rate"]
          and words == ["Passes", "Passes, 80%", "Passes", "Passes"]
          and all(g[1] == "is-pos" for g in gates) and gates[0][3] == "infusion pumps",
          f"the score block shows the four gates with their notes, in words ({words})")
    score = re.search(r'<section class="sec score".*?</section>', html, re.S).group(0)
    check('<p class="tier-line"><span class="tier-pill t-top">Top prospect</span></p>' in score
          and score.index("tier-pill") < score.index("gate-strip")
          and '<span class="r-bit gate-figure">80%</span>' in score
          and score.index("CV match") < score.index("80%") < score.index("Location")
          and "cv-line" not in html and "cv-figure" not in html,
          "Score is its own section: the tier's pill, then the gates, the CV figure inside "
          "the CV match gate and no large figure of its own")
    old_html = pane(db, old)["html"]
    check("Scored on" not in html and "97f11cce" not in html
          and "cv-20261029-091500.md" not in html and "claude-sonnet" not in html
          and 'class="basis"' not in html and "Scored on" not in old_html
          and "pre-record" not in old_html,
          "the pane says nothing of how the score was made: no basis, date, model, prompt or CV")
    meta = re.search(r'<p class="dh-facts"><span class="dh-facts-in">(.*?)</span></p>', html)
    face = next(o for o in jobs_page.build_model(collect(db))["lists"]["open"] if o["id"] == spec)
    bits = jobs_page.facts_bits(face)
    bits[0] = f'<a class="link" id="detail-company" href="/company/{company}">{bits[0]}</a>'
    check(meta is not None and meta.group(1) == jobs_page.meta_html(bits)
          and " · " not in meta.group(1) and ".dh-title{grid-area:title;align-self:center;"
          "font-size:22px" in jobs_page.JOBS_CSS,
          "under a larger title the pane says the row's facts the row's way, set apart by "
          "the drawn rule, the company linked to its page")
    check("·" not in re.sub(r"<style.*?</style>", "", html, flags=re.S),
          "and no middle dot anywhere in the role pane")
    check('<a class="evidence" id="reason-1-evidence" href="#q-1">' in html
          and '<mark id="q-1">You will own the IEC 62304 software lifecycle</mark>' in html
          and '<mark id="q-2">Act as internal auditor under ISO 13485.</mark>' in html
          and "A reason with no quote.</span></li>" in html,
          "each reason's quote links to the words it rests on, marked in the advert, "
          "and a reason without a quote stands alone")
    check("3 met, 2 partly, 1 missing. " in html
          and f'<a class="link" id="asks-in-prepare" href="/prepare/{spec}">' in html,
          "the asks summary counts the asks and links to Prepare application")
    facts = dict(re.findall(r"<dt>([^<]+)</dt><dd>(.*?)</dd>", html))
    check(facts.get("Rate as advertised") == "£720/day, outside IR35"
          and "Day rate £700–£750, outside IR35." in facts.get("Rate line", "")
          and facts.get("State", "").startswith("Live, seen ")
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
    unread = re.search(r'<div class="advert-body">(.*?)</div></section>', card_html, re.S)
    check(unread is not None
          and unread.group(1) == (f'<div class="advert-unread"><a class="btn btn-secondary" '
                                  f'data-prepare="{card}" href="/prepare/{card}">Prepare '
                                  'application</a><p>Advert not read yet.</p></div>')
          and "<mark" not in card_html and 'href="#q-' not in card_html
          and "&ldquo;IVD analyser&rdquo;" in card_html,
          "an advert never read says Advert not read yet. beside Prepare application and no "
          "more; the card's quotes stay with their reasons, unlinked")
    check('id="asks-heading"' not in card_html and "No asks yet" not in card_html,
          "and there is no Asks section until there are asks")
    check(dump(db) == before, "drawing the panes wrote nothing")


# ------------------------------------------------------------------ the advert

def _shown_words(fragment: str) -> list:
    """The words a reader sees in a piece of the page, in order."""
    import html as html_mod
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", fragment)).split()


def _typed_words(text: str) -> list:
    """The advert's words, its typed bullet and number marks aside."""
    return " ".join(jobs_page._BULLET.sub("", line) for line in text.split("\n")).split()


def _blocks_words(text: str, blocks: list) -> list:
    return [w for _kind, items in blocks for item in items
            for w in text[item[0]:item[1]].split()]


def _blocks_text(text: str, blocks: list) -> str:
    return " ".join(text[item[0]:item[1]] for _kind, items in blocks for item in items)


def advert_checks(tmp: Path) -> None:
    import check_jobs
    import seed_jobs_sample
    from fixtures import advert_samples as samples

    # The page checker's own output, one line per block, from the saved pages.
    pages = sorted((TEST_DIR / "fixtures" / "linkedin" / "pages").glob("*.html"))
    stored = {p.stem: check_jobs.read_page(p.read_text(encoding="utf-8"))["description"]
              for p in pages}
    stored = {k: v for k, v in stored.items() if v}
    check(len(stored) >= 5 and all("\n\n" not in v for v in stored.values())
          and not any(re.search(r"^[-*•]\s", v, re.M) for v in stored.values()),
          f"the stored description is plain text, one line per block, with no blank "
          f"line and no typed bullet ({len(stored)} saved pages)")
    every = {**stored, "seed": seed_jobs_sample.ADVERT, "stored": samples.STORED,
             "typed": samples.TYPED, "list tail": samples.LIST_TAIL,
             "all tail": samples.ALL_TAIL}
    for name, text in every.items():
        main, rest = jobs_page.advert_layout(text, "Corvane Medical")
        laid = _blocks_words(text, main) + _blocks_words(text, rest)
        drawn = _shown_words(jobs_page.advert_text_html(text, [], "Corvane Medical"))
        if rest:
            for word in ("Rest", "of", "the", "advert"):
                drawn.remove(word)
        check(sorted(laid) == sorted(_typed_words(text))
              and sorted(drawn) == sorted(_typed_words(text)),
              f"{name}: every word of the advert is shown once, none changed, added or "
              "dropped, its typed bullet and number marks aside")

    # An advert as the page checker stores it, like role 1760 on the box.
    out = jobs_page.advert_text_html(samples.STORED, [], samples.STORED_COMPANY)
    shown, _sep, folded = out.partition('<details class="advert-rest">')
    heads = re.findall(r'<h4 class="adv-h">([^<]+)</h4>', shown)
    check(heads == ["About the job", "Responsibilities", "What you will bring", "Benefits"],
          f"short lines that open a section are headings ({heads})")
    lists = re.findall(r"<ul>(.*?)</ul>", shown)
    check(len(lists) == 3 and lists[0].count("<li>") == 5 and lists[1].count("<li>") == 4
          and lists[2].count("<li>") == 3
          and "<li>Run ISO 14971 risk management for software hazards.</li>" in lists[0]
          and "<li>Pension Scheme</li>" in lists[2],
          "the lines under a heading that read as items are a list, unmarked as they are "
          "stored")
    check(shown.count("<p>") == 2 and shown.index("<p>Corvane Medical is hiring")
          < shown.index('<h4 class="adv-h">Responsibilities'),
          "two sentences on lines of their own under a heading stay paragraphs")
    check(folded.startswith("<summary>Rest of the advert</summary>")
          and out.endswith("</details>") and "<details open" not in out
          and '<h4 class="adv-h">About Corvane Medical</h4>' in folded
          and all(words in folded and words not in shown for words in (
              "equal opportunity employer", "candidate privacy policy",
              "Beware of recruitment scams", "unsolicited CVs from recruitment agencies",
              "210 people across two sites")),
          "the company's own section, the equal opportunity line, the privacy notice, the "
          "scam warning and the agency notice are folded, closed, under Rest of the "
          "advert at the end")

    # Typed by hand: blank lines, typed bullets, numbered steps.
    out = jobs_page.advert_text_html(samples.TYPED, [], samples.TYPED_COMPANY)
    shown, _sep, folded = out.partition('<details class="advert-rest">')
    check(re.findall(r'<h4 class="adv-h">([^<]+)</h4>', shown)
          == ["The role", "What you will do:", "How we hire"]
          and "<ul><li>Review the software development plan" in shown
          and '<ol><li value="1">A thirty-minute call' in shown
          and '<li value="3">An offer within a week.</li></ol>' in shown
          and "- Review" not in out and "1. A thirty" not in out
          and '<h4 class="adv-h">Equal opportunities</h4>' in folded
          and "reasonable adjustments" in folded,
          "a typed advert keeps its headings, its bullets as a list and its numbered "
          "steps as a numbered list with their own numbers; its equal opportunities "
          "section is folded")

    # Boilerplate read as items of a list folds line by line (role 1760's shape).
    text = samples.LIST_TAIL
    main, rest = jobs_page.advert_layout(text, samples.LIST_TAIL_COMPANY)
    folded_lines = [text[s:e] for kind, items in rest for s, e, *_n in items]
    check([k for k, _i in rest] == ["p"] * 7
          and [ln.split()[0] for ln in folded_lines]
          == ["Who", "How", "To", "As", "Data", "Beware", "To"],
          "the labelled company lines, the company page pointer, the equal opportunity, "
          "privacy and scam lines and the agency notice fold, each a paragraph of its own, "
          "in the advert's order")
    tail = main[main.index(("h", [next(it for k, it in main if k == "h"
                                       and text[it[0][0]:it[0][1]] == "Why join us?")[0]])):]
    check([k for k, _i in tail] == ["h", "p"]
          and text[tail[1][1][0][0]:tail[1][1][0][1]].startswith("If you are a quality lead")
          and "<li>Who we are" not in jobs_page.advert_text_html(text, [], samples.LIST_TAIL_COMPANY),
          "the ordinary lines under Why join us? stay, a list left with one line is a "
          "paragraph, and no labelled line is drawn as a list item")
    out = jobs_page.advert_text_html(text, [], samples.LIST_TAIL_COMPANY)
    shown, _sep, folded = out.partition('<details class="advert-rest">')
    check('<h4 class="adv-h">Why join us?</h4>' in shown and "<details open" not in out
          and folded.startswith("<summary>Rest of the advert</summary><p>Who we are :")
          and "Own the IEC 62304" in shown,
          "Why join us? stays, judged line by line and not by its name, and the fold is "
          "closed")
    text = samples.ALL_TAIL
    main, rest = jobs_page.advert_layout(text, samples.LIST_TAIL_COMPANY)
    check(rest and rest[0][0] == "h" and text[rest[0][1][0][0]:rest[0][1][0][1]] == "Why work with us?"
          and all(text[it[0][0]:it[0][1]] != "Why work with us?" for k, it in main if k == "h")
          and len(rest) == 4,
          "a heading whose lines all fold goes into the fold with them, and a list left "
          "with nothing is not drawn")

    # The saved pages: role lines that mention data stay where they are.
    veeva = stored.get("4472532393", "")
    main, rest = jobs_page.advert_layout(veeva, "Veeva Systems")
    check("equal opportunity employer" in _blocks_text(veeva, rest)
          and "GDPR" in _blocks_text(veeva, main)
          and "data privacy requirements" in _blocks_text(veeva, main),
          "a saved page's equal opportunity paragraph is folded, while its role lines on "
          "GDPR and data privacy stay in the advert")
    advert = seed_jobs_sample.ADVERT
    seed_main, seed_rest = jobs_page.advert_layout(advert, "Hartwell Infusion Ltd")
    check(_blocks_text(advert, seed_rest).startswith("About us Hartwell Infusion makes")
          and "No agencies, please." in _blocks_text(advert, seed_main),
          "the sample advert's About us is folded; How to apply, with its no agencies "
          "line, stays")

    # The quotes are marked wherever their words now sit.
    reasons = [{"reason": "Risk", "quote": "Run ISO 14971 risk management"},
               {"reason": "Size", "quote": "we are 210 people across two sites"},
               {"reason": "Across a heading", "quote": "Benefits Pension Scheme"},
               {"reason": "Lifecycle", "quote": "own the IEC 62304 software lifecycle"}]
    spans, hits = jobs_page._spans_for(reasons, samples.STORED)
    out = jobs_page.advert_text_html(samples.STORED, spans, samples.STORED_COMPANY)
    shown, _sep, folded = out.partition('<details class="advert-rest">')
    check(sorted(hits) == [1, 2, 3, 4]
          and '<li><mark id="q-1">Run ISO 14971 risk management</mark> for' in shown
          and '<mark id="q-2">we are 210 people across two sites</mark>' in folded
          and ('<h4 class="adv-h"><mark id="q-3">Benefits</mark></h4><ul><li><mark>Pension '
               'Scheme</mark></li>') in shown
          and all(out.count(f'id="q-{i}"') == 1 for i in range(1, 5)),
          "each quote is marked where its words sit, in a list item, a paragraph, across "
          "a heading and its first item, and in the folded part, each id once")
    check("t.closest('details:not([open])')" in jobs_page.JOBS_SCRIPT
          and "if (a && a.matches('.evidence'))" in jobs_page.JOBS_SCRIPT,
          "a quote's link into Rest of the advert opens it first, so the link lands")
    check(".advert-text{max-width:66ch;" in jobs_page.JOBS_CSS
          and ".adv-h{margin:28px 0 8px;" in jobs_page.JOBS_CSS,
          "the advert's lines run to about 80 characters, and each heading has clear "
          "space above it")

    # Role 1760's case through the pane: no rate figure, 78% CV, a stored advert.
    db = tmp / "advert.sqlite"
    conn = rc.get_db(db)
    oid = put(conn, "Software Quality Lead, ventilator firmware", samples.STORED_COMPANY,
              salary_rate=None, gate_rate=None, rate_stated=0, cv_match=78,
              gate_rate_note=None, pay_currency=None, pay_period=None, pay_min=None,
              pay_max=None,
              gate_cv_note="78% clears the 70% pass mark", score_basis="spec",
              scored_at=ago(1), description_text=samples.STORED,
              description_fetched_at=ago(1),
              description_hash=rc.description_version(samples.STORED),
              engagement_type="permanent", engagement_note="criteria Full-time")
    paid = put(conn, "Software Quality Lead, pumps", samples.STORED_COMPANY,
               engagement_note="day rate, from the page", ir35="outside")
    conn.close()
    html = pane(db, oid).get("html", "")
    paid_html = pane(db, paid).get("html", "")
    check("Rate as advertised" not in html
          and "<dt>Rate as advertised</dt><dd>£720/day, outside IR35</dd>" in paid_html,
          "Details shows Rate as advertised only when the advert states a figure")
    check("<dt>Type</dt><dd>Permanent, full-time</dd>" in html
          and "<dt>Type</dt><dd>Contract, day rate</dd>" in paid_html
          and "criteria Full" not in html and "from the page" not in paid_html
          and jobs_page.type_words({"engagement_type": "contract",
                                    "engagement_note": "contract, from the page"}) == "Contract",
          "Details' Type shows the values only, and once when both say the same")
    check('class="btn btn-secondary prevnext pn-prev is-disabled"' in html
          or 'class="btn btn-secondary prevnext pn-prev"' in html,
          "each arrow carries its own place, enabled or disabled")
    check(".prevnext{display:none}" not in jobs_page.JOBS_CSS
          and ".dh-nav .prevnext{position:absolute;top:12px;width:44px;height:44px}" in jobs_page.JOBS_CSS
          and ".dh-nav .pn-prev{right:68px}" in jobs_page.JOBS_CSS
          and ".dh-nav .pn-next{right:16px}" in jobs_page.JOBS_CSS
          and ".back{display:inline-flex;align-items:center;min-height:44px;" in jobs_page.JOBS_CSS,
          "under 1024 wide Previous and Next show at 44px on Back to Jobs' line, at the right")
    rate = re.search(r'gate-rate"><span class="gate-name">Rate</span><span class="gate-status '
                     r'(is-[a-z]+)">(.*?)</span>', html)
    cv = re.search(r'gate-cv"><span class="gate-name">CV match</span>(.*?)'
                   r'<span class="gate-note">', html)
    check(rate is not None and rate.group(1) == "is-cau"
          and rate.group(2).endswith("Not stated")
          and cv is not None and _shown_words(cv.group(1)) == ["Passes", ",", "78%"],
          "the 1760 case: Rate reads Not stated in amber, and 78% sits inside the CV "
          "match gate")
    check('<h4 class="adv-h">Responsibilities</h4><ul>' in html
          and '<details class="advert-rest"><summary>Rest of the advert</summary>' in html
          and html.index("Rest of the advert") < html.index("candidate privacy policy")
          and html.index("Rest of the advert") < html.index("Beware of recruitment scams"),
          "and its advert has headings and bullets, its privacy and scam notices under "
          "Rest of the advert")


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
    check("is-current" in q.get("html", "") and "counts" not in q
          and isinstance(q.get("stamp"), int),
          "the queue fragment has the rows, the selected one marked, and the stamp")
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
    keydowns = re.findall(r"addEventListener\('keydown', function \(e\) \{(.*?)\n  \}\);", html, re.S)
    check('id="keys-open"' not in html and "<dialog" not in html and "<kbd" not in html
          and "keysOn" not in html and "Single-key" not in html
          and keydowns and all(not re.search(r"'[jkpadcmo?]'", k) for k in keydowns),
          "no shortcuts button, no list of keys and no key hints; the page listens for "
          "Escape alone, so J, K, P and ? do nothing")
    check("No role chosen." in html and "Choose a role" not in html
          and "if (!current) {\n      var first" not in html,
          "with no role chosen the pane says so and no more, and no role opens by itself")
    check('<button type="button" class="btn btn-secondary list-toggle" id="list-toggle" '
          'aria-expanded="true" aria-controls="queue">Hide the list</button>' in html
          and ".app.list-off .queue{display:none}" in html and "'Show the list'" in html,
          "a control hides the list so the role pane takes the full width, and brings it back")
    state = re.search(r'<div class="app" data-page="jobs"[^>]* data-state="([^"]+)"', html)
    import html as htmllib
    check(state is not None and json.loads(htmllib.unescape(state.group(1)))["view"] == "open"
          and '<script type="application/json"' not in html,
          "the page's state is a data attribute, so every script block on the page is script")
    check("if (current && current.id === Number(m[1])) return;" in html,
          "a quote's link moves only the hash and redraws nothing")
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
          and re.search(r'Top prospects</button><span class="group-count">2 of 3</span>', html),
          "two chips show either, and a group counts what it shows of its whole")
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
                        advert_checks, route_checks, page_checks, location_checks):
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
