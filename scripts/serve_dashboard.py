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
reads, and the one POST /prepare it makes when a click on a Jobs row opens
it: that fetches the advert's full description once and scores it once
(prepare_application.py has the guards). Page renders stay read only, the same
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
import build_dashboard
import cv_store
import prepare_application
import score_item

REPO_ROOT = radar_common.REPO_ROOT

# The only files served besides the page itself, exactly the footer links.
STATIC = {
    "/test/last_digest.html": ("test/last_digest.html", "text/html; charset=utf-8"),
    "/test/last_signal.txt": ("test/last_signal.txt", "text/plain; charset=utf-8"),
    "/docs/cost-note.md": ("docs/cost-note.md", "text/plain; charset=utf-8"),
}

ARGS = None
WATCH_LOCK = threading.Lock()


def render(page_name: str = "archive") -> bytes:
    config = radar_common.load_config()
    db_path = Path(ARGS.db).resolve() if ARGS.db else radar_common.DB_PATH
    if not db_path.exists():
        radar_common.get_db(db_path).close()
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    data = build_dashboard.collect(conn, config)
    conn.close()
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
    else:
        page = build_dashboard.render_home_page(data, config, db_label)
    return page.encode("utf-8")


def render_company(company_id: int):
    """The dossier for one company, or None when there is no such id."""
    config = radar_common.load_config()
    dbp = Path(ARGS.db).resolve() if ARGS.db else radar_common.DB_PATH
    conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            db_label = dbp.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            db_label = str(dbp)
        page = build_dashboard.render_company_page(conn, company_id, config,
                                                   db_label)
    finally:
        conn.close()
    return page.encode("utf-8") if page else None


def render_prepare(item_id: int, go: bool = False):
    """The Prepare application page for one role, or None for no such id.

    Reads only, on a read-only connection, whatever the address says. The go
    flag only changes what the page's script is told to do when it loads, see
    build_dashboard.render_prepare_page.
    """
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
                                                   db_label, go=go)
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
            "INSERT INTO touches (company, touched_at, channel, note, company_id)"
            " VALUES (?,?,?,?,?)",
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
    """
    out = {"database": "unreachable", "inbox_last_run": None,
           "signals_last_run": None, "status": "down"}
    try:
        dbp = db_path()
        conn = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for key, workflow in (("inbox_last_run", "inbox"),
                                  ("signals_last_run", "signals")):
                row = conn.execute(
                    "SELECT MAX(ts) t FROM runs WHERE workflow = ?",
                    (workflow,)).fetchone()
                out[key] = row["t"] if row else None
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

    now = datetime.now(timezone.utc)
    def hours_since(iso):
        if not iso:
            return None
        try:
            when = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except ValueError:
            return None
        return round((now - when).total_seconds() / 3600, 1)

    out["inbox_hours_ago"] = hours_since(out["inbox_last_run"])
    out["signals_hours_ago"] = hours_since(out["signals_last_run"])
    # The schedules are hourly and two-hourly. Triple them before calling
    # anything wrong, because one missed run is a blip and a watchdog that
    # cries at blips gets muted.
    stale = [name for name, hrs, limit in
             (("inbox", out["inbox_hours_ago"], 3),
              ("signals", out["signals_hours_ago"], 6))
             if hrs is None or hrs > limit]
    out["status"] = "ok" if not stale else "degraded"
    if stale:
        out["stale"] = stale
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
        if on:
            if radar_common.role_is_applied(conn, item_id):
                return {"ok": False,
                        "note": "that role is already marked applied"}
            cur = conn.execute(
                "UPDATE opportunities SET applied_at = ?, applied_cv = ?"
                " WHERE id = ? AND acknowledged_at IS NULL"
                " AND closed_at IS NULL AND dismissed_at IS NULL"
                " AND expired_at IS NULL AND " + radar_common.NOT_APPLIED_SQL,
                (radar_common.now_iso(), cv, item_id))
        else:
            cur = conn.execute(
                "UPDATE opportunities SET applied_at = NULL, applied_cv = NULL"
                " WHERE (id = ? OR (role_key IS NOT NULL AND role_key = ?))"
                " AND applied_at IS NOT NULL",
                (item_id, target["role_key"]))
        conn.commit()
        if cur.rowcount == 0:
            return _refused(conn, item_id)
        return {"ok": True, "id": item_id, "applied": bool(on)}
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    finally:
        conn.close()


CV_SCRIPT = """
(function () {
  var input = document.getElementById('cv-file');
  var preview = document.getElementById('cv-preview');
  var stagePane = document.getElementById('cv-stage');
  var note = document.getElementById('cv-note');
  var token = null;
  function say (text, bad) {
    note.textContent = text;
    note.className = 'cv-note' + (bad ? ' bad' : '');
  }
  function post (url, body, headers) {
    return fetch(url, { method: 'POST', body: body, headers: headers || {} })
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.note || ('HTTP ' + r.status));
        return j;
      }); });
  }
  if (input) input.addEventListener('change', function () {
    var file = input.files && input.files[0];
    if (!file) return;
    say('Extracting ' + file.name + '\\u2026');
    post('/cv/preview', file, { 'X-Filename': file.name })
      .then(function (j) {
        token = j.token;
        preview.textContent = j.markdown;
        stagePane.hidden = false;
        say('Read it. Confirm makes this the active CV, Discard forgets it.');
      })
      .catch(function (err) { say(err.message, true); });
  });
  var confirmBtn = document.getElementById('cv-confirm');
  if (confirmBtn) confirmBtn.addEventListener('click', function () {
    if (!token) return;
    confirmBtn.disabled = true;
    post('/cv/confirm', JSON.stringify({ token: token }),
         { 'Content-Type': 'application/json' })
      .then(function () { location.reload(); })
      .catch(function (err) { confirmBtn.disabled = false; say(err.message, true); });
  });
  var discardBtn = document.getElementById('cv-discard');
  if (discardBtn) discardBtn.addEventListener('click', function () {
    if (!token) return;
    post('/cv/discard', JSON.stringify({ token: token }),
         { 'Content-Type': 'application/json' })
      .then(function () { location.reload(); })
      .catch(function (err) { say(err.message, true); });
  });
  var rescoreBtn = document.getElementById('cv-rescore');
  var rescoreNote = document.getElementById('cv-rescore-note');
  if (rescoreBtn) rescoreBtn.addEventListener('click', function () {
    if (!window.confirm('Re-score up to 25 unacknowledged items against '
        + 'the active CV? This spends real tokens.')) return;
    rescoreBtn.disabled = true;
    rescoreBtn.textContent = 'Re-scoring';
    post('/cv/rescore', JSON.stringify({ confirm: true }),
         { 'Content-Type': 'application/json' })
      .then(function (j) {
        rescoreBtn.disabled = false;
        rescoreBtn.textContent = 'Re-score stale items';
        rescoreNote.textContent = 'Re-scored ' + (j.stale_rescored || 0)
          + ', still stale ' + (j.remaining_stale || 0) + '.';
      })
      .catch(function (err) {
        rescoreBtn.disabled = false;
        rescoreBtn.textContent = 'Re-score stale items';
        rescoreNote.textContent = err.message;
      });
  });
})();
"""

CV_CSS = """
.cv-wrap{max-width:72ch}
.cv-note{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2);margin:var(--space-12) 0}
.cv-note.bad{color:var(--fail)}
.cv-preview{font:400 var(--text-xs)/1.6 var(--font-mono);background:var(--surface);border:1px solid var(--panel-rule);border-radius:var(--radius);padding:var(--space-16);white-space:pre-wrap;max-height:420px;overflow:auto}
.cv-history{font:400 var(--text-sm)/1.9 var(--font-sans);color:var(--ink-2);margin:0;padding-left:1.2em}
input[type=file]{font:400 var(--text-sm)/1.55 var(--font-sans);color:var(--ink-2)}
"""


def render_cv_page() -> bytes:
    """The CV update section. Upload, preview, explicit confirm."""
    config = radar_common.load_config()
    active = cv_store.active_cv_name() or score_item.get_cv_version(config)
    versions = cv_store.history()
    history_html = ("".join(
        f"<li>{build_dashboard.esc(v)}"
        + (" &mdash; active" if v == active else "") + "</li>"
        for v in versions) or "<li>No uploaded versions yet. The scorer is "
        f"reading {build_dashboard.esc(active)}.</li>")
    page = f"""<!doctype html>
<html lang="en-GB" data-appearance="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MedTech Radar. CV update.</title>
<link rel="icon" href="{build_dashboard.FAVICON}">
<style>{build_dashboard.CSS}{CV_CSS}</style>
</head>
<body>
<main class="page cv-wrap">
<header class="masthead"><div>
<div class="brand">{build_dashboard.MARK_SVG}<h1>CV update</h1></div>
<p class="masthead-sub">The scorer reads whichever version the marker
points at. Confirming below moves the marker, every accepted version
stays on disk, and nothing is ever overwritten.</p>
</div>
<div class="controls"><a href="/">Back to the dashboard</a></div>
</header>
<section class="section">
<div class="section-head"><h2>Active version</h2></div>
<p class="section-note">Scores are stamped with the version that shaped
them, visible in each row's detail, so a score that predates a CV change
is obvious.</p>
<div class="panel panel-sage"><p class="empty">The scorer currently reads
<strong>{build_dashboard.esc(active)}</strong>.</p></div>
</section>
<section class="section">
<div class="section-head"><h2>Upload a new CV</h2></div>
<p class="section-note">md, txt, docx or pdf. Anything not already
markdown is extracted to markdown, and nothing becomes active until you
confirm the extracted text below.</p>
<div class="panel panel-sand" style="padding:16px">
<input type="file" id="cv-file" accept=".md,.markdown,.txt,.docx,.pdf">
<p class="cv-note" id="cv-note">Pick a file to see its extracted text.</p>
<div id="cv-stage" hidden>
<pre class="cv-preview" id="cv-preview"></pre>
<p style="display:flex;gap:12px;margin:16px 0 4px">
<button type="button" class="act-btn" id="cv-confirm">Confirm, make it active</button>
<button type="button" class="act-btn" id="cv-discard">Discard</button>
</p>
</div>
</div>
</section>
<section class="section">
<div class="section-head"><h2>History</h2></div>
<p class="section-note">Append only. Old versions stay for the day a
score needs explaining.</p>
<div class="panel" style="padding:12px 16px">
<ul class="cv-history">{history_html}</ul>
</div>
</section>
<section class="section">
<div class="section-head"><h2>After a change</h2></div>
<p class="section-note">Scores made against an older CV stay stamped with
it. This re-scores up to 25 unacknowledged items against the active
version, with a confirm step, and can be run again for the rest.</p>
<div class="panel panel-clay" style="padding:16px">
<button type="button" class="act-btn" id="cv-rescore">Re-score stale items</button>
<p class="cv-note" id="cv-rescore-note"></p>
</div>
</section>
</main>
<script>{build_dashboard.SCRIPT}{CV_SCRIPT}</script>
</body>
</html>"""
    return page.encode("utf-8")


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
                " next_action, next_action_date) VALUES (?,?,?,?,?,?)",
                (row["company"] or "Unknown company", now, channel, note,
                 next_action, next_date))
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


def run_stale_rescore() -> dict:
    """One capped stale-CV re-score pass. Serialised like the watcher."""
    if not WATCH_LOCK.acquire(blocking=False):
        return {"ok": False, "note": "another background run is in progress"}
    try:
        cmd = [sys.executable, str(SCRIPT_DIR / "rescore.py"),
               "--stale-cv", "--cap", "25"]
        if ARGS.db:
            cmd += ["--db", ARGS.db]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", timeout=600,
                              cwd=str(REPO_ROOT))
        if proc.returncode != 0:
            return {"ok": False,
                    "note": (proc.stderr or "").strip()[-400:]}
        raw = (proc.stdout or "").strip()
        start = raw.find("{")
        summary = json.loads(raw[start:]) if start >= 0 else {}
        return {"ok": True, **summary}
    except subprocess.TimeoutExpired:
        return {"ok": False, "note": "the re-score run timed out"}
    except json.JSONDecodeError:
        return {"ok": False, "note": "the re-score run printed no JSON"}
    finally:
        WATCH_LOCK.release()


def run_watcher() -> dict:
    """One watcher pass. Serialised, so two clicks cannot overlap."""
    if not WATCH_LOCK.acquire(blocking=False):
        return {"ok": False, "note": "a check is already running"}
    try:
        cmd = [sys.executable, str(SCRIPT_DIR / "check_signals.py"),
               "--push" if ARGS.push else "--dry-run"]
        if ARGS.db:
            cmd += ["--db", ARGS.db]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", timeout=600,
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

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self._answered = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not GUARD.admit(self, "GET"):
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/dashboard.html"):
            try:
                self._send(200, render("home"), "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001  a broken render is a 500, not a crash
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
            try:
                page = render_company(int(raw))
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
                return
            if page is None:
                self._send(404, b"No company with that id.",
                           "text/plain; charset=utf-8")
                return
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path.startswith("/prepare/"):
            raw = path[len("/prepare/"):].strip("/")
            # Ascii digits only and short enough for SQLite, because int()
            # takes digits that isdigit() does and SQLite will not hold.
            if not (raw.isascii() and raw.isdigit() and len(raw) <= 18):
                self._send(404, b"Role ids are numbers.",
                           "text/plain; charset=utf-8")
                return
            # The click's address starts the work only when the browser says it
            # came from this site, which is what following the Jobs link is. A
            # link on another site, a typed address, a bookmark and a browser
            # that sends no such header all get the page and a button instead,
            # because a GET that spends money must not be one that any other
            # page could send the browser to.
            go = ("1" in parse_qs(urlsplit(self.path).query).get("go", [])
                  and (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                  == "same-origin")
            try:
                page = render_prepare(int(raw), go)
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
        if path in ("/insights", "/jobs", "/archive"):
            try:
                self._send(200, render(path.lstrip("/")),
                           "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
            return
        if path == "/cv":
            try:
                self._send(200, render_cv_page(), "text/html; charset=utf-8")
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
                self._send(200, json.dumps(
                    {"ok": True, "token": token, "markdown": markdown,
                     "filename": filename}).encode(), "application/json")
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
            result = run_stale_rescore()
            self._send(200 if result.get("ok") else 502,
                       json.dumps(result).encode(), "application/json")
            return
        if path in ("/cv/confirm", "/cv/discard"):
            body = self._read_json_body()
            token = body.get("token") if isinstance(body, dict) else None
            try:
                if path == "/cv/confirm":
                    result = cv_store.confirm(str(token or ""))
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
