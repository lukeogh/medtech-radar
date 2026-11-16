# Box checks: Prepare application final and the CV page final (branch claude/prepare-cv-final)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. BEFORE and AFTER are read only. Nothing here calls
the model: the first paid press after the deploy is yours, on the page.

## Prepare application

What the change touches on the box:

- The page `/prepare/<id>` is rebuilt from design/prepare-application/final
  (scripts/prepare_page.py). It **never spends on opening**, however it is
  opened: the Jobs link no longer carries `?go=1`, the server no longer reads
  it or `Sec-Fetch-Site`, and an old bookmark with `?go=1` opens the page and
  its button. Fetching and scoring the full description sit behind one button
  that states its cost first: "About 3 cents, 1 call: Fetch and score the full
  description".
- The full description prompt asks for the advert sentence of each ask, so
  the prompt version moves from `22353628` to `422cdad4`. A sentence is kept
  only where the stored description has it word for word (the reasons' own
  quote rule). Cards are scored as before, but every score from the next
  inbox run on is stamped `422cdad4`, because the version is the hash of the
  whole file.
- The reply ceiling for a full description, `SPEC_MAX_TOKENS`, rises from
  6,144 to 8,192. A press costs about 3 cents, at most about 12.
- The CV sheet is light paper; the rest of the page stays dark.
- Mark applied (key M) and Undo (key Z) are on the page, through the routes
  the Jobs page already uses (`/apply`, `/unapply`), so Save CV puts the role
  in Preparing and Mark applied moves it to Applied on the board, as before.
- No new table, no new column, no new route, no migration, no n8n workflow
  change and no preferences change.

Don't press anything on a Prepare application page between step 4 and step 5:
the old dashboard is still serving, and its page starts the fetch on opening
from the Jobs link.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code and rollback point'; sudo -u radar git status --short; sudo -u radar git log --oneline -1
echo '--- is anything running'; pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || echo 'nothing running'
echo '--- the prompt and the ceiling the box scores with now (expect 22353628 6144)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common, score_item; print(radar_common.prompt_version(), score_item.SPEC_MAX_TOKENS)"
echo '--- the anthropic SDK in the venv (expect 0.120.x; 1.0 and later drop temperature)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import anthropic; print(anthropic.__version__)"
echo '--- scores held, by prompt and basis'; Q "SELECT prompt_version, score_basis, COUNT(*) AS scores FROM opportunities WHERE tier IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10;"
echo '--- what Prepare application has cost so far, to compare after'; Q "SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens), 0) AS output_tokens, MAX(ts) AS last_call FROM runs WHERE workflow = 'prepare';"
echo '--- the board, to compare after'; Q "SELECT COALESCE(stage, 'off the board') AS stage, COUNT(*) AS items FROM applications GROUP BY 1 ORDER BY 1;"
echo '--- no Jobs row holds a tailored score (expect 0)'; Q "SELECT COUNT(*) AS jobs_rows_with_a_tailored_score FROM opportunities WHERE cv_version LIKE 'tailored-%';"
echo '--- open roles to look at after'; Q "SELECT id, company, title, score_basis FROM opportunities WHERE applied_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND acknowledged_at IS NULL AND tier IN ('top', 'question', 'reading') ORDER BY id DESC LIMIT 3;"
echo '--- /health'; curl -s -m 15 -w '\n%{http_code}\n' $B/health
```

Keep the output: AFTER compares with it. If the anthropic line is not 0.120.x,
stop: the deploy is fine but no press will score until the venv is pinned.

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

4. Pull, only when nothing is running. The commit you are on is saved first as
   the rollback point, in `.git/rollback-prepare-cv-final`, and only if no
   rollback point is saved yet, so running this again keeps the first one.
   Nothing is pulled unless the point is there.

   ```bash
   cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was pulled' || { { test -s .git/rollback-prepare-cv-final || sudo -u radar sh -c 'git rev-parse HEAD > .git/rollback-prepare-cv-final'; } && test -s .git/rollback-prepare-cv-final && echo "rollback point $(cat .git/rollback-prepare-cv-final)" && sudo -u radar git pull --ff-only && sudo -u radar git log --oneline -3; }; }
   ```

5. Restart the dashboard. No database step: nothing in the database changes.

   ```bash
   docker restart medtech-radar
   ```

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- the code'; sudo -u radar git log --oneline -1; docker inspect -f '{{.State.StartedAt}}' medtech-radar
echo '--- the prompt and the ceiling now (expect 422cdad4 8192)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common, score_item; print(radar_common.prompt_version(), score_item.SPEC_MAX_TOKENS)"
ID=$(sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sqlite3; c = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); r = c.execute(\"SELECT id FROM opportunities WHERE applied_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND acknowledged_at IS NULL AND tier IN ('top', 'question', 'reading') ORDER BY id DESC LIMIT 1\").fetchone(); print(r[0] if r else 0)")
echo "--- role $ID, opened three times as the Jobs link opens it (expect 200 200 200)"; for n in 1 2 3; do curl -s -m 20 -o /dev/null -w '%{http_code} ' -H 'Sec-Fetch-Site: same-origin' "$B/prepare/$ID?go=1"; done; echo
echo '--- the page states the cost, or says it has scored, and never starts on its own (expect 1 then 0)'; curl -s -m 20 -H 'Sec-Fetch-Site: same-origin' "$B/prepare/$ID?go=1" | grep -c -E 'About 3 cents, 1 call|Scored once for this description'; curl -s -m 20 -H 'Sec-Fetch-Site: same-origin' "$B/prepare/$ID?go=1" | grep -c 'data-run='
echo '--- nothing was spent by opening (expect the same calls as BEFORE)'; Q "SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens), 0) AS output_tokens, MAX(ts) AS last_call FROM runs WHERE workflow = 'prepare';"
echo '--- the board, as BEFORE'; Q "SELECT COALESCE(stage, 'off the board') AS stage, COUNT(*) AS items FROM applications GROUP BY 1 ORDER BY 1;"
echo '--- no Jobs row holds a tailored score (expect 0)'; Q "SELECT COUNT(*) AS jobs_rows_with_a_tailored_score FROM opportunities WHERE cv_version LIKE 'tailored-%';"
```

Then, your call: open a role's page and press its button once. It costs about
3 cents. Afterwards, this shows its asks and how many kept their advert
sentence; an ask whose sentence was not found word for word shows on the page
without one.

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
echo '--- full description scores on the new prompt, and their sentences'; Q "SELECT o.id, o.scored_at, SUM(t.key = 'sentence' AND t.atom IS NOT NULL) AS sentences_kept, SUM(t.key = 'sentence_dropped' AND t.atom IS NOT NULL) AS sentences_dropped FROM opportunities o, json_tree(o.score_working) t WHERE o.score_basis = 'spec' AND o.prompt_version = '422cdad4' GROUP BY o.id ORDER BY o.scored_at DESC LIMIT 5;"
echo '--- that press, in runs (expect one more call than BEFORE)'; Q "SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens), 0) AS output_tokens, MAX(ts) AS last_call FROM runs WHERE workflow = 'prepare';"
echo '--- no role was scored twice for one stored description (expect 0)'; Q "SELECT COUNT(*) AS scored_twice FROM (SELECT opportunity_id, description_hash FROM score_history WHERE writer = 'prepare' AND event = 'scored' AND description_hash IS NOT NULL GROUP BY opportunity_id, description_hash HAVING COUNT(*) > 1);"
```

## Rollback

Code only. The backup from step 2 is never restored: nothing in the database
changed shape, and restoring it would throw away what happened since.

```bash
cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was rolled back' || { test -s .git/rollback-prepare-cv-final && sudo -u radar git checkout "$(cat .git/rollback-prepare-cv-final)" && sudo -u radar git log --oneline -1 && docker restart medtech-radar; }; }
```

What a rollback leaves: the old page, which starts the fetch on opening from
the Jobs link again. Scores made since the deploy keep their stamp `422cdad4`
and their asks keep their `sentence` and `sentence_dropped`, which the old code
does not read; nothing breaks. A redeploy afterwards is steps 3 to 5 again, and
step 4 keeps the first rollback point. To deploy main again after a rollback,
`sudo -u radar git checkout main` before step 4.
