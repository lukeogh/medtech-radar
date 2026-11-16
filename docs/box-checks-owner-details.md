# Box checks: the owner's details out of the repo

LXC 110, checkout `/opt/biz-admin/medtech-radar`. Run git and Python as
radar, never `git status` as root.

The repo is public, so the branch `claude/owner-details-private` takes the
owner's own details out of the tracked files:

- `config/radar.yaml` ships `n8n_url`, `ntfy_url`, `aggregator_email`,
  `digest_to` and `dashboard_url` empty, and `dashboard_allowed_hosts` with the
  LAN address alone. The real values go in `config/profile/radar.local.yaml`,
  gitignored, whose keys replace radar.yaml's.
- A saved CV copy is named after the `owner_name:` line of
  `config/profile/preferences.md`, not a name in the code.
- The scorer prompt names the owner by first name and reads the home base
  from the preferences file (prompt `93b203d2`, from `1b1ffb78`). The Prepare
  page shows a role scored before this as scored on an older prompt, as after
  every prompt change. Nothing is re-scored and nothing is spent.
- The test files carry an invented owner.

**Order matters.** Step 1 writes the private file and the `owner_name:` line
from the code that is on the box now, so it runs **before** the pull. Pulled
first, the new code would find radar.yaml's empty addresses: the push Off, the
digest with no recipient, and the guard refusing the proxy's name in enforce.

Nothing in the database changes.

## 1. Before the pull: the private file and the owner_name line

Only when nothing is running. It writes `config/profile/radar.local.yaml` with
the values the box uses now, keeps a copy of them in
`/tmp/radar-owner-before.json` for step 4, and adds an `owner_name:` line to
the preferences file with the name the code uses now. It writes nothing if the
private file is already there.

```bash
cd /opt/biz-admin/medtech-radar && PY=/opt/biz-admin/radar-venv/bin/python && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was written' || sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" - <<'PYEOF'
import json, pathlib, re, sys
sys.path.insert(0, "scripts")
import yaml, radar_common, cv_doc
local = pathlib.Path("config/profile/radar.local.yaml")
if local.exists():
    sys.exit("config/profile/radar.local.yaml is already there, nothing written")
cfg = radar_common.load_config()
hosts = [str(h) for h in (cfg.get("dashboard_allowed_hosts") or [])]
proxy = next((h for h in hosts if not re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]+", h)), "")
vals = {k: cfg.get(k) or "" for k in ("n8n_url", "ntfy_url", "aggregator_email", "digest_to")}
vals["dashboard_url"] = cfg.get("dashboard_url") or (f"https://{proxy}" if proxy else "")
vals["dashboard_allowed_hosts"] = hosts
local.write_text("# The owner's own values for config/radar.yaml, gitignored.\n"
                 "# See config/radar.local.example.yaml.\n"
                 + yaml.safe_dump(vals, sort_keys=False, allow_unicode=True), encoding="utf-8")
local.chmod(0o600)
pathlib.Path("/tmp/radar-owner-before.json").write_text(json.dumps(vals, sort_keys=True))
prefs = pathlib.Path(cfg.get("prefs_file") or "config/profile/preferences.md")
text = prefs.read_text(encoding="utf-8")
if re.search(r"^owner_name:", text, re.M):
    print("the owner_name line was already there")
else:
    nl = "\r\n" if "\r\n" in text else "\n"
    with open(prefs, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.rstrip() + nl + nl + "owner_name: " + cv_doc.OWNER + nl)
    print("owner_name line added")
print("private file written with", ", ".join(k for k, v in vals.items() if v))
print("Home base line in the preferences:", "yes" if re.search(r"^Home base ", text, re.M) else "MISSING, add one")
PYEOF
}
```

Expect `owner_name line added`, the six keys named, and `Home base line in the
preferences: yes`. Any key missing from the list was empty before too. If the
Home base line is missing, add one (`Home base <town or county>.`) to
`config/profile/preferences.md` before step 3, because the scorer now reads
the home base from there.

## 2. Optional: the list the hardening runner checks

`config/profile/private_strings.txt`, one string a line, `#` for a comment:
your full name, surname, the consultancy's name and domain, the domains of
your own servers, your email addresses, past employers, your home town. The
hardening runner fails if any tracked file holds one, case ignored, and says
only the file and the count. Write it by hand; it never leaves the box.

```bash
cd /opt/biz-admin/medtech-radar && sudo -u radar nano config/profile/private_strings.txt && sudo -u radar chmod 600 config/profile/private_strings.txt
```

## 3. Pull and restart

Only when nothing is running. Expect the branch's commits (or main's once
merged), then `medtech-radar`.

```bash
cd /opt/biz-admin/medtech-radar && { pgrep -a python | grep -E 'process_email|check_jobs|check_signals|build_digest|rescore|regate|backfill' && echo 'wait, something is running, nothing was pulled' || { if test -s config/profile/radar.local.yaml; then sudo -u radar git rev-parse HEAD | sudo -u radar tee .git/rollback-owner-details && sudo -u radar git fetch origin && sudo -u radar git log --oneline HEAD..origin/main && sudo -u radar git pull --ff-only && sudo -u radar git log --oneline -1 && docker restart medtech-radar; else echo 'step 1 first: no private file, nothing was pulled'; fi; }; }
```

## 4. Check

After a few seconds for the dashboard to start.

```bash
cd /opt/biz-admin/medtech-radar && PY=/opt/biz-admin/radar-venv/bin/python && B=http://192.168.1.100:8787 && curl -s -m 15 -o /dev/null -w 'dashboard %{http_code}\n' $B/ && sudo -u radar env PYTHONDONTWRITEBYTECODE=1 "$PY" -c "
import json, pathlib, sys
sys.path.insert(0, 'scripts')
import radar_common as rc, cv_doc, build_digest as bd
cfg = rc.load_config()
before = json.loads(pathlib.Path('/tmp/radar-owner-before.json').read_text())
same = all((cfg.get(k) or '') == v for k, v in before.items())
print('addresses as before:', 'yes' if same else 'NO: ' + ', '.join(k for k, v in before.items() if (cfg.get(k) or '') != v))
print('digest links to the proxy:', 'yes' if bd.dashboard_url(cfg) == before['dashboard_url'] else 'NO')
print('saved copy name has the owner:', 'yes' if cv_doc.saved_name('Acme', 'Head of QA') == 'CV - ' + (rc.read_owner_name() or '?') + ' - Acme - Head of QA' and rc.read_owner_name() else 'NO')
print('scorer prompt:', rc.prompt_version())
" && P=$(sudo -u radar "$PY" -c "import json; print(json.load(open('/tmp/radar-owner-before.json'))['dashboard_url'].split('//')[-1])") && curl -s -m 15 -o /dev/null -w 'by the proxy name %{http_code}\n' -H "Host: $P" $B/ && { test -s config/profile/private_strings.txt && { sudo -u radar git grep -I -i -c -F -f config/profile/private_strings.txt -- . ':!test/fixtures/linkedin' || echo 'no tracked file holds a private string'; } || echo 'no private_strings.txt, step 2 skipped'; }
```

Expect `dashboard 200`, three `yes`, `scorer prompt: 93b203d2`, `by the
proxy name 200`, and `no tracked file holds a private string` (or the step 2
note). A file listed with a count holds one of your strings: the README's
GitHub address carries your handle and stays, anything else is a find.

## Rollback

Code only, back to the commit saved before the pull. The private file and the
`owner_name:` line can stay: the old code ignores the file, and the scorer
reads the line as one more line of the preferences.

```bash
cd /opt/biz-admin/medtech-radar && test -s .git/rollback-owner-details && sudo -u radar git reset --hard "$(cat .git/rollback-owner-details)" && sudo -u radar git log --oneline -1 && docker restart medtech-radar
```
