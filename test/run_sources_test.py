#!/usr/bin/env python
"""Checks for the news watcher's sources (owner, 18 Nov): the safe fetch,
finding a feed, adding, pausing and removing, the news run reading Luke's
sources beside the built-in ones, and the page at /insights/sources.

  python test/run_sources_test.py

RADAR_MOCK=1 and fixtures only: the fetch is replaced (news_sources.FETCH),
the resolver and the connections are fakes, and no site is ever asked.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("RADAR_MOCK", "1")
TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_signals  # noqa: E402
import insights_page  # noqa: E402
import news_sources as ns  # noqa: E402
import page_frame  # noqa: E402
import radar_common as rc  # noqa: E402
import serve_dashboard  # noqa: E402
import sources_page  # noqa: E402

failures: list[str] = []
NOW = "2026-11-18T12:00:00Z"


def check(cond, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


def rss(title: str, items: list[tuple[str, str]]) -> bytes:
    body = "".join(f"<item><title>{t}</title><link>https://news.example/{t.replace(' ', '-')}</link>"
                   f"<guid>https://news.example/{t.replace(' ', '-')}</guid><pubDate>{d}</pubDate></item>"
                   for t, d in items)
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>'
            f"<link>https://news.example/</link>{body}</channel></rss>").encode()


def day(n: int) -> str:
    return f"{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][n % 7]}, {n:02d} Nov 2026 09:00:00 +0000"


PAGE = (b'<html><head><title>Acme news</title>'
        b'<link rel="alternate" type="application/rss+xml" title="Acme" href="/feed.xml"></head>'
        b'<body>News</body></html>')


class Fixture:
    """news_sources.FETCH for the tests: a dict of address to (status, body)."""

    def __init__(self, pages: dict):
        self.pages, self.asked = pages, []

    def __call__(self, url, ua="x"):
        self.asked.append(url)
        if url not in self.pages:
            raise ns.FetchRefused("no answer (fixture)")
        status, body = self.pages[url]
        return status, url, "application/xml", body


@contextlib.contextmanager
def fetching(pages: dict):
    real = ns.FETCH
    fx = Fixture(pages)
    ns.FETCH = fx
    try:
        yield fx
    finally:
        ns.FETCH = real


# ------------------------------------------------------------ the safe fetch

def fetch_checks() -> None:
    for ip, want in (("10.0.0.5", False), ("127.0.0.1", False), ("192.168.1.15", False),
                     ("169.254.169.254", False), ("100.117.168.87", False), ("::1", False),
                     ("fd00::1", False), ("::ffff:127.0.0.1", False), ("0.0.0.0", False),
                     ("93.184.216.34", True), ("2606:4700::1111", True)):
        if ns._public(ip) is not want:
            check(False, f"{ip} is {'public' if want else 'refused'}")
            break
    else:
        check(True, "loopback, private, link-local, Tailscale's range, mapped and unspecified addresses are refused")
    for bad in ("ftp://example.com/feed", "file:///etc/passwd", "javascript:alert(1)",
                "https://user:pw@example.com/", "example.com"):
        try:
            ns.check_url(bad)
        except ns.FetchRefused:
            continue
        check(False, f"{bad} is refused")
        break
    else:
        check(True, "only http and https, with a host and no login in the address")

    names = {"public.example": "93.184.216.34", "inner.example": "10.1.2.3", "both.example": None}
    real_gai = socket.getaddrinfo

    def gai(host, port, *a, **k):
        if host == "both.example":
            return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", port)),
                    (socket.AF_INET, 1, 6, "", ("192.168.0.9", port))]
        if host not in names:
            raise socket.gaierror("unknown")
        return [(socket.AF_INET, 1, 6, "", (names[host], port))]

    class Resp:
        def __init__(self, status, location="", body=b""):
            self.status, self._loc, self._body = status, location, body

        def getheader(self, name, default=None):
            return self._loc if name == "Location" else "application/xml" if name == "Content-Type" else default

        def read(self, n):
            out, self._body = self._body[:n], self._body[n:]
            return out

    plan = {}

    class Conn:
        def __init__(self, host, ip, port, timeout):
            self.host, self.ip = host, ip
            connected.append((host, ip))

        def request(self, *_a, **_k):
            pass

        def getresponse(self):
            return plan[self.host]

        def close(self):
            pass

    connected: list = []
    saved = (ns._PinnedHTTPS, ns._PinnedHTTP)
    socket.getaddrinfo = gai
    ns._PinnedHTTPS = ns._PinnedHTTP = Conn
    try:
        plan["public.example"] = Resp(302, "http://inner.example/feed")
        try:
            ns.safe_fetch("https://public.example/feed")
            redirected = "fetched"
        except ns.FetchRefused as err:
            redirected = str(err)
        check(redirected == "a local or private address" and connected == [("public.example", "93.184.216.34")],
              "a redirect to a private address is refused before it is asked")
        try:
            ns.safe_fetch("https://both.example/")
            both = "fetched"
        except ns.FetchRefused as err:
            both = str(err)
        check(both == "a local or private address", "a name that also points inward is refused")
        plan["public.example"] = Resp(200, body=b"x" * (ns.FETCH_MAX_BYTES + 1))
        try:
            ns.safe_fetch("https://public.example/big")
            big = "fetched"
        except ns.FetchRefused as err:
            big = str(err)
        check(big == "larger than two megabytes", "a body over two megabytes is refused")
        plan["public.example"] = Resp(200, body=b"<rss/>")
        status, final, _ct, body = ns.safe_fetch("https://public.example/ok")
        check(status == 200 and body == b"<rss/>" and connected[-1] == ("public.example", "93.184.216.34"),
              "a public address is fetched, connecting to the address that was checked")
        real_secs = ns.FETCH_SECONDS
        ns.FETCH_SECONDS = 0
        try:
            ns.safe_fetch("https://public.example/slow")
            slow = "fetched"
        except ns.FetchRefused as err:
            slow = str(err)
        finally:
            ns.FETCH_SECONDS = real_secs
        check(slow == "took longer than ten seconds" and real_secs == 10, "ten seconds at most")
    finally:
        socket.getaddrinfo = real_gai
        ns._PinnedHTTPS, ns._PinnedHTTP = saved


# ------------------------------------------------------------ finding and adding

def add_checks() -> None:
    check(ns.declared_feeds(PAGE.decode(), "https://acme.example/news") == ["https://acme.example/feed.xml"],
          "a feed declared in the page's head is found")
    feed = rss("Acme news", [(f"Story {i}", day(i)) for i in range(1, 8)])
    with tempfile.TemporaryDirectory() as tmp:
        conn = rc.get_db(Path(tmp) / "s.sqlite")
        with fetching({"https://acme.example/news": (200, PAGE), "https://acme.example/feed.xml": (200, feed)}) as fx:
            got = ns.look(conn, "acme.example/news")
        check(got["found"] and got["feed_url"] == "https://acme.example/feed.xml" and got["name"] == "Acme news"
              and [x["headline"] for x in got["latest"]] == ["Story 7", "Story 6", "Story 5", "Story 4", "Story 3"]
              and got["latest"][0]["date"] == "7 Nov 2026",
              "Add finds the feed, its title as the name, and the five newest headlines with their dates")
        check(fx.asked == ["https://acme.example/news", "https://acme.example/feed.xml"],
              "one fetch of the address, then the feed its head declares")
        with fetching({}) as fx:
            out = ns.add(conn, got["token"], "Acme", NOW)
        conn.commit()
        st = conn.execute("SELECT * FROM source_state WHERE source_id = ?", (out["source_id"],)).fetchone()
        check(out["ok"] and out["source_id"] == "mine-acme" and fx.asked == []
              and len(json.loads(st["last_seen_ids"])) == 7,
              "Add source stores it without fetching again, the seven items it holds recorded as seen")
        with fetching({"https://acme.example/news": (200, PAGE)}) as fx:
            again = ns.look(conn, "https://acme.example/news")
        check(again.get("already") and again["note"] == "Already one of your sources." and fx.asked == [],
              "an address already a source says Already one of your sources., without a fetch")
        with fetching({"https://plain.example/": (200, b"<html><head></head><body>hi</body></html>")}):
            nf = ns.look(conn, "https://plain.example/")
        check(nf["found"] is False and nf["note"] == "Radar can't read this one yet. Try the address of its news or press page.",
              "no feed found says so in one line")
        with fetching({}):
            refused = ns.look(conn, "http://127.0.0.1/feed")
        check(refused["found"] is False, "a refused address is no feed found")
        check(ns.add(conn, "nope", "x", NOW).get("gone"), "Add source with no look behind it adds nothing")
        builtin = ns.builtin_sources()[0]["id"]
        ns.set_paused(conn, builtin, True, NOW)
        ns.set_paused(conn, "mine-acme", True, NOW)
        conn.commit()
        check(ns.paused_ids(conn) == {builtin, "mine-acme"}, "any source can be paused, built-in or added")
        ns.set_paused(conn, builtin, False, NOW)
        ns.set_paused(conn, "mine-acme", False, NOW)
        check(ns.paused_ids(conn) == set(), "and resumed")
        check(ns.remove(conn, builtin)["ok"] is False and ns.remove(conn, "mine-acme")["ok"]
              and ns.added_sources(conn) == [], "only a source Luke added can be removed")
        conn.close()


# ------------------------------------------------------------ the news run

def run_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "r.sqlite"
        conn = rc.get_db(db)
        first = rss("Acme news", [(f"Old {i}", day(i)) for i in range(1, 4)])
        with fetching({"https://acme.example/feed.xml": (200, first)}):
            got = ns.look(conn, "https://acme.example/feed.xml")
        ns.add(conn, got["token"], "Acme", NOW)
        conn.execute("UPDATE source_state SET last_checked = '2026-01-01T00:00:00Z' WHERE source_id = 'mine-acme'")
        with fetching({"https://beta.example/feed.xml": (200, rss("Beta", [("B old", day(1))]))}):
            got = ns.look(conn, "https://beta.example/feed.xml")
        ns.add(conn, got["token"], "Beta", NOW)
        ns.set_paused(conn, "mine-beta", True, NOW)
        conn.commit()
        conn.close()
        later = rss("Acme news", [(f"Old {i}", day(i)) for i in range(1, 4)]
                    + [(f"New {i}", day(i)) for i in range(4, 16)])
        real_load, real_get = check_signals.load_watchlist, rc.http_get
        builtin_asked = []
        builtin_feed = rss("Built in", [(f"Built {i}", day(i)) for i in range(1, 3)])
        check_signals.load_watchlist = lambda: [{"id": "builtin-one", "name": "Built in", "method": "rss",
                                                 "url": "https://builtin.example/", "status": "live",
                                                 "rss_url": "https://builtin.example/feed.xml"}]

        def http_get(url, config=None, **k):
            builtin_asked.append(url)
            return 200, {}, builtin_feed.decode()
        rc.http_get = http_get
        try:
            with fetching({"https://acme.example/feed.xml": (200, later),
                           "https://beta.example/feed.xml": (200, b"")}) as fx:
                code = check_signals.main(["--mock", "--db", str(db)])
        finally:
            check_signals.load_watchlist, rc.http_get = real_load, real_get
        conn = sqlite3.connect(db)
        heads = [r[0] for r in conn.execute("SELECT headline FROM signals WHERE source_id = 'mine-acme'")]
        conn.close()
        check(code == 0 and builtin_asked == ["https://builtin.example/feed.xml"],
              "the built-in sources are read as before, with the polite fetch")
        check(fx.asked == ["https://acme.example/feed.xml"], "Luke's source is read with the safe fetch, the paused one not at all")
        check(len(heads) == 10 and not any(h.startswith("Old") for h in heads)
              and {h for h in heads} == {f"New {i}" for i in range(6, 16)},
              f"only items that appeared after it was added are scored, the ten newest ({sorted(heads)})")


# ------------------------------------------------------------ the page

def page_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = rc.get_db(Path(tmp) / "p.sqlite")
        with fetching({"https://acme.example/feed.xml": (200, rss("Acme", [("A", day(1))]))}):
            got = ns.look(conn, "https://acme.example/feed.xml")
        sid = ns.add(conn, got["token"], "Acme news", NOW)["source_id"]
        built = ns.builtin_sources()
        conn.execute("INSERT INTO source_state (source_id, last_checked, last_status) VALUES (?, ?, '200')",
                     (built[0]["id"], "2026-11-17T09:00:00Z"))
        for i, (sid, rel) in enumerate([(built[1]["id"], 50), (built[1]["id"], 45), (built[2]["id"], 10),
                                        (sid, 41)]):
            conn.execute("INSERT INTO signals (url_hash, first_seen, source_id, headline, relevance)"
                         " VALUES (?, ?, ?, 'x', ?)", (f"h{i}", NOW, sid, rel))
        ns.set_paused(conn, built[3]["id"], True, NOW)
        conn.commit()
        html = sources_page.render(conn, now=NOW)
        model = ns.page_model(conn, NOW)
        conn.close()
    names = [s["id"] for s in model["sources"]]
    check(names[:3] == [built[1]["id"], sid, built[2]["id"]], f"sorted by Strong, then Stories ({names[:3]})")
    check(model["total"] == len(built) and model["answering"] == 2,
          "N of M answering counts every source not paused, Luke's included")
    check(html.index('href="/insights">Back to Insights</a>') < html.index("<h1>Sources</h1>")
          and '<a href="/insights" data-group="insights" aria-current="page">' in html
          and '.appbar a[data-group="insights"]{--nav-line:var(--group-story)}' in page_frame.FRAME_CSS,
          "Back to Insights above the title, and the menu marks Insights in its amber")
    head = re.search(r'<header class="ins-head src-head">(.*?)</header>', html, re.S).group(1)
    check(page_frame.chip("feed", "story") in head and f'>{model["answering"]} of {model["total"]} answering</span>' in head
          and 'placeholder="Paste a site or feed address"' in head and '<button type="submit" class="btn" id="src-go">Add</button>' in head,
          "the title row: amber feed chip, Sources, N of M answering, the address box and a bordered Add")
    sec = re.search(r'<section class="src-sec".*?</section>', html, re.S).group(0)
    check(f'<h2 class="sec-title" id="src-h">Your sources</h2><span class="sec-count">{len(model["sources"])}</span>'
          '<span class="src-order">Strongest first</span>' in sec, "Your sources, its count, and Strongest first")
    check(re.search(r'<li class="src-row src-headrow"[^>]*><span>Source</span><span>Last answered</span>'
                    r'<span>Stories 30 days</span><span>Strong 30 days</span><span>Status</span><span></span></li>', sec),
          "one header row: Source, Last answered, Stories 30 days, Strong 30 days, Status")
    rows = re.findall(r'<li class="src-row"( hidden data-more)?>(.*?)</li>', sec, re.S)
    check(len(rows) == len(model["sources"]) and sum(1 for h, _ in rows if h) == len(rows) - 10
          and f"Show the other {len(rows) - 10}</button>" in sec, "ten rows, then Show the other N")
    mine = next(r for _h, r in rows if "Acme news" in r)
    check('<span class="src-mine">Added by you</span>' in mine and "data-src-remove" in mine and ">Pause<" in mine,
          "Luke's source has Added by you, Pause and Remove")
    other = next(r for _h, r in rows if built[3]["id"] not in r and "Acme" not in r)
    check("data-src-remove" not in other and ">Pause<" in other, "a built-in source has Pause and no Remove")
    paused = next(r for _h, r in rows if 'data-src-pause="' + built[3]["id"] + '"' in r)
    check(">Resume<" in paused and '<span class="stchip st-neu">Paused</span>' in paused, "a paused source: Resume, and Paused in neutral")
    check('<span class="stchip st-pos">Answering</span>' in sec and '<span class="stchip st-neg">Not answering</span>' in sec,
          "Answering green, Not answering red")
    first = next(r for _h, r in rows if 'data-src-pause="' + built[0]["id"] + '"' in r)
    check("17 Nov" in first, "Last answered is the day it last answered")
    css = sources_page.CSS
    check(".btn-amber{background:#e0a94a;border-color:#e0a94a;color:#2a1d05" in css
          and ".src-n{font:600 15px/22px" in css and ".src-order{margin-left:auto;font-size:13px" in css
          and ".src-headrow{" in css and "font-size:13px" in css.split(".src-headrow{", 1)[1].split("}", 1)[0],
          "Add source solid amber; the name at 15px/600; the header row and Strongest first muted at 13px")
    check(re.search(r"max-width:767\.98px\)\{.*?\.src-headrow\{display:none\}.*?\.src-k\{display:inline\}", css, re.S),
          "on a phone each row is a small card, name and status first, the counts under")
    sizes = [int(x) for x in re.findall(r"(?:font-size:|font:\d+ )(\d+)px", css)]
    check(min(sizes) >= 13, f"nothing under 13px ({sorted(set(sizes))})")
    js = sources_page.SCRIPT
    check("'Feed found', 'st-pos'" in js and "'No feed found', 'st-cau'" in js and "'Latest from this source'" in js
          and "'Add source'" in js and "'Cancel'" in js and "'Close'" in js,
          "found: the address, Feed found, Name, Latest from this source, Cancel and Add source; not found: No feed found and Close")
    for bad in ("·", "✓", "<kbd"):
        if bad in html:
            check(False, "no middle dot, tick or key hint")
            break
    else:
        check(True, "no middle dot, tick or key hint")


@contextlib.contextmanager
def serving(db: Path):
    saved = (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
             serve_dashboard._MIGRATED)
    serve_dashboard.ARGS = argparse.Namespace(db=str(db), push=False, host="127.0.0.1", port=0)
    serve_dashboard.GUARD = serve_dashboard.WriteGuard("enforce", serve_dashboard.LOOPBACK_NAMES)
    serve_dashboard.Handler.log_message = lambda *_a, **_k: None
    serve_dashboard._MIGRATED = False
    server = serve_dashboard.RadarServer(("127.0.0.1", 0), serve_dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        (serve_dashboard.ARGS, serve_dashboard.GUARD, serve_dashboard.Handler.log_message,
         serve_dashboard._MIGRATED) = saved


def call(port, method, path, data=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=data, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def route_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "srv.sqlite"
        rc.get_db(db).close()
        with serving(db) as port, fetching({"https://acme.example/feed.xml": (200, rss("Acme", [("A", day(1))]))}) as fx:
            own = {"Host": f"127.0.0.1:{port}"}
            w = {**own, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json", "X-Radar-Request": "1"}

            def post(path, payload, headers=w):
                s, b = call(port, "POST", path, json.dumps(payload).encode(), headers)
                return s, json.loads(b or b"{}")
            for path in ("/insights/sources/look", "/insights/sources/add", "/insights/sources/pause",
                         "/insights/sources/remove"):
                s1, _ = post(path, {"url": "https://acme.example/feed.xml"}, {**w, "Origin": "https://evil.example"})
                s2, _ = post(path, {"url": "https://acme.example/feed.xml"},
                             {k: v for k, v in w.items() if k != "X-Radar-Request"})
                if s1 != 403 or s2 != 403:
                    check(False, f"{path} goes through the write guard")
                    break
            else:
                check(fx.asked == [], "every sources POST goes through the write guard, and a refused one fetches nothing")
            s, j = post("/insights/sources/look", {"url": "https://acme.example/feed.xml"})
            s2, j2 = post("/insights/sources/add", {"token": j.get("token"), "name": "Acme"})
            s3, j3 = post("/insights/sources/pause", {"source_id": "mine-acme", "paused": True})
            st, page = call(port, "GET", "/insights/sources", headers=own)
            check(s == 200 and j["found"] and s2 == 200 and s3 == 200 and st == 200
                  and "Acme" in page.decode() and ">Resume<" in page.decode(),
                  "look, add and pause over the server, and GET /insights/sources draws it")
            s4, _ = post("/insights/sources/remove", {"source_id": "mine-acme"})
            st, front = call(port, "GET", "/insights", headers=own)
            check(s4 == 200 and st == 200 and 'href="/insights/sources">' in front.decode(),
                  "Remove, and Insights' N of M sources answering opens /insights/sources")


def main() -> int:
    ap = argparse.ArgumentParser(description="News sources checks")
    ap.add_argument("--db", help="accepted for suite symmetry, unused")
    ap.parse_args()
    for section in (fetch_checks, add_checks, run_checks, page_checks, route_checks):
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
    print("PASS. Every sources check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
