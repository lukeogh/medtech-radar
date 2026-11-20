#!/usr/bin/env python
"""Checks for advert freshness: the shared LinkedIn read limits
(scripts/linkedin_reads.py) and the advert check made when a role is opened
(scripts/advert_check.py, POST /jobs/advert-check).

No model calls and no network: RADAR_MOCK is forced on, every database is a
throwaway under a temporary folder, the page checker's fetch is replaced by a
stub that serves saved pages, and the limits run on a clock moved by hand.

The saved pages (test/fixtures/linkedin): a live advert (live.html), a closed
one (pages/4487779866.html, a real guest page that says No longer accepting
applications, and closed.html), a gone one (gone.html, served at LinkedIn's
expired redirect), a login wall (authwall.html, served at /authwall) and a
429, which is a status with no page.

1. The limits: twenty seconds between any two reads, nothing for ten minutes
   after a 429, one check per role a day, forty reads on open a day.
2. A role opened: live stamps the read and says Live on with its date; closed
   and gone close the role as the page checker does; a login wall, a page
   that does not match and a 429 store nothing; which roles are never read.
3. The route: through the write guard, the page drawing first and the read
   after it, the header's words, and Undo last bringing a closed role back.
4. No model call anywhere on the way.
5. How fresh the check is: every role with a LinkedIn advert says its last
   check in the header's facts line, on Jobs and Prepare application; the
   rows say nothing of it; Details keeps its State line.
"""

from __future__ import annotations

import argparse
import ast
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

os.environ["RADAR_MOCK"] = "1"

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(TEST_DIR))

import advert_check  # noqa: E402
import check_jobs  # noqa: E402
import jobs_page  # noqa: E402
import linkedin_reads as lr  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
FIX = REPO_ROOT / "test" / "fixtures" / "linkedin"
TITLE = "Principal Medical Device Engineer"
STUDY_ID = "4482760631"


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


# ------------------------------------------------------------ the clock

CLOCK = [1_800_000_000.0]       # 15 Jan 2027, mid-morning UTC
lr.clock = lambda: CLOCK[0]
lr.sleep = lambda seconds: CLOCK.__setitem__(0, CLOCK[0] + max(0.0, seconds))


def tick(seconds: float) -> None:
    CLOCK[0] += seconds


# ------------------------------------------------------------ the pages

def page(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def view(jid: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{jid}/"


def served(kind: str, jid: str):
    """(status, final_url, body) for one kind of answer about advert jid."""
    if kind == "live":
        return 200, view(jid), page("live.html").replace(STUDY_ID, jid)
    if kind == "closed":
        return 200, view(jid), page("pages/4487779866.html")
    if kind == "closed-synthetic":
        return 200, view(jid), page("closed.html")
    if kind == "gone":
        return (200, "https://www.linkedin.com/jobs/search/?position=1"
                "&trk=expired_jd_redirect", page("gone.html"))
    if kind == "login":
        return (200, "https://www.linkedin.com/authwall?trk=gf&sessionRedirect="
                + view(jid), page("authwall.html"))
    if kind == "429":
        return 429, view(jid), ""
    if kind == "renamed":
        return 200, view(jid), page("renamed.html")
    if kind == "network":
        return 0, view(jid), ""
    raise ValueError(kind)


ANSWERS: dict = {}
FETCHES: list = []


def fake_fetch(url):
    FETCHES.append(url)
    jid = check_jobs.job_view_id(url)
    return served(ANSWERS.get(jid, "live"), jid)


check_jobs.fetch_job_page = fake_fetch


# ------------------------------------------------------------ a database

def iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh(tmp: str, name: str = "f.sqlite"):
    db = Path(tmp) / name
    return db, rc.get_db(db)


def plant(conn, jid: str, tier: str = "question", **cols) -> int:
    seen = cols.pop("seen", rc.now_iso())
    row = {"url_hash": f"h{jid}", "first_seen": seen, "last_seen": seen,
           "title": cols.pop("title", TITLE), "company": cols.pop("company", "Veritas Medical"),
           "tier": tier, "source": "linkedin-alert",
           "source_url": cols.pop("source_url", f"https://www.linkedin.com/comm/jobs/view/{jid}/")}
    row.update(cols)
    cur = conn.execute(f"INSERT INTO opportunities ({', '.join(row)}) VALUES"
                       f" ({', '.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()
    return cur.lastrowid


def row_of(conn, oid: int) -> dict:
    return dict(conn.execute("SELECT * FROM opportunities WHERE id = ?", (oid,)).fetchone())


def reads(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM linkedin_reads ORDER BY id")]


# ------------------------------------------------------------ 1. the limits

def limiter_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _db, conn = fresh(tmp)
        first = lr.reserve(conn, "open", 1)
        check(first["ok"] and reads(conn)[0]["outcome"] == "pending",
              "a read is reserved before it goes out, and logged as pending")
        second = lr.reserve(conn, "scheduled", 2)
        check(not second["ok"] and second["why"] == "gap"
              and abs(second["wait"] - 20) < 0.01,
              "another read a moment later, whatever starts it, waits for the twenty seconds")
        tick(19.5)
        check(lr.reserve(conn, "prepare", 3)["why"] == "gap",
              "nineteen and a half seconds on, still not")
        tick(0.5)
        check(lr.reserve(conn, "scheduled", 2)["ok"],
              "twenty seconds on, the next read may go")
        lr.finish(conn, first["id"], "live")
        tick(30)
        again = lr.reserve(conn, "open", 1)
        check(not again["ok"] and again["why"] == "role",
              "the same role is not read again inside the day")
        tick(24 * 3600)
        check(lr.reserve(conn, "open", 1)["ok"], "a day on, it may be")

        tick(30)
        hit = lr.reserve(conn, "open", 4)
        lr.finish(conn, hit["id"], "429")
        for kind in lr.KINDS:
            tick(25)
            got = lr.reserve(conn, kind, 50)
            check(not got["ok"] and got["why"] == "backoff",
                  f"after a 429, no {kind} read at all while the backoff runs")
        tick(600)
        check(lr.reserve(conn, "open", 4)["ok"],
              "ten minutes after the 429 reads go again, and the role the 429 hit "
              "is not held for the day, nothing was read")

        # forty reads on open in a UTC day
        tick(24 * 3600)
        day = datetime.fromtimestamp(CLOCK[0], timezone.utc).date()
        made = 0
        for n in range(100, 140):
            tick(21)
            made += bool(lr.reserve(conn, "open", n)["ok"])
        tick(21)
        over = lr.reserve(conn, "open", 999)
        same_day = datetime.fromtimestamp(CLOCK[0], timezone.utc).date() == day
        check(made == 40 and not over["ok"] and over["why"] == "daily" and same_day,
              f"forty reads on open in a day, and the forty-first is refused ({made})")
        check(lr.reserve(conn, "prepare", None)["ok"],
              "the daily cap on open reads does not hold back Prepare application")
        conn.close()


# ------------------------------------------------------------ 2. a role opened

def open_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _db, conn = fresh(tmp)

        def opened(kind: str, jid: str, tier: str = "question", **cols):
            tick(3600)
            ANSWERS[jid] = kind
            oid = plant(conn, jid, tier, **cols)
            before = row_of(conn, oid)
            del FETCHES[:]
            out = advert_check.check_on_open(conn, oid)
            return oid, before, out

        oid, _b, out = opened("live", "5000000001")
        row = row_of(conn, oid)
        check(out["state"] == "live" and out["day"] == advert_check.day(row["last_checked_at"])
              and row["last_check_result"] == "live" and row["last_live_check_at"] == row["last_checked_at"]
              and row["closed_at"] is None and len(FETCHES) == 1,
              "a live advert: one read, stamped live with its date, and the answer says Live on that date")
        check(FETCHES[0] == view("5000000001"),
              "the read is of the advert's own linkedin.com/jobs/view page and nothing else")

        for kind, jid, why in (("closed", "5000000002", "the real guest page saying no longer accepting applications"),
                               ("closed-synthetic", "5000000003", "the closed-job shape"),
                               ("gone", "5000000004", "the expired redirect")):
            oid, _b, out = opened(kind, jid)
            row = row_of(conn, oid)
            want = "gone" if kind == "gone" else "closed"
            check(out["state"] == "closed" and row["closed_at"] and row["closed_source"] == "page_check"
                  and row["last_check_result"] == want and row["last_checked_at"] == row["closed_at"],
                  f"{why}: the role is closed exactly as the page checker closes one, "
                  "closed_source page_check")
            check(rc.life_state(row) == "closed", f"and it reads closed ({kind})")

        for kind, jid, why in (("login", "5000000005", "a login wall"),
                               ("renamed", "5000000006", "a page that does not match the advert"),
                               ("429", "5000000007", "a 429"),
                               ("network", "5000000008", "no answer at all")):
            if kind == "network":
                tick(601)    # past the 429's backoff
            oid, before, out = opened(kind, jid)
            check(out["state"] == "failed" and row_of(conn, oid) == before and len(FETCHES) == 1,
                  f"{why}: one read, nothing stored, and the answer says it could not check")
            if kind == "429":
                tick(30)
                other = plant(conn, "5000000099")
                del FETCHES[:]
                held = advert_check.check_on_open(conn, other)
                check(held["state"] == "limited" and held["why"] == "backoff" and not FETCHES,
                      "and after the 429 the next role opened is not read while the backoff runs")
        logged = [r["outcome"] for r in reads(conn)]
        check(logged[-4:] == ["unknown", "unknown", "429", "network"],
              f"each read is logged with how it ended ({logged[-4:]})")

        for n, tier in enumerate(("reading", "filtered", "top")):
            oid, _b, out = opened("live", f"510000000{n}", tier)
            check(out["state"] == "live", f"a role in {tier} is checked when opened")

        # never read
        oid, before, out = opened("live", "5200000001", added_at=rc.now_iso(),
                                  source="upwork")
        check(out["state"] == "none" and not FETCHES and row_of(conn, oid) == before,
              "a role added by hand is never read, even with a LinkedIn link")
        oid, before, out = opened("live", "5200000002",
                                  source_url="https://www.reed.co.uk/jobs/director/55555")
        check(out["state"] == "none" and not FETCHES, "a role with no LinkedIn advert is never read")
        for n, col in enumerate(("closed_at", "dismissed_at", "expired_at", "acknowledged_at",
                                 "applied_at")):
            oid, before, out = opened("live", f"540000000{n}", **{col: rc.now_iso()})
            check(out["state"] == "none" and not FETCHES,
                  f"a role with {col} set is not in an open group and is not read")
        oid, before, out = opened("live", "5200000003",
                                  last_checked_at=iso(lr.clock() - 3 * 3600),
                                  last_check_result="unknown")
        check(out["state"] == "limited" and not FETCHES,
              "a role whose page was read in the last day is not read again on open")
        check(advert_check.due(row_of(conn, oid), False,
                               iso(lr.clock() + 22 * 3600)),
              "and is due again once that read is a day old")

        # the gap between two reads is a limit, not a wait, when a role opens
        tick(3600)
        a = plant(conn, "5300000001")
        b = plant(conn, "5300000002")
        ANSWERS["5300000001"] = ANSWERS["5300000002"] = "live"
        del FETCHES[:]
        first = advert_check.check_on_open(conn, a)
        second = advert_check.check_on_open(conn, b)
        check(first["state"] == "live" and second["state"] == "limited"
              and second["why"] == "gap" and len(FETCHES) == 1,
              "two roles opened at once: the second is not read and does not wait")
        check(row_of(conn, b)["last_checked_at"] is None,
              "and the second stays due for when it is opened again")
        conn.close()


# ------------------------------------------------------------ 3. the route

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


def header_facts(html: str) -> str:
    found = re.search(r'<p class="(?:dh|ph)-facts">(.*?)</p>', html, re.S)
    return found.group(1) if found else ""


def route_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db, conn = fresh(tmp)
        tick(2 * 24 * 3600)
        due = plant(conn, "6000000001", "top")
        shut = plant(conn, "6000000002", "question")
        fresh_row = plant(conn, "6000000003", "question",
                          last_checked_at=rc.now_iso(), last_check_result="live",
                          last_live_check_at=rc.now_iso())
        ANSWERS.update({"6000000001": "live", "6000000002": "closed"})
        with serving(db) as port:
            own = {"Host": f"127.0.0.1:{port}"}
            w = {**own, "Origin": f"http://127.0.0.1:{port}",
                 "Content-Type": "application/json", "X-Radar-Request": "1"}

            def post(path, payload, headers=w):
                s, b = call(port, "POST", path, json.dumps(payload).encode(), headers)
                return s, json.loads(b or b"{}")

            del FETCHES[:]
            s, b = call(port, "GET", f"/jobs/{due}", headers=own)
            html = b.decode()
            facts = header_facts(html)
            check(s == 200 and not FETCHES,
                  "opening a role draws the page and reads nothing: the read comes after")
            check(f'data-advert-check="{due}"' in facts and 'data-due="1"' in facts
                  and "Checking the advert" in facts and "adv-muted" in facts,
                  "the header says Checking the advert, muted, in the facts line")
            check(advert_check.SCRIPT.strip() and "/jobs/advert-check" in html
                  and "radarAdvertCheck" in html,
                  "and the page carries the script that makes the read")
            s, b = call(port, "GET", f"/jobs/{fresh_row}", headers=own)
            facts = header_facts(b.decode())
            check("data-advert-check" not in facts
                  and f'<span class="adv-check">Live on {advert_check.day(rc.now_iso())}</span>' in facts,
                  "a role read in the last day has no check to make, and says Live on with that day")

            s1, _ = post("/jobs/advert-check", {"id": due}, {**w, "Origin": "https://evil.example"})
            s2, _ = post("/jobs/advert-check", {"id": due},
                         {k: v for k, v in w.items() if k != "X-Radar-Request"})
            check(s1 == 403 and s2 == 403 and not FETCHES,
                  "the route goes through the write guard: another site, or no write header, is 403 and reads nothing")
            s, j = post("/jobs/advert-check", {"id": "7"})
            check(s == 400, "an id that is not an integer is 400")
            s, j = post("/jobs/advert-check", {"id": due})
            check(s == 200 and j["state"] == "live" and j["day"] and len(FETCHES) == 1,
                  "the read: 200, live, with the date")
            tick(25)
            s, j = post("/jobs/advert-check", {"id": shut})
            check(s == 200 and j["state"] == "closed" and row_of(conn, shut)["closed_source"] == "page_check",
                  "a closed advert: the role is closed by page_check")
            s, b = call(port, "GET", "/jobs?part=queue", headers={**own, "Accept": "application/json"})
            queue = json.loads(b)["html"] if s == 200 else ""
            check(f'data-id="{shut}"' not in queue and f'data-id="{due}"' in queue,
                  "and it is gone from the list the next time the list loads")
            s, j = post("/unclose", {"id": shut})
            s, b = call(port, "GET", "/jobs?part=queue", headers={**own, "Accept": "application/json"})
            check(s == 200 and f'data-id="{shut}"' in json.loads(b)["html"]
                  and row_of(conn, shut)["closed_at"] is None,
                  "Undo last's unclose brings it back")
            s, b = call(port, "GET", f"/prepare/{due}", headers=own)
            check(s == 200 and "radarAdvertCheck" in b.decode(),
                  "Prepare application carries the same script")
        conn.close()


def script_checks() -> None:
    js = advert_check.SCRIPT
    check("'/jobs/advert-check'" in js and "method: 'POST'" in js,
          "the page script makes one POST, to the one new route")
    check("'Checking the advert'" in js and "'Live on ' + j.day" in js
          and "'Closed on LinkedIn'" in js and "\"Couldn't check\"" in js,
          "it says Checking the advert, Live on and the date, Closed on LinkedIn, or Couldn't check")
    check("kind: 'close'" in js and "radar-jobs-undo" in js and "radar-undo-changed" in js
          and "document.addEventListener('radar-undo-changed'" in jobs_page.JOBS_SCRIPT,
          "a role it closed goes on Jobs' undo stack, so Undo last restores it")
    check("state === 'limited'" in js and "el.textContent = last" in js,
          "a read a limit held back leaves the last known date and nothing else")
    check("window.radarAdvertCheck(role)" in jobs_page.JOBS_SCRIPT,
          "Jobs asks for the read after it draws a role in the pane")
    css = advert_check.CSS
    sizes = [int(n) for n in re.findall(r"font-size:(\d+)px", css)]
    check(all(n >= 13 for n in sizes) and "var(--negative)" in css and "var(--text-3)" in css,
          f"the chip is red, the muted words are muted, nothing under 13px ({sizes})")
    for bad, label in (("·", "middle dot"), ("✓", "tick"), ("✔", "tick"), ("<kbd", "key hint")):
        check(bad not in js and bad not in css, f"no {label}")


# ------------------------------------------------------------ 4. no model

def model_checks() -> None:
    src = (REPO_ROOT / "scripts" / "advert_check.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    check(not ({"score_item", "anthropic", "claude_call", "prepare_application"} & (names | attrs | imported)),
          "the advert check names no scorer and no model client")
    called = []
    real = rc.claude_call
    rc.claude_call = lambda *a, **k: called.append(1) or (_ for _ in ()).throw(RuntimeError("no model"))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _db, conn = fresh(tmp)
            tick(3600)
            oid = plant(conn, "7000000001", "question")
            ANSWERS["7000000001"] = "live"
            out = advert_check.check_on_open(conn, oid)
            conn.close()
    finally:
        rc.claude_call = real
    check(out["state"] == "live" and not called, "and a read on open makes no model call")


# ------------------------------------------------------------ 5. how fresh

def fresh_header_checks() -> None:
    now = rc.now_iso()
    today = advert_check.day(now)
    with tempfile.TemporaryDirectory() as tmp:
        db, conn = fresh(tmp)
        live = plant(conn, "8000000001", "top", last_checked_at="2026-11-17T09:00:00Z",
                     last_check_result="live", last_live_check_at="2026-11-17T09:00:00Z")
        unclear = plant(conn, "8000000002", "question", last_checked_at="2026-11-19T09:00:00Z",
                        last_check_result="unknown", last_live_check_at="2026-11-17T09:00:00Z")
        older = plant(conn, "8000000003", "question", last_checked_at="2026-11-16T09:00:00Z",
                      last_check_result="live")
        never_acked = plant(conn, "8000000004", "question", acknowledged_at=now)
        shut = plant(conn, "8000000005", "question", closed_at=now, closed_source="page_check",
                     last_checked_at=now, last_check_result="closed")
        hand = plant(conn, "8000000006", "question", added_at=now, source="upwork")
        offsite = plant(conn, "8000000007", "question",
                        source_url="https://www.reed.co.uk/jobs/director/1")
        due = plant(conn, "8000000008", "question")
        rows = {k: row_of(conn, v) for k, v in (("live", live), ("unclear", unclear), ("older", older),
                                               ("acked", never_acked), ("shut", shut), ("hand", hand),
                                               ("offsite", offsite), ("due", due))}
        # Each drawn an hour after its last read, when it is not due another.
        def hour_on(r):
            when = r.get("last_checked_at")
            if not when:
                return now
            at = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=1)
            return at.strftime("%Y-%m-%dT%H:%M:%SZ")
        bit = {k: advert_check.header_bit(r, False, hour_on(r)) for k, r in rows.items()}
        check(bit["live"] == '<span class="adv-check">Live on 17 Nov</span>',
              "a role last read live on 17 Nov says Live on 17 Nov")
        check(bit["unclear"] == '<span class="adv-check">Live on 17 Nov</span>',
              "a later unclear read does not lose the date it was last seen live")
        check(bit["older"] == '<span class="adv-check">Live on 16 Nov</span>',
              "a live read from before the column existed still gives its date")
        check(bit["acked"] == '<span class="adv-check">Not checked yet</span>',
              "a role never read, and not due a read, says Not checked yet")
        check('<span class="adv-closed">Closed on LinkedIn</span>' in bit["shut"],
              "a role the check closed says Closed on LinkedIn in the red chip")
        check(bit["hand"] == "" and bit["offsite"] == "",
              "a role added by hand, or with no LinkedIn advert, has no check line")
        check('data-last="Not checked yet"' in bit["due"] and "Checking the advert" in bit["due"],
              "a role due a read says Checking the advert and keeps Not checked yet for after")
        two_hours = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        shown = plant(conn, "8000000009", "top", last_checked_at=two_hours,
                      last_check_result="live", last_live_check_at=two_hours)
        words = f"Live on {advert_check.day(two_hours)}"
        with serving(db) as port:
            own = {"Host": f"127.0.0.1:{port}"}
            del FETCHES[:]
            s, b = call(port, "GET", f"/jobs/{shown}", headers=own)
            page = b.decode()
            facts = header_facts(page)
            check(s == 200 and facts.endswith('<span class="dv-i"><span class="visually-hidden">, </span>'
                                              f'<span class="adv-check">{words}</span></span></span></span>'),
                  "on Jobs it is the last fact in the header's facts line, after the shared divider")
            queue = re.search(r'<section class="queue" id="queue"[^>]*>(.*?)</section>', page, re.S)
            check(queue and 'data-id="' in queue.group(1)
                  and not any(w in queue.group(1) for w in ("adv-check", "Live on", "Not checked yet",
                                                            "Checking the advert")),
                  "nothing is added to the rows in the list")
            check(f"<dt>State</dt><dd>Live, seen {jobs_page._day(now)}</dd>" in page,
                  "Details keeps its State line as it is")
            s, b = call(port, "GET", f"/prepare/{shown}", headers=own)
            check(s == 200 and words in header_facts(b.decode()),
                  "Prepare application says it the same way")
            s, b = call(port, "GET", f"/prepare/{offsite}", headers=own)
            check(s == 200 and "adv-check" not in header_facts(b.decode()),
                  "and draws no check line for a role with no LinkedIn advert")
            check(not FETCHES, "and drawing any of it reads nothing")
        conn.close()
    del today


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", help="ignored: the suite passes it to every runner")
    parser.parse_args(argv)
    for section in (limiter_checks, open_checks, route_checks, script_checks, model_checks,
                    fresh_header_checks):
        try:
            section()
        except Exception as err:  # noqa: BLE001  one section's failure is reported, the rest run
            import traceback
            traceback.print_exc()
            check(False, f"{section.__name__} stopped on {type(err).__name__}: {err}")
    print()
    if failures:
        print(f"FAIL  {len(failures)} freshness check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("PASS  advert freshness")
    return 0


if __name__ == "__main__":
    sys.exit(main())
