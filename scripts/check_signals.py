"""Radar signal watcher.

Iterates config/watchlist.yaml, checks each fetchable source by RSS or page
diff, scores anything new against prompts/signal-scorer.md, stores results in
the signals table and builds a ntfy payload for anything at or above
fast_signal_threshold.

Safety posture:
- Dry run is the default. Payloads append to test/last_signal.txt with a
  timestamped separator, nothing is sent. Live pushes need an explicit --push.
- Polite fetching via radar_common.http_get. robots.txt respected, one request
  per source per check, etag and last-modified honoured, min interval enforced.
- Idempotent. Signals dedupe on url_hash before any scoring call, so re-runs
  never duplicate rows or re-spend tokens.
- One source cannot stop the rest. A source whose check raises, or that is
  still going after SOURCE_SECONDS, is recorded as failed in source_state and
  the run notes, and the run moves on to the next source. An item whose
  scoring raises is kept unscored for rescore.py, never dropped.
- Every run leaves its row in runs, however it ends: ok, partial when a
  source or an item failed, failed when the run itself stopped, a stop from
  outside (SIGTERM) included. A failed run exits 1, so n8n marks it failed.

Flags:
  --dry-run        explicit dry run (also the default when no flag is given)
  --push           live ntfy push, production mode, only after Luke arms it
  --source ID      spot-check one source, bypasses the interval gate
  --inject FILE    score a local announcement file (Title:/URL:/Date: headers
                   then body) as if it came from a source. No network. Repeatable.
  --mock           force RADAR_MOCK=1, score with test/mocks_signals.py
  --db PATH        alternate database for test isolation
  --budget S       seconds the run may take, default 6000, after which it
                   starts no further source and ends with its record
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import draft_outreach
import radar_common as rc  # noqa: E402
import applications  # noqa: E402
import watchlist  # noqa: E402

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.yaml"
RUBRIC_PATH = REPO_ROOT / "prompts" / "signal-scorer.md"
DRY_RUN_PAYLOAD_PATH = REPO_ROOT / "test" / "last_signal.txt"

MAX_NEW_ITEMS_PER_SOURCE = 5     # cost guardrail per check
MAX_SEEN_IDS = 200               # rolling memory per source
# A sitemap is a complete list rather than a stream, so its seen-set is
# sized to hold all of it. Cheap, a few hundred urls is tens of kilobytes.
MAX_SITEMAP_IDS = 5000
MAX_ITEM_TEXT = 4000             # chars of body text sent to the scorer
# A source's whole check, the listing, up to five article fetches and up to
# five scoring calls, normally takes under a minute. Ten minutes means it is
# stuck, and the source is let go so the sources after it still get checked.
SOURCE_SECONDS = 600
# The schedule is every two hours. A run still going at 100 minutes starts no
# further source, so it ends before the next run starts beside it.
RUN_SECONDS = 100 * 60


class SourceTimeout(BaseException):
    """A source ran past SOURCE_SECONDS.

    A BaseException so that nothing on the way down, http_get's error net
    included, can mistake it for a failed fetch and carry on with the source.
    Only run_source catches it.
    """


class RunStopped(BaseException):
    """The run was stopped from outside, SIGTERM. Ends the run, with its row."""


# ------------------------------------------------------------------ watchlist

def load_watchlist() -> list[dict]:
    import yaml
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("sources", [])


# ------------------------------------------------------------- link extraction

class _LinkExtractor(HTMLParser):
    """Collects (href, anchor text) pairs. The diff watcher keys on links."""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_endtag(self, tag):
        if tag == "a":
            if self._href:
                text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
                self.links.append((self._href, text))
            self._href = None

    def handle_data(self, data):
        if self._href is not None:
            self._parts.append(data)


_SKIP_HOST_FRAGMENTS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "vimeo.com",
)
_SKIP_TEXT_WORDS = (
    "cookie", "privacy", "contact", "about us", "login", "log in", "sign up",
    "subscribe", "newsletter", "terms", "sitemap", "read more", "load more",
    "next page", "previous",
)


def extract_article_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """Plausible article links from a listing page, absolutised and filtered."""
    parser = _LinkExtractor()
    try:
        parser.feed(html)
    except Exception:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(page_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        if any(frag in parts.netloc.lower() for frag in _SKIP_HOST_FRAGMENTS):
            continue
        if len(text) < 15:
            continue  # nav chrome, icons, "Home"
        lowered = text.lower()
        if any(word in lowered for word in _SKIP_TEXT_WORDS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append((absolute, text))
    return out


# ------------------------------------------------------------- source checking

def _get_state(conn, source_id: str):
    row = conn.execute(
        "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
    ).fetchone()
    return dict(row) if row else None


def _save_state(conn, source: dict, **fields) -> None:
    state = _get_state(conn, source["id"]) or {
        "source_id": source["id"], "url": source.get("url"),
        "method": source.get("method"), "last_checked": None,
        "last_hash": None, "last_seen_ids": None, "etag": None,
        "last_modified": None, "last_status": None,
    }
    state.update(fields)
    state["url"] = source.get("url")
    state["method"] = source.get("method")
    conn.execute(
        "INSERT INTO source_state (source_id, url, method, last_checked,"
        " last_hash, last_seen_ids, etag, last_modified, last_status)"
        " VALUES (:source_id, :url, :method, :last_checked, :last_hash,"
        " :last_seen_ids, :etag, :last_modified, :last_status)"
        " ON CONFLICT(source_id) DO UPDATE SET url=:url, method=:method,"
        " last_checked=:last_checked, last_hash=:last_hash,"
        " last_seen_ids=:last_seen_ids, etag=:etag,"
        " last_modified=:last_modified, last_status=:last_status",
        state,
    )
    conn.commit()


def _due(source: dict, state: dict | None, config: dict) -> bool:
    if not state or not state.get("last_checked"):
        return True
    floor = float(config.get("min_check_interval_hours", 6))
    interval = max(float(source.get("check_interval_hours", floor)), floor)
    try:
        last = datetime.fromisoformat(state["last_checked"].replace("Z", "+00:00"))
    except ValueError:
        return True
    elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return elapsed_hours >= interval


def check_rss(source: dict, state: dict | None, config: dict, conn, notes: list):
    """Fetch the feed politely, return new items since the last check."""
    import feedparser
    status, headers, body = rc.http_get(
        source["rss_url"], config,
        etag=state.get("etag") if state else None,
        last_modified=state.get("last_modified") if state else None,
    )
    checked = rc.now_iso()
    if status == 999:
        notes.append(f"{source['id']}: blocked by robots.txt")
        _save_state(conn, source, last_checked=checked, last_status="robots-blocked")
        return []
    if status == 304:
        _save_state(conn, source, last_checked=checked, last_status="304")
        return []
    if status != 200 or not body:
        notes.append(f"{source['id']}: fetch failed with status {status}")
        _save_state(conn, source, last_checked=checked, last_status=str(status))
        return []

    feed = feedparser.parse(body)
    entries = feed.entries or []
    current_ids = [(e.get("id") or e.get("link") or "") for e in entries]
    current_ids = [i for i in current_ids if i][:MAX_SEEN_IDS]

    previous = set()
    first_run = state is None or not state.get("last_seen_ids")
    if not first_run:
        try:
            previous = set(json.loads(state["last_seen_ids"]))
        except (ValueError, TypeError):
            first_run = True

    items = []
    if first_run:
        notes.append(f"{source['id']}: first run, baselined {len(current_ids)} entries")
    else:
        for entry in entries:
            entry_id = entry.get("id") or entry.get("link") or ""
            if not entry_id or entry_id in previous:
                continue
            summary = rc.normalise_page_text(entry.get("summary", "") or "")
            items.append({
                "source_id": source["id"],
                "source_name": source.get("name", source["id"]),
                "url": entry.get("link") or source["url"],
                "headline": (entry.get("title") or "untitled").strip(),
                "date": entry.get("published") or entry.get("updated"),
                "text": summary,
            })
            if len(items) >= MAX_NEW_ITEMS_PER_SOURCE:
                break

    merged = current_ids + [i for i in previous if i not in current_ids]
    _save_state(
        conn, source, last_checked=checked, last_status=str(status),
        last_seen_ids=json.dumps(merged[:MAX_SEEN_IDS]),
        etag=headers.get("ETag"), last_modified=headers.get("Last-Modified"),
    )
    return items


def check_sitemap(source: dict, state: dict | None, config: dict, conn,
                  notes: list):
    """Read a sitemap and treat URLs never seen before as new items.

    Some sites build their listing page with JavaScript, which a plain
    fetcher cannot read, while publishing the same content as a sitemap that
    is server-rendered XML. imec is the case that mattered. Reading the
    sitemap avoids a headless browser entirely, and a browser is a large
    thing to run for a small thing to read.

    New URLs are the signal, not lastmod. A site rebuild can restamp
    lastmod across hundreds of entries at once, which would look like a
    hundred announcements, so the dates are ignored for detection and the
    URL set is what decides.

    include_patterns and exclude_patterns filter URLs by substring, include
    acting as a whitelist when present. A sitemap is the whole site and most
    of a site is not news. imec's carries 279 job vacancies and 240 research
    papers beside its 301 press releases, and only the last are the company
    events this radar is for.
    """
    url = source.get("sitemap_url") or source.get("url")
    status, headers, body = rc.http_get(
        url, config,
        etag=state.get("etag") if state else None,
        last_modified=state.get("last_modified") if state else None,
    )
    checked = rc.now_iso()
    if status == 999:
        notes.append(f"{source['id']}: blocked by robots.txt")
        _save_state(conn, source, last_checked=checked,
                    last_status="robots-blocked")
        return []
    if status == 304:
        _save_state(conn, source, last_checked=checked, last_status="304")
        return []
    if status != 200 or not body:
        notes.append(f"{source['id']}: sitemap fetch failed with status {status}")
        _save_state(conn, source, last_checked=checked, last_status=str(status))
        return []

    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
    includes = [str(x).lower() for x in (source.get("include_patterns") or [])]
    excludes = [str(x).lower() for x in (source.get("exclude_patterns") or [])]
    kept = []
    dropped = 0
    for loc in locs:
        low = loc.lower()
        # Include first, and when a source names one it is a whitelist. A
        # sitemap is the whole site, and most of a site is not news. imec's
        # carries 279 job vacancies and 240 research papers beside its 301
        # press releases, and only the press releases are company events.
        if includes and not any(x in low for x in includes):
            dropped += 1
            continue
        if any(x in low for x in excludes):
            dropped += 1
            continue
        kept.append(loc)
    if dropped:
        notes.append(f"{source['id']}: {dropped} sitemap urls filtered, "
                     f"{len(kept)} kept")

    # Remember every url the sitemap lists, not a window of them. A feed is
    # a stream and a window over it is fine, but a sitemap is a complete
    # list, and remembering only part of a complete list means the part
    # outside the window is permanently unseen and is rediscovered as new on every
    # run, refetching the same articles forever. Bounded by the sitemap.
    current_ids = kept[-MAX_SITEMAP_IDS:]

    previous = set()
    first_run = state is None or not state.get("last_seen_ids")
    if not first_run:
        try:
            previous = set(json.loads(state["last_seen_ids"]))
        except (ValueError, TypeError):
            first_run = True

    items = []
    if first_run:
        notes.append(f"{source['id']}: first run, baselined "
                     f"{len(current_ids)} sitemap urls")
    else:
        for loc in reversed(kept):
            if loc in previous:
                continue
            text = fetch_article_text(loc, config)
            slug = loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").strip()
            items.append({
                "source_id": source["id"],
                "source_name": source.get("name", source["id"]),
                "url": loc,
                "headline": (text.split(". ")[0][:120] if text else slug) or slug,
                "date": None,
                "text": text,
            })
            if len(items) >= MAX_NEW_ITEMS_PER_SOURCE:
                break

    merged = current_ids + [i for i in previous if i not in current_ids]
    _save_state(
        conn, source, last_checked=checked, last_status=str(status),
        last_seen_ids=json.dumps(merged[:MAX_SITEMAP_IDS]),
        etag=headers.get("ETag"), last_modified=headers.get("Last-Modified"),
    )
    return items


def fetch_article_text(url: str, config: dict) -> str:
    """Fetch one fresh article politely and return its normalised text.

    Gives the scorer a body to read instead of a sixty-character headline,
    which is what keeps weak evidence out of the push band. Returns an empty
    string on any failure so the caller falls back to the anchor text.
    Small and separate so tests can stub it without any network.
    """
    status, _, body = rc.http_get(url, config)
    if status != 200 or not body:
        return ""
    return rc.normalise_page_text(body)[:MAX_ITEM_TEXT]


def check_diff(source: dict, state: dict | None, config: dict, conn, notes: list):
    """Fetch the listing page, hash it, and on change diff the article links."""
    status, headers, body = rc.http_get(
        source["url"], config,
        etag=state.get("etag") if state else None,
        last_modified=state.get("last_modified") if state else None,
    )
    checked = rc.now_iso()
    if status == 999:
        notes.append(f"{source['id']}: blocked by robots.txt")
        _save_state(conn, source, last_checked=checked, last_status="robots-blocked")
        return []
    if status == 304:
        _save_state(conn, source, last_checked=checked, last_status="304")
        return []
    if status != 200 or not body:
        notes.append(f"{source['id']}: fetch failed with status {status}")
        _save_state(conn, source, last_checked=checked, last_status=str(status))
        return []

    new_hash = rc.page_hash(body)
    links = extract_article_links(body, source["url"])
    link_urls = [u for u, _ in links]

    previous: set[str] = set()
    first_run = state is None or not state.get("last_hash")
    if not first_run and state.get("last_seen_ids"):
        try:
            previous = set(json.loads(state["last_seen_ids"]))
        except (ValueError, TypeError):
            pass

    items = []
    if first_run:
        notes.append(f"{source['id']}: first run, baselined {len(link_urls)} links")
    elif new_hash == state.get("last_hash"):
        pass  # nothing changed
    else:
        fresh = [(u, t) for u, t in links if u not in previous]
        if not fresh:
            notes.append(f"{source['id']}: page changed but no new article links found")
        for url, text in fresh[:MAX_NEW_ITEMS_PER_SOURCE]:
            # One polite fetch per fresh article, capped by the five-item
            # limit above. The anchor text is the fallback body.
            article = fetch_article_text(url, config)
            if not article:
                notes.append(f"{source['id']}: article fetch failed, "
                             f"scoring from anchor text for {url}")
            items.append({
                "source_id": source["id"],
                "source_name": source.get("name", source["id"]),
                "url": url,
                "headline": text,
                "date": None,
                "text": article or text,
            })

    merged = link_urls + [u for u in previous if u not in link_urls]
    _save_state(
        conn, source, last_checked=checked, last_status=str(status),
        last_hash=new_hash, last_seen_ids=json.dumps(merged[:MAX_SEEN_IDS]),
        etag=headers.get("ETag"), last_modified=headers.get("Last-Modified"),
    )
    return items


# --------------------------------------------------------------------- inject

def parse_announcement_file(path: Path) -> dict:
    """Read a local announcement (Title:/URL:/Date: headers, then body)."""
    raw = path.read_text(encoding="utf-8-sig")
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in raw.splitlines():
        if not in_body:
            match = re.match(r"^(Title|URL|Date)\s*:\s*(.*)$", line, re.IGNORECASE)
            if match:
                headers[match.group(1).lower()] = match.group(2).strip()
                continue
            if not line.strip():
                in_body = bool(headers)
                continue
            in_body = True
        body_lines.append(line)
    if "title" not in headers or "url" not in headers:
        raise ValueError(f"{path}: needs Title: and URL: header lines")
    return {
        "source_id": "inject",
        "source_name": f"injected file {path.name}",
        "url": headers["url"],
        "headline": headers["title"],
        "date": headers.get("date"),
        "text": "\n".join(body_lines).strip(),
    }


# -------------------------------------------------------------------- scoring

def build_user_content(item: dict) -> str:
    return (
        "Score this item.\n\n"
        f"Source: {item['source_id']} ({item.get('source_name', '')})\n"
        f"Headline: {item['headline']}\n"
        f"URL: {item['url']}\n"
        f"Date: {item.get('date') or 'unknown'}\n\n"
        f"{(item.get('text') or '')[:MAX_ITEM_TEXT]}\n"
    )


def score_item(item: dict, system_blocks: list, config: dict, mock_fn):
    """One scoring call with a single retry on unparseable JSON."""
    model = config.get("claude_model_score", "claude-sonnet-4-6")
    usage_total: dict = {}
    content = build_user_content(item)
    text, usage = rc.claude_call(model, system_blocks, content,
                                 max_tokens=1024, mock_fn=mock_fn)
    rc.add_usage(usage_total, usage)
    try:
        return rc.extract_json(text), usage_total
    except ValueError:
        pass
    retry = content + "\nReturn only the JSON object, nothing else."
    text, usage = rc.claude_call(model, system_blocks, retry,
                                 max_tokens=1024, mock_fn=mock_fn)
    rc.add_usage(usage_total, usage)
    try:
        return rc.extract_json(text), usage_total
    except ValueError:
        return None, usage_total


# ----------------------------------------------------------------------- main

def process_item(item: dict, conn, config: dict, system_blocks: list,
                 mock_fn, push_live: bool, result: dict) -> None:
    """Dedupe, score, store and (maybe) push one item. The core pipeline."""
    result["items_in"] += 1
    h = rc.url_hash(item["url"])
    exists = conn.execute(
        "SELECT 1 FROM signals WHERE url_hash = ?", (h,)
    ).fetchone()
    if exists:
        result["duplicates"] += 1
        return

    parsed, usage = score_item(item, system_blocks, config, mock_fn)
    rc.add_usage(result["usage"], usage)

    if parsed is None:
        conn.execute(
            "INSERT INTO signals (url_hash, first_seen, source_id, company,"
            " headline, summary, source_url, relevance, why, playbook_step)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (h, rc.now_iso(), item["source_id"], None, item["headline"],
             (item.get("text") or "")[:500], item["url"], None,
             "scorer returned unparseable output, review by hand", None),
        )
        conn.commit()
        result["items_new"] += 1
        result["notes"].append(f"unparseable scorer output for {item['url']}")
        return

    company = str(parsed.get("company") or "").strip() or "Unknown company"
    if rc.is_placeholder_company(company):
        company = "Unknown company"
    headline = rc.clean_headline(parsed.get("headline") or item["headline"])
    story_type = rc.clean_story_type(parsed.get("type"))
    card_summary = rc.clean_card_summary(parsed.get("card_summary"))
    why = rc.sanitise_free_text(str(parsed.get("why") or "").strip())
    playbook_step = rc.sanitise_free_text(
        str(parsed.get("playbook_step") or "").strip())
    try:
        relevance = max(0, min(100, int(parsed.get("relevance", 0))))
    except (TypeError, ValueError):
        relevance = 0

    # Resolve the company before storing, so the signal lands already
    # pointing at it. "Unknown company" is a real stored name here, and
    # normalise_company keeps it as one row rather than many.
    company_id = rc.resolve_company(conn, company)
    co = conn.execute("SELECT country, city FROM companies WHERE id=?",
                      (company_id,)).fetchone() if company_id else None
    region = rc.region_for(co["country"], co["city"], config) if co else \
        rc.region_group_names(config)[-1]
    cur = conn.execute(
        "INSERT INTO signals (url_hash, first_seen, source_id, company,"
        " headline, summary, source_url, relevance, why, playbook_step,"
        " company_id, region, story_type, card_summary)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, rc.now_iso(), item["source_id"], company, headline,
         (item.get("text") or "")[:500], item["url"], relevance,
         why, playbook_step, company_id, region, story_type, card_summary),
    )
    # One event, however many sources report it: the Insights page shows
    # the reports of an event as one story. Set in code, never by the model.
    rc.assign_event_key(conn, cur.lastrowid)
    conn.commit()
    result["items_new"] += 1
    # A second report of a story a human hid arrives hidden too, and is
    # not pushed: the human already said no to this event.
    if conn.execute("SELECT acknowledged_at FROM signals WHERE id = ?",
                    (cur.lastrowid,)).fetchone()[0]:
        result["notes"].append(f"{company}: a new report of a hidden story, kept hidden")
        return

    threshold = int(config.get("fast_signal_threshold", 75))
    if relevance < threshold:
        return

    # Drafts at push time, and only for the announcement-day step. The
    # words are what turns a notification into a two minute job, so they
    # are written before the push rather than after it.
    drafts = {"drafted": False}
    if draft_outreach.wants_drafts(playbook_step):
        drafts = draft_outreach.generate_drafts(
            conn, h, company, headline, item.get("text") or "", config)
        rc.add_usage(result["usage"], drafts.get("usage") or {})
        conn.commit()
        if drafts.get("note"):
            result["notes"].append(f"drafts for {company}: {drafts['note']}")

    title = f"Radar signal. {company}"
    message = (
        f"{headline}\n"
        f"Why it matters. {why}\n"
        f"Do today. {playbook_step}\n"
        + ("Drafts ready on the Insights page.\n" if drafts.get("drafted") else "")
        + f"{item['url']}"
    )
    if push_live:
        # A failed push must not kill the rest of the run. The pushed flag
        # only sets on success, so the catch-up sweep retries this signal on
        # the next push-mode run inside its 24 hour window.
        try:
            outcome = rc.push_ntfy(config, title, message, dry_run_path=None)
        except Exception as err:
            result["notes"].append(f"push failed for {company}: {err}")
            return
        conn.execute(
            "UPDATE signals SET pushed = 1, pushed_at = ? WHERE url_hash = ?",
            (rc.now_iso(), h),
        )
        conn.commit()
    else:
        outcome = rc.push_ntfy(config, title, message,
                               dry_run_path=DRY_RUN_PAYLOAD_PATH)
    result["pushed"] += 1
    result["payloads"].append({
        "company": company, "relevance": relevance,
        "title": title, "message": message, "outcome": outcome,
    })


@contextlib.contextmanager
def time_limit(seconds: float | None):
    """Raise SourceTimeout inside the block once seconds have passed.

    By SIGALRM, which interrupts a socket wait the per-fetch limits cannot
    reach, a reply that trickles its headers or a scoring call that hangs.
    POSIX and the main thread only. Elsewhere, Windows and a test's thread,
    it does nothing and http_get's own limits are the bound.
    """
    usable = (bool(seconds) and hasattr(signal, "setitimer")
              and threading.current_thread() is threading.main_thread())
    if not usable:
        yield
        return

    def expired(signum, frame):
        raise SourceTimeout(f"still going after {seconds:g} seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def keep_unscored(conn, item: dict, why: str) -> bool:
    """Store an item that could not be scored, so that it is not lost.

    By the time an item is scored its source has been marked as read past
    it, so an item dropped here would never be found again. It is stored the
    way an unparseable reply is, relevance NULL, which puts it in the
    digest's Needs review and in the next rescore.py pass. An item already
    stored is left as it is. Returns whether a row was added.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO signals (url_hash, first_seen, source_id,"
        " company, headline, summary, source_url, relevance, why,"
        " playbook_step) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rc.url_hash(item["url"]), rc.now_iso(), item["source_id"], None,
         item["headline"], (item.get("text") or "")[:500], item["url"],
         None, f"{why}, review by hand", None))
    conn.commit()
    return cur.rowcount > 0


def process_guarded(item: dict, conn, config: dict, system_blocks: list,
                    mock_fn, push_live: bool, result: dict) -> None:
    """process_item, with a failure costing this item's score and no more.

    A scoring call that raises, an outage or a refused request, used to end
    the whole run. Now the item is kept unscored and the run goes on.
    SourceTimeout and RunStopped are not Exceptions and pass straight up.
    """
    try:
        process_item(item, conn, config, system_blocks, mock_fn,
                     push_live, result)
    except Exception as err:                       # noqa: BLE001
        conn.rollback()
        result["errors"] += 1
        result["notes"].append(f"{item['source_id']}: {item['url']} failed "
                               f"part-way, {type(err).__name__}: {err}")
        # A failure after the row was stored, in the drafts or the push,
        # leaves the scored row as it is, and nothing is counted as kept.
        if keep_unscored(conn, item, f"scoring failed, {type(err).__name__}"):
            result["items_unscored"] += 1


def check_source(source: dict, state: dict | None, config: dict, conn,
                 notes: list) -> list:
    """The one check of a source, by its method. Returns its new items."""
    if source.get("method") == "rss" and source.get("rss_url"):
        return check_rss(source, state, config, conn, notes)
    if source.get("method") == "sitemap":
        return check_sitemap(source, state, config, conn, notes)
    return check_diff(source, state, config, conn, notes)


def run_source(source: dict, state: dict | None, conn, config: dict,
               system_blocks: list, mock_fn, push_live: bool, result: dict,
               seconds: float | None = SOURCE_SECONDS) -> None:
    """Check one source and score what it found, without risking the run.

    Whatever goes wrong stays with this source. A check that raises, or is
    still going after seconds, is stamped error in source_state, so the
    sources table on Insights says the check failed and the source waits its
    interval like any failed fetch, and the run moves on to the next source.
    Before 14 Nov 2026 one source that raised or hung ended the run there,
    with no record, and every source after it in the file went unchecked
    until it stopped. Items the check had already read past are kept
    unscored. Only RunStopped gets past this.
    """
    items: list = []
    done = 0
    fetched = False
    try:
        with time_limit(seconds):
            items = check_source(source, state, config, conn, result["notes"])
            fetched = True
            for item in items:
                process_guarded(item, conn, config, system_blocks, mock_fn,
                                push_live, result)
                done += 1
    except (Exception, SourceTimeout) as err:      # noqa: BLE001
        conn.rollback()
        result["errors"] += 1
        why = (str(err) if isinstance(err, SourceTimeout)
               else f"{type(err).__name__}: {err}")
        result["failed_sources"].append(f"{source['id']} ({why})")
        result["notes"].append(f"{source['id']}: the check failed, {why}")
        if not fetched:
            # The check never reached its own save, so nothing says it was
            # tried. Stamp it, or every run would try it again first thing.
            _save_state(conn, source, last_checked=rc.now_iso(),
                        last_status="error")
        for item in items[done:]:
            if keep_unscored(conn, item, "not scored, the source's check "
                             "failed before it was reached"):
                result["items_unscored"] += 1


def catch_up_pushes(conn, config: dict, result: dict) -> None:
    """Push-mode only. Retry unpushed hot signals from the last 24 hours.

    A transient ntfy outage then delays a push rather than losing it. The
    24 hour window stops an old backlog blasting the phone, and the pushed
    flag still only sets on success.
    """
    threshold = int(config.get("fast_signal_threshold", 75))
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT * FROM signals WHERE pushed = 0 AND relevance IS NOT NULL"
        " AND relevance >= ? AND first_seen >= ? AND status = 'new'"
        " AND acknowledged_at IS NULL"
        " ORDER BY first_seen", (threshold, cutoff)).fetchall()
    for row in rows:
        title = f"Radar signal. {row['company'] or 'Unknown company'}"
        message = (
            f"{row['headline'] or ''}\n"
            f"Why it matters. {row['why'] or ''}\n"
            f"Do today. {row['playbook_step'] or ''}\n"
            f"{row['source_url'] or ''}"
        )
        try:
            outcome = rc.push_ntfy(config, title, message, dry_run_path=None)
        except Exception as err:
            result["notes"].append(
                f"catch-up push failed for {row['company']}: {err}")
            continue
        conn.execute(
            "UPDATE signals SET pushed = 1, pushed_at = ? WHERE id = ?",
            (rc.now_iso(), row["id"]))
        conn.commit()
        result["pushed"] += 1
        result["payloads"].append({
            "company": row["company"], "relevance": row["relevance"],
            "title": title, "message": message,
            "outcome": f"catch-up {outcome}",
        })


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry run, payloads to test/last_signal.txt (default)")
    ap.add_argument("--push", action="store_true",
                    help="live ntfy push, only after arming")
    ap.add_argument("--source", metavar="ID",
                    help="spot-check one watchlist source, bypasses the interval gate")
    ap.add_argument("--inject", metavar="FILE", action="append",
                    help="score a local announcement file instead of fetching")
    ap.add_argument("--mock", action="store_true", help="force mock scoring")
    ap.add_argument("--db", metavar="PATH", help="alternate database path")
    ap.add_argument("--budget", type=float, default=RUN_SECONDS, metavar="S",
                    help="seconds the run may take before it starts no "
                         f"further source, default {RUN_SECONDS}. Check now "
                         "passes less than its own timeout, so the run ends "
                         "itself, with its record, before it is killed")
    args = ap.parse_args(argv)

    if args.dry_run and args.push:
        print("Choose one of --dry-run or --push, not both.", file=sys.stderr)
        return 2
    push_live = args.push
    if not push_live and not args.dry_run:
        print("No mode flag given, defaulting to dry run.", file=sys.stderr)

    if args.mock:
        os.environ["RADAR_MOCK"] = "1"

    mock_fn = None
    if rc.mock_mode_active():
        sys.path.insert(0, str(REPO_ROOT / "test"))
        try:
            import mocks_signals
        except ImportError as err:
            print(f"Mock mode needs test/mocks_signals.py: {err}", file=sys.stderr)
            return 2
        mock_fn = mocks_signals.mock_signal_scorer

    config = rc.load_config()
    conn = rc.get_db(Path(args.db)) if args.db else rc.get_db()
    rubric = RUBRIC_PATH.read_text(encoding="utf-8")
    system_blocks = [{
        "type": "text",
        "text": rubric,
        "cache_control": {"type": "ephemeral"},
    }]

    result = {
        "mode": "mock" if mock_fn else ("push" if push_live else "dry-run"),
        "sources_checked": 0, "items_in": 0, "items_new": 0,
        "duplicates": 0, "pushed": 0, "payloads": [], "notes": [],
        "usage": {}, "errors": 0, "failed_sources": [],
        "items_unscored": 0, "left_for_next_run": 0,
    }

    if args.inject:
        try:
            items = [parse_announcement_file(Path(f)) for f in args.inject]
        except (OSError, ValueError) as err:
            print(f"Cannot inject: {err}", file=sys.stderr)
            return 2
    else:
        items = []
        sources = load_watchlist()
        if args.source:
            sources = [s for s in sources if s.get("id") == args.source]
            if not sources:
                print(f"No fetchable watchlist source with id {args.source!r}.",
                      file=sys.stderr)
                return 2

    # From here on the run always ends with its row in runs. A stop from
    # outside, SIGTERM when n8n or the container goes down, becomes
    # RunStopped so that the row is still written on the way out.
    failure = None
    main_thread = threading.current_thread() is threading.main_thread()
    previous_term = signal.SIG_DFL
    started = time.monotonic()
    try:
        if main_thread:
            previous_term = (signal.signal(signal.SIGTERM, _stop_from_outside)
                             or signal.SIG_DFL)
        if args.inject:
            for item in items:
                process_guarded(item, conn, config, system_blocks, mock_fn,
                                push_live, result)
        else:
            # The applications board's 21-day rule and Radar's Follow up ride
            # on this run, every two hours, because it is the scheduled job
            # that writes the database in its everyday mode. No model call.
            # A fault here is noted and the sources are still checked. A spot
            # check of one source leaves it alone.
            if not args.source:
                try:
                    result["applications"] = applications.run_rules(conn)
                except Exception as err:            # noqa: BLE001
                    result["errors"] += 1
                    result["notes"].append(f"applications board rule: "
                                           f"{type(err).__name__}: {err}")
            for source in sources:
                if source.get("method") == "email":
                    continue
                if source.get("status") != "live" and not args.source:
                    result["notes"].append(f"{source['id']}: skipped, status "
                                           f"{source.get('status')}")
                    continue
                state = _get_state(conn, source["id"])
                if not args.source and not _due(source, state, config):
                    continue
                left = args.budget - (time.monotonic() - started)
                if left <= 0:
                    # Due, but the run has used its time. It stays due and
                    # is first in the queue for the next run.
                    result["left_for_next_run"] += 1
                    continue
                result["sources_checked"] += 1
                run_source(source, state, conn, config, system_blocks,
                           mock_fn, push_live, result,
                           seconds=min(SOURCE_SECONDS, max(left, 1)))

        if push_live:
            catch_up_pushes(conn, config, result)
        # The Watchlist's suggestions, worked out in code from what this run
        # and the ones before it stored: never a model call, never a company
        # added. A spot check of one source leaves them alone. A fault is
        # noted and does not fail the run.
        if not args.source:
            try:
                result["suggestions"] = watchlist.refresh_suggestions(conn)
                conn.commit()
            except Exception as err:                # noqa: BLE001
                conn.rollback()
                result["errors"] += 1
                result["notes"].append(f"watchlist suggestions: "
                                       f"{type(err).__name__}: {err}")
    except BaseException as err:                   # noqa: BLE001
        failure = err
    finally:
        if main_thread:
            signal.signal(signal.SIGTERM, previous_term)

    outcome = ("failed" if failure is not None else
               "partial" if result["errors"] or result["left_for_next_run"]
               else "ok")
    result["outcome"] = outcome
    note = f"sources={result['sources_checked']} inject={bool(args.inject)}"
    board = result.get("applications")
    if board and (board["closed"] or board["follow_ups"] or board["made"]):
        note += (f", board: {board['closed']} closed as no reply, "
                 f"{board['follow_ups']} follow-ups added, {board['made']} made")
    sugg = result.get("suggestions")
    if sugg and (sugg["made"] or sugg["folded"]):
        note += (f", watchlist: {sugg['made']} suggested, "
                 f"{sugg['folded']} folded into dismissed")
    if result["failed_sources"]:
        note += (f", {len(result['failed_sources'])} failed: "
                 + "; ".join(result["failed_sources"]))
    if result["items_unscored"]:
        note += f", {result['items_unscored']} kept unscored"
    if result["left_for_next_run"]:
        note += f", {result['left_for_next_run']} left for the next run"
    if failure is not None:
        result["failure"] = f"{type(failure).__name__}: {failure}"
        note += f", stopped: {result['failure']}"
        traceback.print_exception(type(failure), failure,
                                  failure.__traceback__, file=sys.stderr)
        try:
            conn.rollback()
        except Exception:                          # noqa: BLE001
            pass
    try:
        rc.log_run(
            conn, "signals",
            mode="mock" if mock_fn else ("live" if push_live else "dry-run"),
            items_in=result["items_in"], items_new=result["items_new"],
            model=config.get("claude_model_score"), usage=result["usage"],
            note=note[:1000], outcome=outcome,
        )
    except Exception as err:                       # noqa: BLE001
        # The record is the point of all this, but failing to write it must
        # not hide why the run itself failed.
        print(f"The run record could not be written: {err}", file=sys.stderr)

    print(json.dumps(result, indent=2))
    return 1 if failure is not None else 0


def _stop_from_outside(signum, frame):
    raise RunStopped(f"stopped from outside by signal {signum}")


if __name__ == "__main__":
    sys.exit(main())
