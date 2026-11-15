# Box checks: Home final (branch claude/home-final)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. BEFORE, the look in step 6 and AFTER are read
only. Nothing here calls the model.

What the change touches on the box:

- The dashboard's Home, at `/`, is rebuilt from design/home/final: the
  welcome (the date, the greeting, one sentence, four figures), Radar's five
  jobs as lights (On time, Late, Stale, Off or Dry run, each with its own
  mark), the Applications, Open roles and Week by week tiles, and Needs you
  today with the selected item beside the list (under its row on a phone).
  Every item has its own address, `/?item=<id>`; the items taken off Home are
  at `/?show=done`; `/?part=main` is a read only fragment for the page's
  script.
- Done (key E) takes an item off Home, and only off Home. It is kept in a new
  table, `home_done`, made on first open; nothing else reads it, and no other
  table changes. Two new guarded POST routes: `/home/done` and
  `/home/undone`.
- The old Home moves to `/radar`, "Radar's health" (the lights link to it),
  without its entrance cards. Its scoring panel moves to the CV page's
  Settings, `/cv#settings`, with a new field for the first name.
- The welcome greets by the optional `first_name` line of
  `config/profile/preferences.md` (step 7), and says "Good morning." without
  one. The scorer reads that file whole, so the line is in its prompt too; it
  changes no score. A labelled line that is empty now reads as empty, where it
  read the next line before.
- The board opens on the column a Home link names (`#col-applied` and so on).

No migration of data, no n8n workflow, no scheduled job, no write-guard
setting and no radar.yaml change.

**What Home will show on the first day.** The digest and the signal watcher
run in dry run on the box, so their lights say Dry run (the phone push's too,
since it rides on the watcher), and each is an item under Radar in Needs you
today until you press Done on it; it comes back if its state changes. The
status sentence names them after the four counts. Step 6 prints what the page
will list before you restart.

Don't press Done on Home between step 4 and step 8: the old dashboard is still
serving and has no such button, and its Home is the old page.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; sudo -u radar git status --short; sudo -u radar git log --oneline -1
echo '--- is anything running'; pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || echo 'nothing running'
echo '--- the Done table is not there yet (expect 0)'; Q "SELECT COUNT(*) AS home_done FROM sqlite_master WHERE type = 'table' AND name = 'home_done';"
echo '--- each job last finished run, the mode its light reads'; Q "SELECT workflow, MAX(ts) AS last_run, mode FROM runs WHERE workflow IN ('inbox', 'signals', 'check', 'digest') AND COALESCE(outcome, 'ok') <> 'failed' GROUP BY workflow;"
echo '--- what the figures will count: urgent stories, unread Top and One question away roles'; Q "SELECT (SELECT COUNT(DISTINCT COALESCE(event_key, 'e' || id)) FROM signals WHERE relevance >= 75 AND acknowledged_at IS NULL AND status NOT IN ('actioned', 'dead') AND first_seen >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')) AS urgent, (SELECT COUNT(DISTINCT COALESCE(role_key, 'row-' || id)) FROM opportunities WHERE tier = 'top' AND read_at IS NULL AND acknowledged_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL) AS top_unread, (SELECT COUNT(DISTINCT COALESCE(role_key, 'row-' || id)) FROM opportunities WHERE tier = 'question' AND read_at IS NULL AND acknowledged_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL) AS question_unread;"
echo '--- application steps due today or overdue'; Q "SELECT COUNT(*) AS steps_due FROM applications WHERE stage IN ('preparing', 'applied', 'conversation', 'interviewing', 'offer') AND next_step IS NOT NULL AND next_step_due <= date('now');"
echo '--- kept Top scores the week chart will read'; Q "SELECT COUNT(*) AS top_scores_kept, MIN(COALESCE(scored_at, recorded_at)) AS earliest FROM score_history WHERE tier = 'top' AND event <> 'tailored';"
echo '--- the row counts, to compare after'; Q "SELECT (SELECT COUNT(*) FROM opportunities) AS roles, (SELECT COUNT(*) FROM signals) AS signals, (SELECT COUNT(*) FROM applications) AS applications, (SELECT COUNT(*) FROM touches) AS touches;"
echo '--- is there a first_name line yet'; grep -n '^first_name:' config/profile/preferences.md || echo 'no first_name line'
echo '--- /health'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- Home today (expect 200)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/
```

Keep the output: AFTER compares with it. The figures BEFORE prints are a
guide: Home also leaves out a role applied for and anything you have pressed
Done on.

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
   as the rollback point, in `.git/rollback-home-final`, and only if no
   rollback point is saved yet, so running this again after a pull keeps the
   first one. Nothing is pulled unless the point is there.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was pulled' || { { test -s .git/rollback-home-final || sudo -u radar sh -c 'git rev-parse HEAD > .git/rollback-home-final'; } && test -s .git/rollback-home-final && echo "rollback point $(cat .git/rollback-home-final)" && sudo -u radar git pull --ff-only && sudo -u radar git log --oneline -3; }; }
   ```

5. Open the database by hand once. This makes the `home_done` table, and
   prints that it is there and empty.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, the database was not opened' || sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; c = radar_common.get_db(); print('opened, home_done rows:', c.execute('SELECT COUNT(*) FROM home_done').fetchone()[0]); c.close()"; }
   ```

   Expect `opened, home_done rows: 0`.

6. Look at what Home will list, without serving it. Read only.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python -c "import sys, sqlite3; sys.path.insert(0, 'scripts'); import radar_common as rc, build_dashboard as bd, home_page as hp; cfg = rc.load_config(); c = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); c.row_factory = sqlite3.Row; m = hp.build_model(c, bd.collect(c, cfg), cfg); print(hp.greeting(m['now'], m['name'])); print(hp.status_sentence(m)); print('lights:', ', '.join(l['label'] + ' ' + l['word'] for l in m['lights'])); print('groups:', m['counts']); w = m['week']; print('weeks:', [(str(x['week']), x['top'], x['signals']) for x in w['weeks']], 'first week' if w['first_week'] else '')"
   ```

   Expect the greeting (without a name until step 7), the sentence, five
   lights, the group counts and up to four weeks of Top prospects and
   signals. Dry run on the digest, the watcher and the phone push is the box
   as it is, not a fault of this change.

7. The first name for the welcome. Open the preferences file and add one line
   at the end, your first name after `first_name:`, for example
   `first_name: Sam`. Only if BEFORE said `no first_name line`; otherwise edit
   the line that is there. The file is gitignored and stays on the box.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar nano config/profile/preferences.md && grep -n '^first_name:' config/profile/preferences.md
   ```

   Expect one line, `first_name: ` and your name. The CV page's Settings can
   change it later, as it changes the title and the floor. Leave it out, or
   empty, and Home says "Good morning." without a name.

8. Restart the dashboard.

   ```bash
   docker restart medtech-radar
   ```

9. Run AFTER, then open Home in a browser at full width and on the phone.
   Press J and K to move through Needs you today, Enter to open an item where
   it lives and come back, E on an item and then Undo last, and open Done to
   see it there before you undo. Follow each figure, Radar's health and
   Settings, on the CV page.

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code on the box'; sudo -u radar git status --short; sudo -u radar git log --oneline -3
echo '--- the Done table, and what is in it'; Q "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'home_done') AS home_done_table, (SELECT COUNT(*) FROM home_done) AS done_rows;"
echo '--- what was pressed Done, newest first'; Q "SELECT item_key, done_at, title FROM home_done ORDER BY done_at DESC LIMIT 10;"
echo '--- the row counts, the same as BEFORE (Done writes no other table)'; Q "SELECT (SELECT COUNT(*) FROM opportunities) AS roles, (SELECT COUNT(*) FROM signals) AS signals, (SELECT COUNT(*) FROM applications) AS applications, (SELECT COUNT(*) FROM touches) AS touches;"
echo '--- the first_name line'; grep -n '^first_name:' config/profile/preferences.md || echo 'no first_name line, Home greets without a name'
echo '--- /health, the same verdict as BEFORE'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- Home, one item, Done and the fragment (expect 200 1, 200, 200, 200)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/; curl -s -m 15 $B/ | grep -c 'id="welcome-h"'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/?show=done"; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/?item=radar-signals"; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/?part=main"
echo '--- Radar health and Settings (expect 200 1, 200 1)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/radar; curl -s -m 15 $B/radar | grep -c 'Board freshness'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/cv; curl -s -m 15 $B/cv | grep -c 'id="pref-first_name"'
echo '--- the guard is in front of the new routes (enforce: 403; warn: 400, nothing written)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"key": "not a key"}' $B/home/done
```

Expect `1` and as many rows as you pressed Done on; the same row counts as
BEFORE; your `first_name` line; `200 1`, `200`, `200`, `200`; `200 1`,
`200 1`; and `403` (enforce) or `400` (warn).

## Rollback

Code only. The backup from step 2 is not restored: the `home_done` table is
left as it is, and the old code never reads it, so it does no harm and Done
comes back as it was if the branch is deployed again. The `first_name` line
stays in the preferences file too; the old code never reads it as a setting,
and the scorer reads it as it reads the rest of the file.

```bash
cd /opt/biz-admin/medtech-radar && test -s .git/rollback-home-final && sudo -u radar git reset --hard "$(cat .git/rollback-home-final)" && sudo -u radar git log --oneline -1 && docker restart medtech-radar
```

The old code's Home is the old page again, with the scoring panel on it, and
`/radar` is a 404. If the branch goes back on later, the items pressed Done
before the rollback stay off Home, and anything pressed meanwhile is not there
to press.
