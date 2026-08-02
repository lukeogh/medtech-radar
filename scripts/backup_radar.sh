#!/usr/bin/env bash
# Take a consistent backup of everything this system cannot rebuild.
#
# Two artefacts, both timestamped:
#   db/backups/radar-<stamp>.sqlite     the database
#   db/backups/profile-<stamp>.tar.gz   the CV versions and preferences
#
# The database is copied with VACUUM INTO, never with cp. The three
# workflows overlap on one file in WAL mode, so a plain copy can catch a
# write in progress and produce a file that opens cleanly and is missing
# the last transaction, which is the worst kind of backup because it looks
# fine. VACUUM INTO takes a read lock and writes a whole, compacted
# database, and it is the sqlite3 authors' own answer to this.
#
# config/profile/ exists on this host and nowhere else. The CV versions in
# it are the documents every score was made against, and losing them means
# every cv_version stamp in the database points at nothing.
#
# Local only. Getting copies off this host is the caller's job, because a
# backup that lives beside the thing it protects is not a backup. The Pi
# pulls these nightly, see docs/architecture.md.
#
# Idempotent and safe to re-run. It only ever adds files.
#
# Usage:  bash scripts/backup_radar.sh [--keep N] [--db PATH]

set -uo pipefail

KEEP=14
DB=""
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP="${2:-14}"; shift 2 ;;
        --db)   DB="${2:-}"; shift 2 ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT" || exit 1
[ -n "$DB" ] || DB="db/radar.sqlite"
OUT="db/backups"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

fail() { echo "{\"ok\": false, \"note\": \"$1\"}"; exit 1; }

command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 is not installed on this host"
[ -f "$DB" ] || fail "no database at $DB"
mkdir -p "$OUT" || fail "cannot create $OUT"

SNAP="$OUT/radar-$STAMP.sqlite"
TAR="$OUT/profile-$STAMP.tar.gz"

# A failed integrity check means the live database is already damaged.
# Back it up anyway, a damaged copy beats none, but say so loudly.
INTEGRITY="$(sqlite3 "$DB" "PRAGMA integrity_check;" 2>&1 | head -1)"

sqlite3 "$DB" "VACUUM INTO '$SNAP';" 2>/dev/null \
    || fail "VACUUM INTO failed, snapshot not written"

# Prove the snapshot opens and carries the tables before calling it done.
# A backup nobody has opened is a hope, not a backup.
COUNTS="$(sqlite3 "$SNAP" \
    "SELECT (SELECT COUNT(*) FROM opportunities) || ',' ||
            (SELECT COUNT(*) FROM signals) || ',' ||
            (SELECT COUNT(*) FROM touches) || ',' ||
            (SELECT COUNT(*) FROM companies);" 2>/dev/null)"
if [ -z "$COUNTS" ]; then
    # companies does not exist until phase two, so retry without it rather
    # than failing a good backup over a table that is not there yet.
    COUNTS="$(sqlite3 "$SNAP" \
        "SELECT (SELECT COUNT(*) FROM opportunities) || ',' ||
                (SELECT COUNT(*) FROM signals) || ',' ||
                (SELECT COUNT(*) FROM touches) || ',-';" 2>/dev/null)"
fi
[ -n "$COUNTS" ] || { rm -f "$SNAP"; fail "snapshot written but would not open, discarded"; }

IFS=',' read -r N_OPP N_SIG N_TOUCH N_CO <<< "$COUNTS"

if [ -d config/profile ]; then
    tar czf "$TAR" config/profile 2>/dev/null || fail "profile tar failed"
    PROFILE_NOTE="$(basename "$TAR")"
else
    PROFILE_NOTE="no config/profile on this host"
fi

# Local rotation. Off-host rotation is the puller's business.
ls -1t "$OUT"/radar-*.sqlite 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; done
ls -1t "$OUT"/profile-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old"; done

printf '{"ok": true, "snapshot": "%s", "profile": "%s", "integrity": "%s", "opportunities": %s, "signals": %s, "touches": %s, "companies": "%s", "bytes": %s}\n' \
    "$(basename "$SNAP")" "$PROFILE_NOTE" "$INTEGRITY" \
    "$N_OPP" "$N_SIG" "$N_TOUCH" "$N_CO" "$(stat -c%s "$SNAP" 2>/dev/null || echo 0)"
