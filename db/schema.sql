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
  buying_window     INTEGER NOT NULL DEFAULT 0,
  -- Phase three. Excitement does not add up, it gates. Four gates, each a
  -- boolean with a one-line note, and a tier derived from them. combined and
  -- want_match stay for history and nothing new reads them.
  gate_sector       INTEGER,          -- 1 pass, 0 fail, NULL not yet gated
  gate_sector_note  TEXT,
  gate_cv           INTEGER,
  gate_cv_note      TEXT,
  gate_location     INTEGER,
  gate_location_note TEXT,
  gate_rate         INTEGER,
  gate_rate_note    TEXT,
  tier              TEXT CHECK (tier IS NULL OR
                                tier IN ('top','question','reading','filtered')),
  failed_gates      TEXT,             -- JSON array of gate names as text
  question_text     TEXT,             -- the one question, question tier only
  filter_reason     TEXT,             -- why it was filtered, auditable
  rate_stated       INTEGER,          -- 1 the advert stated pay, 0 silent
  rate_value        REAL,             -- the gating figure in GBP, in its own basis
  rate_basis        TEXT,             -- day-rate | hourly | salary | unstated
  ir35              TEXT,             -- inside | outside, recorded never gated
  location_class    TEXT,             -- remote | hybrid | local | relocation | onsite-far
  engagement_type   TEXT,             -- contract | permanent | unknown, off the advert
  engagement_note   TEXT              -- the phrase that decided it, "fixed term" and the like
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
  note             TEXT,
  -- ok | partial | failed, for a job that says how it ended (the signal
  -- watcher and the advert checker), NULL for the jobs that do not. A
  -- failed run writes its row too, so a stopped job shows as failing.
  outcome          TEXT
);

-- The scores an opportunity has held, oldest first, so a later change to the
-- scorer can be measured against what it replaced. A new score writes a row of
-- its own (event scored). Before a score is overwritten in place, by a
-- re-score, by regate_rates.py or by the page checker's rate re-tier, the
-- outgoing values are written here (event replaced) unless they are already
-- the newest row. The opportunities row holds the current score and this table
-- holds the ones before it. The date, basis, prompt version and model describe
-- the score a row carries, and recorded_at is when the history row itself was
-- written. A score made before the record was kept reads card, pre-record,
-- unknown and no date. Rows are never updated or deleted.
CREATE TABLE IF NOT EXISTS score_history (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id  INTEGER NOT NULL,
  recorded_at     TEXT NOT NULL,      -- when this row was written
  event           TEXT NOT NULL,      -- scored | replaced
  writer          TEXT,               -- the script that wrote it
  scored_at       TEXT,               -- when the held score was made, NULL if never recorded
  score_basis     TEXT,               -- card | spec
  prompt_version  TEXT,               -- hash of prompts/scorer.md, or pre-record
  score_model     TEXT,               -- the model that answered, or unknown
  cv_version      TEXT,
  cv_match        INTEGER,
  gates           TEXT,               -- JSON, {"sector": 1, "cv": 0, "location": 1, "rate": null}
  tier            TEXT,
  filter_reason   TEXT,
  one_line_why    TEXT
);
CREATE INDEX IF NOT EXISTS idx_score_history_opportunity
  ON score_history (opportunity_id, id);

-- The tailored CV, one per role. Made from the master CV the first time the
-- role's Prepare application page opens, edited on that page, and never
-- written back to the master. Keyed on the role, so every advert in a repost
-- chain shares one copy: role_ref is 'key:' and the role_key, or 'id:' and the
-- row id for a row that has no role_key. cv_text is the copy as it stands, in
-- the editor's layout, and cv_hash is cv_store.cv_hash of it. The saved copy is
-- what Save CV stored, under the name it was saved as, and is what Download
-- builds the Word file from and what Applied records; a save replaces the one
-- before. score is the latest tailored score as JSON, made against this copy
-- and the role's stored description, and kept here and not on the role,
-- because the role's own score is the master CV's. Every tailored score is
-- also a score_history row with event tailored. Rows are never deleted.
-- Nullable throughout, apart from the key, like every column added since the
-- first schema.
CREATE TABLE IF NOT EXISTS tailored_cvs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  role_ref        TEXT NOT NULL UNIQUE,
  opportunity_id  INTEGER,            -- the row whose page made the copy
  created_at      TEXT,
  master_version  TEXT,               -- the master CV version it was made from
  cv_text         TEXT,
  cv_hash         TEXT,
  updated_at      TEXT,
  saved_text      TEXT,
  saved_hash      TEXT,
  saved_name      TEXT,               -- "CV - Luke Keogh - <Company> - <Job title>"
  saved_at        TEXT,
  score           TEXT
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

-- The applications board (design/jobs/applied-board v2). One row per role
-- applied for or being prepared, keyed on the role as tailored_cvs is
-- (tailored_cv.role_ref), with the stage it stands at. stage is NULL when the
-- role has left the board: an application undone on Jobs with no saved CV.
-- Radar sets preparing (a tailored CV saved, not applied) and applied (marked
-- applied on Jobs or Prepare application), and the 21-day rule closes an
-- untouched Applied item as no-reply; every other move is the owner's. A
-- closed application is not a closed advert: closed_at, expiry and the page
-- checker never read this table, and applied_at stays on the role whatever its
-- stage, so an applied role stays protected. moved_at is the owner's last
-- movement (a stage change or a next step set, changed or cleared); a contact
-- logged with the company counts too and is read from touches. Rows are never
-- deleted. Nullable throughout apart from the key, like every table since the
-- first schema.
CREATE TABLE IF NOT EXISTS applications (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  role_ref           TEXT NOT NULL UNIQUE,
  opportunity_id     INTEGER,
  stage              TEXT CHECK (stage IS NULL OR stage IN
                       ('preparing','applied','conversation','interviewing','offer','closed')),
  closed_reason      TEXT CHECK (closed_reason IS NULL OR closed_reason IN
                       ('no-reply','turned-down','withdrawn')),
  stage_at           TEXT,               -- when it entered this stage
  stage_by           TEXT,               -- radar | owner, who moved it here
  moved_at           TEXT,               -- the last movement, see above
  next_step          TEXT,
  next_step_due      TEXT,               -- YYYY-MM-DD
  next_step_by       TEXT,               -- radar | owner
  interview_at       TEXT,               -- YYYY-MM-DDTHH:MM, UK time as entered
  follow_up_added_at TEXT,               -- when Radar added Follow up in this stay in Applied
  created_at         TEXT
);

-- Every change to an application, with when and who, so Home can show the
-- history later. kind is stage (from_stage to to_stage, reason for a close)
-- or next-step (note holds the step as set, empty when cleared).
CREATE TABLE IF NOT EXISTS application_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  application_id  INTEGER NOT NULL,
  at              TEXT NOT NULL,
  actor           TEXT NOT NULL CHECK (actor IN ('radar','owner')),
  kind            TEXT NOT NULL CHECK (kind IN ('stage','next-step')),
  from_stage      TEXT,
  to_stage        TEXT,
  reason          TEXT,
  note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_application_events_app ON application_events(application_id);

-- Home's Done (key E): an item taken off Home, and only off Home. item_key
-- is the item's address on Home: signal:<event key>, role:<role key>,
-- app:<application id>:<due date> or radar:<job>:<state>, so a new due date
-- or a job's change of state brings the item back as a new one. Undo last
-- deletes the row. Nothing else reads it, and nothing here changes the
-- signal, the role, the application or the job where it lives.
CREATE TABLE IF NOT EXISTS home_done (
  item_key  TEXT PRIMARY KEY,
  done_at   TEXT NOT NULL,
  title     TEXT
);

-- The Watchlist's suggestions (design/watchlist/final, decisions of 15 Nov
-- 2026). Radar suggests a company it has seen and the owner does not watch,
-- from a buying window, a funding round or an Insights story at 70 or more,
-- worked out in code after each signal watcher run (watchlist.refresh_
-- suggestions), never by a model. kind is the strongest signal behind it and
-- signal_id that signal. A suggestion waits until the owner adds it
-- (accepted_at) or dismisses it (dismissed_at, dismissed_by owner); one left
-- unanswered for 14 days is folded into Dismissed by Radar (dismissed_by
-- radar). came_back is 1 on the one return a dismissed company is allowed,
-- and only on a stronger kind of signal than it was dismissed on. Nothing
-- joins the Watchlist from here until the owner presses Add. Rows are never
-- deleted. Nullable throughout apart from the keys.
CREATE TABLE IF NOT EXISTS watch_suggestions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id    INTEGER NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('buying-window','funding','story')),
  signal_id     INTEGER,
  relevance     INTEGER,
  suggested_at  TEXT NOT NULL,
  accepted_at   TEXT,
  dismissed_at  TEXT,
  dismissed_by  TEXT CHECK (dismissed_by IS NULL OR dismissed_by IN ('owner','radar')),
  came_back     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_watch_suggestions_company ON watch_suggestions(company_id);

-- People the owner added by hand to a company's record, with a name and a
-- title, beside the published chief executive and chief technology officer
-- the enricher keeps in companies.people. removed_at hides one, never
-- deleting it.
CREATE TABLE IF NOT EXISTS company_people (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id  INTEGER NOT NULL,
  name        TEXT NOT NULL,
  title       TEXT,
  added_at    TEXT NOT NULL,
  removed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_company_people_company ON company_people(company_id);

-- The experience bank (owner, 18 Nov): one entry is one thing the owner
-- did, and experience_evidence holds what backs it. Entries come in by hand
-- or pasted (experience.py reads the paste in code, never a model), wait in
-- draft until reviewed, and are marked backed only when they have evidence
-- or a named voucher and their stage is not discussed, a rule experience.py
-- holds on the server. tags is one comma-separated line. Deleting an entry
-- deletes its evidence, in code, in one transaction. Nothing else reads or
-- writes these two tables apart from Prepare application, which reads the
-- backed entries.
CREATE TABLE IF NOT EXISTS experience_entries (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  did         TEXT,
  result      TEXT,
  period      TEXT,
  place       TEXT,
  tags        TEXT,
  stage       TEXT NOT NULL DEFAULT 'drafted'
                CHECK (stage IN ('delivered','drafted','discussed')),
  vouch       TEXT,
  questions   TEXT,
  status      TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','backed','parked')),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experience_evidence (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id  INTEGER NOT NULL,
  name      TEXT,
  location  TEXT,
  shows     TEXT,
  level     TEXT NOT NULL DEFAULT 'describe_only'
              CHECK (level IN ('can_show','describe_only','public'))
);
CREATE INDEX IF NOT EXISTS idx_experience_evidence_entry ON experience_evidence(entry_id);

-- Small key value store, e.g. last_digest_ts.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
