#!/usr/bin/env bash
# collect-run-bundle.sh — snapshot every diagnostic artifact for one run into a
# directory: the "measured, not assumed" evidence pack to attach to a bug
# report or review.
#
# Fetches (best-effort; anything unavailable is skipped with a warning):
#   run.json          GET /runs/{id}            full detail incl. per-step trace + raw_log
#   trace.json        extracted from run.json   per-step timing waterfall + wire log + engine log
#   junit.xml         GET /runs/{id}/junit
#   report.html       GET /runs/{id}/html
#   diagnose.json     GET /runs/{id}/diagnose   failure-taxonomy verdicts (+ doctor ride-along)
#   compare.json      GET /runs/{id}/compare    diff vs previous like-for-like run
#   load-run.json     GET /load-runs/{id}       only when the id is a load run
#   load-compare.json GET /load-runs/{id}/compare   (tps/latency/error_categories regression diff)
#
# Usage:
#   ./collect-run-bundle.sh <run_id> [out_dir]     # out_dir defaults to ./run-bundle-<run_id>
#
# Env:
#   ORCH_URL   orchestrator base (default http://localhost:8100)
#   TOKEN      bearer token; optional when the dev auth gate is open
#
# Requires: bash, curl, jq.
# Exit code: 0 on success (even with skipped extras), 1 if the run id doesn't
# exist, 2 on usage error.

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <run_id> [out_dir]" >&2
  exit 2
fi

RUN_ID="$1"
OUT="${2:-run-bundle-$RUN_ID}"
ORCH_URL="${ORCH_URL:-http://localhost:8100}"
TOKEN="${TOKEN:-}"

fetch() {
  if [ -n "$TOKEN" ]; then
    curl -fsS --max-time 30 -H "Authorization: Bearer $TOKEN" "$1"
  else
    curl -fsS --max-time 30 "$1"
  fi
}

mkdir -p "$OUT"

# grab <url-path> <file> <description>  -> 0 if fetched, 1 if skipped
grab() {
  path="$1"; file="$2"; desc="$3"
  if fetch "$ORCH_URL$path" > "$OUT/$file" 2>/dev/null && [ -s "$OUT/$file" ]; then
    echo "ok    $file  ($desc)"
    return 0
  fi
  rm -f "$OUT/$file"
  echo "skip  $file  ($desc -- not available)"
  return 1
}

if ! grab "/runs/$RUN_ID" "run.json" "full run detail"; then
  echo "error: run '$RUN_ID' not found at $ORCH_URL (or unauthorized -- set TOKEN?)" >&2
  exit 1
fi

# Execution trace: the waterfall data already lives inside run.json -- extract
# a standalone trace.json (per step: timing, wire bytes, engine-log lines).
if jq '{run_id: .id, status: .status, label: .label,
        scenarios: [ (.summary.scenarios // [])[]
          | { scenario: (.name // .scenario_id),
              status: .status,
              steps: [ (.steps // [])[]
                | { step_id, target, action, status,
                    started_at_ms, duration_ms,
                    error, raw_log, trace } ] } ]}' \
      "$OUT/run.json" > "$OUT/trace.json" 2>/dev/null; then
  echo "ok    trace.json  (per-step timing waterfall + wire log + engine log)"
else
  rm -f "$OUT/trace.json"
  echo "skip  trace.json  (run.json had no parsable scenario steps)"
fi

grab "/runs/$RUN_ID/junit" "junit.xml" "JUnit XML" || true
grab "/runs/$RUN_ID/html" "report.html" "HTML report" || true
grab "/runs/$RUN_ID/diagnose" "diagnose.json" "failure-taxonomy verdicts" || true
grab "/runs/$RUN_ID/compare" "compare.json" "diff vs previous like-for-like run" || true

# Load-run extras only exist when this id is also a load run.
if grab "/load-runs/$RUN_ID" "load-run.json" "load-run summary (tps_series, error_categories, tps_stability)"; then
  grab "/load-runs/$RUN_ID/compare" "load-compare.json" "load regression diff vs previous" || true
fi

echo
echo "bundle: $OUT"
ls -l "$OUT"
