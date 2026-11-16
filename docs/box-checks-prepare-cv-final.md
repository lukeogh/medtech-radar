# Box checks: Prepare application final and the CV page final (branch claude/prepare-cv-final)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. BEFORE, step 5 and AFTER are read only. Nothing
here calls the model, and nothing paid runs without a confirm on the page
itself: the first paid press after the deploy is yours.

Both pages ship in one deploy: one backup, one pull, one rollback point and
one restart.

## What changes on the box

### Prepare application

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
- Mark applied (key M) and Undo (key Z) are on the page, through the routes
  the Jobs page already uses (`/apply`, `/unapply`), so Save CV puts the role
  in Preparing and Mark applied moves it to Applied on the board, as before.
- No new table, column or route for this page, and no migration.

### The CV page

- The CV page, `/cv`, is rebuilt from design/cv/final: the master CV as an A4
  sheet edited in place part by part, with Add section, Rearrange and Design
  above it; the master and its facts; the History, Preferences and Word import
  tabs. Its code moves from `serve_dashboard.py` to `scripts/cv_screen.py`.
  The keys are the frames' only: Tab, Enter, Esc, Alt Shift with the arrows,
  Ctrl S, Ctrl Z, Ctrl Shift Z, and the arrows along the toolbar and tabs.
- The Preferences tab (still `/cv#settings`, Home's link) writes the live
  `config/profile/preferences.md` only: the four lines `day_rate_floor_gbp`,
  `salary_floor_gbp`, `cv_pass_score` and `cv_floor_score`, and the words of its
  Location and Sectors sections. Every other byte of the file stays as it is.
- Saving a floor, the pass mark or the Filtered floor re-sorts the open, scored
  roles that are not applied for, in code, through `regate_rates.regate` with
  the CV gate judged again from the stored CV match. Each move is kept in
  `score_history` first (writer `cv_preferences`) and the re-sort is one
  `regate` row in `runs`. It costs nothing.
- Saving location or sector re-sorts nothing and offers a paid rescore of every
  open role, its count and cost first. **Rescore affected**, from that offer or
  from the notice over roles scored on an earlier CV, is one confirmed run over
  every affected role, in a thread of the dashboard, on the card scorer. The
  route refuses a run whose count has grown past the one confirmed.
- Design writes `config/profile/cv.design.json` (font, size, spacing,
  margins), which the sheet and the Word file both use. Without it, the Word
  file is the template it has always been.
- New guarded POST routes: `/cv/preferences`, `/cv/preferences/section`,
  `/cv/design`. New read only GET: `/cv/rescore/status`. `/cv/rescore` now
  needs `{"confirm": true, "scope": ..., "expect": n}`; the old 25-a-press
  `--stale-cv` pass is no longer what the page runs (the script still has it).

### Both

- The CV sheet is light paper on both pages; the rest of each page stays dark.
- No table, no column, no migration, no n8n workflow, no scheduled job and no
  radar.yaml change.

**What the first open after the deploy will do.** Either page reads and shows;
it writes nothing and spends nothing. On Prepare application, the cost-first
button waits for a press. On the CV page, if roles are scored on an earlier CV
than the active one, the notice above the sheet says how many and on which
versions, and its button carries the count and the cost; pressing it opens a
confirm, and only the confirm's own button starts the run. Nothing paid runs
without that confirm. The sheet shows the master in the template's look (there
is no `cv.design.json` yet). The **first save** on the Preferences tab, of any
of the four lines, re-sorts every open role against all four values as they
then stand, the CV gate included; step 5 prints how many roles that would move
with today's values, which should be none.

Don't press anything on a Prepare application page between step 4 and step 6:
the old dashboard is still serving, and its page starts the fetch on opening
from the Jobs link.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
A=$(cat config/profile/cv.active)
echo '--- code and rollback point'; sudo -u radar git status --short; sudo -u radar git log --oneline -1
echo '--- is anything running'; pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || echo 'nothing running'
echo '--- the prompt and the ceiling the box scores with now (expect 22353628 6144)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common, score_item; print(radar_common.prompt_version(), score_item.SPEC_MAX_TOKENS)"
echo '--- anthropic in the venv, for the scheduled jobs (expect 0.120.x; 1.0 and later drop temperature)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import anthropic; print(anthropic.__version__)"
echo '--- anthropic in the dashboard, for the presses on both pages (expect 0.120.x)'; docker exec medtech-radar env PYTHONDONTWRITEBYTECODE=1 python -c "import anthropic; print(anthropic.__version__)"
echo '--- scores held, by prompt and basis'; Q "SELECT prompt_version, score_basis, COUNT(*) AS scores FROM opportunities WHERE tier IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10;"
echo '--- what Prepare application has cost so far, to compare after'; Q "SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens), 0) AS output_tokens, MAX(ts) AS last_call FROM runs WHERE workflow = 'prepare';"
echo '--- the board, to compare after'; Q "SELECT COALESCE(stage, 'off the board') AS stage, COUNT(*) AS items FROM applications GROUP BY 1 ORDER BY 1;"
echo '--- no Jobs row holds a tailored score (expect 0)'; Q "SELECT COUNT(*) AS jobs_rows_with_a_tailored_score FROM opportunities WHERE cv_version LIKE 'tailored-%';"
echo '--- open roles to look at after'; Q "SELECT id, company, title, score_basis FROM opportunities WHERE applied_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND acknowledged_at IS NULL AND tier IN ('top', 'question', 'reading') ORDER BY id DESC LIMIT 3;"
echo "--- the active master: $A"; ls -1 config/profile/cv-*.md | tail -5
echo '--- the four scoring lines and the section headings in the live preferences file'; grep -nE '^(day_rate_floor_gbp|salary_floor_gbp|cv_pass_score|cv_floor_score):|^## ' config/profile/preferences.md
echo '--- a design file is not there yet'; ls -l config/profile/cv.design.json 2>/dev/null || echo 'no cv.design.json'
echo '--- the roles the earlier-CV notice will count, by the CV their score names'; Q "SELECT COALESCE(cv_version, '(none)') AS cv_version, COUNT(*) AS roles FROM opportunities WHERE tier IS NOT NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND acknowledged_at IS NULL AND COALESCE(score_basis, '') <> 'spec' AND applied_at IS NULL AND COALESCE(cv_version, '') <> '$A' GROUP BY cv_version ORDER BY roles DESC;"
echo '--- open roles a location or sector change would offer to rescore'; Q "SELECT COUNT(*) AS open_roles FROM opportunities WHERE tier IS NOT NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND acknowledged_at IS NULL AND COALESCE(score_basis, '') <> 'spec' AND applied_at IS NULL;"
echo '--- what the live card rescores have cost, the runs the estimate reads'; Q "SELECT COUNT(*) AS runs, SUM(items_in) AS roles, SUM(input_tokens) AS input, SUM(output_tokens) AS output, SUM(cache_read_tokens) AS cached FROM runs WHERE workflow = 'rescore' AND mode = 'live' AND items_in > 0 AND input_tokens > 0;"
echo '--- the counts, to compare after'; Q "SELECT (SELECT COUNT(*) FROM opportunities) AS roles, (SELECT COUNT(*) FROM score_history) AS history, (SELECT COUNT(*) FROM runs) AS runs;"
echo '--- /health, then /cv today (expect 200)'; curl -s -m 15 -w '\n%{http_code}\n' $B/health; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/cv
```

Keep the output: AFTER compares with it. If either anthropic line is not
0.120.x, deploy anyway but press nothing paid until it is pinned: a live call
with the wrong version fails, is recorded as failed, and keeps each role's
score. The earlier-CV count here does not follow repost chains as the page
does, so a role applied for through a repost may show here and not on the
page; the page's number is the one that counts.

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

5. Before the restart, look at what a first save on the Preferences tab would
   move with today's values. Read only: a dry run on a read only connection.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python -c "import sys, sqlite3; sys.path.insert(0, 'scripts'); import radar_common as rc, regate_rates as rr; cfg = rc.load_config(); c = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); c.row_factory = sqlite3.Row; r = rr.regate(c, cfg, rc.read_rate_floors(cfg), write=False, thresholds=rc.read_score_thresholds(cfg), cv_gate_again=True); print('open roles judged:', r['open_rows'], '| would move:', r['tier_moves_total'], r['tier_moves'], '| rate kept as scored:', r['kept_as_scored'])"
   ```

   Expect `would move: 0 {}`. A number above 0 means some stored gates do not
   match the file as it is now (a line edited by hand since those roles were
   scored); the first save will move exactly those roles, and say so.

6. Restart the dashboard. No database step: nothing in the database changes.

   ```bash
   docker restart medtech-radar
   ```

7. Run AFTER, then open both pages in a browser at full width and on the
   phone. On a Prepare application page, see the cost-first button and press
   nothing. On the CV page, Tab to a section and press Enter, change a word,
   press Escape twice, and see the toolbar say 1 unsaved line; Ctrl Z takes it
   back. Open Design and Rearrange and cancel them. Open the Preferences tab
   and type a pass mark without saving: it says how many roles a save would
   move. Press no Rescore unless you mean to pay for it; its confirm says the
   count and the cost first.

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- the code'; sudo -u radar git status --short; sudo -u radar git log --oneline -1; docker inspect -f '{{.State.StartedAt}}' medtech-radar
echo '--- the prompt and the ceiling now (expect 422cdad4 8192)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common, score_item; print(radar_common.prompt_version(), score_item.SPEC_MAX_TOKENS)"
ID=$(sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sqlite3; c = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); r = c.execute(\"SELECT id FROM opportunities WHERE applied_at IS NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND acknowledged_at IS NULL AND tier IN ('top', 'question', 'reading') ORDER BY id DESC LIMIT 1\").fetchone(); print(r[0] if r else 0)")
echo "--- role $ID, opened three times as the Jobs link opens it (expect 200 200 200)"; for n in 1 2 3; do curl -s -m 20 -o /dev/null -w '%{http_code} ' -H 'Sec-Fetch-Site: same-origin' "$B/prepare/$ID?go=1"; done; echo
echo '--- the page states the cost, or says it has scored, and never starts on its own (expect 1 then 0)'; curl -s -m 20 -H 'Sec-Fetch-Site: same-origin' "$B/prepare/$ID?go=1" | grep -c -E 'About 3 cents, 1 call|Scored once for this description'; curl -s -m 20 -H 'Sec-Fetch-Site: same-origin' "$B/prepare/$ID?go=1" | grep -c 'data-run='
echo '--- nothing was spent by opening (expect the same calls as BEFORE)'; Q "SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens), 0) AS output_tokens, MAX(ts) AS last_call FROM runs WHERE workflow = 'prepare';"
echo '--- the board, as BEFORE'; Q "SELECT COALESCE(stage, 'off the board') AS stage, COUNT(*) AS items FROM applications GROUP BY 1 ORDER BY 1;"
echo '--- no Jobs row holds a tailored score (expect 0)'; Q "SELECT COUNT(*) AS jobs_rows_with_a_tailored_score FROM opportunities WHERE cv_version LIKE 'tailored-%';"
echo '--- the CV page, its sheet and its Preferences tab (expect 200, 1, 1)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/cv; curl -s -m 15 $B/cv | grep -c 'id="cv-sheet"'; curl -s -m 15 $B/cv | grep -c 'role="tabpanel" id="settings"'
echo '--- the earlier-CV notice, if BEFORE counted any (its button carries the count and the cost)'; curl -s -m 15 $B/cv | grep -o 'Rescore [0-9]* roles*, [^<]*' | head -1 || echo 'no notice'
echo '--- nothing is rescoring (expect "state": "idle")'; curl -s -m 15 $B/cv/rescore/status; echo
echo '--- the guard is in front of the new routes (enforce: 403; warn: 422 and nothing written)'; curl -s -m 15 -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"values": {}}' $B/cv/preferences
echo '--- the counts, the same as BEFORE (opening the pages writes nothing)'; Q "SELECT (SELECT COUNT(*) FROM opportunities) AS roles, (SELECT COUNT(*) FROM score_history) AS history, (SELECT COUNT(*) FROM runs) AS runs;"
echo '--- the preferences file, the same four lines as BEFORE'; grep -nE '^(day_rate_floor_gbp|salary_floor_gbp|cv_pass_score|cv_floor_score):' config/profile/preferences.md
```

Expect `422cdad4 8192`; `200 200 200`; `1` then `0`; the same Prepare calls
and board as BEFORE; `0`; `200`, `1`, `1`; the notice's button text if BEFORE
counted roles; `"state": "idle"`; `403` (enforce) or `422` (warn); and the same
counts and lines as BEFORE, unless the inbox or the watcher ran in between and
added roles, history or runs of its own. The `200` on `/cv` is also the sign
the dashboard loaded the new `cv_screen.py`: it imports it when it starts.

Then, your call: open a role's Prepare application page and press its button
once. It costs about 3 cents. Afterwards, this shows its asks and how many
kept their advert sentence; an ask whose sentence was not found word for word
shows on the page without one.

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
echo '--- full description scores on the new prompt, and their sentences'; Q "SELECT o.id, o.scored_at, SUM(t.key = 'sentence' AND t.atom IS NOT NULL) AS sentences_kept, SUM(t.key = 'sentence_dropped' AND t.atom IS NOT NULL) AS sentences_dropped FROM opportunities o, json_tree(o.score_working) t WHERE o.score_basis = 'spec' AND o.prompt_version = '422cdad4' GROUP BY o.id ORDER BY o.scored_at DESC LIMIT 5;"
echo '--- that press, in runs (expect one more call than BEFORE)'; Q "SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens), 0) AS output_tokens, MAX(ts) AS last_call FROM runs WHERE workflow = 'prepare';"
echo '--- no role was scored twice for one stored description (expect 0)'; Q "SELECT COUNT(*) AS scored_twice FROM (SELECT opportunity_id, description_hash FROM score_history WHERE writer = 'prepare' AND event = 'scored' AND description_hash IS NOT NULL GROUP BY opportunity_id, description_hash HAVING COUNT(*) > 1);"
```

After a save on the CV page's Preferences tab, this shows what the re-sort
did:

```bash
cd /opt/biz-admin/medtech-radar && sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python -c "import sqlite3; c = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); print(c.execute(\"SELECT ts, items_in, items_new, note FROM runs WHERE workflow = 'regate' ORDER BY id DESC LIMIT 3\").fetchall()); print(c.execute(\"SELECT COUNT(*) FROM score_history WHERE writer = 'cv_preferences'\").fetchone())"
```

## Rollback

Code only, for both pages at once. The backup from step 2 is never restored:
nothing in the database changed shape, and restoring it would throw away what
happened since.

```bash
cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was rolled back' || { test -s .git/rollback-prepare-cv-final && sudo -u radar git reset --hard "$(cat .git/rollback-prepare-cv-final)" && sudo -u radar git log --oneline -1 && docker restart medtech-radar; }; }
```

Do not roll back while a Rescore is running (AFTER's status line says
`running`): the restart stops it mid-run. Each role it finished is stored, the
rest keep their earlier scores, and its run row is not written, so its tokens
are not in the week's cost.

What a rollback leaves:

- Prepare application is the old page again, which starts the fetch on
  opening from the Jobs link. Scores made since the deploy keep their stamp
  `422cdad4` and their asks keep their `sentence` and `sentence_dropped`,
  which the old code does not read; nothing breaks.
- What the new CV page wrote stays, and the old code reads all of it as it
  always did: the preferences file is plain text the scorer reads whole;
  `cv.design.json` is never read by the old code, so the old Word file is the
  template again; a re-sort's tiers and `score_history` rows are ordinary
  scores and history. A tier the re-sort moved on the pass mark stays where it
  is under the old code until that role is scored again; save the old values
  back on the new page before rolling back if you want them put back.
- A redeploy afterwards is steps 3 to 6 again, and step 4 keeps the first
  rollback point.
