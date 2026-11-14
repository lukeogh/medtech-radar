# Box checks: the operational faults found on 13 November 2026

For LXC 110, the checkout at `/opt/biz-admin/medtech-radar`. Paste each block
into MobaXterm as it stands. Block 1 is read only: it changes nothing in the
database, the checkout or n8n. Block 2 is optional. It reproduces the watcher's
stop on a copy in `/tmp`, touches neither the live database nor the checkout,
and makes one ordinary fetch of optics.org's front page plus up to five of its
articles. Both blocks work on the code the box runs today, before this branch
is pulled.

Run them as the user that normally reads the database, `radar` or one that can
read `db/radar.sqlite` and use docker. If the database is not readable, put
`sudo -u radar` in front of the `$PY` line in step 4.

## Block 1, read only

```bash
cd /opt/biz-admin/medtech-radar
PY=/opt/biz-admin/radar-venv/bin/python
echo '=== 1. The n8n container'; docker ps --format '{{.Names}}  {{.Image}}  {{.Status}}' | grep -i n8n
N8N=$(docker ps --format '{{.Names}}' | grep -i n8n | head -1)
echo "=== 2. What each Radar workflow runs, from $N8N"
cat > /tmp/radar-wf.py <<'PYEOF'
import json, sys
raw = sys.stdin.read()
data, at = None, raw.find("[")
while data is None and at >= 0:
    try:
        data = json.JSONDecoder().raw_decode(raw, at)[0]
        if not (isinstance(data, list) and data and isinstance(data[0], dict)):
            data = None
    except ValueError:
        pass
    if data is None:
        at = raw.find("[", at + 1)
if data is None:
    sys.exit("no workflow JSON came back: " + raw[:300])
for w in sorted(data, key=lambda w: w.get("name", "")):
    if "radar" not in w.get("name", "").lower():
        continue
    print("%s: active=%s" % (w.get("name"), w.get("active")))
    for n in w.get("nodes", []):
        kind, p = n.get("type", ""), n.get("parameters", {})
        name = n.get("name", "") + (" DISABLED" if n.get("disabled") else "")
        if kind.endswith("executeCommand"):
            print("   %s: %s" % (name, p.get("command")))
        elif kind.endswith("gmail"):
            print("   %s: gmail %s" % (name, p.get("operation", "send")))
        elif kind.endswith("scheduleTrigger"):
            print("   %s: %s" % (name, json.dumps(p.get("rule"))))
        elif kind.endswith(".set"):
            vals = {a.get("name"): a.get("value")
                    for a in p.get("assignments", {}).get("assignments", [])}
            print("   %s: %s" % (name, vals))
PYEOF
docker exec "$N8N" n8n export:workflow --all 2>/dev/null | $PY /tmp/radar-wf.py; rm -f /tmp/radar-wf.py
echo '=== 3. A watcher, checker or digest process running right now'
ps -eo pid,lstart,etime,args | grep -E '[c]heck_signals|[c]heck_jobs|[b]uild_digest' || echo 'none running'
echo '=== 4. The database, read only'
$PY - <<'PYEOF'
import sqlite3, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, "scripts")
c = sqlite3.connect("file:db/radar.sqlite?mode=ro", uri=True)
c.row_factory = sqlite3.Row
def show(title, sql, args=()):
    print(f"\n--- {title}")
    rows = c.execute(sql, args).fetchall()
    if not rows:
        print("(no rows)"); return
    print(" | ".join(rows[0].keys()))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))
show("4a. Signal watcher, last 12 rows", "SELECT ts, mode, items_in, items_new, note FROM runs WHERE workflow='signals' ORDER BY ts DESC LIMIT 12")
show("4b. Signal watcher rows by mode", "SELECT mode, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last FROM runs WHERE workflow='signals' GROUP BY mode")
show("4c. Sources, oldest check first", "SELECT source_id, last_checked, last_status FROM source_state ORDER BY last_checked")
show("4d. Pushes", "SELECT COUNT(*) AS signals, SUM(pushed) AS pushed, SUM(relevance >= 75) AS at_75_or_more, MAX(pushed_at) AS last_push FROM signals")
show("5a. Advert checker, last 12 rows", "SELECT ts, mode, items_in, items_new, note FROM runs WHERE workflow='check' ORDER BY ts DESC LIMIT 12")
show("5b. Expiries by day", "SELECT substr(expired_at,1,10) AS day, COUNT(*) AS expired FROM opportunities WHERE expired_at IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT 6")
show("5c. Page-check closures by day", "SELECT substr(closed_at,1,10) AS day, COUNT(*) AS closed FROM opportunities WHERE closed_source='page_check' GROUP BY day ORDER BY day DESC LIMIT 6")
try:
    import check_jobs
    r = check_jobs.expire_stale(c, write=False)
    print(f"\n--- 5d. Roles the 45-day expiry would retire if it ran live now: {r['expired']}")
    for s in r["sample"][:5]:
        print("   newest sign of life", s["newest"])
except Exception as err:
    print(f"\n--- 5d. could not count: {type(err).__name__}: {err}")
show("6a. Digest, last 10 rows", "SELECT ts, mode, items_in, note FROM runs WHERE workflow='digest' ORDER BY ts DESC LIMIT 10")
show("6b. Digest rows by mode", "SELECT mode, COUNT(*) AS n, MAX(ts) AS last FROM runs WHERE workflow='digest' GROUP BY mode")
show("6c. Digest window and builds never committed", "SELECT (SELECT value FROM meta WHERE key='last_digest_ts') AS last_digest_ts, (SELECT COUNT(*) FROM meta WHERE key LIKE 'digest_pending_%') AS uncommitted_builds")
show("6d. Signal statuses", "SELECT status, COUNT(*) AS n FROM signals GROUP BY status")
cut = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
ts = [datetime.fromisoformat(r[0].replace("Z", "+00:00")) for r in
      c.execute("SELECT ts FROM runs WHERE workflow='inbox' AND ts >= ? ORDER BY ts", (cut,))]
gaps = sorted(((b - a).total_seconds() / 3600, a) for a, b in zip(ts, ts[1:]))
print(f"\n--- 7. Inbox, 30 days: {len(ts)} rows")
if gaps:
    hours = [g for g, _ in gaps]
    print(f"   longest gap {hours[-1]:.1f} h, 95 in 100 gaps under {hours[int(0.95 * (len(hours) - 1))]:.1f} h")
    for limit in (3, 6, 12, 24):
        print(f"   gaps over {limit} h: {sum(1 for h in hours if h > limit)}")
    for h, start in gaps[::-1][:5]:
        print(f"   {h:.1f} h from {start:%a %d %b %H:%M} UTC")
PYEOF
```

## Block 2, optional: make the watcher's stop happen again, on a copy

```bash
PY=/opt/biz-admin/radar-venv/bin/python
rm -rf /tmp/radar-repro && mkdir -p /tmp/radar-repro/db
cd /opt/biz-admin/medtech-radar
tar --exclude=./.git --exclude=./.env --exclude=./db --exclude=./config/profile -cf - . | tar -xf - -C /tmp/radar-repro
$PY -c "import sqlite3; s = sqlite3.connect('file:db/radar.sqlite?mode=ro', uri=True); d = sqlite3.connect('/tmp/radar-repro/db/radar.sqlite'); s.backup(d); d.close(); s.close(); print('copied')"
cd /tmp/radar-repro && timeout 900 $PY scripts/check_signals.py --dry-run --mock --source optics-org; echo "exit $?"
cd / && rm -rf /tmp/radar-repro
```

`--source optics-org` checks that one source whatever its interval, `--mock`
scores with the test mocks so nothing is spent, and every write lands in the
copy, which the last line deletes. The `.env`, the CV folder and git history
are not copied.

Also worth a look in the n8n UI: Radar Signals, Executions. The runs since
13 November 02:00 show either Error, with the Python traceback in the Check
signals node, or Running with no end.

## What each answer means

| Step | If it shows | It means |
|---|---|---|
| 2, Radar Signals | `check_signals.py --dry-run` | The phone push is in dry run. Nothing has ever been sent, which is what the 265 "held" signals say. Arming is the sticky note's steps 3 to 5. |
| 2, Radar Signals | `--push` | Push is armed and the cause is elsewhere: look at 4a for "push failed" notes. |
| 2, Radar Page Check | absent, or `active=False` | The advert checker is not scheduled at all. The 10 November retirements came from one run by hand. |
| 2, Radar Page Check | `--dry-run`, `active=True` | It runs daily and writes nothing, so nothing expires or closes. 5a then shows daily rows with mode `dry-run`. |
| 2, Radar Page Check | `--live`, `active=True` | It is armed. 5a should show daily `live` rows; if 5a has none since 10 November it is failing every day (5a's last row and n8n's executions say how). |
| 2, Radar Digest | Send Digest and Mark Digested `DISABLED` | The Monday email is not being sent. The build runs, which is all 6b's `dry-run` rows mean. |
| 2, Radar Digest | both enabled | The email is being sent. 6b should then show `commit` rows, one each Monday. |
| 2, any | `python_bin` other than `python3`, or another `repo_path` | Note it. The scripts and the SDK pin must match that interpreter. |
| 3 | `check_signals.py` processes with start times hours or days ago | The watcher hangs rather than crashes: each run since the first stop is still waiting on optics.org, and n8n starts another every two hours. |
| 3 | `none running` | The runs crash, or are killed, rather than hang. Block 2 or n8n's executions say which exception. |
| 4a | last row 13 Nov 00:00 or so, and nothing since | Runs still are not finishing. A row newer than 13 Nov means they recovered by themselves. |
| 4c | `optics-org` and the three working trade sources still at 12 Nov 00:01 to 00:02, the institute sources recent | Each run gets as far as optics.org and stops there, the audit's reading. |
| 4d | `pushed` 0 or empty | Confirms nothing has reached the phone. |
| 5a | no rows after 10 Nov | The checker has not finished a run since 10 November, dry or live. |
| 5a | daily rows, mode `dry-run` | It runs daily in dry run: classifications print, nothing is written. |
| 5b | only `2026-11-10` | The only live expiry pass ever made, as the audit inferred. |
| 5d | a figure near 38 | The adverts overdue for retirement, by the same rule the live pass uses. The first live run will retire them. |
| 6a, 6b | `dry-run` rows on Mondays and no `commit` row | The digest builds every week and has never been confirmed sent. |
| 6c | `uncommitted_builds` above 0 | One per Monday build whose send was never confirmed. Expected while sending is off. |
| 6d | every signal `new` | No send has been committed, so no signal was marked digested. |
| 7 | gaps over 3 h and over 6 h most days | How often the old limits called a quiet inbox stale (3 h on /health) and stalled (6 h on Home). |
| 7 | longest gap under 24 h | The new inbox limits, quiet at 12 h and stale at 24 h, fit. If a normal week has a longer gap, raise the stale limit to the next whole day above it in `radar_common.JOB_LIMITS`. |
| Block 2 | a traceback ending `InvalidURL` or `UnicodeEncodeError` | A link on optics.org's front page has a space or an accented character in it, and the watcher crashes fetching it. This branch encodes such links and fetches them. |
| Block 2 | a traceback ending `IncompleteRead` or another `http.client` error | optics.org breaks off its replies. This branch reads that as unreachable and moves on. |
| Block 2 | `exit 124` after fifteen minutes | It hangs: the reply never finishes. This branch gives up on a body after 90 seconds and on a whole source after ten minutes. |
| Block 2 | `exit 0` | optics.org answers normally today. The stop was passing, or the link that caused it is gone. n8n's executions from 13 November hold the traceback. |
