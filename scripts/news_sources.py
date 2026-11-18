"""The news watcher's sources as Luke keeps them: the ones he adds, and which
sources, his or the built-in ones in config/watchlist.yaml, are paused.

One table, news_sources (db/schema.sql), created at server start like every
table. A row of kind added is a feed Luke added on /insights/sources. A row
of kind builtin only ever records that a built-in source is paused. The
built-in sources stay in config/watchlist.yaml, read as they always were.

Adding a source is one press. Add fetches the address once (safe_fetch) and
looks for a feed: the address itself, or one the page's head declares, which
is then fetched too. Add source stores it and records the items the feed
holds at that moment as seen, in source_state, as the watcher's own first run
does, so only items that appear afterwards are scored. The news run reads an
added source with the same safe fetch and scores at most its ten newest new
items (MAX_ITEMS). Nothing here makes a model call.

Fetching is safe: http and https only, no local or private address, a
redirect checked the same way, ten seconds and two megabytes at most.
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import radar_common as rc

FETCH_SECONDS = 10
FETCH_MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_ITEMS = 10            # new items scored per added source per run
PREVIEW_ITEMS = 5
CHECK_HOURS = 6           # how often the run reads an added source
NAME_MAX = 120
URL_MAX = 2000
SHOWN = 10                # rows before Show the other N
STRONG = 40               # relevance that counts as strong
LOOK_SECONDS = 30 * 60    # how long a look is held for Add source

ALREADY = "Already one of your sources."
NOT_FOUND = "Radar can't read this one yet. Try the address of its news or press page."


class FetchRefused(Exception):
    """The address may not be fetched, or did not answer as a page should."""


# ------------------------------------------------------------ fetching

def _public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified or not addr.is_global)


def resolve_public(host: str, port: int) -> str:
    """One public address for host, or FetchRefused. Every address the name
    has must be public, so a name that also points inward is refused."""
    if not host:
        raise FetchRefused("no host")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as err:
        raise FetchRefused(f"the name does not resolve ({type(err).__name__})") from err
    ips = [i[4][0] for i in infos]
    if not ips or not all(_public(ip) for ip in ips):
        raise FetchRefused("a local or private address")
    return ips[0]


def check_url(url: str) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchRefused("not an http or https address")
    if parts.username or parts.password:
        raise FetchRefused("an address with a login in it")
    return parts


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connects to the address already checked, not to whatever the name
    resolves to a moment later, and still checks the certificate by name."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


def _one_request(url: str, user_agent: str, deadline: float):
    parts = check_url(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ip = resolve_public(parts.hostname, port)
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchRefused("took longer than ten seconds")
    cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
    conn = cls(parts.hostname, ip, port, timeout=left)
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    try:
        conn.request("GET", path, headers={
            "Host": parts.netloc.rsplit("@", 1)[-1], "User-Agent": user_agent,
            "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,text/html;q=0.9,*/*;q=0.5"})
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308):
            return resp.status, resp.getheader("Location") or "", "", b""
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise FetchRefused("took longer than ten seconds")
            block = resp.read(65536)
            if not block:
                break
            size += len(block)
            if size > FETCH_MAX_BYTES:
                raise FetchRefused("larger than two megabytes")
            chunks.append(block)
        return resp.status, "", resp.getheader("Content-Type") or "", b"".join(chunks)
    except (OSError, http.client.HTTPException) as err:
        raise FetchRefused(f"no answer ({type(err).__name__})") from err
    finally:
        conn.close()


def safe_fetch(url: str, user_agent: str = "MedTechRadar/1.0") -> tuple[int, str, str, bytes]:
    """GET one address safely. (status, final address, content type, body).
    Each redirect is checked as the first address was. FetchRefused for an
    address that may not be fetched, and for no answer at all."""
    deadline = time.monotonic() + FETCH_SECONDS
    for _hop in range(MAX_REDIRECTS + 1):
        status, location, ctype, body = _one_request(url, user_agent, deadline)
        if not location:
            return status, url, ctype, body
        url = urllib.parse.urljoin(url, location)
    raise FetchRefused("too many redirects")


# Tests put a fixture here in place of the network; nothing else changes it.
FETCH = safe_fetch


def _ua(config: dict | None) -> str:
    return (config or {}).get("fetch_user_agent") or "MedTechRadar/1.0"


def run_fetch(url: str, config: dict | None = None):
    """For the news run: (status, headers, body text) as radar_common.http_get
    answers, status 0 with the reason in headers["error"] when refused."""
    try:
        status, _final, ctype, body = FETCH(url, _ua(config))
    except FetchRefused as err:
        return 0, {"error": str(err)}, ""
    return status, {"Content-Type": ctype}, body.decode("utf-8", "replace")


# ------------------------------------------------------------ finding a feed

_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json",
               "application/rdf+xml")


def declared_feeds(page: str, base: str) -> list[str]:
    """Feeds a page's head declares: <link rel="alternate"> with a feed type."""
    head = page.split("</head>", 1)[0] if "</head>" in page.lower() else page[:200_000]
    out = []
    for tag in re.findall(r"<link\b[^>]*>", head, re.IGNORECASE):
        attrs = dict((k.lower(), html.unescape(v)) for k, _q, v in
                     re.findall(r'([\w:-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S))
        rel = attrs.get("rel", "").lower().split()
        if "alternate" in rel and attrs.get("type", "").lower().split(";")[0].strip() in _FEED_TYPES and attrs.get("href"):
            url = urllib.parse.urljoin(base, attrs["href"].strip())
            if url not in out:
                out.append(url)
    return out


def parse_feed(body: bytes):
    """The parsed feed when the body is one, else None."""
    import feedparser
    feed = feedparser.parse(body)
    if feed.get("version") or (feed.entries and not feed.get("bozo")):
        return feed
    return None


def _when(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def entry_id(entry) -> str:
    return entry.get("id") or entry.get("link") or ""


def newest(entries, n: int) -> list:
    """The n newest entries, dated ones by date, undated after in feed order."""
    dated = sorted([e for e in entries if _when(e)], key=_when, reverse=True)
    undated = [e for e in entries if not _when(e)]
    return (dated + undated)[:n]


def find_feed(url: str, config: dict | None = None) -> dict:
    """Fetch the address and find its feed. {"found": bool, "feed_url",
    "feed", "why"}. A refused or failed fetch is not found, with why."""
    try:
        status, final, _ctype, body = FETCH(url, _ua(config))
    except FetchRefused as err:
        return {"found": False, "why": str(err)}
    if status != 200 or not body:
        return {"found": False, "why": f"answered {status}"}
    feed = parse_feed(body)
    if feed is not None:
        return {"found": True, "feed_url": final, "feed": feed}
    for candidate in declared_feeds(body.decode("utf-8", "replace"), final)[:2]:
        try:
            status, feed_final, _c, feed_body = FETCH(candidate, _ua(config))
        except FetchRefused:
            continue
        feed = parse_feed(feed_body) if status == 200 else None
        if feed is not None:
            return {"found": True, "feed_url": feed_final, "feed": feed}
    return {"found": False, "why": "no feed"}


# ------------------------------------------------------------ the table

def has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                        " AND name = 'news_sources'").fetchone() is not None


def rows(conn) -> list[dict]:
    if not has_table(conn):
        return []
    return [dict(r) for r in conn.execute("SELECT * FROM news_sources ORDER BY id")]


def paused_ids(conn) -> set:
    return {r["source_id"] for r in rows(conn) if r["paused_at"]}


def added_sources(conn) -> list[dict]:
    """Luke's sources as the watcher reads a source, paused ones included
    (the caller skips them)."""
    out = []
    for r in rows(conn):
        if r["kind"] != "added":
            continue
        out.append({"id": r["source_id"], "name": r["name"], "method": "rss",
                    "url": r["url"], "rss_url": r["feed_url"], "status": "live",
                    "check_interval_hours": CHECK_HOURS, "added": True,
                    "max_items": MAX_ITEMS})
    return out


def builtin_sources() -> list[dict]:
    """The built-in sources Radar reads, as Insights counts them: every one in
    config/watchlist.yaml that is not unverified."""
    import build_dashboard as bd
    return [s for s in bd.load_watchlist() if (s.get("status") or "live") != "unverified"]


def _canon(url: str) -> str:
    try:
        return rc.url_hash(url)
    except Exception:  # noqa: BLE001
        return str(url)


def held_by(conn, *urls) -> str | None:
    """The source_id of a source, built-in or added, at any of these addresses."""
    want = {_canon(u) for u in urls if u}
    for s in builtin_sources():
        if {_canon(s.get(k)) for k in ("url", "rss_url", "sitemap_url") if s.get(k)} & want:
            return s["id"]
    for r in rows(conn):
        if r["kind"] == "added" and {_canon(r["url"]), _canon(r["feed_url"])} & want:
            return r["source_id"]
    return None


# Looks waiting for Add source: token to (when, what was found). In memory,
# so a server restart forgets them and the next Add looks again.
_LOOKS: dict = {}
_LOOKS_LOCK = threading.Lock()


def _fmt_day(when) -> str:
    return f"{when.day} {when:%b} {when.year}" if when else ""


def look(conn, url, config: dict | None = None) -> dict:
    """What Add shows for an address. {"ok": True, "found": bool, ...}."""
    url = str(url or "").strip()
    if not url or len(url) > URL_MAX:
        return {"ok": False, "invalid": True, "note": "Paste a site or feed address."}
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        url = "https://" + url
    try:
        check_url(url)
    except FetchRefused:
        return {"ok": True, "found": False, "url": url, "note": NOT_FOUND}
    if held_by(conn, url):
        return {"ok": True, "already": True, "url": url, "note": ALREADY}
    got = find_feed(url, config)
    if not got["found"]:
        return {"ok": True, "found": False, "url": url, "note": NOT_FOUND}
    if held_by(conn, got["feed_url"]):
        return {"ok": True, "already": True, "url": url, "note": ALREADY}
    feed = got["feed"]
    title = " ".join(str(feed.feed.get("title") or "").split())[:NAME_MAX] or \
        urllib.parse.urlsplit(url).hostname or url
    latest = [{"headline": " ".join(str(e.get("title") or "Untitled").split())[:200],
               "date": _fmt_day(_when(e))} for e in newest(feed.entries, PREVIEW_ITEMS)]
    ids = [i for i in (entry_id(e) for e in feed.entries) if i]
    token = secrets.token_urlsafe(18)
    with _LOOKS_LOCK:
        cutoff = time.monotonic() - LOOK_SECONDS
        for k in [k for k, v in _LOOKS.items() if v[0] < cutoff]:
            del _LOOKS[k]
        _LOOKS[token] = (time.monotonic(), {"url": url, "feed_url": got["feed_url"], "ids": ids})
    return {"ok": True, "found": True, "url": url, "feed_url": got["feed_url"],
            "name": title, "latest": latest, "token": token}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return s or "source"


def add(conn, token, name, now: str) -> dict:
    """Add source: store the source a look found, and record what its feed held
    then as seen, so no item already there is scored. The caller holds the
    write lock and commits on ok."""
    with _LOOKS_LOCK:
        held = _LOOKS.pop(str(token or ""), None)
    if held is None:
        return {"ok": False, "gone": True, "note": "Press Add again to look at the address."}
    what = held[1]
    name = " ".join(str(name or "").split())[:NAME_MAX]
    if not name:
        return {"ok": False, "invalid": True, "note": "Give the source a name."}
    if held_by(conn, what["url"], what["feed_url"]):
        return {"ok": False, "already": True, "note": ALREADY}
    sid = f"mine-{slug(name)}"
    n = 2
    taken = {r["source_id"] for r in rows(conn)} | {s["id"] for s in builtin_sources()}
    while sid in taken:
        sid = f"mine-{slug(name)}-{n}"
        n += 1
    conn.execute("INSERT INTO news_sources (source_id, kind, name, url, feed_url, added_at)"
                 " VALUES (?, 'added', ?, ?, ?, ?)", (sid, name, what["url"], what["feed_url"], now))
    # The feed's items as they stand, seen and not scored: the watcher's own
    # first-run baseline, written now so the first run after this has a past.
    conn.execute(
        "INSERT INTO source_state (source_id, url, method, last_checked, last_seen_ids, last_status)"
        " VALUES (?, ?, 'rss', ?, ?, '200') ON CONFLICT(source_id) DO UPDATE SET"
        " url = excluded.url, method = 'rss', last_checked = excluded.last_checked,"
        " last_seen_ids = excluded.last_seen_ids, last_status = '200'",
        (sid, what["url"], now, json.dumps(what["ids"][:200])))
    return {"ok": True, "source_id": sid, "name": name, "seen": len(what["ids"])}


def set_paused(conn, source_id, paused: bool, now: str) -> dict:
    sid = str(source_id or "")
    builtin = {s["id"] for s in builtin_sources()}
    mine = {r["source_id"]: r for r in rows(conn)}
    if sid not in builtin and not (sid in mine and mine[sid]["kind"] == "added"):
        return {"ok": False, "gone": True, "note": "That source is no longer on file."}
    if sid in mine:
        conn.execute("UPDATE news_sources SET paused_at = ? WHERE source_id = ?",
                     (now if paused else None, sid))
    elif paused:
        conn.execute("INSERT INTO news_sources (source_id, kind, paused_at) VALUES (?, 'builtin', ?)",
                     (sid, now))
    return {"ok": True, "source_id": sid, "paused": paused}


def remove(conn, source_id) -> dict:
    """Remove one of Luke's sources. Its stories stay."""
    sid = str(source_id or "")
    row = conn.execute("SELECT kind FROM news_sources WHERE source_id = ?", (sid,)).fetchone()
    if row is None or row["kind"] != "added":
        return {"ok": False, "note": "Only a source you added can be removed."}
    conn.execute("DELETE FROM news_sources WHERE source_id = ?", (sid,))
    return {"ok": True, "source_id": sid}


# ------------------------------------------------------------ the page's model

def page_model(conn, now: str | None = None) -> dict:
    """Every source, built-in and added, with its counts. Read only."""
    moment = datetime.strptime(now or rc.now_iso(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    since = (moment - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mine = {r["source_id"]: r for r in rows(conn)}
    state = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM source_state")}
    counts = {r["source_id"]: (r["n"], r["strong"] or 0) for r in conn.execute(
        "SELECT source_id, COUNT(*) AS n, SUM(relevance >= ?) AS strong FROM signals"
        " WHERE first_seen >= ? GROUP BY source_id", (STRONG, since))}
    out = []
    listed = [(s["id"], s.get("name") or s["id"], False) for s in builtin_sources()]
    listed += [(r["source_id"], r["name"], True) for r in mine.values() if r["kind"] == "added"]
    for sid, name, added in listed:
        st = state.get(sid) or {}
        answering = str(st.get("last_status") or "") in ("200", "304")
        paused = bool((mine.get(sid) or {}).get("paused_at"))
        n, strong = counts.get(sid, (0, 0))
        out.append({"id": sid, "name": name, "added": added, "paused": paused,
                    "status": "paused" if paused else "answering" if answering else "not answering",
                    "last": st.get("last_checked") if answering else None,
                    "stories": n, "strong": strong})
    out.sort(key=lambda s: (-s["strong"], -s["stories"], s["name"].lower()))
    active = [s for s in out if not s["paused"]]
    return {"sources": out, "total": len(active),
            "answering": sum(1 for s in active if s["status"] == "answering")}
