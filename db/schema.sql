-- MedTech Radar database schema. SQLite.
-- Applied idempotently on every connection by scripts/radar_common.py.

CREATE TABLE IF NOT EXISTS opportunities (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash          TEXT NOT NULL UNIQUE,
  first_seen        TEXT NOT NULL,
  source            TEXT,              -- e.g. linkedin-alert, reed-alert, indeed-alert
  company           TEXT,
  title             TEXT,
  location          TEXT,
  salary_rate       TEXT,
  source_url        TEXT,
  thread_type       TEXT CHECK (thread_type IN ('inbound','signal')),
  cv_match          INTEGER,
  want_match        INTEGER,
  combined          INTEGER,           -- round((cv_match + want_match) / 2)
  one_line_why      TEXT,
  red_flags         TEXT,              -- JSON array as text
  suggested_action  TEXT,
  act_by            TEXT,
  status            TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','digested','actioned','dead')),
  status_changed_at TEXT,              -- feeds the ageing section of the digest
  notes             TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash     TEXT NOT NULL UNIQUE,
  first_seen   TEXT NOT NULL,
  source_id    TEXT,                   -- watchlist source id
  company      TEXT,
  headline     TEXT,
  summary      TEXT,
  source_url   TEXT,
  relevance    INTEGER,
  why          TEXT,
  playbook_step TEXT,
  pushed       INTEGER NOT NULL DEFAULT 0,   -- 1 once a ntfy push has fired
  pushed_at    TEXT,
  status       TEXT NOT NULL DEFAULT 'new'
               CHECK (status IN ('new','digested','actioned','dead'))
);

-- Page-diff and RSS state per watched source. One row per watchlist entry.
CREATE TABLE IF NOT EXISTS source_state (
  source_id     TEXT PRIMARY KEY,
  url           TEXT,
  method        TEXT,                  -- rss | diff
  last_checked  TEXT,
  last_hash     TEXT,                  -- hash of normalised page text, diff method
  last_seen_ids TEXT,                  -- JSON array of recent RSS entry ids
  etag          TEXT,
  last_modified TEXT,
  last_status   TEXT
);

-- Outreach log. One row per touch, read by the digest for threads awaiting action.
CREATE TABLE IF NOT EXISTS touches (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  company          TEXT NOT NULL,
  touched_at       TEXT NOT NULL,
  channel          TEXT,               -- comment | connection-note | engagement | artefact | other
  note             TEXT,
  next_action      TEXT,
  next_action_date TEXT
);

-- Run log with token usage. One row per script run. Cost guardrail.
CREATE TABLE IF NOT EXISTS runs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  ts               TEXT NOT NULL,
  workflow         TEXT NOT NULL,      -- inbox | digest | signals | test
  mode             TEXT NOT NULL DEFAULT 'live',   -- live | mock | dry-run
  items_in         INTEGER DEFAULT 0,
  items_new        INTEGER DEFAULT 0,
  model            TEXT,
  input_tokens     INTEGER DEFAULT 0,
  output_tokens    INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  note             TEXT
);

-- Small key value store, e.g. last_digest_ts.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
