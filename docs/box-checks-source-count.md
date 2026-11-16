# Box checks: the source count on Home

LXC 110, checkout `/opt/biz-admin/medtech-radar`. Run git and Python as
radar, never `git status` as root.

The branch `claude/source-count` changes two things:

- `config/watchlist.yaml` loses a repeated block. Five sources (medtech-dive,
  massdevice, businesswire-medical, reuters-medtech, bbc-medical-technology)
  were listed twice, so the file had 29 entries for 24 sources. The two
  copies were identical, so nothing the watcher reads changes.
- Home's watcher sentence counts only live sources as expected to answer,
  each by its own latest check, and names the unverified ones apart. It
  reads, for example: "The signal watcher last ran at 10:01 and the inbox at
  09:01. 17 of 18 live sources answered at their last check, and 6 are not
  switched on yet."

Nothing in the database changes, so there is no backup, migration or
database open. It is a pull, a restart and one check.

## Pull and restart

Only when nothing is running. Expect one incoming commit (or the branch's
commits once merged), then `medtech-radar`.

```bash
cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was pulled' || { sudo -u radar git rev-parse HEAD | sudo -u radar tee .git/rollback-source-count && sudo -u radar git fetch origin && sudo -u radar git log --oneline HEAD..origin/main && sudo -u radar git pull --ff-only && sudo -u radar git log --oneline -1 && docker restart medtech-radar; }; }
```

## Check

After a few seconds for the dashboard to start. Expect `200`, then the new
sentence, then `0` for the old one. The sentence only shows on a quiet day,
when nothing needs you; on a busy day the second line prints nothing and the
check is the `0` alone.

```bash
B=http://192.168.1.100:8787; curl -s -m 15 -o /dev/null -w '%{http_code}\n' $B/; curl -s -m 15 $B/ | grep -o -E '[0-9]+ of [0-9]+ live sources? answered at (its|their) last check[^<.]*\.'; curl -s -m 15 $B/ | grep -c 'sources answering'
```

## Rollback

Code only, back to the commit saved before the pull.

```bash
cd /opt/biz-admin/medtech-radar && test -s .git/rollback-source-count && sudo -u radar git reset --hard "$(cat .git/rollback-source-count)" && sudo -u radar git log --oneline -1 && docker restart medtech-radar
```

The old code reads the watchlist without the repeated block too. Its Home
then says 17 of 24 sources answering, as it counts every entry.
