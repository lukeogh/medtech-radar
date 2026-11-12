#!/usr/bin/env python
"""Acceptance checks for roles applied for.

No model calls and no network. RADAR_MOCK is forced on, every page the checker
fetches is a stub, and every database is a throwaway under a temporary
directory, so a mistake here cannot spend a token or touch a real row. The
owner's own preferences file is never read.

What it checks:

- the two columns arrive on a copy of a database in the old shape, nullable,
  at the end of the table, with every existing row untouched, once however many
  processes open it at the same moment, and the status check still refuses a
  new word, so the marker is not a status,
- a role is applied when any row in its repost chain is, a repost that arrives
  later is applied the moment it is stored, a row with no role key stands
  alone, and the SQL condition and the Python helpers agree on every row,
- POST /apply and /unapply stamp and clear the one row and the chain follows,
  refuse what they should, take an optional saved CV reference, and a role
  marked applied cannot be acknowledged, closed or dismissed by hand,
- two pages open at once cannot leave a role with two stamps: four
  simultaneous presses on one role, and the gap between a route's check and
  its write held open by hand,
- the guard still refuses a forged apply and a forged undo, in enforce mode,
  before the route runs and with the database left exactly as it was,
- the button posts through the page's fetch wrapper and a refusal shows the
  guard's reason, run in node when node is installed,
- a repost of an applied role, stored through the inbox, stays out of the open
  tiers, the digest and the Home counts, and the Jobs page lists the role once
  in a folded Applied section with everything the brief asks for,
- the 45-day expiry (in the several statements a big pass makes), the page
  checker (its selection, its --id spot check, its close and the race where a
  role is applied while a page is being fetched, through the advert fetched
  or through another advert of the role), regate_rates.py, the three
  re-score selections, the pay backfill, the digest's five selections and
  touch.py mark all leave an applied role alone, and do their work on an
  identical role that is not applied. Where a writer checks twice, once in
  Python and once in its own statement, each check is tested with the other
  switched off, and where it takes the write lock first, the lock is seen
  held from the look to the write and not across a model call,
- the Home page, with a role applied for: Ageing, the unscored and aged
  attention items, the standing line, the acknowledged list and the prospect
  trend leave it out, the flow chart and the board freshness lights, which
  count mail, do not,
- no other page changes: the Jobs page with a role applied reads the same as
  the Jobs page without that role, apart from the Applied section,
- a scan of every script finds each statement that picks or changes open rows
  and fails on one that neither leaves applied roles out nor is named, with a
  reason, in the short list of exceptions,
- the read-only queries handed to the box for the deploy checks run here, on
  seeded data, and each proof query is shown to catch the harm it exists for.

Exits non zero on any failure with readable FAIL lines. A mistake in the runner
itself also prints a traceback, so it can be found.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import http.client
import io
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Forced on before anything else loads. Nothing in this file may call a model.
os.environ["RADAR_MOCK"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "test"):
    if str(REPO_ROOT / sub) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / sub))

import backfill_pay
import build_dashboard
import build_digest
import check_jobs
import process_email
import radar_common
import regate_rates
import rescore
import score_item
import serve_dashboard
import touch

# Hermetic. The live preferences file holds the owner's own floors and
# thresholds, and a check must neither read it nor fail because it was edited.
TEMPLATE = REPO_ROOT / "config" / "preferences.template.md"
_real_load_config = radar_common.load_config


def _template_config() -> dict:
    return {**_real_load_config(), "prefs_file": str(TEMPLATE)}


radar_common.load_config = _template_config
CFG = radar_common.load_config()
FLOORS = radar_common.read_rate_floors(CFG)

HEADER = build_dashboard.WRITE_HEADER
PROXY = "medtech-radar.keogh.cloud"
EVIL = "evil.example"
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# The instant the functions that take a clock are given. Anything that reads
# the real clock is seeded from the real one instead, so no date here can age.
NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)

QD = ("Quality Director", "Veltrix Diagnostics")       # the role applied for
RD = ("Regulatory Director", "Orbase Diagnostics")     # an identical role, not applied

failures: list[str] = []
skipped: list[str] = []
_seq = iter(range(1, 100_000))


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def note(text: str) -> None:
    """A check that could not run here. Printed, counted, never a failure."""
    print("NOTE  " + text)
    skipped.append(text)


@contextlib.contextmanager
def captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


@contextlib.contextmanager
def captured_stdout():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


@contextlib.contextmanager
def blind_to_applied():
    """The Python re-check switched off, so that only the SQL condition on a
    statement is left to stop it. Several writers check twice, once in
    Python for a clear message or to save a model call, and once in the
    statement itself, which is the one that holds under a race."""
    real = radar_common.role_is_applied
    radar_common.role_is_applied = lambda *_a, **_k: False
    try:
        yield
    finally:
        radar_common.role_is_applied = real


# ------------------------------------------------------------------ helpers

@contextlib.contextmanager
def clock_at(moment: datetime):
    """The dashboard's own clock held at one instant, for pages that label
    ages. The pages read the time through radar_common.now_iso."""
    real = radar_common.now_iso
    radar_common.now_iso = lambda: iso(moment)
    try:
        yield
    finally:
        radar_common.now_iso = real

def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(n: float, base: datetime | None = None) -> str:
    return iso((base or NOW) - timedelta(days=n))


def real_days_ago(n: float) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(days=n))


def seed(conn, **cols) -> int:
    """Insert one opportunity row. The role key follows the title and company."""
    row = {"url_hash": f"applied-{next(_seq)}", "first_seen": days_ago(3),
           "last_seen": days_ago(3), "status": "new", "thread_type": "inbound",
           **cols}
    if "role_key" not in cols:
        row["role_key"] = radar_common.role_key(row.get("title"),
                                                row.get("company"))
    conn.execute(
        f"INSERT INTO opportunities ({', '.join(row)}) VALUES"
        f" ({', '.join('?' for _ in row)})", tuple(row.values()))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def role(conn, who, **cols) -> int:
    """A scored, open Top row for a (title, company) pair."""
    base = {"title": who[0], "company": who[1], "tier": "top", "cv_match": 90,
            "gate_sector": 1, "gate_cv": 1, "gate_location": 1, "gate_rate": 1,
            "failed_gates": "[]", "one_line_why": "Judged against the four gates.",
            "location": "Ghent, Belgium", "salary_rate": "£950 per day",
            "source": "linkedin-alert",
            "source_url": "https://www.linkedin.com/jobs/view/"
                          f"{9000000 + next(_seq)}/"}
    base.update(cols)
    return seed(conn, **base)


def row_of(conn, oid) -> dict:
    return dict(conn.execute("SELECT * FROM opportunities WHERE id = ?",
                             (oid,)).fetchone())


def snapshot(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT * FROM opportunities ORDER BY id")]


def fresh(tmp, name="t.sqlite"):
    return radar_common.get_db(Path(tmp) / name)


def writer_is_blocked(db) -> bool:
    """True while some other connection holds the write lock on db.

    Asked from a second connection with a short patience, the way a click on
    the dashboard would meet it: a writer that has taken the lock is a writer
    nothing else can slip a stamp in beside."""
    other = sqlite3.connect(db, timeout=0.2)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        return False
    except sqlite3.OperationalError as err:
        return "locked" in str(err)
    finally:
        other.close()


def reader(path):
    """A second connection, the way a page render or a box check reads."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def stamp_applied(conn, oid, when=None) -> None:
    conn.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                 (when or days_ago(2), oid))
    conn.commit()


@contextlib.contextmanager
def pointed_at(db):
    """The dashboard's own functions, aimed at a throwaway database."""
    saved = serve_dashboard.ARGS
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False,
                                              host="127.0.0.1", port=0)
    try:
        yield
    finally:
        serve_dashboard.ARGS = saved


@contextlib.contextmanager
def serving(db, guard=None):
    """The real Handler on a loopback port, writing to db, guard installed.

    Nothing is stubbed. A request that gets past the guard runs the real
    route against the real file. The server's access log is silenced so
    what reaches stderr is the guard's lines and nothing else.
    """
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD,
             serve_dashboard.Handler.log_message)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False,
                                              host="127.0.0.1", port=0)
    serve_dashboard.GUARD = guard or serve_dashboard.WriteGuard(
        "enforce", (*serve_dashboard.LOOPBACK_NAMES, PROXY))
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    server = serve_dashboard.RadarServer(("127.0.0.1", 0),
                                         serve_dashboard.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD,
         serve_dashboard.Handler.log_message) = saved


def page_headers(port) -> dict:
    """What the page's own script sends: its Host, its Origin, the header."""
    own = f"127.0.0.1:{port}"
    return {"Host": own, "Origin": f"http://{own}", HEADER: "1",
            "Content-Type": "application/json"}


def call(port, method, path, body=b"", headers=None, timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body or None, headers=headers or {})
        resp = conn.getresponse()
        return (resp.status, {k.lower(): v for k, v in resp.getheaders()},
                resp.read())
    except (OSError, http.client.HTTPException) as err:
        note(f"{method} {path[:40]} failed with {type(err).__name__}")
        return 0, {}, b""
    finally:
        conn.close()


def post(port, path, payload, headers=None, timeout=10):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    status, got, data = call(port, "POST", path, body,
                             headers or page_headers(port), timeout)
    try:
        parsed = json.loads(data or b"{}")
    except ValueError:
        parsed = {}
    return status, parsed, got


def jobs_html(db) -> str:
    with pointed_at(db):
        return serve_dashboard.render("jobs").decode("utf-8")


def home_html(db) -> str:
    with pointed_at(db):
        return serve_dashboard.render("home").decode("utf-8")


def split_applied(html: str) -> tuple[str, str]:
    """(the page before the Applied section, the Applied section)."""
    at = html.find("<h2>Applied</h2>")
    if at < 0:
        return html, ""
    start = html.rfind("<details", 0, at)
    end = html.find("</details>", at) + len("</details>")
    return html[:start] + html[end:], html[start:end]


def row_titles(html: str) -> list[str]:
    return re.findall(r'<span class="row-title">(.*?)</span>', html)


# ---------------------------------------------------------------- migration

def migration_checks() -> None:
    # The deployed shape is built by dropping the two columns from a current
    # database, which SQLite only learned to do in 3.35. Where it cannot, say
    # so and move on, so the sections that follow still run and report.
    if sqlite3.sqlite_version_info < (3, 35, 0):
        note("this SQLite is older than 3.35 and cannot drop a column, so the "
             "old-shape fixture was not built and the migration was not "
             "replayed here")
        return
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "deployed.sqlite"
        conn = radar_common.get_db(base)
        role(conn, QD)
        role(conn, QD, acknowledged_at=days_ago(3))
        role(conn, RD, tier="question", closed_at=days_ago(9),
             closed_source="manual")
        role(conn, ("Clinical Lead", "Novem"), tier="reading",
             dismissed_at=days_ago(4))
        role(conn, ("Safety Officer", "Kordis"), tier="filtered",
             expired_at=days_ago(2))
        seed(conn, title=None, company="Anon", role_key=None)
        conn.close()
        # One open of its own, so the score record's label for rows scored
        # before it existed is already on them and only what this change
        # adds is left for the first open of the copy to do.
        conn = radar_common.get_db(base)
        # The shape a deployed database is in: no such columns.
        for column in ("applied_at", "applied_cv"):
            try:
                conn.execute(f"ALTER TABLE opportunities DROP COLUMN {column}")
            except sqlite3.OperationalError as err:
                check(False, f"the fixture could not drop {column}, so the "
                             f"migration list does not add it ({err})")
        conn.commit()
        conn.close()

        def columns(path):
            raw = sqlite3.connect(path)
            try:
                return [dict(zip(("cid", "name", "type", "notnull", "dflt",
                                  "pk"), r))
                        for r in raw.execute("PRAGMA table_info(opportunities)")]
            finally:
                raw.close()

        def fingerprint(path, names):
            raw = sqlite3.connect(path)
            try:
                return raw.execute(f"SELECT {', '.join(names)} FROM"
                                   " opportunities ORDER BY id").fetchall()
            finally:
                raw.close()

        old = columns(base)
        names = [c["name"] for c in old]
        check("applied_at" not in names and "applied_cv" not in names
              and len(names) > 40,
              "the fixture has the shape of a deployed database, the full "
              "table without the two new columns")
        shipped = Path(tmp) / "copy.sqlite"
        shutil.copyfile(base, shipped)
        before = fingerprint(shipped, names)
        radar_common.get_db(shipped).close()
        new = {c["name"]: c for c in columns(shipped)}
        check({"applied_at", "applied_cv"} <= set(new),
              "the first open of the copy adds applied_at and applied_cv")
        check(all(new[c]["type"] == "TEXT" and new[c]["notnull"] == 0
                  and new[c]["dflt"] is None
                  for c in ("applied_at", "applied_cv")),
              "both are nullable text with no default")
        check(list(new)[:len(names)] == names
              and list(new)[len(names):] == ["applied_at", "applied_cv"],
              "they land at the end of the table and the old columns keep "
              "their order")
        check(fingerprint(shipped, names) == before and len(before) == 6,
              "every existing column of every existing row is exactly what "
              "it was")
        raw = sqlite3.connect(shipped)
        stamped = raw.execute("SELECT COUNT(*) FROM opportunities WHERE"
                              " applied_at IS NOT NULL OR applied_cv IS NOT"
                              " NULL").fetchone()[0]
        integrity = raw.execute("PRAGMA integrity_check").fetchone()[0]
        raw.close()
        check(stamped == 0 and integrity == "ok",
              "no row reads as applied after the migration, and the file "
              "passes its integrity check")
        radar_common.get_db(shipped).close()
        check([c["name"] for c in columns(shipped)] == list(new)
              and fingerprint(shipped, names) == before,
              "a second open changes nothing")

        # A database that has not been migrated yet, read the way the static
        # page writer reads one: read only, no migration. It reads as nothing
        # applied rather than failing.
        legacy = Path(tmp) / "legacy.sqlite"
        shutil.copyfile(base, legacy)
        ro = sqlite3.connect(f"file:{legacy.as_posix()}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            data = build_dashboard.collect(ro, CFG)
            check(data["applied"] == [] and len(data["opportunities"]) == 2
                  and len(data["acknowledged"]) == 1
                  and len(data["retired_roles"]) == 3,
                  "a database not yet migrated reads as nothing applied "
                  "when it is only read, and its lists are what they were")
        except sqlite3.OperationalError as err:
            check(False, f"reading an unmigrated database failed ({err})")
        finally:
            ro.close()

        # Several openers first-opening the same upgraded database at once.
        errors: list[str] = []
        for round_ in range(8):
            race = Path(tmp) / f"race{round_}.sqlite"
            shutil.copyfile(base, race)
            barrier = threading.Barrier(6)

            def opener():
                try:
                    barrier.wait(timeout=10)
                    radar_common.get_db(race).close()
                except Exception as err:  # noqa: BLE001  the test is to see none
                    errors.append(f"{type(err).__name__}: {err}")

            threads = [threading.Thread(target=opener) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        check(not errors,
              "six threads, each with a connection of its own, opening an old "
              "database at the same moment all succeed, eight times over "
              f"({errors[:2]})")
        both = [c["name"] for c in columns(Path(tmp) / "race0.sqlite")
                if c["name"] in ("applied_at", "applied_cv")]
        check(both == ["applied_at", "applied_cv"],
              "and each column is there exactly once")


def status_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # The status check cannot take a new word without a table rebuild,
        # which is why the marker is two columns and not a status.
        live = fresh(tmp, "status.sqlite")
        try:
            seed(live, title="x", company="y", status="applied")
            refused = False
        except sqlite3.IntegrityError:
            refused = True
        check(refused, "the status check still refuses the word applied, so "
                       "the marker is not a status")
        schema = (REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        check("'new','digested','actioned','dead'" in schema
              and "applied" not in schema.split("CREATE TABLE IF NOT EXISTS"
                                                " companies")[0],
              "and the schema file is as it was, the columns come in through "
              "the migration list like the other lifecycle stamps")
        live.close()


# ---------------------------------------------------------------- the rule

def chain_rule_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = fresh(tmp)
        x_stamped = role(conn, QD, applied_at=days_ago(5), applied_cv="cv.pdf",
                         first_seen=days_ago(30), last_seen=days_ago(30))
        x_repost = role(conn, (" quality  DIRECTOR ", "VELTRIX diagnostics"),
                        first_seen=days_ago(1), last_seen=days_ago(1))
        x_closed = role(conn, QD, closed_at=days_ago(20),
                        closed_source="page_check")
        y_other = role(conn, RD)
        z_stamped = seed(conn, title=None, company="Anon", role_key=None,
                         applied_at=days_ago(5))
        z_plain = seed(conn, title="Sales Rep", company=None, role_key=None)
        every = {x_stamped, x_repost, x_closed, y_other, z_stamped, z_plain}
        want = {x_stamped, x_repost, x_closed, z_stamped}

        got = {r[0] for r in conn.execute(
            "SELECT id FROM opportunities WHERE " + radar_common.APPLIED_SQL)}
        check(got == want,
              "the SQL condition picks the stamped row, its repost with a "
              "different spelling, its closed sibling and the lone stamped "
              "row, and nothing else")
        none = {r[0] for r in conn.execute(
            "SELECT id FROM opportunities WHERE "
            + radar_common.NOT_APPLIED_SQL)}
        check(none == every - want,
              "and its negation picks exactly the rest, a row with no key "
              "and no stamp included, so the NULLs never make a third state")
        keys = radar_common.applied_role_keys(conn)
        check(keys == {radar_common.role_key(*QD)},
              "the keys helper returns the one chain, and not the lone row")
        rows = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM opportunities")}
        check({i for i, r in rows.items()
               if radar_common.row_is_applied(r, keys)} == want,
              "the Python test of a loaded row gives the same answer for "
              "every row")
        check({i for i in every if radar_common.role_is_applied(conn, i)}
              == want and not radar_common.role_is_applied(conn, 99999),
              "the single-row helper agrees, and a row that does not exist "
              "is not applied")
        check(radar_common.row_is_applied({"id": 1}, set()) is False,
              "a mapping without the columns reads as not applied")
        cur = conn.execute("UPDATE opportunities SET notes = 'touched' WHERE "
                           + radar_common.NOT_APPLIED_SQL)
        check(cur.rowcount == 2
              and {r[0] for r in conn.execute(
                  "SELECT id FROM opportunities WHERE notes = 'touched'")}
              == {y_other, z_plain},
              "the condition works inside an UPDATE of the same table too")
        conn.commit()
        # Undo returns the chain whole, nothing was copied onto it.
        conn.execute("UPDATE opportunities SET applied_at = NULL"
                     " WHERE id = ?", (x_stamped,))
        conn.commit()
        check(not radar_common.role_is_applied(conn, x_repost)
              and not radar_common.role_is_applied(conn, x_closed),
              "clear the one stamp and the whole chain is open again, "
              "because the stamp was never copied onto it")

        # Random tables, a fixed seed. The SQL condition, its negation, the
        # key helper, the loaded-row helper and the single-row helper must
        # agree with a plain reading of the rule on every row of every table:
        # applied when stamped, or when another row with the same key is, and
        # a row with no key only ever by its own stamp.
        import random
        rng = random.Random(20261112)
        disagreements = 0
        cases = 0
        for _round in range(80):
            conn.execute("DELETE FROM opportunities")
            conn.commit()
            rows = []
            for _ in range(rng.randint(1, 30)):
                key = rng.choice([None, "k1", "k2", "k3", "k4"])
                stamped = rng.random() < 0.2
                rows.append((seed(conn, title="t", company="c", role_key=key,
                                  applied_at=days_ago(1) if stamped else None),
                             key, stamped))
            stamped_keys = {k for _i, k, s in rows if s and k is not None}
            truth = {i for i, k, s in rows
                     if s or (k is not None and k in stamped_keys)}
            everyone = {i for i, _k, _s in rows}
            in_sql = {r[0] for r in conn.execute(
                "SELECT id FROM opportunities WHERE " + radar_common.APPLIED_SQL)}
            out_sql = {r[0] for r in conn.execute(
                "SELECT id FROM opportunities WHERE "
                + radar_common.NOT_APPLIED_SQL)}
            keys = radar_common.applied_role_keys(conn)
            loaded = [dict(r) for r in conn.execute("SELECT * FROM opportunities")]
            in_py = {r["id"] for r in loaded if radar_common.row_is_applied(r, keys)}
            in_one = {i for i in everyone if radar_common.role_is_applied(conn, i)}
            cases += len(rows)
            if not (in_sql == truth == in_py == in_one
                    and out_sql == everyone - truth and keys == stamped_keys):
                disagreements += 1
        check(disagreements == 0,
              "across eighty random tables, the SQL, its negation and the "
              f"three helpers agree with the rule on all {cases} rows")
        conn.close()


# ------------------------------------------------------------------ routes

def routes_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "routes.sqlite"
        conn = radar_common.get_db(db)
        a = role(conn, QD, first_seen=days_ago(20), last_seen=days_ago(20))
        b = role(conn, QD)                                       # the repost
        c = role(conn, QD, closed_at=days_ago(9), closed_source="page_check")
        lone = seed(conn, title=None, company="Anon", role_key=None,
                    tier="question")
        acked = role(conn, ("Clinical Lead", "Novem"),
                     acknowledged_at=days_ago(3))
        closed = role(conn, ("Safety Officer", "Kordis"),
                      closed_at=days_ago(2), closed_source="manual")
        dismissed = role(conn, ("Auditor", "Mentis"), dismissed_at=days_ago(2))
        expired = role(conn, ("Analyst", "Ploom"), expired_at=days_ago(2))
        conn.close()

        def state(oid):
            r = reader(db)
            try:
                return row_of(r, oid)
            finally:
                r.close()

        def everything():
            r = reader(db)
            try:
                return snapshot(r)
            finally:
                r.close()

        with serving(db) as port:
            lo = radar_common.now_iso()
            status, got, hdrs = post(port, "/apply", {"id": a})
            hi = radar_common.now_iso()
            check(status == 200 and got == {"ok": True, "id": a,
                                            "applied": True},
                  f"POST /apply on an open row answers 200 and says so ({status} {got})")
            s = state(a)
            check(ISO.match(s["applied_at"] or "") and lo <= s["applied_at"] <= hi
                  and s["applied_cv"] is None,
                  "the stamp is the moment of the click, in the format every "
                  "other stamp uses, and the CV reference is empty")
            check(all(state(i)["applied_at"] is None for i in (b, c, lone)),
                  "only the row that was pressed carries the stamp, nothing is "
                  "copied onto its chain")
            check(hdrs.get("x-frame-options") == "DENY",
                  "the reply carries the anti-framing headers like every other")

            before = everything()
            status, got, _ = post(port, "/apply", {"id": a})
            check(status == 409 and "already marked applied" in got.get("note", "")
                  and everything() == before,
                  "applying twice is refused and changes nothing")
            status, got, _ = post(port, "/apply", {"id": b})
            check(status == 409 and "already marked applied" in got.get("note", "")
                  and everything() == before,
                  "so is applying on the repost of an applied role, the chain "
                  "is one role")
            status, got, _ = post(port, "/apply", {"id": c})
            check(status == 409 and everything() == before,
                  "and on its closed sibling")

            # Undo from the repost's id reaches the stamped row.
            status, got, _ = post(port, "/unapply", {"id": b})
            check(status == 200 and got.get("applied") is False
                  and state(a)["applied_at"] is None
                  and state(a)["applied_cv"] is None,
                  "Undo from any row of the chain clears the stamp, wherever "
                  "it sits")
            status, got, _ = post(port, "/unapply", {"id": a})
            check(status == 409 and "maybe already handled" in got.get("note", ""),
                  "Undo on a role that is not applied is refused politely")
            status, got, _ = post(port, "/unapply", {"id": 99999})
            check(status == 409 and "no opportunity 99999" in got.get("note", ""),
                  "and so is Undo on a row that does not exist")

            # The saved CV reference.
            status, got, _ = post(port, "/apply", {"id": a, "cv": "  cv-2026-11-12.pdf "})
            check(status == 200 and state(a)["applied_cv"] == "cv-2026-11-12.pdf",
                  "a CV reference is kept, trimmed")
            post(port, "/unapply", {"id": a})
            check(state(a)["applied_cv"] is None and state(a)["applied_at"] is None,
                  "and Undo clears it with the date")
            for label, junk in (("a number", 123), ("a list", ["x"]),
                                ("a line break", "two\nlines"),
                                ("a control character", "bad\x07bell"),
                                ("two hundred and one characters", "c" * 201)):
                before = everything()
                status, got, _ = post(port, "/apply", {"id": a, "cv": junk})
                check(status == 400 and everything() == before,
                      f"a CV reference that is {label} is a 400 and writes nothing")
            status, got, _ = post(port, "/apply", {"id": a, "cv": "c" * 200})
            check(status == 200 and state(a)["applied_cv"] == "c" * 200,
                  "two hundred characters is allowed")
            post(port, "/unapply", {"id": a})
            status, got, _ = post(port, "/apply", {"id": a, "cv": "   "})
            check(status == 200 and state(a)["applied_cv"] is None,
                  "a blank reference means none")
            post(port, "/unapply", {"id": a})

            # What the id must be.
            for label, payload in (("missing", {}), ("a string", {"id": "1"}),
                                   ("true", {"id": True}),
                                   ("a float", {"id": 1.5}),
                                   ("null", {"id": None}),
                                   ("too large for SQLite", {"id": 2 ** 70}),
                                   ("too small for SQLite", {"id": -2 ** 70})):
                before = everything()
                status, got, _ = post(port, "/apply", payload)
                check(status == 400 and everything() == before,
                      f"an id that is {label} is a 400 and writes nothing")
            status, got, _ = post(port, "/apply", b"{not json")
            check(status == 400, "a body that is not JSON is a 400")
            status, got, _ = post(port, "/apply", {"id": 99999})
            check(status == 409 and "no opportunity 99999" in got.get("note", ""),
                  "an id that does not exist is refused by name")

            # Only an open role can be applied for.
            for label, oid in (("acknowledged", acked), ("closed", closed),
                               ("dismissed", dismissed), ("expired", expired)):
                before = everything()
                status, got, _ = post(port, "/apply", {"id": oid})
                check(status == 409 and everything() == before,
                      f"a row that is {label} cannot be applied for, and "
                      "nothing is written")

            # A role marked applied cannot be acknowledged, closed or
            # dismissed by a page that was open before the button was pressed.
            post(port, "/apply", {"id": a})
            frozen = everything()
            for path in ("/ack", "/close", "/dismiss"):
                for oid, who in ((a, "the stamped row"), (b, "its repost")):
                    status, got, _ = post(port, path, {"id": oid})
                    check(status == 409
                          and "marked applied" in got.get("note", "")
                          and everything() == frozen,
                          f"POST {path} on {who} says the role is marked "
                          "applied and changes nothing")
            # The old verdicts still work on a role that is not applied.
            status, got, _ = post(port, "/ack", {"id": lone})
            check(status == 200 and state(lone)["acknowledged_at"],
                  "Acknowledge still works on a role that is not applied")
            status, got, _ = post(port, "/unack", {"id": lone})
            check(status == 200 and state(lone)["acknowledged_at"] is None,
                  "and its undo")
            status, got, _ = post(port, "/close", {"id": lone})
            check(status == 200 and state(lone)["closed_at"],
                  "and Closed")
            post(port, "/unclose", {"id": lone})
            status, got, _ = post(port, "/dismiss", {"id": lone})
            check(status == 200 and state(lone)["dismissed_at"],
                  "and Dismiss")
            post(port, "/undismiss", {"id": lone})

            # Undo restores the role to the open views untouched.
            post(port, "/unapply", {"id": a})
            r = state(a)
            check(r["tier"] == "top" and r["acknowledged_at"] is None
                  and r["closed_at"] is None and r["dismissed_at"] is None
                  and r["expired_at"] is None,
                  "after Undo the role is exactly as it was, tier and all")

        # Unit level: the function behind the routes.
        with pointed_at(db):
            check(serve_dashboard.set_applied(a, True)["ok"]
                  and not serve_dashboard.set_applied(a, True)["ok"]
                  and serve_dashboard.set_applied(a, False)["ok"],
                  "set_applied applies once, refuses a repeat and undoes")
            # The statement's own condition: two pages open at once, both
            # pressing Applied on different adverts of one role. The second
            # check in Python has missed the first stamp, the UPDATE has not.
            serve_dashboard.set_applied(a, True)
            with blind_to_applied():
                late = serve_dashboard.set_applied(b, True)
            r = reader(db)
            check(not late["ok"] and row_of(r, b)["applied_at"] is None,
                  "with the Python check blinded the UPDATE's own condition "
                  "still refuses the second stamp on a chain")
            r.close()
            serve_dashboard.set_applied(a, False)
        check(serve_dashboard.clean_cv_reference(None) is None
              and serve_dashboard.clean_cv_reference("") is None
              and serve_dashboard.clean_cv_reference(" a ") == "a",
              "clean_cv_reference reads none, empty and padded as it should")


def concurrency_checks() -> None:
    """Two pages open at once is not a rare thing. The stamp goes to one row,
    the chain is one role, and no interleaving may leave it with two stamps.

    A handful of threads, not a storm. Each request opens its own connection
    through get_db, and a burst of a dozen simultaneous opens in one process
    stalls for seconds on some Linux builds of SQLite, on the code before this
    change as much as after it. That is a property of the opens and says
    nothing about the rule, so the client waits long and the number is small.
    The race between the check and the write is pinned, with no threads, by
    the blinded-check tests on the routes."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "race.sqlite"
        conn = radar_common.get_db(db)
        chain = [role(conn, QD) for _ in range(4)]
        conn.close()
        results: list[tuple[str, int]] = []
        lock = threading.Lock()
        with serving(db) as port:
            headers = page_headers(port)

            def press(path, oid):
                status, _got, _h = post(port, path, {"id": oid}, headers,
                                        timeout=120)
                with lock:
                    results.append((path, status))

            threads = [threading.Thread(target=press, args=("/apply", oid))
                       for oid in chain]
            for t_ in threads:
                t_.start()
            for t_ in threads:
                t_.join(timeout=180)
            look = reader(db)
            stamped = look.execute("SELECT COUNT(*) FROM opportunities WHERE"
                                   " applied_at IS NOT NULL").fetchone()[0]
            look.close()
            wins = sum(1 for _p, s in results if s == 200)
            check(len(results) == 4 and stamped == 1 and wins == 1
                  and {s for _p, s in results} <= {200, 409},
                  "four simultaneous presses of Applied on different adverts "
                  "of one role: one wins, the rest are refused, one stamp "
                  f"({wins} won, {stamped} stamped)")


# ------------------------------------------------------------------- guard

def guard_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "guard.sqlite"
        conn = radar_common.get_db(db)
        a = role(conn, QD, applied_at=days_ago(2))
        b = role(conn, RD)
        conn.close()
        with serving(db) as port:
            own = f"127.0.0.1:{port}"
            forged = (
                ("a forged no-cors POST from another site",
                 {"Host": own, "Origin": f"https://{EVIL}",
                  "Content-Type": "text/plain;charset=UTF-8"}),
                ("a forged POST that guessed the header",
                 {"Host": own, "Origin": f"https://{EVIL}", HEADER: "1"}),
                ("a DNS rebinding page, header and all",
                 {"Host": f"{EVIL}:{port}", "Origin": f"http://{EVIL}:{port}",
                  HEADER: "1"}),
                ("a sandboxed page whose Origin is null",
                 {"Host": own, "Origin": "null", HEADER: "1"}),
                ("a POST with no header", {"Host": own}),
                ("a POST whose header is empty", {"Host": own, HEADER: ""}),
            )
            for path, payload in (("/apply", {"id": b}), ("/unapply", {"id": a})):
                for name, extra in forged:
                    r = reader(db)
                    before = snapshot(r)
                    r.close()
                    headers = {"Content-Type": "application/json"}
                    headers.update(extra)
                    with captured_stderr() as buf:
                        status, got, hdrs = post(port, path, payload, headers)
                    r = reader(db)
                    after = snapshot(r)
                    r.close()
                    refused = any(ln.startswith("write guard refused")
                                  for ln in buf.getvalue().splitlines())
                    check(status == 403 and refused and after == before
                          and "The write guard refused this request"
                          in got.get("note", "")
                          and hdrs.get("x-frame-options") == "DENY",
                          f"{path}: {name} is refused with a 403 before the "
                          "route runs, the file is untouched, framing is "
                          "forbidden on the reply")
            # The same request with the page's own headers goes through.
            status, got, _ = post(port, "/apply", {"id": b})
            check(status == 200, "and the page's own request is accepted")

        # Warn mode logs the forgery and lets it through, as it does for
        # every other route. This is what the box saw before the flip.
        warn = serve_dashboard.WriteGuard("warn", serve_dashboard.LOOPBACK_NAMES)
        with serving(db, guard=warn) as port:
            own = f"127.0.0.1:{port}"
            with captured_stderr() as buf:
                status, got, _ = post(port, "/unapply", {"id": a},
                                      {"Host": own, "Origin": f"https://{EVIL}",
                                       "Content-Type": "application/json"})
            check(status == 200 and "would refuse POST '/unapply'"
                  in buf.getvalue(),
                  "in warn mode a forged undo is logged as one that would be "
                  "refused")


# --------------------------------------------------------------- page script

NODE_CLICK = r"""
const vm = require('vm');
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let bad = 0;
const say = (ok, label) => {
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label);
  if (!ok) bad += 1;
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  if (typeof Headers === 'undefined') {
    console.log('NOTE  this node has no Headers, so the buttons were not pressed');
    process.exit(0);
  }
  const calls = [];
  const handlers = [];
  const reloads = [];
  const alerts = [];
  const asked = [];
  let agree = true;
  let reply = { ok: true };
  let fail = false;
  const sandbox = {
    Headers,
    document: {
      addEventListener(type, fn) { handlers.push({ type, fn }); },
      getElementById() { return null; },
      querySelectorAll() { return []; },
      documentElement: { setAttribute() {} },
    },
    localStorage: { getItem() { return null; }, setItem() {} },
    location: { reload() { reloads.push(1); } },
    setTimeout() {},
  };
  sandbox.window = sandbox;
  sandbox.alert = (text) => { alerts.push(text); };
  sandbox.confirm = (text) => { asked.push(text); return agree; };
  sandbox.fetch = (url, opts) => {
    calls.push({ url, opts });
    if (fail) return Promise.reject(new Error('network'));
    return Promise.resolve({ json: () => Promise.resolve(reply) });
  };
  vm.createContext(sandbox);
  try {
    vm.runInContext(data.shared + '\n' + data.serve, sandbox);
  } catch (err) {
    say(false, `the page scripts run together (${err.message})`);
    process.exit(1);
  }
  const press = async (attr, id, label) => {
    const btn = {
      dataset: { [attr]: String(id) }, disabled: false, textContent: label,
      hasAttribute(name) { return name === 'data-' + attr; },
    };
    const event = { target: { closest(sel) {
      return sel.includes('[data-' + attr + ']') ? btn : null;
    } } };
    calls.length = 0; reloads.length = 0; alerts.length = 0; asked.length = 0;
    for (const h of handlers.filter((x) => x.type === 'click')) h.fn(event);
    await settle(); await settle(); await settle();
    return btn;
  };
  for (const [attr, path, label] of [['apply', '/apply', 'Applied, sent it'],
                                     ['unapply', '/unapply', 'Undo']]) {
    reply = { ok: true }; fail = false;
    let btn = await press(attr, 42, label);
    const sent = calls[0] && calls[0].opts;
    say(calls.length === 1 && calls[0].url === path && sent.method === 'POST'
        && sent.body === '{"id":42}'
        && sent.headers instanceof Headers
        && sent.headers.get(data.header.toLowerCase()) === '1'
        && sent.headers.get('content-type') === 'application/json',
        `the ${attr} button posts its row id to ${path} through the fetch `
        + `wrapper, with ${data.header} and its own content type`);
    say(reloads.length === 1 && alerts.length === 0,
        `a successful ${attr} reloads the page and says nothing`);
    reply = { ok: false, note: 'The write guard refused this request. Origin is not ours.' };
    btn = await press(attr, 42, label);
    say(alerts.length === 1 && alerts[0] === reply.note && reloads.length === 0
        && btn.disabled === false && btn.textContent === label,
        `a refused ${attr} shows the guard's own reason, restores the button `
        + 'and does not reload');
    reply = { ok: false };
    btn = await press(attr, 42, label);
    say(alerts.length === 1 && /did not stick/.test(alerts[0])
        && btn.textContent === label,
        `a refusal with no note still says so on the ${attr} button`);
    fail = true;
    btn = await press(attr, 42, label);
    say(alerts.length === 1 && /No usable answer/.test(alerts[0])
        && btn.disabled === false && btn.textContent === label && reloads.length === 0,
        `a failed request on the ${attr} button says it may not have saved`);
  }
  // Undo throws the day away, so it asks first. Applying does not ask.
  reply = { ok: true }; fail = false; agree = true;
  let btn = await press('unapply', 42, 'Undo');
  say(asked.length === 1 && /Undo this application/.test(asked[0])
      && /forgotten/.test(asked[0]) && calls.length === 1,
      'Undo asks before it sends, says the day is forgotten, and sends on a yes');
  agree = false;
  btn = await press('unapply', 42, 'Undo');
  say(asked.length === 1 && calls.length === 0 && reloads.length === 0
      && alerts.length === 0 && btn.disabled === false
      && btn.textContent === 'Undo',
      'and on a no sends nothing, reloads nothing and leaves the button as it was');
  agree = false;
  await press('apply', 42, 'Applied, sent it');
  say(asked.length === 0 && calls.length === 1 && calls[0].url === '/apply',
      'the Applied button does not ask, a click that is easy to undo');
  // The older buttons are untouched.
  agree = true; reply = { ok: true }; fail = false;
  await press('close', 7, 'Closed, role gone');
  say(calls.length === 1 && calls[0].url === '/close' && asked.length === 0,
      'the Closed button still posts to /close, without a question');
  process.exit(bad ? 1 : 0);
})();
"""


def page_script_checks() -> None:
    serve = build_dashboard.SERVE_SCRIPT
    check("[data-apply]" in serve and "[data-unapply]" in serve
          and "'apply', 'unapply'" in serve,
          "the served page script knows the Applied button and its undo")
    check(serve.count("fetch(") == serve.count("method: 'POST'") > 0
          and "XMLHttpRequest" not in serve and "sendBeacon" not in serve,
          "and every request the served script makes is a fetch with method "
          "POST, the one shape the header wrapper covers")
    check("kind === 'unapply' && !window.confirm(" in serve
          and serve.count("window.confirm(") == 1,
          "and Undo, which throws the day applied away, is the one button "
          "that asks before it sends")
    node = shutil.which("node")
    if not node:
        note("node is not on the path, so the Applied buttons were not pressed")
        return
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "click.js").write_text(NODE_CLICK, encoding="utf-8")
        (Path(tmp) / "scripts.json").write_text(json.dumps({
            "shared": build_dashboard.SCRIPT, "serve": serve,
            "header": HEADER}), encoding="utf-8")
        try:
            done = subprocess.run(
                [node, str(Path(tmp) / "click.js"),
                 str(Path(tmp) / "scripts.json")],
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as err:
            check(False, f"node pressed the buttons ({type(err).__name__})")
            return
        lines = done.stdout.splitlines()
        for ln in lines:
            if ln.startswith(("PASS  ", "FAIL  ")):
                check(ln.startswith("PASS"), ln[6:])
            elif ln.startswith("NOTE  "):
                note(ln[6:])
        if done.returncode != 0 and not any(ln.startswith("FAIL  ")
                                            for ln in lines):
            check(False, "node pressed the buttons ("
                  + (done.stderr.strip()[:200] or f"exit {done.returncode}")
                  + ")")


# ------------------------------------------------------------ the inbox repost

def email_b64(subject: str, body: str) -> str:
    raw = json.dumps({"subject": subject, "from": "jobalerts-noreply@linkedin.com",
                      "date": "2026-11-09", "body_text": body, "body_html": ""})
    return base64.b64encode(raw.encode()).decode("ascii")


def alert(title: str, company: str, job_id: int,
          where: str = "Ghent, Belgium", pay: str = "£950 per day") -> str:
    return (f"{title}\n{company} · {where}\n{pay}\n"
            f"View job: https://www.linkedin.com/jobs/view/{job_id}/\n")


def run_inbox(db: Path, body: str) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), captured_stderr():
        code = process_email.main(["--mock", "--db", str(db), "--b64",
                                   email_b64("New jobs for you", body)])
    return {"code": code,
            "json": json.loads(out.getvalue().strip().splitlines()[-1])}


def repost_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "repost.sqlite"
        # Day one: the role arrives, and so does an identical role at another
        # firm. Both score Top under the mock scorer.
        first = run_inbox(db, alert(*QD, 4100000001) + "\n"
                          + alert(*RD, 4100000002))
        check(first["code"] == 0 and first["json"]["new"] == 2,
              "the inbox stores both roles on day one")
        conn = radar_common.get_db(db)
        qd1 = conn.execute("SELECT * FROM opportunities WHERE company = ?",
                           (QD[1],)).fetchone()
        rd1 = conn.execute("SELECT * FROM opportunities WHERE company = ?",
                           (RD[1],)).fetchone()
        check(qd1["tier"] == "top" and rd1["tier"] == "top",
              f"both are Top rows under the mock scorer ({qd1['tier']}, "
              f"{rd1['tier']})")
        conn.close()
        with pointed_at(db):
            check(serve_dashboard.set_applied(qd1["id"], True)["ok"],
                  "the first role is marked applied")
        # Day two: both roles are reposted under new links.
        second = run_inbox(db, alert(*QD, 4100000003, where="London, England")
                           + "\n" + alert(*RD, 4100000004,
                                          where="London, England"))
        check(second["code"] == 0 and second["json"]["new"] == 2,
              "the repost of each arrives as a new row with a new link")
        conn = radar_common.get_db(db)
        qd = [row_of(conn, r["id"]) for r in conn.execute(
            "SELECT id FROM opportunities WHERE company = ? ORDER BY id",
            (QD[1],))]
        rd = [row_of(conn, r["id"]) for r in conn.execute(
            "SELECT id FROM opportunities WHERE company = ? ORDER BY id",
            (RD[1],))]
        check(len(qd) == 2 and qd[0]["role_key"] == qd[1]["role_key"]
              and qd[1]["role_key"] is not None,
              "the repost gets the same role key as the original the moment "
              "it is stored")
        check(qd[0]["applied_at"] and qd[1]["applied_at"] is None
              and qd[1]["applied_cv"] is None,
              "and arrives with every stamp empty, as the brief says")
        check(radar_common.role_is_applied(conn, qd[1]["id"])
              and not radar_common.role_is_applied(conn, rd[1]["id"]),
              "the repost of the applied role is applied, the repost of the "
              "other role is not")

        # What the pages say.
        look = reader(db)
        data = build_dashboard.collect(look, CFG)
        look.close()
        open_ids = {o["id"] for o in data["opportunities"]}
        check(qd[0]["id"] not in open_ids and qd[1]["id"] not in open_ids
              and {rd[0]["id"], rd[1]["id"]} <= open_ids,
              "the open list holds both adverts of the role that is not "
              "applied, and neither of the applied one")
        check([o["id"] for o in data["applied"]] == [qd[0]["id"]],
              "the Applied list has the role once, on the row that was "
              "stamped, not the repost")
        jobs = jobs_html(db)
        tiers, applied = split_applied(jobs)
        check(QD[0] not in tiers and RD[0] in tiers,
              "the Jobs page's open tiers carry the other role and never the "
              "applied one, repost included")
        check(tiers.count(f"d-o") >= 1 and row_titles(tiers).count(RD[0]) == 1,
              "and the other role's two adverts are one row (the collapse)")
        check(row_titles(applied) == [QD[0]],
              "the Applied section lists the role once")
        home = home_html(db)
        card = re.search(r"(\d+) roles? in play, (\d+) new this week", home)
        check(card and int(card.group(1)) == 2 and int(card.group(2)) == 2,
              "the Home page's Jobs card counts the two adverts of the other "
              "role and none of the applied one's "
              f"({card.group(0) if card else None})")

        # The digest.
        digest = build_digest.collect(conn, CFG)
        in_digest = {o["id"] for o in digest["inbound"]}
        check({rd[0]["id"], rd[1]["id"]} <= in_digest
              and not ({qd[0]["id"], qd[1]["id"]} & in_digest),
              "the digest announces the other role and never the applied one "
              "or its repost as new")
        conn.close()


# ---------------------------------------------------------------- expiry

class Racing:
    """A connection that runs a hook just before a statement, to put a click
    in the gap between a selection and the write that follows it."""

    def __init__(self, real, prefix, hook):
        self.real, self.prefix, self.hook, self.fired = real, prefix, hook, False
        self.count = 0              # statements seen that start with the prefix

    def _maybe(self, sql):
        if sql.lstrip().startswith(self.prefix):
            self.count += 1
            if not self.fired:
                self.fired = True
                self.hook()

    def execute(self, sql, *args):
        self._maybe(sql)
        return self.real.execute(sql, *args)

    def executemany(self, sql, seq):
        self._maybe(sql)
        return self.real.executemany(sql, seq)

    def __getattr__(self, name):
        return getattr(self.real, name)


def expiry_checks() -> None:
    now = iso(NOW)
    old, older = days_ago(80), days_ago(70)
    with tempfile.TemporaryDirectory() as tmp:
        conn = fresh(tmp)

        def silent(who, **cols):
            return role(conn, who, first_seen=old, last_seen=old, **cols)

        xs = silent(QD, applied_at=days_ago(75))
        xr = role(conn, QD, first_seen=older, last_seen=older)    # unstamped repost
        z = seed(conn, title=None, company="Anon", role_key=None,
                 first_seen=old, last_seen=old, applied_at=days_ago(75))
        y = silent(RD)
        w = seed(conn, title="Sales Rep", company=None, role_key=None,
                 first_seen=old, last_seen=old)
        dry = check_jobs.expire_stale(conn, write=False, now=now)
        check(dry["expired"] == 2 and {s["id"] for s in dry["sample"]} == {y, w},
              "the dry pass would expire the two silent roles that are not "
              f"applied, and not the applied chain or the lone applied row ({dry})")
        wet = check_jobs.expire_stale(conn, write=True, now=now)
        gone = {r["id"] for r in conn.execute(
            "SELECT id FROM opportunities WHERE expired_at IS NOT NULL")}
        check(wet["expired"] == 2 and gone == {y, w},
              "the live pass stamps those two and no others, however long "
              "the applied roles have been silent")
        # The exemption is the only thing between them and expiry.
        conn.execute("UPDATE opportunities SET applied_at = NULL WHERE id IN (?, ?)",
                     (xs, z))
        conn.commit()
        again = check_jobs.expire_stale(conn, write=True, now=now)
        gone = {r["id"] for r in conn.execute(
            "SELECT id FROM opportunities WHERE expired_at IS NOT NULL")}
        check(again["expired"] == 3 and gone == {xs, xr, z, y, w},
              "undo the application and the next pass retires the whole "
              "chain and the lone row, so the exemption was all that held "
              "them")

        # The race: the click lands between the pass choosing its rows and
        # writing the stamps. The write's own condition holds.
        conn.execute("UPDATE opportunities SET expired_at = NULL")
        conn.commit()
        target = xs

        def click():
            conn.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                         (now, target))
            conn.commit()

        racing = Racing(conn, "UPDATE opportunities SET expired_at", click)
        check_jobs.expire_stale(racing, write=True, now=now)
        gone = {r["id"] for r in conn.execute(
            "SELECT id FROM opportunities WHERE expired_at IS NOT NULL")}
        check(racing.fired and xs not in gone and xr not in gone
              and {y, w, z} <= gone,
              "a role applied for after the pass chose its rows but before it "
              "wrote is not expired, the write checks again, its chain with "
              "it")

        # Past five hundred expiries the stamps go in several statements, and
        # every one of them carries the condition. Eleven hundred silent
        # roles make three statements, and the applied ones are scattered
        # through the whole list so that each statement has some.
        conn.execute("DELETE FROM opportunities")
        conn.commit()
        total = 1_100
        rows = []
        for i in range(total):
            who = (f"Role {i}", f"Firm {i}")
            rows.append((f"bulk-{i}", old, old, who[0], who[1],
                         radar_common.role_key(*who)))
        conn.executemany(
            "INSERT INTO opportunities (url_hash, first_seen, last_seen,"
            " status, thread_type, title, company, role_key, tier) VALUES"
            " (?, ?, ?, 'new', 'inbound', ?, ?, ?, 'top')", rows)
        conn.commit()
        every = [r[0] for r in conn.execute("SELECT id FROM opportunities"
                                            " ORDER BY id")]
        clicked = every[::37]

        def click_many():
            conn.executemany("UPDATE opportunities SET applied_at = ?"
                             " WHERE id = ?", [(now, i) for i in clicked])
            conn.commit()

        racing = Racing(conn, "UPDATE opportunities SET expired_at", click_many)
        wet = check_jobs.expire_stale(racing, write=True, now=now)
        gone = {r[0] for r in conn.execute(
            "SELECT id FROM opportunities WHERE expired_at IS NOT NULL")}
        check(wet["expired"] == total and racing.count == 3,
              "eleven hundred roles are chosen and stamped in three "
              f"statements ({wet['expired']} chosen, {racing.count} "
              "statements)")
        check(len(clicked) >= 20 and not (gone & set(clicked))
              and len(gone) == total - len(clicked),
              f"and the {len(clicked)} applied for in the gap are in none of "
              "them, every statement checks again, the rest are all stamped")
        conn.close()


# -------------------------------------------------------------- the checker

def checker_checks() -> None:
    fixtures = REPO_ROOT / "test" / "fixtures" / "linkedin"
    closed_page = (fixtures / "closed.html").read_text(encoding="utf-8")
    live_page = (fixtures / "live.html").read_text(encoding="utf-8")
    jid = "4482760631"
    title = "Principal Medical Device Engineer"
    now = iso(NOW)

    def url(n):
        return f"https://www.linkedin.com/jobs/view/{n}/"

    def planted(conn, who, n, **cols):
        return role(conn, who, source_url=url(n), first_seen=days_ago(30),
                    last_seen=days_ago(30), **cols)

    with tempfile.TemporaryDirectory() as tmp:
        conn = fresh(tmp)
        calls: list[str] = []
        pages: dict = {}

        def fake_fetch(u):
            calls.append(u)
            n = check_jobs.job_view_id(u)
            return pages.get(n, (200, u, closed_page))

        real_fetch = check_jobs.fetch_job_page
        check_jobs.fetch_job_page = fake_fetch
        try:
            a1 = planted(conn, QD, 111, title=title, applied_at=days_ago(9))
            a2 = planted(conn, QD, 112, title=title)         # its repost
            c = planted(conn, RD, 113, title=title + " II")  # not applied
            # ----- selection
            due = check_jobs.due_candidates(conn, now)
            check([r["id"] for r in due] == [c],
                  "the checker's selection holds the role that is not applied "
                  "and neither advert of the one that is")
            # ----- the spot check
            calls.clear()
            spot = check_jobs.run_checks(conn, only_id=a1, write=True, now=now,
                                         pause=lambda: None)
            check(not calls and spot["checked"] == 0 and spot["due"] == 0
                  and any("marked applied" in n for n in spot["notes"]),
                  "--id on the stamped row is refused, nothing is fetched")
            spot = check_jobs.run_checks(conn, only_id=a2, write=True, now=now,
                                         pause=lambda: None)
            check(not calls and spot["checked"] == 0
                  and any("marked applied" in n for n in spot["notes"]),
                  "--id on its repost is refused too, the chain is one role")
            check(row_of(conn, a1)["last_checked_at"] is None
                  and row_of(conn, a2)["closed_at"] is None,
                  "and neither row is stamped")
            spot = check_jobs.run_checks(conn, only_id=c, write=False, now=now,
                                         pause=lambda: None)
            check(len(calls) == 1 and spot["checked"] == 1,
                  "--id on a role that is not applied still fetches its page")
            # ----- the live run
            calls.clear()
            run = check_jobs.run_checks(conn, cap=25, write=True, now=now,
                                        pause=lambda: None)
            check(run["due"] == 1 and len(calls) == 1 and run["closed"] == 1,
                  "a live run fetches and closes the role that is not applied "
                  "and never asks for the applied one's page")
            check(row_of(conn, c)["closed_at"] and not row_of(conn, a1)["closed_at"]
                  and not row_of(conn, a2)["closed_at"]
                  and row_of(conn, a1)["last_checked_at"] is None,
                  "the closed page closes the one and leaves the applied "
                  "chain unstamped")

            # ----- the race: applied while its page is being fetched
            conn.execute("UPDATE opportunities SET closed_at = NULL,"
                         " closed_source = NULL, last_checked_at = NULL,"
                         " last_check_result = NULL")
            conn.execute("UPDATE opportunities SET applied_at = NULL")
            conn.commit()

            first = check_jobs.due_candidates(conn, now)[0]["id"]
            before_row = row_of(conn, first)

            def apply_during_fetch(u):
                calls.append(u)
                conn.execute("UPDATE opportunities SET applied_at = ?"
                             " WHERE id = ?", (now, first))
                conn.commit()
                return (200, u, closed_page)

            check_jobs.fetch_job_page = apply_during_fetch
            calls.clear()
            run = check_jobs.run_checks(conn, cap=1, write=True, now=now,
                                        pause=lambda: None)
            c = first
            after_row = row_of(conn, c)
            check(len(calls) == 1 and run["closed"] == 0
                  and after_row["closed_at"] is None
                  and after_row["last_checked_at"] is None
                  and after_row["description_text"] == before_row["description_text"]
                  and any("marked applied since it fell due" in n
                          for n in run["notes"]),
                  "a role applied for while its page was being fetched is "
                  "not closed, not stamped, nothing is read off the page")

            # ----- the close on its own: the write refuses an applied role
            # even if the check before it were to miss the click.
            conn.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                         (now, c))
            conn.commit()
            check_jobs.fetch_job_page = fake_fetch
            real_helper = radar_common.role_is_applied
            radar_common.role_is_applied = lambda *_a, **_k: False
            try:
                calls.clear()
                run = check_jobs.run_checks(conn, only_id=c, write=True, now=now,
                                            pause=lambda: None)
            finally:
                radar_common.role_is_applied = real_helper
            check(len(calls) == 1 and run["closed"] == 0
                  and row_of(conn, c)["closed_at"] is None
                  and row_of(conn, c)["last_checked_at"] is None
                  and row_of(conn, c)["last_check_result"] is None,
                  "and the stamp and the close statements each refuse an "
                  "applied role by their own condition, whatever the checks "
                  "before them said")

            # ----- the click lands on another advert of the role. The Jobs
            # page shows one row for a role, so the advert pressed is often
            # not the advert being fetched, and the check after the fetch has
            # to look at the whole chain.
            sib = fresh(tmp, "sibling.sqlite")
            s1 = planted(sib, QD, 211, title=title)
            s2 = planted(sib, QD, 212, title=title)
            first = check_jobs.due_candidates(sib, now)[0]["id"]
            other = s2 if first == s1 else s1
            held = row_of(sib, first)

            def apply_to_sibling(u):
                calls.append(u)
                sib.execute("UPDATE opportunities SET applied_at = ?"
                            " WHERE id = ?", (now, other))
                sib.commit()
                return (200, u, closed_page)

            check_jobs.fetch_job_page = apply_to_sibling
            calls.clear()
            run = check_jobs.run_checks(sib, cap=1, write=True, now=now,
                                        pause=lambda: None)
            after = row_of(sib, first)
            check(len(calls) == 1 and run["closed"] == 0
                  and after["closed_at"] is None
                  and after["last_checked_at"] is None
                  and after["last_check_result"] is None
                  and after["description_text"] == held["description_text"]
                  and any("marked applied since it fell due" in n
                          for n in run["notes"]),
                  "a role applied for through another advert while this one's "
                  "page was being fetched is not closed, not stamped, nothing "
                  "is read off the page")
            sib.close()
        finally:
            check_jobs.fetch_job_page = real_fetch
        conn.close()


def apply_page_checks() -> None:
    """What the page checker writes after it has read a page. It looks again
    once it holds the write lock, so no click can land between the look and
    the write."""
    page = {"description": "A contract role in Ghent.", "employment_type": None,
            "pay": {"currency": "GBP", "period": "day", "min": 600.0,
                    "max": 600.0, "text": "£600 per day"},
            "pay_from": "top card"}
    limits = {"pass": 70, "floor": 40}
    bare = dict(pay_min=None, pay_max=None, pay_currency=None, pay_period=None,
                gate_rate=None, salary_rate="")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "page.sqlite"
        conn = radar_common.get_db(db)
        stamped = role(conn, QD, applied_at=days_ago(3), **bare)
        repost = role(conn, QD, **bare)
        free = role(conn, RD, **bare)
        before = snapshot(conn)
        out = check_jobs.apply_page(conn, row_of(conn, repost), page, FLOORS,
                                    CFG, write=True, thresholds=limits)
        check(out.get("left_alone") is True and snapshot(conn) == before,
              "a page read for the repost of an applied role writes nothing "
              "to it, to the stamped advert or to anything else")
        check(not conn.in_transaction and not writer_is_blocked(db),
              "and lets the write lock go again")
        out = check_jobs.apply_page(conn, row_of(conn, stamped), page, FLOORS,
                                    CFG, write=False, thresholds=limits)
        check(not out.get("left_alone") and snapshot(conn) == before,
              "a dry run says what it would do and takes no lock, it does not "
              "pretend to have been refused")

        # The lock is held when the second look is taken, so nothing can be
        # stamped between that look and the write that follows it.
        seen: list[bool] = []
        real = radar_common.role_is_applied

        def watching(c, oid):
            seen.append(writer_is_blocked(db))
            return real(c, oid)

        radar_common.role_is_applied = watching
        try:
            out = check_jobs.apply_page(conn, row_of(conn, free), page, FLOORS,
                                        CFG, write=True, thresholds=limits)
        finally:
            radar_common.role_is_applied = real
        conn.commit()
        check(seen == [True] and not out.get("left_alone")
              and row_of(conn, free)["pay_source"] == "page",
              "for a role that is not applied the second look is taken with "
              f"the write lock already held, and the page is stored ({seen})")
        check(not writer_is_blocked(db),
              "and the lock is gone once the caller commits")
        conn.close()


# ------------------------------------------------------------------- regate

def regate_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = fresh(tmp)
        cols = dict(pay_currency="GBP", pay_period="day", pay_min=600.0,
                    pay_max=600.0, salary_rate="£600 per day", rate_band="above",
                    gate_rate=1)
        # Scored Top when the floor was lower. At the floor now in force
        # (750 a day) £600 a day fails the rate gate and the tier drops.
        applied = role(conn, QD, applied_at=days_ago(4), **cols)
        sibling = role(conn, QD, **cols)
        other = role(conn, RD, **cols)
        lone_applied = seed(conn, title=None, company="Anon", role_key=None,
                            tier="top", gate_sector=1, gate_cv=1,
                            gate_location=1, gate_rate=1, cv_match=90,
                            applied_at=days_ago(4), **{k: v for k, v in cols.items()
                                                       if k != "gate_rate"})
        dry = regate_rates.regate(conn, CFG, FLOORS, write=False)
        check(dry["open_rows"] == 1 and dry["tier_moves"] == {"top to question": 1}
              and [s["id"] for s in dry["sample"]] == [other],
              "regate counts and would move only the role that is not applied "
              f"({dry['open_rows']} open, {dry['tier_moves']})")
        wet = regate_rates.regate(conn, CFG, FLOORS, write=True)
        check(wet["rows_changed"] == 1 and row_of(conn, other)["tier"] == "question",
              "and writes only that one")
        for oid, who in ((applied, "the stamped row"), (sibling, "its repost"),
                         (lone_applied, "the lone applied row")):
            r = row_of(conn, oid)
            check(r["tier"] == "top" and r["gate_rate"] == 1
                  and r["rate_band"] == "above",
                  f"{who} keeps the tier and rate gate it was applied with")
        hist = conn.execute("SELECT COUNT(*) FROM score_history WHERE"
                            " opportunity_id IN (?, ?, ?)",
                            (applied, sibling, lone_applied)).fetchone()[0]
        check(hist == 0, "and nothing is written to their score history")
        conn.execute("UPDATE opportunities SET applied_at = NULL")
        conn.commit()
        check(regate_rates.regate(conn, CFG, FLOORS, write=False)["tier_moves"]
              == {"top to question": 3},
              "undo the application and the same pass would move them, so the "
              "exemption was what held the tier")

        # A live pass takes the write lock before it chooses its rows, so a
        # click cannot land between the choice and the write. A dry run takes
        # none. Asked from inside the pass, at the first row it judges.
        db = Path(tmp) / "t.sqlite"
        seen: list[bool] = []
        real_judge = regate_rates.judge_row

        def watching(*args, **kwargs):
            seen.append(writer_is_blocked(db))
            return real_judge(*args, **kwargs)

        regate_rates.judge_row = watching
        try:
            regate_rates.regate(conn, CFG, FLOORS, write=False)
            dry_seen, seen[:] = list(seen), []
            regate_rates.regate(conn, CFG, FLOORS, write=True)
        finally:
            regate_rates.judge_row = real_judge
        check(dry_seen and not any(dry_seen) and seen and all(seen),
              "a live regate holds the write lock while it judges the rows it "
              "chose, a dry run holds none "
              f"(dry {dry_seen[:2]}, live {seen[:2]})")
        check(not conn.in_transaction and not writer_is_blocked(db)
              and row_of(conn, applied)["tier"] == "question",
              "and lets it go when it commits")
        again = regate_rates.regate(conn, CFG, FLOORS, write=True)
        check(again["rows_changed"] == 0 and not conn.in_transaction
              and not writer_is_blocked(db),
              "a live pass with nothing to write lets the lock go as well")
        conn.close()


# ------------------------------------------------------------------ rescore

def rescore_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = fresh(tmp)
        unscored = dict(tier=None, cv_match=None, gate_sector=None,
                        gate_cv=None, gate_location=None, gate_rate=None,
                        one_line_why=None, failed_gates=None)
        ax = role(conn, QD, applied_at=days_ago(4), **unscored)
        ar = role(conn, QD, **unscored)                          # its repost
        cx = role(conn, RD, **unscored)
        with captured_stderr():
            fixed, failed = rescore.rescore_opportunities(conn, CFG, {})
        check(fixed == 1 and failed == 0 and row_of(conn, cx)["tier"] == "top"
              and row_of(conn, ax)["tier"] is None and row_of(conn, ar)["tier"] is None,
              "the re-score of unscored roles scores the one that is not "
              "applied and leaves the applied chain with no tier")

        conn.execute("UPDATE opportunities SET tier = NULL WHERE id = ?", (cx,))
        conn.commit()
        dry = rescore.regate_backlog(conn, CFG, {}, 10, dry_run=True)
        check(dry["examined"] == 1 and dry["remaining"] == 1
              and [r["id"] for r in dry["would_examine"]] == [cx],
              "the back-catalogue pass examines the role that is not applied "
              "and counts only it as remaining")
        with captured_stderr():
            live = rescore.regate_backlog(conn, CFG, {}, 10, dry_run=False)
        check(live["examined"] == 1 and live["rescored"] == 1
              and live["remaining"] == 0 and row_of(conn, ax)["tier"] is None,
              "and live it scores that one, reports nothing remaining although "
              "an applied row still has no tier, and leaves the applied row alone")

        # Stale CV: scored rows whose stamp predates the active CV.
        conn.execute("UPDATE opportunities SET combined = 50, tier = 'reading',"
                     " cv_version = 'cv-old.md'")
        conn.commit()
        with captured_stderr():
            fixed, failed, remaining = rescore.rescore_stale_cv(conn, CFG, {}, 25)
        check(fixed == 1 and remaining == 0 and failed == 0
              and row_of(conn, cx)["cv_version"] != "cv-old.md"
              and row_of(conn, ax)["cv_version"] == "cv-old.md"
              and row_of(conn, ax)["tier"] == "reading"
              and row_of(conn, ar)["cv_version"] == "cv-old.md",
              "the stale-CV pass re-scores the role that is not applied, "
              "reports nothing remaining, and leaves the applied chain on "
              "the CV and tier it was applied with")

        # A role applied for while a pass was running: the per-row check.
        conn.execute("UPDATE opportunities SET tier = 'reading',"
                     " cv_version = 'cv-old.md' WHERE id = ?", (cx,))
        conn.commit()
        picked = [conn.execute("SELECT * FROM opportunities WHERE id = ?",
                               (i,)).fetchone() for i in (ax, cx)]
        conn.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                     (days_ago(0), cx))
        conn.commit()
        calls = []
        real = score_item.score_opportunity

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        score_item.score_opportunity = counting
        try:
            with captured_stderr():
                fixed, failed = rescore._rescore_rows(conn, CFG, {}, picked)
        finally:
            score_item.score_opportunity = real
        check(fixed == 0 and failed == 0 and not calls
              and row_of(conn, cx)["cv_version"] == "cv-old.md",
              "rows picked before the click are skipped when the pass reaches "
              "them, no model call is made for them")

        # Applied for while the model is answering: no write, no history.
        conn.execute("DELETE FROM score_history")
        conn.execute("DELETE FROM opportunities")
        conn.commit()
        target = role(conn, QD, **unscored)
        picked = [conn.execute("SELECT * FROM opportunities WHERE id = ?",
                               (target,)).fetchone()]

        def applied_during_answer(*args, **kwargs):
            out = real(*args, **kwargs)
            conn.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                         (days_ago(0), target))
            conn.commit()
            return out

        score_item.score_opportunity = applied_during_answer
        try:
            with captured_stderr():
                fixed, failed = rescore._rescore_rows(conn, CFG, {}, picked)
        finally:
            score_item.score_opportunity = real
        held = conn.execute("SELECT COUNT(*) FROM score_history WHERE"
                            " opportunity_id = ?", (target,)).fetchone()[0]
        check(fixed == 0 and row_of(conn, target)["tier"] is None and held == 0,
              "a role applied for while the model was answering keeps what it "
              "had, the answer is dropped and no history row is made")

        # The model call runs with no lock held, so the dashboard is not made
        # to wait on it. The second look and the write run with the lock held,
        # so nothing can be applied for in between.
        db = Path(tmp) / "t.sqlite"
        conn.execute("DELETE FROM score_history")
        conn.execute("DELETE FROM opportunities")
        conn.commit()
        target = role(conn, QD, **unscored)
        picked = [conn.execute("SELECT * FROM opportunities WHERE id = ?",
                               (target,)).fetchone()]
        in_call: list[bool] = []
        in_write: list[bool] = []
        real_archive = radar_common.archive_score

        def spying_call(*args, **kwargs):
            in_call.append(writer_is_blocked(db))
            return real(*args, **kwargs)

        def spying_archive(*args, **kwargs):
            in_write.append(writer_is_blocked(db))
            return real_archive(*args, **kwargs)

        score_item.score_opportunity = spying_call
        radar_common.archive_score = spying_archive
        try:
            with captured_stderr():
                fixed, failed = rescore._rescore_rows(conn, CFG, {}, picked)
        finally:
            score_item.score_opportunity = real
            radar_common.archive_score = real_archive
        check(fixed == 1 and in_call == [False] and in_write == [True]
              and not conn.in_transaction and not writer_is_blocked(db),
              "the re-score holds no lock while the model answers and holds "
              "it from the second look to the commit "
              f"(call {in_call}, write {in_write})")

        # The conditions on the statements, with the Python checks blinded.
        conn.execute("DELETE FROM score_history")
        conn.execute("DELETE FROM opportunities")
        conn.commit()
        ap = role(conn, QD, applied_at=days_ago(2), **unscored)
        ct = role(conn, RD, **unscored)
        with blind_to_applied(), captured_stderr():
            fixed, failed = rescore.rescore_opportunities(conn, CFG, {})
        check(fixed == 1 and row_of(conn, ct)["tier"] == "top"
              and row_of(conn, ap)["tier"] is None,
              "the unscored-role selection leaves the applied role out by its "
              "own condition, with the per-row check blinded")
        # A pass with room for one row must not spend it on an applied role.
        conn.execute("DELETE FROM opportunities")
        conn.commit()
        ap = role(conn, QD, applied_at=days_ago(2), combined=90, tier="reading",
                  cv_version="cv-old.md")
        ct = role(conn, RD, combined=50, tier="reading", cv_version="cv-old.md")
        with captured_stderr():
            fixed, failed, remaining = rescore.rescore_stale_cv(conn, CFG, {}, 1)
        check(fixed == 1 and remaining == 0
              and row_of(conn, ct)["cv_version"] != "cv-old.md"
              and row_of(conn, ap)["cv_version"] == "cv-old.md",
              "a stale-CV pass with room for one row spends it on the role "
              "that is not applied, though the applied one scores higher")
        conn.close()


# ------------------------------------------------------------------ backfill

def backfill_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "backfill.sqlite"
        conn = radar_common.get_db(db)
        unbanded = dict(rate_band=None, salary_rate="£950 per day")
        ax = role(conn, QD, applied_at=days_ago(3), **unbanded)
        ar = role(conn, QD, **unbanded)
        cx = role(conn, RD, **unbanded)
        conn.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), captured_stderr():
            code = backfill_pay.main(["--mock", "--db", str(db)])
        summary = json.loads(out.getvalue().strip().splitlines()[-1])
        conn = radar_common.get_db(db)
        check(code == 0 and summary["examined"] == 1 and summary["remaining"] == 0
              and row_of(conn, cx)["rate_band"] is not None,
              "the pay backfill bands the role that is not applied and "
              f"reports nothing remaining ({summary})")
        check(row_of(conn, ax)["rate_band"] is None
              and row_of(conn, ar)["rate_band"] is None,
              "and leaves the applied chain unbanded, which is why remaining "
              "must not count it")
        # Applied for while the model is answering: the write has its own
        # condition. Two fresh unbanded roles, one is applied for mid-pass.
        conn.execute("DELETE FROM opportunities")
        conn.commit()
        late = role(conn, QD, **unbanded)
        conn.close()
        real_call = score_item._call_for_json

        def applied_while_answering(*args, **kwargs):
            out = real_call(*args, **kwargs)
            raw = sqlite3.connect(db)
            raw.execute("UPDATE opportunities SET applied_at = ? WHERE id = ?",
                        (days_ago(0), late))
            raw.commit()
            raw.close()
            return out

        score_item._call_for_json = applied_while_answering
        try:
            with captured_stdout() as printed, captured_stderr():
                backfill_pay.main(["--mock", "--db", str(db)])
        finally:
            score_item._call_for_json = real_call
        summary = json.loads(printed.getvalue().strip().splitlines()[-1])
        conn = radar_common.get_db(db)
        check(row_of(conn, late)["rate_band"] is None
              and row_of(conn, late)["pay_min"] is None,
              "a role applied for while the model was answering is not "
              "written to, the update has its own condition")
        logged = conn.execute("SELECT items_in, items_new FROM runs WHERE"
                              " workflow = 'backfill' ORDER BY id DESC"
                              " LIMIT 1").fetchone()
        check(summary["examined"] == 1 and summary["banded"] == 0
              and summary["unstated"] == 0 and summary["remaining"] == 0
              and tuple(logged) == (1, 0),
              "and it is not counted as banded, in the summary or in the "
              f"run log ({summary}, logged {tuple(logged)})")
        conn.close()


# -------------------------------------------------------------------- digest

def digest_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = fresh(tmp)
        fresh_s, stale_s = real_days_ago(0.2), real_days_ago(60)
        scored = dict(first_seen=fresh_s, last_seen=fresh_s)

        def pair(who, **cols):
            """(an applied row's twin pair): returns (applied chain id, control id)."""
            a = role(conn, who, applied_at=real_days_ago(1), **{**scored, **cols})
            r = role(conn, who, **{**scored, **cols})           # repost of it
            return a, r

        # inbound
        a_in, r_in = pair(QD)
        c_in = role(conn, RD, **scored)
        # table signals that are opportunities (thread_type signal)
        a_sig, r_sig = pair(("Medical Director", "Kordis Medical"),
                            thread_type="signal")
        c_sig = role(conn, ("Medical Director", "Mentis Medical"),
                     thread_type="signal", **scored)
        # reading count
        a_rd, r_rd = pair(("Clinical Lead", "Novem Clinical"), tier="reading")
        c_rd = role(conn, ("Clinical Lead", "Ploom Clinical"), tier="reading",
                    **scored)
        # needs review: ungated
        ungated = dict(tier=None, cv_match=None, gate_sector=None,
                       gate_cv=None, gate_location=None, gate_rate=None,
                       one_line_why=None, failed_gates=None)
        a_nr, r_nr = pair(("Auditor", "Kordis Audit"), **ungated)
        c_nr = role(conn, ("Auditor", "Mentis Audit"), **ungated, **scored)
        # ageing: first seen two months ago, unknown, Top
        old = dict(first_seen=stale_s, last_seen=stale_s)
        a_ag = role(conn, ("Analyst", "Veltrix Analytics"),
                    applied_at=real_days_ago(30), status="digested", **old)
        r_ag = role(conn, ("Analyst", "Veltrix Analytics"), status="digested",
                    **old)
        c_ag = role(conn, ("Analyst", "Orbase Analytics"), status="digested",
                    **old)

        d = build_digest.collect(conn, CFG)
        inbound = {o["id"] for o in d["inbound"]}
        check(c_in in inbound and not ({a_in, r_in} & inbound),
              "the digest's new prospects hold the role that is not applied "
              "and neither advert of the one that is")
        signals = {s["id"] for s in d["signals"] if s.get("kind") == "opportunity"}
        check(c_sig in signals and not ({a_sig, r_sig} & signals),
              "its signal-type prospects too")
        check(d["reading_count"] == 1,
              f"its Reading count is one, not three ({d['reading_count']})")
        review = {(n["label"], n["company"]) for n in d["needs_review"]}
        check(("Auditor", "Mentis Audit") in review
              and not any(c == "Kordis Audit" for _l, c in review),
              "its Needs review list holds the ungated role that is not "
              "applied and not the applied chain")
        ageing = {a["id"] for a in d["ageing"] if a["kind"] == "opportunity"}
        check(c_ag in ageing and not ({a_ag, r_ag} & ageing),
              "its Ageing list holds the quiet role that is not applied and "
              "not the quiet applied one")
        conn.execute("UPDATE opportunities SET applied_at = NULL")
        conn.commit()
        d2 = build_digest.collect(conn, CFG)
        check({a_in, r_in, c_in} <= {o["id"] for o in d2["inbound"]}
              and d2["reading_count"] == 3
              and {a_ag, r_ag, c_ag} <= {a["id"] for a in d2["ageing"]
                                         if a["kind"] == "opportunity"},
              "undo every application and all of them come back, so the "
              "exemption was what kept them out")
        conn.close()


# --------------------------------------------------------------------- touch

def touch_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "touch.sqlite"
        conn = radar_common.get_db(db)
        ax = role(conn, QD, applied_at=days_ago(3))
        ar = role(conn, QD)
        same_firm = role(conn, ("Head of Quality", QD[1]))
        conn.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = touch.main(["--db", str(db), "mark", QD[1], "--as", "actioned"])
        conn = radar_common.get_db(db)
        check(code == 0 and row_of(conn, same_firm)["acknowledged_at"]
              and row_of(conn, ax)["acknowledged_at"] is None
              and row_of(conn, ar)["acknowledged_at"] is None
              and "Marked applied, left alone" in out.getvalue(),
              "marking a company seen acknowledges its other roles and "
              "leaves the applied role and its repost alone, and says so")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            touch.main(["--db", str(db), "mark", "--as", "actioned",
                        "--opportunity", str(ar)])
        check(row_of(conn, ar)["acknowledged_at"] is None
              and "Marked applied, left alone" in out.getvalue(),
              "and so does marking the repost by its id")
        # The statement's own condition, with the Python check blinded.
        conn.execute("UPDATE opportunities SET acknowledged_at = NULL")
        conn.commit()
        conn.close()
        with blind_to_applied(), contextlib.redirect_stdout(io.StringIO()):
            touch.main(["--db", str(db), "mark", QD[1], "--as", "actioned"])
        conn = radar_common.get_db(db)
        check(row_of(conn, ax)["acknowledged_at"] is None
              and row_of(conn, ar)["acknowledged_at"] is None
              and row_of(conn, same_firm)["acknowledged_at"],
              "with the Python check blinded the UPDATE's own condition still "
              "leaves the applied role and its repost alone")
        conn.close()


# --------------------------------------------------------------------- pages

def page_checks() -> None:
    # One clock for the section. The rows are dated from it and the page is
    # rendered with the dashboard's own clock held at it, so a run that
    # crosses midnight UTC cannot make the labels disagree with the rows.
    now = datetime.now(timezone.utc)

    def at(n: float) -> str:
        return iso(now - timedelta(days=n))

    def jobs_html_now(path) -> str:
        with clock_at(now):
            return jobs_html(path)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "pages.sqlite"
        conn = radar_common.get_db(db)
        long_ago = at(45)
        # Newest application first: three applied roles, applied on different days.
        a1 = role(conn, QD, applied_at=at(10), source="reed-alert",
                  source_url="https://www.reed.co.uk/jobs/qd/1", first_seen=long_ago,
                  last_seen=long_ago)
        a2 = role(conn, ("Head of QA", "Kordis Medical"),
                  applied_at=at(1), source="linkedin-alert",
                  source_url="https://www.linkedin.com/jobs/view/123456/")
        a3 = role(conn, ("Auditor <b>", "Mentis & Co"),
                  applied_at=at(0), source="email-other",
                  source_url="javascript:alert(1)")
        o1 = role(conn, RD)
        o2 = role(conn, ("Clinical Lead", "Novem Clinical"), tier="question",
                  question_text="Is the rate negotiable?")
        o3 = role(conn, ("Safety Officer", "Ploom Medical"), tier="reading")
        conn.close()

        jobs = jobs_html_now(db)
        tiers, applied = split_applied(jobs)
        check(applied.startswith("<details") and " open" not in applied.split(">")[0],
              "the Applied section is a folded details element")
        check(jobs.index("<h2>Applied</h2>") > jobs.index("Reading")
              and jobs.rfind("<h2>", 0, jobs.index("<h2>Applied</h2>")) > 0
              and jobs.index("<h2>Applied</h2>") > jobs.index("One question away")
              and jobs.index("<h2>Applied</h2>") > jobs.index("Top prospects"),
              "and it sits below the open tiers")
        check('<span class="section-count">3</span>' in applied,
              "its count is three, one per role")
        check(row_titles(applied) == ["Auditor &lt;b&gt;", "Head of QA", QD[0]],
              "it lists the roles newest application first, titles escaped")
        check("Kordis Medical" in applied and "Mentis &amp; Co" in applied
              and QD[1] in applied,
              "with the company on each row, escaped")
        check(f"Applied {build_dashboard.fmt_day(at(10))}, "
              "10 days ago" in applied,
              "each row says the day it was applied and the days since")
        check(f"Applied {build_dashboard.fmt_day(at(1))}, "
              "yesterday" in applied
              and f"Applied {build_dashboard.fmt_day(at(0))}, "
              "today" in applied,
              "the day before reads yesterday and the same day reads today")
        check("Via Reed alert." in applied and "Via LinkedIn alert." in applied
              and "Via email alert." in applied,
              "the source each advert came through, by its name")
        check('href="https://www.reed.co.uk/jobs/qd/1"' in applied
              and 'href="https://www.linkedin.com/jobs/view/123456/"' in applied
              and "javascript:" not in applied,
              "a link to each advert, and a link that is not http or https is "
              "dropped")
        check(applied.count("View the advert") == 2,
              "the two that have a safe link say View the advert")
        check(all(f'data-unapply="{i}"' in applied for i in (a1, a2, a3))
              and "data-apply=" not in applied,
              "each row has an Undo button and no Applied button")
        check('aria-label="Undo applied for Head of QA"' in applied
              and 'aria-label="Undo applied for Auditor &lt;b&gt;"' in applied,
              "and each Undo button names its role for a screen reader, escaped")

        # The open tiers.
        check(all(f'data-apply="{i}"' in tiers for i in (o1, o2, o3)),
              "every open row on the Jobs page has an Applied button")
        check(not any(f'data-apply="{i}"' in tiers for i in (a1, a2, a3)),
              "and no applied role is in the tiers to have one")
        check("Applied, sent it" in tiers,
              "labelled as the other verdicts are")
        # Beside Acknowledge, Closed and Dismiss.
        one = tiers.split(f'data-apply="{o1}"')[1].split("</button>")[0]
        order = [tiers.index(f'data-ack="{o1}"'), tiers.index(f'data-apply="{o1}"'),
                 tiers.index(f'data-close="{o1}"'), tiers.index(f'data-dismiss="{o1}"')]
        check(order == sorted(order) and "Applied" in one,
              "the button sits among Acknowledge, Closed and Dismiss in the "
              "expanded row")

        home = home_html(db)
        check("data-apply=" not in home and "<h2>Applied</h2>" not in home,
              "the Home page has no Applied button and no Applied section")
        card = re.search(r"(\d+) roles? in play, (\d+) new this week", home)
        check(card and int(card.group(1)) == 3,
              "its Jobs card counts the three open roles and none of the "
              f"three applied ({card.group(0) if card else None})")
        archive = serve_dashboard_render(db, "archive")
        check("data-apply=" not in archive and "<h2>Applied</h2>" not in archive
              and "Head of QA" not in archive,
              "the full archive page has no Applied button and does not list "
              "the applied roles among the open ones")
        check("apart from the roles you have applied for, which are listed on "
              "Jobs" in archive and "every row on file but the applied ones"
              in home,
              "the archive's masthead and Home's link to it say that applied "
              "roles are on Jobs, so neither promises every row")
        insights = serve_dashboard_render(db, "insights")
        check("<h2>Applied</h2>" not in insights, "the Insights page is untouched")

        # A chain that carries two stamps, which the buttons cannot make but
        # two stale tabs or a hand-edited row can: one entry, on the first
        # stamp, and one Undo clears them both.
        two = Path(tmp) / "two.sqlite"
        c3 = radar_common.get_db(two)
        s1 = role(c3, QD, applied_at=at(9))
        s2 = role(c3, QD, applied_at=at(2))
        c3.close()
        _t, a_two = split_applied(jobs_html_now(two))
        check(a_two.count('class="applied-row"') == 1
              and f'data-unapply="{s1}"' in a_two
              and f"Applied {build_dashboard.fmt_day(at(9))}, 9 days ago"
              in a_two,
              "a chain with two stamps is one entry, on the earlier one")
        with pointed_at(two):
            check(serve_dashboard.set_applied(s2, False)["ok"], "Undo on it works")
        c3 = reader(two)
        check(row_of(c3, s1)["applied_at"] is None
              and row_of(c3, s2)["applied_at"] is None,
              "and clears both stamps")
        c3.close()

        # An empty list still shows the section, folded, with its count.
        empty = Path(tmp) / "empty.sqlite"
        c2 = radar_common.get_db(empty)
        role(c2, RD)
        c2.close()
        e_tiers, e_applied = split_applied(jobs_html_now(empty))
        check('<span class="section-count">0</span>' in e_applied
              and "No applications recorded yet." in e_applied
              and "details" in e_applied.split(">")[0],
              "with nothing applied for the section is still there, folded, "
              "at zero")

        # Applied roles with no role key, no company or no title, which is
        # how a thin email alert arrives. Each is a role of its own, none of
        # them takes the page down, and the words that stand in are plain.
        thin = Path(tmp) / "thin.sqlite"
        c4 = radar_common.get_db(thin)
        k1 = seed(c4, title="Lone title", company=None, role_key=None,
                  tier="top", applied_at=at(3), source=None, source_url=None)
        k2 = seed(c4, title=None, company="Anon Ltd", role_key=None,
                  tier="top", applied_at=at(4), source="email-other",
                  source_url="https://example.com/jobs/9")
        k3 = seed(c4, title=None, company=None, role_key=None, tier="top",
                  applied_at=at(5))
        c4.close()
        _t, a_thin = split_applied(jobs_html_now(thin))
        check(a_thin.count('class="applied-row"') == 3
              and '<span class="section-count">3</span>' in a_thin
              and all(f'data-unapply="{i}"' in a_thin for i in (k1, k2, k3)),
              "applied roles with no role key are three entries and not one, "
              "each with its own Undo")
        check(row_titles(a_thin) == ["Lone title", "Untitled role",
                                     "Untitled role"]
              and a_thin.count("Unknown company.") == 2
              and "Anon Ltd." in a_thin and "None" not in a_thin,
              "a missing title reads Untitled role, a missing company reads "
              "Unknown company, and nothing reads None")
        check(a_thin.count("View the advert") == 1
              and a_thin.count('aria-label="Undo applied for Untitled role"')
              == 2 and "Via unknown source." in a_thin,
              "a missing link means no link, a missing source says so")


def applied_days_checks() -> None:
    days = build_dashboard.applied_days
    check(days("2026-11-11T23:50:00Z", "2026-11-12T00:10:00Z") == 1
          and days("2026-11-12T00:10:00Z", "2026-11-12T23:50:00Z") == 0,
          "days since applying are calendar days: twenty minutes across "
          "midnight is a day, twenty-three hours inside one date is none")
    check(days("2026-10-31T10:00:00Z", "2026-11-02T00:01:00Z") == 2
          and days("2026-02-28T23:59:59Z", "2026-03-01T00:00:01Z") == 1,
          "and they count across a month end")
    check(days("2026-11-13T01:00:00Z", "2026-11-12T12:00:00Z") == 0,
          "a stamp in the future, from a clock set wrong or a hand-edited "
          "row, is today and never a negative number")
    check(days("garbage", "2026-11-12T00:00:00Z") is None
          and days(None, "2026-11-12T00:00:00Z") is None
          and days("", "2026-11-12T00:00:00Z") is None,
          "an unreadable stamp has no day count")
    row = {"id": 1, "title": "T", "company": "C", "source": "linkedin-alert",
           "source_url": None}
    yesterday = build_dashboard.applied_row(
        {**row, "applied_at": "2026-11-11T23:50:00Z"}, "2026-11-12T00:10:00Z")
    check("Applied 11 Nov, yesterday." in yesterday,
          "the row says yesterday for the evening before, the clock is not "
          "what it counts")
    future = build_dashboard.applied_row(
        {**row, "applied_at": "2026-11-13T01:00:00Z"}, "2026-11-12T12:00:00Z")
    check("Applied 13 Nov, today." in future,
          "and today for a stamp that is ahead of the clock")
    stop = build_dashboard.applied_row(
        {**row, "company": "Acme Inc.", "applied_at": "2026-11-11T09:00:00Z"},
        "2026-11-12T12:00:00Z")
    check("Acme Inc. Applied 11 Nov, yesterday." in stop and ".." not in stop,
          "a company name that ends in a full stop does not get a second one")
    junk = build_dashboard.applied_row(
        {**row, "applied_at": "garbage & <b>x</b>"}, "2026-11-12T12:00:00Z")
    check("garbage &amp; &lt;b&gt;x&lt;/b&gt;" in junk and "&amp;amp;" not in junk
          and "<b>x" not in junk and "an unknown while ago" in junk,
          "a stamp that is not a date is shown as text, escaped once, and "
          "says the age is unknown")


def home_checks() -> None:
    """What Home counts and what it leaves out, with a role applied for."""
    now = datetime.now(timezone.utc)

    def at(n: float) -> str:
        return iso(now - timedelta(days=n))

    unscored = dict(tier=None, cv_match=None, gate_sector=None, gate_cv=None,
                    gate_location=None, gate_rate=None, one_line_why=None,
                    failed_gates=None)
    old = at(45)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "home.sqlite"
        conn = radar_common.get_db(db)
        ap = role(conn, QD, applied_at=at(5), first_seen=old, last_seen=old)
        rp = role(conn, QD, first_seen=at(0.2), last_seen=at(0.2))
        sib = role(conn, QD, acknowledged_at=at(20), first_seen=at(40),
                   last_seen=at(40))
        quiet = role(conn, RD, first_seen=old, last_seen=old)
        ack_other = role(conn, ("Medical Director", "Kordis Medical"),
                         acknowledged_at=at(2), first_seen=at(3),
                         last_seen=at(3))
        ap_un = role(conn, ("Unscored One", "Firm Z"), applied_at=at(2),
                     **unscored)
        open_un = role(conn, ("Unscored Two", "Firm Y"), **unscored)
        conn.close()

        def seen():
            look = reader(db)
            try:
                return build_dashboard.collect(look, CFG)
            finally:
                look.close()

        data = seen()
        check({a["id"] for a in data["ageing"]} == {quiet}
              and data["ageing_opp_ids"] == {quiet},
              "the Ageing list holds the quiet role that is not applied and "
              "not the applied one that has been quiet as long")
        check({o["id"] for o in data["acknowledged"]} == {ack_other},
              "the acknowledged list holds the acknowledged role that is "
              "not applied and not the acknowledged advert in the applied "
              "chain")
        check({o["id"] for o in data["opportunities"]} == {quiet, open_un, },
              "the open list holds the two roles that are not applied")
        line = build_dashboard.standing_line(data)
        check(line.startswith("<p>Two roles and no signals on file."),
              f"the standing line counts those two ({line[:60]})")
        weeks = sum(c for _, c in data["weekly_prospects"])
        check(weeks == 2,
              "the prospect trend counts the quiet role and the acknowledged "
              "one, and not the applied role, its repost or its "
              f"acknowledged advert ({weeks})")

        home = home_html(db)
        ageing = re.search(r'<span class="num">(\d+)</span>unknown for over a '
                           r'month', home)
        review = re.search(r'<span class="num">(\d+)</span>roles? unscored',
                           home)
        check(ageing and ageing.group(1) == "1" and review
              and review.group(1) == "1",
              "Home's attention list says one role unknown for over a month "
              "and one unscored, the applied ones are in neither "
              f"({ageing.group(0) if ageing else None}, "
              f"{review.group(0) if review else None})")

        # The control: take the application back and every one of them is
        # counted again, so it was the marker that kept them out.
        with pointed_at(db):
            check(serve_dashboard.set_applied(ap, False)["ok"]
                  and serve_dashboard.set_applied(ap_un, False)["ok"],
                  "Undo on both applied roles works")
        data = seen()
        check({a["id"] for a in data["ageing"]} == {ap, quiet},
              "undo it and the role is in Ageing again")
        check(sum(c for _, c in data["weekly_prospects"]) == 5
              and {o["id"] for o in data["opportunities"]}
              >= {ap, rp, quiet, open_un, ap_un},
              "and in the open list and the prospect trend, with its repost")

        # Arrivals are another matter. A board's light says whether its mail
        # arrives, and mail about a role already applied for is still mail.
        # The flow chart counts arrivals too. Only the prospect trend, which
        # measures what the digest would send, leaves the role out.
        built = {}
        for label, stamp in (("plain", None), ("applied", at(0.1))):
            path = Path(tmp) / f"board-{label}.sqlite"
            c = radar_common.get_db(path)
            role(c, QD, first_seen=at(0.2), last_seen=at(0.2),
                 applied_at=stamp)
            c.close()
            html = home_html(path)
            start = html.index("<h4>Board freshness")
            look = reader(path)
            built[label] = (html[start:html.index("</ul></div>", start)],
                            build_dashboard.collect(look, CFG))
            look.close()
        (p_board, p_data), (a_board, a_data) = built["plain"], built["applied"]
        check("flowing" in p_board and p_board == a_board,
              "the board freshness lights read the same with the only "
              "arrival on the board applied for")
        check(p_data["daily_arrivals"] == a_data["daily_arrivals"]
              and sum(c for _, c in a_data["daily_arrivals"]) == 1,
              "and so does the flow chart, an arrival is an arrival")
        check(sum(p_data["month_prospects"].values()) == 1
              and sum(a_data["month_prospects"].values()) == 0
              and sum(c for _, c in p_data["weekly_prospects"]) == 1
              and sum(c for _, c in a_data["weekly_prospects"]) == 0,
              "while the prospect trend, week and month, drops the applied "
              "role, as the digest does")


def serve_dashboard_render(db, name) -> str:
    with pointed_at(db):
        return serve_dashboard.render(name).decode("utf-8")


def nothing_else_changes_checks() -> None:
    """The Jobs page with a role applied reads the same as the Jobs page
    without that role, apart from the Applied section and the clock."""
    def squash(html: str) -> str:
        # The masthead names the database file and the minute it was made.
        return re.sub(r"Generated [^<]*?\.sqlite\.", "Generated T from DB.", html)

    with tempfile.TemporaryDirectory() as tmp:
        def build(name, applied):
            path = Path(tmp) / name
            conn = radar_common.get_db(path)
            # The same open roles in the same order in every database.
            role(conn, RD, first_seen=real_days_ago(4), last_seen=real_days_ago(4),
                 source_url="https://www.linkedin.com/jobs/view/7000001/")
            role(conn, ("Clinical Lead", "Novem Clinical"), tier="question",
                 question_text="Is the rate negotiable?",
                 first_seen=real_days_ago(3), last_seen=real_days_ago(3),
                 source_url="https://www.linkedin.com/jobs/view/7000002/")
            role(conn, ("Safety Officer", "Ploom Medical"), tier="reading",
                 first_seen=real_days_ago(2), last_seen=real_days_ago(2),
                 source_url="https://www.linkedin.com/jobs/view/7000003/")
            if applied == "applied":
                role(conn, QD, applied_at=real_days_ago(1),
                     source_url="https://www.linkedin.com/jobs/view/7000004/")
                role(conn, QD, source_url="https://www.linkedin.com/jobs/view/7000005/")
            conn.close()
            return path

        plain = build("plain.sqlite", "plain")
        with_applied = build("with.sqlite", "applied")
        t_plain, a_plain = split_applied(jobs_html(plain))
        t_with, a_with = split_applied(jobs_html(with_applied))
        check(squash(t_plain) == squash(t_with),
              "with a role applied for, and its repost, the open tiers and "
              "everything else on the Jobs page are byte for byte the page "
              "without that role")
        check(row_titles(a_with) == [QD[0]] and row_titles(a_plain) == [],
              "and only the Applied section differs")


# ------------------------------------------------------------- the static scan

# Statements that pick open rows by a lifecycle stamp, or write to the
# opportunities table at all, and are right to leave applied roles in. Keyed on
# file, function and a phrase from the statement, with the reason. Everything
# else must carry NOT_APPLIED_SQL.
EXCEPTIONS = {
    # ---- the dashboard's own buttons
    ("serve_dashboard.py", "set_acknowledged", "acknowledged_at = NULL"):
        "Undo clears a stamp, and clearing one on an applied role is harmless",
    ("serve_dashboard.py", "set_lifecycle", "closed_at = NULL"):
        "Unclose reopens a retired row, the chain's applied marker still holds",
    ("serve_dashboard.py", "set_lifecycle", "dismissed_at = NULL"):
        "Undismiss reopens a retired row, the chain's applied marker still holds",
    ("serve_dashboard.py", "set_applied", "applied_at = NULL"):
        "Undo is the one thing that clears the marker",
    # ---- writers that take the write lock and look again before they write,
    # which is stronger than a condition on the statement
    ("check_jobs.py", "apply_page", "for c in sets"):
        "runs after the write lock is taken and the role looked at again, "
        "see apply_page_checks",
    ("check_jobs.py", "apply_page", "SET engagement_type"):
        "the same transaction as the statement above, after the same look",
    ("regate_rates.py", "regate", "for c in _GATE_COLS"):
        "the write lock is taken before the rows are chosen, so what was "
        "chosen is what is written, see regate_checks",
    ("rescore.py", "_rescore_rows", "SET cv_match"):
        "the write lock is taken and the role looked at again after the "
        "model answers and before this runs, see rescore_checks",
    # ---- bookkeeping that is true whoever has applied for the role
    ("build_digest.py", "commit_token", "status = 'digested'"):
        "marks the rows a digest that went out already carried, and applied "
        "roles are not in the digest",
    ("process_email.py", "main", "seen_count = seen_count + 1"):
        "records that the advert was seen again, which is true of an applied "
        "role too",
    ("process_email.py", "main", "expired_at = NULL"):
        "a sighting revives an expired row, and an applied chain is never "
        "expired, so there is nothing to protect",
    ("engagement.py", "backfill", "engagement_type IS NULL"):
        "reads contract or permanent off the advert's own words, no model, no "
        "tier, nothing that moves a role",
    ("enrich_company.py", "enrich_company", "SET region"):
        "a region cached per company, no tier or lifecycle effect",
    ("radar_common.py", "recompute_regions", "SET region"):
        "a region cached per company, no tier or lifecycle effect",
    # ---- the one-time migrations, each behind its own flag
    ("radar_common.py", "_migrate", "status IN ('actioned','dead')"):
        "the one-time mapping of the old retirement statuses, behind its flag",
    ("radar_common.py", "_migrate", "last_seen = first_seen"):
        "fills a missing first sighting date, once",
    ("radar_common.py", "_migrate", "score_basis = ?"):
        "labels scores made before the record existed, once",
    ("radar_common.py", "_migrate", "SET role_key = ?"):
        "gives a row its role key when it has none, the same key every time",
    # ---- picks that only list rows
    ("touch.py", "flip_opportunity", "SELECT id, company, title, acknowledged_at"):
        "lists the rows to mark, and the loop that follows leaves an applied "
        "role alone with a line that says so",
    ("build_dashboard.py", "render_company_page", "acknowledged_at"):
        "the company page is the history of everything on file, applied or not",
}
LIFECYCLE = ("acknowledged_at", "closed_at", "dismissed_at", "expired_at")
WRITES = re.compile(r"\b(UPDATE|DELETE FROM)\s+opportunities\b")


def sql_statements(path: Path):
    """Each maximal string expression in a statement that selects rows from
    the opportunities table by a lifecycle stamp, or updates or deletes rows
    in it. A statement put together out of variables, with no string naming
    the table in one piece, is beyond what a scan of the syntax can see."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}

    def textual(n) -> bool:
        return (isinstance(n, ast.JoinedStr)
                or (isinstance(n, ast.Constant) and isinstance(n.value, str))
                or (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add)
                    and (textual(n.left) or textual(n.right))))

    def enclosing(n) -> str:
        best, best_line = "<module>", -1
        for f in ast.walk(tree):
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if f.lineno <= n.lineno <= f.end_lineno and f.lineno > best_line:
                    best, best_line = f.name, f.lineno
        return best

    for n in ast.walk(tree):
        if not textual(n):
            continue
        parent = parents.get(n)
        if isinstance(parent, ast.BinOp) and textual(parent):
            continue
        text = ast.unparse(n)
        picks = ("opportunities" in text
                 and re.search(r"\bSELECT\b", text)
                 and any(col in text for col in LIFECYCLE))
        if WRITES.search(text) or picks:
            yield path.name, enclosing(n), n.lineno, text


def static_scan_checks() -> None:
    sites = []
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        sites.extend(sql_statements(path))
    check(len(sites) >= 40,
          f"the scan sees the statements that pick open rows or write to the "
          f"table ({len(sites)} of them across the scripts)")
    used = set()
    unexplained = []
    for fname, func, line, text in sites:
        if "NOT_APPLIED_SQL" in text:
            continue
        key = next((k for k in EXCEPTIONS
                    if k[0] == fname and k[1] == func and k[2] in text), None)
        if key:
            used.add(key)
        else:
            unexplained.append(f"{fname}:{line} {func}")
    check(not unexplained,
          "every statement that picks open rows or writes to the table leaves "
          "applied roles out or is named, with its reason, in the exceptions "
          f"(unexplained: {unexplained})")
    check(used == set(EXCEPTIONS),
          "and every exception still matches a statement "
          f"(stale: {sorted(set(EXCEPTIONS) - used)})")
    wired = {(f, fn) for f, fn, _l, t in sites if "NOT_APPLIED_SQL" in t}
    needed = {("check_jobs.py", "due_candidates"), ("check_jobs.py", "run_checks"),
              ("check_jobs.py", "expire_stale"), ("regate_rates.py", "regate"),
              ("rescore.py", "rescore_opportunities"),
              ("rescore.py", "regate_backlog"), ("rescore.py", "rescore_stale_cv"),
              ("backfill_pay.py", "main"), ("build_digest.py", "collect"),
              ("serve_dashboard.py", "set_acknowledged"),
              ("serve_dashboard.py", "set_lifecycle"),
              ("serve_dashboard.py", "set_applied")}
    check(needed <= wired,
          "and the scan finds the condition in every function the brief "
          f"names (missing: {sorted(needed - wired)})")


# The read-only queries handed to the box, kept here so that what is pasted
# into a terminal is what this file ran. Each is one statement for the sqlite3
# command line, with no double quote, dollar sign, backtick, backslash or
# exclamation mark in it, so it survives a bash double-quoted string.
APPLIED_RULE = ("(applied_at IS NOT NULL OR (role_key IS NOT NULL AND role_key IN"
                " (SELECT role_key FROM opportunities"
                " WHERE applied_at IS NOT NULL AND role_key IS NOT NULL)))")
BOX_QUERIES = {
    "the file is sound": "PRAGMA integrity_check;",
    "the two columns are there (expect 2)":
        "SELECT COUNT(*) AS new_columns FROM pragma_table_info('opportunities')"
        " WHERE name IN ('applied_at','applied_cv');",
    "how many are applied":
        "SELECT (SELECT COUNT(*) FROM opportunities WHERE applied_at IS NOT NULL)"
        " AS stamped_rows,"
        " (SELECT COUNT(DISTINCT COALESCE(role_key, 'row-' || id))"
        " FROM opportunities WHERE applied_at IS NOT NULL) AS applied_roles,"
        " (SELECT COUNT(*) FROM opportunities WHERE " + APPLIED_RULE
        + ") AS rows_on_applied_roles;",
    "which, newest first":
        "SELECT id, company, title, applied_at,"
        " CAST(julianday(date('now')) - julianday(date(applied_at)) AS INTEGER)"
        " AS days, applied_cv FROM opportunities WHERE applied_at IS NOT NULL"
        " ORDER BY applied_at DESC, id DESC;",
    "no stamped row is expired, closed or dismissed (expect 0)":
        "SELECT COUNT(*) AS stamped_rows_retired FROM opportunities"
        " WHERE applied_at IS NOT NULL AND (expired_at IS NOT NULL"
        " OR closed_at IS NOT NULL OR dismissed_at IS NOT NULL);",
    "no row of an applied role was expired, closed or dismissed since the"
    " day it was applied (expect 0)":
        "WITH chain AS (SELECT role_key, MIN(applied_at) AS first_applied"
        " FROM opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT"
        " NULL GROUP BY role_key) SELECT COUNT(*) AS retired_since_applying"
        " FROM opportunities o LEFT JOIN chain c ON c.role_key = o.role_key"
        " WHERE COALESCE(c.first_applied, o.applied_at) IS NOT NULL AND"
        " (o.expired_at >= COALESCE(c.first_applied, o.applied_at)"
        " OR o.closed_at >= COALESCE(c.first_applied, o.applied_at)"
        " OR o.dismissed_at >= COALESCE(c.first_applied, o.applied_at));",
    "no applied row has had its score replaced since (expect 0)":
        "SELECT COUNT(*) AS scores_replaced_since_applying FROM score_history h"
        " JOIN opportunities o ON o.id = h.opportunity_id WHERE"
        " o.applied_at IS NOT NULL AND h.event = 'replaced'"
        " AND h.recorded_at >= o.applied_at;",
}


def box_query_checks() -> None:
    check(APPLIED_RULE == radar_common.APPLIED_SQL,
          "the rule written into the box queries is the rule the code uses")
    check(all(not any(ch in q for ch in ('"', "$", "`", "\\", "!"))
              for q in BOX_QUERIES.values()),
          "none of the box queries holds a character the shell would read: a "
          "double quote, dollar sign, backtick, backslash or exclamation mark")
    names = list(BOX_QUERIES)
    sql = {n: BOX_QUERIES[n] for n in names}

    def ask(conn, label_start):
        q = next(v for k, v in sql.items() if k.startswith(label_start))
        return [tuple(r) for r in conn.execute(q).fetchall()]

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "box.sqlite"
        conn = radar_common.get_db(db)
        a = role(conn, QD, first_seen=days_ago(30), last_seen=days_ago(30))
        a2 = role(conn, QD)                                        # a repost
        b = role(conn, RD)
        old_sibling = role(conn, QD, closed_at=days_ago(40),
                           closed_source="page_check")             # before applying
        lone = seed(conn, title=None, company="Anon", role_key=None, tier="top")
        conn.close()
        with pointed_at(db):
            check(serve_dashboard.set_applied(a, True, "cv-a.pdf")["ok"]
                  and serve_dashboard.set_applied(lone, True)["ok"],
                  "two roles are applied through the route")
        conn = reader(db)
        check(ask(conn, "the file is sound") == [("ok",)]
              and ask(conn, "the two columns") == [(2,)],
              "the integrity and column queries answer ok and 2")
        counts = ask(conn, "how many")[0]
        check(counts == (2, 2, 4),
              "the count query says two stamped rows, two roles, and four "
              f"rows on applied roles including the repost and the old "
              f"sibling ({counts})")
        listing = ask(conn, "which")
        check([r[0] for r in listing] == [lone, a] and listing[1][5] == "cv-a.pdf"
              and all(isinstance(r[4], int) for r in listing),
              "the listing is newest first with the days and the CV reference")
        check(ask(conn, "no stamped row") == [(0,)]
              and ask(conn, "no row of an applied role") == [(0,)]
              and ask(conn, "no applied row has had") == [(0,)],
              "after applying, all three proofs read 0, although the chain "
              "holds a sibling closed before the application")
        conn.close()

        # The same queries catch the harm they exist to catch.
        raw = sqlite3.connect(db)
        raw.execute("UPDATE opportunities SET expired_at = ? WHERE id = ?",
                    (iso(datetime.now(timezone.utc)), a2))
        raw.commit()
        raw.close()
        conn = reader(db)
        check(ask(conn, "no stamped row") == [(0,)]
              and ask(conn, "no row of an applied role") == [(1,)],
              "a repost expired after the application is caught by the chain "
              "proof, which the stamped-row proof alone would miss")
        conn.close()
        raw = sqlite3.connect(db)
        raw.execute("UPDATE opportunities SET closed_at = ? WHERE id = ?",
                    (iso(datetime.now(timezone.utc)), lone))
        raw.commit()
        raw.close()
        conn = reader(db)
        check(ask(conn, "no stamped row") == [(1,)]
              and ask(conn, "no row of an applied role") == [(2,)],
              "and a stamped row closed after it is caught by both")
        conn.close()
        raw = sqlite3.connect(db)
        raw.execute("INSERT INTO score_history (opportunity_id, recorded_at,"
                    " event, writer) VALUES (?, ?, 'replaced', 'test')",
                    (a, iso(datetime.now(timezone.utc))))
        raw.commit()
        raw.close()
        conn = reader(db)
        check(ask(conn, "no applied row has had") == [(1,)],
              "and a score replaced after the application is caught")
        conn.close()
        # The one HTTP check in the block, read only: the Jobs page carries the
        # Applied section once and one button for each open role. Done here
        # with the same two patterns the box's grep uses.
        page = jobs_html(db)
        seen = re.findall(r'<h2>Applied</h2>|data-apply="', page)
        check(seen.count("<h2>Applied</h2>") == 1
              and seen.count('data-apply="') == 1,   # only RD is still open
              "the page check in the box block finds the Applied section once "
              "and one button for each open role")
        # Every query runs through the sqlite3 command line too, if it is here.
        cli = shutil.which("sqlite3")
        if cli:
            bad = []
            for name, q in sql.items():
                done = subprocess.run([cli, "-readonly", "-header", "-column",
                                       str(db), q], capture_output=True,
                                      text=True, timeout=30)
                if done.returncode != 0 or done.stderr.strip():
                    bad.append(f"{name[:30]}: {done.stderr.strip()[:80]}")
            check(not bad, f"each query runs under the sqlite3 command line ({bad})")
        else:
            note("the sqlite3 command line is not here, so the box queries "
                 "were run through Python only")


def docs_checks() -> None:
    doc = (REPO_ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    check("## Roles applied for" in doc and "NOT_APPLIED_SQL" in doc
          and "applied_at" in doc and "/apply" in doc,
          "the architecture notes describe the marker, the rule, and the routes")
    section = doc.split("## Roles applied for")[1].split("\n## ")[0]
    check("second first-open event" in section and "begin_write" in section
          and "Restoring the pre-deploy backup" in section
          and "prospect trend" in section,
          "and they say what old code does to an applied role, why the "
          "pre-deploy backup is the wrong rollback, that this is a second "
          "first-open event, and which writers take the lock")
    cfg = (REPO_ROOT / "test" / "run_all.sh").read_text(encoding="utf-8")
    check("run_applied_test.py" in cfg, "and run_all.sh runs this file")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", help="accepted for suite symmetry, unused")
    parser.parse_args()
    for section in (migration_checks, status_checks, chain_rule_checks,
                    routes_checks, concurrency_checks,
                    guard_checks, page_script_checks, repost_checks,
                    expiry_checks, checker_checks, apply_page_checks, regate_checks,
                    rescore_checks, backfill_checks, digest_checks,
                    touch_checks, page_checks, applied_days_checks,
                    home_checks, nothing_else_changes_checks,
                    static_scan_checks, box_query_checks, docs_checks):
        # One section that raises is one FAIL that names it. The sections
        # after it still run, so a mistake in the first does not hide the rest.
        try:
            section()
        except Exception as err:  # noqa: BLE001  reported, never swallowed
            import traceback
            traceback.print_exc()
            check(False, f"{section.__name__} stopped on {type(err).__name__}:"
                         f" {err}")
    print()
    if failures:
        print(f"{len(failures)} APPLIED-ROLES CHECK(S) FAILED")
        return 1
    print("ALL APPLIED-ROLES CHECKS PASSED"
          + (f" ({len(skipped)} skipped, see the NOTE lines)" if skipped else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:  # noqa: BLE001  readable FAIL, never a traceback
        import traceback
        print(f"\nFAIL. Unexpected {type(err).__name__}. {err}")
        traceback.print_exc()
        sys.exit(1)
