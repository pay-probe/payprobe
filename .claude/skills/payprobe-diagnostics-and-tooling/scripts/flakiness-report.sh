#!/usr/bin/env bash
# flakiness-report.sh — flaky scenarios + daily regression trend in one view.
#
# Calls:
#   GET $ORCH_URL/runs/flakiness?days=&min_runs=[&label=]
#   GET $ORCH_URL/runs/trend?days=[&label=]
# and prints both tables plus the interpretation cheat-sheet.
#
# Usage:
#   ./flakiness-report.sh                # last 30 days, min 3 runs per scenario
#   ./flakiness-report.sh 14             # last 14 days
#   ./flakiness-report.sh 30 nightly     # only runs labelled "nightly"
#   MIN_RUNS=5 ./flakiness-report.sh
#
# Env:
#   ORCH_URL   orchestrator base (default http://localhost:8100)
#   TOKEN      bearer token; optional when the dev auth gate is open
#   MIN_RUNS   minimum runs a scenario must appear in to be scored (default 3)
#
# Requires: bash, curl, jq.
# Exit code: 0 on success, 2 if the orchestrator is unreachable/unauthorized.

set -euo pipefail

DAYS="${1:-30}"
LABEL="${2:-}"
MIN_RUNS="${MIN_RUNS:-3}"
ORCH_URL="${ORCH_URL:-http://localhost:8100}"
TOKEN="${TOKEN:-}"

fetch() {
  if [ -n "$TOKEN" ]; then
    curl -fsS --max-time 20 -H "Authorization: Bearer $TOKEN" "$1"
  else
    curl -fsS --max-time 20 "$1"
  fi
}

flaky_url="$ORCH_URL/runs/flakiness?days=$DAYS&min_runs=$MIN_RUNS"
trend_url="$ORCH_URL/runs/trend?days=$DAYS"
if [ -n "$LABEL" ]; then
  flaky_url="$flaky_url&label=$LABEL"
  trend_url="$trend_url&label=$LABEL"
fi

if ! flaky="$(fetch "$flaky_url" 2>/dev/null)"; then
  echo "error: GET /runs/flakiness unreachable or unauthorized (set TOKEN?)" >&2
  exit 2
fi

echo "== flaky scenarios (last $DAYS days, min $MIN_RUNS runs${LABEL:+, label=$LABEL}) =="
count="$(printf '%s' "$flaky" | jq 'length')"
if [ "$count" -eq 0 ]; then
  echo "none flipped -- every scored scenario was always-pass or always-fail."
  echo "(always-fail is a regression, not flakiness: check the trend below.)"
else
  printf 'score\tflips\truns\tpass\tfail\tlast\tname\n'
  printf '%s' "$flaky" \
    | jq -r '.[] | [.score, .flips, .runs, .passed, .failed, .last_status, .name] | @tsv'
fi

echo
echo "== daily trend (last $DAYS days${LABEL:+, label=$LABEL}) =="
if trend="$(fetch "$trend_url" 2>/dev/null)"; then
  printf 'date\truns\tpassed\tfailed\tpass_rate\n'
  printf '%s' "$trend" \
    | jq -r '.[] | [.date, .runs, .passed, .failed, (.pass_rate // "-")] | @tsv'
else
  echo "warn: GET /runs/trend unavailable" >&2
fi

cat <<'NOTE'

interpretation:
  score = flips / (runs - 1), in 0..1. Only scenarios that BOTH passed and
  failed in the window are listed.
    1.0        alternates every run -> ordering / shared-state / timing bug
    high+even  pass/fail split      -> classic race or timeout signature
    low score  a single blip        -> if last=failed it may be a fresh
                                       regression that just started, not flakiness
  Empty table does not mean healthy: a scenario failing EVERY run never flips.
  Read the trend -- a persistent pass_rate step-down is a landed regression
  (bisect with GET /runs/{id}/compare).
NOTE
