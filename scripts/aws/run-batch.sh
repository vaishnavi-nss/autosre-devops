#!/usr/bin/env bash
# Run on the lab instance (as root, for example through SSM). Starts a named batch of fault scenarios in the background through the fault console's own API
# (demo-runs.sh), logging to /home/ubuntu/batch-<name>.out, and returns at once. The scenario names are the runner's keys (docs/completion).
#
#   bash run-batch.sh sim-escalate     faults the system must hand to a person (simulate: nothing is fixed, and nothing needs to be)
#   bash run-batch.sh sim-act          faults the system should fix, as dry runs: alert, diagnosis and chosen playbook only (NOT recoveries)
#   APPROVED_LIVE=yes bash run-batch.sh live-act     the same faults for real; only with the owner's explicit approval
set -uo pipefail
NAME="${1:?usage: run-batch.sh sim-escalate|sim-act|live-act|live-<scenarios,comma,separated>}"
OWNER="${OWNER:-ubuntu}"
DIR="/home/$OWNER/autosre-console"
case "$NAME" in
  sim-escalate) MODE=simulate; LIST="pool:partial memory:light disk:threshold rollout:tx-image rollout:gw-probe unknown:latency unknown:errors unknown:partition unknown:podkill" ;;
  sim-act)      MODE=simulate; LIST="down:database down:transaction-service down:api-gateway rollout:tx-crash rollout:gw-crash" ;;
  live-act)     MODE=live;     LIST="down:database down:transaction-service down:api-gateway rollout:tx-crash rollout:gw-crash" ;;
  live-*)       MODE=live;     LIST="$(echo "${NAME#live-}" | tr ',' ' ')" ;;
  sim-*)        MODE=simulate; LIST="$(echo "${NAME#sim-}" | tr ',' ' ')" ;;
  *) echo "unknown batch $NAME"; exit 2 ;;
esac
if [ "$MODE" = live ] && [ "${APPROVED_LIVE:-}" != yes ]; then echo "refusing a live batch without APPROVED_LIVE=yes"; exit 2; fi
sudo -u "$OWNER" git -C "$DIR" pull -q 2>&1 | tail -1
[ -d "/home/$OWNER/autosre-completion/.git" ] && sudo -u "$OWNER" git -C "/home/$OWNER/autosre-completion" pull -q 2>&1 | tail -1
OUT="/home/$OWNER/batch-$NAME.out"
sudo -u "$OWNER" -H env KUBECONFIG="/home/$OWNER/.kube/config" MODE="$MODE" APPROVED_LIVE="${APPROVED_LIVE:-}" SCENARIOS="$LIST" setsid nohup bash "$DIR/scripts/aws/demo-runs.sh" > "$OUT" 2>&1 < /dev/null &
echo "started batch $NAME ($MODE): $LIST"
echo "log: $OUT"
