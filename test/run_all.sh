#!/usr/bin/env bash
# MedTech Radar acceptance suite. Runs the three workstream test runners in
# order against one throwaway database path. Works in Git Bash on Windows and
# in plain bash on Linux. Exits non-zero if any runner fails.
#
# Usage:  bash test/run_all.sh

set -u

# Resolve the Python interpreter. Prefer python3, fall back to python.
# A candidate must actually run. The Windows Store ships a fake python3
# alias that prints an install nag and exits non-zero, so probe each one.
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c "import sys" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: no working python3 or python on PATH" >&2
    exit 1
fi

# cd to the repo root, one level up from this script.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR/.." || exit 1

TEST_DB="test/test_radar.sqlite"
rm -f "$TEST_DB"

# Mock mode detection, matching the runners. An explicit RADAR_MOCK forces
# mock. Otherwise live only when ANTHROPIC_API_KEY is present in the
# environment or set to a non-empty value in .env.
have_key=0
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    have_key=1
elif [ -f .env ] && grep -Eq '^[[:space:]]*ANTHROPIC_API_KEY[[:space:]]*=[[:space:]]*[^[:space:]#]+' .env; then
    have_key=1
fi

if [ -n "${RADAR_MOCK:-}" ]; then
    echo "=========================================================================="
    echo "MOCK MODE - RADAR_MOCK set explicitly, no live API calls this run."
    echo "=========================================================================="
elif [ "$have_key" -eq 0 ]; then
    export RADAR_MOCK=1
    echo "=========================================================================="
    echo "MOCK MODE - no API key found in env or .env"
    echo "Scoring uses the deterministic mocks in test/mocks.py and mocks_signals.py."
    echo "Fill ANTHROPIC_API_KEY in .env and rerun for live validation."
    echo "=========================================================================="
else
    echo "Live mode. ANTHROPIC_API_KEY found, runners will call the real API."
fi

overall=0
results=""

run_one() {
    name="$1"
    script="$2"
    echo
    echo "--------------------------------------------------------------------------"
    echo "RUNNING $name"
    echo "--------------------------------------------------------------------------"
    "$PY" "$script" --db "$TEST_DB"
    status=$?
    if [ "$status" -eq 0 ]; then
        results="$results
PASS  $name"
    else
        results="$results
FAIL  $name (exit $status)"
        overall=1
    fi
}

run_one "unit checks      (test/run_unit_test.py)"    "test/run_unit_test.py"
run_one "inbox pipeline   (test/run_inbox_test.py)"   "test/run_inbox_test.py"
run_one "monday digest    (test/run_digest_test.py)"  "test/run_digest_test.py"
run_one "signals pipeline (test/run_signals_test.py)" "test/run_signals_test.py"

echo
echo "=========================================================================="
echo "SUMMARY$results"
echo "=========================================================================="
if [ "$overall" -ne 0 ]; then
    echo "RESULT: FAIL"
else
    echo "RESULT: PASS. All runners green."
fi
exit "$overall"
