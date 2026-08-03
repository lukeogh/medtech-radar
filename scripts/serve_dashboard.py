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
actions. Acknowledge and its undo land on POST /ack and /unack, and the
CV update section lives at /cv. Page renders stay read only, the same
guarantee as build_dashboard.py. Writes open their own short-lived
connection through radar_common.get_db, which also applies migrations.

The LAN-trust assumption, stated plainly. Anyone who can reach this port
can read scored opportunities and acknowledge rows. Binding defaults to
dashboard_host and dashboard_port in radar.yaml, 127.0.0.1 unless
changed there. Open it to the LAN or tailnet only behind that trust, and
if it ever goes behind the reverse proxy, give it authentication.

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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


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
    """
    try:
        conn = radar_common.get_db(db_path())
    except sqlite3.OperationalError as err:
        return {"ok": False, "note": f"database busy, try again. {err}"}
    try:
        if on:
            cur = conn.execute(
                "UPDATE opportunities SET acknowledged_at = ?"
                " WHERE id = ? AND acknowledged_at IS NULL",
                (radar_common.now_iso(), item_id))
        else:
            cur = conn.execute(
                "UPDATE opportunities SET acknowledged_at = NULL,"
                " status = CASE WHEN status IN ('actioned','dead')"
                "              THEN 'new' ELSE status END,"
                " status_changed_at = CASE WHEN status IN ('actioned','dead')"
                "              THEN ? ELSE status_changed_at END"
                " WHERE id = ? AND acknowledged_at IS NOT NULL",
                (radar_common.now_iso(), item_id))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False,
                    "note": f"no opportunity {item_id} in the state that "
                            "action expects, maybe already handled"}
        return {"ok": True, "id": item_id,
                "acknowledged": bool(on)}
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


class Handler(BaseHTTPRequestHandler):
    server_version = "RadarDashboard/1.0"

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/dashboard.html"):
            try:
                self._send(200, render("home"), "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001  a broken render is a 500, not a crash
                self._send(500, f"Render failed. {type(err).__name__}: {err}"
                           .encode(), "text/plain; charset=utf-8")
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

    def _read_json_body(self) -> dict | None:
        length = self._body_length(1_000_000)
        if length is None:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return None

    def do_POST(self):
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
    global ARGS
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
