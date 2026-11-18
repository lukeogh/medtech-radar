#!/usr/bin/env python
"""Checks for roles added by hand (hand_roles, the Add a role dialog on Jobs):
the two columns and their migration, the fields and their checks, the place
rules, the route and its guard, Add and score over a real server, how an
added role reads on Jobs and Prepare application, and that the advert
checker, the expiry pass and rescore leave these roles alone.

  python test/run_add_role_test.py

RADAR_MOCK=1: the score is the deterministic mock, no model and no network.
The server runs on a loopback port against a throwaway database.
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
from pathlib import Path

os.environ.setdefault("RADAR_MOCK", "1")

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import add_role_page  # noqa: E402
import check_jobs  # noqa: E402
import hand_roles as hr  # noqa: E402
import jobs_page  # noqa: E402
import prepare_application  # noqa: E402
import prepare_page  # noqa: E402
import radar_common as rc  # noqa: E402
import rescore  # noqa: E402
import score_item  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
NOW = "2026-11-18T12:00:00Z"
CONFIG = rc.load_config()

ADVERT = """Head of Software for a medical device start-up.

You will lead the firmware team through IEC 62304 and own the software
development plan. Remote, with a monthly visit.
"""


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|template|dialog)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def body(**over) -> dict:
    b = {"link": "https://www.upwork.com/jobs/~01abc", "title": "Head of Software, medical devices",
         "advert": ADVERT, "client": "", "location": "Remote", "type": "contract",
         "source": "upwork", "rate_min": "60", "rate_max": "90", "currency": "USD", "period": "hour"}
    b.update(over)
    return b


# ------------------------------------------------------------ storage

def storage_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.sqlite"
        schema = (REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        old = schema.replace(
            "  engagement_note   TEXT,             -- the phrase that decided it, \"fixed term\" and the like\n"
            "  -- Roles added by hand on Jobs. When, and the type chosen. NULL on every\n"
            "  -- collected row. Set in code, never by a model (hand_roles).\n"
            "  added_at          TEXT,\n"
            "  hand_type         TEXT              -- contract | permanent | fixed-price\n",
            "  engagement_note   TEXT\n")
        check(old != schema, "the schema's opportunities table carries the two columns")
        conn = sqlite3.connect(db)
        conn.executescript(old)
        conn.execute("INSERT INTO opportunities (url_hash, first_seen, title) VALUES ('h1', ?, 'A role')", (NOW,))
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(opportunities)")}
        conn.close()
        check("added_at" not in cols and "hand_type" not in cols,
              "a database from before has neither column")
        for _ in range(2):
            rc.get_db(db).close()
        conn = sqlite3.connect(db)
        info = [r for r in conn.execute("PRAGMA table_info(opportunities)") if r[1] in ("added_at", "hand_type")]
        row = conn.execute("SELECT added_at, hand_type FROM opportunities WHERE url_hash = 'h1'").fetchone()
        n = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        conn.close()
        check(len(info) == 2 and all(r[2] == "TEXT" and r[3] == 0 and r[4] is None for r in info),
              "get_db adds both as nullable TEXT, once, opened twice")
        check(row == (None, None) and n == 1, "the row on file reads NULL for both and nothing else changed")
    mig = [m for m in rc._MIGRATIONS if m[1] in ("added_at", "hand_type")]
    check(mig == [("opportunities", "added_at", "TEXT"), ("opportunities", "hand_type", "TEXT")],
          "both are in _MIGRATIONS, the buying_window pattern")


# ------------------------------------------------------------ fields

def field_checks() -> None:
    _f, e = hr.check_fields(body(title="  ", advert="\n \n"))
    check(e.get("title") == "Add a title." and e.get("advert") == "Paste the advert.",
          "no title says Add a title., no advert says Paste the advert.")
    f, e = hr.check_fields(body())
    check(not e and f["pay_currency"] == "USD" and f["pay_period"] == "hour"
          and f["pay_min"] == 60 and f["pay_max"] == 90, "a full role reads clean, the rate as written")
    f, e = hr.check_fields(body(rate_min="", rate_max="", type=None, source=None))
    check(not e and f["pay_min"] is None and f["type"] == "contract" and f["source"] == "other",
          "the rate is optional; with no type it is a contract, with no source Other")
    f, e = hr.check_fields(body(rate_min="4,000", rate_max="", period="fixed", currency="GBP", type="fixed-price"))
    check(not e and f["pay_min"] == f["pay_max"] == 4000 and f["pay_period"] == "fixed",
          "a single fixed price is its own top")
    for bad in ({"rate_min": "abc"}, {"rate_min": "90", "rate_max": "60"}, {"rate_min": "", "rate_max": "60"},
                {"currency": "JPY"}, {"period": "week"}, {"rate_min": "-5"}):
        _f, e = hr.check_fields(body(**bad))
        if e.get("rate") != "Check the rate.":
            check(False, f"a bad rate is refused: {bad}")
            break
    else:
        check(True, "a rate that is not a number, an upper under the lower, an upper alone, "
                    "or a currency or period not offered says Check the rate.")
    _f, e = hr.check_fields(body(link="javascript:alert(1)"))
    check(e.get("link") == "Check the link.", "a link that is not http or https is refused")
    _f, e = hr.check_fields(body(title="x" * 201, advert="y" * 20_001))
    check("title" in e and "advert" in e, "a title over 200 or an advert over 20,000 characters is refused, not cut")
    _f, e = hr.check_fields(body(type="freelance", source="email"))
    check("type" in e and "source" in e, "a type or source not offered is refused")
    f, _e = hr.check_fields({**body(), "title": "Changed", "advert": "Changed"}, only=hr.EDITABLE)
    check("title" not in f and "advert" not in f, "Edit reads no title and no advert")


def place_checks() -> None:
    cases = {"Remote": "Elsewhere", "London": "Local", "Guildford, Surrey": "Local",
             "Manchester, UK": "UK", "Berlin, Germany": "Europe", "Austin, Texas": "Elsewhere",
             "Remote (UK)": "UK", "": "Elsewhere"}
    got = {k: hr.region_of(k, CONFIG) for k in cases}
    check(got == cases, f"the region comes from the location by the existing rules ({got})")
    check(hr.location_class("Remote") == "remote" and hr.location_class("remote, UK") == "remote"
          and hr.location_class("Remoteville") is None and hr.location_class("London") is None,
          "Remote, a whole word in any case, makes the role remote; anything else is left to the scorer")
    check(jobs_page.loc_matches({"region": "Elsewhere", "location_class": "remote"}, ("remote",)),
          "a remote role shows under the Remote chip")


# ------------------------------------------------------------ storing

def add_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = rc.get_db(Path(tmp) / "a.sqlite")
        conn.execute("INSERT INTO companies (norm_name, display_name, first_seen) VALUES ('hartwell infusion', 'Hartwell Infusion', ?)", (NOW,))
        before = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        out = hr.add_role(conn, body(client="Hartwell  Infusion"), NOW, CONFIG)
        conn.commit()
        row = dict(conn.execute("SELECT * FROM opportunities WHERE id = ?", (out["id"],)).fetchone())
        check(out.get("ok") and row["added_at"] == NOW and row["hand_type"] == "contract"
              and row["source"] == "hand-upwork" and row["tier"] is None,
              "the role is stored unscored, added by you, Upwork its source")
        check(row["description_text"] == ADVERT.strip("\n") and row["description_fetched_at"] == NOW
              and row["read_at"] == NOW and row["status"] == "new",
              "the pasted advert is its description, and a role you added is not New to you")
        check(row["salary_rate"] == "$60 to $90 an hour" and row["pay_currency"] == "USD"
              and row["pay_period"] == "hour" and (row["pay_min"], row["pay_max"]) == (60, 90),
              "the rate is stored as written")
        check(row["location_class"] == "remote" and row["region"] == "Elsewhere"
              and row["engagement_type"] == "contract", "Remote is remote, contract is contract")
        check(row["company_id"] is not None and conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == before,
              "a client matching a company on file links to it, and no company is created")
        again = hr.add_role(conn, body(title="Another", advert="Another"), NOW, CONFIG)
        check(again == {"ok": True, "existing": out["id"]}, "a link Radar already holds is not added twice")
        a = hr.add_role(conn, body(link="", client="Nobody Yet"), NOW, CONFIG)
        b = hr.add_role(conn, body(link=""), NOW, CONFIG)
        conn.commit()
        check(a.get("id") and b.get("id") and a["id"] != b["id"],
              "with no link, two roles are two roles")
        check(conn.execute("SELECT company_id FROM opportunities WHERE id = ?", (a["id"],)).fetchone()[0] is None
              and conn.execute("SELECT COUNT(*) FROM companies WHERE norm_name = 'nobody yet'").fetchone()[0] == 0,
              "a client not on file links nowhere and makes no company")
        bad = hr.add_role(conn, body(title=""), NOW, CONFIG)
        check(bad.get("invalid") and bad["errors"] == {"title": "Add a title."},
              "without a title nothing is stored")
        conn.close()


# ------------------------------------------------------------ leaving them alone

def skip_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = rc.get_db(Path(tmp) / "s.sqlite")
        old = "2026-09-01T09:00:00Z"
        out = hr.add_role(conn, body(link="https://www.linkedin.com/jobs/view/4100000001/"), old, CONFIG)
        conn.execute("UPDATE opportunities SET tier = 'top', last_seen = ? WHERE id = ?", (old, out["id"]))
        conn.execute("INSERT INTO opportunities (url_hash, first_seen, last_seen, title, company, source_url, tier)"
                     " VALUES ('c1', ?, ?, 'Collected', 'Acme', 'https://www.linkedin.com/jobs/view/4100000002/', 'top')",
                     (old, old))
        conn.commit()
        due = [r["id"] for r in check_jobs.due_candidates(conn, NOW)]
        check(out["id"] not in due and len(due) == 1, "the advert checker never chooses a role added by hand, a LinkedIn link included")
        res = check_jobs.run_checks(conn, only_id=out["id"], now=NOW)
        check(res["checked"] == 0 and any("added by hand" in n for n in res["notes"]),
              "a spot check of one is refused before anything is fetched")
        exp = check_jobs.expire_stale(conn, write=True, now=NOW)
        left = conn.execute("SELECT expired_at FROM opportunities WHERE id = ?", (out["id"],)).fetchone()[0]
        check(exp["expired"] == 1 and left is None, "the expiry pass retires the quiet collected role and leaves this one")
        conn.execute("UPDATE opportunities SET tier = NULL, expired_at = NULL")
        conn.commit()
        rows = conn.execute("SELECT * FROM opportunities WHERE tier IS NULL"
                            " AND acknowledged_at IS NULL AND added_at IS NULL").fetchall()
        src = Path(rescore.__file__).read_text(encoding="utf-8")
        check(src.count("AND acknowledged_at IS NULL AND added_at IS NULL") == 3 and len(rows) == 1,
              "rescore's unscored passes never card-score a role added by hand")
        conn.close()


# ------------------------------------------------------------ the server

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


def call(port, method, path, data=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def route_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "r.sqlite"
        rc.get_db(db).close()
        with serving(db) as port:
            own = {"Host": f"127.0.0.1:{port}"}
            w = {**own, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
                 "X-Radar-Request": "1"}

            def post(payload, headers=w, path="/jobs/add"):
                s, b = call(port, "POST", path, json.dumps(payload).encode(), headers)
                return s, json.loads(b or b"{}")

            def count():
                with contextlib.closing(sqlite3.connect(db)) as c:
                    return c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

            s1, _ = post(body(), {**w, "Origin": "https://evil.example"})
            s2, _ = post(body(), {k: v for k, v in w.items() if k != "X-Radar-Request"})
            check(s1 == 403 and s2 == 403 and count() == 0,
                  "/jobs/add refuses another site's page and a request without the write header, storing nothing")
            s, j = post(body(title="", advert=""))
            check(s == 422 and j["errors"] == {"title": "Add a title.", "advert": "Paste the advert."}
                  and count() == 0, "a missing title and advert are 422 with one line each, and nothing is stored")
            s, b = call(port, "POST", "/jobs/add", b"not json", w)
            check(s == 400, "a body that is not JSON is 400")
            s, j = post(body())
            with contextlib.closing(sqlite3.connect(db)) as c:
                c.row_factory = sqlite3.Row
                row = dict(c.execute("SELECT * FROM opportunities WHERE id = ?", (j.get("id"),)).fetchone())
            check(s == 200 and j.get("scored") and j.get("outcome") == "scored" and row["tier"] == "top"
                  and row["score_basis"] == rc.SCORE_BASIS_SPEC,
                  "Add and score stores the role and scores it on the pasted advert, the full-description path")
            check(row["engagement_type"] == "contract" and row["location_class"] == "remote",
                  "the type chosen and Remote stand over what the scorer read")
            check(row["gate_rate"] is None and row["rate_stated"] == 0,
                  "dollars are judged by today's logic, which reads them as not stated")
            first = j["id"]
            s, j = post(body(title="Same link", advert="Same link"))
            check(s == 200 and j == {"ok": True, "existing": first} and count() == 1,
                  "the same link again opens the role Radar holds and spends nothing")

            real = score_item.score_opportunity

            def broken(*_a, **_k):
                raise RuntimeError("no scorer")
            score_item.score_opportunity = broken
            try:
                s, j = post(body(link="", title="Head of Quality, IVD", location="Leeds, UK"))
            finally:
                score_item.score_opportunity = real
            with contextlib.closing(sqlite3.connect(db)) as c:
                tier = c.execute("SELECT tier FROM opportunities WHERE id = ?", (j.get("id"),)).fetchone()
            check(s == 200 and j.get("id") and not j.get("scored") and tier == (None,),
                  "a score that fails leaves the role stored and unscored")
            failed = j["id"]
            # A failure is remembered for a few minutes and the button waits
            # it out (prepare_application's cool-down); past it, it is offered.
            prepare_application.GUARD = prepare_application.Guard()
            s, b = call(port, "GET", f"/prepare/{failed}", headers=own)
            check(s == 200 and "Score the full description" in b.decode() and prepare_page.COST in b.decode(),
                  "and Prepare application offers Score the full description on it, with its cost")
            s, b = call(port, "GET", f"/jobs/{failed}", headers=own)
            check(s == 200 and "Head of Quality, IVD" in b.decode(), "an unscored role opens on Jobs")

            s, b = call(port, "GET", f"/jobs/{first}", headers=own)
            page_checks(b.decode(), first)
            s, b = call(port, "GET", f"/prepare/{first}", headers=own)
            prep = text(b.decode())
            check(s == 200 and "Client not named" in prep and "Added by you 18 Nov" in prep,
                  "Prepare application says it the same way")
            check("Open the advert on Upwork" in prep, "and its advert link names Upwork")

            s, j = post(body(link="", title="Fixed piece of work, medical device", type="fixed-price",
                             rate_min="4000", rate_max="", currency="GBP", period="fixed",
                             source="referral", client="Hartwell"))
            s, b = call(port, "GET", f"/jobs/{j['id']}", headers=own)
            page = b.decode()
            tile = re.search(r'<div class="gate gate-rate ([^"]+)">(.*?)</div>', page, re.S)
            check(tile and "is-cau" in tile.group(1) and "Fixed price" in tile.group(2)
                  and "£4,000 fixed." in tile.group(2),
                  "a fixed price shows the Rate gate amber Fixed price, the price its reason")
            with contextlib.closing(sqlite3.connect(db)) as c:
                t = c.execute("SELECT tier, gate_rate FROM opportunities WHERE id = ?", (j["id"],)).fetchone()
            check(t[1] is None and t[0] == "top", "neither a pass nor a fail: the tier reads it as not stated")
            facts = text(page)
            check("Open the advert" not in facts, "with no link there is no advert link")
            check("Referral" in facts and "Fixed price" in facts, "the facts name the source and the type")


def page_checks(page: str, oid: int) -> None:
    head = re.search(r'<div class="pagehead">(.*?)</div>\s*</div>', page, re.S).group(1)
    check('id="ar-open"' in head and ">Add a role</button>" in head and "btn-secondary" in head,
          "Jobs has a bordered Add a role button in its title row")
    dlg = re.search(r'<dialog class="ar-dlg".*?</dialog>', page, re.S).group(0)
    order = [m for m in re.findall(r'(?:for|id)="ar-(link|title|advert|client|location|type|source|rate)"', dlg)]
    seen = list(dict.fromkeys(order))
    check(seen == ["link", "title", "advert", "client", "location", "type", "source", "rate"],
          f"the dialog's fields are in the brief's order ({seen})")
    check('<h2 id="ar-h">Add a role</h2>' in dlg and 'aria-label="Close"' in dlg,
          "the dialog's heading is Add a role, with a close button")
    check('placeholder="Not named"' in dlg, "Client shows Not named")
    check(re.search(r'name="type" value="contract" checked', dlg)
          and not re.search(r'name="source" value="[^"]+" checked', dlg),
          "Contract is chosen to begin with, no source until one is")
    words = [w for _k, w in hr.SOURCES]
    check(words == ["Upwork", "Referral", "Recruiter", "Company site", "Other"]
          and all(f"<span>{w}</span>" in dlg for w in words), "the five sources")
    check(all(f"<span>{w}</span>" in dlg for w in ("£", "$", "€", "an hour", "a day", "a year", "fixed"))
          and '<span class="ar-to">to</span>' in dlg, "the rate: an amount, to, an upper amount, a currency and a period")
    foot = re.search(r'<div class="ar-foot">(.*?)</div>', dlg, re.S).group(1)
    check(f'<span class="ar-cost">{prepare_page.COST}</span>' in foot
          and foot.index("ar-cost") < foot.index("Cancel") < foot.index("Add and score"),
          "the footer: the scoring cost at the left as Prepare words it, then Cancel and Add and score")
    css = add_role_page.CSS
    check(".ar-go{" in css and "background:#a79cf0" in css and "color:#15122e" in css,
          "Add and score is the solid purple button")
    check("border:1.5px solid #a79cf0" in css and "rgb(167 156 240 / .16)" in css and "height:36px" in css,
          "a chosen choice has a 1.5px purple border on a 16% purple ground; choices are 36px high")
    check("font:600 26px/32px" in css, "the heading is 26px/600")
    check(".ar-foot{" in css and "border-top:1px solid var(--border-subtle)" in css, "the footer sits above a hairline")
    sizes = [int(x) for x in re.findall(r"font(?:-size)?:[^;}]*?(\d+)px", css)]
    check(sizes and min(sizes) >= 14, f"nothing in the dialog under 14px ({sorted(set(sizes))})")
    check(re.search(r"max-width:767\.98px\)\{[^}]*\.ar-dlg\{width:calc\(100vw - 32px\)", css)
          and ".ar-pair{grid-template-columns:minmax(0,1fr)}" in css,
          "on a phone the dialog fills the width with 16px margins and the pair stacks")
    for bad, label in (("\u00b7", "middle dot"), ("✓", "tick"), ("<kbd", "key hint")):
        if bad in dlg or bad in add_role_page.SCRIPT:
            check(False, f"no {label} in the dialog")
            break
    else:
        check(True, "no middle dot, tick or key hint in the dialog")
    pane = re.search(r'<section class="role role-detail".*?</section>\s*</main>', page, re.S).group(0)
    facts = text(re.search(r'<p class="dh-facts">(.*?)</p>', pane, re.S).group(1))
    check("Client not named" in facts and "Remote" in facts and "Contract" in facts and "Upwork" in facts
          and "Added by you 18 Nov" in facts, f"the facts line: client, place, type, source, Added by you ({facts.strip()})")
    raw = re.search(r'<p class="dh-facts">(.*?)</p>', pane, re.S).group(1)
    check(raw.startswith('<span class="dv') and "\u00b7" not in facts, "set apart by the shared divider")
    check("Open the advert on Upwork" in pane, "the advert link reads Open the advert on Upwork")
    check("Pasted by you on 18 Nov" in pane and "Read from" not in pane, "the description strip says Pasted by you")
    check('id="detail-company"' not in pane, "no company record link for a client not on file")
    script = add_role_page.SCRIPT
    check("'/jobs/add'" in script and "window.location.href = '/jobs/' + id" in script
          and "j.existing || j.id" in script, "after adding the browser goes to the role, or to the one held")
    check("MISSING.title" in script and json.loads(re.search(r'data-missing="([^"]+)"', page).group(1)
                                                   .replace("&quot;", '"')) == hr.MISSING,
          "the page says a missing field in the server's words")


def main() -> int:
    ap = argparse.ArgumentParser(description="Add a role checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    for section in (storage_checks, field_checks, place_checks, add_checks, skip_checks, route_checks):
        try:
            section()
        except Exception as err:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(False, f"a section stopped on {type(err).__name__}: {err}")
    print()
    if failures:
        print(f"FAIL. {len(failures)} check(s) failed.")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS. Every add a role check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
