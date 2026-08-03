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
  notes             TEXT,
  -- Structured pay, extracted by the fast model, judged by code.
  pay_currency      TEXT,              -- GBP | EUR | USD | '' as extracted
  pay_period        TEXT,              -- year | day | hour | '' as extracted
  pay_min           REAL,              -- lower bound where a range is stated
  pay_max           REAL,              -- upper bound, equals pay_min when single
  day_rate          REAL,              -- converted GBP day rate, code-computed
  rate_band         TEXT,              -- above | close | below | unstated
  -- A human said "seen it". The row leaves the default view, the digest and
  -- the ageing section, but never the database, so dedupe keeps rejecting it.
  acknowledged_at   TEXT,
  cv_version        TEXT,              -- CV file label the score was made against
  -- The doctrine's buying window. Set in code at store time by matching the
  -- advert's company against the touch log, never by a model, because it is
  -- a fact about our own history and not a judgement about the role. A low
  -- job score does not clear it, a generic advert at a company already
  -- touched is still the week the standards stop being abstract for them.
  buying_window     INTEGER NOT NULL DEFAULT 0
);

-- The landscape is companies, not rows. One row per company, keyed on the
-- normalised name so two spellings of the same firm meet here rather than
-- living as strangers in three tables.
--
-- Enrichment fills the descriptive columns once, at first sight. They stay
-- NULL until then and nothing depends on them being present.
--
-- state holds only the three a human sets, and only ever by hand. The other
-- states, seen and touched and window open, are facts already in the
-- database and are derived at read time rather than stored, because a
-- stored copy of a derivable fact is a second source of truth that will
-- eventually disagree with the first.
CREATE TABLE IF NOT EXISTS companies (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  norm_name        TEXT NOT NULL UNIQUE,   -- normalise_company(display_name), the natural key
  display_name     TEXT NOT NULL,          -- as first seen, for the page
  country          TEXT,
  region           TEXT,                   -- computed from country and city, phase two task three
  city             TEXT,
  ecosystem        TEXT,                   -- imec, Ghent, KU Leuven, and the like
  stage            TEXT,                   -- seed, series A, and the like
  what_they_build  TEXT,
  software_content TEXT,
  people           TEXT,                   -- JSON array, only where the company published them
  first_seen       TEXT NOT NULL,
  enriched_at      TEXT,
  enrich_status    TEXT,                   -- ok | text-only | failed, with the reason
  state            TEXT CHECK (state IS NULL OR
                               state IN ('in-conversation','client','dead')),
  state_changed_at TEXT,
  notes            TEXT
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
               CHECK (status IN ('new','digested','actioned','dead')),
  cv_version   TEXT,                   -- reserved, unused. Signal scoring is
                                       -- rubric-only today and never reads
                                       -- the CV, so nothing writes this.
  -- A human said "not interested". The insight leaves the Insights page,
  -- the digest and the catch-up pushes, but never the database, so the
  -- URL-hash dedupe keeps rejecting it forever, same rule as the jobs.
  acknowledged_at TEXT
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
  next_action_date TEXT,
  -- What came back. NULL and 'none' both mean nothing yet, which is the
  -- honest default and the common case. A human sets this, never the
  -- machine, because only a human can tell a polite acknowledgement from
  -- the start of a conversation.
  outcome          TEXT CHECK (outcome IS NULL OR
                               outcome IN ('none','reply','conversation'))
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

-- Extraction attempts per email, keyed on a hash of sender, subject and
-- date. Feeds the poison-message cap. An email that fails extraction three
-- times gets give_up in the script output and the radar-failed label in the
-- workflow, so it stops retrying every hour forever.
CREATE TABLE IF NOT EXISTS email_attempts (
  email_hash   TEXT PRIMARY KEY,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_attempt TEXT,
  subject      TEXT
);

-- Small key value store, e.g. last_digest_ts.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
