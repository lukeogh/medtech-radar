#!/usr/bin/env python
"""Serve the Radar dashboard in a browser, always fresh, and take writes.

    python scripts/serve_dashboard.py

Then open http://127.0.0.1:8787. Every page load re-renders from the
database, so the browser never shows a stale file. The masthead gains two
buttons when served. Refresh re-renders the page. Check now runs the
signals watcher on demand, and the watcher's own politeness gate means
repeated clicks cannot hammer any source, sources inside their check
interval are simply skipped. The page also reloads itself every fifteen
minutes, and n8n keeps feeding the database on its own schedule either
way. The inbox drains through n8n only, it needs the Gmail credential,
so Check now covers the watchlist and the database re-render covers the
rest.

Since the 29 July brief this server is also the write path for human
actions. Acknowledge and its undo land on POST /ack and /unack, Applied
and its undo on POST /apply and /unapply, and the CV update section
lives at /cv. Prepare application is the page /prepare/<id>, which only
reads and never spends on opening, however it is opened; its own button,
which states the cost first, makes the one POST /prepare, which fetches the
advert's full description once and scores it once (prepare_application.py
has the guards, prepare_page.py draws the page). Page renders stay read only, the same
guarantee as build_dashboard.py. Writes open their own short-lived
connection through radar_common.get_db, which also applies migrations.

The LAN-trust assumption, stated plainly. Anyone who can reach this port
can read scored opportunities and acknowledge rows. Binding defaults to
dashboard_host and dashboard_port in radar.yaml, 127.0.0.1 unless
changed there. Open it to the LAN or tailnet only behind that trust, and
if it ever goes behind a reverse proxy that does not restrict who may
connect, give it authentication.

One part of that trust is closed in code. A page open in Luke's own
browser can make that browser send a request to this server, and the
proxy's IP allow list passes it because it really does come from his
machine. The write guard below refuses those requests. It is not
authentication, because anyone who can reach this port can still send the
header. dashboard_guard in radar.yaml sets it to warn or enforce.

Standard library only, no new dependencies. The watcher subprocess runs
dry by default and only ever pushes to ntfy when the server is started
with --push, mirroring check_signals.py itself.

Flags:
    --host ADDR   bind address, overrides dashboard_host in radar.yaml
    --port N      overrides dashboard_port in radar.yaml
    --db PATH     database override, tests use this
    --push        Check now runs the watcher in push mode. Only after
                  the system is armed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class RadarServer(ThreadingHTTPServer):
    # On Windows, address reuse lets a second instance silently bind the
    # same port and steal connections unpredictably. Fail loudly instead.
    # On other systems reuse only covers TIME_WAIT, so it stays on for
    # painless restarts.
    allow_reuse_address = os.name != "nt"

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import radar_common
import applications
import build_dashboard
import jobs_page
import insights_page
import settings_page
import page_frame
import cv_doc
import cv_page
import cv_screen
import cv_store
import experience
import experience_page
import hand_roles
import news_sources
import sources_page
import home_page
import prepare_application
import score_item
import tailored_cv
import watchlist

REPO_ROOT = radar_common.REPO_ROOT

# The only files served besides the page itself, exactly the footer links.
STATIC = {
    "/test/last_digest.html": ("test/last_digest.html", "text/html; charset=utf-8"),
    "/test/last_signal.txt": ("test/last_signal.txt", "text/plain; charset=utf-8"),
    "/docs/cost-note.md": ("docs/cost-note.md", "text/plain; charset=utf-8"),
}

ARGS = None
WATCH_LOCK = threading.Lock()
WATCH_TIMEOUT = 600        # seconds Check now waits for the watcher
REREAD_LOCK = threading.Lock()   # one Read again at a time


def render(page_name: str = "archive", query: dict | None = None) -> bytes:
    """A whole page as bytes: home (the default for anything unnamed),
    radar (Radar's health), archive, insights or jobs. query is the address's
    parsed query, which Home reads for item and show."""
    config = radar_common.load_config()
    db_path = Path(ARGS.db).resolve() if ARGS.db else radar_common.DB_PATH
    if not db_path.exists():
        radar_common.get_db(db_path).close()
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = build_dashboard.collect(conn, config)
        try:
            db_label = db_path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            db_label = str(db_path)
        if page_name == "insights":
            page = build_dashboard.render_insights_page(data, config, db_label)
        elif page_name == "jobs":
            page = build_dashboard.render_jobs_page(data, config, db_label)
        elif page_name == "archive":
            page = build_dashboard.render_page(data, config, db_label,
                                               REPO_ROOT / "dashboard.html",
                                               serve=True)
        elif page_name == "radar":
            page = build_dashboard.render_health_page(data, config, db_label)
        else:
            # Home reads a few things of its own on the same connection.
            item, show = home_query(query or {})
            page = home_page.render(conn, data, config, item, show,
                                    push_on=bool(getattr(ARGS, "push", False)))
    finally:
        conn.close()
    return page.encode("utf-8")


def home_query(query: dict) -> tuple[str | None, str]:
    """(the item an address names, the view): an item is a short id of
    letters, digits and hyphens, and anything else names none."""
    item = (query.get("item") or [""])[0]
    if not (item and len(item) <= 64 and item.isascii()
            and all(c.isalnum() or c == "-" for c in item)):
        item = None
    show = "done" if (query.get("show") or [""])[0] == "done" else "todo"
    return item, show


def render_home_main(query: dict) -> dict:
    """Home's main element as JSON, for its script to put in place after Done
    or Undo last. Read only, like the page."""
    config = radar_common.load_config()
    dbp = db_path()
    if not dbp.exists():
        radar_common.get_db(dbp).close()
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = build_dashboard.collect(conn, config)
        item, show = home_query(query)
        return home_page.main_payload(conn, data, config, item, show,
                                      push_on=bool(getattr(ARGS, "push", False)))
    finally:
        conn.close()


def job_verdicts_now() -> dict | None:
    """The scheduled jobs' verdicts from radar_common.job_health, for the top
    bar of a page that does not read them anyway. None when the database
    cannot be read, and the bar then says nothing about the inbox."""
    dbp = db_path()
    try:
        conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
        try:
            return radar_common.job_health(conn)
        finally:
            conn.close()
    except Exception:                              # noqa: BLE001
        return None


def _query_int(query: dict, name: str) -> int | None:
    raw = (query.get(name) or [""])[0]
    if raw.isascii() and raw.isdigit() and len(raw) <= 18:
        return int(raw)
    return None


def jobs_new_since(since: int) -> dict:
    """The newest row id, and how many open roles have arrived after the row
    a page was drawn with, for the Jobs page's notice. A role counts once,
    and only when no advert on its chain was on file before. Filtered and
    unscored roles are not counted, as the queue does not list them."""
    dbp = db_path()
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    try:
        stamp = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM opportunities").fetchone()[0]
        new = conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(role_key, 'row-' || id))"
            " FROM opportunities WHERE id > ?"
            " AND tier IN ('top', 'question', 'reading')"
            " AND acknowledged_at IS NULL AND closed_at IS NULL"
            " AND dismissed_at IS NULL AND expired_at IS NULL"
            " AND (role_key IS NULL OR role_key NOT IN (SELECT role_key"
            " FROM opportunities WHERE id <= ? AND role_key IS NOT NULL))"
            " AND " + radar_common.NOT_APPLIED_SQL, (since, since)).fetchone()[0]
    finally:
        conn.close()
    return {"stamp": int(stamp), "new": int(new)}


def render_jobs(selected: int | None, query: dict) -> tuple[int, bytes, str]:
    """The Jobs page or one of its fragments, as (status, body, type).

    show picks the view. part asks for a fragment: role (the role pane for
    selected), queue (the queue and the counts, reading=1 with the Reading
    rows drawn), reading (the Reading rows), board (the applications board,
    board_page.board_payload) or stamp (see jobs_new_since). layout=list
    draws the Applied view as the List instead of the Board.
    current names the open role, so a fragment marks it. All read only.
    """
    view = (query.get("show") or ["open"])[0]
    if view not in jobs_page.VIEWS:
        view = "open"
    part = (query.get("part") or [""])[0]
    if part == "stamp":
        payload = jobs_new_since(_query_int(query, "since") or 0)
        return 200, json.dumps(payload).encode(), "application/json"
    config = radar_common.load_config()
    dbp = db_path()
    if not dbp.exists():
        radar_common.get_db(dbp).close()
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        data = build_dashboard.collect(conn, config)
    finally:
        conn.close()
    data["serve"] = True
    try:
        db_label = dbp.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        db_label = str(dbp)
    loc = jobs_page.parse_loc((query.get("loc") or [""])[0])
    model = jobs_page.build_model(data, loc=loc)
    current = _query_int(query, "current")
    current_key = None
    if current is not None:
        face, _where = jobs_page.find_role(data, model, current)
        current_key = jobs_page.role_key(face) if face is not None else None
    if part == "role":
        payload = (jobs_page.role_payload(data, model, selected, view)
                   if selected is not None else None)
        if payload is None:
            return (404, json.dumps({"ok": False, "note": "no such role"}).encode(),
                    "application/json")
        return 200, json.dumps(payload).encode(), "application/json"
    if part == "queue":
        reading = (query.get("reading") or ["0"])[0] == "1"
        payload = jobs_page.queue_payload(model, view, current_key, reading)
        return 200, json.dumps(payload).encode(), "application/json"
    if part == "reading":
        payload = jobs_page.reading_payload(model, current_key)
        return 200, json.dumps(payload).encode(), "application/json"
    if part == "board":
        import board_page
        payload = board_page.board_payload(data, loc)
        return 200, json.dumps(payload).encode(), "application/json"
    reading = (query.get("reading") or ["0"])[0] == "1"
    # The Applied view is a Board unless the address asks for the List.
    layout = "list" if (query.get("layout") or [""])[0] == "list" else "board"
    status, page = jobs_page.render(data, config, db_label, selected, view,
                                    reading, loc, layout)
    return status, page.encode("utf-8"), "text/html; charset=utf-8"


_MIGRATED = False
FONT_DIR = REPO_ROOT / "static" / "fonts"


def _insights_conn():
    """A read-only connection for the Insights pages. The first view after a
    start opens the file once for writing, which runs the migrations, so a
    page never reads a database that lacks the story columns."""
    global _MIGRATED
    dbp = db_path()
    if not _MIGRATED or not dbp.exists():
        radar_common.get_db(dbp).close()
        _MIGRATED = True
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def render_insights(kind: str, arg=None):
    """The Insights front page, one story or one section, as bytes, or None
    when the story or the section does not exist."""
    config = radar_common.load_config()
    conn = _insights_conn()
    try:
        if kind == "sources":
            page = sources_page.render(conn, config)
        elif kind == "story":
            page = insights_page.render_story(conn, arg, config)
        elif kind == "section":
            page = insights_page.render_section(conn, arg, config)
        else:
            page = insights_page.render_front(conn, config)
    finally:
        conn.close()
    return page.encode("utf-8") if page is not None else None


def _write(fn):
    """Run fn(conn) on a writable connection with the dashboard's manners: a
    busy database is a note, not a crash."""
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        return fn(conn)
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    finally:
        conn.close()


def _event_ids(conn, signal_id: int) -> list:
    row = conn.execute("SELECT COALESCE(event_key, 'e' || id) FROM signals WHERE id = ?",
                       (signal_id,)).fetchone()
    if row is None:
        return []
    return [r[0] for r in conn.execute(
        "SELECT id FROM signals WHERE COALESCE(event_key, 'e' || id) = ?", (row[0],))]


def set_story_hidden(signal_id: int, on: bool) -> dict:
    """Hide or show one story: every report of its event, so a story never
    comes back through a second source. Hidden is acknowledged_at, the same
    stamp the old Dismiss set: out of sight, never out of the database."""
    def go(conn):
        radar_common.begin_write(conn)
        ids = _event_ids(conn, signal_id)
        if not ids:
            conn.rollback()
            return {"ok": False, "note": "That story is no longer on file. Reload the page."}
        marks = ",".join("?" * len(ids))
        if on:
            cur = conn.execute(f"UPDATE signals SET acknowledged_at = ? WHERE id IN ({marks})"
                               " AND acknowledged_at IS NULL", (radar_common.now_iso(), *ids))
        else:
            cur = conn.execute(f"UPDATE signals SET acknowledged_at = NULL WHERE id IN ({marks})"
                               " AND acknowledged_at IS NOT NULL", ids)
        conn.commit()
        if cur.rowcount == 0:
            word = "hidden" if on else "on the page"
            return {"ok": False, "note": f"That story is already {word}, perhaps from another tab. Reload the page."}
        return {"ok": True, "id": signal_id, "reports": cur.rowcount}
    return _write(go)


def split_story(signal_id: int) -> dict:
    """Not the same story: take one report out of its event for good. Its key
    starts "solo-", which nothing ever groups again."""
    def go(conn):
        cur = conn.execute("UPDATE signals SET event_key = ? WHERE id = ?",
                           (f"solo-{signal_id}", signal_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "note": "That report is no longer on file. Reload the page."}
        return {"ok": True, "id": signal_id}
    return _write(go)


def set_watched(company_id: int, on: bool, via: str = "insights", signal_id=None,
                undo_add: bool = False, since=None) -> dict:
    """Add a company to the Watchlist, or take it off. From Insights, the add
    remembers the story it came from and the Watchlist says so on the next
    visit; Insights' Undo is undo_add, which puts the company back as it was
    rather than marking it removed. Remove on the Watchlist keeps the thread
    on file and stops it counting (watchlist.unwatch)."""
    if via not in watchlist.VIA_WORDS:
        via = "insights"
    sid = signal_id if isinstance(signal_id, int) and not isinstance(signal_id, bool) else None

    def go(conn):
        radar_common.begin_write(conn)
        if on:
            result = watchlist.watch(conn, company_id, via, sid)
        else:
            result = watchlist.unwatch(conn, company_id, undo_add=undo_add,
                                       since=since if isinstance(since, str) else None)
        if result.get("ok"):
            conn.commit()
        else:
            conn.rollback()
        return result
    return _write(go)


LOG_CHANNELS = tuple(watchlist.CHANNEL_WORDS)


def log_contact(company_id: int, body: dict) -> dict:
    """Log a contact from the Watchlist's record: its date, how, a note, what
    came back, and the next step with its due date (watchlist.log_contact).
    A company not on the Watchlist joins it."""
    def go(conn):
        radar_common.begin_write(conn)
        result = watchlist.log_contact(conn, company_id, body)
        if result.get("ok"):
            conn.commit()
        else:
            conn.rollback()
        return result
    return _write(go)


def watchlist_write(fn) -> dict:
    """Run one Watchlist write under the write lock: commit what succeeded,
    roll back what was refused."""
    def go(conn):
        radar_common.begin_write(conn)
        result = fn(conn)
        if result.get("ok"):
            conn.commit()
        else:
            conn.rollback()
        return result
    return _write(go)


def render_watchlist(company_id=None, view: str = "table"):
    """The Watchlist: the table with the record beside it (view table) or one
    company's record as its own view (view record, /watchlist/<id>). Read
    only. None when a record's company is not on file."""
    import watchlist_page
    config = radar_common.load_config()
    conn = _insights_conn()
    try:
        data_jobs = None
        try:
            data_jobs = radar_common.job_health(conn)
        except Exception:  # noqa: BLE001  the bar's inbox line is optional
            data_jobs = None
        model = watchlist.build_model(conn, config, company_id=company_id)
    finally:
        conn.close()
    if view == "record" and model["selected"] is None:
        return None
    return watchlist_page.render_page(model, data_jobs, view).encode("utf-8")


def find_companies(query: str) -> dict:
    conn = _insights_conn()
    try:
        return {"ok": True, "matches": watchlist.find_names(conn, query)}
    finally:
        conn.close()


def run_reread(company_id: int, confirmed: bool) -> dict:
    """Read again: one polite fetch and one model call, on a press only and
    only once its cost was shown (the page sends confirm after showing it)."""
    if not confirmed:
        conn = _insights_conn()
        try:
            return {"ok": False, "confirm": True, **watchlist.reread_cost(conn, radar_common.load_config())}
        finally:
            conn.close()
    if not REREAD_LOCK.acquire(blocking=False):
        return {"ok": False, "note": "A Read again is already running. Wait for it to finish."}
    try:
        def go(conn):
            return watchlist.reread(conn, company_id, radar_common.load_config())
        return _write(go)
    finally:
        REREAD_LOCK.release()


def render_prepare(item_id: int, go: bool = False):
    """The Prepare application page for one role, or None for no such id.

    Reads only, on a read-only connection, whatever the address says. go is
    kept for old callers and changes nothing: the page never starts work on
    its own (prepare_page.render_prepare_page).
    """
    del go
    config = radar_common.load_config()
    dbp = db_path()
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            db_label = dbp.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            db_label = str(dbp)
        page = build_dashboard.render_prepare_page(conn, item_id, config,
                                                   db_label)
    finally:
        conn.close()
    return page.encode("utf-8") if page else None


def run_prepare(item_id: int) -> dict:
    """The one write Prepare application makes: fetch, score, store, answer.

    prepare_application.prepare does the work under its guards and says what
    it did. This adds the page's new inside, drawn from the database after
    the write, so the script that made the POST can swap it in. A database
    that stays locked comes back as a polite failure like the other routes.
    """
    try:
        result = prepare_application.prepare(item_id, db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    if not result.get("ok"):
        return result
    config = radar_common.load_config()
    conn = sqlite3.connect(f"file:{db_path().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        inner = build_dashboard.render_prepare_inner(
            conn, item_id, config, notices=result.get("notices"))
    finally:
        conn.close()
    if inner is None:
        return {"ok": False, "note": f"no opportunity {item_id}"}
    result["html"] = inner
    return result


# ------------------------------------------------------- the tailored CV

COPY_ACTIONS = {
    "open": prepare_application.open_copy,
    "save": prepare_application.save_copy,
    "reset": prepare_application.reset_copy,
    "rescore": prepare_application.rescore_copy,
}


def run_copy(action: str, item_id: int, text=None, base=None) -> dict:
    """One button on the tailored CV: open, save, reset or rescore.

    prepare_application does the work under its guards, and says what it did.
    Save, reset and rescore add the right-hand side of the page as it now
    stands, so the script can swap in the new score and asks without touching
    the editor. Open adds nothing: it runs as the page loads, when the Prepare
    application click may be at work on that side, and a copy just made is
    the master in its own layout, numbered as the master is, so the side as
    drawn is already right. base is the version of the copy the page took,
    sent with save and rescore, see prepare_application._moved; a copy that
    has moved on adds nothing either, the page is left as it is and told to
    reload.
    """
    work = COPY_ACTIONS[action]
    try:
        if action in ("save", "rescore"):
            result = work(item_id, text, db_path(), base=base)
        else:
            result = work(item_id, db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    if (not result.get("ok") or action == "open"
            or result.get("outcome") == "copy_moved"):
        return result
    # The right side as a page opened now would draw it, with no notices of
    # its own: what the button had to say goes beside the editor, and a page
    # drawn with a notice leaves out the button under the description that a
    # notice may point to.
    config = radar_common.load_config()
    conn = sqlite3.connect(f"file:{db_path().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        inner = build_dashboard.render_prepare_inner(conn, item_id, config)
    finally:
        conn.close()
    if inner is not None:
        result["html"] = inner
    return result


def run_copy_open(item_id: int) -> dict:
    return run_copy("open", item_id)


def run_copy_save(item_id: int, text, base=None) -> dict:
    return run_copy("save", item_id, text, base)


def run_copy_reset(item_id: int) -> dict:
    return run_copy("reset", item_id)


def run_copy_rescore(item_id: int, text, base=None) -> dict:
    return run_copy("rescore", item_id, text, base)


COPY_BASE_MAX = 64


def clean_copy_base(value) -> str | None:
    """The version of the copy a Save or Rescore request says it took.

    None when the request names none, which is not checked, as for a page
    from before this check. Otherwise short text, or ValueError, which the
    route turns into a 400 before anything is opened.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= COPY_BASE_MAX:
        raise ValueError("base must be the version of the copy the page took")
    return value


def save_master_cv(text, base) -> dict:
    """Save the master CV from the editor as a new version, never over one.

    base is the version the page was opened on, so a page left open from
    before another save cannot quietly undo it.
    """
    try:
        _doc, clean_text = cv_doc.accept(text)
        if base is not None and not isinstance(base, str):
            raise cv_doc.CvError("The version the page was opened on is not text.")
        saved = cv_store.save_master(clean_text, base or None)
    except (cv_doc.CvError, cv_store.UploadError) as err:
        return {"ok": False, "note": str(err)}
    return {"ok": True, **saved}


def content_disposition(name: str) -> str:
    """An attachment header for a saved CV's name, whatever letters it has.

    The name is already safe for a file name. The plain form keeps only the
    ASCII letters a browser cannot misread, the starred form carries the name
    as it is, and browsers take the starred form when they can.
    """
    import unicodedata
    from urllib.parse import quote
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    plain = "".join(ch if ch.isalnum() or ch in " .,()_-" else "_" for ch in plain)
    plain = " ".join(plain.split()) or "CV"
    return (f'attachment; filename="{plain}.docx"; '
            f"filename*=UTF-8''{quote(name + '.docx', safe='')}")


def download_cv(item_id: int) -> tuple[int, bytes, str, dict]:
    """The Word file of a role's saved CV, read only. (status, body, type, headers).

    It is built from the copy Save CV stored, not from what is on screen,
    and opening it writes nothing. No saved copy, or no such role, is a 404.
    """
    dbp = db_path()
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT id, role_key FROM opportunities WHERE id = ?",
                           (item_id,)).fetchone()
        copy = tailored_cv.for_row(conn, dict(row)) if row is not None else None
    finally:
        conn.close()
    text_type = "text/plain; charset=utf-8"
    if row is None:
        return 404, b"No role with that id.", text_type, {}
    if not copy or not copy.get("saved_text"):
        return (404, b"No CV has been saved for this role yet. Press Save CV on "
                b"its Prepare application page first.", text_type, {})
    import cv_word
    try:
        body = cv_word.build(copy["saved_text"], copy.get("saved_name") or "")
    except cv_word.WordUnavailable as err:
        return 503, str(err).encode(), text_type, {}
    name = copy.get("saved_name") or cv_doc.saved_name("", "")
    return 200, body, cv_word.MIME, {"Content-Disposition": content_disposition(name)}


def set_company_state(company_id: int, state: str, confirmed: bool) -> dict:
    """Set one of the three states a human owns, and log why.

    The machine never calls this. Setting a state also writes a touch, so
    six months later the timeline says who decided and when, rather than
    the state having simply appeared.
    """
    if state not in ("in-conversation", "client", "dead"):
        return {"ok": False, "note": f"{state!r} is not a state a human sets here"}
    if state == "dead" and not confirmed:
        return {"ok": False, "note": "dead outranks every other state, confirm first"}
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        row = conn.execute("SELECT display_name, state FROM companies"
                           " WHERE id = ?", (company_id,)).fetchone()
        if row is None:
            return {"ok": False, "note": f"no company {company_id}"}
        now = radar_common.now_iso()
        conn.execute("UPDATE companies SET state = ?, state_changed_at = ?"
                     " WHERE id = ?", (state, now, company_id))
        note = radar_common.sanitise_free_text(
            f"State set to {state.replace('-', ' ')}"
            + (f" from {row['state'].replace('-', ' ')}." if row["state"] else "."))
        conn.execute(
            "INSERT INTO touches (company, touched_at, channel, note, company_id, kind)"
            " VALUES (?,?,?,?,?,'state')",
            (row["display_name"], now, "other", note, company_id))
        conn.commit()
        return {"ok": True, "id": company_id, "state": state,
                "showing": radar_common.company_state(conn, company_id)}
    finally:
        conn.close()


def set_touch_outcome(touch_id: int, outcome: str) -> dict:
    """Record what came back from one touch. Human-set, never inferred."""
    if outcome not in ("none", "reply", "conversation"):
        return {"ok": False, "note": f"{outcome!r} is not an outcome"}
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        cur = conn.execute("UPDATE touches SET outcome = ? WHERE id = ?",
                           (outcome, touch_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "note": f"no touch {touch_id}"}
        return {"ok": True, "id": touch_id, "outcome": outcome}
    finally:
        conn.close()


def health_payload() -> tuple[int, dict]:
    """Is the machine alive, as JSON, for something else to watch.

    Exists so that silence means quiet rather than broken. Nobody opens a
    dashboard at three in the morning, and a pipeline that stopped at
    midnight looks exactly like a pipeline with nothing to say until
    somebody looks. This is the thing Uptime Kuma can look at instead.

    Degraded rather than dead is a real state and gets its own answer. The
    database being reachable while the inbox has not run for a day is not
    an outage, it is a fact worth a nudge.

    Each job is judged by its own limits, radar_common.JOB_LIMITS, the same
    verdict Home shows, and jobs holds all of them. A job named in
    health_jobs (all four unless config/radar.yaml says otherwise) that is
    stale or has never run makes the answer degraded. A job that is failing
    but still inside its limits does not, one bad run is a blip. The two
    old pairs of keys stay as they were for whatever already reads them.
    """
    out = {"database": "unreachable", "inbox_last_run": None,
           "signals_last_run": None, "status": "down"}
    try:
        dbp = db_path()
        conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            jobs = radar_common.job_health(conn)
            out["opportunities"] = conn.execute(
                "SELECT COUNT(*) n FROM opportunities").fetchone()["n"]
            out["signals"] = conn.execute(
                "SELECT COUNT(*) n FROM signals").fetchone()["n"]
        finally:
            conn.close()
        out["database"] = "ok"
    except Exception as err:                       # noqa: BLE001
        out["note"] = f"{type(err).__name__}"
        return 503, out

    try:
        watched = radar_common.health_jobs(radar_common.load_config())
    except Exception:                              # noqa: BLE001
        watched = radar_common.HEALTH_JOBS_DEFAULT
    for name in ("inbox", "signals"):
        out[f"{name}_last_run"] = jobs[name]["last_run"]
        out[f"{name}_hours_ago"] = jobs[name]["hours_ago"]
    for name, verdict in jobs.items():
        verdict["watched"] = name in watched
    out["jobs"] = jobs
    stale = [name for name in watched
             if jobs[name]["state"] in ("stale", "never")]
    out["status"] = "ok" if not stale else "degraded"
    if stale:
        out["stale"] = stale
    failing = [name for name, v in jobs.items() if v["state"] == "failing"]
    if failing:
        out["failing"] = failing
    return (200 if out["status"] == "ok" else 503), out


def db_path() -> Path:
    return Path(ARGS.db).resolve() if ARGS.db else radar_common.DB_PATH


def set_acknowledged(item_id: int, on: bool) -> dict:
    """Stamp or clear acknowledged_at on one opportunity row.

    The one human write this page makes. A short-lived writable
    connection, WAL and the busy timeout make it safe next to the n8n
    writers, and a database that stays locked past the timeout comes
    back as a polite failure rather than a dead handler thread. The row
    never leaves the database, so the URL-hash dedupe keeps rejecting
    the same advert forever.

    Undo also returns a legacy-retired status to machine-owned 'new'.
    Without that, a row mapped from the old actioned/dead retirement
    would look active on the page after Undo while the digest, the
    ageing nag and the backfill all still excluded it on status,
    half-alive forever.

    The verdict covers the repost chain (J18). One role renders as one
    row, so "seen it" on the row must reach every advert sharing its
    role_key, or the next-freshest sibling would stand straight back
    up as the role. A row without a key stands alone, the NULL never
    matches the scope clause.

    A role marked applied is not acknowledged. It is out of the open views
    already, and its Undo is the way back.
    """
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        target = conn.execute(
            "SELECT role_key FROM opportunities WHERE id = ?",
            (item_id,)).fetchone()
        if target is None:
            return {"ok": False, "note": f"no opportunity {item_id}"}
        key = target["role_key"]
        scope = " (id = ? OR (role_key IS NOT NULL AND role_key = ?))"
        if on:
            cur = conn.execute(
                "UPDATE opportunities SET acknowledged_at = ?"
                " WHERE" + scope + " AND acknowledged_at IS NULL"
                " AND closed_at IS NULL AND dismissed_at IS NULL"
                " AND expired_at IS NULL AND " + radar_common.NOT_APPLIED_SQL,
                (radar_common.now_iso(), item_id, key))
            # Acting on a role is reading it (the Jobs page's New).
            if cur.rowcount:
                radar_common.mark_read(conn, item_id, key)
        else:
            cur = conn.execute(
                "UPDATE opportunities SET acknowledged_at = NULL,"
                " status = CASE WHEN status IN ('actioned','dead')"
                "              THEN 'new' ELSE status END,"
                " status_changed_at = CASE WHEN status IN ('actioned','dead')"
                "              THEN ? ELSE status_changed_at END"
                " WHERE" + scope + " AND acknowledged_at IS NOT NULL",
                (radar_common.now_iso(), item_id, key))
        conn.commit()
        if cur.rowcount == 0:
            return _refused(conn, item_id, applied_matters=on)
        return {"ok": True, "id": item_id,
                "acknowledged": bool(on)}
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    finally:
        conn.close()


def set_read(item_id: int) -> dict:
    """The Jobs page opened a role: stamp read_at on its chain.

    The one write behind the page's New. Idempotent: a role read already is
    still a success, with nothing stamped. It changes nothing else, so an
    applied, closed or acknowledged role can be read like any other. Same
    writable-connection manners as set_acknowledged.
    """
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        target = conn.execute(
            "SELECT role_key FROM opportunities WHERE id = ?",
            (item_id,)).fetchone()
        if target is None:
            return {"ok": False, "note": f"no opportunity {item_id}"}
        stamped = radar_common.mark_read(conn, item_id, target["role_key"])
        conn.commit()
        return {"ok": True, "id": item_id, "stamped": stamped}
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    finally:
        conn.close()


def _refused(conn, item_id: int, applied_matters: bool = False) -> dict:
    """The reply when an UPDATE found nothing in the state it expects.

    A role marked applied is named as such, because the usual cause is a
    page left open from before the Applied button was pressed, and "maybe
    already handled" would send the reader looking in the wrong place.
    """
    if applied_matters and radar_common.role_is_applied(conn, item_id):
        return {"ok": False,
                "note": "that role is marked applied, undo that first"}
    return {"ok": False,
            "note": f"no opportunity {item_id} in the state that "
                    "action expects, maybe already handled"}


def set_lifecycle(item_id: int, action: str) -> dict:
    """close, unclose, dismiss or undismiss on one opportunity row.

    The two human verdicts the lifecycle takes: the role is confirmed
    gone, or it is not for me. Either takes the row out of every working
    view, never out of the database. A hand close stamps closed_source
    manual, the confirmation the closed state demands, and unclose
    clears a page-check close too, the human outranking the checker.
    Same writable-connection manners as set_acknowledged.

    A hand verdict judges the role, not the advert, so it covers the
    repost chain (J14, J18): every advert sharing the role_key takes
    the same stamp, and the undo lifts it from all of them. Only the
    checker's page_check close stays per advert, because a closed
    advert is not a closed role.

    A role marked applied can be neither closed nor dismissed. It is out
    of the open views already, and Undo on the Applied list is the way
    back to a role these verdicts can reach.
    """
    scope = " (id = ? OR (role_key IS NOT NULL AND role_key = ?))"
    statements = {
        "close": ("UPDATE opportunities SET closed_at = ?,"
                  " closed_source = 'manual'"
                  " WHERE" + scope + " AND closed_at IS NULL"
                  " AND " + radar_common.NOT_APPLIED_SQL, True),
        "unclose": ("UPDATE opportunities SET closed_at = NULL,"
                    " closed_source = NULL"
                    " WHERE" + scope + " AND closed_at IS NOT NULL", False),
        "dismiss": ("UPDATE opportunities SET dismissed_at = ?"
                    " WHERE" + scope + " AND dismissed_at IS NULL"
                    " AND " + radar_common.NOT_APPLIED_SQL, True),
        "undismiss": ("UPDATE opportunities SET dismissed_at = NULL"
                      " WHERE" + scope + " AND dismissed_at IS NOT NULL",
                      False),
    }
    if action not in statements:
        return {"ok": False, "note": f"unknown action {action}"}
    sql, stamps = statements[action]
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        target = conn.execute(
            "SELECT role_key FROM opportunities WHERE id = ?",
            (item_id,)).fetchone()
        if target is None:
            return {"ok": False, "note": f"no opportunity {item_id}"}
        key = target["role_key"]
        params = ((radar_common.now_iso(), item_id, key) if stamps
                  else (item_id, key))
        cur = conn.execute(sql, params)
        # Acting on a role is reading it (the Jobs page's New).
        if stamps and cur.rowcount:
            radar_common.mark_read(conn, item_id, key)
        conn.commit()
        if cur.rowcount == 0:
            return _refused(conn, item_id,
                            applied_matters=action in ("close", "dismiss"))
        return {"ok": True, "id": item_id, "action": action}
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    finally:
        conn.close()


CV_REFERENCE_MAX = 200


def move_application(app_id: int, to, reason=None, expect=None, undo=False) -> dict:
    """The applications board's Move to: the owner moves one application, or
    undoes their last move. See applications.move."""
    return _write(lambda conn: applications.move(conn, app_id, to, reason, expect,
                                                 undo=undo))


def set_application_next(app_id: int, text, due, interview) -> dict:
    """The owner sets, changes or clears an application's next step."""
    return _write(lambda conn: applications.set_next(conn, app_id, text, due, interview))


def set_home_done(key: str, on: bool, title=None) -> dict:
    """Home's Done and its undo: take one item off Home, or put it back. Only
    the home_done table changes, never the item where it lives."""
    return _write(lambda conn: home_page.mark_done(conn, key, title) if on
                  else home_page.mark_undone(conn, key))


def clean_cv_reference(value) -> str | None:
    """The saved CV reference a request names, or None for no reference.

    The column is free text until a CV page says what a reference looks
    like, so this only keeps junk out: a string, printable, one line, short.
    An empty string means no reference. Anything else raises ValueError, and
    the route turns that into a 400 before the database is opened. What it
    returns is still untrusted, whoever reads it back must escape it and must
    not treat it as a path.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("cv must be text")
    text = value.strip()
    if not text:
        return None
    if len(text) > CV_REFERENCE_MAX or not text.isprintable():
        raise ValueError(f"cv must be one printable line of at most "
                         f"{CV_REFERENCE_MAX} characters")
    return text


def set_applied(item_id: int, on: bool, cv: str | None = None) -> dict:
    """Mark a role applied for, or undo it. One row takes the stamp.

    The stamp is the moment of the click and the saved CV reference, on the
    row the button was on, and from then on the whole repost chain counts
    as applied (radar_common.APPLIED_SQL), a repost that has not arrived
    yet included. Nothing is copied onto the other rows. That is why Undo
    is one clean step: it clears every stamp in the chain, and the role is
    back in the open tiers with the tier it left them with, because
    nothing that works on open rows has touched it since.

    Only an open role can be applied for. A row a human has acknowledged,
    closed or dismissed, or that the forty-five day rule has retired, is
    refused, and so is a role already applied, because a page left open
    from before can send any of them. Same writable-connection manners as
    set_acknowledged.

    The saved CV is the role's saved copy from its Prepare application page,
    by the name Save CV gave it, read in the same statement that stamps the
    role so that a save a moment before is the one recorded. A role with no
    saved copy records none (Brief 4: the saved name, or empty when there is
    none). A reference the request names itself is still checked by the
    route, so junk is a 400 as before, and is not recorded: what is recorded
    is what Save CV stored, never what a request says it was.
    """
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        target = conn.execute(
            "SELECT id, role_key FROM opportunities WHERE id = ?",
            (item_id,)).fetchone()
        if target is None:
            return {"ok": False, "note": f"no opportunity {item_id}"}
        now = radar_common.now_iso()
        if on:
            if radar_common.role_is_applied(conn, item_id):
                return {"ok": False,
                        "note": "that role is already marked applied"}
            radar_common.begin_write(conn)
            count = applications.stamp_applied(conn, item_id, target["role_key"], now)
            # Radar fills Applied on the applications board.
            if count:
                applications.on_applied(conn, item_id, now)
        else:
            # Once the owner has moved the application on, the board is
            # where it moves back, so Undo here is refused.
            radar_common.begin_write(conn)
            why = applications.unapply_refusal(conn, target)
            if why:
                conn.rollback()
                return {"ok": False, "note": why}
            count = applications.clear_applied(conn, item_id, target["role_key"])
            if count:
                applications.on_unapplied(conn, target, now)
        conn.commit()
        if count == 0:
            return _refused(conn, item_id)
        return {"ok": True, "id": item_id, "applied": bool(on)}
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    finally:
        conn.close()


def render_cv_page(query: dict | None = None) -> bytes:
    """The CV page, which lives in cv_screen.py: the master CV on its sheet,
    its versions, the Preferences tab, the Word import and Rescore affected.
    query is the address's parsed query (view, compare)."""
    return cv_screen.render_page(radar_common.load_config(), db_path(),
                                 job_verdicts_now(), query or {})


def render_settings_page() -> bytes:
    """The Settings page, settings_page.py: what the CV page's Preferences tab
    held, on its own page (owner, 18 Nov)."""
    return settings_page.render_page(radar_common.load_config(), db_path(), job_verdicts_now())


def render_experience(rest: str) -> bytes | None:
    """The experience bank (owner, 18 Nov): the list at /cv/experience, the
    empty form at /cv/experience/new, one entry at /cv/experience/<id>. Read
    only. None for an address that is no entry."""
    jobs = job_verdicts_now()
    if rest == "":
        return experience_page.render_list(db_path(), jobs)
    if rest == "new":
        return experience_page.render_entry(db_path(), None, jobs)
    if not rest.isdigit() or len(rest) > 18:
        return None
    return experience_page.render_entry(db_path(), int(rest), jobs)


def add_hand_role(body) -> dict:
    """Add and score: store the role added by hand, under the write lock and
    committed before anything is spent, then score it on the pasted advert
    through Prepare application's path, the one call the press says yes to.
    A score that fails leaves the role stored and unscored. A link Radar
    already holds comes back as existing, and nothing is stored or spent."""
    config = radar_common.load_config()
    now = radar_common.now_iso()

    def go(conn):
        radar_common.begin_write(conn)
        result = hand_roles.add_role(conn, body, now, config)
        if result.get("id"):
            conn.commit()
        else:
            conn.rollback()
        return result
    result = _write(go)
    if not result.get("id"):
        return result
    try:
        scored = prepare_application.prepare(result["id"], db_path(), config)
    except sqlite3.OperationalError:
        scored = {"outcome": "busy"}
    result["outcome"] = scored.get("outcome")
    result["scored"] = bool(scored.get("scored"))
    return result


def sources_write(path: str, body: dict) -> dict:
    """One of /insights/sources' writes. Look fetches the address once, before
    any lock; the rest are one transaction under the write lock."""
    config = radar_common.load_config()
    now = radar_common.now_iso()
    if path == "/insights/sources/look":
        conn = _insights_conn()
        try:
            return news_sources.look(conn, body.get("url"), config)
        finally:
            conn.close()

    def go(conn):
        radar_common.begin_write(conn)
        if path == "/insights/sources/add":
            result = news_sources.add(conn, body.get("token"), body.get("name"), now)
        elif path == "/insights/sources/pause":
            result = news_sources.set_paused(conn, body.get("source_id"), body.get("paused") is True, now)
        else:
            result = news_sources.remove(conn, body.get("source_id"))
        if result.get("ok"):
            conn.commit()
        else:
            conn.rollback()
        return result
    return _write(go)


def edit_hand_role(body) -> dict:
    """Edit on a role added by hand: its link, client, location, type, source,
    rate and fee, under the write lock in one transaction, the Rate gate and
    the tier judged again in code. No model call."""
    config = radar_common.load_config()
    now = radar_common.now_iso()

    def go(conn):
        radar_common.begin_write(conn)
        result = hand_roles.edit_role(conn, body, now, config)
        if result.get("ok"):
            conn.commit()
        else:
            conn.rollback()
        return result
    return _write(go)


def experience_write(fn) -> dict:
    """One experience bank write under the write lock, in one transaction:
    commit what succeeded, roll back what was refused."""
    def go(conn):
        radar_common.begin_write(conn)
        result = fn(conn)
        if result.get("ok"):
            conn.commit()
        else:
            conn.rollback()
        return result
    return _write(go)


def version_when(name: str | None) -> str:
    return cv_screen.version_when(name)


def save_cv_preferences(body) -> dict:
    """The Preferences tab's scoring lines, and the re-sort in code after."""
    return cv_screen.save_preferences(body, radar_common.load_config(), db_path())


def save_cv_section(body) -> dict:
    """The Preferences tab's location or sector words, and the offer after."""
    return cv_screen.save_section(body, radar_common.load_config(), db_path())


def save_cv_design(body) -> dict:
    """The Design dialog's choices, for the sheet and the Word file."""
    return cv_screen.save_design(body)


def _infer_channel(step: str) -> str:
    """The playbook step usually names its own channel. Read it off."""
    text = (step or "").lower()
    if "comment" in text:
        return "comment"
    if "connect" in text or "connection" in text:
        return "connection-note"
    if "article" in text or "post" in text or "write" in text:
        return "artefact"
    return "engagement"


# Step 3 of playbook/announcement-day.md. Around three weeks after the
# announcement, one genuine engagement, brief, no standards and no ask.
WEEK_THREE_ACTION = "Week three. One genuine engagement with their content, brief."
WEEK_THREE_DAYS = 21


def week_three_booking(channel: str, now: datetime | None = None):
    """The next action to book after a touch, or nothing at all.

    Only the announcement-day channels book week three. A comment or a
    connection note is the first contact, so the playbook's next event is
    ours to make and it belongs in the diary. An engagement already is week
    three, and an artefact send is the buying-window move whose next event
    belongs to them, a reply or silence, both fine. Booking a date on either
    would invent a chase the doctrine explicitly forbids.

    Returns (action, YYYY-MM-DD) or (None, None).
    """
    if channel not in ("comment", "connection-note"):
        return None, None
    when = (now or datetime.now(timezone.utc)) + timedelta(days=WEEK_THREE_DAYS)
    return WEEK_THREE_ACTION, when.strftime("%Y-%m-%d")


def set_signal_state(item_id: int, action: str) -> dict:
    """done, ack or unack on one signal row.

    done marks the suggestion carried out. The signal flips to actioned,
    the only status a human sets here, and the touch is logged against
    the company so the tracker, the threads section and the digest all
    know the first contact happened. ack and unack are the jobs rule
    applied to insights, out of sight, never out of the database.
    """
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        now = radar_common.now_iso()
        if action == "done":
            row = conn.execute(
                "SELECT company, headline, playbook_step FROM signals"
                " WHERE id = ? AND status = 'new'"
                " AND acknowledged_at IS NULL", (item_id,)).fetchone()
            if row is None:
                return {"ok": False,
                        "note": f"no live signal {item_id} to mark done, "
                                "maybe already handled"}
            conn.execute("UPDATE signals SET status = 'actioned'"
                         " WHERE id = ?", (item_id,))
            channel = _infer_channel(row["playbook_step"])
            note = f"Did the suggestion. {row['headline'] or 'Signal'}."
            next_action, next_date = week_three_booking(channel)
            conn.execute(
                "INSERT INTO touches (company, touched_at, channel, note,"
                " next_action, next_action_date, company_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (row["company"] or "Unknown company", now, channel, note,
                 next_action, next_date,
                 radar_common.resolve_company(conn, row["company"])
                 if not radar_common.is_placeholder_company(row["company"])
                 else None))
            conn.commit()
            out = {"ok": True, "id": item_id, "channel": channel}
            if next_date:
                out["week_three"] = next_date
            return out
        if action == "ack":
            cur = conn.execute(
                "UPDATE signals SET acknowledged_at = ? WHERE id = ?"
                " AND acknowledged_at IS NULL", (now, item_id))
        else:
            cur = conn.execute(
                "UPDATE signals SET acknowledged_at = NULL WHERE id = ?"
                " AND acknowledged_at IS NOT NULL", (item_id,))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False,
                    "note": f"no signal {item_id} in the state that action "
                            "expects, maybe already handled"}
        return {"ok": True, "id": item_id}
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    finally:
        conn.close()


def run_cv_rescore(body) -> dict:
    """Rescore affected from the CV page: one confirmed run over every role
    a CV or preference change reaches, started in the background and shared
    with Check now's lock (cv_screen.start_rescore)."""
    return cv_screen.start_rescore(body, radar_common.load_config(), db_path(),
                                   WATCH_LOCK)


def run_watcher() -> dict:
    """One watcher pass. Serialised, so two clicks cannot overlap."""
    if not WATCH_LOCK.acquire(blocking=False):
        return {"ok": False, "note": "a check is already running"}
    try:
        # The run is given less time than the kill below, so it stops
        # itself, with its row in runs, rather than being killed without.
        cmd = [sys.executable, str(SCRIPT_DIR / "check_signals.py"),
               "--push" if ARGS.push else "--dry-run",
               "--budget", str(WATCH_TIMEOUT - 60)]
        if ARGS.db:
            cmd += ["--db", ARGS.db]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", timeout=WATCH_TIMEOUT,
                              cwd=str(REPO_ROOT))
        ok = proc.returncode == 0
        summary = {}
        if ok:
            raw = (proc.stdout or "").strip()
            start = raw.find("{")
            if start >= 0:
                try:
                    parsed = json.loads(raw[start:])
                    summary = {k: parsed.get(k) for k in
                               ("sources_checked", "items_new", "pushed")}
                except json.JSONDecodeError:
                    pass
        return {"ok": ok, "summary": summary,
                "note": "" if ok else (proc.stderr or "").strip()[-400:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "note": "the watcher run timed out"}
    finally:
        WATCH_LOCK.release()


# ------------------------------------------------------------ the Watchlist's writes

WATCHLIST_POSTS = ("/watchlist/add", "/watchlist/remove", "/watchlist/restore",
                   "/watchlist/add-name", "/watchlist/suggestion", "/watchlist/arrived",
                   "/watchlist/thread", "/watchlist/person", "/watchlist/person/remove",
                   "/watchlist/reread", "/touch/log")


def _int(body: dict, name: str):
    value = body.get(name)
    if (not isinstance(value, int) or isinstance(value, bool)
            or not -2 ** 63 <= value < 2 ** 63):
        return None
    return value


def watchlist_post(path: str, body: dict) -> tuple:
    """(result, None) or (None, the 400 note) for one Watchlist POST."""
    if path == "/watchlist/suggestion":
        sid = _int(body, "id")
        action = str(body.get("action") or "")
        if sid is None or action not in ("accept", "dismiss", "undo", "again"):
            return None, "id must be an integer and action accept, dismiss, undo or again"
        return watchlist_write(lambda c: watchlist.answer_suggestion(c, sid, action)), None
    if path == "/watchlist/arrived":
        ids = body.get("company_ids")
        if not isinstance(ids, list):
            return None, "company_ids must be a list of integers"
        return watchlist_write(lambda c: {"ok": True, "marked": watchlist.mark_arrivals_seen(c, ids)}), None
    if path == "/watchlist/add-name":
        cid = body.get("company_id")
        if cid is not None and _int(body, "company_id") is None:
            return None, "company_id must be an integer"
        name = body.get("name")
        if cid is None and not isinstance(name, str):
            return None, "send company_id or name"
        return watchlist_write(lambda c: watchlist.add_by_name(
            c, name=name, company_id=cid, anyway=bool(body.get("anyway")))), None
    if path == "/watchlist/person/remove":
        pid = _int(body, "id")
        if pid is None:
            return None, "id must be an integer"
        return watchlist_write(lambda c: watchlist.remove_person(c, pid, bool(body.get("undo")))), None
    cid = _int(body, "company_id")
    if cid is None:
        return None, "company_id must be an integer"
    if path == "/watchlist/add":
        return set_watched(cid, True, str(body.get("via") or "insights"), body.get("signal_id")), None
    if path == "/watchlist/remove":
        return set_watched(cid, False, undo_add=bool(body.get("undo_add")),
                           since=body.get("since")), None
    if path == "/watchlist/restore":
        return watchlist_write(lambda c: watchlist.restore(c, cid)), None
    if path == "/touch/log":
        return log_contact(cid, body), None
    if path == "/watchlist/thread":
        return watchlist_write(lambda c: watchlist.set_thread(
            c, cid, body.get("next_step"), body.get("due"), restore=bool(body.get("restore")))), None
    if path == "/watchlist/person":
        return watchlist_write(lambda c: watchlist.add_person(c, cid, body.get("name"), body.get("title"))), None
    if path == "/watchlist/reread":
        return run_reread(cid, bool(body.get("confirm"))), None
    return None, "unknown Watchlist route"


# ------------------------------------------------------------ the write guard
#
# The proxy's allow list decides which machines may reach this server. It
# cannot tell Luke from a page he happens to have open, because that page's
# requests leave his machine too. So a tab on any other site can POST to
# /ack, /profile/setting or /cv/rescore and the allow list will pass it.
# Two checks close that, and both read request headers and nothing else.
#
# 1. Every POST carries WRITE_HEADER. The page scripts add it to every fetch
#    call that sets method POST (build_dashboard.SCRIPT). A page on another
#    site cannot add a custom header without a CORS preflight, and this
#    server answers no preflight, so the browser never sends the forgery.
# 2. Host, and Origin on a POST, must name this server. A hostile page can
#    point its own name at this address, which makes the browser treat the
#    server as its own origin and lets the page add the header. Its Host and
#    Origin still carry its own name, so they give it away.
#
# Names are compared without the scheme or the port. GET /health skips the
# Host check, because a monitor reaches it by whatever name suits it and it
# returns counts, not content.
#
# warn logs what would be refused and refuses nothing, so Luke can watch his
# own browser and the proxy pass before enforce is switched on. Enforce also
# forbids framing, because a page that frames the dashboard could get a real
# click on a real button, and that request would pass both checks.
#
# This is not authentication. Anyone who can reach the port and sends the
# header gets through.

WRITE_HEADER = build_dashboard.WRITE_HEADER
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


def host_name(value) -> str:
    """The bare, lower-case host in a Host header, an Origin or a config entry.

    Name, name:8787, [::1]:8787 and https://name:8443/ all give the name.
    Anything that cannot be read as one gives an empty string, and an empty
    string never matches an allowed name.
    """
    text = str(value or "").strip().lower()
    if "://" in text:
        try:
            text = urlsplit(text).netloc
        except ValueError:
            return ""
    if not text or any(ch in text for ch in "@/\\ \t"):
        return ""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return ""
        tail = text[end + 1:]
        if tail and not (tail.startswith(":") and tail[1:].isdigit()):
            return ""
        return text[1:end]
    if text.count(":") == 1:
        text, port = text.split(":")
        if port and not port.isdigit():
            return ""
    return text.rstrip(".")


class WriteGuard:
    """Who may talk to this server, judged from the request headers alone."""

    # What to do about a refusal, by the kind of problem that caused it.
    HINTS = {
        "name": "If this is your own browser, add its name to "
                "dashboard_allowed_hosts in config/radar.yaml and restart "
                "the dashboard.",
        "header": "If this is your own browser, reload the page.",
    }

    def __init__(self, mode: str, names) -> None:
        self.mode = mode if mode in ("warn", "enforce") else "enforce"
        self.names = frozenset(n for n in (host_name(x) for x in names) if n)
        self._seen: set = set()
        self._lock = threading.Lock()

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"

    @staticmethod
    def _shown(value) -> str:
        """A value from the request as one safe log token."""
        return repr(str(value)[:120])

    def _findings(self, method: str, path: str,
                  headers) -> list[tuple[str, str]]:
        """Each reason this request would be refused, and what would fix it."""
        found: list[tuple[str, str]] = []
        # A guard that cannot read a request must not wave it through.
        try:
            if not (method == "GET" and path == "/health"):
                hosts = headers.get_all("Host") or []
                if len(hosts) > 1:
                    found.append(("there is more than one Host header", ""))
                elif hosts and host_name(hosts[0]) not in self.names:
                    found.append((f"Host {self._shown(hosts[0])} is not a "
                                  "name this server answers to", "name"))
            if method == "POST":
                origins = headers.get_all("Origin") or []
                if len(origins) > 1:
                    found.append(("there is more than one Origin header", ""))
                elif origins:
                    origin = origins[0].strip()
                    if origin.lower() == "null":
                        found.append(("the Origin is null, which a sandboxed "
                                      "or redirected page sends", ""))
                    elif host_name(origin) not in self.names:
                        found.append((f"Origin {self._shown(origins[0])} is "
                                      "not a name this server answers to",
                                      "name"))
                if not (headers.get(WRITE_HEADER) or "").strip():
                    found.append((f"the {WRITE_HEADER} header is missing",
                                  "header"))
        except Exception as err:  # noqa: BLE001  fail closed
            kind = type(err).__name__
            found.append((f"the headers could not be read ({kind})", ""))
        return found

    def problems(self, method: str, path: str, headers) -> list[str]:
        """Why this request would be refused. Empty when it would pass."""
        return [why for why, _kind in self._findings(method, path, headers)]

    def admit(self, handler, method: str) -> bool:
        """True when the request may go on. Logs, and refuses if enforcing."""
        try:
            path = urlsplit(handler.path).path
        except ValueError:
            # A request line the router will fail on, not a crash here.
            path = ""
        found = self._findings(method, path, handler.headers)
        if not found:
            if not self.enforcing:
                self._note_sighting(handler, method)
            return True
        reasons = " ".join(
            why[:1].upper() + why[1:] + "." for why, _kind in found)
        what = f"{method} {self._shown(path)} from {handler.address_string()}"
        if not self.enforcing:
            sys.stderr.write(f"write guard would refuse {what} (warn only). "
                             f"{reasons}\n")
            return True
        sys.stderr.write(f"write guard refused {what}. {reasons}\n")
        kinds = dict.fromkeys(kind for _why, kind in found if kind)
        note = " ".join(["The write guard refused this request.", reasons]
                        + [self.HINTS[kind] for kind in kinds])
        try:
            if method == "POST":
                handler._drain_body()
                body = json.dumps({"ok": False, "note": note}).encode()
                handler._send(403, body, "application/json")
            else:
                handler._send(403, note.encode(), "text/plain; charset=utf-8")
        except OSError:
            pass  # the caller went away, there is nobody to tell
        return False

    def _note_sighting(self, handler, method: str) -> None:
        """Log each distinct way a request passes once, in warn mode only.

        This is what Luke reads after a day, the Host and Origin his own
        browser and the proxy really send. Keyed on the parsed names, and on
        the Origin and the header only for a POST, so a client cannot grow
        the set by varying a port or a header that is never checked.
        """
        host = handler.headers.get("Host")
        origin = handler.headers.get("Origin")
        header = bool((handler.headers.get(WRITE_HEADER) or "").strip())
        key = (method, host_name(host))
        if method == "POST":
            key += (host_name(origin), header)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
        line = f"write guard sees {method} "
        line += (f"Host {self._shown(host)}" if host is not None
                 else "no Host header")
        if method == "POST":
            line += (f", Origin {self._shown(origin)}" if origin is not None
                     else ", no Origin header")
            line += f", {'with' if header else 'without'} {WRITE_HEADER}"
        sys.stderr.write(line + ", would pass\n")


def guard_from_config(config: dict, bind_host: str | None) -> WriteGuard:
    """Build the guard from radar.yaml. A mode it cannot read enforces.

    The names always allowed are the loopback names and the address the
    server binds to. dashboard_allowed_hosts adds the rest, the proxy's host
    name and any address or tailnet name Luke reaches the server by.
    """
    raw = config.get("dashboard_guard", "enforce")
    mode = raw.strip().lower() if isinstance(raw, str) else ""
    if mode not in ("warn", "enforce"):
        sys.stderr.write(f"dashboard_guard {raw!r} is neither warn nor "
                         "enforce, so the guard enforces.\n")
        mode = "enforce"
    names = list(LOOPBACK_NAMES)
    if bind_host and bind_host not in ("0.0.0.0", "::"):
        names.append(bind_host)
    extra = config.get("dashboard_allowed_hosts") or []
    if isinstance(extra, str):
        extra = extra.replace(",", " ").split()
    elif not isinstance(extra, (list, tuple)):
        sys.stderr.write(f"dashboard_allowed_hosts {extra!r} is not a list of "
                         "host names, so it is ignored.\n")
        extra = []
    for entry in extra:
        if isinstance(entry, str) and host_name(entry):
            names.append(entry)
        else:
            sys.stderr.write(f"dashboard_allowed_hosts entry {entry!r} is not "
                             "a plain host name, so it is ignored.\n")
    return WriteGuard(mode, names)


# Replaced from radar.yaml in main(). Until then it enforces, loopback only.
GUARD = WriteGuard("enforce", LOOPBACK_NAMES)


class Handler(BaseHTTPRequestHandler):
    server_version = "RadarDashboard/1.0"
    _answered = False

    def end_headers(self):
        # Enforce mode forbids framing on every reply, error pages included.
        # A page on another site could otherwise frame the dashboard and
        # get a click on one of its real buttons, which passes both checks.
        if GUARD.enforcing:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "frame-ancestors 'none'")
        super().end_headers()

    def _send(self, status: int, body: bytes, ctype: str,
              headers: dict | None = None) -> None:
        self._answered = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not GUARD.admit(self, "GET"):
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/dashboard.html"):
            query = parse_qs(urlsplit(self.path).query)
            try:
                if (query.get("part") or [""])[0] == "main":
                    self._send(200, json.dumps(render_home_main(query)).encode(),
                               "application/json")
                else:
                    self._send(200, render("home", query), "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001  a broken render is a 500, not a crash
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
            return
        if path == "/radar":
            try:
                self._send(200, render("radar"), "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
            return
        if path == "/health":
            code, payload = health_payload()
            self._send(code, json.dumps(payload).encode(), "application/json")
            return
        if path.startswith("/company/"):
            raw = path[len("/company/"):].strip("/")
            if not raw.isdigit():
                self._send(404, b"Company ids are numbers.",
                           "text/plain; charset=utf-8")
                return
            # One company page (owner, 17 Nov): the old page is the
            # company's record on the Watchlist now, on its own page.
            self.send_response(302)
            self.send_header("Location", f"/watchlist/{int(raw)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path.startswith("/prepare/") and path.endswith("/cv.docx"):
            # The saved CV as a Word file. Read only, behind the same Host
            # check as every page, and built from what Save CV stored.
            raw = path[len("/prepare/"):-len("/cv.docx")]
            if not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
                self._send(404, b"Role ids are numbers.",
                           "text/plain; charset=utf-8")
                return
            try:
                status, body, ctype, extra = download_cv(int(raw))
            except Exception as err:  # noqa: BLE001
                self._send(500, f"The Word file could not be made. "
                           f"{type(err).__name__}: {err}".encode(),
                           "text/plain; charset=utf-8")
                return
            self._send(status, body, ctype, extra)
            return
        if path.startswith("/prepare/"):
            raw = path[len("/prepare/"):].strip("/")
            # Ascii digits only and short enough for SQLite, because int()
            # takes digits that isdigit() does and SQLite will not hold.
            if not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
                self._send(404, b"Role ids are numbers.",
                           "text/plain; charset=utf-8")
                return
            # The page never spends on opening, however it is opened: the
            # Jobs link, an old address with ?go=1 on it, a bookmark, another
            # site. Its own button, which states the cost first, makes the one
            # POST that may spend (decisions of 15 Nov).
            try:
                page = render_prepare(int(raw))
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
                return
            if page is None:
                self._send(404, b"No role with that id.",
                           "text/plain; charset=utf-8")
                return
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/jobs" or path.startswith("/jobs/"):
            raw = path[len("/jobs"):].strip("/")
            if raw and not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
                self._send(404, b"Role ids are numbers.",
                           "text/plain; charset=utf-8")
                return
            try:
                status, body, ctype = render_jobs(
                    int(raw) if raw else None,
                    parse_qs(urlsplit(self.path).query))
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
                return
            self._send(status, body, ctype)
            return
        if path == "/insights" or path.startswith("/insights/"):
            rest = path[len("/insights"):].strip("/")
            parts = rest.split("/") if rest else []
            if not parts:
                kind, arg = "front", None
            elif parts == ["sources"]:
                kind, arg = "sources", None
            elif len(parts) == 2 and parts[0] == "story" and parts[1].isdigit():
                kind, arg = "story", int(parts[1])
            elif len(parts) == 2 and parts[0] == "section":
                kind, arg = "section", parts[1]
            else:
                self._send(404, b"Nothing here. Insights is at /insights.",
                           "text/plain; charset=utf-8")
                return
            try:
                body = render_insights(kind, arg)
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
                return
            if body is None:
                self._send(404, b"No such story or section. It may have been split or removed.",
                           "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path == "/watchlist/find":
            query = parse_qs(urlsplit(self.path).query)
            text = (query.get("q") or [""])[0][:120]
            try:
                payload = find_companies(text)
            except sqlite3.OperationalError as err:
                payload = {"ok": False, "note": f"database busy, try again. {err}"}
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if path == "/watchlist" or path.startswith("/watchlist/"):
            raw = path[len("/watchlist"):].strip("/")
            query = parse_qs(urlsplit(self.path).query)
            if raw and not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
                self._send(404, b"Nothing here. The Watchlist is at /watchlist.",
                           "text/plain; charset=utf-8")
                return
            picked = raw or (query.get("company") or [""])[0]
            if picked and not (picked.isascii() and picked.isdigit() and len(picked) <= 18):
                picked = ""
            try:
                body = render_watchlist(int(picked) if picked else None,
                                        "record" if raw else "table")
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
                return
            if body is None:
                self._send(404, b"No company with that id. The Watchlist is at /watchlist.",
                           "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if path.startswith("/static/fonts/"):
            name = path[len("/static/fonts/"):]
            target = FONT_DIR / name
            if name in insights_page.FONT_FILES and target.is_file():
                self._send(200, target.read_bytes(), "font/woff2")
            else:
                self._send(404, b"No such font here.", "text/plain; charset=utf-8")
            return
        if path == "/archive":
            try:
                self._send(200, render(path.lstrip("/")),
                           "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
            return
        if path == "/cv/rescore/status":
            self._send(200, json.dumps(cv_screen.rescore_status()).encode(),
                       "application/json")
            return
        if path == "/settings":
            try:
                self._send(200, render_settings_page(), "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
            return
        if path == "/cv/experience" or path.startswith("/cv/experience/"):
            try:
                page = render_experience(path[len("/cv/experience/"):]
                                         if path.startswith("/cv/experience/") else "")
                if page is None:
                    self._send(404, b"No such entry.", "text/plain; charset=utf-8")
                else:
                    self._send(200, page, "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
            return
        if path == "/cv":
            try:
                self._send(200, render_cv_page(parse_qs(urlsplit(self.path).query)),
                           "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
            return
        if path in STATIC:
            rel, ctype = STATIC[path]
            target = REPO_ROOT / rel
            if target.exists():
                self._send(200, target.read_bytes(), ctype)
            else:
                self._send(404, b"Not written yet. Run the suite or a dry run first.",
                           "text/plain; charset=utf-8")
            return
        self._send(404, b"Nothing here. The dashboard lives at /.",
                   "text/plain; charset=utf-8")

    def _body_length(self, cap: int) -> int | None:
        """Content-Length as a safe int, None when the header is hostile.

        The header is client input. Negative values would turn
        rfile.read into read-to-EOF and pin a handler thread forever on a
        keep-alive connection, and a non-numeric value would raise. Both
        get a clean refusal instead.
        """
        raw = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw)
        except (ValueError, TypeError):
            return None
        if length < 0 or length > cap:
            return None
        return length

    def _drain_body(self) -> None:
        """Read and drop the request body, so a refusal arrives cleanly.

        Closing a socket that still holds unread request data resets the
        connection, and the browser then reports a network failure instead
        of showing the refusal. The body is read in small pieces and thrown
        away, so a large declared length holds no memory, and a client that
        stalls is dropped after a few seconds. Capped at the largest upload
        the server takes.
        """
        left = self._body_length(cv_store.MAX_UPLOAD_BYTES)
        if not left:
            return
        try:
            self.connection.settimeout(10)
            while left > 0:
                piece = self.rfile.read(min(left, 65536))
                if not piece:
                    break
                left -= len(piece)
        except OSError:
            pass

    def _read_json_body(self) -> dict | None:
        length = self._body_length(1_000_000)
        if length is None:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return None

    def do_POST(self):
        self._answered = False
        try:
            if not GUARD.admit(self, "POST"):
                return
            self._route_post()
        except ConnectionError as err:
            # The caller went away, so there is nobody to tell. One line, so
            # a reset raised by route code does not pass unseen.
            sys.stderr.write(f"POST {self.path[:120]!r} ended early with "
                             f"{type(err).__name__}\n")
        except Exception as err:  # noqa: BLE001  one clear 500
            # Log the raw request target, not a parsed one, because a target
            # the router could not parse is exactly what may have got us here.
            sys.stderr.write(f"POST {self.path[:120]!r} failed with "
                             f"{type(err).__name__}\n"
                             + traceback.format_exc())
            if not self._answered:
                note = (f"Server error ({type(err).__name__}). The request "
                        "may not have completed. The dashboard log has the "
                        "details.")
                try:
                    self._send(500, json.dumps(
                        {"ok": False, "note": note}).encode(),
                        "application/json")
                except OSError:
                    pass

    def _route_post(self):
        path = urlsplit(self.path).path
        if path == "/watch":
            result = run_watcher()
            self._send(200 if result["ok"] else 502,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/ack", "/unack"):
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            # bool is a subclass of int in Python, and JSON true would
            # otherwise acknowledge row 1. Integers only, genuinely.
            if not isinstance(item_id, int) or isinstance(item_id, bool):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            result = set_acknowledged(item_id, path == "/ack")
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path == "/read":
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            # Integers only, for the same bool-subclass reason as /ack.
            if (not isinstance(item_id, int) or isinstance(item_id, bool)
                    or not -2 ** 63 <= item_id < 2 ** 63):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            result = set_read(item_id)
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/close", "/unclose", "/dismiss", "/undismiss"):
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            # Integers only, for the same bool-subclass reason as /ack.
            if not isinstance(item_id, int) or isinstance(item_id, bool):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            result = set_lifecycle(item_id, path[1:])
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/apply", "/unapply"):
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            # Integers only, for the same bool-subclass reason as /ack, and
            # inside what SQLite can hold, so that an absurd id is a clear
            # 400 and not an OverflowError in the log.
            if (not isinstance(item_id, int) or isinstance(item_id, bool)
                    or not -2 ** 63 <= item_id < 2 ** 63):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            try:
                cv = (clean_cv_reference(body.get("cv"))
                      if path == "/apply" else None)
            except ValueError as err:
                self._send(400, json.dumps(
                    {"ok": False, "note": str(err)}).encode(),
                    "application/json")
                return
            result = set_applied(item_id, path == "/apply", cv)
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/application/move", "/application/next"):
            body = self._read_json_body()
            app_id = body.get("id") if isinstance(body, dict) else None
            # The same id rule as /apply: a real integer that SQLite can hold.
            if (not isinstance(app_id, int) or isinstance(app_id, bool)
                    or not -2 ** 63 <= app_id < 2 ** 63):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            if path == "/application/move":
                to, reason, expect = (body.get("to"), body.get("reason"), body.get("from"))
                if not all(v is None or isinstance(v, str) for v in (to, reason, expect)):
                    self._send(400, b'{"ok": false, "note": "to, reason and from must be text"}',
                               "application/json")
                    return
                result = move_application(app_id, to, reason, expect,
                                          undo=body.get("undo") is True)
            else:
                result = set_application_next(app_id, body.get("text"), body.get("due"),
                                              body.get("interview"))
            status = (200 if result.get("ok") else
                      400 if result.get("invalid") else 409)
            self._send(status, json.dumps(result).encode(), "application/json")
            return
        if path in ("/home/done", "/home/undone"):
            body = self._read_json_body()
            title = body.get("title") if isinstance(body, dict) else None
            try:
                key = home_page.clean_key(body.get("key") if isinstance(body, dict) else None)
                if title is not None and not isinstance(title, str):
                    raise ValueError("title must be text")
            except ValueError as err:
                self._send(400, json.dumps({"ok": False, "note": str(err)}).encode(),
                           "application/json")
                return
            result = set_home_done(key, path == "/home/done", title)
            self._send(200 if result.get("ok") else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path == "/prepare":
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            # The same id rule as /apply: a real integer that SQLite can hold.
            if (not isinstance(item_id, int) or isinstance(item_id, bool)
                    or not -2 ** 63 <= item_id < 2 ** 63):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            result = run_prepare(item_id)
            self._send(200 if result.get("ok") else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/prepare/cv/open", "/prepare/cv/save",
                    "/prepare/cv/reset", "/prepare/cv/rescore"):
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            # The same id rule as /prepare.
            if (not isinstance(item_id, int) or isinstance(item_id, bool)
                    or not -2 ** 63 <= item_id < 2 ** 63):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            action = path.rsplit("/", 1)[1]
            text = body.get("text")
            if action in ("save", "rescore") and not isinstance(text, str):
                self._send(400, b'{"ok": false, "note": "text must be the CV as text"}',
                           "application/json")
                return
            try:
                base = clean_copy_base(body.get("base"))
            except ValueError as err:
                self._send(400, json.dumps(
                    {"ok": False, "note": str(err)}).encode(),
                    "application/json")
                return
            result = {"open": lambda: run_copy_open(item_id),
                      "save": lambda: run_copy_save(item_id, text, base),
                      "reset": lambda: run_copy_reset(item_id),
                      "rescore": lambda: run_copy_rescore(item_id, text, base)}[action]()
            status = (200 if result.get("ok") else
                      400 if result.get("invalid") else 409)
            self._send(status, json.dumps(result).encode(), "application/json")
            return
        if path in ("/cv/experience/save", "/cv/experience/delete", "/cv/experience/paste"):
            body = self._read_json_body()
            if not isinstance(body, dict):
                self._send(400, b'{"ok": false, "note": "send JSON"}', "application/json")
                return
            now = radar_common.now_iso()
            if path == "/cv/experience/save":
                result = experience_write(lambda c: experience.save_entry(c, body, now))
            elif path == "/cv/experience/paste":
                result = experience_write(lambda c: experience.add_pasted(c, body.get("text"), now))
            else:
                eid = _int(body, "id")
                if eid is None:
                    self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                               "application/json")
                    return
                result = experience_write(lambda c: experience.delete_entry(c, eid))
            status = (200 if result.get("ok") else 422 if result.get("invalid")
                      else 404 if result.get("gone") else 409)
            self._send(status, json.dumps(result).encode(), "application/json")
            return
        if path == "/cv/master/save":
            body = self._read_json_body()
            if not (isinstance(body, dict) and isinstance(body.get("text"), str)):
                self._send(400, b'{"ok": false, "note": "text must be the CV as text"}',
                           "application/json")
                return
            result = save_master_cv(body["text"], body.get("base"))
            self._send(200 if result.get("ok") else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/cv/preferences", "/cv/preferences/section", "/cv/design"):
            body = self._read_json_body()
            result = {"/cv/design": save_cv_design,
                      "/cv/preferences": save_cv_preferences,
                      "/cv/preferences/section": save_cv_section}[path](body)
            status = 200 if result.get("ok") else 422 if result.get("invalid") else 409
            self._send(status, json.dumps(result).encode(), "application/json")
            return
        if path == "/profile/setting":
            body = self._read_json_body()
            key = body.get("key") if isinstance(body, dict) else None
            value = body.get("value") if isinstance(body, dict) else None
            try:
                stored = radar_common.update_pref_line(str(key or ""),
                                                       str(value or ""))
                self._send(200, json.dumps(
                    {"ok": True, "key": key, "value": stored}).encode(),
                    "application/json")
            except ValueError as err:
                self._send(422, json.dumps(
                    {"ok": False, "note": str(err)}).encode(),
                    "application/json")
            return
        if path == "/touch/outcome":
            body = self._read_json_body()
            tid = body.get("id") if isinstance(body, dict) else None
            if not isinstance(tid, int) or isinstance(tid, bool):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            result = set_touch_outcome(tid, str(body.get("outcome") or ""))
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path == "/company/state":
            body = self._read_json_body()
            cid = body.get("id") if isinstance(body, dict) else None
            state = body.get("state") if isinstance(body, dict) else None
            if not isinstance(cid, int) or isinstance(cid, bool):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            result = set_company_state(cid, str(state or ""),
                                       bool(body.get("confirm")))
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/insights/sources/look", "/insights/sources/add",
                    "/insights/sources/pause", "/insights/sources/remove"):
            body = self._read_json_body()
            if not isinstance(body, dict):
                self._send(400, b'{"ok": false, "note": "send JSON"}', "application/json")
                return
            result = sources_write(path, body)
            status = (200 if result.get("ok") else 422 if result.get("invalid")
                      else 404 if result.get("gone") else 409)
            self._send(status, json.dumps(result).encode(), "application/json")
            return
        if path in ("/jobs/add", "/jobs/edit-role"):
            body = self._read_json_body()
            if not isinstance(body, dict):
                self._send(400, b'{"ok": false, "note": "send JSON"}', "application/json")
                return
            result = add_hand_role(body) if path == "/jobs/add" else edit_hand_role(body)
            status = (200 if result.get("ok") else 422 if result.get("invalid")
                      else 404 if result.get("gone") else 409)
            self._send(status, json.dumps(result).encode(), "application/json")
            return
        if path == "/jobs/source":
            body = self._read_json_body()
            name = body.get("name") if isinstance(body, dict) else None
            sender = body.get("sender") if isinstance(body, dict) else None
            url = body.get("url") if isinstance(body, dict) else None
            try:
                added = radar_common.add_job_source(str(name or ""),
                                                    str(sender or ""),
                                                    str(url or ""))
                self._send(200, json.dumps({"ok": True, **added}).encode(),
                           "application/json")
            except ValueError as err:
                self._send(422, json.dumps(
                    {"ok": False, "note": str(err)}).encode(),
                    "application/json")
            return
        if path in ("/story/hide", "/story/show", "/story/split"):
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            if (not isinstance(item_id, int) or isinstance(item_id, bool)
                    or not -2 ** 63 <= item_id < 2 ** 63):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            if path == "/story/split":
                result = split_story(item_id)
            else:
                result = set_story_hidden(item_id, path == "/story/hide")
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in WATCHLIST_POSTS:
            body = self._read_json_body()
            if not isinstance(body, dict):
                self._send(400, b'{"ok": false, "note": "send a JSON object"}',
                           "application/json")
                return
            result, bad = watchlist_post(path, body)
            if bad:
                self._send(400, json.dumps({"ok": False, "note": bad}).encode(),
                           "application/json")
                return
            self._send(200 if result.get("ok") or result.get("confirm") else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/sig/done", "/sig/ack", "/sig/unack"):
            body = self._read_json_body()
            item_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(item_id, int) or isinstance(item_id, bool):
                self._send(400, b'{"ok": false, "note": "id must be an integer"}',
                           "application/json")
                return
            result = set_signal_state(item_id, path.rsplit("/", 1)[1])
            self._send(200 if result["ok"] else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path == "/cv/preview":
            try:
                length = self._body_length(cv_store.MAX_UPLOAD_BYTES)
                if length is None:
                    raise cv_store.UploadError(
                        "The upload's Content-Length is missing, negative "
                        "or bigger than any CV needs to be.")
                data = self.rfile.read(length)
                filename = self.headers.get("X-Filename") or ""
                markdown = cv_store.extract_markdown(filename, data)
                token = cv_store.stage(markdown)
                try:
                    active_text = score_item.load_cv_text(radar_common.load_config())[0]
                except (SystemExit, OSError):
                    active_text = None
                report = cv_screen.import_report(markdown, active_text)
                self._send(200, json.dumps(
                    {"ok": True, "token": token, "markdown": markdown,
                     "filename": filename, "report": report}).encode(),
                    "application/json")
            except cv_store.UploadError as err:
                self._send(422, json.dumps(
                    {"ok": False, "note": str(err)}).encode(),
                    "application/json")
            return
        if path == "/cv/rescore":
            body = self._read_json_body()
            if not (isinstance(body, dict) and body.get("confirm") is True):
                self._send(400, json.dumps(
                    {"ok": False, "note": "re-scoring spends real tokens, "
                     "send {\"confirm\": true} to mean it"}).encode(),
                    "application/json")
                return
            result = run_cv_rescore(body)
            self._send(200 if result.get("ok") else 400 if result.get("invalid") else 409,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/cv/confirm", "/cv/discard"):
            body = self._read_json_body()
            token = body.get("token") if isinstance(body, dict) else None
            try:
                if path == "/cv/confirm":
                    result = cv_store.confirm(str(token or ""),
                                              laid_out=isinstance(body, dict)
                                              and body.get("laid_out") is True)
                    self._send(200, json.dumps(
                        {"ok": True, **result}).encode(), "application/json")
                else:
                    cv_store.discard(str(token or ""))
                    self._send(200, b'{"ok": true}', "application/json")
            except cv_store.UploadError as err:
                self._send(409, json.dumps(
                    {"ok": False, "note": str(err)}).encode(),
                    "application/json")
            return
        self._send(404, b"Nothing here.", "text/plain; charset=utf-8")

    def log_message(self, fmt, *log_args):  # one quiet line per request
        sys.stderr.write(f"{self.address_string()} {fmt % log_args}\n")


def main(argv=None) -> int:
    global ARGS, GUARD
    parser = argparse.ArgumentParser(
        description="Serve the Radar dashboard, re-rendered on every load.")
    parser.add_argument("--host", default=None,
                        help="bind address, overrides dashboard_host in radar.yaml")
    parser.add_argument("--port", type=int, default=None,
                        help="port, overrides dashboard_port in radar.yaml")
    parser.add_argument("--db", help="database path override")
    parser.add_argument("--push", action="store_true",
                        help="Check now runs the watcher in push mode")
    ARGS = parser.parse_args(argv)

    radar_common.load_env()
    config = radar_common.load_config()
    if ARGS.host is None:
        ARGS.host = str(config.get("dashboard_host", "127.0.0.1"))
    if ARGS.port is None:
        ARGS.port = int(config.get("dashboard_port", 8787))
    GUARD = guard_from_config(config, ARGS.host)
    # Apply schema and migrations once up front, so read-only renders of an
    # older database see every column the page expects.
    radar_common.get_db(db_path()).close()
    try:
        server = RadarServer((ARGS.host, ARGS.port), Handler)
    except OSError:
        print(f"Port {ARGS.port} is already taken, probably by another copy "
              "of this server. Use the one that's running, or pick another "
              "port with --port.", file=sys.stderr)
        return 2
    mode = "push" if ARGS.push else "dry run"
    print(f"Radar dashboard at http://{ARGS.host}:{ARGS.port}/ "
          f"(watcher button runs in {mode} mode, Ctrl+C to stop)")
    print(f"Write guard is on {GUARD.mode}"
          + (", so it logs what it would refuse and refuses nothing"
             if not GUARD.enforcing else "")
          + f". It answers to {', '.join(sorted(GUARD.names))}.",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
