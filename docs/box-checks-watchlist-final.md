# Box checks: the Watchlist final (branch claude/watchlist-final)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. BEFORE, the look in step 6 and AFTER are read
only. Nothing here calls the model.

What the change touches on the box:

- A new page, the Watchlist, at `/watchlist`, back in the top bar between
  Insights and CV: your companies in a table (threads due first), Radar's
  suggestions, and a company's record beside the table from 1024 wide
  (`/watchlist?company=<id>`) or as its own view on a phone
  (`/watchlist/<id>`). The record shows the facts with their source and date,
  signals and stories, roles on Jobs, the newest story's drafts with Copy, the
  thread and the contacts, Log a contact, people added by hand, and Read
  again.
- Two new tables, made on first open: `watch_suggestions` (Radar's
  suggestions and how each was answered) and `company_people` (people added
  by hand). New columns on `companies` (`watched_via`, `watched_signal_id`,
  `arrival_seen_at`, `unwatched_at`, `watched_was`, `site_url`,
  `fact_sources`) and on `touches` (`kind`). The rows Set state has written
  ("State set to ...") are marked `kind = 'state'` once, so a state change
  never reads as the newest contact.
- Log a contact moves from the Insights story page to the record. It takes the
  date, how, a note, what came back, and a next step with its due date; a next
  step needs a due date, and leaving it empty closes the thread. An existing
  next step with no date shows "No date set" and never counts as due. Logging
  a contact for a company not on the Watchlist adds it.
- Suggestions are worked out in code at the end of each signal watcher run
  (no model call): buying windows, funding rounds and Insights stories at 70
  or more, from the last 30 days, five at most. Nothing joins the Watchlist
  until you press Add. The first run after the deploy fills them.
- Home gets its fifth figure, threads due, and a Threads due group in Needs
  you today, counting only Watchlist companies.
- Read again on a record makes one polite fetch and one model call (the
  enricher's fast model), only on a press, with the cost shown first. The
  enricher's prompt (`prompts/enricher.md`) now also asks where each fact came
  from, so a company first seen after the deploy keeps a source per fact.
- Guarded POST routes, new: `/watchlist/add-name`, `/watchlist/suggestion`,
  `/watchlist/arrived`, `/watchlist/restore`, `/watchlist/thread`,
  `/watchlist/person`, `/watchlist/person/remove`, `/watchlist/reread`;
  `/watchlist/add`, `/watchlist/remove` and `/touch/log` take more fields.

No n8n workflow, no scheduled job, no write-guard setting and no radar.yaml
change.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; sudo -u radar git status --short; sudo -u radar git log --oneline -1
echo '--- is anything running'; pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || echo 'nothing running'
echo '--- the new tables are not there yet (expect 0)'; Q "SELECT COUNT(*) AS new_tables FROM sqlite_master WHERE type = 'table' AND name IN ('watch_suggestions', 'company_people');"
echo '--- companies on the Watchlist today'; Q "SELECT id, display_name, watched_at FROM companies WHERE watched_at IS NOT NULL ORDER BY watched_at;"
echo '--- the rows Set state wrote, which will be marked as state rows'; Q "SELECT COUNT(*) AS state_rows FROM touches WHERE channel = 'other' AND note LIKE 'State set to %';"
echo '--- next steps on file with no date (they will show No date set)'; Q "SELECT company, touched_at, next_action FROM touches WHERE COALESCE(TRIM(next_action), '') <> '' AND next_action_date IS NULL ORDER BY touched_at DESC LIMIT 10;"
echo '--- the row counts, to compare after'; Q "SELECT (SELECT COUNT(*) FROM companies) AS companies, (SELECT COUNT(*) FROM touches) AS touches, (SELECT COUNT(*) FROM signals) AS signals, (SELECT COUNT(*) FROM opportunities) AS roles;"
echo '--- /health'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- the Watchlist today (expect 404)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/watchlist
```

Keep the output: AFTER compares with it.

## The deploy, one block at a time

1. Run BEFORE.

2. Back up first.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar bash scripts/backup_radar.sh
   ```

3. Merge the branch on GitHub, then see what will arrive. Expect this
   branch's commits and nothing else you have not deployed.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar git fetch origin && sudo -u radar git log --oneline HEAD..origin/main
   ```

4. Pull, only when nothing is running. The commit you are on is saved first
   as the rollback point, in `.git/rollback-watchlist-final`, and only if no
   rollback point is saved yet, so running this again after a pull keeps the
   first one. Nothing is pulled unless the point is there.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was pulled' || { { test -s .git/rollback-watchlist-final || sudo -u radar sh -c 'git rev-parse HEAD > .git/rollback-watchlist-final'; } && test -s .git/rollback-watchlist-final && echo "rollback point $(cat .git/rollback-watchlist-final)" && sudo -u radar git pull --ff-only && sudo -u radar git log --oneline -3; }; }
   ```

5. Open the database by hand once, only when nothing is running. This makes
   the two tables and the columns, and marks the Set state rows, then prints
   what it found.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, the database was not opened' || sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; c = radar_common.get_db(); print('opened, suggestions:', c.execute('SELECT COUNT(*) FROM watch_suggestions').fetchone()[0], 'people:', c.execute('SELECT COUNT(*) FROM company_people').fetchone()[0], 'state rows:', c.execute('SELECT COUNT(*) FROM touches WHERE kind = ?', ('state',)).fetchone()[0]); c.close()"; }
   ```

   Expect `opened, suggestions: 0 people: 0 state rows:` and the number
   BEFORE printed for the Set state rows.

6. Look at what the Watchlist and Home will show, without serving them. Read
   only.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python -c "import sys, sqlite3; sys.path.insert(0, 'scripts'); import radar_common as rc, watchlist as wl; c = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); c.row_factory = sqlite3.Row; m = wl.build_model(c, rc.load_config()); print('watched:', m['counts']['watched']); print('threads due:', m['counts']['due'], '(today', m['counts']['today'], 'overdue', m['counts']['overdue'], ')'); [print(' ', r['name'], '|', r['thread']['state'], r['thread']['due_date'] or '', '|', r['state_words']) for r in m['rows']]; print('suggestions waiting:', m['counts']['waiting'])"
   ```

   Expect your watched companies, each with its thread state (overdue, today,
   upcoming, nodate for No date set, closed, or none) and `threads due:` the
   number Home's fifth figure will show. Suggestions waiting is 0 until the
   next signal watcher run.

7. Restart the dashboard.

   ```bash
   docker restart medtech-radar
   ```

8. Let the signal watcher run once, on its schedule or with Check now on
   Home, then see the suggestions it made. Read only.

   ```bash
   cd /opt/biz-admin/medtech-radar
   PY=/opt/biz-admin/radar-venv/bin/python
   Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
   Q "SELECT ts, outcome, note FROM runs WHERE workflow = 'signals' ORDER BY id DESC LIMIT 1;"
   Q "SELECT s.id, c.display_name, s.kind, s.relevance, s.suggested_at FROM watch_suggestions s JOIN companies c ON c.id = s.company_id WHERE s.accepted_at IS NULL AND s.dismissed_at IS NULL ORDER BY s.id;"
   ```

   Expect the run's note to say how many were suggested, and at most five
   waiting. None of them is on the Watchlist until you press Add.

9. Run AFTER, then open the Watchlist in a browser at full width and on the
   phone. Press J and K to move through the table, Enter to open a record and
   Esc to come back, L to reach Log a contact, and / to add a company by name.
   Log a contact with a next step and no date to see it refused, then with a
   date. Open Home and follow the threads due figure.

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code on the box'; sudo -u radar git status --short; sudo -u radar git log --oneline -3
echo '--- the new tables (expect 2)'; Q "SELECT COUNT(*) AS new_tables FROM sqlite_master WHERE type = 'table' AND name IN ('watch_suggestions', 'company_people');"
echo '--- suggestions by how they stand'; Q "SELECT CASE WHEN accepted_at IS NOT NULL THEN 'added' WHEN dismissed_at IS NOT NULL THEN 'dismissed by ' || dismissed_by ELSE 'waiting' END AS stands, COUNT(*) AS n FROM watch_suggestions GROUP BY stands;"
echo '--- how each watched company joined'; Q "SELECT COALESCE(watched_via, 'before this change') AS via, COUNT(*) AS n FROM companies WHERE watched_at IS NOT NULL GROUP BY via;"
echo '--- the row counts, the same as BEFORE unless you logged or added something'; Q "SELECT (SELECT COUNT(*) FROM companies) AS companies, (SELECT COUNT(*) FROM touches) AS touches, (SELECT COUNT(*) FROM signals) AS signals, (SELECT COUNT(*) FROM opportunities) AS roles;"
echo '--- /health, the same verdict as BEFORE'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- the Watchlist and its lookup (expect 200 1, 200)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/watchlist; curl -s -m 15 $B/watchlist | grep -c '<h1'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/watchlist/find?q=ab"
echo '--- the top bar links to it, and Home has the threads due figure (expect 1, 1)'; curl -s -m 15 $B/jobs | grep -c 'href="/watchlist"'; curl -s -m 15 $B/ | grep -c 'id="figure-threads"'
echo '--- the guard is in front of the new routes (enforce: 403; warn: 400, nothing written)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"company_id": "not a number"}' $B/touch/log
```

Expect `2`; your suggestions, waiting until you answer them; the watched
companies from before under `before this change`; the same row counts as
BEFORE apart from what the watcher run in step 8 stored (signals, and the
companies they name) and any contact you logged or company you added; `200 1`, `200`; `1`,
`1`; and `403` (enforce) or `400` (warn).

## Rollback

Code only. The backup from step 2 is not restored: the two new tables and the
new columns are left as they are, and the old code never reads them, so they
do no harm and the suggestions, the people added by hand and how each company
joined are there again if the branch is deployed again. The contacts logged
meanwhile stay in `touches` with their next steps and due dates, which the old
code reads as it always has. The enricher prompt goes back with the code.

```bash
cd /opt/biz-admin/medtech-radar && test -s .git/rollback-watchlist-final && sudo -u radar git reset --hard "$(cat .git/rollback-watchlist-final)" && sudo -u radar git log --oneline -1 && docker restart medtech-radar
```

After it, `/watchlist` is a 404 again, the top bar has no Watchlist link,
Home has four figures, and Log a contact is back on the Insights story page.
