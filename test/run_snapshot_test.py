#!/usr/bin/env python
"""Page snapshots: every page and main view, drawn the way the server draws it,
compared with a stored copy in test/snapshots/. A build can then say exactly
which pages it changed and which it left alone.

    python test/run_snapshot_test.py             check: the suite runs this, and
                                                 it fails with the pages that differ
    python test/run_snapshot_test.py --summary   one line: the pages changed and
                                                 the count unchanged; always exits 0
    python test/run_snapshot_test.py --diff NAME the stored copy against today's
                                                 page, for one view
    python test/run_snapshot_test.py --update    rewrite the stored copies. Only
                                                 ever on purpose, after reading
                                                 what changed

How a page is drawn. One database, test/snapshot_radar.sqlite, is built fresh
on every run from the three invented samples (seed_jobs_sample,
seed_watchlist_sample and seed_insights_sample), at one fixed clock, FROZEN
below, 13 Nov 2026 09:14 UTC. The real server (serve_dashboard.Handler) runs on
a loopback port and every view is fetched through it with a GET, so the
routing, the 200 and the content type are part of what is checked. RADAR_MOCK
is on, and any connection that is not to the loopback address is refused, so
nothing reaches the network.

What is held still, and how (the brief's "fixed at source or masked"):
  - The clock. datetime in every module under scripts/ and in the three seeds
    is a subclass whose now(), utcnow() and today() return FROZEN, naive ones
    in UTC. A module imported after the clock was set would escape it, so the
    run fails if one turns up.
  - The database's path, which pages print: always test/snapshot_radar.sqlite.
  - The profile. config/profile is personal and differs per machine, so the
    CV and the preferences come from test/snapshot_profile/, written fresh each
    run: the fixture CV (test/fixtures/profile_snapshot.md) as one dated
    version with the cv.active marker, and the preferences template. Every
    file there has its modified time set to FROZEN. radar.yaml's prefs_file
    and cv_file are pointed there; nothing else in radar.yaml is touched.
  - config/job_sources.yaml, personal sources the Jobs page writes, is pointed
    at a file that does not exist.
  - The archive's footer links to test/last_digest.html and
    test/last_signal.txt only when those files exist, and other runners write
    them, so the archive is drawn as if neither exists.
  - The Host header is always 127.0.0.1:8787, never the loopback port the run
    happened to get.
Nothing is masked in the stored copies: after the above, two runs give the
same bytes (MASKS below is empty, and the run says so when it is not).

  - radar.yaml's own addresses (n8n_url, ntfy_url, aggregator_email,
    digest_to, dashboard_allowed_hosts) are swapped for invented example.test
    ones, so the public repo's stored copies carry none of the owner's, and
    cv_doc.OWNER is the fixture CV's invented owner.

What the stored copies hold. The page as served, with each <style> and
<script> block's content replaced by a short hash of it and its length, so a
style or script change still shows as a change but the copies stay small
enough to read in a diff, and a line break added before each block-level
opening tag (always added, never merged with one the page has, so two
different pages are never stored alike).

No model calls, no network, and it writes only test/snapshot_radar.sqlite,
test/snapshot_profile/ and, with --update, test/snapshots/.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import http.client
import os
import re
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ["RADAR_MOCK"] = "1"

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TEST_DIR))

SNAP_DIR = TEST_DIR / "snapshots"
DB = TEST_DIR / "snapshot_radar.sqlite"
PROFILE = TEST_DIR / "snapshot_profile"
FIXTURE_CV = TEST_DIR / "fixtures" / "profile_snapshot.md"
CV_NAME = "cv-20261029-091500.md"     # the version the Jobs sample scored against
HOST = "127.0.0.1:8787"

# Substitutions made on every page before it is compared, as (pattern,
# replacement, why). Empty: nothing varies once the sources above are fixed.
MASKS: list[tuple[str, str, str]] = []


# ================================================================ no network

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _loopback_only(address) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ConnectionRefusedError(
            f"the snapshot runner allows no network; refused a connection to {host!r}")


def _connect(self, address):
    _loopback_only(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _loopback_only(address)
    return _real_connect_ex(self, address)


socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex


# ================================================================ the clock

class FrozenDatetime(datetime):
    """datetime with now() held at FROZEN. Naive moments read as UTC, so the
    machine's own time zone never shows."""

    @classmethod
    def now(cls, tz=None):
        moment = FROZEN.astimezone(tz) if tz is not None else FROZEN.replace(tzinfo=None)
        return cls._of(moment)

    @classmethod
    def utcnow(cls):
        return cls._of(FROZEN.replace(tzinfo=None))

    @classmethod
    def today(cls):
        return cls.utcnow()

    @classmethod
    def _of(cls, moment: datetime) -> "FrozenDatetime":
        return cls(moment.year, moment.month, moment.day, moment.hour, moment.minute,
                   moment.second, moment.microsecond, moment.tzinfo)


# 13 Nov 2026, 09:14 UTC: the Jobs design's "today", in the UK morning. A
# FrozenDatetime itself, so a module's isinstance(x, datetime), with datetime
# swapped for the subclass, holds for it as for every moment made from it.
FROZEN = FrozenDatetime(2026, 11, 13, 9, 14, tzinfo=timezone.utc)


# Every module a page can reach, imported now so that the clock reaches them
# all. serve_dashboard imports most; the rest are imported inside functions.
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402
import board_page  # noqa: E402,F401
import build_dashboard  # noqa: E402,F401
import cv_doc  # noqa: E402
import cv_screen  # noqa: E402,F401
import cv_store  # noqa: E402
import engagement  # noqa: E402,F401
import home_page  # noqa: E402
import insights_page  # noqa: E402,F401
import jobs_page  # noqa: E402,F401
import page_frame  # noqa: E402,F401
import prepare_page  # noqa: E402,F401
import score_item  # noqa: E402
import watchlist  # noqa: E402,F401
import watchlist_page  # noqa: E402,F401
import seed_insights_sample  # noqa: E402
import seed_jobs_sample  # noqa: E402
import seed_watchlist_sample  # noqa: E402

for _name in sorted(n for n in os.listdir(SCRIPTS) if n.endswith(".py")):
    # Every script that imports cleanly, so a page's lazy import finds its
    # module already loaded, and already on the fixed clock.
    _mod = _name[:-3]
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except Exception:  # noqa: BLE001  a script that will not import draws no page
            pass


def _ours(module) -> bool:
    path = getattr(module, "__file__", None) or ""
    try:
        Path(path).resolve().relative_to(REPO_ROOT)
    except ValueError:
        return False
    return path.endswith(".py") and "snapshot" not in Path(path).name


def fix_clock() -> list[str]:
    """Point every one of our modules' datetime at FrozenDatetime. Returns the
    module names it changed."""
    fixed = []
    for name, module in list(sys.modules.items()):
        if module is not None and _ours(module) and getattr(module, "datetime", None) is datetime:
            module.datetime = FrozenDatetime
            fixed.append(name)
    return fixed


def clock_escapes() -> list[str]:
    """Our modules that still read the real clock: any loaded after fix_clock."""
    return sorted(name for name, module in sys.modules.items()
                  if module is not None and _ours(module)
                  and getattr(module, "datetime", None) is datetime)


# ================================================================ the inputs

def write_profile() -> None:
    """test/snapshot_profile/: the fixture CV as one version, marked active,
    and the preferences template, every file dated FROZEN."""
    shutil.rmtree(PROFILE, ignore_errors=True)
    PROFILE.mkdir(parents=True)
    (PROFILE / CV_NAME).write_text(FIXTURE_CV.read_text(encoding="utf-8"), encoding="utf-8",
                                   newline="\n")
    (PROFILE / cv_store.MARKER_NAME).write_text(CV_NAME + "\n", encoding="utf-8", newline="\n")
    template = REPO_ROOT / "config" / "preferences.template.md"
    (PROFILE / "preferences.md").write_text(template.read_text(encoding="utf-8"),
                                            encoding="utf-8", newline="\n")
    stamp = FROZEN.timestamp()
    for path in PROFILE.iterdir():
        os.utime(path, (stamp, stamp))


_real_load_config = rc.load_config
PROFILE_REL = PROFILE.relative_to(REPO_ROOT).as_posix()


# radar.yaml's own addresses, swapped for invented ones wherever radar.yaml
# sets them: the repo is public, and the stored copies should neither carry the
# owner's hosts or emails nor change when one of them does.
INVENTED = {
    "n8n_url": "https://n8n.radar.example.test",
    "ntfy_url": "https://ntfy.radar.example.test",
    "aggregator_email": "radar-inbox@example.test",
    "digest_to": "digest@example.test",
    "dashboard_allowed_hosts": ["radar.example.test"],
}


def snapshot_config(*args, **kwargs) -> dict:
    config = dict(_real_load_config(*args, **kwargs))
    config["prefs_file"] = f"{PROFILE_REL}/preferences.md"
    config["cv_file"] = f"{PROFILE_REL}/cv.pdf"          # never written: no PDF
    for key, value in INVENTED.items():
        if config.get(key):
            config[key] = value
    return config


def point_inputs() -> None:
    rc.load_config = snapshot_config
    cv_doc.OWNER = "Alex Rennie"   # the fixture CV's invented owner, as the CV runner sets it
    cv_store.PROFILE_DIR = PROFILE
    score_item.PROFILE_DIR = PROFILE
    rc.JOB_SOURCES_PATH = PROFILE / "job_sources.yaml"   # never written
    # The archive footer's dry-run links, as if no runner had written them.
    real_exists = Path.exists
    hidden = {(REPO_ROOT / "test" / "last_digest.html").resolve(),
              (REPO_ROOT / "test" / "last_signal.txt").resolve()}

    def exists(self, *a, **k):
        if self.is_absolute() and self in hidden:
            return False
        return real_exists(self, *a, **k)
    build_dashboard_exists_patch(exists)


def build_dashboard_exists_patch(exists) -> None:
    """Path.exists for the archive footer only: build_dashboard's render_page
    is wrapped so the patch lives just while it draws."""
    real_render = build_dashboard.render_page

    def render_page(*args, **kwargs):
        saved = Path.exists
        Path.exists = exists
        try:
            return real_render(*args, **kwargs)
        finally:
            Path.exists = saved
    build_dashboard.render_page = render_page


def build_db() -> dict:
    """test/snapshot_radar.sqlite from the three samples, at FROZEN. Returns
    the ids the views need."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(DB) + suffix).unlink(missing_ok=True)
    seed_jobs_sample.SHIFT = (FROZEN.replace(hour=0, minute=0, second=0, microsecond=0)
                              - seed_jobs_sample.DESIGN_TODAY)
    ids = dict(seed_jobs_sample.seed(DB))
    conn = rc.get_db(DB)
    try:
        # Insights before the Watchlist: the Insights sample keeps a company's
        # people as plain names, the Watchlist sample (and enrichment) as
        # {name, role}, and the record reads only the second.
        seed_insights_sample.seed(conn, FROZEN.replace(second=0, microsecond=0))
        companies = seed_watchlist_sample.seed(conn, FROZEN)
        conn.commit()
        ids["company"] = companies["Hartwell Infusion Ltd"]
        # A company not on the Watchlist, reached from a role on Jobs: the Top
        # prospect's company where it is unwatched, else the first unwatched.
        row = conn.execute(
            "SELECT c.id FROM companies c JOIN opportunities o ON o.company_id = c.id"
            " WHERE o.id = ? AND c.watched_at IS NULL", (ids["T1"],)).fetchone() or conn.execute(
            "SELECT id FROM companies WHERE watched_at IS NULL ORDER BY id LIMIT 1").fetchone()
        ids["unwatched"] = row[0]
    finally:
        conn.close()
    return ids


# ================================================================ the views

def views(ids: dict) -> list[tuple[str, str]]:
    """(name, address) for every page and main view. The story is found on
    the Insights front page once it is drawn, so it is added later."""
    return [
        ("home-todo", "/"),
        ("home-done", "/?show=done"),
        ("jobs-open", "/jobs"),
        ("jobs-reading-open", "/jobs?reading=1"),
        ("jobs-applied-list", "/jobs?show=applied&layout=list"),
        ("jobs-applied-board", "/jobs?show=applied"),
        ("jobs-acknowledged", "/jobs?show=acknowledged"),
        ("jobs-filtered", "/jobs?show=filtered"),
        ("role-top", f"/jobs/{ids['T1']}"),
        ("role-question", f"/jobs/{ids['Q1']}"),
        ("role-reading", f"/jobs/{ids['R1']}"),
        ("role-filtered", f"/jobs/{ids['F1']}?show=filtered"),
        ("role-applied", f"/jobs/{ids['A2']}?show=applied"),
        ("prepare", f"/prepare/{ids['T1']}"),
        ("insights", "/insights"),
        ("watchlist", "/watchlist"),
        ("watchlist-company", f"/watchlist/{ids['company']}"),
        ("company-unwatched", f"/watchlist/{ids['unwatched']}"),
        ("cv", "/cv"),
        ("radar-health", "/radar"),
        ("archive", "/archive"),
    ]


def fetch(port: int, address: str) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request("GET", address, headers={"Host": HOST})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        ctype = resp.getheader("Content-Type") or ""
    finally:
        conn.close()
    if resp.status != 200 or not ctype.startswith("text/html"):
        raise RuntimeError(f"GET {address} gave {resp.status} {ctype}: {body[:300]}")
    return body


_BLOCK = re.compile(r"(<(style|script)\b[^>]*>)(.*?)(</\2>)", re.S | re.I)
# A line break goes in before each of these opening tags, so a diff shows the
# element that changed rather than one line of fifty thousand characters. One
# is always added, never folded into a break the page already has, so no two
# different pages are ever stored alike.
_BREAK = re.compile(
    r"(?=<(?:html|head|body|meta|link|title|div|section|article|aside|header|footer|nav|main"
    r"|ul|ol|li|dl|dt|dd|p|h[1-6]|table|thead|tbody|tfoot|tr|td|th|caption|form|fieldset"
    r"|legend|details|summary|dialog|figure|figcaption|blockquote|pre|hr|button|select"
    r"|option|textarea|label|input|style|script|template|svg|noscript)[\s>/])", re.I)


def stored_form(page: str) -> str:
    """The page as kept: each style and script block's content as a hash."""
    def hashed(m: re.Match) -> str:
        body = m.group(3)
        if not body.strip():
            return m.group(0)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        return f"{m.group(1)}/* {m.group(2).lower()} {digest}, {len(body)} chars */{m.group(4)}"
    text = _BREAK.sub("\n", _BLOCK.sub(hashed, page))
    for pattern, replacement, _why in MASKS:
        text = re.sub(pattern, replacement, text)
    text = text.replace("\r\n", "\n")
    return text if text.endswith("\n") else text + "\n"


def render_all() -> dict[str, str]:
    """Every view's stored form, by name, in the order views() lists them."""
    write_profile()
    point_inputs()
    fixed = fix_clock()
    if not fixed:
        raise RuntimeError("the clock reached no module")
    ids = build_db()
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message)
    serve_dashboard.ARGS = argparse.Namespace(db=str(DB), push=False,
                                              host="127.0.0.1", port=8787)
    serve_dashboard.GUARD = serve_dashboard.WriteGuard("enforce", serve_dashboard.LOOPBACK_NAMES)
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pages: dict[str, str] = {}
    try:
        port = server.server_address[1]
        # Done on Home: the first item on the list, taken off by the same
        # function the Done button's route calls, so Done has one item.
        first = re.search(r'data-done="([^"]+)"', fetch(port, "/"))
        if first is None:
            raise RuntimeError("Home drew no item with a Done button")
        conn = rc.get_db(DB)
        try:
            home_page.mark_done(conn, first.group(1), "the first item on Home")
        finally:
            conn.close()
        for name, address in views(ids):
            pages[name] = fetch(port, address)
            if name == "insights":
                story = re.search(r'href="/insights/story/(\d+)"', pages[name])
                if story is None:
                    raise RuntimeError("the Insights front page links to no story")
                pages["insights-story"] = fetch(port, f"/insights/story/{story.group(1)}")
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD,
         serve_dashboard.Handler.log_message) = saved
    escaped = clock_escapes()
    if escaped:
        raise RuntimeError("these modules were loaded after the clock was fixed and read the "
                           f"real one: {', '.join(escaped)}. Import them at the top of the runner.")
    return {name: stored_form(page) for name, page in pages.items()}


# ================================================================ compare

def stored(name: str) -> str | None:
    path = SNAP_DIR / f"{name}.html"
    return path.read_text(encoding="utf-8") if path.exists() else None


def compare(pages: dict[str, str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """(changed, unchanged, new, gone): new has no stored copy yet, gone is a
    stored copy no view draws any more."""
    changed, unchanged, new = [], [], []
    for name, text in pages.items():
        old = stored(name)
        if old is None:
            new.append(name)
        elif old == text:
            unchanged.append(name)
        else:
            changed.append(name)
    on_disk = {p.stem for p in SNAP_DIR.glob("*.html")} if SNAP_DIR.exists() else set()
    gone = sorted(on_disk - set(pages))
    return changed, unchanged, new, gone


def first_difference(old: str, new: str) -> str:
    a, b = old.splitlines(), new.splitlines()
    for i, (x, y) in enumerate(zip(a, b), 1):
        if x != y:
            return f"line {i}"
    return f"line {min(len(a), len(b)) + 1} (one is longer)"


def unified(name: str, new: str) -> str:
    old = stored(name) or ""
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"test/snapshots/{name}.html (stored)", tofile=f"{name} (drawn now)", n=2))


def main(argv=None) -> int:
    # Pages carry characters a Windows console's code page cannot show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", help="ignored: the suite passes it to every runner; "
                        "this runner always builds test/snapshot_radar.sqlite")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true",
                      help="rewrite the stored copies (on purpose only)")
    mode.add_argument("--summary", action="store_true",
                      help="print one line: pages changed and the count unchanged")
    mode.add_argument("--diff", metavar="NAME", help="show one view's diff against its stored copy")
    args = parser.parse_args(argv)

    started = time.monotonic()
    pages = render_all()
    changed, unchanged, new, gone = compare(pages)
    took = time.monotonic() - started

    if args.summary:
        moved = changed + [f"{n} (new)" for n in new] + [f"{n} (gone)" for n in gone]
        print(f"Page snapshots: {len(moved)} changed"
              + (f" ({', '.join(moved)})" if moved else "")
              + f", {len(unchanged)} unchanged.")
        return 0

    if args.diff:
        if args.diff not in pages:
            print(f"No view called {args.diff}. Views: {', '.join(pages)}", file=sys.stderr)
            return 2
        text = unified(args.diff, pages[args.diff])
        print(text or f"{args.diff} is unchanged.")
        return 0

    if args.update:
        SNAP_DIR.mkdir(exist_ok=True)
        for name, text in pages.items():
            if stored(name) != text:
                (SNAP_DIR / f"{name}.html").write_text(text, encoding="utf-8", newline="\n")
        for name in gone:
            (SNAP_DIR / f"{name}.html").unlink()
        print(f"Updated test/snapshots/: {len(changed)} rewritten"
              + (f" ({', '.join(changed)})" if changed else "")
              + f", {len(new)} added" + (f" ({', '.join(new)})" if new else "")
              + f", {len(gone)} removed" + (f" ({', '.join(gone)})" if gone else "")
              + f", {len(unchanged)} unchanged. {took:.1f}s.")
        return 0

    # Check mode.
    for name in pages:
        if name in unchanged:
            print(f"PASS  {name} is as stored")
        elif name in changed:
            print(f"FAIL  {name} differs from test/snapshots/{name}.html, first at "
                  f"{first_difference(stored(name) or '', pages[name])}")
        else:
            print(f"FAIL  {name} has no stored copy (test/snapshots/{name}.html)")
    for name in gone:
        print(f"FAIL  test/snapshots/{name}.html is stored but no view draws it")
    bad = changed + new + gone
    print(f"\n{len(pages)} views, {len(unchanged)} unchanged, {len(bad)} differ. {took:.1f}s.")
    if bad:
        print("Pages that differ: " + ", ".join(bad))
        print("See one with: python test/run_snapshot_test.py --diff NAME. If every change is "
              "meant, rewrite them with: python test/run_snapshot_test.py --update")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
