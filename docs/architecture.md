# Radar architecture

How the pieces fit, and why they fit that way.

## The shape of it

Email is the ingestion bus. Every job alert, Google Alert and Dealroom notification lands in one Gmail aggregator inbox. That gives us one door to watch, free deduplication of sources, and no scraping of anywhere that would rather not be scraped. LinkedIn data arrives this way only. That is a hard rule.

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

```dockerfile
FROM n8nio/n8n
USER root
RUN apk add --no-cache python3 py3-pip && pip3 install --break-system-packages anthropic PyYAML feedparser
USER node
```

## The fallback

If putting Python next to n8n ever becomes a nuisance, the scripts do not need n8n at all. Run `process_email.py`, `check_signals.py` and `build_digest.py` by cron on any box that has the repo and the key. In that setup n8n keeps only the jobs that genuinely need its credentials, reading Gmail and sending the digest email. Everything else is just Python, SQLite and a schedule.
