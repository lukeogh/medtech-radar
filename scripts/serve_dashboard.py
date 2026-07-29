#!/usr/bin/env python
"""Serve the Radar dashboard in a browser, always fresh.

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

Standard library only, no new dependencies. The database is opened read
only on every render, the same guarantee as build_dashboard.py. The
watcher subprocess runs dry by default and only ever pushes to ntfy when
the server is started with --push, mirroring check_signals.py itself.

Flags:
    --host ADDR   bind address, default 127.0.0.1. If you open this up,
                  put it behind your reverse proxy with authentication,
                  the page carries scored opportunities and names.
    --port N      default 8787
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

REPO_ROOT = radar_common.REPO_ROOT

# The only files served besides the page itself, exactly the footer links.
STATIC = {
    "/test/last_digest.html": ("test/last_digest.html", "text/html; charset=utf-8"),
    "/test/last_signal.txt": ("test/last_signal.txt", "text/plain; charset=utf-8"),
    "/docs/cost-note.md": ("docs/cost-note.md", "text/plain; charset=utf-8"),
}

ARGS = None
WATCH_LOCK = threading.Lock()


def render() -> bytes:
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
    page = build_dashboard.render_page(data, config, db_label,
                                       REPO_ROOT / "dashboard.html", serve=True)
    return page.encode("utf-8")


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
                self._send(200, render(), "text/html; charset=utf-8")
            except Exception as err:  # noqa: BLE001  a broken render is a 500, not a crash
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

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/watch":
            result = run_watcher()
            self._send(200 if result["ok"] else 502,
                       json.dumps(result).encode(), "application/json")
            return
        self._send(404, b"Nothing here.", "text/plain; charset=utf-8")

    def log_message(self, fmt, *log_args):  # one quiet line per request
        sys.stderr.write(f"{self.address_string()} {fmt % log_args}\n")


def main(argv=None) -> int:
    global ARGS
    parser = argparse.ArgumentParser(
        description="Serve the Radar dashboard, re-rendered on every load.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address, default 127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", help="database path override")
    parser.add_argument("--push", action="store_true",
                        help="Check now runs the watcher in push mode")
    ARGS = parser.parse_args(argv)

    radar_common.load_env()
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
