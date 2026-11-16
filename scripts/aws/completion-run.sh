#!/usr/bin/env bash
# Run on the lab instance as root (for example through SSM, see start-completion-run.sh):
#
#   RUN=comp1 LIVE=0 SCENARIOS=guardrails_dry,pool,memory,disk,unknown bash completion-run.sh
#
# 1. fetches the BRANCH, 2. rebuilds the seven images inside minikube's Docker, 3. applies the manifests, monitoring rules and
# dashboard, 4. checks the playbooks (ansible syntax-check) inside the dispatcher image, 5. runs scripts/scenarios/run_scenarios.py.
# LIVE=0 (default) records decisions only. LIVE=1 switches AUTO_REMEDIATE on for the run and the runner switches it off again.
# Progress goes to the SSM parameter `autosre-prep`; results sync to s3://$BUCKET/results-$RUN every 45 s.
set -uo pipefail
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
BUCKET="${BUCKET:-autosre-validation-841736140404}"
REPO="${REPO:-https://github.com/vaishnavi-nss/autosre-devops}"
BRANCH="${BRANCH:-devops-completion}"
RUN="${RUN:?set RUN, e.g. comp1}"
LIVE="${LIVE:-0}"
SCENARIOS="${SCENARIOS:-guardrails_dry,pool,memory,disk,unknown}"
COOLDOWN="${COOLDOWN:-900}"          # dispatcher cooldown (s) during the run; lower it only to repeat a scenario
HOME_DIR=/home/ubuntu
WORK="$HOME_DIR/autosre-completion"
RESULTS="$HOME_DIR/results/$RUN"

st() { aws ssm put-parameter --name autosre-prep --type String --overwrite --value "$(date -u +%H:%M) $1" >/dev/null 2>&1 || true; }

st "$RUN: fetching $BRANCH"
# Stop a previous runner and WAIT for it to restore the safe defaults (it handles SIGTERM); the [s] keeps pkill from matching this script.
pkill -TERM -f 'run_scenario[s]' || true
for _ in $(seq 1 60); do pgrep -f 'run_scenario[s]' >/dev/null || break; sleep 5; done
rm -rf /tmp/src-completion && git clone -q --depth 1 -b "$BRANCH" "$REPO" /tmp/src-completion || { st "$RUN: git clone failed"; exit 1; }
SRC=/tmp/src-completion
[ -d "$SRC/autosre" ] && SRC=/tmp/src-completion/autosre
rm -rf "$WORK" && mkdir -p "$WORK" && cp -a "$SRC/." "$WORK/" && chown -R ubuntu:ubuntu "$WORK"
COMMIT=$(git -C /tmp/src-completion rev-parse --short HEAD)
mkdir -p "$RESULTS" && chown -R ubuntu:ubuntu "$HOME_DIR/results"

cat > "$HOME_DIR/prep-completion.sh" <<'EOF'
#!/bin/bash
set -u
minikube status >/dev/null 2>&1 || minikube start --driver=docker
cd /home/ubuntu/autosre-completion
bash scripts/build-images.sh 2>&1 | tail -3
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/ 2>&1 | tail -20
if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
  kubectl apply -f monitoring/servicemonitor.yaml -f monitoring/alert-rules.yaml
  bash scripts/install-dashboard.sh 2>&1 | tail -3 || true
fi
# `kubectl apply` leaves environment overrides that an earlier run set with `kubectl set env`. Force the safe defaults explicitly.
kubectl -n autosre set env deploy/remediation-dispatcher AUTO_REMEDIATE=false COOLDOWN_SECONDS- VERIFY_TIMEOUT_SECONDS- >/dev/null
kubectl -n autosre set env deploy/transaction-service CHAOS_ENABLED=false >/dev/null
kubectl -n autosre set env deploy/database CHAOS_ENABLED=false >/dev/null
kubectl -n autosre set env deploy/correlation-engine SECOND_OPINION_MODE=shadow ESCALATE_ON_AMBIGUITY- >/dev/null
# Replace every pod so none carries a stale image or an environment override from an earlier session.
for d in frontend api-gateway transaction-service database correlation-engine second-opinion remediation-dispatcher; do
  kubectl -n autosre rollout restart deploy/$d >/dev/null
done
for d in frontend api-gateway transaction-service database correlation-engine second-opinion remediation-dispatcher; do
  kubectl -n autosre rollout status deploy/$d --timeout=300s | tail -1
done
EOF
chmod +x "$HOME_DIR/prep-completion.sh"; chown ubuntu:ubuntu "$HOME_DIR/prep-completion.sh"
st "$RUN: commit $COMMIT; building and deploying"
PREP=$(sudo -u ubuntu -H -g docker bash "$HOME_DIR/prep-completion.sh" 2>&1 | tail -12 | tr '\n' '|' | cut -c1-500)
st "$RUN: deployed ($PREP); settling 4 min"
echo "$PREP" > "$RESULTS/deploy.txt"
sleep 240

# Static checks of the playbooks and the image, recorded with the results.
{
  echo "== dispatcher pod: ansible, collections and playbook syntax (checked inside the running pod; this minikube uses containerd, so docker run cannot see the images)"
  sudo -u ubuntu -H bash -c 'kubectl -n autosre exec deploy/remediation-dispatcher -- sh -c "ansible-playbook --version | head -1; ansible-galaxy collection list 2>/dev/null | grep -i kubernetes.core; cd /app/ansible && for p in playbooks/*.yml; do echo syntax-check \$p; ansible-playbook -i inventory/localhost.yml \$p --syntax-check 2>&1 | tail -2; done"'
} > "$RESULTS/image-checks.txt" 2>&1
st "$RUN: image checks written"

setsid nohup bash -c "while true; do aws s3 sync $RESULTS s3://$BUCKET/results-$RUN --only-show-errors; aws s3 cp $HOME_DIR/$RUN.log s3://$BUCKET/results-$RUN/$RUN.log --only-show-errors; sleep 45; done" >/dev/null 2>&1 </dev/null &
LIVEFLAG=""; [ "$LIVE" = "1" ] && LIVEFLAG="--live"
sudo -u ubuntu -H -g docker bash -c "cd $WORK && setsid nohup python3 scripts/scenarios/run_scenarios.py --out $RESULTS --scenarios $SCENARIOS --cooldown $COOLDOWN $LIVEFLAG > $HOME_DIR/$RUN.log 2>&1 < /dev/null &"
sleep 5
st "$RUN: scenario runner started on $COMMIT (live=$LIVE): $SCENARIOS"
