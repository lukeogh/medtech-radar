# MedTech Radar

A personal opportunity pipeline. Radar watches the places where new medical
device and IVD work first becomes visible, scores everything it finds against
a CV and a preferences file, and delivers a Monday digest plus an immediate
push when a signal is hot enough to act on the same day. It exists so that
opportunities find Luke instead of the other way round.

Nothing sends, posts or pushes until it is armed by hand. Dry run is the
default everywhere.

## How it works.

Email is the universal ingestion bus. Job alerts from LinkedIn, Reed, Indeed
and CV-Library, Google Alerts and Dealroom notifications all land in one
aggregator inbox. An n8n workflow drains that inbox, splits each mail into
individual opportunities, extracts fields with a fast Claude model, dedupes on
URL hash against SQLite, scores new items with a stronger model against the CV
and the preferences file, and stores the results. A second workflow watches
the upstream sources that have no email route, RSS where a feed exists and
page diff where it does not, and pushes genuine fast signals to ntfy
immediately, because those are the ones where hours matter. A third workflow
builds and sends the Monday digest from the database. A playbook says exactly
what to do when a signal fires.

n8n is a thin trigger and IO shell. All logic lives in versioned Python
scripts under scripts/, which the workflows call through Execute Command
nodes. That keeps one source of truth, and it means the acceptance tests
exercise the exact code the workflows run, so nothing drifts between the n8n
JSON and the scripts. The Claude API key lives in .env on whichever machine
runs the scripts. Gmail OAuth and the actual email sending stay inside n8n
and its credential store.

## Repo layout.

```
medtech-radar/
  README.md
  .env.example            # copy to .env, fill in, never commit
  requirements.txt
  config/
    radar.yaml            # thresholds, models, schedule. Luke edits, everything reads
    watchlist.yaml        # the watched sources, tiered
    preferences.template.md
    profile/              # gitignored. CV and live preferences go here
  scripts/                # all the logic. n8n calls these via Execute Command
  workflows/              # n8n workflow JSON exports, import then arm
  prompts/                # versioned Claude prompts
  db/                     # schema.sql, radar.sqlite created on first run
  playbook/               # announcement-day sequence and the touch tracker
  drafts/                 # article skeleton, gated until anonymisation clears
  test/                   # samples, runners, run_all.sh, dry-run outputs
  docs/                   # architecture note, cost note, upgrade paths
```

## Quick start.

1. Install dependencies with `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill it in. On Linux hosts run
   `chmod 600 .env`.
3. Drop the CV into `config/profile/` as `cv.txt`, or as `cv.pdf` with pypdf
   installed (`pip install pypdf`). Without either, scripts fall back to a
   fixture profile and warn loudly.
4. Copy `config/preferences.template.md` to `config/profile/preferences.md`
   and edit it. It ships with working defaults.
5. Check `config/radar.yaml`. Lines marked TODO were inferred during the
   build and need confirming.

## Google Alerts to create.

Create these at google.com/alerts, delivered as email to the aggregator
inbox, as-it-happens:

- "imec spin-off"
- "Ghent University spin-off"
- "KU Leuven spin-off"
- "IVD seed funding"
- "medical device software funding"
- "photonics diagnostics funding"

Add Dealroom spinout and funding alerts for imec, Ghent and KU Leuven to the
same inbox.

## Morning checklist. Arming the system.

Work through this in order. Everything ships dry run and inactive, so nothing
fires until the step that arms it.

1. Skim the build log at the bottom of this file.
2. Get the repo onto the n8n host, cloned there or mounted into the
   container, with python3, the pip dependencies and a filled `.env` on that
   host. The workflows shell out to the scripts locally.
3. Run `test/run_all.sh`. Everything green before touching n8n.
4. Read `test/last_digest.html`. Carry on only if it reads well.
5. Import the three workflow JSONs from `workflows/` into n8n. They import
   inactive. Point each workflow's Config node at the repo path and python
   binary on that host.
6. Connect the Gmail credential on the trigger and send nodes, and create
   the `radar-processed` label in the aggregator inbox once.
7. Trigger radar-inbox manually against the real inbox and sanity-check the
   scores in SQLite before activating it.
8. In radar-digest, enable the Gmail send node. It ships disabled on
   purpose.
9. In radar-signals, switch `check_signals.py` from `--dry-run` to `--push`,
   spot-check two watchlist sources with `--source`, and send one test ntfy
   push to the phone.
10. Read `playbook/announcement-day.md` once, so the first real signal is
    executed rather than improvised.

## Playbook and drafts.

`playbook/announcement-day.md` is the exact sequence for the day a signal
fires. `playbook/tracker.md` explains where touches get logged, with
`scripts/touch.py` as the one-command logger. `drafts/` holds the
compliance-cost article skeleton, which stays unpublished until every
anonymisation marker in it is signed off.

## Ground rules.

- Build, never act. Nothing sends without being armed by hand.
- Secrets live in `.env` only. The CV and preferences never leave this
  machine, and `config/profile/` is gitignored.
- No LinkedIn scraping. LinkedIn data arrives by email alert only.
- Polite fetching. robots.txt respected, one request per source per check,
  six hours minimum between checks of any source.
- Idempotent. Re-runs never duplicate records. The dedupe key is a hash of
  the normalised URL.
- Cost guardrails. Claude calls are batched and logged, and nothing already
  in the database is ever scored twice.

## Build log

Written at the end of the overnight build, in the early hours of 21 July 2026.
Plain reporting.

### What was built

The shared foundation first. db/schema.sql, scripts/radar_common.py (config,
secrets, dedupe hashing, Claude calls with prompt caching, polite fetching,
dry-run ntfy) and config/radar.yaml with the unfilled CONFIG values inferred
and marked TODO.

Workstream 1, feeds and matching. prompts/extractor.md and prompts/scorer.md.
scripts/process_email.py, scripts/score_item.py and scripts/build_digest.py.
A preferences template at config/preferences.template.md with a working copy
in config/profile/. Five fabricated sample alert emails, deterministic mocks
and two test runners. workflows/radar-inbox.json and radar-digest.json, both
importable and inactive, with the Gmail send node shipped disabled.

Workstream 2, signals and watchlist. All 21 watchlist sources were fetched and
checked live overnight. Twenty verified, new-electronics blocks plain fetches
and is marked unverified. Four RSS feeds found, the rest run on page diff.
config/watchlist.yaml, prompts/signal-scorer.md, scripts/check_signals.py,
three fabricated announcements with a test runner, docs/upgrade-paths.md and
workflows/radar-signals.json.

Workstream 3, playbook and drafts. playbook/announcement-day.md with templates
and two worked examples, playbook/tracker.md with scripts/touch.py, and
drafts/compliance-cost-article-skeleton.md with a draft introduction and
anonymisation markers throughout. Plus docs/architecture.md and
docs/cost-note.md.

A four-way review then fixed twenty findings across voice, n8n structure,
brief compliance and safety. The notable fixes were a multi-email polling bug
in the inbox workflow, an oversized argument risk on large alert emails, the
ntfy helper now dry-running when a caller forgets the argument, failed
extractions no longer marking an email processed, and the digest gaining a
threads awaiting action section fed by the tracker.

### What passed

bash test/run_all.sh is green end to end and stays green on a re-run. Five
sample emails extract and score to valid JSON. The deliberate duplicate is
stored once. The dry-run digest renders with Inbound, Signals, Ageing, threads
awaiting action and the stats line. The three announcements rank perfect above
marginal above irrelevant, and only the perfect one produces a ntfy payload
carrying the company, what happened, why it matters and the step to take
today.

All of that ran in mock mode because no API key exists on this machine
tonight. The runners switch to live scoring on their own once .env holds
ANTHROPIC_API_KEY. That live rerun is the first morning step.

### Needs your judgement in the morning

1. Fill .env and rerun bash test/run_all.sh live before touching n8n. Mock
   scores are heuristic, so sanity-check the live numbers against your own
   judgement before trusting the threshold of 70.
2. config/radar.yaml carries TODOs. The aggregator address, the digest
   address, and the ntfy URL, which is a guess at ntfy.keogh.cloud.
3. Drop the CV into config/profile/ as cv.txt, or cv.pdf after
   pip install pypdf. Until then scoring uses a fixture profile built from
   the knowledge pack and warns on every run.
4. Set the real day-rate floor in config/profile/preferences.md. The scorer
   needs a number to judge wrong-rate roles against.
5. imec-press renders its news list with JavaScript, so the plain diff
   watcher may stay blind on the single most important origin source. Tier 2
   and the email alerts cover the gap for now. changedetection.io is the
   upgrade, see docs/upgrade-paths.md.
6. white-fund has a valid but currently empty RSS feed. new-electronics
   blocks plain fetches. Spot-check both with
   python scripts/check_signals.py --source white-fund after arming.
7. The digest commit step re-collects rather than committing the exact built
   set, so an item arriving in the seconds between build and send could be
   marked digested without appearing in the email. Closing it is a small
   design change waiting on your nod.
8. Separate from this repo. Claude_Code_N8n/wf_current.json on this machine
   contains a hardcoded Anthropic API key and a Firefly III token, and the
   .claude/settings.local.json there embeds your n8n API key. Rotate all
   three and move them into credential stores.
