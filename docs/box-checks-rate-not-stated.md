# Box checks: Rate not stated, and the tier worked out in code (branch claude/rate-not-stated)

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. The dry run, BEFORE and AFTER are read only on the
live database. Nothing here calls the model and nothing here costs anything.

## What changes on the box

- The Rate gate has three states, set in code from the pay on file and never by
  the scorer. Passes is a stated figure at or above its floor. Fails is one
  under it. Not stated is no figure on file, competitive included. The floor
  rules are the same: a day rate against the day floor, an hourly rate times
  eight against the day floor, a salary against the salary floor, a range on
  its top figure, pounds and euros only.
- The tier is worked out in code from the four gate states. Top prospects:
  Sector, CV and Location pass, and Rate passes or is not stated. One question
  away: Sector and CV pass, and exactly one of Location and Rate fails.
  Reading: everything else. Filtered is unchanged.
- The scorer prompt no longer asks for a rate verdict or a question, so the
  prompt version moves from `1b1ffb78` to `3c8e6c63` on every score from the
  next inbox run on. The card part changes, the full description part only
  in one sentence.
- Jobs, Home, Prepare application and the old page at `/radar` draw a third
  mark for not stated: a question mark in the neutral colour, read out as
  "Rate not stated, that's your first question". The role pane's Rate gate
  reads "Not stated". A Top prospect carries no question. A One question away
  role asks only about the gate that fails.
- No new table, column or route, and no migration.

## The dry run on a copy of the database

Run this before merging. It copies the live database read only, judges the
copy with the branch's code, writes the copy twice to show the re-sort is free
and repeatable, and removes everything after. The live database and checkout
are not touched.

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
sudo -u radar git fetch origin
sudo -u radar git worktree add --detach /tmp/radar-rate origin/claude/rate-not-stated
sudo -u radar cp -r config/profile /tmp/radar-rate/config/
sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sqlite3; s = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); d = sqlite3.connect('/tmp/radar-rate-copy.sqlite'); s.backup(d); d.close(); print('copied')"
cd /tmp/radar-rate
R() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sys, json, sqlite3; sys.path.insert(0, 'scripts'); import radar_common as rc, regate_rates as rr; cfg = rc.load_config(); c = rc.get_db(__import__('pathlib').Path('/tmp/radar-rate-copy.sqlite')); r = rr.regate(c, cfg, rc.read_rate_floors(cfg), write=$1, thresholds=rc.read_score_thresholds(cfg)); [print(k, json.dumps(r[k], ensure_ascii=False)) for k in ('mode', 'open_rows', 'tiers_before', 'tiers_after', 'tier_moves', 'rate_before', 'rate_after', 'gate_not_judged', 'questions_replaced', 'rows_changed', 'history_rows_written', 'rate_unclear')]; [print('  unclear', json.dumps(u, ensure_ascii=False)) for u in r['rate_unclear_sample']]; print('runs with tokens since', c.execute(\"SELECT COUNT(*) FROM runs WHERE workflow = 'regate' AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0\").fetchone()[0])"; }
echo '=== 1. the dry run'; R False
echo '=== 2. the re-sort, on the copy'; R True
echo '=== 3. again, which should change nothing (expect rows_changed 0, history_rows_written 0)'; R True
echo '=== the roles you named, on the copy'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sqlite3; c = sqlite3.connect('/tmp/radar-rate-copy.sqlite'); [print(r) for r in c.execute(\"SELECT id, tier, gate_sector, gate_cv, gate_location, gate_rate, rate_stated, substr(company, 1, 30), substr(title, 1, 40), question_text FROM opportunities WHERE id IN (1760, 2350, 1939, 1349, 1868, 2150) ORDER BY id\")]"
cd /opt/biz-admin/medtech-radar
sudo -u radar git worktree remove --force /tmp/radar-rate
rm -f /tmp/radar-rate-copy.sqlite
```

What to look for. `tiers_before` and `tiers_after` count every open, scored
role that's not applied for, one row per advert. The Jobs page counts repost
chains, so its numbers are lower. `rate_after` should hold no `passes` or
`fails` beyond the roles with a figure on file. `rate_unclear` counts roles
with a number in the pay text, or a scorer that said a rate was stated, but no
figure Radar can judge. They read not stated. If it's above 0, look at the
listed roles before the live re-sort: the live `--write` refuses until you
pass `--unclear-ok`. On the live page on 16 Nov there was one, role 2246, a
monthly figure, which no floor covers. Step 3 should change nothing, and no
regate run should hold tokens.

## BEFORE

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- code'; sudo -u radar git status --short; sudo -u radar git log --oneline -1
echo '--- is anything running'; pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || echo 'nothing running'
echo '--- the prompt (expect 1b1ffb78)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; print(radar_common.prompt_version())"
echo '--- open roles by tier and stored Rate gate'; Q "SELECT tier, CASE WHEN gate_rate IS NULL THEN 'null' WHEN gate_rate = 1 THEN 'pass' ELSE 'fail' END AS rate, COALESCE(rate_band, 'none') AS band, COUNT(*) AS roles FROM opportunities WHERE tier IS NOT NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND applied_at IS NULL GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;"
echo '--- the Jobs page group counts'; curl -s -m 30 $B/jobs | grep -oE 'id="group-(top|question|reading)"|class="group-count"[^<]*' | head -6
echo '--- the counts, to compare after'; Q "SELECT (SELECT COUNT(*) FROM opportunities) AS roles, (SELECT COUNT(*) FROM score_history) AS history, (SELECT COUNT(*) FROM runs) AS runs;"
```

## The deploy, one block at a time

1. Run BEFORE.

2. Back up first.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar bash scripts/backup_radar.sh
   ```

3. Merge the branch on GitHub, then see what will arrive. Expect this
   branch's commits and anything else on main you haven't deployed.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar git fetch origin && sudo -u radar git log --oneline HEAD..origin/main
   ```

4. Pull, only when nothing is running. The commit you're on is saved first as
   the rollback point, in `.git/rollback-rate-not-stated`, only if none is
   saved yet.

   ```bash
   cd /opt/biz-admin/medtech-radar && { { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' || curl -s -m 10 http://192.168.1.100:8787/cv/rescore/status | grep -o '"state": "running"'; } && echo 'wait, something is running, nothing was pulled' || { { test -s .git/rollback-rate-not-stated || sudo -u radar sh -c 'git rev-parse HEAD > .git/rollback-rate-not-stated'; } && test -s .git/rollback-rate-not-stated && echo "rollback point $(cat .git/rollback-rate-not-stated)" && sudo -u radar git pull --ff-only && sudo -u radar git log --oneline -3; }; }
   ```

5. Restart the dashboard, so the pages draw the new marks.

   ```bash
   docker restart medtech-radar
   ```

6. The re-sort. First the dry run on the live database, which writes nothing.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python scripts/regate_rates.py
   ```

   Then the write, between inbox runs, because it takes the write lock for a
   moment. It refuses and lists the roles if `rate_unclear` is above 0. Add
   `--unclear-ok` once you've looked at them and they should read not stated.

   ```bash
   cd /opt/biz-admin/medtech-radar && sudo -u radar env PYTHONDONTWRITEBYTECODE=1 /opt/biz-admin/radar-venv/bin/python scripts/regate_rates.py --write
   ```

   It sends nothing, no push and no email, whatever the dry-run setting. Each
   role it moves keeps its old score in `score_history` first, writer
   `regate_rates`, and the pass is one `regate` row in `runs`. A save on the
   CV page's Preferences tab runs the same re-sort, so a floor change later
   keeps it right.

7. Run AFTER, then look at Jobs.

## AFTER

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
Q() { sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import sqlite3, sys; c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True); cur = c.execute(sys.argv[1]); print(" | ".join(d[0] for d in cur.description)); [print(" | ".join("" if v is None else str(v) for v in r)) for r in cur]' "$1"; }
B=http://192.168.1.100:8787
echo '--- the prompt (expect 3c8e6c63)'; sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "import sys; sys.path.insert(0, 'scripts'); import radar_common; print(radar_common.prompt_version())"
echo '--- open roles by tier and Rate state (expect no pass or fail where rate_stated is 0)'; Q "SELECT tier, CASE WHEN gate_rate IS NULL AND rate_stated = 0 THEN 'not stated' WHEN gate_rate IS NULL THEN 'null' WHEN gate_rate = 1 THEN 'pass' ELSE 'fail' END AS rate, rate_stated, COUNT(*) AS roles FROM opportunities WHERE tier IS NOT NULL AND closed_at IS NULL AND dismissed_at IS NULL AND expired_at IS NULL AND applied_at IS NULL GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;"
echo '--- the re-sort run'; Q "SELECT ts, items_in, items_new, input_tokens, output_tokens, note FROM runs WHERE workflow = 'regate' ORDER BY id DESC LIMIT 2;"
echo '--- the Jobs page: marks for a rate (expect no "Rate passes" or "Rate fails" beside a rate not stated)'; curl -s -m 30 $B/jobs | grep -oE 'aria-label="Rate [a-z ]+' | sort | uniq -c
echo '--- the roles you named'; Q "SELECT id, tier, gate_rate, rate_stated, substr(title, 1, 40) AS title, question_text FROM opportunities WHERE id IN (1760, 2350, 1939, 1349, 1868, 2150) ORDER BY id;"
```

The Jobs page's marks line only counts the rows the first screen draws. The
Reading group loads on its own, from `/jobs?part=reading`.

## Rollback

Code only. The backup from step 2 is never restored.

```bash
cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was rolled back' || { test -s .git/rollback-rate-not-stated && sudo -u radar git reset --hard "$(cat .git/rollback-rate-not-stated)" && sudo -u radar git log --oneline -1 && docker restart medtech-radar; }; }
```

What a rollback leaves. The re-sorted roles keep their new tiers and their Rate
gate as NULL with `rate_stated` 0. The old code reads a NULL Rate gate as not
judged and draws its empty circle, and its tier rule treats it as no fail, so
the tiers hold. The scorer's old rate verdicts are not put back: the history
keeps the gates and tier each role had, not the notes. New scores go back to
prompt `1b1ffb78`.
