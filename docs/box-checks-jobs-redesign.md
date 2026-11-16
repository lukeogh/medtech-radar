# Box checks: the Jobs page redesign (branch claude/jobs-redesign)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. BEFORE and AFTER are read only: they change
nothing in the database, the checkout or n8n. The one POST in AFTER names a
role that does not exist, so nothing can be written by it in either guard mode.

What the change touches on the box: the dashboard's pages (the new top bar on
every page, the new Jobs page) and one new nullable column, `read_at`, with a
one-time stamp of every row on file. No prompt, no scorer, no n8n workflow and
no write-guard setting changes. The inbox, the advert checker and the watcher
run the same code paths as before; the first of them to open the database
after the pull would add the column, so step 5 adds it by hand first, at a
moment you choose.

Run them as the user that normally reads the database, `radar` or one that can
read `db/radar.sqlite` and use docker.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; git status --short; git log --oneline -1
echo '--- the dashboard container'; docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.State.Status}} {{.RestartCount}} {{.State.StartedAt}}' medtech-radar
echo '--- is anything running'; pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' || echo 'nothing running'
echo '--- the write guard mode (keep it as it is)'; grep -E '^dashboard_guard' config/radar.yaml
echo '--- read_at is not there yet (expect 0 and 0)'; Q "SELECT (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name = 'read_at') AS read_at_column, (SELECT COUNT(*) FROM meta WHERE key = 'migrated_read_at') AS read_flag;"
echo '--- rows by state, to compare after'; Q "SELECT COUNT(*) AS rows, SUM(acknowledged_at IS NOT NULL) AS acknowledged, SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(expired_at IS NOT NULL) AS expired, SUM(applied_at IS NOT NULL) AS applied FROM opportunities;"
echo '--- open rows by tier, to compare after'; Q "SELECT COALESCE(tier, 'unscored') AS tier, COUNT(*) AS rows FROM opportunities WHERE acknowledged_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND applied_at IS NULL GROUP BY 1 ORDER BY 1;"
echo '--- applied roles, to compare after'; Q "SELECT id, applied_at, applied_cv, tier FROM opportunities WHERE applied_at IS NOT NULL ORDER BY id;"
echo '--- /health'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- the Jobs page today: 200, the old location filter 1, the new page 0'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/jobs; curl -s -m 15 $B/jobs | grep -c 'id="loc-filter"'; curl -s -m 15 $B/jobs | grep -c '<div class="app" data-page="jobs"'
echo '--- the old fifteen-minute reload is on the Jobs page today (expect 1)'; curl -s -m 15 $B/jobs | grep -c '900000'
```

Expect a clean `git status`, `nothing running`, `0` and `0` for `read_at`,
`200`, `1`, `0` and `1` for the Jobs page. Keep the rest of the output: the
row counts, the tiers, the applied roles and `/health` are what AFTER is
compared with. Note the guard mode line; nothing here changes it.

## The deploy, one block at a time

1. Run the BEFORE block. Expect what it says above.

2. Back up first.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar bash scripts/backup_radar.sh
   ```

3. Merge the branch on GitHub. Then on the box, see what will arrive. Expect
   this change and nothing you have not deployed before.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar git fetch origin && git log --oneline HEAD..origin/main
   ```

4. Pull, only when nothing is running. While something is, the line says so
   and pulls nothing: wait for it to finish and run the line again.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' && echo 'wait, something is running, nothing was pulled' || { sudo -u radar git pull --ff-only && git log --oneline -3; }; }
   ```

5. Straight after, open the database by hand with the venv's Python. This
   adds `read_at` and stamps every row on file read, once, behind its flag, so
   nothing shows as New at switch-on. It takes the write lock for a moment, so
   it too waits for nothing to be running.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' && echo 'wait, something is running, the database was not opened' || sudo -u radar /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; radar_common.get_db().close(); print('opened')"; }
   ```

6. Restart the dashboard, which serves the new pages. n8n needs nothing.

   ```bash
   docker restart medtech-radar
   ```

7. Run the AFTER block.

8. In a browser, open the Jobs page through the proxy and by the LAN address,
   at full width and on the phone. Press J to open the first role, A to
   acknowledge it, then Undo last; the role should leave the list and come
   back, with no page reload. Press ? for the list of keys. Leave a role open
   for two seconds and its New word, if it had one, goes.

   ```bash
   echo "$(python3 -c 'import sys; sys.path.insert(0, "scripts"); import radar_common, build_digest; print(build_digest.dashboard_url(radar_common.load_config()))')/jobs"; echo 'http://192.168.1.100:8787/jobs'
   ```

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code on the box'; git status --short; git log --oneline -3
echo '--- the dashboard started again (expect a time after the restart in step 6)'; docker inspect -f '{{.State.Status}} {{.State.StartedAt}}' medtech-radar
echo '--- read_at is there, its flag is set, and every row on file is read (expect 1, 1, 0)'; Q "SELECT (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name = 'read_at') AS read_at_column, (SELECT COUNT(*) FROM meta WHERE key = 'migrated_read_at') AS read_flag, (SELECT COUNT(*) FROM opportunities WHERE read_at IS NULL AND first_seen <= (SELECT MIN(read_at) FROM opportunities)) AS unread_from_before;"
echo '--- rows by state, the same as BEFORE'; Q "SELECT COUNT(*) AS rows, SUM(acknowledged_at IS NOT NULL) AS acknowledged, SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(expired_at IS NOT NULL) AS expired, SUM(applied_at IS NOT NULL) AS applied FROM opportunities;"
echo '--- open rows by tier, the same as BEFORE'; Q "SELECT COALESCE(tier, 'unscored') AS tier, COUNT(*) AS rows FROM opportunities WHERE acknowledged_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND applied_at IS NULL GROUP BY 1 ORDER BY 1;"
echo '--- applied roles, the same as BEFORE'; Q "SELECT id, applied_at, applied_cv, tier FROM opportunities WHERE applied_at IS NOT NULL ORDER BY id;"
echo '--- /health, the same verdict as BEFORE'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
echo '--- the new Jobs page: 200, new page 1, old filter 0, no reload 0'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/jobs; curl -s -m 15 $B/jobs | grep -c '<div class="app" data-page="jobs"'; curl -s -m 15 $B/jobs | grep -c 'id="loc-filter"'; curl -s -m 15 $B/jobs | grep -c '900000'
ID=$(Q "SELECT id FROM opportunities WHERE tier = 'top' AND acknowledged_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND applied_at IS NULL ORDER BY id DESC LIMIT 1;" | sed -n 2p); echo "role $ID"
echo '--- one role by its address, the page and the pane (expect 200, 200, and the role id)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/jobs/$ID; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/jobs/$ID?part=role"; curl -s -m 15 "$B/jobs/$ID?part=role" | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["id"])'
echo '--- the views (expect 200 four times)'; for v in open acknowledged filtered applied; do curl -s -m 15 -o /dev/null -w "$v %{http_code}\n" "$B/jobs?show=$v"; done
echo '--- the new-roles notice reads the newest row (expect a stamp and new 0)'; curl -s -m 15 "$B/jobs?part=stamp&since=$(Q 'SELECT MAX(id) AS m FROM opportunities;' | sed -n 2p)"; echo
echo '--- every page has the top bar and no Watchlist link (expect 1 and 0 for each)'; for p in / /jobs /insights /cv /archive; do printf '%s ' "$p"; curl -s -m 15 "$B$p" | grep -c 'class="appbar"' | tr '\n' ' '; curl -s -m 15 "$B$p" | grep -c 'Watchlist'; done
echo '--- the guard still stands in front of the new route (enforce: 403; warn: 409, no such role, nothing written)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"id": 0}' $B/read
```

Expect the commit you merged on top, a fresh start time, `1`, `1` and `0` for
`read_at`, row counts, tiers and applied roles exactly as BEFORE, the same
`/health` verdict, then `200`, `1`, `0`, `0` for the page, `200` `200` and the
role id, `200` for each view, a stamp with `"new": 0`, `1` and `0` for each
page, and `403` (enforce) or `409` (warn) for the last line.

## Rollback

Rollback is code only. Revert the merge on main, push, then on the box pull
and restart the dashboard. The old Jobs page and the old page tops come back
with the code. The `read_at` column and its flag stay in the database, and old
code ignores them. If the new code is deployed again later, every role that
arrived while it was rolled back reads as New, because the one-time stamp has
already run; acknowledge them or let them be. Never restore the pre-deploy
backup: it would drop every score, sighting and click made since. In the
commands below, `<merge>` is the merge commit's hash.

```bash
# on a clone of the repo, then push
git revert -m 1 <merge> && git push origin main
```

```bash
cd /opt/biz-admin/medtech-radar && sudo -u radar git pull --ff-only && docker restart medtech-radar
```
