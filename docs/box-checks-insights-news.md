# Box checks: Insights as a news page (branch claude/insights-news)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. BEFORE and AFTER are read only.

**This branch sits on `claude/jobs-redesign`** (the new Jobs page and the shared
top bar), which is not on main yet. Merging this branch brings both. Run the
Jobs redesign's own pack, `docs/box-checks-jobs-redesign.md`, alongside this
one: its step 5 (the `read_at` stamp) still applies, and its AFTER line that
counts "Watchlist" on every page will now find it on /insights, because the
story cards say Add to Watchlist. The top bar still has no Watchlist link.

What the change touches on the box:

- The dashboard: /insights is the news page (variation B from
  design/insights/news), with /insights/story/<id>, /insights/section/<key>,
  and six new guarded POST routes: /story/hide, /story/show, /story/split,
  /watchlist/add, /watchlist/remove and /touch/log. /archive is unchanged.
- Four new nullable columns, added on first open: `signals.story_type`,
  `signals.card_summary`, `signals.event_key` and `companies.watched_at`.
  Nothing is stamped by the migration.
- The signal scorer prompt, v1.2: each item now also gets a type and a card
  summary, headlines lose their closing full stop, and rule changes for
  device software have a band (40 to 74). Relevance for funding, spin-off and
  accelerator stories is unchanged.
- The watcher and the inbox store the type, the summary and the event. A
  buying window's headline is now "<Company> advertises for a <role>, a first
  hire at a company you have been in touch with", and that is what its phone
  push starts with.
- One story per event: a report about the same company, found up to seven
  days after an earlier one, with a similar headline once the company's name is
  left out, joins that story. A new report of a story you hid arrives hidden
  and is not pushed. rescore.py now gives a late-scored row the same type,
  summary, company and story as one scored on arrival.
- Three regulator sources in `config/watchlist.yaml`, all `status: unverified`,
  so no run reads them until you switch them on (step 9).
- A one-off backfill, `scripts/backfill_stories.py`: grouping is free; typing
  the last 90 days costs one scoring call a row (about 0.7 to 1.4 cents each,
  see docs/cost-note.md) and only runs with `--live`.

No write-guard setting and no n8n workflow changes.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; git status --short; git log --oneline -1
echo '--- is anything running'; pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || echo 'nothing running'
echo '--- the new columns are not there yet (expect 0 0 0 0)'; Q "SELECT (SELECT COUNT(*) FROM pragma_table_info('signals') WHERE name='story_type'), (SELECT COUNT(*) FROM pragma_table_info('signals') WHERE name='card_summary'), (SELECT COUNT(*) FROM pragma_table_info('signals') WHERE name='event_key'), (SELECT COUNT(*) FROM pragma_table_info('companies') WHERE name='watched_at');"
echo '--- signals by state, to compare after'; Q "SELECT COUNT(*) AS signals, SUM(acknowledged_at IS NOT NULL) AS dismissed, SUM(relevance IS NULL) AS unscored, SUM(relevance >= 40) AS at_40, SUM(first_seen >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 days')) AS last_7_days FROM signals;"
echo '--- touches, to compare after'; Q "SELECT COUNT(*) AS touches FROM touches;"
echo '--- /health'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- Insights today (expect 200)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/insights
```

Expect a clean `git status`, `nothing running`, `0 0 0 0`, and `200`. Keep the
counts for AFTER.

## The deploy, one block at a time

1. Run BEFORE.

2. Back up first.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar bash scripts/backup_radar.sh
   ```

3. Merge the branch on GitHub, then see what will arrive. Expect the Jobs
   redesign's commits and this branch's, nothing else you have not deployed.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar git fetch origin && git log --oneline HEAD..origin/main
   ```

4. Pull, only when nothing is running.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was pulled' || { sudo -u radar git pull --ff-only && git log --oneline -3; }; }
   ```

5. Open the database by hand once, which adds the four columns (and runs the
   Jobs redesign's one-time `read_at` stamp, if that has not happened yet).

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, the database was not opened' || sudo -u radar /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; radar_common.get_db().close(); print('opened')"; }
   ```

6. Group the stories already on file, free, and see how many the paid pass
   would read. Nothing is sent to the model.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar /opt/biz-admin/radar-venv/bin/python scripts/backfill_stories.py
   ```

   It prints how many signals make how many stories and how many rows were
   given a key (a second run gives none), how many hiring signals were typed
   in code, and "Dry run: N signals from the last 90 days have no
   type". N times about one cent is the cost of step 7.

7. Your call: type and summarise the last 90 days. Without it, those stories
   all file under Regulation and market news until they age out, and their
   cards show the longer why-it-matters line. New stories are typed as they
   arrive either way.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar /opt/biz-admin/radar-venv/bin/python scripts/backfill_stories.py --live
   ```

8. Restart the dashboard.

   ```bash
   docker restart medtech-radar
   ```

9. Your call, any time later: the regulator sources. Spot-check each, and
   switch it to `status: live` in `config/watchlist.yaml` only if it answers
   and what it finds is the regulator's own news.

   ```bash
   cd /opt/biz-admin/medtech-radar && for s in mhra-news eu-mdcg-guidance team-nb-news; do echo "== $s"; sudo -u radar /opt/biz-admin/radar-venv/bin/python scripts/check_signals.py --dry-run --source $s 2>&1 | tail -8; done
   ```

10. Run AFTER, then open /insights in a browser at full width and on the
    phone. Hide a story and press Undo; add a company to the Watchlist and
    press Undo in the message; open a story and check its sources.

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code on the box'; git status --short; git log --oneline -3
echo '--- the columns are there (expect 1 1 1 1)'; Q "SELECT (SELECT COUNT(*) FROM pragma_table_info('signals') WHERE name='story_type'), (SELECT COUNT(*) FROM pragma_table_info('signals') WHERE name='card_summary'), (SELECT COUNT(*) FROM pragma_table_info('signals') WHERE name='event_key'), (SELECT COUNT(*) FROM pragma_table_info('companies') WHERE name='watched_at');"
echo '--- signals by state, the same as BEFORE'; Q "SELECT COUNT(*) AS signals, SUM(acknowledged_at IS NOT NULL) AS dismissed, SUM(relevance IS NULL) AS unscored, SUM(relevance >= 40) AS at_40, SUM(first_seen >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 days')) AS last_7_days FROM signals;"
echo '--- stories: signals per event, the biggest first'; Q "SELECT event_key, COUNT(*) AS reports, MAX(relevance) AS best, MIN(company) AS company FROM signals GROUP BY event_key HAVING COUNT(*) > 1 ORDER BY reports DESC LIMIT 10;"
echo '--- types (all NULL but the hiring ones if step 7 was skipped)'; Q "SELECT COALESCE(story_type, 'none') AS type, COUNT(*) FROM signals GROUP BY 1 ORDER BY 2 DESC;"
echo '--- touches, the same as BEFORE'; Q "SELECT COUNT(*) AS touches FROM touches;"
echo '--- /health, the same verdict as BEFORE'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- the news page (expect 200, 1, 0)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/insights; curl -s -m 15 $B/insights | grep -c 'Most relevant this week'; curl -s -m 15 $B/insights | grep -ci 'I did it'
SID=$(Q "SELECT id FROM signals WHERE relevance IS NOT NULL ORDER BY relevance DESC, id DESC LIMIT 1;" | sed -n 2p); echo "story $SID"
echo '--- one story and one section (expect 200 200)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/insights/story/$SID; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/insights/section/funding
echo '--- the guard is in front of the new routes (enforce: 403; warn: 409, no such story, nothing written)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"id": 0}' $B/story/hide
```

Expect `1 1 1 1`, the same signal and touch counts as BEFORE, the reports of
one event grouped under one key, `200 1 0`, `200 200`, and `403` (enforce) or
`409` (warn).

## Rollback

The columns are nullable and nothing outside this branch reads them, so they
can stay.

```bash
cd /opt/biz-admin/medtech-radar && sudo -u radar git log --oneline -8
# pick the commit from before the merge, then:
sudo -u radar git reset --hard <that commit> && docker restart medtech-radar
```

If you want the old page and the rows exactly as they were, restore the
backup from step 2 the way `scripts/backup_radar.sh` describes.

## The font

The headlines are set in IBM Plex Serif from `static/fonts/`, and fall back to
Georgia if a file is missing. The three files (`IBMPlexSerif-Regular.woff2`,
`-Medium`, `-SemiBold`, 69 to 73 KB each) are IBM's, from `@ibm/plex-serif`
2.0.0 (`fonts/complete/woff2`), under the SIL Open Font License, whose text is
`static/fonts/OFL.txt`. They arrive with the pull, as the dashboard serves them
from the checkout. To check after step 8 (expect `200 font/woff2` three times):

```bash
for f in Regular Medium SemiBold; do curl -s -m 15 -o /dev/null -w '%{http_code} %{content_type}\n' http://192.168.1.100:8787/static/fonts/IBMPlexSerif-$f.woff2; done
```
