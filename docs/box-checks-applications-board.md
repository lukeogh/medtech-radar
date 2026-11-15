# Box checks: the applications board (branch claude/applications-board)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. BEFORE, the look in step 6 and AFTER are read
only. Nothing here calls the model.

What the change touches on the box:

- The dashboard: Jobs' Applied view gets a List and Board switch, with Board
  the default at `/jobs?show=applied` and the List at
  `/jobs?show=applied&layout=list`. Six columns, Preparing, Applied, In
  conversation, Interviewing, Offer and Closed, with "Waiting on you" across
  the top. Every move is the Move to menu or a key (M opens it, 2 to 5 picks a
  column, N, T or W closes with that reason, Z undoes), never a drag. Two new
  guarded POST routes: `/application/move` and `/application/next`, and a read
  only fragment, `/jobs?show=applied&part=board`.
- The Jobs header's Applied count now covers Preparing to Offer, not Closed.
- Two new tables, made on first open: `applications` (one row per role, its
  stage, who put it there and when, its next step and interview) and
  `application_events` (every change, with when and who). No column is added
  to an existing table.
- A one-time migration on that first open, behind the meta flag
  `migrated_applications`: every role with `applied_at` goes to Applied, dated
  from `applied_at`, and every role with a saved tailored CV and no
  `applied_at` goes to Preparing, dated from `saved_at`.
- Radar fills Preparing (Save CV on Prepare application) and Applied (Mark
  applied on Jobs or Prepare application). Undo applied on Jobs takes an
  Applied item back to Preparing, or off the board, and is refused once you
  have moved it on. Every later move is yours.
- The 21-day rule and Radar's "Follow up" ride on the **signals run**
  (`scripts/check_signals.py`, the radar-signals workflow, every two hours),
  as its first step. No new n8n workflow and no workflow change. An Applied
  item with no movement (a stage change, a next step set or changed, or a
  contact logged with the company) for 21 days is closed as no reply without
  asking; from day 14 it says when. An item the migration made is never
  closed sooner than 7 days after the migration, so the first runs after the
  deploy do not close old applications that never showed the warning. Seven
  days after a role is marked applied, the run adds a "Follow up" next step,
  due that day, unless you set one.
- A closed application is not a closed advert: `closed_at`, the expiry and the
  page checker are untouched, and `applied_at` stays on the role in every
  stage, so an applied role stays protected.

No write-guard setting, no n8n workflow and no preferences change.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; git status --short; git log --oneline -1
echo '--- is anything running'; pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || echo 'nothing running'
echo '--- the board tables are not there yet (expect 0 0)'; Q "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'applications') AS applications, (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'application_events') AS events;"
echo '--- roles applied for, what the migration will put in Applied'; Q "SELECT COUNT(DISTINCT COALESCE('key:' || role_key, 'id:' || id)) AS applied_roles, MIN(applied_at) AS earliest, MAX(applied_at) AS latest FROM opportunities WHERE applied_at IS NOT NULL;"
echo '--- saved tailored CVs, what may go to Preparing'; Q "SELECT COUNT(*) AS saved_cvs FROM tailored_cvs WHERE saved_text IS NOT NULL;"
echo '--- the role rows lifecycle, to compare after'; Q "SELECT COUNT(*) AS roles, SUM(applied_at IS NOT NULL) AS applied, SUM(closed_at IS NOT NULL) AS closed, SUM(expired_at IS NOT NULL) AS expired, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(acknowledged_at IS NOT NULL) AS acknowledged FROM opportunities;"
echo '--- touches, to compare after'; Q "SELECT COUNT(*) AS touches FROM touches;"
echo '--- /health'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- the Applied view today (expect 200)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/jobs?show=applied"
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
   cd /opt/biz-admin/medtech-radar && sudo -u radar git fetch origin && git log --oneline HEAD..origin/main
   ```

4. Pull, only when nothing is running. The commit you are on is saved first
   as the rollback point, in `.git/rollback-applications-board`.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was pulled' || { git rev-parse HEAD | sudo -u radar tee .git/rollback-applications-board && sudo -u radar git pull --ff-only && git log --oneline -3; }; }
   ```

5. Open the database by hand once. This makes the two tables and runs the
   one-time migration, and prints what it made.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, the database was not opened' || sudo -u radar /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; c = radar_common.get_db(); print('opened'); [print(r[0], r[1]) for r in c.execute('SELECT stage, COUNT(*) FROM applications GROUP BY stage')]; c.close()"; }
   ```

   Expect `opened`, then `applied` with the applied_roles count from BEFORE,
   and `preparing` with up to the saved_cvs count (a saved CV for a role you
   have applied for is in Applied, not Preparing).

6. Look at what the 21-day rule will do, without doing it. Read only.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar /opt/biz-admin/radar-venv/bin/python -c "import sys, sqlite3; sys.path.insert(0, 'scripts'); import applications as a; c = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); c.row_factory = sqlite3.Row; rows = [r for r in a.load(c) if r['stage'] == 'applied']; print(len(rows), 'in Applied,', sum(a.warned(r, None) for r in rows), 'showing the 21-day warning'); dates = sorted(a.close_on(r) for r in rows if a.close_on(r)); print('first close by the rule:', dates[0] if dates else 'none'); print('closes in the first week after the migration:', sum(1 for r in rows if a.close_on(r) and a.close_on(r) <= a.parse(r['created_at']) + a.timedelta(days=7)))"
   ```

   Every application already 21 days old closes on the first signals run 7
   days after this step, unless you move it, give it a next step or log a
   contact with the company before then. If you would rather keep some, look
   at the board after step 7 and move them.

7. Restart the dashboard.

   ```bash
   docker restart medtech-radar
   ```

8. Your call, any time: run the rule now rather than waiting for the next
   signals run. It writes (Follow up steps due, and any close the rule calls
   for), makes no model call and touches no role row.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, the rule did not run' || sudo -u radar /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common, applications; c = radar_common.get_db(); print(applications.run_rules(c)); c.close()"; }
   ```

   It prints `{'made': 0, 'closed': N, 'follow_ups': M}`. After the next
   signals run, its run record says the same (see AFTER).

9. Run AFTER, then open `/jobs?show=applied` in a browser at full width and on
   the phone. Move an item with Move to and press Undo in the message; press
   M on an item, then 3, then Z; set a next step and clear it; switch to List
   and back.

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code on the box'; git status --short; git log --oneline -3
echo '--- the tables and the migration flag (expect 1 1 1)'; Q "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'applications') AS applications, (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'application_events') AS events, (SELECT COUNT(*) FROM meta WHERE key = 'migrated_applications') AS migrated;"
echo '--- applications by stage'; Q "SELECT COALESCE(stage, 'off the board') AS stage, closed_reason, stage_by, COUNT(*) AS n FROM applications GROUP BY 1, 2, 3 ORDER BY 1;"
echo '--- every applied role has its application (expect 0)'; Q "SELECT COUNT(*) AS missing FROM (SELECT DISTINCT COALESCE('key:' || role_key, 'id:' || id) AS ref FROM opportunities WHERE applied_at IS NOT NULL) WHERE ref NOT IN (SELECT role_ref FROM applications);"
echo '--- no application in an applied column has lost its role stamp (expect 0)'; Q "SELECT COUNT(*) AS unstamped FROM applications a JOIN opportunities o ON o.id = a.opportunity_id WHERE a.stage IN ('applied', 'conversation', 'interviewing', 'offer') AND NOT EXISTS (SELECT 1 FROM opportunities x WHERE x.applied_at IS NOT NULL AND (x.id = o.id OR (o.role_key IS NOT NULL AND x.role_key = o.role_key)));"
echo '--- the history, by who and what'; Q "SELECT actor, kind, COUNT(*) AS n, MAX(at) AS latest FROM application_events GROUP BY 1, 2;"
echo '--- the role rows lifecycle, the same as BEFORE'; Q "SELECT COUNT(*) AS roles, SUM(applied_at IS NOT NULL) AS applied, SUM(closed_at IS NOT NULL) AS closed, SUM(expired_at IS NOT NULL) AS expired, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(acknowledged_at IS NOT NULL) AS acknowledged FROM opportunities;"
echo '--- touches, the same as BEFORE'; Q "SELECT COUNT(*) AS touches FROM touches;"
echo '--- the last signals runs, and what the rule did in them'; Q "SELECT ts, outcome, note FROM runs WHERE workflow = 'signals' ORDER BY id DESC LIMIT 3;"
echo '--- /health, the same verdict as BEFORE'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- the Board and the List (expect 200 1, 200 1)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/jobs?show=applied"; curl -s -m 15 "$B/jobs?show=applied" | grep -c 'id="board"'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/jobs?show=applied&layout=list"; curl -s -m 15 "$B/jobs?show=applied&layout=list" | grep -c 'id="queue"'
echo '--- the board fragment (expect 200)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/jobs?show=applied&part=board"
echo '--- the guard is in front of the new routes (enforce: 403; warn: 409, nothing written)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"id": 0, "to": "offer"}' $B/application/move
```

Expect `1 1 1`; applications in Applied and Preparing, all by radar on the
first day; `0` missing and `0` unstamped; the same role-row and touch counts
as BEFORE (the migration and the rule never change a role row); the signals
runs' note saying `board: N closed as no reply, M follow-ups added` once the
rule has done something; `200 1`, `200 1`, `200`; and `403` (enforce) or
`409` (warn).

## Rollback

Code only. The backup from step 2 is not restored: the two new tables are
left as they are, and the old code never reads them, so they do no harm and
the board comes back as it was if the branch is deployed again. Moves, next
steps and closes made meanwhile stay in them.

```bash
cd /opt/biz-admin/medtech-radar && test -s .git/rollback-applications-board && sudo -u radar git reset --hard "$(cat .git/rollback-applications-board)" && git log --oneline -1 && docker restart medtech-radar
```

The old code's Jobs page lists applied roles as before, and its Applied
count counts them, Closed board items included. While it is rolled back,
Mark applied and Undo applied do not update the board; if the branch goes
back on later, an application undone in that time still sits in Applied, and
moving it to Preparing or closing it as withdrawn tidies it.
