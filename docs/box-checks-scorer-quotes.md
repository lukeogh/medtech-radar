# Scorer quotes box checks: the reasons a score gives, with their quotes

For LXC 110, the radar checkout at `/opt/biz-admin/medtech-radar`, run as the
`radar` user. Every query is read only. `test/run_quotes_test.py` runs each
query on this page against seeded data, as it is written here, and shows each
proof query catching what it exists to catch. No `sqlite3` command line is
assumed: `Q` runs a query through Python's `sqlite3` in the venv at
`/opt/biz-admin/radar-venv`, on a read-only connection, and prints a header
line and then one line a row, the columns split by ` | `.

What the deploy changes on the box:

- two nullable columns, `opportunities.score_reasons` and
  `score_history.reasons`, added by the first open of the database, a fourth
  first-open event after Briefs 2, 3 and 4. Nothing fills them for a score
  already made: every row and every history row on the box keeps them NULL,
  and nothing is re-scored,
- `prompts/scorer.md`, which asks for up to four reasons, each with a quote of
  25 words or fewer from the advert, so the prompt version moves from
  `97f11cce` to `22353628`. n8n's next inbox run reads the new prompt and the
  new scripts with no restart,
- the ceilings: a card's score has its own, `CARD_MAX_TOKENS`, 2,048, a full
  description's `SPEC_MAX_TOKENS` is 6,144, and the extractor and the pay
  backfill keep `MAX_TOKENS`, 1,024,
- the expanded Jobs row and the Prepare application page list the reasons and
  their quotes, with one line under them where code has judged the gates again
  since the reasons were given,
- nothing in `config/profile/preferences.md`, no new package and no n8n
  workflow.

If the BEFORE block says `0` for `copies_table`, Brief 4 is not on the box yet
and this pull brings it too. Deploy it first, by `docs/box-checks-brief-4.md`,
and come back here. That page's blocks define `Q` with the `sqlite3` command
line, which is not assumed here. Paste each of its blocks with its `Q() {` line
replaced by this page's `PY=` and `Q() {` lines, from the BEFORE block below,
and its role line, the one that starts `ID=$(sudo -u radar sqlite3`, replaced by
this one, which picks the same role through `Q`:

```bash
ID=$(Q "SELECT id FROM opportunities o WHERE source_url LIKE '%linkedin.com/%jobs/view/%' AND acknowledged_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND applied_at IS NULL AND NOT (role_key IS NOT NULL AND role_key IN (SELECT role_key FROM opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL)) ORDER BY id DESC LIMIT 1;" | sed -n 2p); echo "role $ID"
```

The rows `Q` prints have their columns split by ` | ` where that page shows
them in columns, and the figures to expect are the same.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; git status --short; git log --oneline -1
echo '--- the dashboard container'; docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.State.Status}} {{.RestartCount}}' medtech-radar
echo '--- is anything running'; pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' || echo 'nothing running'
echo '--- the prompt the box scores with now (expect 97f11cce)'; sudo -u radar "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; print(radar_common.prompt_version())"
echo '--- SQLite can read the reasons when they come (expect json_ok 1)'; Q "SELECT sqlite_version() AS sqlite, json_valid('[]') AS json_ok;"
echo '--- Brief 4 is on the box, so the pull brings only this change (expect 1 and 1)'; Q "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'tailored_cvs') AS copies_table, (SELECT COUNT(*) FROM pragma_table_info('score_history') WHERE name = 'cv_hash') AS history_cv_hash;"
echo '--- the reasons columns are not there yet (expect 0 and 0)'; Q "SELECT (SELECT COUNT(*) FROM pragma_table_info('opportunities') WHERE name = 'score_reasons') AS on_opportunities, (SELECT COUNT(*) FROM pragma_table_info('score_history') WHERE name = 'reasons') AS on_history;"
echo '--- row counts and history rows, to compare after'; Q "SELECT COUNT(*) AS roles, SUM(expired_at IS NOT NULL) AS expired, SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(applied_at IS NOT NULL) AS applied, (SELECT COUNT(*) FROM score_history) AS history_rows FROM opportunities;"
echo '--- the scores held now, to compare after'; Q "SELECT COUNT(*) AS scored_rows, COUNT(DISTINCT prompt_version) AS prompt_versions, SUM(cv_match) AS cv_sum, SUM(LENGTH(one_line_why)) AS why_chars, MAX(scored_at) AS newest_score FROM opportunities WHERE tier IS NOT NULL AND COALESCE(prompt_version, '') <> '22353628';"
echo '--- scores by basis and prompt version, to compare after'; Q "SELECT COALESCE(score_basis, 'unscored') AS basis, COALESCE(prompt_version, 'none') AS prompt, COUNT(*) AS roles FROM opportunities GROUP BY 1, 2 ORDER BY 1, 2;"
echo '--- applied roles and the score each holds, to compare after'; Q "SELECT id, applied_at, score_basis, prompt_version, scored_at, tier, cv_match FROM opportunities WHERE applied_at IS NOT NULL ORDER BY id;"
echo '--- every row of an applied role, to compare after'; Q "SELECT COUNT(*) AS rows_on_applied_roles, SUM(cv_match) AS cv_sum, MAX(scored_at) AS newest_score FROM opportunities WHERE (applied_at IS NOT NULL OR (role_key IS NOT NULL AND role_key IN (SELECT role_key FROM opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL)));"
echo '--- what scoring cost in the last seven days, to compare with the week after'; Q "SELECT workflow, COUNT(*) AS runs, SUM(items_new) AS new_items, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens FROM runs WHERE ts >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days') AND workflow IN ('inbox', 'prepare', 'rescore') GROUP BY 1 ORDER BY 1;"
echo '--- the Jobs page lists no reasons yet (expect 200 and 0)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/jobs; curl -s -m 15 $B/jobs | grep -o 'class="reasons"' | wc -l
echo '--- the dashboard runs the code from before the deploy (expect 0)'; curl -s -m 15 $B/jobs | grep -c '\.reasons{'
```

Expect a clean `git status`, `nothing running`, `97f11cce`, `json_ok` 1, `1`
and `1` for Brief 4, `0` and `0` for the reasons columns, `200` and `0` for
the Jobs page, and `0` for the dashboard's code: the style for the reasons is
not in the page it serves yet. Keep the rest of the output: the row counts,
the scores held, the applied roles and the costs are what AFTER is compared
with.

## The deploy, one block at a time

1. Run the BEFORE block. Expect what it says above. A `0` for `copies_table`
   means deploy Brief 4 first, as above.

2. Back up first.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar bash scripts/backup_radar.sh
   ```

3. Merge the branch on GitHub. Then on the box, see what will arrive. Expect
   the quoted reasons and nothing you have not deployed before.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar git fetch origin && git log --oneline HEAD..origin/main
   ```

Run steps 4, 5 and 6 back to back, between inbox runs, and press no Prepare
application and no Rescore role from the pull until the restart in step 6 is
done. The prompt is read from disk each time a score is set up, while code
already loaded keeps running, so in that window the dashboard would score with
the new prompt and its old code. Such a score is stamped `22353628` and stores
no reasons, and a full description scored that way is never scored again, so
that role would never get its reasons. An inbox run already under way when the
pull lands would do the same to its adverts, which is why the pull waits for
nothing to be running.

4. Pull, only when nothing is running. While something is, the line says so
   and pulls nothing: wait for it to finish and run the line again.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' && echo 'wait, something is running, nothing was pulled' || { sudo -u radar git pull --ff-only && git log --oneline -3; }; }
   ```

5. Straight after, open the database by hand with the venv's Python. This
   adds the two columns and fills nothing, and needs the write lock for a
   moment, which an inbox run holds for a whole email, so it too waits for
   nothing to be running.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -af 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill_pay' && echo 'wait, something is running, the database was not opened' || sudo -u radar /opt/biz-admin/radar-venv/bin/python -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; radar_common.get_db().close(); print('opened')"; }
   ```

6. Straight after, restart the dashboard, which draws the reasons and makes
   the Prepare application scores. n8n takes the scripts and the prompt on its
   next run with no restart.

   ```bash
   docker restart medtech-radar
   ```

7. Run the AFTER block. Prepare application and Rescore role may be pressed
   again once it shows the dashboard on the new code.

8. Let the next inbox run store a new advert, and press Prepare application
   on one open LinkedIn role whose description has not been scored. If you
   use Rescore role on its tailored copy too, its score is counted as well.
   Then run the new scores block.

## AFTER, before any new score

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code on the box'; git status --short; git log --oneline -3
echo '--- the dashboard container'; docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.State.Status}} {{.RestartCount}}' medtech-radar
echo '--- the prompt the box scores with now (expect 22353628)'; sudo -u radar "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; print(radar_common.prompt_version())"
echo '--- the file is sound'; Q "PRAGMA integrity_check;"
echo '--- row counts and history rows, the same query as before the deploy'; Q "SELECT COUNT(*) AS roles, SUM(expired_at IS NOT NULL) AS expired, SUM(closed_at IS NOT NULL) AS closed, SUM(dismissed_at IS NOT NULL) AS dismissed, SUM(applied_at IS NOT NULL) AS applied, (SELECT COUNT(*) FROM score_history) AS history_rows FROM opportunities;"
echo '--- the two columns are there, TEXT, nullable and with no default (expect two rows, notnull 0 and dflt_value empty)'; Q "SELECT 'opportunities' AS in_table, * FROM pragma_table_info('opportunities') WHERE name = 'score_reasons' UNION ALL SELECT 'score_history', * FROM pragma_table_info('score_history') WHERE name = 'reasons';"
echo '--- no score carries reasons yet (expect 0 and 0)'; Q "SELECT (SELECT COUNT(*) FROM opportunities WHERE score_reasons IS NOT NULL) AS rows_with_reasons, (SELECT COUNT(*) FROM score_history WHERE reasons IS NOT NULL) AS history_with_reasons;"
echo '--- the scores held, the same numbers as before the deploy'; Q "SELECT COUNT(*) AS scored_rows, COUNT(DISTINCT prompt_version) AS prompt_versions, SUM(cv_match) AS cv_sum, SUM(LENGTH(one_line_why)) AS why_chars, MAX(scored_at) AS newest_score FROM opportunities WHERE tier IS NOT NULL AND COALESCE(prompt_version, '') <> '22353628';"
echo '--- applied roles and the score each holds, the same as before'; Q "SELECT id, applied_at, score_basis, prompt_version, scored_at, tier, cv_match FROM opportunities WHERE applied_at IS NOT NULL ORDER BY id;"
echo '--- every row of an applied role, the same as before and with no reasons'; Q "SELECT COUNT(*) AS rows_on_applied_roles, SUM(cv_match) AS cv_sum, MAX(scored_at) AS newest_score, SUM(score_reasons IS NOT NULL) AS with_reasons FROM opportunities WHERE (applied_at IS NOT NULL OR (role_key IS NOT NULL AND role_key IN (SELECT role_key FROM opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL)));"
echo '--- the Jobs page, and no reasons on it until a new score lands (expect 200 and 0)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/jobs; curl -s -m 15 $B/jobs | grep -o 'class="reasons"' | wc -l
echo '--- the dashboard runs the new code (expect 1)'; curl -s -m 15 $B/jobs | grep -c '\.reasons{'
echo '--- and started again for it (expect a time after the restart in step 6)'; docker inspect -f '{{.State.StartedAt}}' medtech-radar
```

Expect `22353628`, `ok`, the same row counts as BEFORE, two column rows reading
`TEXT`, `0` under `notnull` and nothing under `dflt_value`, `0` and `0` for the
reasons, the same scores held as BEFORE to the last figure, the same applied
roles with the same scores, the same rows of applied roles with `0` reasons,
`200` and `0` for the Jobs page, `1` for the dashboard's code, and a start time,
in UTC, after the restart. A `0` for the code means the dashboard still serves the code
from before the deploy: run step 6 again, and press nothing until this line
reads `1`. The `RestartCount` above does not move for a restart by hand, so the
start time is the one to read. An inbox run that landed between the deploy and
this block may have stored a new advert with reasons already; then the row
counts are up by what it stored and the reasons are not `0`, and the scores
held from before still read as BEFORE.

## New scores, after the next inbox run and one Prepare application click

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- new scores by basis (expect with_reasons equal to scores on each line)'; Q "SELECT score_basis AS basis, COUNT(*) AS scores, SUM(score_reasons IS NOT NULL) AS with_reasons FROM opportunities WHERE prompt_version = '22353628' GROUP BY 1 ORDER BY 1;"
echo '--- every new score carries the reasons made with it (expect 0)'; Q "SELECT COUNT(*) AS new_scores_without_their_reasons FROM opportunities o WHERE o.prompt_version = '22353628' AND NOT EXISTS (SELECT 1 FROM json_tree(o.score_reasons) t WHERE t.key = 'scored_at' AND t.atom = o.scored_at);"
echo '--- new scores with no reasons, which only old code with the new prompt makes (expect the header and no rows)'; Q "SELECT id, score_basis, scored_at FROM opportunities WHERE prompt_version = '22353628' AND score_reasons IS NULL ORDER BY scored_at, id;"
echo '--- the reasons the scores on the rows gave, and their quotes kept and dropped, by basis'; Q "SELECT o.score_basis AS basis, SUM(t.key = 'reason') AS reasons, SUM(t.key = 'quote' AND t.type = 'text') AS quotes_kept, SUM(t.key = 'quote_dropped' AND t.atom = 'not_found') AS not_found, SUM(t.key = 'quote_dropped' AND t.atom = 'too_long') AS too_long, SUM(t.key = 'quote_dropped' AND t.atom = 'empty') AS empty, SUM(t.key = 'quote_dropped' AND t.atom = 'not_text') AS not_text, SUM(CASE WHEN t.key = 'dropped' THEN t.atom ELSE 0 END) AS entries_left_out FROM opportunities o, json_tree(o.score_reasons) t WHERE o.score_reasons IS NOT NULL GROUP BY 1 ORDER BY 1;"
echo '--- the history keeps them (expect with_reasons equal to rows)'; Q "SELECT event, writer, score_basis AS basis, COUNT(*) AS rows, SUM(reasons IS NOT NULL) AS with_reasons FROM score_history WHERE prompt_version = '22353628' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;"
echo '--- the quotes the history keeps, kept and dropped, by event and basis'; Q "SELECT h.event, h.score_basis AS basis, SUM(t.key = 'reason') AS reasons, SUM(t.key = 'quote' AND t.type = 'text') AS quotes_kept, SUM(t.key = 'quote_dropped' AND t.atom = 'not_found') AS not_found, SUM(t.key = 'quote_dropped' AND t.atom = 'too_long') AS too_long, SUM(t.key = 'quote_dropped' AND t.atom = 'empty') AS empty, SUM(t.key = 'quote_dropped' AND t.atom = 'not_text') AS not_text, SUM(CASE WHEN t.key = 'dropped' THEN t.atom ELSE 0 END) AS entries_left_out FROM score_history h, json_tree(h.reasons) t WHERE h.reasons IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2;"
echo '--- no score made before the deploy carries reasons (expect 0 and 0)'; Q "SELECT (SELECT COUNT(*) FROM opportunities WHERE COALESCE(prompt_version, '') <> '22353628' AND score_reasons IS NOT NULL) AS old_rows_with_reasons, (SELECT COUNT(*) FROM score_history WHERE COALESCE(prompt_version, '') <> '22353628' AND reasons IS NOT NULL) AS old_history_with_reasons;"
echo '--- the scores made before the deploy, as before less any you re-scored since'; Q "SELECT COUNT(*) AS scored_rows, COUNT(DISTINCT prompt_version) AS prompt_versions, SUM(cv_match) AS cv_sum, SUM(LENGTH(one_line_why)) AS why_chars, MAX(scored_at) AS newest_score FROM opportunities WHERE tier IS NOT NULL AND COALESCE(prompt_version, '') <> '22353628';"
echo '--- applied roles and the score each holds, the same as before'; Q "SELECT id, applied_at, score_basis, prompt_version, scored_at, tier, cv_match FROM opportunities WHERE applied_at IS NOT NULL ORDER BY id;"
echo '--- no row of an applied role was scored after the role was applied, reposts apart (expect 0)'; Q "WITH chain AS (SELECT role_key, MIN(applied_at) AS first_applied FROM opportunities WHERE applied_at IS NOT NULL AND role_key IS NOT NULL GROUP BY role_key) SELECT COUNT(*) AS rescored_after_applying FROM opportunities o LEFT JOIN chain c ON c.role_key = o.role_key WHERE COALESCE(c.first_applied, o.applied_at) IS NOT NULL AND o.first_seen <= COALESCE(c.first_applied, o.applied_at) AND o.scored_at > COALESCE(c.first_applied, o.applied_at);"
echo '--- what the new scores have cost, per run, to compare with the week before'; Q "SELECT workflow, COUNT(*) AS runs, SUM(items_new) AS new_items, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens FROM runs WHERE ts >= (SELECT MIN(scored_at) FROM opportunities WHERE prompt_version = '22353628') AND workflow IN ('inbox', 'prepare', 'rescore') GROUP BY 1 ORDER BY 1;"
echo '--- every quote kept is in the text stored for its role now (expect not in the stored text 0)'
sudo -u radar "$PY" - <<'PY'
import sqlite3, sys
sys.path.insert(0, "scripts")
import radar_common as rc
c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True)
c.row_factory = sqlite3.Row
kept = missing = unchecked = 0
for row in c.execute("SELECT * FROM opportunities WHERE score_reasons IS NOT NULL"):
    record = rc.reasons_record(row["score_reasons"], row["scored_at"])
    if record is None:
        continue
    if row["score_basis"] == "spec":
        if row["description_hash"] != rc.description_version(row["description_text"]):
            unchecked += 1
            continue
        sources = [row["description_text"] or ""]
    else:
        sources = [row[f] or "" for f in ("title", "company", "location", "salary_rate")]
    for item in record["reasons"]:
        if item.get("quote"):
            kept += 1
            if not any(rc.find_verbatim(item["quote"], s, whole_words=True) for s in sources):
                missing += 1
                print("not in the stored text:", row["id"], item["quote"][:80])
print("quotes kept", kept, "not in the stored text", missing,
      "rows whose description has changed since", unchecked)
PY
ID=$(Q "SELECT id FROM opportunities WHERE score_basis = 'spec' AND prompt_version = '22353628' ORDER BY scored_at DESC, id DESC LIMIT 1;" | sed -n 2p); echo "role $ID"
echo '--- its Prepare page lists the reasons (expect 200, then 1, or 2 once Rescore role has scored its copy)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' "$B/prepare/$ID"; curl -s -m 15 "$B/prepare/$ID" | grep -o 'class="reasons"' | wc -l
echo '--- and the Jobs page lists the reasons of the new scores it shows (expect 1 or more once one sits in a listed tier)'; curl -s -m 15 $B/jobs | grep -o 'class="reasons"' | wc -l
```

Expect, on each line of the first query, `with_reasons` equal to `scores`, a
`card` line once an inbox run has stored a new advert and a `spec` line once a
Prepare application click has scored one, `0` on the second, and no rows on the
third. A line of the first query one short, with `1` on the second, is a score
made with the new prompt by code from before the deploy: a Prepare application
click or a Rescore role pressed between the pull and the restart, or an inbox
run that was under way when the pull landed. The third query names each one,
and its `scored_at` is before the start time AFTER showed. Such a role keeps a
score with no reasons until it is scored again, and a full description is not
scored again while it stays the same, nor a card score until a re-score reaches
it. The history is then one short of reasons on its line too. The tally is
how many quotes were dropped: `reasons` is how many the scores gave, at most
four a score, `quotes_kept` how many quotes were found word for word in what
was scored, and `not_found`, `too_long`, `empty` and `not_text` why the rest
were not kept, while `entries_left_out` counts what in a reply could not be
read as a reason at all. A `not_found` now and then is the rule working, a
quote the scorer tidied or took from the CV; most of them on every score would
say the prompt needs another look. Expect the history to have reasons on every
line of the next query, `scored` for the inbox and the click, `tailored` for a
Rescore role, the same kind of tally after it, `0` and `0` for the scores from
before, the scores held from before as BEFORE less the roles you clicked or
re-scored, the applied roles as BEFORE, `0` for the applied proof, and
`not in the stored text 0` from the Python check. That check holds every kept
quote to the text stored for its role by the rule that kept it, word for word
with whole words at either end: the stored description for a full description
score, skipped and counted when the page checker has stored a newer description
since, and the title, company, location and pay text the row stores for a
card, which the inbox holds each quote to before it stores it. A line it prints
names a role whose kept quote is not in its stored text, which should never
happen. The cost lines
are the runs since the first new score, to compare with the week before: the
reasons add about 180 output tokens to a score, a little under 0.3 cents on
Sonnet's prices, on each path. If `role` is empty no role has been scored on its
full description yet, and the two Prepare lines have nothing to show.

## Rollback

Rollback is code only. Revert the merge on main, push, then on the box pull and
restart. The prompt goes back to `97f11cce` with the code, and n8n reads it on
its next run. The two columns and every reason stored stay in the database, and
old code ignores them. A score old code makes in the meantime stores no reasons,
and its date is not the date of any reasons left on its row, so once the code
rolls forward again no page shows those old reasons beside it and the history
files none with it. Never restore the pre-deploy backup: it would drop every
score, sighting and click made since. In the commands below, `<merge>` is the
merge commit's hash.

```bash
# on a clone of the repo, then push
git revert -m 1 <merge> && git push origin main
```

```bash
cd /opt/biz-admin/medtech-radar && sudo -u radar git pull --ff-only && docker restart medtech-radar
```
