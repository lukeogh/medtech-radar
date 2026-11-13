# Brief 4 box checks: the CV page and the tailored CV

For LXC 110, the radar checkout at `/opt/biz-admin/medtech-radar`, run as the
`radar` user. Every query is read only. `test/run_cv_test.py` runs each query on
this page against seeded data, and the proof queries are shown there to catch
what they exist to catch. The `sqlite3` command line was not installed where
they were written, so they were run through Python's `sqlite3`, not the CLI.

What the deploy changes on the box:

- a new table, `tailored_cvs`, and one nullable column, `score_history.cv_hash`,
  added by the first open of the database (a third first-open event, after
  Briefs 2 and 3),
- python-docx, which the dashboard container installs from `requirements.txt`
  when it starts. Only the dashboard needs it, for Download. The LXC venv at
  `/opt/biz-admin/radar-venv` needs it only if the test suite is run there, and
  n8n's Python never does,
- nothing in `prompts/`, nothing in `config/profile/preferences.md`, and no n8n
  workflow.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
Q() { sudo -u radar sqlite3 -readonly -header -column db/radar.sqlite "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; git status --short; git log --oneline -1
echo '--- the dashboard container'; docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.State.Status}} {{.RestartCount}}' medtech-radar
echo '--- is anything running'; pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' || echo 'nothing running'
echo '--- Briefs 2, 3 and 5 are live (expect 4, 2, 3 and 1)'; Q "SELECT (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name IN ('score_basis','prompt_version','score_model','scored_at')) AS record_columns, (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name IN ('applied_at','applied_cv')) AS applied_columns, (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name IN ('description_fetched_at','description_hash','score_working')) AS prepare_columns, (SELECT COUNT(*) FROM sqlite_master WHERE name = 'score_history') AS history_table;"
echo '--- Brief 4 is not there yet (expect 0 and 0)'; Q "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'tailored_cvs') AS copies_table, (SELECT COUNT(*) FROM pragma_table_info('score_history') WHERE name = 'cv_hash') AS history_cv_hash;"
echo '--- row counts and history rows, to compare after'; Q "SELECT COUNT(*) AS roles, SUM(expired_at IS NOT NULL) AS expired, SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(applied_at IS NOT NULL) AS applied, (SELECT COUNT(*) FROM score_history) AS history_rows FROM opportunities;"
echo '--- the master CV the scorer reads now'; cat config/profile/cv.active; ls -l config/profile/ | grep -E 'cv-.*\.md|cv\.txt|cv\.pdf' | tail -5
echo '--- the Jobs page now: one Applied button and one Prepare link for each open role'; curl -s -m 15 $B/jobs | grep -o -E 'data-apply="|data-prepare="' | sort | uniq -c
echo '--- python-docx in the dashboard container (expect not installed before the deploy)'; docker exec medtech-radar python -c "import importlib.metadata as m; print('python-docx', m.version('python-docx'))" 2>&1 | tail -1
```

## The deploy, one block at a time

1. Run the BEFORE block. Expect a clean `git status`, `nothing running`, `4`,
   `2`, `3` and `1` for Briefs 2, 3 and 5, `0` and `0` for Brief 4, and equal
   Applied and Prepare counts.

2. Back up first.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar bash scripts/backup_radar.sh
   ```

3. Merge the branch on GitHub. Then on the box, see what will arrive.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar git fetch origin && git log --oneline HEAD..origin/main
   ```

4. Pull.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar git pull --ff-only && git log --oneline -3
   ```

5. Between inbox runs, check nothing is running.

   ```bash
   pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' || echo 'nothing running'
   ```

6. Open the database by hand with the venv's Python. This adds the table and
   the column, and needs the write lock for a moment, which an inbox run holds
   for a whole email.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; radar_common.get_db().close(); print('opened')"
   ```

7. Restart the dashboard. Its container installs `requirements.txt` as it
   starts, which brings python-docx and lxml from PyPI. n8n takes the scripts
   on its next run with no restart.

   ```bash
   docker restart medtech-radar
   ```

8. Check python-docx arrived in the container. Expect `python-docx 1.2.x`.

   ```bash
   docker exec medtech-radar python -c "import importlib.metadata as m; print('python-docx', m.version('python-docx'))"
   ```

   Only if it says it is not installed, install it inside the running container
   from the repository's own `requirements.txt`, then restart once more.

   ```bash
   docker exec medtech-radar sh -c 'S=$(find / -name radar_common.py -path "*/scripts/*" 2>/dev/null | head -1); pip install -r "$(dirname "$(dirname "$S")")/requirements.txt"' && docker restart medtech-radar
   ```

9. Run the AFTER block.

10. In your browser, through the proxy name, open the CV page first. This step
    has a decision in it. If the page says the version was not written by this
    editor, no role's Prepare application page will make a tailored copy until
    the master has been saved here once, because a copy's line numbers would
    not be the scorer's. Read the master in the editor, line by line against
    your Word CV, fix anything the import read wrongly, and press Save as a new
    version. That makes a new master version, which the scorer reads from then
    on, and the CV page's After a change section says what a new version means
    for scores already made. If you would rather not change the master yet,
    stop here: everything else on the box works as before, and the Prepare
    pages show the master read only with a link to the CV page.

    Then open one open LinkedIn role's Prepare application page: the CV is on
    the left and the copy is made as the page opens. Edit a line, press
    Rescore role, then Save CV and Download. Then run the click block.

## AFTER, before any click

```bash
cd /opt/biz-admin/medtech-radar
Q() { sudo -u radar sqlite3 -readonly -header -column db/radar.sqlite "$1"; }
B=http://192.168.1.100:8787
echo '--- code on the box'; git status --short; git log --oneline -3
echo '--- the dashboard container, and the install it made as it started'; docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.State.Status}} {{.RestartCount}}' medtech-radar; docker logs --tail 40 medtech-radar 2>&1 | grep -i -E 'python-docx|lxml|Successfully installed|error|Write guard' | tail -8
echo '--- python-docx in the container (expect 1.2.x)'; docker exec medtech-radar python -c "import importlib.metadata as m; print('python-docx', m.version('python-docx'))" 2>&1 | tail -1
echo '--- the file is sound'; Q "PRAGMA integrity_check;"
echo '--- row counts and history rows, the same query as before the deploy'; Q "SELECT COUNT(*) AS roles, SUM(expired_at IS NOT NULL) AS expired, SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(applied_at IS NOT NULL) AS applied, (SELECT COUNT(*) FROM score_history) AS history_rows FROM opportunities;"
echo '--- the new table and column are there (expect 1, 13 and 1)'; Q "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'tailored_cvs') AS copies_table, (SELECT COUNT(*) FROM pragma_table_info('tailored_cvs')) AS copy_columns, (SELECT COUNT(*) FROM pragma_table_info('score_history') WHERE name = 'cv_hash') AS history_cv_hash;"
echo '--- no copy and no tailored score yet (expect 0 and 0)'; Q "SELECT (SELECT COUNT(*) FROM tailored_cvs) AS copies, (SELECT COUNT(*) FROM score_history WHERE event = 'tailored') AS tailored_scores;"
echo '--- the Jobs page: the two counts must still be equal'; curl -s -m 15 $B/jobs | grep -o -E 'data-apply="|data-prepare="' | sort | uniq -c
echo '--- the CV page: 200, the editor and the relabelled import'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/cv; curl -s -m 15 $B/cv | grep -o -E 'id="cv-editor"|One-off import from a Word file' | sort | uniq -c
ID=$(sudo -u radar sqlite3 -readonly db/radar.sqlite "SELECT id FROM opportunities o WHERE source_url LIKE '%linkedin.com/%jobs/view/%' AND acknowledged_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND applied_at IS NULL AND NOT (role_key IS NOT NULL AND role_key IN (SELECT role_key FROM opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL)) ORDER BY id DESC LIMIT 1;"); echo "role $ID"
echo '--- its Prepare page by curl, which runs no script, so no copy is made: 200 and the editor'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/prepare/$ID"; curl -s -m 15 "$B/prepare/$ID" | grep -o -E 'id="cv-editor"|id="cv-pane"' | sort | uniq -c
echo '--- is the master in the editor layout yet (1 means save it once on the CV page, step 10; 0 means it is)'; curl -s -m 15 "$B/prepare/$ID" | grep -c 'has not been saved on the CV page'
echo '--- Download for a role with nothing saved is a 404, a foreign Host a 403'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/prepare/$ID/cv.docx"; curl -s -m 15 -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' "$B/prepare/$ID/cv.docx"
echo '--- and opening it wrote nothing (expect 0 and 0, and the same row counts)'; Q "SELECT (SELECT COUNT(*) FROM tailored_cvs) AS copies, (SELECT COUNT(*) FROM score_history WHERE event = 'tailored') AS tailored_scores;"; Q "SELECT COUNT(*) AS roles, SUM(expired_at IS NOT NULL) AS expired, SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(applied_at IS NOT NULL) AS applied, (SELECT COUNT(*) FROM score_history) AS history_rows FROM opportunities;"
```

Expect `ok`, the same row counts as BEFORE, `1`, `13` and `1`, `0` and `0`,
equal Jobs counts, `200` and both markers on the CV page, `200` and both
markers on the Prepare page, `1` or `0` for the layout (step 10 says what to do
with a `1`), `404` then `403`, and `0` and `0` again. If `role` is empty there
is no open LinkedIn role to try, and the Prepare lines have nothing to show.

## The click block, after your first real use

```bash
cd /opt/biz-admin/medtech-radar
Q() { sudo -u radar sqlite3 -readonly -header -column db/radar.sqlite "$1"; }
echo '--- the copies, newest first'; Q "SELECT id, role_ref, opportunity_id, master_version, cv_hash, LENGTH(cv_text) AS chars, saved_name, saved_at FROM tailored_cvs ORDER BY id DESC LIMIT 10;"
echo '--- the tailored scores, newest first'; Q "SELECT id, opportunity_id, recorded_at, cv_match, tier, description_hash, cv_hash FROM score_history WHERE event = 'tailored' ORDER BY id DESC LIMIT 10;"
echo '--- what Rescore role has cost'; Q "SELECT COUNT(*) AS calls, COALESCE(SUM(items_new), 0) AS stored, COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens FROM runs WHERE workflow = 'prepare' AND note LIKE '%tailored copy%';"
echo '--- no pair of description and copy was scored twice for a role (expect 0)'; Q "SELECT COUNT(*) AS pairs_scored_twice FROM (SELECT COALESCE('key:' || o.role_key, 'id:' || o.id) AS role, h.description_hash, h.cv_hash FROM score_history h JOIN opportunities o ON o.id = h.opportunity_id WHERE h.event = 'tailored' GROUP BY 1, 2, 3 HAVING COUNT(*) > 1);"
echo '--- no tailored score ever landed on a role (expect 0)'; Q "SELECT COUNT(*) AS roles_with_a_tailored_score FROM opportunities WHERE cv_version LIKE 'tailored-%';"
echo '--- no role was scored or saved for after it was applied (expect 0)'; Q "WITH chain AS (SELECT role_key, MIN(applied_at) AS first_applied FROM opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL GROUP BY role_key) SELECT COUNT(*) AS tailored_after_applying FROM score_history h JOIN opportunities o ON o.id = h.opportunity_id LEFT JOIN chain c ON c.role_key = o.role_key WHERE h.event = 'tailored' AND COALESCE(c.first_applied, o.applied_at) IS NOT NULL AND h.recorded_at >= COALESCE(c.first_applied, o.applied_at);"
echo '--- no copy was saved or changed after its role was applied (expect 0 and 0)'; Q "WITH applied AS (SELECT COALESCE('key:' || role_key, 'id:' || id) AS role, MIN(applied_at) AS first_applied FROM opportunities WHERE applied_at IS NOT NULL GROUP BY 1) SELECT COALESCE(SUM(t.saved_at > a.first_applied), 0) AS saved_after_applying, COALESCE(SUM(t.updated_at > a.first_applied), 0) AS changed_after_applying FROM tailored_cvs t JOIN applied a ON a.role = t.role_ref;"
echo '--- applied roles and the CV recorded with them'; Q "SELECT id, title, applied_at, applied_cv FROM opportunities WHERE applied_at IS NOT NULL ORDER BY applied_at DESC LIMIT 5;"
echo '--- the master CV versions, if you saved it on the CV page'; cat config/profile/cv.active; ls -l config/profile/ | grep -E 'cv-.*\.md' | tail -3
```

Expect a copy for each role you opened, a tailored score for each Rescore role
that called the model, `0` on the three proof queries and `0` and `0` on the
fourth, and the saved copy's name beside any role you marked applied after
saving, or an empty `applied_cv` where nothing was saved.

## Rollback

Rollback is code only. Revert the merge on main, push, then on the box pull and
restart. The table, the column, every copy and every tailored score stay in the
database, and old code ignores them. Old code's Re-score stale items reaches
full description scores again. Don't restore the pre-deploy backup: it would
drop every copy and every score made since. In the commands below, `<merge>` is
the merge commit's hash.

```bash
# on a clone of the repo, then push
git revert -m 1 <merge> && git push origin main
```

```bash
cd /opt/biz-admin/medtech-radar && sudo -u radar git pull --ff-only && docker restart medtech-radar
```

A rollback does not move the master CV marker. If you saved the master on the
CV page and want the scorer back on the version before, list the versions and
put the old file name in the marker, as `radar`.

```bash
cd /opt/biz-admin/medtech-radar/config/profile && ls -1 cv-*.md && cat cv.active
```

```bash
cd /opt/biz-admin/medtech-radar/config/profile && echo 'cv-<the old stamp>.md' | sudo -u radar tee cv.active
```
