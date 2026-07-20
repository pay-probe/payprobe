#!/usr/bin/env bash
# payprobe-doctor.sh — one-shot platform health verdict (measure, don't eyeball).
#
# Calls, in order:
#   GET $ORCH_URL/health          orchestrator liveness            (public)
#   GET $SCENARIO_URL/health      scenario-service liveness        (public)
#   GET $AUTH_URL/health          auth-service liveness            (public)
#   GET $ORCH_URL/ready           readiness: run store + Redis     (public; 503 = not ready)
#   GET $ORCH_URL/status          dependency pulse                 (public)
#   GET $ORCH_URL/diagnostics     layered platform doctor          (needs TOKEN when auth enforced)
# and pretty-prints only the problems, each with its fix hint.
#
# Usage:
#   ./payprobe-doctor.sh
#   TOKEN=<jwt> ./payprobe-doctor.sh
#   LAYERS=connections,listeners ./payprobe-doctor.sh     # scope the doctor pass
#   CONNECTION=visa ENVIRONMENT=mock ./payprobe-doctor.sh
#
# Env:
#   ORCH_URL      orchestrator base       (default http://localhost:8100)
#   SCENARIO_URL  scenario-service base   (default http://localhost:8000)
#   AUTH_URL      auth-service base       (default http://localhost:8300)
#   TOKEN         bearer token; optional when the dev auth gate is open
#                 (mint one: curl -s -X POST $AUTH_URL/token -d username=admin -d password=<pw> | jq -r .access_token)
#   LAYERS, CONNECTION, ENVIRONMENT   optional /diagnostics scoping
#
# Requires: bash, curl, jq.
# Exit code: 0 = healthy, 1 = degraded/failing, 2 = a service was unreachable.

set -euo pipefail

ORCH_URL="${ORCH_URL:-http://localhost:8100}"
SCENARIO_URL="${SCENARIO_URL:-http://localhost:8000}"
AUTH_URL="${AUTH_URL:-http://localhost:8300}"
TOKEN="${TOKEN:-}"
LAYERS="${LAYERS:-}"
CONNECTION="${CONNECTION:-}"
ENVIRONMENT="${ENVIRONMENT:-}"

fetch() {
  if [ -n "$TOKEN" ]; then
    curl -fsS --max-time 20 -H "Authorization: Bearer $TOKEN" "$1"
  else
    curl -fsS --max-time 20 "$1"
  fi
}

rc=0

echo "== liveness =="
for svc in "orchestrator|$ORCH_URL/health" \
           "scenario-service|$SCENARIO_URL/health" \
           "auth-service|$AUTH_URL/health"; do
  name="${svc%%|*}"
  url="${svc#*|}"
  if fetch "$url" >/dev/null 2>&1; then
    echo "ok    $name  ($url)"
  else
    echo "FAIL  $name  ($url) -- unreachable"
    rc=2
  fi
done

echo
echo "== readiness (orchestrator /ready) =="
if ready_json="$(fetch "$ORCH_URL/ready" 2>/dev/null)"; then
  printf '%s\n' "$ready_json" \
    | jq -r '"ready=\(.ready)  " + (.checks | to_entries | map("\(.key)=\(.value)") | join("  "))'
else
  echo "NOT READY -- /ready returned 503 or is unreachable (run store or Redis down)"
  if [ "$rc" -eq 0 ]; then rc=1; fi
fi

echo
echo "== dependency pulse (orchestrator /status) =="
if status_json="$(fetch "$ORCH_URL/status" 2>/dev/null)"; then
  overall="$(printf '%s' "$status_json" | jq -r .status)"
  echo "overall: $overall"
  printf '%s\n' "$status_json" \
    | jq -r '.services | to_entries[]
        | "\(.value.status)\t\(.key)\t\((.value.error // .value.detail // "") | tostring | .[0:120])"'
  if [ "$overall" != "ok" ] && [ "$rc" -eq 0 ]; then rc=1; fi
else
  echo "FAIL  GET /status -- unreachable"
  rc=2
fi

echo
echo "== platform doctor (orchestrator /diagnostics) =="
diag_url="$ORCH_URL/diagnostics"
sep="?"
if [ -n "$LAYERS" ]; then diag_url="$diag_url${sep}layers=$LAYERS"; sep="&"; fi
if [ -n "$CONNECTION" ]; then diag_url="$diag_url${sep}connection=$CONNECTION"; sep="&"; fi
if [ -n "$ENVIRONMENT" ]; then diag_url="$diag_url${sep}environment=$ENVIRONMENT"; sep="&"; fi

if diag_json="$(fetch "$diag_url" 2>/dev/null)"; then
  overall="$(printf '%s' "$diag_json" | jq -r .status)"
  printf '%s\n' "$diag_json" | jq -r \
    '"doctor: \(.status)  (ok=\(.summary.ok) warn=\(.summary.warn) fail=\(.summary.fail) skip=\(.summary.skip) disabled=\(.summary.disabled), \(.duration_ms) ms, layers=\(.layers | join(",")))"'
  problems="$(printf '%s' "$diag_json" \
    | jq -r '.checks[] | select(.status == "fail" or .status == "warn")
        | "\(.status | ascii_upcase)  [\(.layer)] \(.name)" +
          (if .target != "" and .target != null then " -> \(.target)" else "" end) +
          "\n      error: \((.error // .detail // "-") | tostring | .[0:200])" +
          "\n      fix:   \(.hint // "-")"')"
  if [ -n "$problems" ]; then
    printf '%s\n' "$problems"
  else
    echo "no failing or warning checks"
  fi
  if [ "$overall" != "ok" ] && [ "$rc" -eq 0 ]; then rc=1; fi
else
  echo "FAIL  GET /diagnostics -- unreachable or unauthorized (set TOKEN?)"
  rc=2
fi

exit "$rc"
