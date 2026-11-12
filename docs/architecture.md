# Radar architecture

How the pieces fit, and why they fit that way.

## The shape of it

Email is the ingestion bus. Every job alert, Google Alert and Dealroom notification lands in one Gmail aggregator inbox. That gives us one door to watch, free deduplication of sources, and no scraping of anywhere that would rather not be scraped. LinkedIn data arrives this way, and the radar does not fetch LinkedIn on a schedule. That is a hard rule. The one function that asks LinkedIn for a page is `check_jobs.fetch_job_page`, an anonymous read of a public job page with no login. It has two callers, the page checker, whose workflow is committed inactive and dry run, and the one fetch a click on Prepare application makes.

n8n is a thin shell. It handles triggers, Gmail OAuth and email sending, and nothing else. All logic lives in versioned Python scripts under `scripts/`, which n8n calls through Execute Command nodes. The reason is one source of truth. The acceptance tests in `test/` exercise the exact code the workflows run, so there is no drift between what was tested and what runs in production. If a scoring rule changes, it changes in one file under version control.

SQLite is the store. One file, `db/radar.sqlite`, holding opportunities, signals, source state for the diff watcher, touches for the tracker, and a runs table that logs token usage per execution. The schema in `db/schema.sql` applies idempotently, so scripts can be re-run without ceremony. Deduplication is on a normalised URL hash, checked before any scoring call is made.

## The three workflows

**radar-inbox** drains the aggregator inbox. The trigger keys on labels alone, not read state, so opening an email on the phone cannot hide it from the pipeline. Each email is packed to JSON and handed to `scripts/process_email.py`, which splits alert emails into individual opportunities, extracts fields with Haiku, dedupes against SQLite, scores new items with Sonnet against the CV and preferences, and writes rows. A processed email gets the `radar-processed` label. An email that fails extraction stays unlabelled for a retry, and after three failed attempts, tracked in the `email_attempts` table, it is shelved under `radar-failed` so a poison message costs at most three attempts. Live scoring refuses to run when no CV is in `config/profile/`, because a plausible score from the wrong document is worse than a loud failure.

**radar-digest** runs Monday at 07:30. The cron fires in the n8n instance timezone, so the host sets `GENERIC_TIMEZONE=Europe/London` or the expression needs adjusting. `scripts/build_digest.py` pulls everything at or above the score threshold since the last digest, Inbound first then Signals, plus an ageing section for flagged items older than two weeks, threads awaiting action from the tracker, and a Needs review section for anything the scorer failed to score. Every build stores its exact item set under a one-use token, and after Gmail confirms a sent message id the workflow commits that token, marking exactly the emailed items digested and stamping the window from the build time, so a late arrival falls into next week rather than vanishing. A quiet week still sends a short digest saying so, controlled by `digest_send_when_empty`, because silence should mean breakage and nothing else.

**radar-signals** runs every two hours. `scripts/check_signals.py` walks `config/watchlist.yaml`, reading RSS where a feed exists and fetching plus hashing the page where it does not. Politeness lives in the script. Robots.txt is respected, intervals are honoured, ETags are reused. Fresh diff items get one polite fetch of the article page so the scorer reads real text rather than a headline, falling back to the anchor text when the fetch fails. New items are scored for relevance, and anything at or above the fast signal threshold becomes an ntfy push with the playbook step to take today. A failed push is logged and retried by a catch-up sweep on later push runs, inside a 24 hour window, so an outage delays a push rather than losing it. Everything else waits for the digest.

Two commands close the loop by hand. `python scripts/touch.py mark "Company" --as actioned` retires a thread so the ageing section stops nagging, and `python scripts/rescore.py` re-scores anything in Needs review. SQLite runs in WAL mode with a busy timeout so the three workflows can overlap on the one file.

## Modes

- **Live.** `ANTHROPIC_API_KEY` present in `.env`, real Claude calls, real writes.
- **Mock.** `RADAR_MOCK=1` or `--mock`. Deterministic keyword scorers from `test/`, no network, no key needed. Every mock row is marked `mode='mock'` in the runs table.
- **Dry run.** The default for anything that would leave the machine. The digest writes to `test/last_digest.html` instead of sending. Signal pushes write to `test/last_signal.txt` instead of hitting ntfy. Nothing goes live until you arm it deliberately.

## Deployment on the n8n host

The scripts must exist on the machine where n8n runs, because Execute Command runs there.

1. Clone the repo onto the host, for example to `/data/medtech-radar`.
2. Install Python 3.11 or later, then `pip install -r requirements.txt`.
3. Create `.env` in the repo root with `ANTHROPIC_API_KEY=...` and `chmod 600 .env`. The key lives nowhere else.
4. Copy your CV and preferences into `config/profile/`. Both are gitignored.
5. In each workflow's Config node, set `repo_path` and `python_bin` to match the host.

If n8n runs in Docker, mount the repo into the container so Execute Command can see it.

```yaml
# docker-compose snippet
services:
  n8n:
    volumes:
      - /data/medtech-radar:/data/medtech-radar
```

The official n8n image is Alpine and ships without Python, so extend it.
Newer n8n images, 2.x onwards, are hardened and ship without `apk` as well,
so the package manager has to be borrowed from a build stage and removed
again afterwards. Pin the n8n version you are already running, which means
n8n upgrades need an image rebuild rather than a pull.

```dockerfile
FROM alpine:3.22 AS tools
RUN apk add --no-cache apk-tools-static

FROM n8nio/n8n:2.14.2
USER root
COPY --from=tools /sbin/apk.static /tmp/apk.static
RUN /tmp/apk.static add --no-cache python3 py3-pip \
 && pip3 install --break-system-packages \
        "anthropic>=0.120.2,<0.121" PyYAML feedparser \
 && rm /tmp/apk.static
USER node
```

On older images that still carry `apk`, the plain
`apk add --no-cache python3 py3-pip` route works without the build stage.

The anthropic range in that line is the one in requirements.txt, and the two
move together. The scorer sends `temperature=0`, the Python SDK removed that
keyword at 1.0.0, and an unpinned install now resolves to 2.x, where every
live call raises a TypeError before it reaches the API.

n8n 2.x also disables the Execute Command node in code, not in the
environment. The default in `@n8n/config` excludes both
`n8n-nodes-base.executeCommand` and `n8n-nodes-base.localFileTrigger`, so a
freshly deployed instance rejects the Radar workflows with "Unrecognized
node type" even though nothing set `NODES_EXCLUDE`. The fix is to set it
explicitly on the n8n service, keeping the half of the default Radar does
not need:

```yaml
NODES_EXCLUDE: '["n8n-nodes-base.localFileTrigger"]'
```

Worth remembering that this is instance-wide. Any workflow in that n8n, and
anyone with access to its UI, can then run shell commands inside the
container, which has the repo, `.env` and the API key mounted. On a
single-user homelab that is the bargain the thin-shell architecture is
built on, and excluding only `localFileTrigger` is the smallest version of
that bargain.

## Viewing the dashboard, and writing back through it

`scripts/serve_dashboard.py` serves the dashboard over HTTP, re-rendering
from the database on every load, with a Refresh button and a Check now
button that runs the signals watcher on demand through its normal
politeness gate. Since the 29 July brief it is also the write path for
human actions, which changes the deploy by one step: the process needs
write access to `db/` and to `config/profile/`, where before it could run
read-only.

The writes it takes. Acknowledge and its undo (POST /ack and /unack)
stamp or clear `acknowledged_at` on one opportunity row. Applied and its
undo (POST /apply and /unapply) mark a role applied for, see "Roles applied
for" below. Prepare application (POST /prepare, behind a read-only page at
GET /prepare/<id>) fetches an advert's full description and scores the role
against it, see "Prepare application" below. The /cv page
accepts a CV as md, txt, docx or pdf, extracts it to markdown, shows the
extracted text, and on an explicit confirm saves it as a dated version
under `config/profile/` with the `cv.active` marker pointing at it. Every
accepted version stays, history is append-only. A capped stale re-score
button re-scores up to 25 unacknowledged rows against the active CV after
a change, behind a confirm, spending real tokens.

Bind address and port come from `dashboard_host` and `dashboard_port` in
radar.yaml, 127.0.0.1 by default, `--host` and `--port` override. The
LAN-trust assumption, stated plainly: anyone who can reach the port can
read scored opportunities and acknowledge rows or change the active CV.
Keep it on the LAN or the tailnet, and if it ever goes behind a reverse
proxy that does not restrict who may connect, give it authentication. Start
it with `--push` only once the system is armed, so the button matches what
the schedule does.

The write guard. A page open in Luke's browser can make that browser send
a request to this server, and the proxy's allow list passes it, because it
really does come from his machine. So every POST must carry an
`X-Radar-Request` header. The page scripts add it to every fetch call that
sets method POST, so a script added later must post that way. A page on
another site cannot add the header without a CORS preflight, and this
server never answers one. The Host header must also name the server, and so
must the Origin header on a POST. The names it answers to are `localhost`,
`127.0.0.1`, `::1`, the bind address and `dashboard_allowed_hosts` in
radar.yaml, compared without the scheme or the port. In enforce mode a name
that is not listed gets a 403 on every page, so add the LAN address or the
tailnet name before using it. `GET /health` skips the Host check so a
monitor can reach it by any name.

`dashboard_guard: warn` logs each request it would refuse and refuses
nothing. It also logs once each distinct Host and Origin that passes, which
is how the first day shows what Luke's own browser and the proxy really
send. `dashboard_guard: enforce` refuses with a 403 and a note the page
shows, and forbids framing the dashboard in another page. The mode is read
when the server starts, so changing it takes a restart.

This is not authentication. It stops a page on another site using Luke's
browser to write. It does not stop anyone who can reach the port and sends
the header.

An exception inside a route is a 500 with a note instead of a dropped
connection, and the traceback goes to the dashboard log.
`test/run_hardening_test.py` checks the guard, the 500 and the anthropic
range.

Upgrading an existing deployment to the rate-band version: pull, restart
the dashboard server, then run `python scripts/backfill_pay.py` once per
hundred stored rows until it reports `"remaining": 0`. The backfill uses
the fast model only, skips acknowledged and retired rows, and leaves
stored why text alone. Database columns migrate automatically the first
time any writer opens the file, and a timestamped copy under
`db/backups/` before the first run of the new version is cheap insurance.

## Backups, and how to restore

Two things here cannot be rebuilt. `db/radar.sqlite` is the business memory,
the touch log, the buying-window history and the dedupe record that stops the
same advert being scored twice forever. `config/profile/` holds the CV versions
every score was made against, is gitignored by design, and exists on the n8n
host and nowhere else. Losing it turns every `cv_version` stamp in the database
into a pointer at nothing.

`scripts/backup_radar.sh` takes both. The database is copied with `VACUUM INTO`
and never with `cp`, because three workflows overlap on one file in WAL mode and
a plain copy can catch a write in progress, producing a file that opens cleanly
and is quietly missing the last transaction. That is the worst kind of backup,
the kind that looks fine. The script proves the snapshot opens and carries rows
before reporting success, and rotates its own local copies.

Getting the copies off the host is deliberately a separate job, because a backup
that lives beside the thing it protects is not a backup. On this homelab that is
`/home/keoghpi/medtech-radar-offsite.sh` on the Pi, run by the
`medtech-radar-backup.timer` systemd timer at 02:37 nightly with `Persistent=true`
so a machine that was off overnight still takes its backup on the next boot. It
runs the repo script inside the container, copies the artefacts out through the
Proxmox node, verifies the copy that landed rather than the one at source, and
keeps 14 daily and 8 weekly under `/mnt/ssd3/backups/medtech-radar/`. It uses
only credentials that already existed, and in particular the container was not
given an SSH key to the Pi to make it work.

### Restoring

```bash
# 1. Pick a copy. Newest first.
ls -1t /mnt/ssd3/backups/medtech-radar/daily/radar-*.sqlite | head

# 2. Restore to a scratch path and open it there first. Never restore
#    straight over a live database.
cp /mnt/ssd3/backups/medtech-radar/daily/radar-<stamp>.sqlite /tmp/restored.sqlite
python3 -c "import sqlite3;c=sqlite3.connect('file:/tmp/restored.sqlite?mode=ro',uri=True);\
print(c.execute('PRAGMA integrity_check').fetchone()[0]);\
print([c.execute(f'select count(*) from {t}').fetchone()[0] for t in ('opportunities','signals','touches')])"

# 3. Compare against live before deciding anything.
sudo -u radar sqlite3 /opt/biz-admin/medtech-radar/db/radar.sqlite \
  "select count(*) from opportunities; select count(*) from signals; select count(*) from touches;"

# 4. Only when the numbers make sense, stop the dashboard, move the live
#    file aside rather than deleting it, and put the restored copy in place
#    owned by uid 1000.
```

The profile archive restores with `tar xzf profile-<stamp>.tar.gz -C <target>`,
which recreates `config/profile/`. Restore it alongside the database from the
same night, because a database whose scores were made against a CV you no longer
have is only half a restore.

Restore was proved on 2 August 2026 from the off-host copy, matching live at 190
opportunities, 34 signals and 0 touches, integrity ok, with `cv.txt` and
`preferences.md` intact in the archive.


## The companies model, added August 2026

The landscape is companies, not rows. `companies` is keyed on the normalised
name, lowercase and collapsed whitespace and nothing cleverer, because suffix
stripping would let a different Veltrix inherit a touch history it never earned.
`opportunities`, `signals` and `touches` all carry `company_id`, and they keep
their name columns too, because those are what the page shows and what a
restored row still has if a join ever breaks.

Relationship states are split by who owns them, and that split is the point.
**Seen, touched and window open are derived at read time** from facts already
stored, an item exists, a touch exists, a job-advert signal exists. **In
conversation, client and dead are stored** and only ever set by a human, through
`touch.py state` or the dossier. A stored copy of a derivable fact is a second
source of truth that eventually disagrees with the first. Dead outranks
everything including client, because a human said so.

**Enrichment** runs once per company, ever, at first sight, capped by
`enrich_cap_per_run` and logged as its own `enrich` line in the runs table. It
makes one polite fetch of the company site only where the item names one, and
guesses no domain from a company name, because guessing a domain and knocking on
it is how a polite watcher stops being one. LinkedIn heads a list of hosts never
treated as a company site. Empty fields are the honest answer and the common
one, and the prompt is told so twice.

**Regions** group the Insights page as Local, UK, Europe and Elsewhere, ordered
in `radar.yaml` where Luke edits the Local list. The region is a cache, computed
from country and city and mirrored onto every row so grouping is a column read.
The rules carry a fingerprint, and a company with no region is itself reason to
recompute, which is the lesson of 3 August when enrichment filled in 39
countries and the page did not move because only the rules were being watched.

**The dossier** at `/company/<id>` is one screen per company, the timeline
interleaving adverts, insights and touches newest first. It is read-only except
the three human states and the touch outcomes.

**Drafts** are written at push time for the announcement-day step only, stored on
the signal, and never sent by anything. Every draft is checked before storing and
refused outright on failure, because a bad draft is worse than none, it is
sitting there ready to send.

**Tripwires** route a first quality or regulatory hire at an untouched medtech
company down the signal path under `qa-tripwire`, with no fixed relevance, so
the rubric judges it. Three gates, title, sector and not-already-touched, and
the sector exclusion beats the inclusion so a hospital QA post never fires.

**Traction** on Home measures whether the doctrine is being worked rather than
whether the market moved. Outcomes on touches, none, reply or conversation, are
set by hand only.

## How a score was made, and what it replaced

Every score says how it was made. The opportunity row keeps four things beside
the score. The basis is `card` for the advert card an alert email carries, or
`spec` for a full job spec. No scorer is given a full spec yet, so every score
reads `card` today. The prompt version is a short hash of `prompts/scorer.md`,
the first eight hex characters of its sha256 with the line endings made alike,
taken from the file when the rubric is loaded, so it cannot drift from what the
model was shown. The model is the one that answered. The date is when. A row
scored before this was kept reads `card`, `pre-record`, `unknown` and no date,
because the history was not recorded and is not guessed.

The label in the prompt's header comment is for people, and editing the comment
changes the hash like any other edit. To read the version of the file as it
stands, run `python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; print(radar_common.prompt_version())"`
from the repository. `rescore.py` and `regate_rates.py` print the current
version in the JSON they emit. The version covers the rubric only. The
preferences file and the CV are also read by the model, and a change to either
is not a new version. The CV is told apart by `cv_version`, kept beside the
version in the history.

`score_history` keeps the past. A new score writes a row of its own. A
re-score, `regate_rates.py --write` and the page checker's rate re-tier all
overwrite a score in place, and before any of them does, the values it replaces
are written to the table with the date, basis, prompt version, model, CV
version, CV match, gates, tier, filter reason and one-line why they were made
with. The row itself always holds the current score, and the history receives a
state when it is replaced. A re-tier by code changes the tier and the rate gate
and leaves the date, prompt version and model of the model's answer where they
were, because the answer is unchanged. A score the newest history row already
holds is not kept twice, and a dry run writes nothing. The history keeps those
ten fields and no more. The gate notes, red flags, suggested action and the
rate and engagement columns are overwritten without a copy, and so is the
employment type the page checker reads off a page.

A reply the model did not finish, one that stopped at `max_tokens`, is a failed
score. It is not parsed, not stored as a score and not written to the history.
A re-score keeps the previous score, a first score stores the advert unscored
with the reason, and the log names the advert. An advert left unscored still
comes up in the list a manual re-score works through, as before. Only scores
apply this rule. The extractor and the pay backfill parse a reply as they always
have.

The CV pass mark and the Filtered floor are two labelled lines in the
preferences file, `cv_pass_score` and `cv_floor_score`, 70 and 40 as shipped,
read beside the rate floors. A CV match at or above the pass mark passes the CV
gate when a role is scored. A CV match under the floor is Filtered whenever a
tier is worked out, so a changed floor reaches a regate and the page checker,
and a changed pass mark reaches new scores. A role already scored keeps the CV
gate it was given, so a regate reports `cv_gate_stale`, the number of open rows
whose stored gate no longer matches the pass mark now, and only a re-score
changes those. If a line is missing or unreadable
the code uses 70 and 40 and warns once per process, which in the inbox workflow
is once per email, because the hourly run must not stop over it. The inbox run's
note in the `runs` table says when a line was missing or unreadable, because the
warning itself goes to stderr and the workflow does not keep stderr. The reason
stored on a Filtered row is built from the floor in force. The model reads the
preferences file and the rubric points at `cv_pass_score`, so add both lines to
the live file before the first run on this code. The two lines are the only
thing the template says about the thresholds, because the model reads that file
too.

The first open of a database that predates all this takes the write lock for a
moment, to add the four columns and label the rows already scored. The inbox
run holds the write lock for the whole of an email, model calls included, so a
first open that lands in the middle of one waits five seconds and then fails
with `database is locked`. Open it between runs. A dry run of
`python scripts/regate_rates.py` is a harmless way to open it on purpose, and
its `tier_moves_total` is the check that the thresholds moved nothing.
`test/run_record_test.py` checks the record, the history, the thresholds, the
cut off replies and the migration on a copy of a database in the old shape.

## Roles applied for

The Applied button in an expanded Jobs row says "I applied for this". It
stamps `applied_at` with the moment, and `applied_cv` with the saved CV
reference, on the one row it was pressed on, and from then on the whole role
counts as applied. A role is a repost chain, every row sharing a `role_key`, and
a repost arrives as a new row with every stamp empty, so a stamp copied onto the
rows that exist would never reach it. Nothing is copied. The rule is worked out
each time a query runs: `radar_common.APPLIED_SQL` is true for a row that carries
`applied_at`, or whose `role_key` belongs to a row that does, and a row with no
`role_key` stands alone. Every statement that picks open rows, or changes one,
adds `NOT_APPLIED_SQL`, so a repost of an applied role is applied the moment it
is stored and never reaches the open tiers as new. Undo clears every stamp in
the chain, and the role is back in the tiers with the tier it left them with.

The marker is two nullable columns that arrive through `_MIGRATIONS`, not a
status. The status check (new, digested, actioned, dead) cannot take a new word
without rebuilding the table. `applied_cv` is empty until a CV page hands a
reference to POST /apply, which takes an optional `cv`, one printable line of at
most 200 characters. Nothing displays it yet.

What leaves an applied role alone, and so keeps its tier as it was on the day:
the forty-five day expiry, the page checker's selection, its close and its
`--id` spot check, `regate_rates.py`, the three re-score selections and their
remaining counts, the pay backfill, the digest's five selections, and
`touch.py mark`. Acknowledge, Closed and Dismiss answer that the role is marked
applied, undo that first, so a page left open from before changes nothing. The
Jobs and Home pages build their open lists without it, so the tiers, the Home
counts, the prospect trend and the ageing never see one. Two things still count
its rows, because they count mail and not work: the flow chart of arrivals and
the board freshness lights. A board's light says whether its email arrives, and
mail about a role already applied for is still mail. The Applied section,
folded below the open tiers on the Jobs page and there at zero too, lists one
entry per role, newest application first, with the company, the title, the day
applied and the days since, the source, a link to the advert and an Undo. Undo
asks first, because it throws the day away.

The inbox still stores a repost of an applied role, scores it and gives it its
role key, so the chain rule has it applied from the moment it exists. At a
company in the touch log it writes no buying-window signal for it, because the
signals sweep would push that to the phone about a role already applied for, and
a company only ever gets one window. The run record notes each such advert. The
row keeps its `buying_window` flag, which is a fact about the touch log and not
a record that a window was written, so the company's dossier marks the advert
and reads touched rather than window open. An advert for another role at the
same company still opens the window, and so does a repost that arrives after the
application is undone, but a repost that was skipped is not revisited by Undo.
The first-hire tripwire, which is for a company that is not touched, is left as
it was.

A click can land at any moment, and the model calls and page fetches are long
enough for one to land in the middle of a pass. Every writer that reads, decides
and then writes closes that gap one of two ways, and a click that arrives while
it works waits for its commit. Either the statement that writes carries the
condition, as the expiry, the page checker's stamp and close, the pay backfill,
touch and the three buttons do, or the writer takes the write lock
(`radar_common.begin_write`) and looks at the marker again under it, as the
regate, the re-score and the page checker's page-read write do. The page checker
and the re-score also look before they spend a fetch or a model call, to save
the work, and the pay backfill counts a row as banded only if its statement
changed one.

Three limits. A repost under another title or another spelling of the company has
another `role_key` and is another role, as it is for Acknowledge and Closed, and
by the same rule a new requisition with the same title at the same company reads
as a repost until Undo. Undo puts a role back with its original dates, so an
application undone after more than forty-five quiet days is retired by the next
live expiry pass, as it would have been without the application. And rolling the
code back does not roll the marker back. The columns and every stamp stay in the
file and the old code ignores them, so on old code an applied role is back in the
tiers and the digest, accepts Closed and Dismiss, and, once the page check is
armed live (the repo's workflow runs it with `--dry-run`), is expired after
forty-five quiet days, closed on a closed page and re-tiered, and the re-score
spends tokens on it. Roll forward again and the stamps are still there, but an
expired role has no Undo. Restoring the pre-deploy backup is the wrong rollback,
because it loses every stamp made since. Run no `rescore.py`, no
`regate_rates.py --write` and no `check_jobs.py --live` on old code in the
meantime.

This is a second first-open event, like the one above it. The first open of a
database that lacks the two columns adds them, which needs the write lock for a
moment, and the inbox run holds that lock for the whole of an email. A restart of
the dashboard that lands in the middle of one waits five seconds, then exits
with `database is locked`. Open the database by hand between runs, as the user
that owns it and from the repository root, before the restart:

```bash
python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; radar_common.get_db().close(); print('opened')"
```

Once that earlier event has happened this open changes nothing else, and a
second open takes no lock at all. On a file that predates it, the open also does
that event's work.

`test/run_applied_test.py` checks all of it, including a scan of the syntax of
every script that fails when a statement picks open rows by a lifecycle stamp,
or updates or deletes rows of the opportunities table, and neither carries the
condition nor is named, with a reason, in its list of exceptions. It reads the
strings that name the table, so a statement put together out of variables is
beyond it.

## Prepare application

The Prepare application link in an expanded Jobs row opens `GET /prepare/<id>`,
a page that only reads. It shows the role, the advert's full description, the
score made against it, each ask the advert makes with the CV lines that evidence
it, and the pay the description names. Its address carries `go=1`. A page opened
with that, for a role with work to do, tells its script to make the one write
this feature has, `POST /prepare` with the role id, behind the guard every other
write is behind. The script cleans the address before it posts, so a reload, the
back button and a second tab open the page and do nothing more, and a page opened
any other way offers a button instead. The page reads on a read-only connection.
An id that is not plain digits, or not a role, is a 404.

What one POST does for an open role. If the row stores no description, it
fetches the advert's public LinkedIn page once, with `check_jobs.fetch_job_page`,
the page checker's own fetch with its user agent, its identity encoding, its
timeout, no cookie and no login. Only a page the checker's ladder calls live is
read, so a closed advert, an expired redirect, the authwall and a page for some
other advert are told to the page and nothing is stored. The description is
stored in `description_text` as the checker's reader cuts it, at its 20,000
characters, with `description_fetched_at`. A 429 stores nothing and stops every
later fetch from this page for the checker's backoff, ten minutes, held in memory
and so forgotten by a restart. Fetches are also spaced by the checker's eight
seconds. The 45 day expiry is not touched, and neither is `last_checked_at` or
`closed_at`: a click is not a sighting, and nothing here ever closes a role.

Then it scores the role against the stored description, once per version of it.
The version of a description is `description_version`, twelve hex characters of
its sha256, and the score records the version it was made against in
`description_hash`, so a description that has been scored is not scored again
however often the page is opened, and one that has changed is scored once more.
The score is stamped `spec`, with the prompt version, the model and the date, the
score it replaces goes to `score_history` first, and the new one is recorded
there. The four gates and the tiers are the scorer's and `derive_tier`'s as ever.
A tier that moves is recorded in the history on the score that moved it
(`tier_from`), reported on the page, and never dismisses anything. A role that
moves to Filtered stops being listed on the Jobs page, which counts Filtered
roles, and its Prepare page still opens.

Three columns on `opportunities` and three on `score_history` arrive through
`_MIGRATIONS`, all nullable. `description_fetched_at` is when the text was stored
(the page checker sets it too, beside the text it stores). `description_hash` is
the version of the description the held score was made against, empty for a card
score. `score_working` is the per-ask working as JSON. The history keeps the hash
and the working as `description_hash` and `working`, so it now keeps twelve
fields of a score, and `tier_from`. The working carries the date of the score it
belongs to, and a reader only shows it beside a spec score of that date, so a
writer that replaces the score and knows nothing of the working cannot leave an
old one looking current.

The asks. `prompts/scorer.md` has a marker line, and everything below it is sent
only when the advert is the full description. A card is scored with the file
above the marker, the bytes it always was, and a full description with the whole
file and the CV numbered line by line, `[12] text`. The prompt version is the hash
of the whole file either way. The scorer returns each ask as met, partly or
missing, with the CV line numbers that evidence it, and the code checks every
one: a line must exist and have text, at most four are kept for an ask, at most
25 asks are kept, a met or partly with no valid line is recorded as unanchored and
keeps its status, and a missing ask keeps no line. The lines are numbered by
`cv_store.cv_lines`, one rule for the scorer and for any page that shows the CV
beside the asks: a line break is a line feed, a carriage return and line feed or a
lone carriage return, and a blank line keeps its number. The working keeps the
text of each cited line and the label of the CV it numbered, so a later page can
find the line again and tell whether the CV has changed. A reply with no list of
asks is not a score. The code does not recompute the CV match from the asks, it
reports the asks that are unevidenced.

The ceiling. `MAX_TOKENS` is still 1,024 for the extractor and for a card, whose
replies run 160 to 360 tokens. A full description's call uses `SPEC_MAX_TOKENS`,
4,096, which is about twice the largest reply the prompt allows. What is billed is
what the model writes, about 540 tokens more than a card for a typical twelve
asks, and a reply that ran out of the ceiling is thrown away after being paid for,
so the margin is the cheap part. `test/run_prepare_test.py` builds the largest
reply and holds the ceiling to it.

The pay. The scorer is asked for the shortest stretch of the description that
states pay. The code shows it only if it is found in the description, without
regard to case and with white space folded, and what is shown is the advert's own
characters. A quote that is not found is replaced by the page checker's own
reader, labelled as found by pattern, and where that finds nothing either the page
says the scorer reported pay the advert cannot be made to show. A reply that names
none is believed, and the page says the description names no pay. Nothing is
hard-coded: the floors are read from the preferences file where the gate is made.
The gate itself is as it was, a figure on the row is judged in code against its
floor and a figure only the description states is the model's reading, because the
page checker's pay reader misreads a bonus for the pay and nothing here writes its
figure onto a row.

What stops a click being paid for twice. A role has one lock in the process, so a
second click waits for the first and then finds the work done. A description is
scored once per version. One click is one model call with no retry, where a card
still gets two. A reply that could not be used is not paid for again for five
minutes, and a call that raised is not repeated for twenty seconds, both
remembered for the same role, description, prompt and CV and forgotten by a
restart. The script sends one POST per click and none on a timer. These guards live
in one dashboard process, which is how it runs. Every model call is a row in
`runs` under the workflow `prepare`, so the week's cost counts it.

An applied role, and every row of its chain, is shown and touched by nothing: no
fetch, no model call, no write. A role that is acknowledged, closed, dismissed or
expired is the same. The write that stores a description and the write that stores
a score both take the write lock with `radar_common.begin_write` and look at the
marker again under it, so a role applied for while the page is fetched or while the
model answers keeps the score it was applied with, and the reply that was paid for
is not stored. Both writes also carry `NOT_APPLIED_SQL` on their statements.

What it leaves alone. The Rescore button on the CV page, `/cv/rescore` and
`rescore.py`, are not changed. They share `score_opportunity` and the history
writers with this feature, whose default paths are unchanged. A role scored on its
full description stays eligible for a card re-score if it still carries a legacy
`combined` score, which that button selects on, and the button would replace the
spec score with a card one, keeping the old one in the history.

This is a second first-open event, like the ones above it. The first open of a
database that lacks the six columns adds them, which needs the write lock for a
moment, and the inbox run holds that lock for the whole of an email. A restart of
the dashboard that lands in the middle of one waits five seconds, then exits with
`database is locked`. Open the database by hand between runs, as the user that owns
it and from the repository root, before the restart:

```bash
python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; radar_common.get_db().close(); print('opened')"
```

n8n runs the scripts fresh for each execution, so the prompt and the scripts reach
the inbox with no restart, and only the dashboard needs `docker restart
medtech-radar`. The dashboard process needs the API key in its environment, as the
CV page's re-score button does, and a route to linkedin.com, which nothing else the
dashboard runs needs. Rolling back is code only. The columns stay and old code
ignores them, a spec score on a row reads fine, and a full description that was
stored stays. `test/run_prepare_test.py` checks all of it.

## The renderer, and the one page to trust

`serve_dashboard.py` is canonical. It re-renders from the database on every load
and takes the writes. `build_dashboard.py` is the rendering library it imports,
and its command line still writes a static archive page but prints a deprecation
note when it does. A file written this morning is a photograph of a database
that has moved on, and two surfaces disagreeing is worse than having one.

`GET /health` returns JSON for something else to watch, last inbox run, last
signals run, database reachability and row counts, with a 200 when fresh and a
503 when the database is unreachable or a pipeline has gone quiet for more than
triple its schedule. It exists so that silence means quiet rather than broken,
because nobody opens a dashboard at three in the morning and a pipeline that
stopped at midnight looks exactly like one with nothing to say.

## The fallback

If putting Python next to n8n ever becomes a nuisance, the scripts do not need n8n at all. Run `process_email.py`, `check_signals.py` and `build_digest.py` by cron on any box that has the repo and the key. In that setup n8n keeps only the jobs that genuinely need its credentials, reading Gmail and sending the digest email. Everything else is just Python, SQLite and a schedule.
