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
against it, see "Prepare application" below. The same page holds the role's
tailored CV, with four writes of its own (POST /prepare/cv/open, save, reset
and rescore) and a read-only Word download, see "The CV page and the tailored
CV" below. The /cv page holds the master CV in an editor, and saving it there
(POST /cv/master/save) writes a new dated version under `config/profile/`
with the `cv.active` marker pointing at it. Its one-off import accepts a CV as
md, txt, docx or pdf, extracts it to markdown, shows the extracted text, and
on an explicit confirm saves it as a version the same way. Every accepted
version stays, history is append-only. A capped stale re-score button
re-scores up to 25 unacknowledged rows against the active CV after a change,
behind a confirm, spending real tokens.

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
`spec` for a full job description. Only Prepare application, below, gives a
scorer one, so every score the inbox and the re-score make reads `card`. The
prompt version is a short hash of `prompts/scorer.md`,
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
ten fields, for a score made on a full description the hash of that
description and the per-ask working, and for any score made since the quoted
reasons (below) its reasons, thirteen fields in all, and no more. A re-tier
by code keeps the reasons where they are, with the date of the answer they
came with and the gates they were given beside, so a page that shows them
under gates judged again since says so. The gate notes, red flags, suggested action and the
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

## The Rate gate and the tier

A gate has one of four states, kept in one place, `radar_common.gate_state`:
passes, fails, not stated and not judged. Not stated means the advert gives
nothing for the gate to judge, and it is neither a pass nor a fail. Only Rate can
be not stated today. Sector or Location could join it by adding a check to
`radar_common.STATABLE_GATES`, and the pages and the tier follow from that table.

Rate is code's alone, from the extractor's pay columns, never the scorer's. A
figure on file (`radar_common.pay_on_file`: a positive amount, a year, day or hour
period, pounds or euros) passes at or above its floor and fails under it, by the
floor rules `gate_pay` has always applied. No figure, competitive included, is
not stated: `gate_rate` NULL, `rate_stated` 0, `rate_basis` and `rate_band`
unstated and code's note. Since prompt `3c8e6c63` the scorer is asked for no rate
verdict and no question, and anything it returns for them is not read. A figure
that only a full job spec states is not on file, so it reads not stated. The
dry run counts those rows (`rate_unclear`).

`score_item.derive_tier` turns the four states into a tier, so the same states
always give the same tier:

- Top prospects. Sector, CV and Location pass, and Rate passes or is not stated.
- One question away. Sector and CV pass, and exactly one of Location and Rate
  fails.
- Reading. Everything else: Sector or CV failing, Location and Rate both failing,
  or a Sector, CV or Location gate never judged.
- Filtered is unchanged, the CV match under the Filtered floor or not a real role.

The question on a One question away role is code's (`radar_common.question_for`)
and asks only about the gate that fails. A question stored before is kept where it
asks about that gate alone and replaced where it asks about pay on a role whose
Rate does not fail (`radar_common.question_kept`), at render and by the re-sort. A
Top prospect carries no question. Its Rate mark, a question mark in the neutral
colour, says "Rate not stated, that's your first question".

On Jobs the row says only what differs between roles (`jobs_page.row_html`). Pay
shows when the advert's pay text holds a figure, whatever the Rate mark says, and
nothing shows otherwise. The source shows when it is not LinkedIn. The CV figure
is left to the band heading and the role pane. The only state tag is Closed, for
an advert the checker confirmed closed, and a closed advert never reaches the open,
acknowledged or filtered lists, so in practice no row carries one; State in the
pane says live or unknown. A One question away row leads with the stored reason
of the gate that fails, without "judged in code" (`jobs_page.row_lead`), and never
with code's question, which the role pane does not show either
(`jobs_page.pane_lead`). A question the model wrote and that is still stored shows
in the pane. Home keeps `lead_line` and its own tags. The title wraps to two lines
before it is cut, at every width.

The re-sort is `regate_rates.regate`, free, the path Preferences uses when a floor
changes. `python scripts/regate_rates.py` reports what it would do and writes
nothing. `--write` looks first and writes nothing while `rate_unclear` is above
nought, unless `--unclear-ok` says to. It sends nothing, no push and no email.

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
without rebuilding the table. `applied_cv` is the name Save CV gave the role's
tailored copy on its Prepare application page, read in the statement that
stamps the role, or empty when no copy was saved (see "The CV page and the
tailored CV"). POST /apply still takes an optional `cv`, and still refuses one
that is not one printable line of at most 200 characters, but what it names is
not recorded: the column holds what Save CV stored, not what a request says.
The Prepare application page of an applied role shows the name.

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

### The applications board

Built from design/jobs/applied-board v2 (15 Nov 2026). Jobs' Applied view is a
Board by default (`/jobs?show=applied`), with the List a switch away
(`&layout=list`); a role's own address always shows the List beside its pane.
`scripts/applications.py` keeps the store and the rules, `scripts/board_page.py`
draws the board and holds its script.

Each application is a row of `applications`, keyed on the role as
`tailored_cvs` is, with its stage (Preparing, Applied, In conversation,
Interviewing, Offer, Closed), who put it there and when, the owner's last
movement, its next step with a due date, and an interview time. Every change is
a row of `application_events`, with when and who (radar or owner). Radar fills
Preparing when Save CV stores a tailored CV and Applied when a role is marked
applied; Undo applied on Jobs takes it back, and is refused once the owner has
moved the item on. Every later move is the owner's, through POST
`/application/move` (the Move to menu and its keys, never a drag), and next
steps go through POST `/application/next`. A move out of Preparing into an
applied column stamps the role as Mark applied does (`applications.stamp_applied`,
which `set_applied` shares), and the Undo of that move clears it.

A closed application is not a closed advert. Nothing here sets `closed_at`, the
expiry or the page checker's columns, and `applied_at` stays on the role in
every stage, so `APPLIED_SQL` keeps protecting it on the board, Closed included.

The 21-day rule and Radar's Follow up run as the first step of the signals run
(`check_signals.py`, every two hours), `applications.run_rules`: an Applied
item with no movement for 21 days (a stage change, a next step, or a touch
logged with its company) closes as no reply, by Radar; from day 14 the board
says when. An application the one-time migration made is never closed sooner
than 7 days after it, so the first runs after the deploy close nothing
unwarned. Seven days into a stay in Applied the run adds a "Follow up" next
step due that day, unless the owner set one. The run's record notes what it did.
The migration itself runs once, behind `migrated_applications`: applied roles
to Applied dated from `applied_at`, saved CVs to Preparing dated from
`saved_at`; the scheduled run makes any application something else left out.
`test/run_board_test.py` checks it, and `docs/box-checks-applications-board.md`
is the deploy.

## Home

Built from design/home/final and the owner's decisions of 15 Nov 2026, restyled
17 Nov ("Option B"): colour groups things, stories amber, jobs purple,
applications and follow-ups teal (`page_frame`'s `--group-*` colours), each
with an icon chip and a glow from the top left of its cards. On Home a signal
is a story and the signal watcher is the News watcher.
`scripts/home_page.py` draws `/` from one `build_dashboard.collect()` and a few
reads of its own on the same read-only connection (the `home_done` table, the
last runs of a job, company records, Top scores kept in `score_history`). Every
item has its own address, `/?item=<id>`, the items taken off Home are at
`/?show=done`, and `/?part=main` is the page's main element as JSON for its
script.

- The welcome greets by the optional `first_name` line of the owner's
  preferences file (`radar_common.read_first_name`), never a name in the code,
  and "Good morning." without one, with Radar's five lights beside it.
- Five tiles, each a link: urgent stories (a story at the fast bar or over,
  found in the last seven days, hiring stories included), Top prospects and
  One question away (every open role in the tier, `jobs_page.tier_totals`,
  the number on the Jobs page's tier bands), company follow-ups due
  (`watchlist.threads_due`, Watchlist companies only) and application
  follow-ups due today or overdue (the board's next steps).
- Five lights from `job_health`: On time, Late, Stale, Off, Not sending or,
  for the inbox, Quiet, each a dot, the job's name and the word. The phone
  push has no run of its own, so its light comes from the news watcher's runs
  and the ntfy address in radar.yaml.
- Open actions lists Radar's jobs that need a look, urgent stories, new Top
  prospects and new One question away roles (unread, the Jobs page's New),
  and the two kinds of follow-up due, each its own card. Beside it, Jobs
  counts each tier as the Jobs page's bands do and Applications reads the
  board. Week by week, in Radar's health panel, counts new Top prospects (each
  role once, in the week it was first scored Top, from `score_history` and
  each advert's own score date, an applied role left out) and stories that
  scored 40 or more, over four Monday weeks, from the week Radar's first row
  arrived.
- Done is a row in `home_done`, keyed on the item's address on Home
  (`signal:`, `role:`, `app:<id>:<due date>`, `radar:<job>:<state>`), through
  POST `/home/done` and `/home/undone` behind the write guard. It changes
  nothing where the item lives; a new due date or a change in a job's state
  brings the item back.

The old Home is Radar's health, at `/radar` (the lights' "Radar's health"
link): the job lines, the flow, board freshness, the trends, traction, the
circle and the add-a-source form. Its scoring panel moved to the CV page's
Settings (`/cv#settings`), with the first name beside the scorer's lines.
`test/run_home_test.py` checks it, and `docs/box-checks-home-final.md` is the
deploy.

## Watchlist

Built from design/watchlist/final and the owner's decisions of 15 Nov 2026.
`scripts/watchlist.py` holds the rules and every read and write;
`scripts/watchlist_page.py` draws `/watchlist` (the table, with a company's
record beside it at `/watchlist?company=<id>` from 1024 wide) and
`/watchlist/<id>` (the record as its own view, for a phone). The page reads
only; its script writes through guarded POST routes in serve_dashboard.py
(`WATCHLIST_POSTS`, dispatched by `watchlist_post`).

- On the Watchlist means `companies.watched_at` is set. `watched_via` says how
  it joined (insights, suggestion, name, contact) and `watched_signal_id` the
  story it came from. Remove clears `watched_at` and keeps the rest, the thread
  among it, which stops counting; Undo puts back the first date.
- A thread is a company's newest contact in `touches` (newest `touched_at`,
  then id), skipping the rows Set state writes (`kind = 'state'`). It is open
  when that contact set `next_action`; it is due when `next_action_date` is
  today or earlier, UK time. A next step logged here needs a due date; one on
  file with no date shows No date set and is never due. Home's fifth figure
  and the table's tags both read `thread_of`, and only Watchlist companies
  count.
- Suggestions live in `watch_suggestions`, worked out in code by
  `refresh_suggestions` at the end of each signal watcher run (not a spot
  check of one source): an unwatched company with a buying window, a funding
  round at 40 or more or a story at 70 or more found in the last 30 days, at most five
  waiting, strongest first. Fourteen days unanswered folds one into Dismissed
  (`dismissed_by = 'radar'`); a dismissed company comes back once
  (`came_back`), only on a stronger kind of signal; a removed one only on a
  signal since its removal. Nothing joins until the owner presses Add.
- Add by name looks among the companies Radar has seen, ignoring capitals,
  spacing and endings such as Ltd (`loose_name`, for lookup only; the natural
  key stays `normalise_company`). An unknown name is added only when asked, as
  a bare row whose facts stay Not known.
- People added by hand are `company_people`, beside the published chief
  executive and chief technology officer in `companies.people`.
- Read again (`watchlist.reread`) calls `enrich_company` with `force`: one
  polite fetch of `companies.site_url` (or a site the newest story names, never
  a guess) and one call to the fast model, on a press, with the cost from
  `reread_cost` shown first, logged as an enrich run. Each fact keeps its
  source and date in `companies.fact_sources`; a field the new reading leaves
  empty keeps the old fact.

`test/run_watchlist_test.py` checks it, `test/seed_watchlist_sample.py` seeds
the design's invented world for looking at the page, and
`docs/box-checks-watchlist-final.md` is the deploy.

## Prepare application

The Prepare application link in an expanded Jobs row opens `GET /prepare/<id>`,
a page that only reads and never spends on opening, however it is opened
(decisions of 15 Nov). It shows the role, the advert's full description, the
scores, each ask the advert makes beside the CV line that evidences it, and the
pay the description names. The one write this feature has, `POST /prepare` with
the role id, behind the guard every other write is behind, is made by the page's
own button, which states its cost first ("About 3 cents, 1 call: Fetch and score
the full description"), one press at a time. Until 15 Nov the link carried
`go=1` and the page started the work on opening when the browser said, with
`Sec-Fetch-Site: same-origin`, that the click came from this site; that path is
gone. The server reads neither the address nor the header, the Jobs links carry
no `go=1`, and an old bookmark that still has it opens the page and its button.
A refusal or a request that failed gives the button back. The page reads on a
read-only connection. An id that is not plain digits, or not a role, is a 404.

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
seconds. Any other page that cannot be used, an expired redirect, a closed advert,
the authwall, a page for another advert or one with no description, is remembered
for five minutes for that role, so the click that follows says why and asks nobody;
a network that was down is not remembered, and the page offers Try again. A page
opened again inside any wait, that one, the 429's, or a failed score's, says why
and offers no button that would be refused, and the button comes back when the wait
is over. The 45
day expiry is not touched, and neither is `last_checked_at` or `closed_at`: a click
is not a sighting, and nothing here ever closes a role.

Then it scores the role against the stored description, once per version of it.
The version of a description is `description_version`, twelve hex characters of
its sha256, and the score records the version it was made against in
`description_hash`, so a description that has been scored is not scored again
however often the page is opened, and one that has changed is scored once more.
The score is stamped `spec`, with the prompt version, the model and the date, the
score it replaces goes to `score_history` first, and the new one is recorded
there. The gates and the tiers are the scorer's, code's Rate gate and
`derive_tier`'s as ever.
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
old one looking current. A description the page checker has replaced since the
score was made is not drawn beside the asks and the pay read from the old one: the
page says so and offers to score the new one.

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

The ceiling. `MAX_TOKENS` is still 1,024 for the extractor, and for the pay
backfill, which passes no ceiling of its own. A card had it too
until the quoted reasons gave a card's score `CARD_MAX_TOKENS`, 2,048, see
"Quoted reasons" below. A full description's call uses `SPEC_MAX_TOKENS`, 10,240 since
each partly or missing ask carries a short reason (8,192 with the advert sentences
alone, 6,144 before them, see "Prepare application" below).
The largest reply the prompt allows is 9,961 characters, 8,220 of them before
the reasons, about 2,490 tokens at four characters a token and 3,320 at a
pessimistic three, so the ceiling is a little under twice the pessimistic count;
4,096 held the reply before the reasons with that count under three quarters of
it, and does not hold the one with them. What is billed is what the model
writes, about 540 tokens more than a card for a typical twelve asks, and a reply
that ran out of the ceiling is thrown away after being paid for, so the margin is
the cheap part. `test/run_prepare_test.py` builds the largest reply and holds the
ceiling to it.

The pay. The scorer is asked for the shortest stretch of the description that
states pay. The code shows it only if it is found in the description, without
regard to case and with white space folded, and what is shown is the advert's own
characters (`radar_common.find_verbatim`, the rule the quote of every reason is
held to as well, which for a reason also asks that the quote begin and end on
the edges of words; the pay quote is found as it was). A quote that is not found is replaced by the page checker's own
reader, labelled as found by pattern, and where that finds nothing either the page
says the scorer reported pay the advert cannot be made to show. A reply that names
none is believed only if the pattern reader finds nothing either, and then the page
says the description names no pay. Where the reader finds a sterling figure with
its period that a day, an hour or a year of pay could be (a turnover of £50m is
not one), or one of the advert's own words for pay that is not a figure beside a
pay noun (a competitive salary, a negotiable rate, salary depending on experience,
a market rate), the page shows that wording as a disagreement and says it may not
be pay, because a bonus reads the same. A competitive market, a non-negotiable
standard and a negotiable start date are not offered. Nothing is hard-coded: the
floors are read from the preferences file where the gate is made.
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
`runs` under the workflow `prepare`, so the week's cost counts it. One click is one
model call from this code. A reply that is paid for and cannot be read, for any
reason, a number that is an infinity among them, is the same failure with its cost
recorded, and that record waits for the write lock as the writes do. Neither that
notice nor the one for a call that raised offers Try again, because the next click
is refused until the wait is over. The Anthropic client under `claude_call` keeps
its default of two retries for a request that failed in transit, as it does for
every caller, and this feature does not change that because the function is shared.

An applied role, and every row of its chain, is shown and touched by nothing: no
fetch, no model call, no write. A role that is acknowledged, closed, dismissed or
expired is the same. The write that stores a description and the write that stores
a score both take the write lock with `radar_common.begin_write` and look at the
role again under it, applied or not open, so a role applied for, acknowledged,
closed, dismissed or expired while the page is fetched or while the model answers
keeps what it had, the page says so, and the reply that was paid for is not stored,
though what it cost is still a row in `runs`, marked not stored. Both writes also
carry `NOT_APPLIED_SQL` on their statements.

Work that has been paid for is not thrown away because the database is busy. The
inbox run holds the write lock for the whole of an email, model calls included, so
a click can land in the middle of one. Both writes wait for the lock for up to 90
seconds (`WRITE_WAIT_SECONDS`), not the five the connection otherwise waits. If the
database is still busy, the page that was read, or the reply that was paid for, is
held in memory for 30 minutes (`PARK_SECONDS`), the click says so and offers Try
again, and the next click stores it without a second fetch or a second call. What
is held is for the same role, description, prompt and CV, so a changed CV or a
changed description asks again, and a restart of the dashboard forgets it.

What it leaves alone. The Rescore button on the CV page, `/cv/rescore` and
`rescore.py`, share `score_opportunity` and the history writers with this
feature, whose default paths are unchanged. Since the CV page brief, Re-score
stale items picks and counts no role scored on its full description, and every
pass in `rescore.py` looks again under the write lock, so a card score never
replaces a spec score that arrived while it was being made.

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

### The final page (15 Nov)

The page is drawn by `scripts/prepare_page.py`, from design/prepare-application/final
and the decisions of 15 Nov; `build_dashboard.py` keeps thin names
(`render_prepare_page`, `render_prepare_inner`, `PREPARE_CSS`, `PREPARE_SCRIPT`)
for the callers that import them. The role head; the rail of Score, Application,
Rate line and Gates; the job description; and the work: the tailored CV's head,
the bar of its buttons, the gaps and the sheet with its margin notes. Side by side
from 1800px, a band of two columns with the description folded from 1024px, one
column on a phone with the gates and the description folded and the bar at the foot.

Three scores sit side by side: the card's (the row's own, or the newest card score
the history kept), the full spec's, which is the role's and what the Jobs row
shows, and this copy's. The copy's is stored on the copy and in `score_history`
marked `tailored` (`prepare_application._store_tailored`), and nothing writes it to
`opportunities`; the Prepare runner holds the row byte for byte across a Rescore
role.

The CV sheet is light paper inside the dark page, the one approved exception to
dark only: the editor sits inside `.sheet-paper`, whose tokens are in
`page_frame.py`. The editor is `cv_page.py`'s; this page does not restyle it. Its
margin notes are a layer beside the sheet, found by the editor's
`.cvl[data-n]`, numbered as `cv_store.cv_lines`. Gaps come first, above the sheet;
each met or partly ask stands beside the first line it cites that is still in the
copy; met notes fold to one line and open on a click or on focus. `PREPARE_SCRIPT`
finds each cited line again by its words as the copy is edited (`RadarCv.findLine`),
marks an ask stale when a line it cited is edited away, recounts, and moves the
note with its line; the marks themselves change only on Rescore role. It sets no
timer. Mark applied (key M) posts `/apply`, which stamps the role with the saved
CV (`applications.stamp_applied`) and moves it to Applied (`on_applied`); with an
edit not saved, or nothing saved, it asks first in a dialog on the page. Undo (Z)
posts `/unapply`. Save CV puts the role in Preparing through
`applications.on_saved`, as it did. The keys keep the Jobs page's manners: never
while typing, never with a modifier, and a switch, shared with Jobs and the board,
turns them off.

What a press changes comes back as parts: `render_prepare_inner` answers with one
`<template data-part>` for each slot of the page but the editor, the editor puts
the answer into `#prepare-body` as it always has, and the page script moves each
part into its slot. `cv_page.py` gains one additive hook, `window.RadarCvEditor`
(`edited()`, `leaving()` and `lines()`), so the page can tell an edited copy, reload
after Mark applied without the leave prompt, and draw the page estimate.

Each ask now carries the advert sentence it came from. The full description prompt
asks for it (prompt `422cdad4`, from `22353628`), and `score_item.normalise_asks`
holds it to the stored description by the reasons' own rule, `_hold_quote`, which is
`radar_common.find_verbatim` with whole words: kept only where the description has
it word for word, as the description's characters, and otherwise `None` with
`sentence_dropped` saying why. An ask whose sentence was not found shows without a
quoted sentence, and says why only where one was asked for and dropped
(`prepare_page.SENTENCE_DROPPED`): an ask stored before the sentences has no
`sentence` key and says nothing about one.

Since 16 Nov each partly or missing ask also carries a short reason, `why`, at
most 20 words in the scorer's own words (prompt `1b1ffb78`, from `422cdad4`).
`score_item._ask_why` runs it through the voice rules and drops one that is too
long or is a quote of the description (`_hold_quote` again), with `why_dropped`
saying why. A partly note shows it under its sentence, and a gap shows it in
place of "No line on the CV evidences this ask.", which stays only where there
is no reason. A reply can now be 25 partly asks each with a sentence of 25 words
and a reason of 20, so `SPEC_MAX_TOKENS` is 10,240, where the worst reply is 67
per cent of it (it was 6,144, then 8,192). What a press costs is about 3 cents,
at most about 15; the cost note has the sums.

The sheet is drawn as the CV page draws it: the CV page's Design
(`cv_store.read_design`, font, size, spacing and margins) is set on `#cv-pane`
as the CV page's own custom properties, and the A4 page estimate
(`cv_doc.estimate_pages`, and `RadarCv.pages` as the copy is edited) breaks the
sheet into pages, marks the lines past two and says the count under the sheet.
A line's buttons and its add chips show only on the line being edited, and on a
touch screen they are 44px. The notes come before the sheet in the source, so
Tab reaches them before the editor's fields. After a paid press focus goes to
the Score heading and the outcome is said in `#pa-live`, the page's one status
region, there from the start. The gates' Why gives each column its own score's
reason, and says when the copy's gates were judged and against which floor. The
master-change notice names versions by number and date and links to the CV
page's comparison (`/cv?compare=`).

## The CV page and the tailored CV

The master CV lives in Radar as structured text, in `config/profile/` as it
always has, and the scorer reads it there as it always has. The layout is
`scripts/cv_doc.py`: a name on the first line as a level one heading, headline
lines under it, then sections as level two headings, each with paragraph lines
or `- ` bullets, then entries as level three headings with lines of their own.
The four standard sections, Summary, Experience, Skills, and Education and
certifications, are always in the editor, and an empty one puts no bare heading
in the text. `render` turns the structure into text and `parse` turns the text
back, so a CV the editor wrote reads back to the byte; `load_doc` reads any
other text, a version imported from Word, the way a person would, by the usual
words for the sections and the usual bullet marks. That reading keeps every
line, in the order it came in. The first line is the name. A CV with headings
of its own, a Word file's Heading styles, is read by those headings alone: the
shallowest heading that names a usual section sets the section depth, deeper
headings are entries, and a plain line is content, so a label under a job such
as "Overview:" or "Key skills:" stays with the job. Only a CV with no headings
at all has its sections read from plain lines that are only the usual word for
one. A usual section met again while it is still being read carries on (its
words open an entry when it has entries, so "Certifications" after a degree
comes after it); one met again further on becomes a section of its own; an
empty heading gives its words back as the last line before it. Bullet and
heading marks are taken off a line in one pass, so a line of nothing but marks
costs no more than any other, and a text no CV could be, over 200,000
characters, 2,000 lines or 5,000 characters on a line, is refused before any
of it is read (`cv_doc.check_raw`), whether a request sent it or it is the
master on file, so neither a POST nor a page can be held up by one. A master
read that way is not in the editor's layout until it is saved once on the CV
page, and `cv_doc.is_canonical` says which a text is.

The line numbers agree everywhere. The text the editor produces is the text the
scorer is given, so `cv_store.cv_lines` and `cv_store.numbered_cv` number the
page, the scorer's view and the working the same way. The page draws the lines
from the stored text, and its script (`scripts/cv_page.py`, `CV_MODEL_JS`)
writes the layout again in JavaScript to number lines while they are typed; it
checks its numbering of the CV it was given against the server's and refuses to
edit if they differ. `test/run_cv_test.py` holds the two to the same answers on
a corpus under node, and holds the page, the scorer's numbered view and a
working's citations to each other.

The CV page, GET /cv. The master in the editor, saved by Save as a new version
(POST /cv/master/save), which writes a new dated `cv-<stamp>.md` with exclusive
create and moves `cv.active` to it in one rename, so the scorer reads the old
version or the new one and never an empty marker. Every save is a new version
and none is overwritten; a save that changes nothing writes nothing; a save
from a page opened on another version is refused. The upload is relabelled the
one-off import from a Word file and works as it did. Re-score stale items
leaves full description scores alone, see below.

The tailored copy. One per role, in the table `tailored_cvs` (`schema.sql`),
keyed on the role: `key:` and the role_key, so the adverts of a repost chain
share it, or `id:` and the row for a row with no key. It is made from the
master the first time the role's Prepare application page opens: drawing the
page writes nothing, and its script then makes POST /prepare/cv/open, a write
that asks no model anything and costs nothing. The answer carries the copy and
leaves the right of the page as it was drawn, which the Prepare application
click may be filling at that moment. A copy is made only from a master in the
editor's layout, because only then are its line numbers the ones the master's
own score cites; until the master has been saved on the CV page, the left of
the page shows it as it is stored, each line under the number the scorer gives
it, read only, says why, and links to the CV page. The copy is edited on the
left of the page; the role,
the score, the asks and the description are on the right. On a narrow screen
the two stack. Hovering an ask lights the CV lines it cites, and hovering a
line lights the asks that cite it, from the cited lines the working stores. A
cited line is found again in an edited copy by all its words, without the
heading or bullet marks a layout puts in front of it; the working keeps the
first 240 characters of a line with its citation and the whole of a longer
line in `long_lines`, so an edit past that point is seen too. A score the
Prepare application click made before this branch kept only those 240
characters, so such a line is found as the line that starts with them
(`cv_doc.cited_is_cut`, `data-cv-cut` for the script). An ask that was met or
partly is marked stale the moment a line it cited is gone, and the counts above
the asks count it as stale and not as met, in the page script as it is typed
as on the server; the tailored score says the copy was edited since it was
made. The page script goes by the text: an edit typed and undone again is no
edit, for that note, the Download hint, Applied and leaving the page. Start
again from
master (POST /prepare/cv/reset) puts the master back on the copy, and keeps
the saved copy and the scores. The copy is capped at 20,000 characters, 400
lines and 1,000 characters a line, and a CV with no name, or an entry with
lines and no heading, is refused.

Rescore role and Save CV send the version of the copy the page took, its
`cv_hash`. A copy changed in another tab or window since is not written over:
nothing is stored or spent, and the page says to reload (`COPY_MOVED`).

Rescore role (POST /prepare/cv/rescore) stores the text on screen as the copy,
then scores it against the stored description through `prepare_application._score`,
the same code as the Prepare application click: one call at `SPEC_MAX_TOKENS`
with no retry, the same failure windows (an unusable reply five minutes, a call
that raised twenty seconds), the same parking of a paid-for reply for half an
hour when the database is busy, the same 90 second wait for the write lock, and
a lock per role in the process, so a double click and a storm pay once. It also
takes the Prepare application click's lock for the advert, after the copy's
own, so a Rescore role pressed while the click is still scoring the master
waits for it and then finds its score; the click never takes the copy's lock,
so the two cannot wait on each other. The failure windows are remembered by
the copy and its version, so a failure on the copy never holds up the master's
click, nor the other way round. The scorer is given
`score_item.build_scorer_system` with the copy's text in place of the master's
and nothing else changed; the prompt is not changed. A pair of description and
copy is scored once, by `description_version` and `cv_store.cv_hash` (the hash
of the copy as numbered): a pair scored before, on the copy, in the history, or
as the master itself (whose working records its `cv_hash`), is shown again and
nothing is spent. A working the click stored before this branch has no
`cv_hash`; for one made on a dated master version that is still the active
one, the active master's hash stands in (`tailored_cv.scored_master_hash`), and
for any other nothing is assumed and the copy is scored. A copy that is the
master unchanged, for a role whose own score is not on this description yet,
is not scored by Rescore role: that is the role's own score, which the click
under the description makes once (`UNCHANGED_COPY`). The right side every copy
button hands back is drawn as a page opened then would be, without the
button's words, which go beside the editor, so a word that points to the
button under the description finds it there.

Where the tailored score goes. It belongs to the copy: it is stored on the copy
as JSON (`tailored_cvs.score`) with the score it replaced and the asks that
changed, and in `score_history` as a row with event `tailored`, writer
`prepare`, and the copy's hash in the new nullable column `cv_hash`. It is
never written to the role, because the Jobs page shows the role's score and that
must stay the master CV's. `archive_score` compares a role's score with the
role's last history row that is not tailored, so a tailored row never makes it
keep a score twice. Its cost is a `runs` row under `prepare`, naming the copy.

Save CV (POST /prepare/cv/save) stores the copy on screen as the saved copy,
named `CV - Luke Keogh - <Company> - <Job title>`, or `CV - Luke Keogh - <Job
title>` where the company is not known, made safe for a file name. A save
replaces the save before for that role, and only a save does. Download (GET
/prepare/<id>/cv.docx) builds a Word file from the saved copy with python-docx
(`scripts/cv_word.py`) from one fixed template: one column, no tables, text
boxes, images, fields, header or footer, one plain font in black on A4, the name
as Heading 1, sections as Heading 2, entries as Heading 3 and bullets as real
List Bullet paragraphs with their numbering on the paragraph. The route reads
only, is behind the same Host check as every page, and names the file by the
saved copy; the link carries `download`, so the browser fetches the file
without leaving the page and without asking about unsaved edits. The price is
that a refusal from the route, the 503 of a dashboard without python-docx
included, shows as a download that failed rather than as its words; deploy
step 8 checks python-docx is there. Applied
records the saved copy's name in `applied_cv`, in the same statement that
stamps the role, and records none where no copy was saved; a `cv` the request
names is still checked and is not recorded. Pressed while the copy on screen
is not the copy saved, Applied asks first, since once the role is applied
nothing more can be saved for it. Undo is as it was.

Nothing here touches an applied role, or one acknowledged, closed, dismissed or
expired: no copy is made, nothing is saved, reset or scored, every write looks
again under the write lock, and the page shows the copy that was saved for an
applied role, as it was sent, read only, with Download. The score beside it is
that copy's own (`tailored_cv.sent_standing`): the one the history keeps for
the shown text, not one made later on a text that was never saved, or a plain
word that it was not scored on its own. The words name the copy shown
(`build_dashboard.shown_copy_words`): the copy saved and sent, the copy saved
for a role that closed, or the copy as it was left, never saved.

python-docx is in `requirements.txt` on the 1.2 line. Only the dashboard
imports it, inside the download, so a dashboard without it serves every page and
answers the download with a 503 that says so (which the browser shows as a
failed download, see above), and n8n's Python never needs it.
The dashboard container installs `requirements.txt` when it starts.

This is a third first-open event: the first open of a database without them adds
the table and `score_history.cv_hash`, which takes the write lock for a moment.
Open the database by hand between inbox runs, as Brief 3's deploy does, before
`docker restart medtech-radar`. Rolling back is code only; the table and the
column stay and old code ignores them, though old code's Re-score stale items
would again reach spec rows. `test/run_cv_test.py` checks all of it, and
`docs/box-checks-brief-4.md` holds the deploy.

### The CV page final (design/cv/final, 15 Nov)

The page itself is `scripts/cv_screen.py`; `serve_dashboard.py` keeps thin calls
and the routes. The master is drawn as an A4 sheet of light paper (page_frame's
`.sheet-paper`, the approved exception to dark only) and edited in place one part
at a time: the head or a section is read until Enter or a click opens it, and
only the open part has fields. That is `cv_page.CV_EDITOR_JS` with `sheet` set in
its data; the Prepare page leaves it off and edits as before. Either way every CV
line is one `.cvl[data-n]` element numbered as `cv_store.cv_lines` numbers the
text, now inside `.cv-part` wrappers, which is the contract Prepare's margin
notes rely on. The editor gives the page `window.RadarCvEditor` (sections, order,
add, undo and redo of unsaved edits, the page estimate), and the page's own
script (`CV_SCREEN_JS`) draws the toolbar, Rearrange and Design dialogs, the
tabs, the keys and the notices on it.

One master with dated versions, as before. History views or compares an older
version (`/cv?view=`, `/cv?compare=`) and can save one as the next version. The
Word import shows the file laid out the editor's way with its guesses named
(`cv_screen.import_report`) and saves that laid-out text (`cv_store.confirm`
with `laid_out`). Design (`cv_store.read_design`, `config/profile/cv.design.json`)
sets the font, size, spacing and margins of the sheet and of the Word file
(`cv_word.build`); the text, and so the scorer, do not change. `cv_doc.
estimate_pages` estimates where Word would break the A4 pages from the
template's sizes, and `CV_MODEL_JS`'s `pages` does the same sums in the browser;
past two pages the page says where page 3 starts, and saving is still allowed.

Preferences are edited on the page and written to the live preferences file only
(`radar_common.write_scoring_prefs` and `write_pref_section`), each write
changing its own lines and leaving every other byte as it was. A change to the
day-rate or salary floor, the CV pass mark or the Filtered floor is a re-sort in
code, for free: `regate_rates.regate` with `cv_gate_again`, which judges the CV
gate again from the stored CV match. That is exact because since phase three the
CV gate has only ever been code's (`score_item.cv_gate`); the rate gate is
code's on every role, a stored figure judged against its floor and no figure not
stated, and the floor reaches `derive_tier`. What cannot be worked out again is
counted on the page: roles with a number in the pay text but no figure Radar can
judge (`rate_unclear`, which read not stated), roles never gated, and the Location
and Sector gates, which the model judges from the prose. Roles never
gated have no tier, so no re-sort and no run on the page reaches them; the page
says so, with their cost, and names `scripts/rescore.py --regate`, run by hand on
the box, as what scores them. A re-sort keeps each replaced score in
`score_history` (writer `cv_preferences`) and logs one `regate` run, and applied
roles keep their tier. A CV gate the re-sort flips gets code's note
(`regate_rates.cv_gate_note`, "CV match 64 against the pass mark of 70, below,
judged in code."), so a fail mark never sits beside the scorer's words that it
passed; `gate_cv_note` is one of the columns `regate` writes. A database too busy
to re-sort leaves the file saved and the roles as they were (`resorted` false),
and the page keeps a Re-sort now that sends the same values again. A one-line
setting (title, keywords, first name, `/profile/setting`, `update_pref_line`) is
written the same atomic way, and the floors, the pass mark and the Filtered floor
are not one-line settings, so they never change without a re-sort. An inbox run
reads the floors and the thresholds again once it holds its write lock, for the
whole email, so a save landing mid-run is either read by that run or re-sorts
its roles after it.

A location or sector change offers a paid rescore instead, and a master change
flags the roles still scored on an older version. Rescore affected
(`rescore.affected`, `rescore.rescore_affected`, `cv_screen.start_rescore`) is
one confirmed run over every affected role, not 25 a press, in a thread of the
dashboard that shares Check now's lock, on the card scorer; acknowledged,
applied and full-description roles are left out and counted. The count and the
cost are said first (`cv_screen.rescore_cost`: the cost note's 1.1 to 1.8 cents a
card score, widened by what live rescores on file have cost a role, priced as
Home prices the week, cache writes at 1.25 times input), the confirm sends the
count it stated and a hash of the roles it named (`rescore.ids_hash`), and a run
whose count has grown, or whose roles are another set, is refused. A paid reply
the database is too busy to take (an inbox run holds the lock for a whole email)
is held and written again after a pause (`rescore.BUSY_TRIES`, `BUSY_PAUSE`);
one that still cannot be written is counted as not stored, said so on the page,
and the run goes on. The run's row in `runs` is written however the run ends,
with the tokens spent so far, and the running line says the cost so far. Every
call's cache-write tokens are recorded (`runs.cache_write_tokens`, added by
`_migrate`). `test/run_cv_final_test.py` and
`test/cv_screen_harness.js` check it, and `docs/box-checks-prepare-cv-final.md` holds
the deploy.

## Quoted reasons

Every score now carries up to four reasons, each with the words of the advert it
rests on. `prompts/scorer.md` asks for them in the card's part of the rubric, so
a card and a full description are both asked: `reasons`, a list of up to four,
each a reason of one plain line of 20 words or fewer and a quote of 25 words or
fewer, copied character for character from the advert and never from the CV or
the preferences file. The reasons come beside the gate results, and the voice
rules apply to each reason and never to its quote. Below the marker, a full
description's quotes come from the description and never from the card fields
beside it. Nothing else the prompt asks for changed, and its version moved from
`97f11cce` to `22353628`.

The code holds every quote to the text that was scored, by the rule the pay line
uses, `radar_common.find_verbatim`: a quote is kept only where it is found word
for word, with case ignored and every run of white space on either side counted
as one space, and what is kept is that text's own characters, so a page shows
the advert's words and never the model's. A reason's quote must also begin and
end on the edges of words (`whole_words`), so a word or a figure cut short is
not kept: "£65" is not found in "£650 per day", nor "Senior Software Engineer"
in "Senior Software Engineering Manager". On a card the text is the four fields
the scorer was given (`score_item.quote_sources`), the title, company, location
and pay text, each searched on its own, so a quote cannot run from one field
into the next. The inbox stores the title, company and location as the scorer's
reply named them, which can differ from the card, so it holds each quote kept
from the card again to the four fields the row stores, and a quote stays only
where both hold it (`score_item.hold_reasons`). On a re-score the four fields
sent are the row's own. On a full description the text is the stored
description, `description_text`, the one the score is made on. A quote that is
empty or has no letter or digit in it, is not text, has more than 25 words or is
not found is dropped, its reason is kept, and `quote_dropped` on the reason says
which (`empty`, `not_text`, `too_long`, `not_found`). At most four reasons are
kept, each run through the voice rules and cut at 240 characters. A reason given
as a bare string is kept with no quote, anything else in the list is left out
and counted, and a reasons field that is not a list keeps none and counts one.
No shape of reply makes the check raise, and should it ever, the score keeps no
reasons: reasons never cost a score (`score_item.normalise_reasons` and
`reasons_json`). A lone surrogate, which a reply can carry as a JSON escape and
SQLite cannot store, becomes the replacement character in every free-text field,
the reasons among them (`radar_common.sanitise_free_text`), so it never costs
the advert it came with.

What is stored. The JSON `{"v": 1, "scored_at", "basis", "reasons": [{"reason",
"quote", "quote_dropped"}], "dropped", "gates"}` goes in
`opportunities.score_reasons`, written in the statement that writes the score it
belongs to, by the inbox
(`process_email.insert_opportunity`), the re-score (`rescore.py`, every pass)
and the Prepare application click, and in `score_history.reasons` with every
score the history keeps, so the history keeps thirteen fields of a score. A
tailored copy's score keeps its reasons in its record on `tailored_cvs.score`
and on its `tailored` history row, and a score found again in the history keeps
them too. The reasons carry the date of their score, and a page shows them, and
`archive_score` files them, only beside a score of that date
(`radar_common.reasons_record`), so a writer that replaces a score and knows
nothing of them, old code after a rollback among them, cannot leave old reasons
looking current. Both columns are nullable with no default and arrive through
`_MIGRATIONS`. Nothing fills them for a score already made: no migration writes
them and nothing is re-scored to give a score reasons, so every row and history
row from before stays NULL and shows none. A re-tier by code, the regate or the
page checker's, keeps the reasons where they are, because the answer they came
with is unchanged. Their record carries the four gates of the score they were
given with, so where code has judged the gates again since, the Jobs row and the
Prepare application page show the reasons with one line that says so
(`radar_common.reasons_gates_moved`), and the history files them with those
gates inside them.

The ceilings. A card's reply had the extractor's 1,024 tokens. The worst card
reply the prompt now allows is 3,583 characters, the gates and notes at the
lengths the full description's count uses (1,842) and four reasons of 20 words
with quotes of 25 (1,741), about 900 tokens at four characters a token and 1,194
at three, which is over 1,024. So a card's score has its own `CARD_MAX_TOKENS`,
2,048, where the pessimistic count is 58 per cent of the ceiling, and the
extractor and the pay backfill keep 1,024. A full description's ceiling was
6,144, was 8,192 with the advert sentences, and is 10,240 since the asks'
short reasons, see Prepare application. `docs/cost-note.md` prices the reasons: about
180 output tokens for four typical reasons and 160 to 190 tokens of prompt,
about 0.3 cents a score on either path.

Where they show. The expanded Jobs row lists the reasons under its gates, and the
Prepare application page under the master CV's score and under the tailored
copy's: each reason as a sentence with its quote after it in quotation marks,
everything escaped, as plain text for now. A reason whose quote was dropped is
its words alone, and nothing more is said about it. A full description score's
quotes are not drawn once the page checker has stored another description, which
may not hold them, as the asks and the pay line are not; the reasons are, with
one line that says why (`build_dashboard.quotes_hold`). No other page draws them.

This is a fourth first-open event, after Briefs 2, 3 and 4: the first open of a
database without the two columns adds them, which takes the write lock for a
moment. Open the database by hand between inbox runs, before `docker restart
medtech-radar`, as the earlier deploys do. n8n reads the new prompt and scripts
on its next run. The prompt is read from disk each time a scorer is set up, and
code already loaded keeps running, so between the pull and the restart the
dashboard, and an inbox run already under way, would score with the new prompt
and old code: such a score is stamped `22353628` and stores no reasons, and a
full description scored that way is not scored again. So the pull waits for
nothing to be running, the pull, the open and the restart run back to back, and
no Prepare application or Rescore role is pressed until the restart is done.
Rolling back is code only; the columns and every reason stay and old code
ignores them. `test/run_quotes_test.py` checks all of it, and
`docs/box-checks-scorer-quotes.md` holds the deploy.

## The renderer, and the one page to trust

`serve_dashboard.py` is canonical. It re-renders from the database on every load
and takes the writes. `build_dashboard.py` is the rendering library it imports,
and its command line still writes a static archive page but prints a deprecation
note when it does. A file written this morning is a photograph of a database
that has moved on, and two surfaces disagreeing is worse than having one.

`GET /health` returns JSON for something else to watch, last inbox run, last
signals run, database reachability and row counts, and under `jobs` a verdict
for each scheduled job, with a 200 when fresh and a 503 when the database is
unreachable or a job named in `health_jobs` has gone stale. Each job has its own
limits in `radar_common.JOB_LIMITS`, because the jobs do not run alike: the
inbox writes a row only when an email arrives, so it is stale after a day with
none, the signal watcher after six hours, the advert checker after two days and
the digest after eight. Home and Radar's health draw their lights from the same verdicts. The
watcher and the advert checker write their runs row however a run ends, with an
outcome of ok, partial or failed, so a job that keeps failing shows as failing
rather than as silence. It exists so that silence means quiet rather than
broken, because nobody opens a dashboard at three in the morning and a pipeline
that stopped at midnight looks exactly like one with nothing to say.

## The fallback

If putting Python next to n8n ever becomes a nuisance, the scripts do not need n8n at all. Run `process_email.py`, `check_signals.py` and `build_digest.py` by cron on any box that has the repo and the key. In that setup n8n keeps only the jobs that genuinely need its credentials, reading Gmail and sending the digest email. Everything else is just Python, SQLite and a schedule.
