#!/usr/bin/env bash
# Run on the lab instance as the user that owns kubectl (ubuntu). Starts fault scenarios one after another through the fault console's
# own API (so every console guard applies: token, one run at a time, preflight, restore) and waits for each to finish.
#
#   MODE=simulate SCENARIOS="pool memory disk" bash demo-runs.sh
#   MODE=live APPROVED_LIVE=yes SCENARIOS="pool memory disk" bash demo-runs.sh     # only with the owner's explicit approval
#
# simulate: the real fault is injected, AUTO_REMEDIATE stays false, nothing is fixed (not a recovery).
# live: AUTO_REMEDIATE is on for the run only; the runner restores it. The script refuses MODE=live unless APPROVED_LIVE=yes.
set -uo pipefail
MODE="${MODE:-simulate}"
SCENARIOS="${SCENARIOS:-pool memory disk}"
TOKEN="$(cat "$HOME/.fault-console-token")"
API=http://localhost:8082
if [ "$MODE" = live ] && [ "${APPROVED_LIVE:-}" != yes ]; then echo "refusing a live run without APPROVED_LIVE=yes"; exit 2; fi

running() { curl -s "$API/api/status" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["running"] else 1)'; }
for s in $SCENARIOS; do
  while running; do sleep 15; done
  body="{\"scenario\":\"$s\",\"mode\":\"$MODE\""
  [ "$MODE" = live ] && body="$body,\"confirm\":\"LIVE\""
  resp=$(curl -s -X POST -H "X-Token: $TOKEN" -H 'Content-Type: application/json' -d "$body}" "$API/api/run")
  echo "$(date -u +%T) START $s $MODE: $resp"
  sleep 25
  while running; do sleep 15; done
  curl -s "$API/api/status" | python3 -c '
import json, sys, time
j = json.load(sys.stdin)
print(time.strftime("%H:%M:%S", time.gmtime()), "END", j["job"]["scenario"], j["job"]["mode"], "|", j["phase"], "|", j["outcome"], "| auto_remediate", j["auto_remediate"], "| out", j["job"]["out"])'
done
echo "$(date -u +%T) ALL DONE"
