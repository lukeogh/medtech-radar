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
import http.client
import json
import os
import re
import sqlite3
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
    r"midsig|eid|otpToken|lipi)", re.IGNORECASE
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
    # Phase two task seven. What came back from a touch. Replies were
    # happening and going uncounted, so the doctrine could not be measured.
    ("touches", "outcome", "TEXT"),
    # Phase three. The four gates, their notes, and the tier derived from
    # them. Nullable throughout, because a row scored before the gates
    # existed has no honest answer and inventing one would be worse.
    ("opportunities", "gate_sector", "INTEGER"),
    ("opportunities", "gate_sector_note", "TEXT"),
    ("opportunities", "gate_cv", "INTEGER"),
    ("opportunities", "gate_cv_note", "TEXT"),
    ("opportunities", "gate_location", "INTEGER"),
    ("opportunities", "gate_location_note", "TEXT"),
    ("opportunities", "gate_rate", "INTEGER"),
    ("opportunities", "gate_rate_note", "TEXT"),
    ("opportunities", "tier", "TEXT"),
    ("opportunities", "failed_gates", "TEXT"),
    ("opportunities", "question_text", "TEXT"),
    ("opportunities", "filter_reason", "TEXT"),
    ("opportunities", "rate_stated", "INTEGER"),
    ("opportunities", "rate_value", "REAL"),
    ("opportunities", "rate_basis", "TEXT"),
    ("opportunities", "ir35", "TEXT"),
    # Brief C. What the page checker reads off the job page while it is
    # there: the advert's own description, and where the alert card was
    # silent on pay, the page's stated figure with its source recorded.
    ("opportunities", "description_text", "TEXT"),
    ("opportunities", "pay_source", "TEXT"),       # card | page, where pay_* came from
    ("opportunities", "location_class", "TEXT"),
    # Engagement type, contract or permanent or unknown, read off the
    # advert by the scorer for new rows and by the regex backfill for the
    # backlog. The note is the phrase that decided it, so nothing is
    # asserted that the advert did not say.
    ("opportunities", "engagement_type", "TEXT"),
    ("opportunities", "engagement_note", "TEXT"),
    # Closure detection, stage one. A duplicate card in an alert is a
    # sighting of a live advert, so the row keeps the latest date and a
    # count instead of discarding them at the door. seen_count carries its
    # DEFAULT so the ALTER lands on a populated table, existing rows
    # reading 1, the one sighting their first_seen already records.
    ("opportunities", "last_seen", "TEXT"),
    ("opportunities", "seen_count", "INTEGER NOT NULL DEFAULT 1"),
    # Closure detection, stage two. The lifecycle stamps, all nullable,
    # NULL meaning it has not happened. closed_source says who confirmed
    # the close, manual or page_check, because no row is ever marked
    # closed without confirmation. The last_check pair belongs to the
    # stage three page checker and lands now so one migration carries
    # the whole lifecycle.
    ("opportunities", "closed_at", "TEXT"),
    ("opportunities", "closed_source", "TEXT"),
    ("opportunities", "dismissed_at", "TEXT"),
    ("opportunities", "expired_at", "TEXT"),
    ("opportunities", "last_checked_at", "TEXT"),
    ("opportunities", "last_check_result", "TEXT"),
    # Closure detection, stage four. One role across its reposts,
    # normalised title plus normalised company. Set in code at store
    # time, backfilled once below, so a repost chain is a GROUP BY
    # rather than a render-time fuzzy pass. Nullable, because a row
    # missing either half cannot be told from its neighbours and
    # honestly stands alone.
    ("opportunities", "role_key", "TEXT"),
    # Score record-keeping. How each score was made, set in code at the
    # moment the score is made, never by a model. All nullable, because a
    # row that was never scored has none of them. A row scored before the
    # record was kept is labelled by the backfill in _migrate, as card,
    # pre-record, unknown and no date, because the history was not kept and
    # is not guessed.
    ("opportunities", "score_basis", "TEXT"),      # card | spec, what the scorer was shown
    ("opportunities", "prompt_version", "TEXT"),   # hash of prompts/scorer.md, or pre-record
    ("opportunities", "score_model", "TEXT"),      # the model that answered, or unknown
    ("opportunities", "scored_at", "TEXT"),        # when, NULL where it was never recorded
    # Applied roles. The moment a human said "I applied", and the saved CV
    # that went with the application. Both are set by the Applied button and
    # cleared by its Undo, never by a model or a script, and both are NULL on
    # a role that has not been applied for. Columns and not a status, because
    # the status CHECK cannot take a new word without a table rebuild.
    # applied_cv is the name Save CV gave the role's tailored copy, read from
    # tailored_cvs as the role is stamped, or empty when no copy was saved.
    # It is still text to escape wherever it is shown and never to join onto
    # a path. One row carries the stamp and the whole repost chain counts as
    # applied, see APPLIED_SQL.
    ("opportunities", "applied_at", "TEXT"),
    ("opportunities", "applied_cv", "TEXT"),
    # Prepare application. description_text has held the advert's own words
    # since the page checker first read a page. These three say more about
    # it and about the score made on it. All nullable, set in code, never by a
    # model, and NULL on every row that Prepare application has not touched.
    # description_fetched_at is when the text was stored. description_hash is
    # the version of the description the score the row holds was made against
    # (description_version), NULL for a card score, and is what lets a stored
    # description be scored once per version and no more. score_working is the
    # per-ask working of that score as JSON: each ask the advert makes, met,
    # partly or missing, with the CV lines that evidence it. It only means
    # anything beside the score it was made with, and readers check that.
    ("opportunities", "description_fetched_at", "TEXT"),
    ("opportunities", "description_hash", "TEXT"),
    ("opportunities", "score_working", "TEXT"),
    # The history keeps the same two for every score it holds, and tier_from,
    # which is set only on the score that moved a role from one tier to
    # another, with the tier it moved from.
    ("score_history", "description_hash", "TEXT"),
    ("score_history", "working", "TEXT"),
    ("score_history", "tier_from", "TEXT"),
    # The tailored CV. A score of a role's tailored copy is kept in the
    # history with event tailored, and cv_hash is the version of the copy it
    # was made against (cv_store.cv_hash), so that one pair of description and
    # copy is scored once. NULL on every other score. The copies themselves
    # are the table tailored_cvs in schema.sql.
    ("score_history", "cv_hash", "TEXT"),
    # How a run ended: ok, partial (it finished, but a source or an item
    # failed on the way) or failed (it stopped before the end). A run used to
    # write its row only as its last step, so a run that died part-way left
    # nothing, and a stopped job could only show as a row that had grown old.
    # NULL on every row written before this, all of them runs that finished.
    ("runs", "outcome", "TEXT"),
    # Quoted reasons. Up to four reasons the scorer gave for a score, each with
    # the stretch of the advert it rests on, as JSON beside the score it was
    # made with (score_item.normalise_reasons). A quote is kept only where code
    # found it, word for word, in the text that was scored, the alert card or
    # the stored description, and a quote it could not find is dropped with the
    # drop recorded on its reason. Set in code when a score is made, on the
    # card path and the full description path alike, and never by a migration:
    # every row and every history row scored before this was kept stays NULL,
    # because those scores were made without reasons and nothing is re-scored
    # to give them some. The history keeps the same JSON as reasons.
    ("opportunities", "score_reasons", "TEXT"),
    ("score_history", "reasons", "TEXT"),
    # The Jobs page's New. When a human first opened a role, or acted on it.
    # A role is read when any advert on its chain carries the stamp, so a
    # repost of a role already read arrives read (mark_read stamps the chain
    # as it stands, and the page asks the chain, see role_read_keys). Set only
    # by the Jobs page through POST /read and the verdict routes, never by a
    # model or a pipeline. Every row on file when the column arrives is
    # stamped once, below, so the switch-on shows nothing as new.
    ("opportunities", "read_at", "TEXT"),
    # Insights as a news page (design/insights/news, B). What kind of story a
    # signal is, one of STORY_TYPES, and a one-sentence card summary of about
    # 90 characters, both from the signal scorer, or set in code for the
    # hiring signals the code writes itself. NULL on a row scored before them
    # until scripts/backfill_stories.py fills it. event_key ties together the
    # reports of one event (same company, within seven days, similar headline),
    # set in code at store time by event_key_for and never by a model; a key
    # starting "solo-" was split off by a human and is never regrouped.
    ("signals", "story_type", "TEXT"),
    ("signals", "card_summary", "TEXT"),
    ("signals", "event_key", "TEXT"),
    # The Watchlist. When a human added the company, NULL when it is not on
    # the list. Set by Add to Watchlist on Insights and cleared by its Undo.
    ("companies", "watched_at", "TEXT"),
    # The tokens a call wrote to the prompt cache, which are billed at 1.25
    # times the input price (cache_read_tokens are read back at 0.1 times).
    # They were never recorded, so a run's cost read low. 0 on every row
    # written before this, which is what was recorded, not what was spent.
    ("runs", "cache_write_tokens", "INTEGER DEFAULT 0"),
    # The Watchlist final (design/watchlist/final). How a company joined:
    # insights (Add to Watchlist on a story), suggestion (Radar suggested it
    # and the owner added it), name (added by name on the Watchlist) or
    # contact (a contact was logged for a company not on it), and the story
    # it came from where there was one. arrival_seen_at is when the Watchlist
    # first said it had arrived from Insights, so the next visit says so once.
    # unwatched_at is when it was last taken off; its thread stays on file
    # and stops counting, and adding it again restores it. watched_was keeps
    # the watched_at a removal cleared, for Remove's Undo.
    ("companies", "watched_via", "TEXT"),
    ("companies", "watched_signal_id", "INTEGER"),
    ("companies", "arrival_seen_at", "TEXT"),
    ("companies", "unwatched_at", "TEXT"),
    ("companies", "watched_was", "TEXT"),
    # The site the enricher read, so Read again can make its one polite fetch
    # of the same page, and the source and date of each fact as JSON
    # ({"stage": {"from": "site"|"item", "label": "...", "at": "..."}}), set
    # when the facts are read. NULL on a company read before they were kept.
    ("companies", "site_url", "TEXT"),
    ("companies", "fact_sources", "TEXT"),
    # What a touch is. NULL for a contact; state for the row Set state writes
    # to say who changed a company's state, which is not a contact and never
    # opens, closes or moves a thread.
    ("touches", "kind", "TEXT"),
    # Roles added by hand (hand_roles). added_at is when Luke added the role
    # on Jobs, NULL on every collected row, which is what tells the two apart;
    # hand_type is the type he chose, contract, permanent or fixed-price, kept
    # apart from engagement_type because the scorer writes that one. Both set
    # in code by the Add a role dialog, never by a model, and NULL on every row
    # on file when they arrive, which is the honest answer: none was added by
    # hand.
    ("opportunities", "added_at", "TEXT"),
    ("opportunities", "hand_type", "TEXT"),
)

# What the score record says about a row scored before the record existed.
SCORE_BASIS_CARD = "card"        # the advert card an alert email carries
SCORE_BASIS_SPEC = "spec"        # the full job spec, when a scorer is ever given one
PRE_RECORD_PROMPT = "pre-record"
UNKNOWN_MODEL = "unknown"

# A row has a score when any of the columns a scorer fills is present. A row
# that failed to score has all of them NULL and nothing to keep or label.
_SCORED_SQL = ("(cv_match IS NOT NULL OR want_match IS NOT NULL"
               " OR combined IS NOT NULL OR tier IS NOT NULL"
               " OR one_line_why IS NOT NULL)")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            # No such table, so nothing to add to. get_db has always run the
            # schema first, which creates every table here, so this is only a
            # file opened by hand without it.
            continue
        if column not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError as err:
                # Two processes can first-open an upgraded database in the
                # same moment, and the loser finds the column its rival has
                # just added. That is not a failure, anything else is.
                if "duplicate column name" not in str(err):
                    raise
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
    # Closure detection stage one. Rows stored before sightings were
    # recorded read as last seen when they were first seen, the only honest
    # date on file. The UPDATE is idempotent, the flag only keeps the open
    # cheap, matching the pattern above.
    done_seen = conn.execute(
        "SELECT value FROM meta WHERE key = 'migrated_last_seen'").fetchone()
    if not done_seen:
        conn.execute("UPDATE opportunities SET last_seen = first_seen"
                     " WHERE last_seen IS NULL")
        conn.execute("INSERT OR IGNORE INTO meta (key, value)"
                     " VALUES ('migrated_last_seen', '1')")
    # Closure detection stage four. Every stored row gets its role key,
    # once per database, the same manners as the backfills above. The
    # key is computed in code, so this is a loop rather than an UPDATE,
    # and it is idempotent, a re-run finds nothing with a NULL key left.
    done_role = conn.execute(
        "SELECT value FROM meta WHERE key = 'migrated_role_key'").fetchone()
    if not done_role:
        for row in conn.execute(
                "SELECT id, title, company FROM opportunities"
                " WHERE role_key IS NULL").fetchall():
            key = role_key(row["title"], row["company"])
            if key:
                conn.execute("UPDATE opportunities SET role_key = ?"
                             " WHERE id = ?", (key, row["id"]))
        conn.execute("INSERT OR IGNORE INTO meta (key, value)"
                     " VALUES ('migrated_role_key', '1')")
    # The Jobs page's New. Every row on file when read_at arrives is stamped
    # read, once per database ever, behind its flag, so switching it on shows
    # nothing as new and only roles that arrive afterwards are. Without the
    # flag this would run on every open and read every role as it landed.
    # Stamped with the moment of the migration, not a guess at when each
    # role was looked at.
    done_read = conn.execute(
        "SELECT value FROM meta WHERE key = 'migrated_read_at'").fetchone()
    if not done_read:
        conn.execute("UPDATE opportunities SET read_at = ? WHERE read_at IS NULL",
                     (now_iso(),))
        conn.execute("INSERT OR IGNORE INTO meta (key, value)"
                     " VALUES ('migrated_read_at', '1')")
    # Score record-keeping. Every scored row that carries no basis gets the
    # honest label for a score made before the record was kept. That is the
    # card, because it is all any scorer has been given, a prompt version of
    # pre-record, an unknown model and no date. Nothing is guessed. Unlike
    # the backfills above this has no meta flag, because it is a plain
    # idempotent UPDATE and the read below keeps it from taking the write
    # lock on the many opens that find nothing to do. That also labels a row
    # that an old copy of the code scores in the moment of a deploy, which
    # is exactly what it is.
    if conn.execute("SELECT 1 FROM opportunities WHERE score_basis IS NULL"
                    " AND " + _SCORED_SQL + " LIMIT 1").fetchone():
        conn.execute(
            "UPDATE opportunities SET score_basis = ?, prompt_version = ?,"
            " score_model = ? WHERE score_basis IS NULL AND " + _SCORED_SQL,
            (SCORE_BASIS_CARD, PRE_RECORD_PROMPT, UNKNOWN_MODEL))
    # Regions are a cache of a config rule, so they are recomputed whenever
    # that rule changes and skipped entirely when it has not. Wrapped
    # because a malformed regions block must not make the database
    # unopenable, the page degrades to Elsewhere instead.
    try:
        recompute_regions(conn, load_config())
    except Exception:                              # noqa: BLE001
        pass
    # The applications board. Every role applied for gets its application in
    # Applied, dated from applied_at, and every role with a saved tailored CV
    # and no application gets one in Preparing, dated from saved_at: once per
    # database, the first time a writer opens it after the board arrives,
    # behind its own flag like the backfills above. After that Save CV, Mark
    # applied and the board make applications as they go, and the scheduled
    # rule (applications.run_rules) makes any that something else left out.
    # The rows Set state has always written are marked as what they are,
    # once per database, so a state change never reads as the newest contact
    # and closes the thread it sat on top of.
    done_kind = conn.execute(
        "SELECT value FROM meta WHERE key = 'migrated_touch_kind'").fetchone()
    if not done_kind:
        conn.execute("UPDATE touches SET kind = 'state' WHERE kind IS NULL"
                     " AND channel = 'other' AND note LIKE 'State set to %'")
        conn.execute("INSERT OR IGNORE INTO meta (key, value)"
                     " VALUES ('migrated_touch_kind', '1')")
    done_apps = conn.execute(
        "SELECT value FROM meta WHERE key = 'migrated_applications'").fetchone()
    if not done_apps:
        import applications
        applications.sync(conn)
        conn.execute("INSERT OR IGNORE INTO meta (key, value)"
                     " VALUES ('migrated_applications', '1')")


LIVE_WINDOW_DAYS = 7


def _row_field(row, key):
    """One column off a dict or sqlite3.Row, a missing column reads None."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def latest_liveness(row) -> str:
    """The newest sign of life on one row, as a timestamp string.

    A sighting, or a page check that answered live. A check that
    answered anything else said nothing about life and does not count.
    A legacy row without the lifecycle columns judges on first_seen,
    the only honest date on file.
    """
    checked = (_row_field(row, "last_checked_at") or ""
               if _row_field(row, "last_check_result") == "live" else "")
    return max(_row_field(row, "last_seen")
               or _row_field(row, "first_seen") or "", checked)


def life_state(row, now: str | None = None) -> str:
    """Live, unknown or closed for one opportunity row. Derived, never stored.

    Closed is a confirmed stamp and outranks everything, including a
    fresh sighting. Live is any sighting, or a live page check, inside
    the window. Everything else is unknown, and the page shows the age
    plainly rather than guessing.
    """
    if _row_field(row, "closed_at"):
        return "closed"
    moment = datetime.strptime(now or now_iso(), "%Y-%m-%dT%H:%M:%SZ")
    cutoff = ((moment - timedelta(days=LIVE_WINDOW_DAYS))
              .strftime("%Y-%m-%dT%H:%M:%SZ"))
    return "live" if latest_liveness(row) >= cutoff else "unknown"


def role_key(title, company) -> str | None:
    """One role across its reposts: normalised title plus normalised company.

    The matcher both closure studies validated. The title drops case,
    punctuation and doubled spaces, the company goes through the same
    rule the companies table keys on. Location stays out on purpose,
    same-day copies in several cities are one role. Either half missing
    means the chain cannot be told from its neighbours, so the key is
    None and the row honestly stands alone.
    """
    cleaned = re.sub(r"[\W_]+", " ", str(title or "").casefold()).strip()
    named = normalise_company(company) if company else ""
    if not cleaned or not named:
        return None
    return f"{cleaned}|{named}"


# A row is on an applied role when it, or any row sharing its role_key, has
# applied_at set. That is the whole rule, and it is worked out each time a
# query runs. It is not copied onto the rest of the chain, because a repost
# arrives as a new row with every stamp NULL and a copy would never reach it,
# while this reaches it the moment it is stored. A row with no role_key stands
# alone, the NULL never matches. The subquery names no column of the outer
# row, so SQLite runs it once per statement. Every statement that picks open
# rows, or changes one, adds NOT_APPLIED_SQL to its WHERE. Written with bare
# column names so it works in a plain query and in an UPDATE of the table.
APPLIED_SQL = (
    "(applied_at IS NOT NULL OR (role_key IS NOT NULL AND role_key IN"
    " (SELECT role_key FROM opportunities"
    " WHERE applied_at IS NOT NULL AND role_key IS NOT NULL)))")
NOT_APPLIED_SQL = "NOT " + APPLIED_SQL


def mark_read(conn, opportunity_id: int, key=None) -> int:
    """Stamp read_at on a role's chain where it is not stamped. The caller
    commits. Returns the rows stamped, 0 when the role was read already.

    The chain is every advert sharing the role_key, as the verdicts reach
    them; a row without a key is stamped alone. key is the row's role_key
    when the caller has read it already.
    """
    if key is None:
        found = conn.execute("SELECT role_key FROM opportunities WHERE id = ?",
                             (opportunity_id,)).fetchone()
        key = found[0] if found else None
    cur = conn.execute(
        "UPDATE opportunities SET read_at = ? WHERE read_at IS NULL"
        " AND (id = ? OR (role_key IS NOT NULL AND role_key = ?))",
        (now_iso(), opportunity_id, key))
    return cur.rowcount


def role_read_keys(conn) -> set | None:
    """The roles that have been read, as the Jobs page keys them: the
    role_key, or row-<id> for a row with none. None when the database has no
    read_at column yet, which reads as every role read, the state a migration
    would leave it in."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(opportunities)")}
    if "read_at" not in have:
        return None
    return {r[0] or f"row-{r[1]}" for r in conn.execute(
        "SELECT role_key, id FROM opportunities WHERE read_at IS NOT NULL")}


def applied_role_keys(conn) -> set:
    """The role keys that have an applied row, for code that filters in Python."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT role_key FROM opportunities"
        " WHERE applied_at IS NOT NULL AND role_key IS NOT NULL")}


def row_is_applied(row, keys) -> bool:
    """APPLIED_SQL for one loaded row, given applied_role_keys of the same moment."""
    key = _row_field(row, "role_key")
    return bool(_row_field(row, "applied_at")) or (bool(key) and key in keys)


def role_is_applied(conn, opportunity_id) -> bool:
    """Is this row's role applied. A row that does not exist is not."""
    return conn.execute("SELECT 1 FROM opportunities WHERE id = ? AND "
                        + APPLIED_SQL, (opportunity_id,)).fetchone() is not None


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


def begin_write(conn: sqlite3.Connection) -> None:
    """Take the write lock now, unless a transaction already holds it.

    For a writer that reads, decides and then writes. Anything that commits
    between the read and the write, a role being applied for among it, is
    not seen by the read and is overwritten by the write. With the lock taken
    first, what was read is what is written. archive_score does the same for
    the score it keeps. Python's sqlite3 opens a transaction only at the
    first INSERT, UPDATE or DELETE, so without this a read is never inside
    one. The caller commits, or rolls back to let the lock go.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


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


RUN_OUTCOMES = ("ok", "partial", "failed")


def log_run(conn: sqlite3.Connection, workflow: str, mode: str = "live",
            items_in: int = 0, items_new: int = 0, model: str | None = None,
            usage: dict | None = None, note: str | None = None,
            outcome: str | None = None) -> None:
    """One row in runs for one execution.

    outcome is ok, partial or failed, for a job that says how it ended, and
    is left NULL by the jobs that do not. A NULL reads as a run that finished.
    """
    if outcome is not None and outcome not in RUN_OUTCOMES:
        raise ValueError(f"unknown run outcome {outcome!r}")
    usage = usage or {}
    conn.execute(
        "INSERT INTO runs (ts, workflow, mode, items_in, items_new, model,"
        " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        " note, outcome)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), workflow, mode, items_in, items_new, model,
         usage.get("input_tokens", 0), usage.get("output_tokens", 0),
         usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0),
         note, outcome),
    )
    conn.commit()


# ------------------------------------------------------------- job health

# How long each scheduled job may go without a finished run before it reads
# as late, a word on Home, and then as stale, a fault: /health answers
# degraded. Hours. One figure for every job cannot be right, because the
# jobs do not run alike.
#
# - The inbox has no schedule of its own that writes anything. n8n polls
#   Gmail hourly, but a row is written only when an email is processed, and
#   alert mail arrives unevenly through the day, so a quiet night is normal.
#   A day with no email at all is not: the trigger, the Gmail credential or
#   the alert subscriptions have stopped.
# - The signal watcher runs every two hours. Late is two runs missed in a
#   row, stale is three, the six hours /health has always allowed it.
# - The advert checker runs once a day and takes up to about 45 minutes.
#   Late is a day missed, stale is two.
# - The digest builds every Monday at 07:30. Late is a Monday missed by two
#   hours, stale is a Monday and a day.
JOB_LIMITS = {
    "inbox": {"label": "Inbox", "late": 12, "stale": 24},
    "signals": {"label": "News watcher", "late": 4, "stale": 6},
    "check": {"label": "Advert checker", "late": 26, "stale": 50},
    "digest": {"label": "Monday digest", "late": 7 * 24 + 2, "stale": 8 * 24},
}
# The jobs /health holds to account unless config/radar.yaml names others
# under health_jobs. The rest are still reported, they just never turn the
# answer to degraded.
HEALTH_JOBS_DEFAULT = ("inbox", "signals", "check", "digest")


def health_jobs(config: dict | None) -> tuple:
    """The jobs whose lateness makes /health degraded, from health_jobs."""
    named = (config or {}).get("health_jobs")
    if not isinstance(named, (list, tuple)):
        return HEALTH_JOBS_DEFAULT
    return tuple(str(n) for n in named if str(n) in JOB_LIMITS)


def hours_between(earlier: str | None, now: datetime) -> float | None:
    if not earlier:
        return None
    try:
        when = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - when).total_seconds()) / 3600, 1)


def job_health(conn: sqlite3.Connection, now: str | None = None) -> dict:
    """Each scheduled job's last runs and what they say, one dict per job.

    For /health and for Home, so the two can never disagree. Reads only, and
    works on a read-only connection and on a database whose runs table has
    not had its outcome column added yet.

    last_run is the newest run that finished, ok or partial, and the ages
    and the state go by it. A failed run is a row too, so a job that keeps
    failing shows as failing, with its last note, rather than as nothing.
    state is never, ok, partial, late, failing or stale. mode is the mode
    the last finished run ran in, live or dry-run for the jobs that have
    both. The digest also says when an email was last confirmed sent.
    """
    moment = (datetime.fromisoformat(now.replace("Z", "+00:00")) if now
              else datetime.now(timezone.utc))
    have = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    outcome = "outcome" if "outcome" in have else "NULL"
    out = {}
    for name, limits in JOB_LIMITS.items():
        done = conn.execute(
            f"SELECT ts, mode, note, {outcome} AS outcome FROM runs"
            f" WHERE workflow = ? AND COALESCE({outcome}, 'ok') <> 'failed'"
            " ORDER BY ts DESC, id DESC LIMIT 1", (name,)).fetchone()
        since = done[0] if done else ""
        failed = conn.execute(
            f"SELECT COUNT(*), MAX(ts) FROM runs WHERE workflow = ?"
            f" AND {outcome} = 'failed' AND ts > ?", (name, since)).fetchone()
        failed_note = None
        if failed[0]:
            failed_note = conn.execute(
                f"SELECT note FROM runs WHERE workflow = ? AND {outcome} = 'failed'"
                " ORDER BY ts DESC, id DESC LIMIT 1", (name,)).fetchone()[0]
        hours = hours_between(done[0], moment) if done else None
        if done is None and not failed[0]:
            state = "never"
        elif done is None or hours is None or hours > limits["stale"]:
            state = "stale"
        elif failed[0]:
            state = "failing"
        elif hours > limits["late"]:
            state = "late"
        elif done[3] == "partial":
            state = "partial"
        else:
            state = "ok"
        verdict = {
            "label": limits["label"], "state": state,
            "last_run": done[0] if done else None, "hours_ago": hours,
            "late_hours": limits["late"], "stale_hours": limits["stale"],
            "mode": done[1] if done else None,
            "outcome": (done[3] or "ok") if done else None,
            "note": done[2] if done else None,
        }
        if failed[0]:
            verdict["failed_runs"] = failed[0]
            verdict["last_failed"] = failed[1]
            verdict["failed_note"] = failed_note
        if name == "digest":
            sent = conn.execute(
                "SELECT MAX(ts) FROM runs WHERE workflow = 'digest'"
                " AND mode = 'commit'").fetchone()[0]
            verdict["last_sent"] = sent
        out[name] = verdict
    return out



# ------------------------------------------------------------- news stories

# The kinds of story a signal can be. The signal scorer returns first-hire
# for a hiring story and one of the last six otherwise; the code sets the two
# hiring kinds on the signals it writes itself (process_email's buying window,
# the tripwire's first hire). Anything else a model returns reads as "other".
STORY_TYPES = ("buying-window", "first-hire", "funding", "spin-off",
               "accelerator", "regulation", "market", "other")
# Hiring is not news: since 5 Nov the Jobs page owns it and the scorer puts a
# hiring story at 0 to 39. The Insights page leaves these kinds out entirely
# and files the rest into its three sections.
HIRING_TYPES = ("buying-window", "first-hire")
STORY_SECTIONS = (
    ("funding", "Funding", ("funding",)),
    ("spinoffs", "Spin-offs and accelerators", ("spin-off", "accelerator")),
    ("market", "Regulation and market news", ("regulation", "market", "other")),
)
# How far apart two reports of one event can be found, and how alike their
# headlines must be (the share of meaningful words they have in common).
EVENT_WINDOW_DAYS = 7
EVENT_SIMILARITY = 0.3
CARD_SUMMARY_MAX = 140
# Names a model writes when it cannot name the company. None of them is a
# company to watch or to group news under.
PLACEHOLDER_COMPANIES = frozenset({"unknown company", "unknown", "not stated", "not known",
                                   "n a", "n/a", "na", "none", "various", "multiple companies",
                                   "unnamed", "undisclosed", ""})
_EVENT_STOP = frozenset("""a an and the of for to in on at by with from its it is as into new
    has have will be this that their than over after out up our we via per""".split())


def clean_story_type(value) -> str:
    """One of STORY_TYPES from whatever a model wrote, "other" when unsure."""
    word = re.sub(r"[^a-z]+", "-", str(value or "").strip().lower()).strip("-")
    aliases = {"spinoff": "spin-off", "spin-out": "spin-off", "spinout": "spin-off",
               "funding-round": "funding", "accelerator-entry": "accelerator",
               "market-news": "market", "regulatory": "regulation",
               "first-hire-advert": "first-hire", "hiring": "first-hire"}
    word = aliases.get(word, word)
    return word if word in STORY_TYPES else "other"


def clean_card_summary(value) -> str | None:
    """The card's one sentence: plain, at most CARD_SUMMARY_MAX characters,
    cut at a word and closed with a full stop. None when there is nothing."""
    text = sanitise_free_text(str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    if len(text) > CARD_SUMMARY_MAX:
        text = text[:CARD_SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(",;: ")
    if not text:
        return None
    if text[-1] not in ".?":
        text += "."
    return text


def clean_headline(value) -> str:
    """A headline in the page's one style: plain, no closing full stop."""
    text = re.sub(r"\s+", " ", sanitise_free_text(str(value or ""))).strip()
    return text[:-1] if text.endswith(".") and not text.endswith("..") else text


def is_placeholder_company(name) -> bool:
    """True for "Unknown company" and the other ways a model says it could not name one."""
    return normalise_company(name or "") in PLACEHOLDER_COMPANIES or \
        str(name or "").strip().lower() in PLACEHOLDER_COMPANIES


def headline_words(text, drop=frozenset()) -> frozenset:
    words = re.findall(r"[a-z0-9£€$][a-z0-9.£€$-]*", str(text or "").lower())
    return frozenset(w.strip(".-") for w in words
                     if w.strip(".-") and w.strip(".-") not in _EVENT_STOP
                     and w.strip(".-") not in drop)


def headline_similarity(a, b, company=None) -> float:
    """Words in common over words in either, 0 to 1. The company's own name
    is left out: every candidate is about the same company, so its name
    would make any two headlines look alike."""
    drop = headline_words(company) if company else frozenset()
    wa, wb = headline_words(a, drop), headline_words(b, drop)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def event_key_for(conn, signal_id: int, company_id, headline, first_seen) -> str:
    """The event a new signal belongs to: the key of an earlier signal about
    the same company, found up to EVENT_WINDOW_DAYS before this one, whose
    headline, less the company's name, is at least EVENT_SIMILARITY alike,
    or its own new key. Only earlier signals count, so grouping at store time
    and the backfill, which walks the rows in order of arrival, agree. A
    signal with no company, or a placeholder for one, stands alone. A key a
    human split off ("solo-") is never joined."""
    own = f"e{signal_id}"
    if not company_id:
        return own
    name = conn.execute("SELECT norm_name, display_name FROM companies WHERE id = ?",
                        (company_id,)).fetchone()
    if name is None or is_placeholder_company(name[1]) or name[0] in PLACEHOLDER_COMPANIES:
        return own
    try:
        when = datetime.fromisoformat(str(first_seen).replace("Z", "+00:00"))
    except ValueError:
        return own
    lo = (when - timedelta(days=EVENT_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    best, best_score = None, 0.0
    for row in conn.execute(
            "SELECT id, headline, event_key FROM signals WHERE company_id = ?"
            " AND id <> ? AND first_seen >= ? AND first_seen <= ?"
            " AND event_key IS NOT NULL AND event_key NOT LIKE 'solo-%'"
            " ORDER BY first_seen, id", (company_id, signal_id, lo, hi)):
        score = headline_similarity(headline, row[1], name[1])
        if score >= EVENT_SIMILARITY and score > best_score:
            best, best_score = row[2], score
    return best or own


def assign_event_key(conn, signal_id: int) -> str:
    """Work out and store one signal's event key. For the writers, right
    after the INSERT, so the row's own id is known. A report that joins a
    story a human has hidden is hidden with it, so a hidden story never
    comes back through a second source."""
    row = conn.execute("SELECT company_id, headline, first_seen, event_key"
                       " FROM signals WHERE id = ?", (signal_id,)).fetchone()
    if row is None:
        return ""
    if row[3] and str(row[3]).startswith("solo-"):
        return row[3]
    key = event_key_for(conn, signal_id, row[0], row[1], row[2])
    conn.execute("UPDATE signals SET event_key = ? WHERE id = ?", (key, signal_id))
    if key != f"e{signal_id}":
        hidden = conn.execute(
            "SELECT COUNT(*), SUM(acknowledged_at IS NULL), MAX(acknowledged_at) FROM signals"
            " WHERE event_key = ? AND id <> ?", (key, signal_id)).fetchone()
        if hidden[0] and not hidden[1]:
            conn.execute("UPDATE signals SET acknowledged_at = ? WHERE id = ?"
                         " AND acknowledged_at IS NULL", (hidden[2], signal_id))
    return key

# -------------------------------------------------------------- score record

PROMPTS_DIR = REPO_ROOT / "prompts"
GATE_NAMES = ("sector", "cv", "location", "rate")

# The fields of a score the history keeps, in the order of its columns.
# description_hash and working arrived with Prepare application: the version of
# the description a score was made against, and its per-ask working. A card
# score holds neither, so both read None for every score made before, and the
# comparison in archive_score is unchanged for them. reasons arrived with the
# quoted reasons, and reads None for every score made before them, card or
# full description, for the same reason.
_HISTORY_FIELDS = ("scored_at", "score_basis", "prompt_version", "score_model",
                   "cv_version", "cv_match", "gates", "tier", "filter_reason",
                   "one_line_why", "description_hash", "working", "reasons")

# The version of the JSON a score's reasons are kept in.
REASONS_VERSION = 1


def flat_text(text) -> str:
    """Text with every run of white space made one space, and none at the ends."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def find_verbatim(quote, text, whole_words: bool = False) -> str | None:
    """Where quote is in text word for word, as text has it, else None.

    The one matching rule for a quote the scorer gives, which the pay line of
    a full description (prepare_application.rate_line) and the quote of every
    reason (score_item.normalise_reasons) both use. Case is ignored, and a run
    of white space on either side counts as one space, so a quote is found
    across a line break, a doubled space or a non-breaking space, but never
    with a word added, dropped or changed, and never with a character of
    punctuation that is not in the text. What is returned is the text's own
    characters, never the quote's, so what a page shows is the advert's words.
    None for an empty quote, and for one that is not there.

    With whole_words, which the reasons ask for, a quote that begins or ends
    with a letter or a digit is found only where that end is the edge of a
    word in the text, so a word or a figure cut short is not the text word for
    word: "£65" is not found in "£650 per day", nor "Senior Software Engineer"
    in "Senior Software Engineering Manager". A quote with no letter or digit
    in it at all is never found that way. The pay line is found without it, as
    it always was.
    """
    wanted = flat_text(quote)
    if not wanted:
        return None
    pattern = re.escape(wanted)
    if whole_words:
        if not re.search(r"\w", wanted):
            return None
        if re.match(r"\w", wanted):
            pattern = r"(?<!\w)" + pattern
        if re.search(r"\w\Z", wanted):
            pattern += r"(?!\w)"
    found = re.search(pattern, flat_text(text), re.IGNORECASE)
    return found.group(0) if found else None


def reasons_record(raw, scored_at) -> dict | None:
    """The reasons kept beside a score, if they belong to that score, else None.

    raw is the stored JSON, or the record itself. The reasons are kept beside
    the score they were made with and mean nothing beside another, so they are
    only returned when they carry the very date of the score they sit with. A
    writer that replaces a score and knows nothing of the reasons, old code
    after a rollback among them, cannot leave old reasons looking current. A
    record that cannot be read is None too, and so is a score with no date.
    """
    if raw is None or not scored_at:
        return None
    record = raw
    if isinstance(raw, (str, bytes)):
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if (not isinstance(record, dict) or record.get("v") != REASONS_VERSION
            or record.get("scored_at") != scored_at
            or not isinstance(record.get("reasons"), list)):
        return None
    return record


def reasons_gates_moved(record, row: dict) -> bool:
    """Whether the gates a row holds now are not the ones its reasons came with.

    record is what reasons_record returns, and it carries the four gates of
    the score the reasons were given with. A re-tier by code, the regate or
    the page checker's, judges the rate gate again and keeps the date of the
    model's answer, so the reasons stay beside gates they were not given for,
    and a page that shows them says so. False for no record, and for a record
    without gates.
    """
    gates = record.get("gates") if isinstance(record, dict) else None
    if not isinstance(gates, dict):
        return False

    def flag(value):
        return None if value is None else int(bool(value))

    return any(flag(gates.get(name)) != flag(row.get(f"gate_{name}"))
               for name in GATE_NAMES)


def score_reasons(raw, scored_at) -> list[dict]:
    """The reasons of a score as a page shows them: each one's words, and the
    quote where one was kept. Empty when the score has none of its own."""
    record = reasons_record(raw, scored_at)
    out = []
    for item in (record or {}).get("reasons") or []:
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            continue
        quote = item.get("quote")
        out.append({"reason": reason.strip(),
                    "quote": quote.strip() if isinstance(quote, str)
                    and quote.strip() else None})
    return out


def description_version(text) -> str | None:
    """Twelve hex characters of the sha256 of an advert's description.

    The version of a stored description, so that the same text is the same
    version wherever it is read and an edit to it is another. White space at
    either end and the line endings do not make another version, because the
    text is scored stripped and a stored copy with a trailing newline must
    still be the version that was scored. None for no text at all, because
    nothing was read and a hash of nothing would look like a version.
    """
    if text is None or not str(text).strip():
        return None
    return hashlib.sha256(
        str(text).strip().replace("\r\n", "\n").encode("utf-8")).hexdigest()[:12]


def prompt_hash(text: str) -> str:
    """Eight hex characters of the sha256 of a prompt's text.

    Line endings are made alike first, so one file hashes the same on a
    Windows checkout and on the box.
    """
    return hashlib.sha256(
        text.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:8]


def prompt_version(name: str = "scorer.md") -> str:
    """The version of a prompt file, a short hash of its contents.

    The version cannot drift from the file because it is the file. The
    label in the header comment is for people, and editing it changes the
    version like any other edit.
    """
    return prompt_hash((PROMPTS_DIR / name).read_text(encoding="utf-8"))


def reply_was_cut_off(usage: dict | None) -> bool:
    """True when the model stopped because it ran out of max_tokens."""
    return (usage or {}).get("stop_reason") == "max_tokens"


def _held_score(conn: sqlite3.Connection, opportunity_id: int) -> dict | None:
    """The score one opportunity row holds, as the history keeps it.

    None when the row is missing or has no score at all, because there is
    then nothing to keep. The gates are JSON of the four results as stored.
    It reads plain tuples whatever row factory the connection has.
    """
    cur = conn.cursor()
    cur.row_factory = None
    raw = cur.execute(
        "SELECT cv_match, want_match, combined, tier, one_line_why, scored_at,"
        " score_basis, prompt_version, score_model, cv_version, filter_reason,"
        " gate_sector, gate_cv, gate_location, gate_rate,"
        " description_hash, score_working, score_reasons"
        " FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    if raw is None:
        return None
    row = dict(zip([d[0] for d in cur.description], raw))
    if all(row[c] is None for c in ("cv_match", "want_match", "combined",
                                    "tier", "one_line_why")):
        return None
    row["gates"] = json.dumps({name: row[f"gate_{name}"] for name in GATE_NAMES},
                              sort_keys=True)
    # The history column is called working, the row's is score_working.
    row["working"] = row["score_working"]
    # Only a score made against a full description has a version of one and a
    # working. A card score holds neither, whatever the columns still say: a
    # writer that replaces a spec score with a card score, and knows nothing of
    # these two columns, leaves the old values on the row, and the history must
    # not file them under a score they were not made with.
    if row["score_basis"] != SCORE_BASIS_SPEC:
        row["description_hash"] = None
        row["working"] = None
    # The reasons go with any score, card or full description, but only the
    # ones made with the score the row holds, which carry its date. A writer
    # that replaced the score and left the reasons of the one before is not
    # allowed to file them under a score they were not given for.
    row["reasons"] = (row["score_reasons"]
                      if reasons_record(row["score_reasons"], row["scored_at"])
                      is not None else None)
    return {field: row[field] for field in _HISTORY_FIELDS}


def _write_history(conn: sqlite3.Connection, opportunity_id: int, event: str,
                   writer: str, state: dict, now: str | None,
                   tier_from: str | None = None,
                   cv_hash: str | None = None) -> None:
    conn.execute(
        "INSERT INTO score_history (opportunity_id, recorded_at, event, writer,"
        " " + ", ".join(_HISTORY_FIELDS) + ", tier_from, cv_hash)"
        " VALUES (?,?,?,?" + ",?" * len(_HISTORY_FIELDS) + ",?,?)",
        (opportunity_id, now or now_iso(), event, writer,
         *(state[f] for f in _HISTORY_FIELDS), tier_from, cv_hash))


# A score of a role's tailored CV. It is kept in the history beside the role's
# own scores, and is never one of them: the row it names holds the master CV's
# score, so archive_score never compares a tailored score with it.
TAILORED_EVENT = "tailored"


def archive_score(conn: sqlite3.Connection, opportunity_id: int, writer: str,
                  now: str | None = None) -> bool:
    """Keep the score a row holds, before something overwrites it.

    Every writer that changes a score in place calls this first, in the
    same transaction as its UPDATE, and the row's outgoing values go to
    score_history with their own date, basis, prompt version and model. It
    writes nothing when the row has no score, and nothing when the newest
    history row already holds exactly these values, which is the case for
    any score made since the record began. So a row scored before the
    record gets its old score kept the first time it is overwritten, and a
    row already kept is not kept twice. Returns True when it wrote a row.
    The caller commits.

    The write lock is taken before the values are read, so what is kept is
    exactly what the caller's UPDATE then replaces, even with another
    process writing to the same row. That costs nothing the UPDATE would
    not cost anyway, since it needs the same lock a moment later.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    state = _held_score(conn, opportunity_id)
    if state is None:
        return False
    cur = conn.cursor()
    cur.row_factory = None
    last = cur.execute(
        "SELECT " + ", ".join(_HISTORY_FIELDS) + " FROM score_history"
        " WHERE opportunity_id = ? AND event <> ?"
        " ORDER BY id DESC LIMIT 1",
        (opportunity_id, TAILORED_EVENT)).fetchone()
    if last is not None and last == tuple(state[f] for f in _HISTORY_FIELDS):
        return False
    _write_history(conn, opportunity_id, "replaced", writer, state, now)
    return True


def record_score(conn: sqlite3.Connection, opportunity_id: int, writer: str,
                 now: str | None = None, tier_from: str | None = None) -> bool:
    """Write the score a row now holds to the history as a new score.

    Called after a model has scored the row, once for a first score and
    once for a re-score. Returns True when it wrote a row. The caller
    commits. tier_from is set by a caller whose score moved the role from
    one tier to another, and is the tier it moved from. It is kept on the
    row for the new score, so the move can be read off the history without
    comparing one row with the next.
    """
    state = _held_score(conn, opportunity_id)
    if state is None:
        return False
    _write_history(conn, opportunity_id, "scored", writer, state, now,
                   tier_from)
    return True


def record_tailored_score(conn: sqlite3.Connection, opportunity_id: int,
                          state: dict, cv_hash: str, writer: str = "prepare",
                          now: str | None = None) -> None:
    """Write a score of a role's tailored CV to the history, marked tailored.

    state carries the thirteen fields the history keeps, as _held_score gives
    them for a role's own score, and cv_hash is the version of the copy the
    score was made against. Nothing on the opportunities row is read or
    written, because a tailored score is not the role's score. The caller
    commits.
    """
    _write_history(conn, opportunity_id, TAILORED_EVENT, writer,
                   {field: state.get(field) for field in _HISTORY_FIELDS},
                   now, cv_hash=cv_hash)


# ---------------------------------------------------------------- Claude API

def mock_mode_active() -> bool:
    return os.environ.get("RADAR_MOCK", "").strip() in ("1", "true", "yes")


def claude_call(model: str, system_blocks: list | str, user_content: str,
                max_tokens: int = 1024, mock_fn=None) -> tuple[str, dict]:
    """One Claude call. Returns (response_text, usage_dict).

    The usage dict carries the token counts, the mode, the model that
    answered and the stop_reason, so a caller can tell a reply the model
    finished from one that ran out of max_tokens (see reply_was_cut_off).

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
                                       "cache_read_tokens": 0,
                                       "cache_write_tokens": 0, "mode": "mock",
                                       "model": "mock",
                                       "stop_reason": "end_turn"}

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
        # Billed at 1.25 times the input price, on the call that fills the cache.
        "cache_write_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        "mode": "live",
        "model": getattr(response, "model", None) or model,
        "stop_reason": getattr(response, "stop_reason", None),
    }
    return text, usage


def add_usage(total: dict, usage: dict) -> dict:
    for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens"):
        total[key] = total.get(key, 0) + (usage.get(key, 0) or 0)
    return total


# Half of a UTF-16 pair on its own. json.loads gives one for an escape such as
# "\ud83d" with no partner, and Python cannot encode it, so SQLite cannot take it.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


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

    A lone surrogate, which a reply can carry as a JSON escape and which
    cannot be written as UTF-8, so not stored by SQLite, becomes the
    replacement character, so that one stray character never costs the row
    it is written with.

    None passes through untouched so optional columns stay NULL.
    """
    if text is None:
        return None
    s = _LONE_SURROGATE.sub("\ufffd", str(text))
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

# Salaried pay converts to a day rate at this many working days a year for
# the market panel's comparison column only. Gating never converts: a
# salary is judged as a salary against the salary floor.
WORKING_DAYS_PER_YEAR = 220
HOURS_PER_DAY = 8
CLOSE_BAND_GBP = 50           # a day rate under its floor by up to this reads as close
SALARY_CLOSE_BAND_GBP = 5000  # a salary under its floor by up to this reads as close

# The two machine-readable lines in the preferences file. Both are required.
_FLOOR_LINES = (
    ("day", "day_rate_floor_gbp", "750",
     re.compile(r"^day_rate_floor_gbp:\s*([0-9]+(?:\.[0-9]+)?)\s*$",
                re.IGNORECASE | re.MULTILINE)),
    ("salary", "salary_floor_gbp", "100000",
     re.compile(r"^salary_floor_gbp:\s*([0-9]+(?:\.[0-9]+)?)\s*$",
                re.IGNORECASE | re.MULTILINE)),
)


# The labelled lines the dashboard may edit inside the preferences file, one
# at a time, through /profile/setting. The scorer reads the whole file, so a
# value set here flows into scoring with no other plumbing. Everything else in
# that file stays the owner's prose. The floors, the pass mark and the Filtered
# floor are not here: they are saved only on the CV page's Preferences tab
# (write_scoring_prefs, SCORING_PREFS below), which re-sorts the roles already
# scored and keeps each move in score_history. A floor saved here would change
# the gates with no re-sort and no history.
PREF_EDITABLE = {
    "target_title": "text",
    "keywords": "text",
    # Optional. The first name Home's welcome greets, so the name lives in the
    # owner's own gitignored file and never in the code. Home greets without
    # a name when the line is missing or empty.
    "first_name": "text",
}


def _prefs_path(config: dict | None = None) -> Path:
    config = config or load_config()
    prefs_rel = config.get("prefs_file", "config/profile/preferences.md")
    live = REPO_ROOT / prefs_rel
    if live.exists():
        return live
    return REPO_ROOT / "config" / "preferences.template.md"


def read_pref_line(key: str, config: dict | None = None) -> str | None:
    """The value of one labelled line, or None when the line is absent. Any
    line the dashboard edits, on /profile/setting or the Preferences tab."""
    if key not in PREF_EDITABLE and key not in SCORING_PREFS:
        raise ValueError(f"{key} is not a dashboard-editable line.")
    path = _prefs_path(config)
    if not path.exists():
        return None
    # Blanks around the value, never the line's end: \s would run on past the
    # end of an empty line and read the next line as its value.
    match = re.search(rf"^{re.escape(key)}:[^\S\n]*(\S.*?)[^\S\n]*$",
                      path.read_text(encoding="utf-8"),
                      re.IGNORECASE | re.MULTILINE)
    return match.group(1) if match else None


# The longest value one labelled preferences line holds. update_pref_line cuts
# at this, and the Settings page's dialog stops adding before it.
PREF_LINE_MAX = 200


def update_pref_line(key: str, value: str,
                     config: dict | None = None) -> str:
    """Surgically set one labelled line in the live preferences file.

    Replaces the line in place when it exists, up to its own end, otherwise
    appends a small dashboard-managed section at the end. Never touches any
    other byte, line endings included: the file is the owner's prose and stays
    that way. The file is written in one step (_write_prefs, a file beside it
    renamed over it), so the scorer, reading it in another process, sees the
    old file or the new one and never half of it. Never the tracked template.
    Returns the stored value.
    """
    if key in SCORING_PREFS:
        raise ValueError(f"{key} is saved under Scoring on the Settings page, "
                         "which re-sorts the roles already scored.")
    if key not in PREF_EDITABLE:
        raise ValueError(f"{key} is not a dashboard-editable line.")
    value = " ".join(str(value or "").split())[:PREF_LINE_MAX]
    # The first name may be cleared: the line stays, empty, and Home greets
    # without a name. Every other line is a setting the scorer needs.
    if not value and key != "first_name":
        raise ValueError("An empty value would delete the line. Type "
                         "something, or edit the file by hand to remove it.")
    path, text = _prefs_text(config, live_only=True)
    if path is None:
        raise ValueError(f"No preferences file at {live_prefs_path(config)} "
                         "to edit. Copy config/preferences.template.md there first.")
    newline = "\r\n" if "\r\n" in text else "\n"
    line = f"{key}: {value}" if value else f"{key}:"
    found = re.search(rf"^{re.escape(key)}:[^\r\n]*", text,
                      re.IGNORECASE | re.MULTILINE)
    if found:
        text = text[:found.start()] + line + text[found.end():]
    else:
        if "## Title and keywords." not in text:
            text = (text.rstrip() + newline + newline + "## Title and keywords."
                    + newline + newline + "Lines the dashboard's scoring panel "
                    "manages. The scorer reads them as part of this file."
                    + newline + newline)
        text = text.rstrip() + newline + line + newline
    _write_prefs(path, text)
    return value


def read_first_name(config: dict | None = None) -> str | None:
    """The first name Home greets, from the optional first_name line, or None.

    Read like the other labelled lines, from the live preferences file (the
    template when there is none, whose line is empty). One line of plain text,
    cut to 40 characters, and None for nothing.
    """
    try:
        value = read_pref_line("first_name", config)
    except (OSError, UnicodeDecodeError):
        return None
    value = " ".join(str(value or "").split())[:40].strip()
    return value or None


def read_rate_floors(config: dict | None = None) -> dict:
    """Both floors in GBP, {"day": ..., "salary": ...}, from the two lines.

    The preferences file is the single home of desire, so the floors live
    there as two clearly labelled lines, day_rate_floor_gbp and
    salary_floor_gbp, read by both the scorer prompt (the model sees the
    whole file) and this code. A stated day rate meets the day floor, an
    hourly rate times eight meets the day floor, an annual salary meets
    the salary floor, and nothing converts between them. Live copy first,
    tracked template second, loud failure when either line is missing,
    because gating against a guessed floor would be worse than no gating.
    """
    config = config or load_config()
    prefs_rel = config.get("prefs_file", "config/profile/preferences.md")
    candidates = [REPO_ROOT / prefs_rel,
                  REPO_ROOT / "config" / "preferences.template.md"]
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            floors = {}
            for name, key, example, pattern in _FLOOR_LINES:
                match = pattern.search(text)
                if not match:
                    raise RuntimeError(
                        f"No {key} line in {path}. Add one to the Rates "
                        f"section, for example '{key}: {example}', so "
                        "gating and the scorer read the same floors.")
                floors[name] = float(match.group(1))
            return floors
    raise RuntimeError(
        "No preferences file found for the rate floors. Expected "
        f"{candidates[0]} or {candidates[1]}.")


def read_rate_floor(config: dict | None = None) -> float:
    """The day-rate floor alone, for the readers that label day rates."""
    return read_rate_floors(config)["day"]


# The CV pass mark and the Filtered floor. The preferences file is their
# home. These are only what is used when it cannot supply them.
DEFAULT_CV_PASS_SCORE = 70
DEFAULT_CV_FLOOR_SCORE = 40

_SCORE_LINES = (
    ("pass", "cv_pass_score", DEFAULT_CV_PASS_SCORE),
    ("floor", "cv_floor_score", DEFAULT_CV_FLOOR_SCORE),
)
_WARNED: set[str] = set()
_THRESHOLDS_KEY = "cv-thresholds"


def _warn_once(key: str, message: str) -> None:
    """One WARNING line on stderr per key per process, which is per run.

    A stderr that cannot be written to is ignored, because a log line must
    never be what stops a run.
    """
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        print(f"WARNING. {message}", file=sys.stderr)
    except (OSError, ValueError):
        pass


def thresholds_fell_back() -> bool:
    """True when this process found a CV threshold line missing or unreadable."""
    return _THRESHOLDS_KEY in _WARNED


def read_score_thresholds(config: dict | None = None) -> dict:
    """The CV pass mark and the Filtered floor, {"pass": 70, "floor": 40}.

    Two labelled lines in the preferences file, cv_pass_score and
    cv_floor_score, beside the rate floors and read the same way, live copy
    first and the tracked template second. A CV match at or above the pass
    mark passes the CV gate. Under the floor a role is Filtered whatever its
    other gates say.

    Unlike read_rate_floors this never raises. A line that is missing or
    unreadable takes its default, and a file that is missing, or a pair that
    makes no sense (the floor above the pass mark, either outside 0 to 100),
    gives 70 and 40. Whatever went wrong is one WARNING per run naming every
    line concerned, because the hourly inbox run must never stop over a
    threshold. Callers read this once per run and pass the values down.
    """
    defaults = {name: default for name, _key, default in _SCORE_LINES}
    out = dict(defaults)
    try:
        config = config or load_config()
        prefs_rel = config.get("prefs_file", "config/profile/preferences.md")
        text = where = None
        for path in (REPO_ROOT / prefs_rel,
                     REPO_ROOT / "config" / "preferences.template.md"):
            if path.exists():
                text, where = path.read_text(encoding="utf-8-sig"), path
                break
        if text is None:
            _warn_once(_THRESHOLDS_KEY, "No preferences file found for the CV "
                       f"thresholds, using {defaults['pass']} and "
                       f"{defaults['floor']}.")
            return out
        problems = []
        for name, key, default in _SCORE_LINES:
            match = re.search(rf"^{key}:[ \t]*([0-9]+(?:\.[0-9]+)?)[ \t]*$",
                              text, re.IGNORECASE | re.MULTILINE)
            if match:
                number = float(match.group(1))
                out[name] = int(number) if number == int(number) else number
                continue
            seen = re.search(rf"^[^\w\n]*{key}\b[^\n]*$", text,
                             re.IGNORECASE | re.MULTILINE)
            if seen:
                problems.append(f"The {key} line {seen.group(0).strip()[:60]!r} "
                                f"is not a plain number, using {default}.")
            else:
                problems.append(f"There is no {key} line, using {default}.")
        if not 0 <= out["floor"] <= out["pass"] <= 100:
            problems.append(f"A floor of {out['floor']:g} and a pass mark of "
                            f"{out['pass']:g} do not fit, each must be 0 to 100 "
                            "with the floor at or under the pass mark, so "
                            f"{defaults['pass']} and {defaults['floor']} stand.")
            out = dict(defaults)
        if problems:
            _warn_once(_THRESHOLDS_KEY, f"The CV thresholds in {where} fell "
                       "back to the defaults. " + " ".join(problems))
    except Exception as err:  # noqa: BLE001  never stop the hourly run over a threshold
        _warn_once(_THRESHOLDS_KEY, "The CV thresholds could not be read "
                   f"({type(err).__name__}), using {defaults['pass']} and "
                   f"{defaults['floor']}.")
        out = dict(defaults)
    return out


# ------------------------------------------- the CV page's Preferences tab
#
# The tab writes the owner's live preferences file (prefs_file in radar.yaml,
# gitignored) and nothing else: never the tracked template, which is only ever
# read when there is no live file. Every write changes only the lines it names
# and leaves every other byte of the file as it was, line endings and all, and
# lands in one step (a file beside it, renamed over it), so the scorer, which
# reads the file in another process, sees the old file or the new one.

# The four scoring lines, each with what it is called on the page and the
# range a value must sit in. The floors are pounds, the two scores 0 to 100.
SCORING_PREFS = {
    "day_rate_floor_gbp": ("The day-rate floor", 1, 10_000),
    "salary_floor_gbp": ("The salary floor", 1_000, 1_000_000),
    "cv_pass_score": ("The CV pass mark", 0, 100),
    "cv_floor_score": ("The Filtered floor", 0, 100),
}

# The two prose sections the tab edits: the words a heading must start with
# (after "## "), and the heading a file without one gets at its end.
PREF_SECTIONS = {
    "location": (r"location", "## Location and travel."),
    "sector": (r"sectors?", "## Sectors."),
}
PREF_SECTION_MAX = 2_000     # characters of prose in one section


def live_prefs_path(config: dict | None = None) -> Path:
    """The live preferences file the page writes, whether or not it exists."""
    config = config or load_config()
    return REPO_ROOT / config.get("prefs_file", "config/profile/preferences.md")


def _prefs_text(config: dict | None = None, live_only: bool = False):
    """(path, text) of the preferences file as it is on disk, line endings
    kept: the live file, else the tracked template unless live_only. (None,
    None) when there is none."""
    live = live_prefs_path(config)
    paths = [live] if live_only else [live, REPO_ROOT / "config" / "preferences.template.md"]
    for path in paths:
        if path.exists():
            with open(path, encoding="utf-8", newline="") as fh:
                return path, fh.read()
    return None, None


def _write_prefs(path: Path, text: str) -> None:
    """The whole file in one step, exactly these characters."""
    staged = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
    with open(staged, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(staged, path)


def _plain_number(value) -> str:
    return str(int(value)) if float(value) == int(value) else f"{float(value):g}"


def read_scoring_prefs(config: dict | None = None) -> dict:
    """The four scoring lines as numbers, None for a line that is missing or
    not a plain number, from the file the scorer reads, and where that is."""
    path, text = _prefs_text(config)
    out = {"where": None if path is None else str(path),
           "live": path is not None and path == live_prefs_path(config)}
    for key in SCORING_PREFS:
        found = (re.search(rf"^{key}:[ \t]*([0-9]+(?:\.[0-9]+)?)[ \t]*\r?$",
                           text, re.IGNORECASE | re.MULTILINE) if text else None)
        out[key] = float(found.group(1)) if found else None
    return out


def clean_scoring_prefs(values: dict, config: dict | None = None) -> dict:
    """The scoring lines a request sends, checked, as the text each line will
    hold. ValueError says what is wrong in the page's words. Lines the request
    leaves out keep what the file says, and the pass mark and the floor are
    checked together, with the floor at or under the pass mark."""
    if not isinstance(values, dict) or not values:
        raise ValueError("Nothing to save.")
    out = {}
    for key, raw in values.items():
        if key not in SCORING_PREFS:
            raise ValueError(f"{key} is not a line this page edits.")
        label, low, high = SCORING_PREFS[key]
        if isinstance(raw, bool) or raw is None:
            raise ValueError(f"{label} needs a plain number.")
        try:
            number = float(str(raw).replace("£", "").replace(",", "").strip())
        except ValueError as err:
            raise ValueError(f"{label} needs a plain number.") from err
        if number != number or not low <= number <= high:
            raise ValueError(f"{label} must be from {low:,} to {high:,}.")
        out[key] = number
    now = read_scoring_prefs(config)
    # A partner line the file does not have (or holds as no plain number) is
    # judged as the readers would read it, at its default, 70 or 40: a floor of
    # 80 with no pass mark line would make read_score_thresholds throw both
    # away for 70 and 40, and the saved line would mean nothing.
    passing = out.get("cv_pass_score", now.get("cv_pass_score"))
    floor = out.get("cv_floor_score", now.get("cv_floor_score"))
    if "cv_pass_score" in out or "cv_floor_score" in out:
        passing = DEFAULT_CV_PASS_SCORE if passing is None else passing
        floor = DEFAULT_CV_FLOOR_SCORE if floor is None else floor
    if passing is not None and floor is not None and floor > passing:
        raise ValueError(f"The Filtered floor ({_plain_number(floor)}) must be at or "
                         f"under the CV pass mark ({_plain_number(passing)}).")
    return {key: _plain_number(number) for key, number in out.items()}


def write_scoring_prefs(values: dict, config: dict | None = None) -> dict:
    """Set the scoring lines in the live preferences file, in one write.

    values is what clean_scoring_prefs returned. A line is replaced where it
    is, its key's first line, and only up to its own end; a line the file
    does not have is added at the end. Returns {key: stored text}.
    """
    path, text = _prefs_text(config, live_only=True)
    if path is None:
        raise ValueError(f"There is no preferences file at "
                         f"{live_prefs_path(config)} to change. Copy "
                         "config/preferences.template.md there first.")
    newline = "\r\n" if "\r\n" in text else "\n"
    for key, value in values.items():
        line = f"{key}: {value}"
        found = re.search(rf"^{re.escape(key)}:[^\r\n]*", text,
                          re.IGNORECASE | re.MULTILINE)
        if found:
            text = text[:found.start()] + line + text[found.end():]
        else:
            if text and not text.endswith(("\n", "\r")):
                text += newline
            text += line + newline
    _write_prefs(path, text)
    return dict(values)


def _labelled_keys() -> tuple:
    return tuple(PREF_EDITABLE) + tuple(SCORING_PREFS)


def _section_span(text: str, key: str):
    """(start of the body, end of the body) of a prose section, or None."""
    words, _heading = PREF_SECTIONS[key]
    head = re.search(rf"^##[ \t]+{words}\b[^\r\n]*(?:\r\n|\n|\r|\Z)", text,
                     re.IGNORECASE | re.MULTILINE)
    if head is None:
        return None
    nxt = re.compile(r"^#{1,6}[ \t]", re.MULTILINE).search(text, head.end())
    return head.end(), (nxt.start() if nxt else len(text))


def read_pref_section(key: str, config: dict | None = None) -> str | None:
    """A prose section's words, without the blank lines around them, with
    line feeds, from the file the scorer reads. None when it has none."""
    if key not in PREF_SECTIONS:
        raise ValueError(f"{key} is not a section this page edits.")
    _path, text = _prefs_text(config)
    if text is None:
        return None
    span = _section_span(text, key)
    if span is None:
        return None
    body = text[span[0]:span[1]].replace("\r\n", "\n").replace("\r", "\n")
    return body.strip("\n").rstrip() or ""


def clean_pref_section(key: str, body) -> str:
    """A prose section as a request sends it, checked: its lines with line
    feeds, no blank lines at either end. ValueError says why it cannot be
    taken: a heading would start another section, and a labelled line would
    change a number the gates read."""
    if key not in PREF_SECTIONS:
        raise ValueError(f"{key} is not a section this page edits.")
    if not isinstance(body, str):
        raise ValueError("The section must be sent as text.")
    lines = [line.rstrip() for line in
             body.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines)
    if not text:
        raise ValueError("An empty section would leave the scorer nothing to "
                         "read. Write what you want, or edit the file by hand.")
    if len(text) > PREF_SECTION_MAX:
        raise ValueError(f"The section is {len(text):,} characters, and the most "
                         f"it may be is {PREF_SECTION_MAX:,}.")
    for line in lines:
        if line.lstrip().startswith("#"):
            raise ValueError("A line may not start with #, which would start "
                             "another section of the file.")
        if re.match(rf"^({'|'.join(_labelled_keys())})\s*:", line.strip(), re.IGNORECASE):
            raise ValueError("A line may not start with one of the file's "
                             "labelled names, such as day_rate_floor_gbp:, which "
                             "the gates read as a number.")
    return text


def write_pref_section(key: str, body: str, config: dict | None = None) -> str:
    """Replace one prose section's words in the live preferences file.

    body is what clean_pref_section returned. The heading line and everything
    outside the section are left as they are; the section keeps one blank
    line after its heading and one before the next heading. A section that
    holds a labelled line is refused, because replacing its words would take
    that line with them. A file without the section gets it at its end.
    Returns the words stored.
    """
    path, text = _prefs_text(config, live_only=True)
    if path is None:
        raise ValueError(f"There is no preferences file at "
                         f"{live_prefs_path(config)} to change. Copy "
                         "config/preferences.template.md there first.")
    newline = "\r\n" if "\r\n" in text else "\n"
    words = body.replace("\n", newline)
    span = _section_span(text, key)
    if span is None:
        if text and not text.endswith(("\n", "\r")):
            text += newline
        text += (newline if text else "") + PREF_SECTIONS[key][1] + newline + newline
        text += words + newline
    else:
        start, end = span
        held = text[start:end]
        if re.search(rf"^({'|'.join(_labelled_keys())})\s*:", held,
                     re.IGNORECASE | re.MULTILINE):
            raise ValueError("This section of the preferences file holds a "
                             "labelled line the scorer or the gates read, so "
                             "the page will not replace it. Edit the file by hand.")
        tail = newline + newline if end < len(text) else newline
        text = text[:start] + newline + words + tail + text[end:]
    _write_prefs(path, text)
    return body


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


def band_for(figure: float | None, floor: float,
             close_band: float = CLOSE_BAND_GBP) -> str:
    """above | close | below | unstated, judged in code, never by the model.

    The figure and the floor share a basis: a day rate against the day
    floor, a salary against the salary floor with its own wider close band.
    """
    if figure is None:
        return "unstated"
    if figure >= floor:
        return "above"
    if figure >= floor - close_band:
        return "close"
    return "below"


def gate_pay(currency, period, pay_min, pay_max, floors: dict,
             config: dict | None = None) -> dict:
    """The rate gate, judged in code from what the extractor stored.

    A stated day rate meets the day floor, an hourly rate times eight
    meets the day floor, an annual salary meets the salary floor. No
    figure converts into another period, so a £100k salary is judged as
    £100k and never as £455 a day. A range gates on its top figure, the
    best case. Euro amounts convert at eur_to_gbp from radar.yaml, and a
    currency with no configured conversion reads as unstated rather than
    guessed. gate is None when nothing usable is stated, and the caller
    lets that pass as unknown, because silence is a question, not a fail.
    """
    out = {"gate": None, "figure": None, "basis": "unstated",
           "floor": None, "band": "unstated"}

    def num(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    amounts = [a for a in (num(pay_min), num(pay_max)) if a and a > 0]
    if not amounts:
        return out
    amount = max(amounts)
    period = (period or "").strip().lower()
    if period == "year":
        basis, floor, close = "salary", floors["salary"], SALARY_CLOSE_BAND_GBP
    elif period == "day":
        basis, floor, close = "day-rate", floors["day"], CLOSE_BAND_GBP
    elif period == "hour":
        basis, floor, close = "hourly", floors["day"], CLOSE_BAND_GBP
        amount *= HOURS_PER_DAY
    else:
        return out
    currency = (currency or "").strip().upper()
    if currency == "EUR":
        config = config or load_config()
        amount *= float(config.get("eur_to_gbp", 0.85))
    elif currency != "GBP":
        return out
    out.update(gate=amount >= floor, figure=amount, basis=basis,
               floor=floor, band=band_for(amount, floor, close))
    return out


def pay_on_file(currency, period, pay_min, pay_max) -> bool:
    """Whether the stored pay holds a figure the rate gate can judge, by the
    floor rules gate_pay applies: a positive amount, a year, day or hour
    period, in pounds or euros. Anything else is a rate not stated, words like
    competitive included, because no floor can be met by it."""
    def num(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return (any(a is not None and a > 0 for a in (num(pay_min), num(pay_max)))
            and (period or "").strip().lower() in ("year", "day", "hour")
            and (currency or "").strip().upper() in ("GBP", "EUR"))


# ------------------------------------------------------------- gate states
#
# A gate has one of four states. Passes and fails are the two verdicts. Not
# stated means the advert gives nothing for the gate to judge, which is
# neither a pass nor a fail and is the first question to ask. Not judged
# means no verdict is on file at all.
#
# Only Rate can be not stated today, and code decides it, never the scorer:
# a rate with no figure on file (pay_on_file) is not stated, and the gate
# column holds NULL with rate_stated 0. Sector or Location could carry not
# stated later by adding a check of their own to STATABLE_GATES; every reader
# below, the tier included, follows from that one table.

PASSES, FAILS, NOT_STATED, NOT_JUDGED = "passes", "fails", "not stated", "not judged"

# What code writes beside a rate the advert does not state. The mark says the
# rest, "Rate not stated, that's your first question".
RATE_NOT_STATED_NOTE = "No figure in the advert. That's your first question."


def _rate_silent(row: dict) -> bool:
    return not pay_on_file(row.get("pay_currency"), row.get("pay_period"),
                           row.get("pay_min"), row.get("pay_max"))


STATABLE_GATES = {"rate": _rate_silent}


def gate_state(row: dict, gate: str) -> str:
    """One gate's state for a stored row: passes, fails, not stated or not
    judged. A gate that can be not stated reads so once the row has been
    scored and the advert gives nothing for it, whatever verdict an older
    score stored, so a rate with no figure never shows a pass or a fail."""
    value = row.get(f"gate_{gate}")
    silent = STATABLE_GATES.get(gate)
    scored = (value is not None or row.get("tier") is not None
              or row.get(f"{gate}_stated") is not None)
    if silent is not None and scored and silent(row):
        return NOT_STATED
    if value is None:
        return NOT_JUDGED
    return PASSES if int(value) else FAILS


def gate_flag(state: str):
    """A state as derive_tier reads it: True, False, NOT_STATED or None."""
    return {PASSES: True, FAILS: False, NOT_STATED: NOT_STATED}.get(state)


def gate_column(value):
    """What a gate column stores for a derive_tier value: 1, 0, or NULL for
    not stated and not judged alike (rate_stated tells those apart)."""
    return int(value) if isinstance(value, bool) else None


def gates_of(row: dict) -> dict:
    """The four gates of a stored row as derive_tier reads them."""
    return {g: gate_flag(gate_state(row, g)) for g in GATE_NAMES}


# The one question a One question away role carries, by the movable gate that
# fails. Written in code, never by the scorer.
QUESTIONS = {"location": "Would they go remote",
             "rate": "Would they move on the rate"}
MOVABLE_GATES = ("location", "rate")

# Words that make a stored question ask about pay. A question the scorer wrote
# before code took the Rate gate over could ask about a rate the advert does
# not state, or ask about pay beside the gate that fails.
_RATE_WORDS = re.compile(
    r"£|\b(rates?|pay|paid|salary|salaries|package|compensation|"
    r"remuneration|money|budget|fees?)\b", re.IGNORECASE)


def asks_about_rate(text) -> bool:
    return bool(_RATE_WORDS.search(str(text or "")))


def question_for(gates: dict) -> str:
    """The question for the one movable gate that fails, else nothing. It
    never asks about a gate that passes or is not stated."""
    for name in MOVABLE_GATES:
        if gates.get(name) is False:
            return QUESTIONS[name]
    return ""


def question_kept(stored, gates: dict) -> str:
    """The question a One question away row shows: the stored one where it
    asks about the gate that fails and not about pay otherwise, else code's
    own. A stored question that asks about pay is kept only on a role whose
    Rate fails, and one on such a role that does not ask about pay is
    replaced, because it was written for another gate. Free, no model call."""
    own = question_for(gates)
    stored = str(stored or "").strip()
    if not stored or not own:
        return own
    return stored if asks_about_rate(stored) == (gates.get("rate") is False) else own


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

FETCH_TIMEOUT = 30          # seconds any one socket operation may wait
FETCH_BODY_SECONDS = 90     # seconds a whole body may take, however it trickles
FETCH_MAX_BYTES = 2_000_000


def request_url(url: str) -> str:
    """The URL as a request line can carry it.

    http.client refuses a space or a control character in a URL and cannot
    encode anything outside ASCII, and both raise errors that are not network
    errors. A link scraped off a page can hold either, and one such link on a
    watched page used to stop the whole watcher run. Anything unsafe is
    percent-encoded the way urllib encodes a redirect, the escapes already
    there are kept, a non-ASCII host name goes to its IDNA form and the
    fragment, which never leaves the browser, is dropped. The URL that is
    stored and hashed is not this one, only the one asked for.
    """
    parts = urllib.parse.urlsplit(url.strip())
    netloc = parts.netloc
    if not netloc.isascii():
        try:
            netloc = netloc.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    safe = string.punctuation
    return urllib.parse.urlunsplit((
        parts.scheme, netloc, urllib.parse.quote(parts.path, safe=safe),
        urllib.parse.quote(parts.query, safe=safe), ""))


def read_capped(resp, limit: int, seconds: float) -> bytes:
    """Read a response body, at most limit bytes and at most seconds long.

    The socket timeout bounds each wait for data, not the whole read, so a
    server that trickles a byte every few seconds could hold a plain read
    open for ever, and with it the run that asked. read1 makes one call on
    the socket at a time, so the clock is looked at between every arrival.
    """
    read = getattr(resp, "read1", None) or resp.read
    deadline = time.monotonic() + seconds
    chunks, total = [], 0
    while total < limit:
        if time.monotonic() > deadline:
            raise TimeoutError(f"the body took over {seconds:g} seconds")
        chunk = read(min(65536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


# What a fetch can raise that says the page could not be had: the network,
# the server's reply, or a URL the request could not be made from.
FETCH_ERRORS = (urllib.error.URLError, http.client.HTTPException,
                TimeoutError, OSError, ValueError)


def _robots_disallows(base: str, ua: str) -> list[str]:
    """Fetch and minimally parse robots.txt for a host. Cached per process."""
    if base in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[base]
    disallows: list[str] = []
    try:
        req = urllib.request.Request(request_url(base + "/robots.txt"),
                                     headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = read_capped(resp, 200_000, 30).decode("utf-8", "replace")
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
    """Polite GET. Returns (status, headers_dict, body_text). 304 gives empty body.

    Status 0 is every way the page could not be had, with the reason in
    headers["error"]: the network, a reply that broke off, a body that took
    too long or a URL no request could be made from. None of them raises, so
    one bad page costs its caller one answer and never the run.
    """
    config = config or load_config()
    ua = config.get("fetch_user_agent", "MedTechRadar/1.0")
    try:
        if not robots_allowed(url, ua):
            return 999, {}, ""  # 999 means blocked by robots.txt, caller records it
        headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml,*/*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        req = urllib.request.Request(request_url(url), headers=headers)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            body = read_capped(resp, FETCH_MAX_BYTES, FETCH_BODY_SECONDS)
            return resp.status, dict(resp.headers), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        if err.code == 304:
            return 304, dict(err.headers), ""
        return err.code, dict(err.headers), ""
    except FETCH_ERRORS as err:
        return 0, {"error": f"{type(err).__name__}: {err}"}, ""


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
