#!/usr/bin/env python
"""Checks for the experience bank (owner, 18 Nov): the two tables, the paste
reader, the backing rule on the server and the page, the routes and their
guard, and the pages at /cv/experience and /cv/experience/<id>.

  python test/run_experience_test.py

No model calls and no network: the server runs on a loopback port against a
throwaway database in a temporary folder.
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

import experience as xp  # noqa: E402
import experience_page as xpp  # noqa: E402
import page_frame  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
NOW = "2026-11-18T12:00:00Z"


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def body_only(html: str) -> str:
    return re.sub(r"(?is)<(script|style)\b.*?</\1>", "", html)


def text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


PASTE = """Here is what I found.
=== ENTRY ===
TITLE: Led the IEC 62304 gap analysis
what i did: Ran the gap analysis across three products.
Wrote the remediation plan.
Result: Not known
When: 2025 to 2026
Where: Acme Medical
Tags: IEC 62304, Risk management, , risk management
Stage: delivered
Evidence: Gap report | SharePoint | the findings | Can show
Evidence: Slides | Drive
- Memo | inbox | sign-off | top secret
Vouch: Nobody named
Questions: None
=== ENTRY ===
What I did: a block with no title at all
Stage: Delivered
=== entry ===
Title: Shaped the QMS roadmap
Stage: whatever this is
Tags: QMS
Vouch: Head of Quality
=== ENTRY ===
Title: """ + "x" * 201 + """
=== ENTRY ===
Title: Seven tags
Tags: a, b, c, d, e, f, g
NOT INCLUDED
=== ENTRY ===
Title: After the cut, never read
"""


def parse_checks() -> None:
    read, refused = xp.parse_paste(PASTE)
    check([e["fields"]["title"] for e in read] == ["Led the IEC 62304 gap analysis", "Shaped the QMS roadmap"],
          "the paste reads two entries, labels matched whatever their case, the marker whatever its case")
    first = read[0]
    check(first["fields"]["did"] == "Ran the gap analysis across three products.\nWrote the remediation plan.",
          "a field runs until the next label, its lines kept")
    check(first["tags"] == ["IEC 62304", "Risk management"],
          "tags split on commas, trimmed, empties and repeats whatever their case dropped")
    check(first["fields"]["stage"] == "delivered" and read[1]["fields"]["stage"] == "drafted",
          "a stage is read whatever its case, and an unrecognised one becomes Drafted")
    check(first["evidence"] == [
        {"name": "Gap report", "location": "SharePoint", "shows": "the findings", "level": "can_show"},
        {"name": "Slides", "location": "Drive", "shows": "", "level": "describe_only"},
        {"name": "Memo", "location": "inbox", "shows": "sign-off", "level": "describe_only"}],
        "evidence repeats one line per item, split on |, short lines kept, an unknown level Describe only")
    check(first["fields"]["vouch"] == "" and first["fields"]["questions"] == ""
          and first["fields"]["result"] == "Not known",
          "Nobody named and None are stored as empty; Not known is kept")
    check(read[1]["fields"]["vouch"] == "Head of Quality", "a named voucher is kept")
    check([(r["first_line"], r["why"]) for r in refused] == [
        ("What I did: a block with no title at all", "No title."),
        ("Title: " + "x" * 112 + "…", "Title is longer than 200 characters."),
        ("Title: Seven tags", "More than 6 tags.")],
        "an unreadable block reports its first line and why: no title, a field over its cap, too many tags")
    check(all("After the cut" not in e["fields"]["title"] for e in read)
          and all("After the cut" not in r["first_line"] for r in refused),
          "nothing after a NOT INCLUDED line is read")
    check(xp.parse_paste("Here is some prose with no entries at all.") ==
          ([], [{"first_line": "Here is some prose with no entries at all.", "why": "No title."}]),
          "text with no marker at all is one block, and says why it was not read")
    check(xp.parse_paste("Intro words\n=== ENTRY ===\nTitle: One\n")[1] == [],
          "words before the first marker with no label in them are not a block")
    one = xp.parse_paste("Title: Only one, unmarked\nStage: Discussed\n")[0]
    check(len(one) == 1 and one[0]["fields"]["stage"] == "discussed",
          "a single entry pasted without its marker is read")


def storage_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "x.sqlite"
        conn = rc.get_db(db)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            check({"experience_entries", "experience_evidence"} <= tables, "the two tables are made by get_db")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(experience_entries)")]
            check(cols == ["id", "title", "did", "result", "period", "place", "tags", "stage", "vouch",
                           "questions", "status", "created_at", "updated_at"],
                  "experience_entries has exactly the brief's columns")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(experience_evidence)")]
            check(cols == ["id", "entry_id", "name", "location", "shows", "level"],
                  "experience_evidence has exactly the brief's columns")
            before = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
            conn.close()
            conn = rc.get_db(db)
            conn.executescript(rc.SCHEMA_PATH.read_text(encoding="utf-8"))
            after = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master")}
            check(before == after, "opening the database again, and running the schema twice, changes nothing")
            base = {"title": "One", "stage": "delivered", "tags": "a, b", "evidence": []}
            r = xp.save_entry(conn, {**base, "status": "backed"}, NOW)
            check(r["ok"] and r["status"] == "draft", "a new entry is a draft, whatever status was sent")
            eid = r["id"]
            r = xp.save_entry(conn, {**base, "id": eid, "status": "backed"}, NOW)
            check(not r["ok"] and r["note"] == xp.NEEDS_BACKING
                  and conn.execute("SELECT status FROM experience_entries WHERE id=?", (eid,)).fetchone()[0] == "draft",
                  "the server refuses Mark backed with no evidence and no voucher, and changes nothing")
            r = xp.save_entry(conn, {**base, "id": eid, "stage": "discussed", "vouch": "CTO", "status": "backed"}, NOW)
            check(not r["ok"] and r["note"] == xp.DISCUSSED, "the server refuses Mark backed for discussed work")
            r = xp.save_entry(conn, {**base, "id": eid, "vouch": "CTO", "status": "backed"}, NOW)
            check(r["ok"] and r["status"] == "backed", "a named voucher is enough to back an entry")
            r = xp.save_entry(conn, {**base, "id": eid, "vouch": "", "status": None}, NOW)
            check(not r["ok"] and r["note"] == xp.NEEDS_BACKING,
                  "a backed entry cannot be saved out of what backing needs")
            ev = [{"name": "Report", "location": "Drive", "shows": "it", "level": "public"},
                  {"name": "", "location": "", "shows": "", "level": "public"},
                  {"name": "Memo", "level": "nonsense"}]
            r = xp.save_entry(conn, {**base, "id": eid, "vouch": "", "evidence": ev}, NOW)
            got = xp.get_entry(conn, eid)
            check(r["ok"] and [(e["name"], e["level"]) for e in got["evidence"]] == [("Report", "public"), ("Memo", "describe_only")],
                  "evidence is enough to stay backed; an empty row is dropped, an unknown level is Describe only")
            check(not xp.save_entry(conn, {**base, "id": eid, "title": "  "}, NOW)["ok"]
                  and not xp.save_entry(conn, {**base, "id": eid, "did": "x" * 2001}, NOW)["ok"]
                  and not xp.save_entry(conn, {**base, "id": eid, "tags": "a,b,c,d,e,f,g"}, NOW)["ok"]
                  and not xp.save_entry(conn, {**base, "id": eid, "stage": "shipped"}, NOW)["ok"]
                  and not xp.save_entry(conn, {**base, "id": True}, NOW)["ok"],
                  "a save is refused for an empty title, a field over its cap, seven tags, an unknown stage, a bool id")
            conn.commit()
            r = xp.delete_entry(conn, eid)
            conn.commit()
            check(r["ok"] and conn.execute("SELECT COUNT(*) FROM experience_evidence WHERE entry_id=?", (eid,)).fetchone()[0] == 0
                  and xp.get_entry(conn, eid) is None, "deleting an entry deletes its evidence")
            check(not xp.delete_entry(conn, eid)["ok"], "deleting it again says it is gone")
            other = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            check(other == tables, "nothing else in the database was added or changed")
        finally:
            conn.close()


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


def route_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "x.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        with serving(db) as port:
            own = {"Host": f"127.0.0.1:{port}"}
            w = {**own, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
                 "X-Radar-Request": "1"}
            status, ctype, body = call(port, "GET", "/cv/experience", headers=own)
            page = body.decode()
            check(status == 200 and ctype.startswith("text/html") and "Nothing to review." in page,
                  "GET /cv/experience draws before the tables exist, and opening it writes nothing")
            names = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
            check("experience_entries" not in names, "a page render never creates the tables")

            def post(path, payload, headers=w):
                s, _c, b = call(port, "POST", path, json.dumps(payload).encode(), headers)
                return s, json.loads(b or b"{}")
            for path in ("/cv/experience/save", "/cv/experience/delete", "/cv/experience/paste"):
                s1, _ = post(path, {"text": PASTE, "id": 1, "title": "x", "stage": "drafted"},
                             {**w, "Origin": "https://evil.example"})
                s2, _ = post(path, {"text": PASTE}, {k: v for k, v in w.items() if k != "X-Radar-Request"})
                check(s1 == 403 and s2 == 403,
                      f"POST {path} refuses another site's Origin and a request without the write header")
            names = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
            check("experience_entries" not in names, "a refused POST never reached the database")
            s, j = post("/cv/experience/paste", {"text": PASTE})
            check(s == 200 and j["added"] == 2 and len(j["refused"]) == 3,
                  "POST /cv/experience/paste adds what reads and reports what does not")
            rows = sqlite3.connect(db).execute("SELECT status, title FROM experience_entries ORDER BY id").fetchall()
            check(rows == [("draft", "Led the IEC 62304 gap analysis"), ("draft", "Shaped the QMS roadmap")],
                  "every pasted entry is a draft, and nothing from an unreadable block was saved")
            s, j = post("/cv/experience/paste", {"text": 5})
            check(s == 422, "a paste that is not text is refused")
            first, second = [r[0] for r in sqlite3.connect(db).execute("SELECT id FROM experience_entries ORDER BY id")]
            s, j = post("/cv/experience/save", {"id": first, "title": "Led the IEC 62304 gap analysis",
                                                "stage": "delivered", "tags": "IEC 62304", "evidence": [],
                                                "status": "backed"})
            check(s == 422 and j["note"] == xp.NEEDS_BACKING, "POST save refuses Mark backed without backing, 422")
            s, j = post("/cv/experience/save", {"id": first, "title": "Led the IEC 62304 gap analysis",
                                                "stage": "delivered", "tags": "IEC 62304",
                                                "place": "Acme Medical", "period": "2025 to 2026",
                                                "evidence": [{"name": "Report", "level": "can_show"}],
                                                "status": "backed"})
            check(s == 200 and j["status"] == "backed", "POST save backs an entry with evidence")
            s, j = post("/cv/experience/save", {"title": "Hand made", "stage": "discussed", "evidence": []})
            check(s == 200 and j["created"], "POST save with no id creates a draft")
            s, j = post("/cv/experience/save", {"id": 999, "title": "Gone", "stage": "drafted"})
            check(s == 404, "saving an entry that is not on file is a 404")
            s, j = post("/cv/experience/delete", {"id": "1"})
            check(s == 400, "delete needs an integer id")
            status, _c, body = call(port, "GET", "/cv/experience", headers=own)
            page = body.decode()
            list_checks(page)
            status, _c, body = call(port, "GET", f"/cv/experience/{first}", headers=own)
            entry_checks(body.decode(), backed=True)
            status, _c, body = call(port, "GET", f"/cv/experience/{second}", headers=own)
            draft = body.decode()
            check(status == 200 and '<button type="button" class="btn" data-xp-status="backed" id="xp-back" '
                  'aria-describedby="xp-why">Mark backed</button><span class="xp-why" id="xp-why"></span>' in draft,
                  "a draft with a named voucher has Mark backed enabled, with no line beside it")
            s, j = post("/cv/experience/save", {"id": second, "title": "Shaped the QMS roadmap",
                                                "stage": "drafted", "vouch": "", "evidence": []})
            status, _c, body = call(port, "GET", f"/cv/experience/{second}", headers=own)
            check('aria-describedby="xp-why" disabled>Mark backed</button><span class="xp-why" id="xp-why">'
                  'Add evidence or someone who can vouch.</span>' in body.decode(),
                  "Mark backed is disabled with the line saying what is missing")
            status, _c, body = call(port, "GET", "/cv/experience/new", headers=own)
            new = body_only(body.decode())
            check(status == 200 and "<h1 id=\"xp-h1\">New entry</h1>" in new and "data-xp-status" not in new
                  and "xp-del-open" not in new and 'value="drafted" checked' in new,
                  "Add entry opens an empty form: Save only, Drafted chosen, no status buttons and no Delete")
            for bad in ("/cv/experience/999", "/cv/experience/abc", "/cv/experience/-1"):
                check(call(port, "GET", bad, headers=own)[0] == 404, f"GET {bad} is a 404")
            s, j = post("/cv/experience/delete", {"id": first})
            left = sqlite3.connect(db).execute(
                "SELECT COUNT(*) FROM experience_evidence WHERE entry_id = ?", (first,)).fetchone()[0]
            check(s == 200 and left == 0, "POST delete removes the entry and its evidence")
            cv = call(port, "GET", "/cv", headers=own)[2].decode()
            check('<nav class="cv-switch" aria-label="CV pages"><a href="/cv" aria-current="page">CV</a>'
                  '<a href="/cv/experience">Experience</a></nav>' in cv
                  and cv.index("</header>", cv.index('class="cv-head"')) < cv.index('class="cv-switch"'),
                  "the CV page has the switch under its title row, CV marked")


def list_checks(page: str) -> None:
    bar = page[page.index('<header class="appbar">'):page.index("</header>")]
    check('<a href="/cv" data-group="cv" aria-current="page">CV</a>' in bar,
          "the menu marks CV, in rose, with no new menu item")
    check(page.count('href="/cv/experience"') == 1 + 0 and "Experience</a></nav>" in page
          and '<a href="/cv/experience" aria-current="page">Experience</a>' in page,
          "the switch under the title row marks Experience")
    head = page[page.index('<header class="xp-head">'):page.index("</header>", page.index('<header class="xp-head">'))]
    check('<span class="gchip g-cv"' in head and "<h1>Experience</h1>" in head
          and '<span class="pill" id="xp-n-backed">1 backed</span>' in head
          and '<span class="pill" id="xp-n-review">2 to review</span>' in head
          and head.index("Paste entries") < head.index("Add entry")
          and '<a class="btn btn-rose" href="/cv/experience/new">Add entry</a>' in head,
          "the title row: rose chip, Experience, N backed and N to review, Paste entries then Add entry")
    check(".btn-rose,.xp a.btn-rose{background:#e88fb4;border-color:#e88fb4;color:#2a0f1c" in xpp.XP_CSS,
          "Add entry is solid rose, ground #e88fb4 and text #2a0f1c")
    titles = re.findall(r'<h2 class="sec-title" id="xp-(\w+)-h">([^<]+)</h2><span class="sec-count">(\d+)</span>', page)
    check(titles == [("draft", "To review", "2"), ("backed", "Backed", "1"), ("parked", "Parked", "0")],
          "three sections in order, To review, Backed, Parked, each titled outside its cards with a count")
    check('<p class="xp-none">Nothing parked.</p>' in page and "Nothing to review." not in page,
          "an empty section shows its title and one muted line")
    card = re.search(r'<li><a class="card g-cv glow-faint xp-card" href="/cv/experience/(\d+)">(.*?)</a></li>', page)
    check(card is not None, "an entry is one card with the faint rose glow, and the whole card opens it")
    backed = page[page.index('id="xp-backed-h"'):page.index('id="xp-parked-h"')]
    check('<span class="xp-t">Led the IEC 62304 gap analysis</span>' in backed
          and '<span class="dv xp-facts"><span class="dv-in"><span class="dv-i">Acme Medical</span>'
              f'<span class="dv-i">{page_frame.SR_COMMA}2025 to 2026</span></span></span>' in backed
          and '<li class="chip-v">IEC 62304</li>' in backed
          and '<span class="stage st-delivered">Delivered</span><span class="xp-ev">1 evidence</span>' in backed,
          "a card: the title, place and period with the shared divider, tag chips, its stage chip and evidence count")
    check(".st-delivered{" in xpp.XP_CSS and "var(--positive)" in xpp.XP_CSS.split(".st-delivered{")[1].split("}")[0]
          and "var(--caution)" in xpp.XP_CSS.split(".st-drafted{")[1].split("}")[0]
          and "var(--neutral)" in xpp.XP_CSS.split(".st-discussed{")[1].split("}")[0],
          "the stage chip: Delivered green, Drafted amber, Discussed neutral")
    check('id="xp-paste-dlg"' in page and ">Add as drafts</button>" in page and 'id="xp-paste-in"' in page,
          "Paste entries opens a dialog with one large text box and Add as drafts")
    check("@media (max-width:767.98px){" in xpp.XP_CSS
          and ".xp-acts{flex-basis:100%;margin-left:0}" in xpp.XP_CSS
          and ".xp-dlg{width:calc(100vw - 32px);margin:auto 16px;padding:20px}" in xpp.XP_CSS
          and ".pair,.ev-row{grid-template-columns:minmax(0,1fr)}" in xpp.XP_CSS,
          "on a phone: one column, the buttons wrap under the title, the dialog full width with 16px margins")
    rules_checks(page, "the list page")


def entry_checks(page: str, backed: bool) -> None:
    page = body_only(page)
    check('<p class="xp-backlink"><a class="plain" href="/cv/experience">Back to Experience</a></p>' in page
          and page.index("Back to Experience") < page.index('<header class="xp-head">'),
          "an open entry has Back to Experience above it")
    labels = re.findall(r'<label for="xp-(\w+)">([^<]+)</label>', page)
    check([l for _k, l in labels] == ["Title", "What I did", "Result", "When", "Where", "Tags",
                                     "Who can vouch", "Questions"]
          and "<legend>Stage</legend>" in page and page.count('name="stage"') == 3,
          "every field is editable: Title, What I did, Result, When, Where, Tags, Stage, Who can vouch, Questions")
    check('<h2 class="xp-h2" id="xp-ev-h">Evidence</h2>' in page and "data-ev-remove>Remove</button>" in page
          and 'id="xp-ev-add">Add evidence</button>' in page
          and re.search(r'<option value="can_show" selected>Can show</option><option value="describe_only">'
                        r'Describe only</option><option value="public">Public</option>', page),
          "the Evidence section: each item's name, where, what it shows and level, Remove, and Add evidence")
    check('id="xp-save">Save</button>' in page and page.index('id="xp-ev-h"') < page.index('id="xp-save"'),
          "Save at the bottom")
    if backed:
        foot = page[page.index('<div class="xp-foot">'):page.index('<p class="say" id="xp-say"')]
        check("Mark backed" not in foot and '>Park</button>' in foot and '>Back to review</button>' in foot
              and 'id="xp-del-open" aria-haspopup="dialog">Delete</button>' in foot
              and 'id="xp-del-dlg"' in page and ">Delete this entry and its evidence?</h2>" in page,
              "a backed entry offers Park, Back to review and Delete, which asks first")
        check('<span class="pill st-backed">Backed</span>' in page, "its status shows by the title")
    rules_checks(page, "the entry page")


def rules_checks(page: str, where: str) -> None:
    words = text(page)
    check("·" not in page, f"{where} has no middle dots")
    check(not re.search("[✓✔✅☑]", page)
          and not re.search("[\U0001F300-\U0001FAFF☀-➿]", words),
          f"{where} has no tick characters and no emoji")
    check("<kbd" not in page and "aria-keyshortcuts" not in page[page.index('<main'):],
          f"{where} has no key hints")
    sizes = [int(n) for n in re.findall(r"font(?:-size)?:\s*(?:\d+\s+)?(\d+)px", xpp.XP_CSS)]
    check(sizes and min(sizes) >= 13, f"{where}: nothing under 13px in its own style")
    check(not re.search(r"<(script|link)[^>]+(src|href)=\"https?://", page), f"{where} loads nothing from another site")


def script_checks() -> None:
    js = xpp.XP_JS
    check("fetch(path, { method: 'POST'" in js and "build_dashboard.SCRIPT" in Path(xpp.__file__).read_text(),
          "the page posts with fetch and the shared script, so every write carries the write header")
    check("Discussed work can't be backed." in js and "Add evidence or someone who can vouch." in js
          and xp.DISCUSSED == "Discussed work can't be backed." and xp.NEEDS_BACKING == "Add evidence or someone who can vouch.",
          "the page holds the same rule and the same words as the server")
    check("plural(j.added, 'entry', 'entries') + ' added as drafts.'" in js and "r.first_line" in js and "r.why" in js,
          "after a paste the page says how many were added and, for each block not read, its first line and why")


def prepare_checks() -> None:
    """Commit 2: From your experience on Prepare application."""
    import prepare_page
    import seed_experience_sample
    import seed_jobs_sample
    cfg = rc.load_config()
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "x.sqlite"
        ids = seed_jobs_sample.seed(db)
        t1 = ids["T1"]

        def draw() -> str:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                return prepare_page.render_prepare_page(conn, t1, cfg)
            finally:
                conn.close()
        page = draw()
        check('data-slot="experience"' in page and "From your experience" not in body_only(page),
              "with no entries at all the fold does not show")
        conn = rc.get_db(db)
        bank = seed_experience_sample.seed(conn, NOW)
        conn.close()
        before = db.read_bytes()
        page = draw()
        check(db.read_bytes() == before, "drawing Prepare with the fold writes nothing")
        body = body_only(page)
        fold = body[body.index('<details class="pa-xp">'):body.index("</details>", body.index('<details class="pa-xp">'))]
        col = body.index('id="cv-h">Tailored CV</h2>')
        at = body.index('<details class="pa-xp">')
        sheet = body.find('<div class="sheet-wrap">')
        check(col < at and (at < sheet if sheet >= 0 else body.index('class="notice warn pa-unlaid"') < at),
              "the fold is in the Tailored CV column, above the sheet (under the save-your-master line "
              "when there is no sheet yet)")
        check('<summary><span class="xp-sum">From your experience</span><span class="xp-n">6</span></summary>' in fold
              and "color:var(--group-cv)" in prepare_page.PREPARE_CSS.split(".pa-xp summary{")[1].split("}")[0],
              "one fold titled From your experience with its count, in rose")
        rows = re.findall(r'<li class="xp-row"( hidden data-xp-more)?><a class="plain xp-link" '
                          r'href="/cv/experience/(\d+)">([^<]+)</a><ul class="chips" aria-label="Matched tags">(.*?)</ul></li>', fold)
        counts = [len(re.findall("chip-v", r[3])) for r in rows]
        check(len(rows) == 6 and counts == sorted(counts, reverse=True)
              and [bool(r[0]) for r in rows] == [False] * 5 + [True]
              and '<button type="button" class="btn-text" data-xp-all>Show all 6</button>' in fold,
              "most matching tags first, five shown, Show all 6 for the rest")
        shown = {int(r[1]) for r in rows}
        backed = {i for i, f in zip(bank["ids"], seed_experience_sample.FATE) if f == "backed"}
        check(shown == backed, "only backed entries appear; the drafts and the parked entry, whose tags the advert "
                               "also names, never do")
        check('<li class="chip-v">IEC 62304</li>' in rows[0][3] and '<li class="chip-v">ISO 14971</li>' in rows[0][3]
              and "<li class=\"chip-v\">FDA 510(k)</li>" in fold,
              "each row links to its entry and shows the matched tags as chips")
        check("textarea" not in fold and "<input" not in fold and fold.count("<button") == 1,
              "the fold is read only")
        check("[data-xp-all]" in prepare_page.PREPARE_SCRIPT and "'experience'" in prepare_page.PREPARE_SCRIPT,
              "Show all opens the rest, and the fold is redrawn with the page's parts after a press")
        conn = rc.get_db(db)
        conn.execute("UPDATE experience_entries SET status = 'parked' WHERE status = 'backed'")
        conn.commit()
        conn.close()
        check("From your experience" not in body_only(draw()), "with no backed entries the fold does not show")
        conn = rc.get_db(db)
        conn.execute("UPDATE experience_entries SET status = 'backed', tags = 'infusio, ra, qual'"
                     " WHERE id = ?", (bank["backed"],))
        conn.commit()
        conn.close()
        check("From your experience" not in body_only(draw()),
              "a tag matches only as a whole word or phrase, never inside one (infusio, ra, qual)")
        conn = rc.get_db(db)
        conn.execute("UPDATE experience_entries SET tags = 'SOFTWARE QUALITY, hardware-IN-the-loop' WHERE id = ?",
                     (bank["backed"],))
        conn.commit()
        conn.close()
        fold = body_only(draw())
        check('<span class="xp-n">1</span>' in fold and "SOFTWARE QUALITY" in fold and "hardware-IN-the-loop" in fold,
              "whatever the case, and a phrase across words")
    check(xp.matched_tags(["IEC 62304", "C++", "FDA 510(k)"], "iec\n62304, C++ and fda 510(k).") ==
          ["IEC 62304", "C++", "FDA 510(k)"], "a phrase matches across a line break, and tags with symbols match")
    check(xp.matched_tags(["AI"], "We maintain it.") == [], "a short tag never matches inside a word")


def main() -> int:
    ap = argparse.ArgumentParser(description="Experience bank checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    for section in (parse_checks, storage_checks, route_checks, script_checks, prepare_checks):
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
    print("PASS. Every experience bank check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
