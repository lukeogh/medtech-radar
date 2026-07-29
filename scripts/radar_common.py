"""Shared helpers for MedTech Radar scripts.

Every script in this repo goes through these helpers for config, secrets,
database access, Claude API calls, polite fetching and ntfy pushes. That keeps
the rules in one place:

- Secrets come from .env only, referenced by name via config api_key_env.
- Every Claude call logs token usage to the runs table. Cost guardrail.
- Mock mode (RADAR_MOCK=1 or an explicit mock function) lets the full pipeline
  run without an API key. Scripts never mock silently. Live mode with no key
  fails loudly.
- Fetching is polite. Custom UA, robots.txt respected, one request per source.
- Deduplication is by sha256 of a normalised URL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "db" / "radar.sqlite"
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"
CONFIG_PATH = REPO_ROOT / "config" / "radar.yaml"
ENV_PATH = REPO_ROOT / ".env"

# Query parameters that carry tracking noise, stripped before hashing a URL.
_TRACKING_PARAMS = re.compile(
    r"^(utm_|trk|trackingid|refid|ref|mkt_tok|gclid|fbclid|li_|midtoken|"
    r"eid|otpToken|lipi)", re.IGNORECASE
)


# ---------------------------------------------------------------- env/config

def load_env(path: Path | None = None) -> dict:
    """Read KEY=VALUE lines from .env into os.environ. Existing vars win."""
    path = path or ENV_PATH
    loaded = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            loaded[key] = value
            os.environ.setdefault(key, value)
    return loaded


def load_config() -> dict:
    import yaml
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ database

def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the radar database, applying the schema idempotently.

    WAL journal mode plus a five second busy timeout, because the inbox,
    signals and digest workflows can overlap on the same file and the
    default locking would eventually throw a locked-database error at the
    worst possible moment.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def url_hash(url: str) -> str:
    """sha256 of a normalised URL. The dedupe key for opportunities and signals."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        query = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_PARAMS.match(k)
        ]
        normalised = urllib.parse.urlunsplit((
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urllib.parse.urlencode(sorted(query)),
            "",  # drop fragment
        ))
    except ValueError:
        normalised = url.strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def log_run(conn: sqlite3.Connection, workflow: str, mode: str = "live",
            items_in: int = 0, items_new: int = 0, model: str | None = None,
            usage: dict | None = None, note: str | None = None) -> None:
    usage = usage or {}
    conn.execute(
        "INSERT INTO runs (ts, workflow, mode, items_in, items_new, model,"
        " input_tokens, output_tokens, cache_read_tokens, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), workflow, mode, items_in, items_new, model,
         usage.get("input_tokens", 0), usage.get("output_tokens", 0),
         usage.get("cache_read_tokens", 0), note),
    )
    conn.commit()


# ---------------------------------------------------------------- Claude API

def mock_mode_active() -> bool:
    return os.environ.get("RADAR_MOCK", "").strip() in ("1", "true", "yes")


def claude_call(model: str, system_blocks: list | str, user_content: str,
                max_tokens: int = 1024, mock_fn=None) -> tuple[str, dict]:
    """One Claude call. Returns (response_text, usage_dict).

    In mock mode (RADAR_MOCK=1) the caller must supply mock_fn(user_content)
    returning a string. Live mode with no API key raises a clear error rather
    than silently degrading.

    system_blocks may be a plain string or a list of system content blocks.
    Pass cache_control on the stable block when scoring in a loop, e.g.
      [{"type": "text", "text": cv_and_prefs, "cache_control": {"type": "ephemeral"}},
       {"type": "text", "text": rubric}]
    """
    if mock_mode_active():
        if mock_fn is None:
            raise RuntimeError(
                "RADAR_MOCK=1 but no mock function supplied. Refusing to fake a result."
            )
        return mock_fn(user_content), {"input_tokens": 0, "output_tokens": 0,
                                       "cache_read_tokens": 0, "mode": "mock"}

    load_env()
    config = load_config()
    key_name = config.get("api_key_env", "ANTHROPIC_API_KEY")
    if not os.environ.get(key_name):
        raise RuntimeError(
            f"{key_name} is not set. Fill .env (see .env.example) or set "
            f"RADAR_MOCK=1 to run the offline test path."
        )

    import anthropic
    client = anthropic.Anthropic(api_key=os.environ[key_name])
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0,  # same advert, same score, run to run
        system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        "mode": "live",
    }
    return text, usage


def add_usage(total: dict, usage: dict) -> dict:
    for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
        total[key] = total.get(key, 0) + usage.get(key, 0)
    return total


def sanitise_free_text(text):
    """Make the voice rules mechanical for model-written free text.

    Prompt compliance narrows variance, it cannot remove it, so every
    free-text field passes through here on its way to the database. Em
    dashes become commas. Semicolons become full stops with the following
    letter capitalised. Doubled spaces and stray double stops collapse.
    The worked case is the Meridian wrong-rate line:

      in.  "No action needed — the rate is less than half the floor;
            renegotiation to £650 a day would change that."
      out. "No action needed, the rate is less than half the floor.
            Renegotiation to £650 a day would change that."

    None passes through untouched so optional columns stay NULL.
    """
    if text is None:
        return None
    s = str(text)
    # Em dashes, spaced or not, read naturally as commas.
    s = re.sub(r"\s*—\s*", ", ", s)
    # A semicolon ends a sentence, so the next letter starts one.
    s = re.sub(r"\s*;\s*(\w)", lambda m: ". " + m.group(1).upper(), s)
    s = re.sub(r"\s*;\s*", ".", s)
    # Tidy the seams the substitutions can leave.
    s = re.sub(r"\.(\s*\.)+", ".", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r" {2,}", " ", s)
    return s.strip()


def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of a model response."""
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in response: {text[:200]!r}")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError(f"Unbalanced JSON in response: {text[:200]!r}")


# ----------------------------------------------------------- polite fetching

_ROBOTS_CACHE: dict[str, list[str]] = {}


def _robots_disallows(base: str, ua: str) -> list[str]:
    """Fetch and minimally parse robots.txt for a host. Cached per process."""
    if base in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[base]
    disallows: list[str] = []
    try:
        req = urllib.request.Request(base + "/robots.txt",
                                     headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(200_000).decode("utf-8", "replace")
        applies = False
        for line in body.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                applies = value == "*"
            elif key == "disallow" and applies and value:
                disallows.append(value)
    except Exception:
        pass  # unreadable robots.txt is treated as allow-all
    _ROBOTS_CACHE[base] = disallows
    return disallows


def robots_allowed(url: str, ua: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    for rule in _robots_disallows(base, ua):
        if path.startswith(rule):
            return False
    return True


def http_get(url: str, config: dict | None = None,
             etag: str | None = None, last_modified: str | None = None):
    """Polite GET. Returns (status, headers_dict, body_text). 304 gives empty body."""
    config = config or load_config()
    ua = config.get("fetch_user_agent", "MedTechRadar/1.0")
    if not robots_allowed(url, ua):
        return 999, {}, ""  # 999 means blocked by robots.txt, caller records it
    headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml,*/*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(2_000_000).decode("utf-8", "replace")
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as err:
        if err.code == 304:
            return 304, dict(err.headers), ""
        return err.code, dict(err.headers), ""
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return 0, {"error": str(err)}, ""


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return " ".join(self._chunks)


def normalise_page_text(html: str) -> str:
    """Visible text with whitespace collapsed. The input to page-diff hashing."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        text = parser.text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def page_hash(html: str) -> str:
    return hashlib.sha256(normalise_page_text(html).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- ntfy

_NTFY_UNSET = object()  # sentinel so omitting dry_run_path still means dry run


def push_ntfy(config: dict, title: str, message: str,
              tags: str = "satellite", priority: str = "high",
              dry_run_path=_NTFY_UNSET) -> str:
    """Write a would-be ntfy push to a file, or send it when explicitly armed.

    Dry run is the default safety posture. Omitting dry_run_path writes the
    payload to test/last_signal.txt. Pass a Path to choose another file. A
    live POST only happens when the caller explicitly passes
    dry_run_path=None, the armed production path.

    Dry-run payloads append with a timestamped separator rather than
    overwriting, so a run that produces several signals keeps them all for
    review.
    """
    rendered = (
        f"POST {config.get('ntfy_url', '')}/{config.get('ntfy_topic_radar', '')}\n"
        f"Title: {title}\n"
        f"Priority: {priority}\n"
        f"Tags: {tags}\n"
        f"\n{message}\n"
    )
    if dry_run_path is _NTFY_UNSET:
        dry_run_path = REPO_ROOT / "test" / "last_signal.txt"
    if dry_run_path is not None:
        dry_run_path.parent.mkdir(parents=True, exist_ok=True)
        separator = ""
        if dry_run_path.exists() and dry_run_path.stat().st_size > 0:
            separator = f"\n----- next payload, written {now_iso()} -----\n"
        with open(dry_run_path, "a", encoding="utf-8") as fh:
            fh.write(separator + rendered)
        return "dry-run"

    url = f"{config['ntfy_url'].rstrip('/')}/{config['ntfy_topic_radar']}"
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    load_env()
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return f"sent:{resp.status}"


if __name__ == "__main__":
    # Smoke check: schema applies, config loads, hashing is stable.
    conn = get_db()
    cfg = load_config()
    sample = url_hash("https://example.com/job/123?utm_source=alert&ref=email")
    same = url_hash("https://EXAMPLE.com/job/123/")
    print(json.dumps({
        "db": str(DB_PATH),
        "tables": [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
        "config_keys": sorted(cfg.keys()),
        "hash_stable": sample == same,
    }, indent=2))
