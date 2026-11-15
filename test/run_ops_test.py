#!/usr/bin/env python
"""Checks for the operational faults the 13 November 2026 audit found.

1. The signal watcher. One source whose check raised or hung used to end
   the run there with no row in runs, so every source after it in the file
   went unchecked and the only sign was a "last run" that grew old. Now a
   fetch never raises, a source that fails is stamped and passed over, an
   item whose scoring fails is kept unscored, and every run writes its row,
   ok, partial or failed.
2. The advert checker writes its row however its run ends, and a reply
   that breaks off is a network failure, not a crash.
4. Each job is judged by its own limits, by /health and by Home alike, so a
   quiet night is not a fault and Home cannot show green while /health
   answers degraded.
5. The company page reads the CV match, not the retired combined score,
   and a contact's outcome as stored.

No network beyond a loopback server this runner starts, mock scoring only,
no n8n. Checks that need SIGALRM are skipped where it does not exist, on
Windows, and run on Linux. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import http.server
import io
import json
import os
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(TEST_DIR))

import build_dashboard  # noqa: E402
import check_jobs as cj  # noqa: E402
import check_signals as cs  # noqa: E402
import home_page  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402

failures: list[str] = []
skipped: list[str] = []
HAVE_ALARM = hasattr(signal, "setitimer")
TERM_AT_START = signal.getsignal(signal.SIGTERM)


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def skip(label: str) -> None:
    print("SKIP  " + label)
    skipped.append(label)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ loopback server

PAGES: dict[str, str] = {}


class Pages(http.server.BaseHTTPRequestHandler):
    """Serves PAGES by unquoted path, plus two pages that misbehave."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):                              # noqa: N802
        path = urllib.parse.unquote(self.path)
        if path == "/trickle":
            # Headers at once, then a byte every fifth of a second. Each
            # byte arrives well inside any socket timeout, so only a limit
            # on the whole body can end the read.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass
            return
        if path == "/broken-chunked":
            # One whole chunk, then the connection drops mid-reply. Reading
            # it raises http.client.IncompleteRead, which is not an OSError.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"a\r\n0123456789\r\n")
            self.wfile.flush()
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            return
        body = PAGES.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):                  # noqa: D401
        pass


def start_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------------ helpers

def run_main(module, argv: list[str]) -> tuple[int, dict]:
    """A script's main() in process, its stdout JSON parsed."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = module.main(argv)
    raw = out.getvalue()
    start = raw.find("{")
    try:
        parsed = json.loads(raw[start:]) if start >= 0 else {}
    except ValueError:
        parsed = {}
    parsed["_stderr"] = err.getvalue()
    return code, parsed


def last_run(dbp: Path, workflow: str) -> dict | None:
    conn = sqlite3.connect(dbp)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM runs WHERE workflow = ?"
                           " ORDER BY id DESC LIMIT 1", (workflow,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def query(dbp: Path, sql: str, params=()) -> list[dict]:
    conn = sqlite3.connect(dbp)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def make_due(dbp: Path) -> None:
    conn = sqlite3.connect(dbp)
    conn.execute("UPDATE source_state SET last_checked = '2000-01-01T00:00:00Z'")
    conn.commit()
    conn.close()


def seed_run(conn, ts: str, workflow: str, mode: str = "live",
             outcome: str | None = None, note: str | None = None) -> None:
    conn.execute("INSERT INTO runs (ts, workflow, mode, note, outcome)"
                 " VALUES (?,?,?,?,?)", (ts, workflow, mode, note, outcome))


@contextlib.contextmanager
def patched(obj, name: str, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# ------------------------------------------------------------------- checks

def fetch_checks(base: str) -> None:
    print("\nFetching. A page can no longer stop a run by how it fails.")

    check(rc.request_url("https://x.example/a b?q=c d#part")
          == "https://x.example/a%20b?q=c%20d",
          "a space in the path and the query is percent-encoded, the fragment dropped")
    check(rc.request_url("https://x.example/café")
          == "https://x.example/caf%C3%A9",
          "a non-ASCII path is sent as its UTF-8 escapes")
    check(rc.request_url("https://x.example/already%20done")
          == "https://x.example/already%20done",
          "an escape already in the URL is kept, not escaped twice")
    check(rc.request_url("  https://x.example/news/1\n")
          == "https://x.example/news/1",
          "whitespace around a scraped href is trimmed")
    check(rc.request_url("https://bücher.example/")
          == "https://xn--bcher-kva.example/",
          "a non-ASCII host name goes to its IDNA form")
    plain = "https://optics.org/news/17/11/12?x=1&y=2"
    check(rc.request_url(plain) == plain, "an ordinary URL is unchanged")

    # The premise. The standard library refuses these with errors that are
    # not OSErrors, which is why the old except clause let them out.
    for label, url in (("a space", f"{base}/news/a b"),
                       ("a non-ASCII path", f"{base}/news/café")):
        try:
            urllib.request.urlopen(url, timeout=5).close()
            raised = None
        except Exception as err:                   # noqa: BLE001
            raised = err
        check(raised is not None and not isinstance(raised, OSError),
              f"urllib alone refuses {label} with "
              f"{type(raised).__name__ if raised else 'nothing'}, not a network error")
    try:
        with urllib.request.urlopen(f"{base}/broken-chunked", timeout=5) as r:
            r.read(2_000_000)
        raised = None
    except Exception as err:                       # noqa: BLE001
        raised = err
    check(isinstance(raised, http.client.HTTPException)
          and not isinstance(raised, OSError),
          "a reply that breaks off raises http.client's own error, not an OSError "
          f"(got {type(raised).__name__ if raised else 'nothing'})")

    cfg = {"fetch_user_agent": "RadarOpsTest/1.0"}

    def get(url):
        # A raise is the fault these checks are about, so it is a FAIL with
        # its name, never a traceback that stops the runner.
        try:
            return rc.http_get(url, cfg)
        except Exception as err:                   # noqa: BLE001
            return -1, {"error": f"raised {type(err).__name__}"}, ""

    PAGES["/news/a b"] = "<p>Spaced article body</p>"
    PAGES["/news/café"] = "<p>Accented article body</p>"
    status, _, body = get(f"{base}/news/a b")
    check(status == 200 and "Spaced article body" in body,
          f"http_get fetches a link with a space in it (status {status})")
    status, _, body = get(f"{base}/news/café")
    check(status == 200 and "Accented article body" in body,
          f"http_get fetches a non-ASCII link (status {status})")

    status, headers, _ = get(f"{base}/broken-chunked")
    check(status == 0 and "IncompleteRead" in headers.get("error", ""),
          f"a reply that breaks off is status 0 with its reason, not a raise "
          f"({headers.get('error')})")

    with patched(rc, "FETCH_BODY_SECONDS", 1.0):
        began = time.monotonic()
        status, headers, _ = get(f"{base}/trickle")
        took = time.monotonic() - began
    check(status == 0 and "TimeoutError" in headers.get("error", "")
          and took < 10,
          f"a body that trickles is given up on at its limit, {took:.1f}s "
          f"({headers.get('error')})")

    status, _, _ = get("http://[not-a-host/x")
    check(status == 0, "a URL nothing can parse is status 0, not a raise")

    # The advert checker's own fetch, outside mock mode, for one call.
    saved = os.environ.pop("RADAR_MOCK", None)
    try:
        status, url, body = cj.fetch_job_page(f"{base}/broken-chunked")
    except Exception as err:                       # noqa: BLE001
        status, url, body = -1, "", f"raised {type(err).__name__}"
    finally:
        if saved is not None:
            os.environ["RADAR_MOCK"] = saved
    check(status == 0 and body == "",
          "the advert checker reads a reply that breaks off as a network failure")


def watcher_checks(base: str, tmp: Path) -> None:
    print("\nThe watcher. One bad source costs that source, never the run.")
    dbp = tmp / "watcher.sqlite"
    rc.get_db(dbp).close()

    def source(sid: str) -> dict:
        return {"id": sid, "name": sid, "url": f"{base}/{sid}/news",
                "method": "diff", "check_interval_hours": 6, "status": "live",
                "tier": 1}

    sources = [source("src-a"), source("src-broken"), source("src-c")]
    PAGES["/src-a/news"] = '<a href="/src-a/news/one">Alpha raises a seed round</a>'
    PAGES["/src-broken/news"] = '<a href="/src-broken/news/one">Broken source item</a>'
    PAGES["/src-c/news"] = '<a href="/src-c/news/one">Gamma opens a photonics lab</a>'
    real_check_diff = cs.check_diff

    def check_diff(src, state, config, conn, notes):
        if src["id"] == "src-broken":
            raise RuntimeError("stub: a page shaped like nothing seen before")
        return real_check_diff(src, state, config, conn, notes)

    with patched(cs, "load_watchlist", lambda: [dict(s) for s in sources]), \
            patched(cs, "check_diff", check_diff):
        code, out = run_main(cs, ["--dry-run", "--mock", "--db", str(dbp)])
        states = {r["source_id"]: r for r in query(dbp, "SELECT * FROM source_state")}
        run = last_run(dbp, "signals")
        check(code == 0, f"a run with one failing source exits 0 (got {code})")
        check(states.get("src-c", {}).get("last_checked") is not None,
              "the source after the failing one is still checked")
        check(states.get("src-broken", {}).get("last_status") == "error"
              and states["src-broken"].get("last_checked"),
              "the failing source is stamped error, so it waits its interval")
        check(run is not None and run["outcome"] == "partial"
              and "src-broken" in (run["note"] or ""),
              f"the run's row says partial and names the source ({run and run['note']})")
        check(out.get("outcome") == "partial" and out.get("errors") == 1,
              "the run's JSON says partial with one error")
        check(build_dashboard.RESULT_WORDS["error"][0] == "the check failed",
              "Insights words the stamp as the check failed")

        # Second pass. Each page lists one new article. src-a's link has a
        # space in it, which used to raise inside the article fetch.
        make_due(dbp)
        PAGES["/src-a/news"] = ('<a href="/src-a/news/new item">Alpha opens a '
                                'diagnostics lab in Leuven</a>'
                                '<a href="/src-a/news/one">Alpha raises a seed round</a>')
        PAGES["/src-a/news/new item"] = ("<p>Alpha Diagnostics opened a photonic "
                                         "IVD lab and has no software team.</p>")
        PAGES["/src-c/news"] = ('<a href="/src-c/news/two">Gamma enters an '
                                'accelerator programme</a>'
                                '<a href="/src-c/news/one">Gamma opens a photonics lab</a>')
        PAGES["/src-c/news/two"] = "<p>Gamma joins the accelerator.</p>"
        code, out = run_main(cs, ["--dry-run", "--mock", "--db", str(dbp)])
        rows = {r["source_url"]: r for r in query(dbp, "SELECT * FROM signals")}
        spaced = rows.get(f"{base}/src-a/news/new item")
        check(code == 0 and spaced is not None
              and "no software team" in (spaced["summary"] or ""),
              "an article whose link holds a space is fetched and scored from its text")
        check(f"{base}/src-c/news/two" in rows,
              "and the source after the failing one still has its item scored")

    print("\nThe watcher. An item whose scoring fails is kept, not lost.")
    PAGES.clear()
    PAGES["/robots.txt"] = ""
    samples = TEST_DIR / "sample_announcements"
    real_score = cs.score_item

    def score_item(item, system_blocks, config, mock_fn):
        if "bramert" in item["url"]:
            raise RuntimeError("stub: the scoring service is down")
        return real_score(item, system_blocks, config, mock_fn)

    dbp2 = tmp / "items.sqlite"
    with patched(cs, "score_item", score_item):
        code, out = run_main(cs, ["--inject", str(samples / "marginal.md"),
                                  "--inject", str(samples / "irrelevant.md"),
                                  "--dry-run", "--mock", "--db", str(dbp2)])
    rows = query(dbp2, "SELECT source_url, relevance, why FROM signals")
    kept = [r for r in rows if "bramert" in r["source_url"]]
    check(code == 0 and len(rows) == 2,
          f"both injected items are stored (got {len(rows)}, exit {code})")
    check(kept and kept[0]["relevance"] is None
          and "scoring failed, RuntimeError" in (kept[0]["why"] or ""),
          "the one whose scoring raised is kept unscored, for rescore.py")
    check(out.get("outcome") == "partial" and out.get("items_unscored") == 1,
          "and the run says partial with one item kept unscored")
    check((last_run(dbp2, "signals") or {}).get("outcome") == "partial",
          "the row in runs says partial too")

    print("\nThe watcher. A source that runs out of time keeps its unread items.")
    dbp3 = tmp / "timeout.sqlite"
    conn = rc.get_db(dbp3)
    items = [{"source_id": "slow", "source_name": "slow", "url": f"https://slow.example/{n}",
              "headline": f"Slow item {n}", "date": None, "text": f"Body {n}"}
             for n in (1, 2, 3)]
    calls = []

    def process_item(item, *a, **k):
        calls.append(item["url"])
        if item["url"].endswith("/2"):
            raise cs.SourceTimeout("still going after 1 seconds")
        cs.keep_unscored(conn, item, "stub stored")

    result = {"notes": [], "errors": 0, "failed_sources": [], "items_unscored": 0}
    with patched(cs, "check_source", lambda *a, **k: items), \
            patched(cs, "process_item", process_item):
        cs.run_source({"id": "slow"}, None, conn, {}, [], None, False, result,
                      seconds=None)
    stored = {r["source_url"]: r["why"] for r in conn.execute(
        "SELECT source_url, why FROM signals")}
    check(len(stored) == 3 and "reached" in stored.get("https://slow.example/3", ""),
          "the items after the one it stopped on are stored unscored, not lost")
    check(result["errors"] == 1 and result["failed_sources"]
          and "still going" in result["failed_sources"][0],
          "the source is reported failed, with the time it ran out")
    conn.close()

    if HAVE_ALARM:
        began = time.monotonic()
        try:
            with cs.time_limit(0.3):
                time.sleep(3)
            fired = False
        except cs.SourceTimeout:
            fired = True
        took = time.monotonic() - began
        check(fired and took < 1.5,
              f"the time limit interrupts a wait it cannot see into, {took:.2f}s")
        dbp4 = tmp / "hang.sqlite"
        conn = rc.get_db(dbp4)
        result = {"notes": [], "errors": 0, "failed_sources": [], "items_unscored": 0}
        hung = {"id": "hangs", "url": "https://hangs.example/", "method": "diff"}
        began = time.monotonic()
        with patched(cs, "check_source", lambda *a, **k: time.sleep(5) or []):
            cs.run_source(hung, None, conn, {}, [], None, False, result,
                          seconds=0.3)
        took = time.monotonic() - began
        state = conn.execute("SELECT last_status FROM source_state"
                             " WHERE source_id = 'hangs'").fetchone()
        check(took < 2 and state is not None and state["last_status"] == "error",
              f"a source that hangs is let go and stamped error, {took:.2f}s")
        check(any("still going after 0.3 seconds" in n for n in result["notes"]),
              "the run notes say it ran out of time")
        conn.close()
    else:
        skip("the time limit needs SIGALRM, which this platform lacks")

    print("\nThe watcher. A run that stops still writes its row.")
    PAGES["/src-a/news"] = '<a href="/src-a/news/one">Alpha raises a seed round</a>'
    PAGES["/src-c/news"] = '<a href="/src-c/news/one">Gamma opens a photonics lab</a>'
    dbp5 = tmp / "stops.sqlite"
    rc.get_db(dbp5).close()
    two = [source("src-a"), source("src-c")]
    real_get_state = cs._get_state
    seen = []

    def get_state(conn, source_id):
        seen.append(source_id)
        if source_id == "src-c":
            raise sqlite3.OperationalError("database is locked")
        return real_get_state(conn, source_id)

    with patched(cs, "load_watchlist", lambda: [dict(s) for s in two]), \
            patched(cs, "_get_state", get_state):
        code, out = run_main(cs, ["--dry-run", "--mock", "--db", str(dbp5)])
    run = last_run(dbp5, "signals")
    check(code == 1, f"a run that fails outside any one source exits 1 (got {code})")
    check(run is not None and run["outcome"] == "failed"
          and "OperationalError: database is locked" in (run["note"] or ""),
          f"and still writes a failed row with the reason ({run and run['note']})")
    check(run is not None and "sources=1" in (run["note"] or ""),
          "the row counts the source it did check")
    check("Traceback" in out.get("_stderr", ""),
          "the traceback goes to stderr, where n8n shows it")

    def stopped(*a, **k):
        raise cs.RunStopped("stopped from outside by signal 15")

    with patched(cs, "load_watchlist", lambda: [dict(s) for s in two]), \
            patched(cs, "run_source", stopped):
        make_due(dbp5)
        code, out = run_main(cs, ["--dry-run", "--mock", "--db", str(dbp5)])
    run = last_run(dbp5, "signals")
    check(code == 1 and run["outcome"] == "failed"
          and "RunStopped" in (run["note"] or ""),
          "a run stopped from outside writes a failed row on its way out")
    try:
        cs._stop_from_outside(15, None)
        raised = False
    except cs.RunStopped:
        raised = True
    check(raised, "SIGTERM's handler raises RunStopped")
    check(not issubclass(cs.RunStopped, Exception)
          and not issubclass(cs.SourceTimeout, Exception),
          "neither stop can be caught by an except Exception on the way down")

    if hasattr(signal, "SIGTERM") and os.name == "posix":
        before = signal.getsignal(signal.SIGTERM)

        def terminated(*a, **k):
            signal.raise_signal(signal.SIGTERM)

        with patched(cs, "load_watchlist", lambda: [dict(s) for s in two]), \
                patched(cs, "run_source", terminated):
            make_due(dbp5)
            code, out = run_main(cs, ["--dry-run", "--mock", "--db", str(dbp5)])
        run = last_run(dbp5, "signals")
        check(code == 1 and run["outcome"] == "failed"
              and "signal 15" in (run["note"] or ""),
              "a real SIGTERM mid-run ends it with a failed row")
        check(signal.getsignal(signal.SIGTERM) is before,
              "and the SIGTERM handler is put back afterwards")
    else:
        skip("a real SIGTERM needs POSIX")

    with patched(cs, "load_watchlist", lambda: [dict(s) for s in two]):
        make_due(dbp5)
        code, out = run_main(cs, ["--dry-run", "--mock", "--db", str(dbp5),
                                  "--budget", "0"])
    check(signal.getsignal(signal.SIGTERM) == TERM_AT_START,
          "after every run the SIGTERM handler is the one the process started with")
    run = last_run(dbp5, "signals")
    states = {r["source_id"]: r["last_checked"]
              for r in query(dbp5, "SELECT * FROM source_state")}
    check(code == 0 and out.get("left_for_next_run") == 2
          and out.get("sources_checked") == 0,
          "a run out of time starts no further source")
    check(run["outcome"] == "partial"
          and "2 left for the next run" in (run["note"] or ""),
          "and says so in its row")
    check(all(v == "2000-01-01T00:00:00Z" for v in states.values()),
          "the sources it left are still due for the next run")


def checker_checks(tmp: Path) -> None:
    print("\nThe advert checker. A run that fails part-way still writes its row.")
    dbp = tmp / "checker.sqlite"
    rc.get_db(dbp).close()
    rows = [{"id": 1, "title": "Software Lead", "tier": "top",
             "source_url": "https://www.linkedin.com/comm/jobs/view/111",
             "due_reason": "stale"},
            {"id": 2, "title": "QA Lead", "tier": "top",
             "source_url": "https://www.linkedin.com/comm/jobs/view/222",
             "due_reason": "stale"}]
    fetched = []

    def fetch(url):
        fetched.append(url)
        if "222" in url:
            raise RuntimeError("stub: a page nobody expected")
        return 404, url, ""

    with patched(cj, "due_candidates", lambda conn, now=None: [dict(r) for r in rows]), \
            patched(cj, "fetch_job_page", fetch):
        code, out = run_main(cj, ["--dry-run", "--db", str(dbp)])
    run = last_run(dbp, "check")
    check(code == 1, f"a checker run that raises exits 1 (got {code})")
    check(run is not None and run["outcome"] == "failed"
          and "RuntimeError: stub: a page nobody expected" in (run["note"] or ""),
          f"and writes a failed row with the reason ({run and run['note']})")
    check(run is not None and run["items_in"] == 1 and run["mode"] == "dry-run",
          "the row keeps what it got through before it failed, and its mode")

    with patched(cj, "due_candidates", lambda conn, now=None: []):
        code, out = run_main(cj, ["--dry-run", "--db", str(dbp)])
    run = last_run(dbp, "check")
    check(code == 0 and run["outcome"] == "ok",
          "a run that finishes writes ok")


def health_checks(tmp: Path) -> None:
    print("\nJob health. Each job by its own limits.")
    now = datetime(2026, 11, 13, 22, 36, tzinfo=timezone.utc)
    stamp = iso(now)

    def ago(hours: float) -> str:
        return iso(now - timedelta(hours=hours))

    def verdicts(seed) -> dict:
        dbp = tmp / f"jobs-{time.monotonic_ns()}.sqlite"
        conn = rc.get_db(dbp)
        seed(conn)
        conn.commit()
        got = rc.job_health(conn, now=stamp)
        conn.close()
        return got

    # The night of the audit: the inbox 1.6 hours old, the watcher's last
    # finished run 22.6 hours old.
    jobs = verdicts(lambda c: (seed_run(c, ago(1.6), "inbox"),
                               seed_run(c, ago(22.6), "signals", "dry-run")))
    check(jobs["inbox"]["state"] == "ok" and jobs["signals"]["state"] == "stale",
          "13 Nov: the inbox fine, the watcher stale")

    for hours, want in ((5, "ok"), (11.9, "ok"), (13, "late"), (25, "stale")):
        jobs = verdicts(lambda c, h=hours: seed_run(c, ago(h), "inbox"))
        check(jobs["inbox"]["state"] == want,
              f"the inbox {hours} hours after its last email is {want} "
              f"(got {jobs['inbox']['state']})")
    jobs = verdicts(lambda c: None)
    check(all(v["state"] == "never" for v in jobs.values()),
          "a job with no row at all has never run")

    for hours, want in ((3, "ok"), (5, "late"), (7, "stale")):
        jobs = verdicts(lambda c, h=hours: seed_run(c, ago(h), "signals"))
        check(jobs["signals"]["state"] == want,
              f"the watcher {hours} hours after a finished run is {want}")
    jobs = verdicts(lambda c: (seed_run(c, ago(3), "signals", outcome="ok"),
                               seed_run(c, ago(1), "signals", outcome="failed",
                                        note="sources=0, stopped: RunStopped")))
    v = jobs["signals"]
    check(v["state"] == "failing" and v["failed_runs"] == 1
          and v["last_run"] == ago(3),
          "a failed run inside the limits reads failing, timed by the last good run")
    jobs = verdicts(lambda c: (seed_run(c, ago(7), "signals", outcome="ok"),
                               seed_run(c, ago(4), "signals", outcome="failed",
                                        note="first"),
                               seed_run(c, ago(2), "signals", outcome="failed",
                                        note="second")))
    v = jobs["signals"]
    check(v["state"] == "stale" and v["failed_runs"] == 2
          and v["failed_note"] == "second",
          "runs that keep failing make the job stale, with the newest reason")
    jobs = verdicts(lambda c: seed_run(c, ago(1), "signals", outcome="partial"))
    check(jobs["signals"]["state"] == "partial", "a partial run reads partial")
    jobs = verdicts(lambda c: seed_run(c, ago(1), "signals", outcome="failed"))
    check(jobs["signals"]["state"] == "stale" and jobs["signals"]["last_run"] is None,
          "a job that has only ever failed is stale, not never run")

    for hours, want in ((25, "ok"), (27, "late"), (51, "stale")):
        jobs = verdicts(lambda c, h=hours: seed_run(c, ago(h), "check", "dry-run"))
        check(jobs["check"]["state"] == want,
              f"the advert checker {hours} hours after its last run is {want}")
    check(jobs["check"]["mode"] == "dry-run",
          "the checker's verdict carries the mode it ran in")

    jobs = verdicts(lambda c: (seed_run(c, ago(24 * 9), "digest", "commit"),
                               seed_run(c, ago(48), "digest", "dry-run")))
    check(jobs["digest"]["state"] == "ok"
          and jobs["digest"]["last_sent"] == ago(24 * 9),
          "the digest is judged by its weekly build and says when it last sent")
    jobs = verdicts(lambda c: seed_run(c, ago(48), "digest", "dry-run"))
    check(jobs["digest"]["last_sent"] is None, "a digest never sent says so")
    jobs = verdicts(lambda c: seed_run(c, ago(24 * 8 + 1), "digest", "dry-run"))
    check(jobs["digest"]["state"] == "stale", "a Monday and a day missed is stale")

    old = tmp / "no-outcome.sqlite"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, ts TEXT,"
                 " workflow TEXT, mode TEXT, note TEXT)")
    conn.execute("INSERT INTO runs (ts, workflow, mode) VALUES (?, 'signals', 'live')",
                 (ago(1),))
    conn.commit()
    conn.row_factory = sqlite3.Row
    jobs = rc.job_health(conn, now=stamp)
    conn.close()
    check(jobs["signals"]["state"] == "ok",
          "a database whose runs table has no outcome column yet still reads")

    check(rc.health_jobs({"health_jobs": ["inbox", "nonsense"]}) == ("inbox",),
          "health_jobs keeps the names it knows")
    check(rc.health_jobs({}) == rc.HEALTH_JOBS_DEFAULT
          and rc.health_jobs({"health_jobs": "inbox"}) == rc.HEALTH_JOBS_DEFAULT,
          "a missing or malformed health_jobs watches all four")

    print("\n/health. The answer follows the per-job verdicts.")
    real_now = datetime.now(timezone.utc)

    def real_ago(hours: float) -> str:
        return iso(real_now - timedelta(hours=hours))

    def health(seed, config=None) -> tuple[int, dict]:
        dbp = tmp / f"health-{time.monotonic_ns()}.sqlite"
        conn = rc.get_db(dbp)
        seed(conn)
        conn.commit()
        conn.close()
        serve_dashboard.ARGS = type("A", (), {"db": str(dbp)})()
        if config is None:
            return serve_dashboard.health_payload()
        with patched(rc, "load_config", lambda: config):
            return serve_dashboard.health_payload()

    def fresh(c, inbox_hours=1.0, signals_hours=1.0, extra=()):
        seed_run(c, real_ago(inbox_hours), "inbox")
        seed_run(c, real_ago(signals_hours), "signals", "dry-run", "ok")
        seed_run(c, real_ago(20), "check", "dry-run", "ok")
        seed_run(c, real_ago(48), "digest", "dry-run")
        for row in extra:
            seed_run(c, *row)

    code, out = health(lambda c: fresh(c))
    check(code == 200 and out["status"] == "ok" and "jobs" in out,
          "every job inside its limits answers 200 ok, with each job's verdict")
    check(all(k in out for k in ("inbox_last_run", "signals_last_run",
                                 "inbox_hours_ago", "signals_hours_ago")),
          "the keys a monitor already reads are still there")
    code, out = health(lambda c: fresh(c, inbox_hours=5))
    check(code == 200 and out["jobs"]["inbox"]["state"] == "ok",
          "a quiet night, five hours without an email, is not a fault "
          "(the old rule called it stale at three)")
    code, out = health(lambda c: fresh(c, signals_hours=22.6))
    check(code == 503 and out["status"] == "degraded"
          and out.get("stale") == ["signals"],
          "the watcher's 13 Nov stall still answers 503 degraded")
    code, out = health(lambda c: fresh(c, extra=(
        (real_ago(0.5), "signals", "dry-run", "failed", "stopped: RunStopped"),)))
    check(code == 200 and out.get("failing") == ["signals"],
          "one failed run inside the limits is reported, not degraded")
    code, out = health(lambda c: (seed_run(c, real_ago(1), "inbox"),
                                  seed_run(c, real_ago(1), "signals", "dry-run", "ok")))
    check(code == 503 and set(out.get("stale", [])) == {"check", "digest"},
          "a watched job that has never run turns /health degraded")
    code, out = health(lambda c: (seed_run(c, real_ago(1), "inbox"),
                                  seed_run(c, real_ago(1), "signals", "dry-run", "ok")),
                       config={"health_jobs": ["inbox", "signals"]})
    check(code == 200 and out["jobs"]["check"]["watched"] is False
          and out["jobs"]["check"]["state"] == "never",
          "a job left off health_jobs is reported and never turns the answer")


def home_checks(tmp: Path) -> None:
    # The old Home's job lines are Radar's health's now (/radar), and Home
    # final's five lights read the same verdicts; both are checked here.
    print("\nRadar's health and Home. The lights are /health's verdicts.")
    real_now = datetime.now(timezone.utc)
    config = rc.load_config()

    last_lights: dict = {}

    def home(seed, cfg=None) -> str:
        """Radar's health for this seed; Home final's light words go in
        last_lights."""
        dbp = tmp / f"home-{time.monotonic_ns()}.sqlite"
        conn = rc.get_db(dbp)
        seed(conn)
        conn.commit()
        conn.close()
        read = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
        read.row_factory = sqlite3.Row
        use = cfg or config
        data = build_dashboard.collect(read, use)
        model = home_page.build_model(read, data, use)
        read.close()
        last_lights.clear()
        last_lights.update({light["name"]: light["word"] for light in model["lights"]})
        return build_dashboard.render_health_page(data, use, "t")

    def ago(hours: float) -> str:
        return iso(real_now - timedelta(hours=hours))

    def fresh(c, signals_hours=1.0, inbox_hours=1.0, check_mode="dry-run"):
        seed_run(c, ago(inbox_hours), "inbox")
        seed_run(c, ago(signals_hours), "signals", "dry-run", "ok")
        seed_run(c, ago(20), "check", check_mode, "ok")
        seed_run(c, ago(48), "digest", "dry-run")

    page = home(lambda c: fresh(c, signals_hours=22.6))
    check("Needs a look. Signal watcher stalled" in page
          and 'dot-red" title="Needs a look.' in page,
          "with the watcher stalled Radar's health says so in red, as /health does")
    check(last_lights.get("signals") == "Stale",
          f"and Home's light for the watcher says Stale ({last_lights})")
    body = page.split("<main", 1)[1]
    check("Inbox on schedule" not in body,
          "the inbox no longer claims a schedule it does not keep")
    bar = page.split('<header class="appbar">', 1)[1].split("</header>", 1)[0]
    check("Inbox on schedule" in bar and "Signal watcher" not in bar,
          "the top bar's inbox line is the inbox's own verdict from job_health, "
          "on schedule here, and says nothing of the other jobs")

    page = home(lambda c: fresh(c, inbox_hours=5))
    check("Every job on schedule" in page and "Inbox flowing" in page,
          "a quiet night reads flowing and every job on schedule")
    check(last_lights == {"inbox": "On time", "signals": "Dry run", "check": "Dry run",
                          "digest": "Dry run", "push": "Dry run"},
          f"and Home's lights say On time for the inbox and Dry run for the rest ({last_lights})")
    check("Signal watcher on schedule, phone push in dry run" in page,
          "the watcher's line says the phone push is in dry run")
    check("Advert checker on schedule, dry run, writes nothing" in page,
          "the checker's line says a dry run writes nothing")
    check("Monday digest on schedule, never sent" in page,
          "the digest's line says it has never sent")

    page = home(lambda c: fresh(c, inbox_hours=13))
    check("Running. Inbox quiet" in page and 'dot-amber" title="Running.' in page,
          "half a day without an email is amber and quiet, not a fault")
    check(last_lights.get("inbox") == "Late", f"and Home's inbox light says Late ({last_lights})")

    page = home(lambda c: (seed_run(c, ago(1), "inbox"),
                           seed_run(c, ago(1), "signals", "live", "ok")),
                cfg={**config, "health_jobs": ["inbox", "signals"]})
    check("Every job on schedule" in page,
          "a job /health is not watching does not turn the verdict")
    check("Advert checker never run, not watched by /health" in page
          and 'dot-grey" title="never run"></span>Advert checker' in page,
          "and it shows grey, saying it is not watched")
    check(last_lights.get("check") == "Off" and last_lights.get("push") == "On time",
          f"Home says Off for a job that has never run, and the push is on time with a live "
          f"watcher ({last_lights})")
    check("phone push in dry run" not in page,
          "a watcher armed to push says nothing about dry run")


def company_checks(tmp: Path) -> None:
    print("\nThe company page. The CV match and the outcome as stored.")
    dbp = tmp / "company.sqlite"
    conn = rc.get_db(dbp)
    cid = rc.resolve_company(conn, "Timeline Diagnostics")
    seen = "2026-11-10T09:00:00Z"
    for key, title, cv, combined in (
            ("a", "Software Lead (new scorer)", 62, None),
            ("b", "QA Manager (old scorer)", 40, 25),
            ("c", "Unscored Role", None, None)):
        conn.execute(
            "INSERT INTO opportunities (url_hash, first_seen, company, title,"
            " cv_match, combined, company_id, source, thread_type)"
            " VALUES (?,?,?,?,?,?,?,'linkedin-alert','inbound')",
            (rc.url_hash(f"radar://ops/{key}"), seen, "Timeline Diagnostics",
             title, cv, combined, cid))
    conn.execute("INSERT INTO touches (company, touched_at, channel, note,"
                 " outcome, company_id) VALUES (?,?,?,?,?,?)",
                 ("Timeline Diagnostics", "2026-11-11T10:00:00Z", "comment",
                  "Replied to their post.", "reply", cid))
    conn.execute("INSERT INTO touches (company, touched_at, channel, note,"
                 " outcome, company_id) VALUES (?,?,?,?,?,?)",
                 ("Timeline Diagnostics", "2026-11-12T10:00:00Z", "other",
                  "Sent the article.", None, cid))
    conn.commit()
    reply_id = conn.execute("SELECT id FROM touches WHERE outcome = 'reply'"
                            ).fetchone()["id"]
    page = build_dashboard.render_company_page(conn, cid, rc.load_config(), "t")
    conn.close()
    check("CV match 62%." in page,
          "a role scored since 3 August shows its CV match")
    check("CV match 40%." in page and "Score 25" not in page,
          "a role from the old scorer shows its CV match, not the retired score")
    check("CV match unscored." in page and "Score unscored" not in page,
          "only a role with no CV match reads unscored")
    check("Outcome, reply." in page
          and f'data-outcome="reply" data-touch-id="{reply_id}" disabled' in page,
          "a contact's stored outcome is shown and its button marked")
    check("Outcome, none." in page,
          "a contact with no outcome reads none")


def main() -> int:
    ap = argparse.ArgumentParser(description="operational fault checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    os.environ["RADAR_MOCK"] = "1"
    server, base = start_server()
    PAGES["/robots.txt"] = ""
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            fetch_checks(base)
            watcher_checks(base, tmp_path)
            checker_checks(tmp_path)
            health_checks(tmp_path)
            home_checks(tmp_path)
            company_checks(tmp_path)
    finally:
        server.shutdown()
        server.server_close()

    print()
    if skipped:
        print(f"{len(skipped)} check(s) skipped on this platform.")
    if failures:
        print(f"FAIL. {len(failures)} check(s) failed.")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("PASS. The operational fault checks hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
