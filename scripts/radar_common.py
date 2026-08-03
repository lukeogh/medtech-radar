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

# Columns added after the first schema shipped. CREATE TABLE IF NOT EXISTS
# cannot add columns to an existing table, so every writer migrates here,
# idempotently, before touching a row. Order never matters, each ALTER is
# guarded by a look at what the table already has.
_MIGRATIONS = (
    ("opportunities", "pay_currency", "TEXT"),
    ("opportunities", "pay_period", "TEXT"),
    ("opportunities", "pay_min", "REAL"),
    ("opportunities", "pay_max", "REAL"),
    ("opportunities", "day_rate", "REAL"),
    ("opportunities", "rate_band", "TEXT"),
    ("opportunities", "acknowledged_at", "TEXT"),
    ("opportunities", "cv_version", "TEXT"),
    # Set in code at store time, never by a model. NOT NULL carries its
    # DEFAULT so the ALTER lands on a populated table without rewriting it,
    # existing rows read as 0, which is the honest answer for adverts
    # stored before the touch log was ever consulted.
    ("opportunities", "buying_window", "INTEGER NOT NULL DEFAULT 0"),
    ("signals", "cv_version", "TEXT"),
    ("signals", "acknowledged_at", "TEXT"),
    # Phase two. Every stored row points at the company it belongs to. The
    # name columns stay where they are, because they are what the page
    # shows and what a restored-from-backup row still has if the join ever
    # breaks. Nullable on purpose, a row that arrives before its company
    # can be resolved is still a row worth keeping.
    ("opportunities", "company_id", "INTEGER"),
    ("signals", "company_id", "INTEGER"),
    ("touches", "company_id", "INTEGER"),
    # Phase two task three. The company's region, mirrored onto the rows so
    # grouping the page is a column read rather than a join on every render.
    # recompute_regions keeps the mirror honest when the rules change.
    ("opportunities", "region", "TEXT"),
    ("signals", "region", "TEXT"),
    # Phase two task five. Drafts written at push time and stored on the
    # signal, so the words are waiting when the phone is picked up. Never
    # sent by anything, only copied by a human.
    ("signals", "draft_comment", "TEXT"),
    ("signals", "draft_note", "TEXT"),
    ("signals", "draft_source", "TEXT"),   # article | headline, what it drafted from
    ("signals", "drafted_at", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # Retirement is absorbed into acknowledgement for opportunities. Rows a
    # human already marked actioned or dead get the acknowledged stamp from
    # their status change, once per database ever, behind a meta flag.
    # Without the flag this mapping would re-run on every connection and
    # silently revert an Undo on a legacy-retired row, the acknowledged
    # stamp coming back the moment the next writer opened the file.
    # Statuses are left in place as history.
    done = conn.execute(
        "SELECT value FROM meta WHERE key = 'migrated_retirement'").fetchone()
    if not done:
        conn.execute(
            "UPDATE opportunities SET acknowledged_at ="
            " COALESCE(status_changed_at, ?)"
            " WHERE status IN ('actioned','dead') AND acknowledged_at IS NULL",
            (now_iso(),))
        # OR IGNORE because two workflows can first-open the upgraded
        # database in the same moment. The loser of that race must shrug,
        # not die on the primary key.
        conn.execute("INSERT OR IGNORE INTO meta (key, value)"
                     " VALUES ('migrated_retirement', '1')")
    # Phase two. Give every row already stored a company to belong to, once
    # per database, behind its own flag for the same reason as above. New
    # rows resolve their company at store time and never come through here.
    # The backfill itself is idempotent, so a hand re-run after the flag is
    # set is harmless, it simply finds nothing to do.
    done_co = conn.execute(
        "SELECT value FROM meta WHERE key = 'migrated_companies'").fetchone()
    if not done_co:
        backfill_companies(conn)
        conn.execute("INSERT OR IGNORE INTO meta (key, value)"
                     " VALUES ('migrated_companies', '1')")
    # Regions are a cache of a config rule, so they are recomputed whenever
    # that rule changes and skipped entirely when it has not. Wrapped
    # because a malformed regions block must not make the database
    # unopenable, the page degrades to Elsewhere instead.
    try:
        recompute_regions(conn, load_config())
    except Exception:                              # noqa: BLE001
        pass


def normalise_company(name) -> str:
    """A company name reduced to something two spellings can agree on.

    Lowercase and collapsed whitespace only. Deliberately not clever, no
    suffix stripping, because turning "Veltrix Diagnostics" into "veltrix"
    would let a different Veltrix borrow a touch history it never earned.

    This is the natural key for the companies table, so changing this rule
    changes which rows are the same company. Do not change it casually.
    """
    return " ".join(str(name or "").lower().split())


def resolve_company(conn, display_name, now: str | None = None):
    """The id of the company row for this name, creating it if new.

    Idempotent. Two workflows racing on the same new company both end up
    pointing at one row, because the insert is OR IGNORE against the unique
    normalised name and the read that follows is what decides the answer.

    Returns None for an empty name rather than inventing a company, since
    an unnamed advert is a real thing and a company called "" is not.
    """
    key = normalise_company(display_name)
    if not key:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO companies (norm_name, display_name, first_seen)"
        " VALUES (?,?,?)",
        (key, str(display_name).strip(), now or now_iso()))
    row = conn.execute(
        "SELECT id FROM companies WHERE norm_name = ?", (key,)).fetchone()
    return row["id"] if row else None


def backfill_companies(conn) -> dict:
    """Give every stored item and touch a company row. Safe to re-run.

    Adds and annotates only. It never rewrites a name, never merges two
    companies, and never clears a company_id that is already set, so a
    second run over a populated database is a no-op that costs one scan.
    """
    made = 0
    linked = {"opportunities": 0, "signals": 0, "touches": 0}
    for table in ("opportunities", "signals", "touches"):
        rows = conn.execute(
            f"SELECT DISTINCT company FROM {table}"
            " WHERE company IS NOT NULL AND TRIM(company) <> ''"
            "   AND company_id IS NULL").fetchall()
        for r in rows:
            key = normalise_company(r["company"])
            if not key:
                continue
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM companies WHERE norm_name = ?",
                (key,)).fetchone()["n"]
            cid = resolve_company(conn, r["company"])
            if cid is None:
                continue
            if not before:
                made += 1
            # Exact match on the stored spelling, because the loop is over
            # distinct stored spellings. Two spellings of one firm both
            # resolve to the same id above, so they meet at the company row
            # without SQL ever needing to know the normalising rule.
            cur = conn.execute(
                f"UPDATE {table} SET company_id = ?"
                f" WHERE company_id IS NULL AND company = ?",
                (cid, r["company"]))
            linked[table] += cur.rowcount
    return {"companies_created": made, "linked": linked}


# Relationship states. The three a human sets are stored on the company
# row. The rest are facts already in the database and are read, not kept.
# Strength order, weakest first. dead is not in this list because it is
# not a rung on the ladder, it ends the climb.
_STATE_ORDER = ("seen", "touched", "window open", "in conversation", "client")


def company_display_state(stored, has_items: bool, has_touches: bool,
                          has_window: bool) -> str:
    """The one state to show for a company. The strongest that applies.

    dead trumps everything, including client, because a human said so and
    no amount of derived activity should argue with them. Otherwise the
    strongest rung wins, and a company with nothing at all reads as new.
    """
    if stored == "dead":
        return "dead"
    candidates = []
    if has_items:
        candidates.append("seen")
    if has_touches:
        candidates.append("touched")
    if has_window:
        candidates.append("window open")
    if stored == "in-conversation":
        candidates.append("in conversation")
    if stored == "client":
        candidates.append("client")
    if not candidates:
        return "new"
    return max(candidates, key=_STATE_ORDER.index)


def company_state(conn, company_id) -> str:
    """company_display_state for one company, reading the facts itself."""
    row = conn.execute("SELECT state FROM companies WHERE id = ?",
                       (company_id,)).fetchone()
    stored = row["state"] if row else None
    def any_row(sql):
        return conn.execute(sql, (company_id,)).fetchone() is not None
    return company_display_state(
        stored,
        any_row("SELECT 1 FROM opportunities WHERE company_id = ? LIMIT 1")
        or any_row("SELECT 1 FROM signals WHERE company_id = ? LIMIT 1"),
        any_row("SELECT 1 FROM touches WHERE company_id = ? LIMIT 1"),
        any_row("SELECT 1 FROM signals WHERE company_id = ?"
                " AND source_id = 'job-advert' LIMIT 1"))


_REGION_FALLBACK = "Elsewhere"


def _norm_place(value) -> str:
    return " ".join(str(value or "").lower().split())


def region_for(country, city, config: dict) -> str:
    """Which group an insight belongs to. First rule that matches wins.

    The last group catches everything left, including a company nobody has
    enriched yet, because a company with no country must still land
    somewhere. Vanishing off the page is the one outcome a grouping rule
    must never produce.
    """
    groups = config.get("regions") or []
    if not groups:
        return _REGION_FALLBACK
    c_country, c_city = _norm_place(country), _norm_place(city)
    for g in groups:
        how = str(g.get("match") or "").lower()
        if how == "places":
            places = {_norm_place(p) for p in (g.get("local_places") or [])}
            if (c_city and c_city in places) or (c_country and c_country in places):
                return str(g.get("name") or _REGION_FALLBACK)
        elif how == "countries":
            names = {_norm_place(n) for n in (g.get("countries") or [])}
            if c_country and c_country in names:
                return str(g.get("name") or _REGION_FALLBACK)
        elif how == "rest":
            return str(g.get("name") or _REGION_FALLBACK)
    last = groups[-1]
    return str(last.get("name") or _REGION_FALLBACK)


def region_group_names(config: dict) -> list:
    """Group names in configured order, for rendering and for the digest."""
    return [str(g.get("name")) for g in (config.get("regions") or [])
            if g.get("name")] or [_REGION_FALLBACK]


def regions_fingerprint(config: dict) -> str:
    """A stable hash of the region rules, so a config edit can be noticed."""
    return hashlib.sha256(
        json.dumps(config.get("regions") or [], sort_keys=True,
                   ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def recompute_regions(conn, config: dict, force: bool = False) -> dict:
    """Recompute company regions when the rules change, and mirror them.

    The region is stored on companies and mirrored onto opportunities and
    signals so the page groups by reading a column rather than joining on
    every render. That mirror is a cache, and this is what keeps a cache
    honest when the thing it copies moves.

    Cheap when nothing changed. The fingerprint of the rules is kept in
    meta, and an unchanged fingerprint means an early return.
    """
    fp = regions_fingerprint(config)
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'regions_fingerprint'").fetchone()
    # The rules changing is not the only way this cache goes stale. Enrichment
    # fills in a country long after the company row was created, and a company
    # created since the last pass has no region at all. Either leaves rows
    # grouped as Elsewhere while the database plainly knows better, which is
    # exactly what happened on 3 August, so an unplaced row is also a reason
    # to do the work.
    unplaced = conn.execute(
        "SELECT 1 FROM companies WHERE region IS NULL LIMIT 1").fetchone()
    if not force and row and row["value"] == fp and not unplaced:
        return {"changed": False, "companies": 0}
    updated = 0
    for co in conn.execute(
            "SELECT id, country, city FROM companies").fetchall():
        want = region_for(co["country"], co["city"], config)
        cur = conn.execute(
            "UPDATE companies SET region = ? WHERE id = ? AND"
            " (region IS NOT ? OR region IS NULL)", (want, co["id"], want))
        updated += cur.rowcount
        conn.execute("UPDATE opportunities SET region = ? WHERE company_id = ?",
                     (want, co["id"]))
        conn.execute("UPDATE signals SET region = ? WHERE company_id = ?",
                     (want, co["id"]))
    # Rows with no company at all still need a group, and Elsewhere is the
    # honest one. A row with no region reads as a bug on the page.
    fallback = region_group_names(config)[-1]
    for t in ("opportunities", "signals"):
        conn.execute(f"UPDATE {t} SET region = ? WHERE region IS NULL", (fallback,))
    conn.execute("INSERT INTO meta (key, value) VALUES ('regions_fingerprint', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (fp,))
    return {"changed": True, "companies": updated}


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the radar database, applying schema and migrations idempotently.

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
    _migrate(conn)
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
    letter capitalised. Exclamation marks end their sentence as full
    stops. Mid-sentence colons become commas, except between digits, a
    time like 07:30 is not punctuation. Doubled spaces and stray double
    stops collapse. The worked case is the Meridian wrong-rate line:

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
    # An exclamation mark ends its sentence as a plain full stop.
    s = re.sub(r"\s*!+\s*(\w)", lambda m: ". " + m.group(1).upper(), s)
    s = re.sub(r"\s*!+", ".", s)
    # A mid-sentence colon reads as a comma, a trailing one as a stop.
    # Only digit-colon-digit survives, 07:30 is a time not punctuation,
    # so the exemption needs digits on BOTH sides. A one-sided check
    # would leave "floor is 650: a hard number" untouched. Times hide
    # behind a placeholder while everything else converts, and :// is
    # left alone rather than silently mangling a stray URL.
    s = re.sub(r"(?<=\d):(?=\d)", "\x00", s)
    s = re.sub(r"\s*:(?!/)\s*$", ".", s)
    s = re.sub(r"\s*:(?!/)\s*", ", ", s)
    s = s.replace("\x00", ":")
    # Tidy the seams the substitutions can leave.
    s = re.sub(r"\.(\s*\.)+", ".", s)
    s = re.sub(r"\.\s*,", ".", s)
    s = re.sub(r",\s*\.", ".", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r",\s*$", ".", s)
    s = re.sub(r" {2,}", " ", s)
    return s.strip()


# ---------------------------------------------------------------- job sources

# The boards the radar was born knowing. Custom ones live in
# config/job_sources.yaml, personal config like the profile, written by
# the Jobs page and readable by hand, gitignored so it travels by hand.
JOB_SOURCES_PATH = REPO_ROOT / "config" / "job_sources.yaml"
BUILTIN_JOB_SOURCES = (
    {"id": "linkedin-alert", "name": "LinkedIn", "sender_contains": "linkedin",
     "url": "https://www.linkedin.com/jobs/", "badge": "in",
     "colour": "#0A66C2"},
    {"id": "reed-alert", "name": "Reed", "sender_contains": "reed",
     "url": "https://www.reed.co.uk/", "badge": "R", "colour": "#E0257B"},
    {"id": "indeed-alert", "name": "Indeed", "sender_contains": "indeed",
     "url": "https://uk.indeed.com/", "badge": "i", "colour": "#2557A7"},
    {"id": "cvlibrary-alert", "name": "CV-Library",
     "sender_contains": "cv-library",
     "url": "https://www.cv-library.co.uk/", "badge": "CV",
     "colour": "#0E7490"},
)


def load_job_sources(path: Path | None = None) -> list[dict]:
    """Built-in boards first, then whatever the Jobs page has added."""
    sources = [dict(s) for s in BUILTIN_JOB_SOURCES]
    path = path or JOB_SOURCES_PATH
    if path.exists():
        import yaml
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            print(f"WARNING. {path} would not parse, custom job sources "
                  "skipped this run.", file=sys.stderr)
            return sources
        for entry in data.get("sources") or []:
            if (isinstance(entry, dict) and entry.get("id")
                    and entry.get("sender_contains")):
                custom = {
                    "id": str(entry["id"]),
                    "name": str(entry.get("name") or entry["id"]),
                    "sender_contains": str(entry["sender_contains"]).lower(),
                }
                if entry.get("url"):
                    custom["url"] = str(entry["url"])
                sources.append(custom)
    return sources


def add_job_source(name: str, sender_contains: str,
                   url: str | None = None,
                   path: Path | None = None) -> dict:
    """Append one custom source to the registry. Refuses duplicates.

    The id is the slugged name plus -alert, matching the built-in shape,
    so detect_source and the dashboard treat customs exactly like the
    boards the radar shipped with. The optional url powers the section
    heading's open-the-site arrow and nothing else.
    """
    path = path or JOB_SOURCES_PATH
    name = " ".join((name or "").split())[:60]
    sender = (sender_contains or "").strip().lower()[:120]
    site = (url or "").strip()[:200]
    if site:
        if not re.match(r"^https?://", site, re.IGNORECASE):
            site = "https://" + site
        if " " in site or "." not in site.split("//", 1)[-1]:
            raise ValueError("The site does not look like a web address. "
                             "Something like technojobs.co.uk works, or "
                             "leave it empty.")
    if not name or len(name) < 2:
        raise ValueError("The source needs a name, two characters or more.")
    if not sender or len(sender) < 3:
        raise ValueError("The sender match needs three characters or more, "
                         "part of the board's From address, for example "
                         "jobs@theboard.com or theboard.")
    slug = re.sub(r"-{2,}", "-",
                  re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")
    if not slug:
        raise ValueError("The name needs at least one letter or digit.")
    source_id = f"{slug}-alert"
    existing = load_job_sources(path)
    for s in existing:
        if s["id"] == source_id:
            raise ValueError(f"A source called {name} already exists.")
        if s["sender_contains"] == sender:
            raise ValueError(f"{s['name']} already matches that sender.")

    customs = [s for s in existing
               if s["id"] not in {b["id"] for b in BUILTIN_JOB_SOURCES}]
    new_entry = {"id": source_id, "name": name, "sender_contains": sender}
    if site:
        new_entry["url"] = site
    customs.append(new_entry)
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Custom job alert sources, written by the Jobs page and read by\n"
        "# the inbox pipeline. Edit or delete lines freely, the built-in\n"
        "# boards live in code and are not listed here.\n"
        + yaml.safe_dump({"sources": customs}, sort_keys=False,
                         allow_unicode=True),
        encoding="utf-8")
    return dict(new_entry)


# ----------------------------------------------------------------- rate bands

# Salaried pay converts to a day rate at this many working days a year, per
# the preferences file's own comparison rule.
WORKING_DAYS_PER_YEAR = 220
HOURS_PER_DAY = 8
CLOSE_BAND_GBP = 50  # under the floor by up to this much reads as close

_FLOOR_LINE = re.compile(r"^day_rate_floor_gbp:\s*([0-9]+(?:\.[0-9]+)?)\s*$",
                         re.IGNORECASE | re.MULTILINE)


# The labelled lines the dashboard may edit inside the preferences file.
# The scorer reads the whole file, so a value set here flows into scoring
# with no other plumbing. Everything else in that file stays Luke's prose.
PREF_EDITABLE = {
    "day_rate_floor_gbp": "number",
    "target_title": "text",
    "keywords": "text",
}


def _prefs_path(config: dict | None = None) -> Path:
    config = config or load_config()
    prefs_rel = config.get("prefs_file", "config/profile/preferences.md")
    live = REPO_ROOT / prefs_rel
    if live.exists():
        return live
    return REPO_ROOT / "config" / "preferences.template.md"


def read_pref_line(key: str, config: dict | None = None) -> str | None:
    """The value of one labelled line, or None when the line is absent."""
    if key not in PREF_EDITABLE:
        raise ValueError(f"{key} is not a dashboard-editable line.")
    path = _prefs_path(config)
    if not path.exists():
        return None
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$",
                      path.read_text(encoding="utf-8"),
                      re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else None


def update_pref_line(key: str, value: str,
                     config: dict | None = None) -> str:
    """Surgically set one labelled line in the preferences file.

    Replaces the line in place when it exists, otherwise appends a small
    dashboard-managed section at the end. Never touches any other line,
    the file is Luke's prose and stays that way. Returns the stored value.
    """
    if key not in PREF_EDITABLE:
        raise ValueError(f"{key} is not a dashboard-editable line.")
    value = " ".join(str(value or "").split())[:200]
    if not value:
        raise ValueError("An empty value would delete the line. Type "
                         "something, or edit the file by hand to remove it.")
    if PREF_EDITABLE[key] == "number":
        try:
            number = float(value.replace("£", "").replace(",", ""))
        except ValueError as err:
            raise ValueError(f"{key} needs a plain number.") from err
        if not 0 < number <= 10000:
            raise ValueError(f"{key} of {number:g} is outside any sane "
                             "day-rate range.")
        value = f"{number:g}"
    path = _prefs_path(config)
    if not path.exists():
        raise ValueError(f"No preferences file at {path} to edit.")
    text = path.read_text(encoding="utf-8")
    line = f"{key}: {value}"
    pattern = re.compile(rf"^{re.escape(key)}:.*$",
                         re.IGNORECASE | re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        if "## Title and keywords." not in text:
            text = (text.rstrip() + "\n\n## Title and keywords.\n\n"
                    "Lines the dashboard's scoring panel manages. The "
                    "scorer reads them as part of this file.\n\n")
        text = text.rstrip() + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")
    return value


def read_rate_floor(config: dict | None = None) -> float:
    """The day-rate floor in GBP, from the one machine-readable line.

    The preferences file is the single home of desire, so the floor lives
    there as a clearly labelled line, day_rate_floor_gbp: 650, read by both
    the scorer prompt (the model sees the whole file) and this code. Live
    copy first, tracked template second, loud failure when neither carries
    the line, because banding against a guessed floor would be worse than
    no banding at all.
    """
    config = config or load_config()
    prefs_rel = config.get("prefs_file", "config/profile/preferences.md")
    candidates = [REPO_ROOT / prefs_rel,
                  REPO_ROOT / "config" / "preferences.template.md"]
    for path in candidates:
        if path.exists():
            match = _FLOOR_LINE.search(path.read_text(encoding="utf-8"))
            if match:
                return float(match.group(1))
            raise RuntimeError(
                f"No day_rate_floor_gbp line in {path}. Add one to the Rates "
                "section, for example 'day_rate_floor_gbp: 650', so banding "
                "and the scorer read the same floor.")
    raise RuntimeError(
        "No preferences file found for the rate floor. Expected "
        f"{candidates[0]} or {candidates[1]}.")


def convert_to_day_rate(currency, period, pay_min, pay_max,
                        config: dict | None = None) -> float | None:
    """Deterministic conversion to a GBP day rate, or None when unusable.

    Annual divides by 220 working days. Hourly multiplies by 8. Daily
    passes through. Euro amounts convert at eur_to_gbp from radar.yaml.
    Where a range is stated the band sits on the top of it, the best case,
    and the dashboard legend says so. Currencies without a configured
    conversion return None rather than a guessed figure.
    """
    amounts = [a for a in (pay_min, pay_max)
               if isinstance(a, (int, float)) and a > 0]
    if not amounts:
        return None
    amount = float(max(amounts))

    period = (period or "").strip().lower()
    if period == "year":
        amount /= WORKING_DAYS_PER_YEAR
    elif period == "hour":
        amount *= HOURS_PER_DAY
    elif period != "day":
        return None

    currency = (currency or "").strip().upper()
    if currency == "EUR":
        config = config or load_config()
        amount *= float(config.get("eur_to_gbp", 0.85))
    elif currency != "GBP":
        return None
    return amount


def band_for(day_rate: float | None, floor: float) -> str:
    """above | close | below | unstated, judged in code, never by the model."""
    if day_rate is None:
        return "unstated"
    if day_rate >= floor:
        return "above"
    if day_rate >= floor - CLOSE_BAND_GBP:
        return "close"
    return "below"


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
