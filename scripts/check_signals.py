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

Flags:
  --dry-run        explicit dry run (also the default when no flag is given)
  --push           live ntfy push, production mode, only after Luke arms it
  --source ID      spot-check one source, bypasses the interval gate
  --inject FILE    score a local announcement file (Title:/URL:/Date: headers
                   then body) as if it came from a source. No network. Repeatable.
  --mock           force RADAR_MOCK=1, score with test/mocks_signals.py
  --db PATH        alternate database for test isolation
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import radar_common as rc  # noqa: E402

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.yaml"
RUBRIC_PATH = REPO_ROOT / "prompts" / "signal-scorer.md"
DRY_RUN_PAYLOAD_PATH = REPO_ROOT / "test" / "last_signal.txt"

MAX_NEW_ITEMS_PER_SOURCE = 5     # cost guardrail per check
MAX_SEEN_IDS = 200               # rolling memory per source
MAX_ITEM_TEXT = 4000             # chars of body text sent to the scorer


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
    headline = str(parsed.get("headline") or item["headline"]).strip()
    why = str(parsed.get("why") or "").strip()
    playbook_step = str(parsed.get("playbook_step") or "").strip()
    try:
        relevance = max(0, min(100, int(parsed.get("relevance", 0))))
    except (TypeError, ValueError):
        relevance = 0

    conn.execute(
        "INSERT INTO signals (url_hash, first_seen, source_id, company,"
        " headline, summary, source_url, relevance, why, playbook_step)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (h, rc.now_iso(), item["source_id"], company, headline,
         (item.get("text") or "")[:500], item["url"], relevance,
         why, playbook_step),
    )
    conn.commit()
    result["items_new"] += 1

    threshold = int(config.get("fast_signal_threshold", 75))
    if relevance < threshold:
        return

    title = f"Radar signal. {company}"
    message = (
        f"{headline}\n"
        f"Why it matters. {why}\n"
        f"Do today. {playbook_step}\n"
        f"{item['url']}"
    )
    if push_live:
        outcome = rc.push_ntfy(config, title, message, dry_run_path=None)
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
        "usage": {},
    }

    if args.inject:
        for file_arg in args.inject:
            try:
                item = parse_announcement_file(Path(file_arg))
            except (OSError, ValueError) as err:
                print(f"Cannot inject {file_arg}: {err}", file=sys.stderr)
                return 2
            process_item(item, conn, config, system_blocks, mock_fn,
                         push_live, result)
    else:
        sources = load_watchlist()
        if args.source:
            sources = [s for s in sources if s.get("id") == args.source]
            if not sources:
                print(f"No fetchable watchlist source with id {args.source!r}.",
                      file=sys.stderr)
                return 2
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
            result["sources_checked"] += 1
            if source.get("method") == "rss" and source.get("rss_url"):
                items = check_rss(source, state, config, conn, result["notes"])
            else:
                items = check_diff(source, state, config, conn, result["notes"])
            for item in items:
                process_item(item, conn, config, system_blocks, mock_fn,
                             push_live, result)

    rc.log_run(
        conn, "signals",
        mode="mock" if mock_fn else ("live" if push_live else "dry-run"),
        items_in=result["items_in"], items_new=result["items_new"],
        model=config.get("claude_model_score"), usage=result["usage"],
        note=f"sources={result['sources_checked']} inject={bool(args.inject)}",
    )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
