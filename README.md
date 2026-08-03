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
6. To view the dashboard in a browser, run
   `python scripts/serve_dashboard.py` and open http://127.0.0.1:8787. The
   page re-renders from the database on every load, the Refresh button
   reloads it, Check now runs the signals watcher on demand, and it reloads
   itself every fifteen minutes while n8n keeps feeding the database on its
   own schedule. `python scripts/build_dashboard.py` still writes the plain
   static file. Both are read only against the database. If you serve it
   beyond this machine, put it behind the reverse proxy with authentication,
   the page carries scored opportunities and names.
7. When a thread is handled or has gone nowhere, retire it so the ageing
   section stops nagging, for example
   `python scripts/touch.py mark "Cantilex Dx" --as actioned`. Use
   `--as dead` for the ones that died.
8. If the digest or dashboard shows a Needs review section, something
   failed scoring. Run `python scripts/rescore.py` to re-score it in
   place once the cause is fixed.

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
   host. The workflows shell out to the scripts locally. Then copy your CV
   and preferences into `config/profile/` on that host by hand. Both are
   gitignored, so a clone arrives without them, and live scoring refuses to
   run until the CV is there.
3. Run `test/run_all.sh`. Everything green before touching n8n.
4. Read `test/last_digest.html`. Carry on only if it reads well.
5. Import the three workflow JSONs from `workflows/` into n8n. They import
   inactive. Point each workflow's Config node at the repo path and python
   binary on that host.
6. Connect the Gmail credential on the trigger and send nodes, and create
   the `radar-processed` and `radar-failed` labels in the aggregator inbox
   once. Put both label ids into the two label nodes in radar-inbox.
7. Trigger radar-inbox manually against the real inbox and sanity-check the
   scores in SQLite before activating it.
8. Arm radar-digest as one action. Enable the Send Digest node and the Mark
   Digested node together, never one without the other. Both ship disabled
   on purpose. Send without commit resends the same digest every Monday,
   because nothing marks the items digested. Commit alone can never fire,
   it is gated on Gmail returning a sent message id, but a split arming
   invites confusion. While you are in there, confirm the n8n instance
   timezone is Europe/London or adjust the cron expression. n8n runs cron
   in the instance timezone, set `GENERIC_TIMEZONE=Europe/London` on the
   host.
9. In radar-signals, switch `check_signals.py` from `--dry-run` to `--push`,
   spot-check two watchlist sources with `--source`, and send one test ntfy
   push to the phone.
10. Read `playbook/announcement-day.md` once, so the first real signal is
    executed rather than improvised.

## The traffic lights, and the flow when one goes red.

The Jobs page's metrics are instruments with four states. Green with a
pulse means flowing, the inbox ran within two hours or the board emailed
within two days. Amber means late or quiet, two to six hours for the
inbox, up to a week for a board, watch it, don't chase it. Red means
stalled or silent, over six hours for the inbox, over a week for a board
that used to send. A hollow dot is a board never heard from, waiting on a
subscription, not broken.

Inbox red, the flow. Open n8n, Executions, Radar Inbox. Three findings,
three fixes. Deactivated, reactivate it. Failing, open the execution and
read the failing node, Gmail auth errors mean the OAuth credential needs
re-signing, Execute Command errors mean the mount or the checkout. No
executions at all with the workflow active, the trigger itself, check the
n8n instance is up and its logs. If executions look green but the light
stays red, the dashboard is reading a different database file than n8n
writes, check the container mount.

Board red, the flow. The radar is fine, the supply died. Check the alert
settings under the medtechradar account on the board itself, then the
aggregator's spam folder. LinkedIn has one extra link, it arrives through
the filter-forward from the personal Gmail, so confirm that filter still
exists and forwarding is still verified there.

Failed emails red. Poison messages hit the three-strike cap and were
shelved under radar-failed. The digest names them, read one in the
aggregator inbox to see why extraction hates it.

## Hosting.

The repo lives in a private GitHub repository at
github.com/lukeogh/medtech-radar, so the laptop's C drive is no longer the
single point of failure. Clone it onto the n8n host with
`gh repo clone lukeogh/medtech-radar` or over https with a token. Two things
never travel with a clone. `.env` holds the API key and `config/profile/`
holds the CV and preferences, both gitignored, both copied across by hand.
The history was scanned for secret material before the first push and is
clean. Keep it that way, secrets go in `.env` and nowhere else.

The dashboard server is part of the deployment now, not a viewer bolted
on. It takes the acknowledge clicks and CV uploads, so wherever it runs
it needs write access to `db/` and `config/profile/`, and its bind
address lives in radar.yaml as `dashboard_host` and `dashboard_port`.
After pulling a version with new columns, run
`python scripts/backfill_pay.py` until it reports nothing remaining, one
fast-model pass per stored row, capped at a hundred per run.

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

### Entry two. The hardening night, 28 July 2026.

A full external review of the first build turned into sixteen ordered
tasks. All sixteen are done. Plain reporting.

What changed. Line endings are pinned to LF so shebangs survive the Linux
host. The signal scorer can no longer pitch in first contact, and both test
runners now enforce that doctrine in mock and live alike. Scoring runs at
temperature zero, so the same advert scores the same on any day. The digest
gained build tokens, so the commit step marks exactly the emailed items and
a late arrival falls into next week instead of vanishing, plus an honest
send gate that counts ageing, threads and needs-review items, and a
quiet-week email so silence can only mean breakage. Items can die now,
touch.py mark retires threads from the ageing nag. Live scoring refuses to
run against the fixture CV. The inbox trigger keys on labels rather than
read state, and a poison email costs three attempts before it is shelved
under radar-failed. Failed scorings surface in a Needs review section in
the digest and dashboard, cleared by the new rescore.py. Diff signals fetch
and score real article text instead of a headline. SQLite runs in WAL mode.
A failed ntfy push is retried by a catch-up sweep inside 24 hours. Test
runners pin their mode so a stale shell variable cannot flip a run. And the
repo now lives in a private GitHub remote, history scanned clean before the
first push.

What passed. bash test/run_all.sh, all three runners, in mock mode, after
every one of the sixteen tasks and once more after the final merge. The
review's own regression, the mixed live and mock runs of 28 July, is now a
test that fails if it ever happens again.

What still needs you. Rotate the Anthropic key, it was treated as
compromised all night and no live call was made with it. Re-import all
three workflow JSONs into n8n, they changed on disk. Copy the CV and
preferences into config/profile/ on the n8n host. Create both Gmail
labels. Confirm the ntfy URL and the n8n timezone. Then run the suite live
with the fresh key and read the previews before arming anything. The full
ordered list is in MORNING_REPORT.md.

29 July 2026. Three fixes from the first live run. Failure-injection tests
now force mock, the digest renders NULL fields instead of crashing, the
runners fail readably, and the doctrine test allows the playbook's own
peer gesture while catching pitch shapes. Suite green twice, mock and
live.

30 July 2026. The free-text style rules are mechanical now. The prompt
repeats the punctuation rules inside the suggested_action field spec, and
a shared sanitiser in radar_common cleans every free-text field in both
pipelines, em dashes to commas, semicolons to full stops with the next
letter capitalised. The voice tests enforce a guarantee rather than a
hope. Suite green twice, mock and live.

30 July 2026, later. The dashboard now implements the First Light design
system's kit. Standing line, soft-tinted section papers, one row grammar
with detail on expand, watchlist grouped by tier, the heartbeat at footer
weight, and an honest as-delivered state for a database that has never
run. The meters, tiles and pipeline table are gone, as the kit specifies.

29 July 2026, later still. The dashboard is servable. serve_dashboard.py
re-renders the page from the database on every browser load, adds Refresh
and Check now buttons in the masthead, and reloads itself every fifteen
minutes. Check now runs the watcher through its own politeness gate. The
static file generator is unchanged and both stay read only.

29 July 2026, evening. The system went live. All three workflows are armed
on the homelab, inbox hourly, digest Monday 07:30, signals every two hours
with a real push, and the test push travelled the production path to the
phone. The email plumbing is real too. Personal Gmail was found forwarding
everything to the aggregator, that is off, replaced by one precise filter,
jobalerts-noreply@linkedin.com forwards and nothing else does. The old
LinkedIn filter had matched zero emails ever. Six Google Alerts stand under
the aggregator account, the README's set with tighter phrasing, one
duplicate deleted. The first real roles arrive with the next LinkedIn
alert. Deployment learning folded into docs/architecture.md, n8n 2.x
disables Execute Command in code and the fix is a one-line NODES_EXCLUDE
that keeps localFileTrigger dark.

30 July 2026. Pay left the prose and became a column. The fast model
extracts currency, period and amounts, code converts and bands them
against the one machine-readable floor line in the preferences file, and
the scorer stopped talking about money, which took two prompt rounds
because its own worked example was a pay sentence. Above, Close, Below or
Unstated on every dashboard row and digest item, one legend each, the page
widened to use its width. Acknowledge arrived with it. One click and a row
leaves the view, the digest and the ageing nag, never the database, so
dedupe keeps rejecting what has been seen. touch.py mark folded into the
same stamp and old actioned and dead rows were mapped across in an
idempotent migration. The CV became versioned, uploaded through the served
page as md, txt, docx or pdf, previewed, confirmed, dated, never
overwritten, with an active marker the scorer reads and a version stamp on
every score, plus a capped re-score for rows a CV change left stale. The
sanitiser closed its last two gaps, loose colons and exclamation marks.
The First Light prototype regenerated from the real renderer with demo
data, a deliberate divergence from the original kit, the Rate column,
legend and acknowledge grammar are design now. A two-round adversarial
review of the branch caught what the suite had not, an undo that a
reconnect silently reverted, a write lock held across live model calls,
a zip bomb path, a same-second upload race, and each catch now has a
fix and a test. Suite green twice after all of it, mock and live, four
runners including the new unit fixtures.

30 July 2026, later. Signals earned a front page. An Insights tab sits at
the top of the served pages and reads like a newspaper, the highest fresh
signal as the lead with its do-today step boxed, the rest as story cards
under Also fresh and Earlier, widgets carrying the week in numbers, what
reached the phone and coverage. The Watchlist health table left the served
archive page and now sits collapsed at the bottom of Insights as Sources,
open it when you want provenance, ignore it when you want news. The static
single-page fallback keeps everything as it was, and the design project
gained the insights page alongside the regenerated dashboard so the
reference has both.

30 July 2026, later still. The insights act now. The playbook box reads
Suggestion, and each story carries I did it and Dismiss. I did it flips
the signal to actioned and logs the touch against the company, channel
read off the suggestion's own words, so the tracker, the threads section
and the digest all know the first contact happened, and any later story
about a touched company says so on its card, you last touched this
company on the date via the channel. Dismiss follows the jobs rule to
the letter, acknowledged_at on the signal, out of the page, the digest,
the ageing nag and the catch-up pushes, never out of the database, so
the URL-hash dedupe keeps a dismissed insight from ever resurfacing.
Undo lives behind the Dismissed fold. Suite green with the loop pinned.

30 July 2026, evening. Jobs earned a tab and the archive became Home.
Three tabs now, Home, Jobs, Insights. The Jobs page opens on a trust
strip, the machine's last run, the week's flow, and each board's
freshness, then Top prospects, then every board in its own section,
LinkedIn, Reed, Indeed, CV-Library and Other email alerts, an empty
board saying subscribe rather than hiding. Job sources are configurable
now. The built-in four live in code, customs live in
config/job_sources.yaml, written by the page's own add-a-source form and
read by detect_source, so a new board's emails file under its own name
the moment they start arriving. The form says the honest second half out
loud, subscribing the aggregator inbox on the board is still yours to
do. Suite green, the registry, the tagging and the endpoints pinned.

30 July 2026, last touch. The boards got faces. Each Jobs section heading
carries a small brand-coloured badge tile, the board's name, and the
box-with-arrow glyph opening the board's site in a new tab, inline SVG
throughout because the page stays self-contained. The add-a-source form
gained an optional site field so a custom board earns the same arrow.
The metrics went visual the same evening. Traffic lights on the machine
and on every board, green pulsing when the flow is alive, amber for quiet,
red for stalled or silent, a hollow waiting dot for boards not yet
subscribed, judged against honest thresholds, two days for a board, the
hourly schedule for the inbox. A fourteen-day arrival chart sits between
them so the flow is seen, not asserted. Trust by instrumentation.

31 July 2026. Home became the entrance. Three cards carry their icons and
their live counts, Jobs, Insights, CV, each the door to its page. Under
them the walking-past answers, the sync light, the week's new jobs, top
prospects charted week on week with a month-on-month line beneath, the
market's give and take readable at a glance, and a twelve-week line of
insight activity for the year's rhythm, peaks announce, troughs say
build. The scoring panel puts the machine-readable preference lines on
the page, title, day-rate floor and keywords, edited surgically in the
preferences file itself so the scorer's next system prompt carries them,
prose never touched. A Needs-you section appears only when something
needs him. The old archive page kept every row and moved to /archive,
linked from the footer. The working tables live where the work lives,
Home just tells the truth quickly.

1 August 2026. The buying window stopped being doctrine and became
machinery. Every advert that arrives is now matched in code against the
touch log, and one from a company already spoken to is flagged on the
row and written up as its own insight, source id job-advert, relevance
90, the playbook's step four carried on its back. The match is a fact
about our own history, so it is decided at store time and never by a
model, and a job score has no vote. A generic role at a touched
seed-stage company used to score low on want match by design and sink
without trace. It now surfaces as what it actually is, the week IEC
62304 and ISO 13485 stop being abstract for them. One window per company
ever, checked against the job-advert signals themselves so a restore
from backup cannot re-open a conversation, and idempotent on the
advert's own url hash. Nothing here pushes. The catch-up sweep already
collects any new unacknowledged signal above the fast bar within a day,
so arming semantics did not move.

Week three books itself the same day. Marking an insight done through
the page already logged the touch, and now the announcement-day channels
book the follow-up too, twenty-one days out with the action named. Only
comment and connection-note book anything. An engagement already is week
three, and an artefact send is the buying-window move whose next event
belongs to them, a reply or silence, both fine, so booking a date on
either would invent the chase the playbook forbids. The Insights card
carries the booking under the last touch, and Home counts what has come
due, because a thread waiting and a thread late are not the same thread.

31 July 2026, later. The insight line stretched to a rolling year, months
as buckets, and learned two kinds of honesty. Only signals at or above
the digest bar count now, the rubric's own judgement of a medtech event
that could need input, so the trend charts the market that matters
rather than ecosystem chatter. And months before the earliest signal on
file draw nothing at all, with a note naming when recording began,
because a gap in the radar's life must never read as a quiet market. The
annual shape earns itself month by month from here.

2 August 2026. The dashboard stopped answering the internet. radar.yaml has
said since the day it was written that this page is LAN or tailnet only by
design, and it was not. Nginx Proxy Manager served it on a hostname whose
ports 80 and 443 are open on the router, with no access list, no
authentication and nothing between a stranger and every scored opportunity,
every company name, the CV upload at /cv and the endpoints that spend
Anthropic tokens. Proved rather than assumed. A request to the WAN address
carrying the right host header returned 200 on every page, and nginx logged
it against the public IP, which is exactly what an outside client looks
like. The fix is an allow list on the proxy host, LAN and both Tailscale
ranges and nothing else, scoped to location / so the ACME challenge path is
untouched and the wildcard certificate keeps renewing on its DNS challenge.
After it, the same WAN request gets 403 on every page and on /ack and
/unack, and the LAN still gets 200. The denied writes changed no rows.

The scan that found it found more, and none of the rest was touched. Twenty
five proxy hosts, every one enabled, every one with no access list, and no
access lists defined at all. Among them the proxy's own admin page, the
hypervisor console, the password vault, the DNS server and an n8n that can
run shell next to the API key. That list is in MORNING_REPORT.md with a
decision to make, because closing another man's door without asking is how
you lock him out of his own house. Public DNS for the domain also points at
an address this network no longer holds, which is the only reason the open
door was not being walked through, and correcting it before the doors are
shut would make the exposure immediate rather than theoretical.

gitleaks over all fifty seven commits found one real secret, an Anthropic
key committed to .env.example on 29 July and removed from the working tree
the same day. History keeps it. The key in that commit is not the key in
use, compared by fingerprint without either value being printed, so the
rotation already happened and what remains is revoking the old one. Nothing
was fixed silently and nothing was rewritten.

2 August 2026, later. The rest of the doors were shut. Twenty five hostnames
were being served to the open internet with no access list and no
authentication, and after the dashboard came the proxy's own admin page, the
hypervisor console, the vault, the DNS server, Home Assistant and two vhosts
carrying personal financial records that the earlier count had missed
entirely, because that count read NPM's database and the database is not the
authority, the generated config is. Two rows in it have no conf file and were
never served at all. Twenty four of the twenty five now refuse anything that
is not the LAN or the tailnet. ntfy stays public on purpose, deny-all with
token auth, and the phone needs it.

The first attempt was an allow list per host and it left jellyfin open. A
return inside a location runs in the rewrite phase, before the access phase
where allow and deny are read, so the redirect at location = / answered
strangers happily while the allow list sat underneath it doing nothing. The
fix refuses in the same phase the redirect lives in. A geo block names who is
local, a map names the exceptions in one readable place, and a two line
include that NPM already writes into every server block it generates does the
refusing. That last detail is the point. A proxy host created next month
inherits the deny without anyone remembering to apply it, which is the only
kind of rule that survives a busy week.

One path stays open from the WAN by choice. Two onlyoffice certificates still
renew over HTTP-01, so the ACME challenge directory is exempt and was proved
still reachable. Move those two to the DNS challenge and the exemption can go.
Nothing else was touched, LAN access was checked host by host afterwards, and
the two that answer 403 there were answering 403 before, from the applications
themselves, proved by going straight to their backends with the proxy out of
the path.

2 August 2026, later still. The deny got a watchdog, because a rule nobody
checks is a rule that has already failed quietly. A weekly job asks the
question from outside, resolving every hostname to the WAN address the way a
stranger would, and calls anything that is not a refusal what it is. It reads
the host list from the live nginx config rather than a list of its own, so a
proxy host added next month is checked without anyone remembering, and it
reads the deliberately-public exceptions from the same file that enforces
them, so the check and the rule cannot drift apart. All three of its verdicts
were exercised before it was trusted, the pass, the failure with a host opened
on purpose to see the alarm work, and the inconclusive that fires when the
proxy cannot be read at all. That last one matters most. A check that cannot
see the estate must never report it safe.

2 August 2026, last of the day. The watchdog got a voice. It publishes to
ntfy on failure and on the inconclusive verdict, and stays silent on a pass,
because an alarm that fires weekly when nothing is wrong gets muted and a
muted alarm is worse than none. Four paths were exercised rather than two.
The pass sends nothing, the failure publishes and the message was read back
off the server to prove it arrived, the unreadable-proxy case sends its own
warning, and a destination that refuses the message records that refusal in
the report instead of swallowing it, because a notification path that has
quietly died looks exactly like a week with no problems. The destination
lives in a config file rather than the script, so a token never lands in
version control and the target can move without an edit. It currently points
at the homelab ntfy, which asks for no token and cannot do phone push. The
server that can is deny-all and wants a credential, and credentials are not
ours to mint.
2 August 2026, backups. The two things that cannot be rebuilt now leave the
machine that holds them. The database is the business memory, the touch log
and the buying-window history and the dedupe record, and config/profile/ is
the CV versions every score was made against, gitignored by design and
existing on one host and nowhere else. Neither survived that host dying.

The database is copied with VACUUM INTO and never with cp. Three workflows
overlap on one file in WAL mode, and a plain copy can catch a write in
flight and produce a file that opens cleanly while quietly missing the last
transaction, which is the worst kind of backup because it looks like a good
one. The script proves the snapshot opens and counts its rows before calling
itself finished, and a snapshot that will not open is deleted rather than
kept, because a file that cannot be read is not a backup and should not
occupy the slot of one.

Getting the copies away is a separate job on purpose, since a backup beside
the thing it protects is not one. A timer on the Pi runs at 02:37 nightly,
persistent so a machine that was off overnight still takes its copy on the
next boot, reaches into the container through the node, and verifies the
copy that landed here rather than the one at source. Fourteen daily and
eight weekly on the NAS drives, a different machine and different disks.
It mints no credentials to do it, and the container was deliberately not
given a key to the Pi.

Then it was restored, because a backup nobody has restored is a rumour. The
off-host copy opened at 190 opportunities, 34 signals and 0 touches against
a live database reading exactly the same, integrity ok, with the CV and the
preferences intact in the archive beside it. The procedure is written down
in docs/architecture.md with the warning that matters most, restore to a
scratch path and compare before anything goes near the live file.

2 August 2026, companies. The landscape stopped being rows and became
companies. One table keyed on the normalised name, so two spellings of one
firm meet in a single row instead of living as strangers in three tables,
and company_id on opportunities, signals and touches pointing at it. The
name columns stay exactly where they were, because they are what the page
shows and what a row restored from a backup still carries if a join ever
breaks. The normalising rule moved out of the inbox script into
radar_common, since it is now the natural key of a table and three writers
need the same answer to the same question.

The states are split by who owns them. Seen, touched and window open are
facts already in the database, an item exists, a touch exists, a job-advert
signal exists, and they are derived when the page asks rather than stored,
because a written-down copy of a derivable fact is a second source of truth
that eventually disagrees with the first. In conversation, client and dead
are stored, and only a human ever sets them, through touch.py state today
and the dossier later. The strongest that applies is the one shown, except
dead, which is not a rung on that ladder but the end of the climb and
outranks even client, because a human said so and no amount of derived
activity should argue.

The migration ran against a real copy before it ran anywhere else. A
hundred and forty five companies out of a hundred and ninety opportunities
and thirty four signals, every named row linked, nothing named left
unlinked, and the thirty four adverts that never carried a company name
left pointing at nothing rather than at an invented company called nothing.
Opened three times over and hand-run a fourth, it creates nothing the
second time. The backfill adds and annotates only. It never rewrites a
name, never merges two companies and never clears an id already set, so
the worst a stray re-run can do is cost one scan.

2 August 2026, enrichment. A company earns one description the first time it
is seen, so the same-day package never needs a research step. One fast-model
pass, capped for the whole run rather than per item so a busy email cannot
spend the day's budget, and logged as its own line in the runs table because
a cost that happens once per company behaves nothing like one that happens
once per advert and hiding them together would make both unreadable.

The prompt is told, twice, that it is describing what the evidence says and
not recalling a company it may have read about. Empty is a correct answer and
the common one, since most first sightings are a name, a city and a round.
Filling what_they_build with a plausible guess is worse than leaving it
blank, because the blank is honest and the guess gets believed. People are
kept only where the company published the name itself, so a founder quoted by
a journalist does not become a stored contact.

One polite fetch of the company site where the item names one, robots
deciding, and a disallow means no fetch and a status that says so. Nothing is
guessed from the company name, because guessing a domain and knocking on it
is how a polite watcher stops being one, and LinkedIn heads a list of hosts
that are never treated as a company site whatever the item says. A failed
fetch, an unparseable answer and an unknown company all end the same way, a
status written down and the advert stored and scored regardless, because
enrichment is a description and the record does not depend on it.

Proved against the real copy. A hundred and forty five stored companies
drained in three capped runs with no failures, and a re-run of the same email
enriched nothing, because a company already described is never described
again however often it turns up.

2 August 2026, geography. Insights group as Local, UK, Europe and
Elsewhere, in the order radar.yaml lists them, and the order is the config's
to decide rather than the code's. The lead story stays above the groups,
because the strongest story of the week is the strongest wherever it
happened and a front page has exactly one of those. Inside a group the fresh
and earlier split is untouched. An empty group is not rendered at all, so a
quiet month in Europe leaves no empty shelf on the page.

The catch-all earns its place. A company nobody has enriched has no country,
and a grouping rule that cannot place a row must put it somewhere rather
than drop it, because a row that vanishes from a page is indistinguishable
from a row that was never collected. Every unenriched company lands in
Elsewhere and says so, and the test that pins this asserts no signal is ever
left without a group.

The region is a cache. It is computed from the country and city the enricher
found, stored on the company and mirrored onto every opportunity and signal,
so grouping the page is a column read rather than a join on every render.
Caches go stale, so the rules carry a fingerprint and any edit to them
recomputes every row on the next database open. Editing the Local list is
therefore a config change and nothing more, no migration and no redeploy,
which is the point, because that list is a guess about where Luke will
travel and it should be cheap to correct.

The digest gets the group name as a plain prefix and nothing else. It is a
list read on a phone, not a page to browse, and geography earns three words
there rather than headings of its own.

2 August 2026, the dossier. One screen per company, at /company/ and its
id, reached by clicking the company name anywhere it appears. The timeline
interleaves adverts, insights and touches newest first, because the question
this page answers is what has happened with these people, and that question
does not care which table the answer is in. Above it sit the enrichment
summary, the region, the relationship state and the next move with its date.

Empty fields are shown as not known rather than hidden. A blank the
enricher honestly left is information, and hiding it would make a company
nobody has looked at yet indistinguishable from one that was studied and
found plain.

Three buttons and no more. In conversation, client and dead are the states a
human owns, and the page refuses anything else outright, including the
derived ones, so seen and touched and window open cannot be forced by a
POST. Dead sits behind a confirm because it outranks every other state
including client. Every accepted change writes a touch saying what it was
and what it became, so six months later the timeline explains itself rather
than the state having simply appeared. The notes go through the sanitiser
like every other generated line, and a test pins that.

Read only otherwise, and behind nothing of its own. It is served by the same
process as every other page, so it sits behind the same LAN and tailnet
restriction that closed the estate this morning, which is the whole reason
that work came first.

2 August 2026, the drafts. When a signal clears the fast bar and its step
is the announcement-day comment and connection note, both are written at
push time and stored on the signal, so the phone buzzing becomes a two
minute editing job rather than a blank page. The push says drafts are ready
and stays short. Nothing is ever posted by the machine, and the words sit
there until a human copies them.

The comment must earn its specificity. It is drafted from the fetched
article where there is one and from the headline where there is not, and
the row records which, because a comment written from a headline is a
weaker thing and the page should not pretend otherwise. When the text is
too thin to say something that could not be pasted under another company's
announcement, the drafter returns an empty comment on purpose. Silence
under a founder's post is better than enthusiasm that reads as automated.

Every draft is checked before it is stored and refused outright if it
fails, because a bad draft is worse than none, it is sitting there ready to
send. A price, a link, a named service, an offer, an ask, standards in the
comment where they belong only in the note, or a note over the three
hundred character limit, any of those and nothing is stored and the run
says why. The doctrine tests now cover the drafts in both modes and also
prove the guard catches what it claims to, by feeding it four drafts that
should fail and checking each one does.

3 August 2026. The backfill enriched every stored company and the page did
not move, which is how a bug in the region cache announced itself. The cache
was recomputed only when the rules changed, and the rules had not. What had
changed was the evidence, because enrichment fills a country in long after
the company row was created, so thirty nine companies whose country the
database plainly knew sat grouped as Elsewhere, and one company created
since the last pass had no group at all.

Two causes, two fixes. Enrichment now decides the region at the moment the
country becomes known, which is the natural place for it, and a company
with no region is now itself a reason to recompute rather than something the
fingerprint check silently steps over. The live rows were corrected first
and the code second, so the page told the truth before the fix shipped. A
regression test pins both, a late-arriving company forcing a recompute and a
country learned after the fact regrouping its rows.

The backfill itself, for the record. A hundred and fifty seven companies,
none failed, a hundred and fifty three thousand input tokens on the fast
model and seven thousand out, which is pennies. The yield is honest and
thin, thirty nine countries, forty six cities, seventeen descriptions of
what a company builds and two of its software. That is not the enricher
underperforming. Most of these companies arrived as job adverts carrying a
title, a location and a board URL, and there is nothing else in that to
know. The companies that arrive as news will read better, and the empty
fields are the prompt doing as it was told rather than inventing.

3 August 2026, later. The group headings were showing an empty shelf. Each
group emitted its name as a rule of its own and then the fresh and earlier
rules underneath, so the eye read a heading called UK with nothing beneath
it and the stories apparently belonging to the next heading down. One rule
per run of cards now, naming both the place and the freshness, UK. Fresh
this week. The information icon rides on the first rule of each group rather
than repeating.

3 August 2026, the last of the trade press. Silicon Semiconductor and PIC
Magazine follow Electronics Weekly and Semiconductor Digest off the
watchlist, and for the same reason, which is that they cannot produce what
the radar is looking for. The story that made the case was a Scottish
Enterprise grant to Cirrus Logic for smart metering silicon, which arrived
on a page meant to find European medtech companies who might need software
help. The rubric scored it 8 and was right. The source was still wrong.

Four trade feeds parked in three days, and the arithmetic behind all four is
the same. Every one of them averaged between 2 and 6 on the rubric and none
ever came within sixty points of the digest bar, while optics.org averages
17 and stays. PIC Magazine is the one exception to the evidence rule, having
produced nothing at all, so it is parked on category rather than on measure.
Photonic integrated circuits are a component industry, and this radar wants
the companies building diagnostics with them rather than the fabs.

What is left is thin on purpose. Fourteen tier 1 origin sources that are
quiet by nature and valuable when they speak, optics.org, Startups Magazine
and a New Electronics entry that robots has kept dark since July. A page
that shows four relevant things a month is worth more than one showing
thirty five irrelevant ones, and the quiet is the system working rather than
the system broken.

3 August 2026, the net comes in. Startups Magazine follows the other four
off the watchlist. Four scored signals, average relevance 2.0, best 2, and
it is the most instructive of the five because it sounds like a fit and is
not. A general UK startup feed spends its volume on everything that is not
European medtech, and covering the target by accident once a quarter is not
coverage.

The tier 2 safety net is now optics.org and a New Electronics entry that
robots has kept dark since July. Fourteen origin sources remain, the press
desks and the investors and the accelerators, which is what the watchlist
was for before the trade press was added to it as insurance. That insurance
cost five sources worth of scoring calls and returned nothing above 22 on a
bar of 70, so it has been cancelled.

The page will get quieter, not better looking, and the thirty stories
already sitting in Elsewhere are history rather than a feed. Parking a
source stops new arrivals, it does not retract what is stored. If the quiet
reads as breakage rather than discipline, the display floor is the smaller
change and it is still on the table.
