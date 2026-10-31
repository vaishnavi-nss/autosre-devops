#!/usr/bin/env bash
# Turn a bare Ubuntu 24.04 EC2 instance into a single-node AutoSRE validation
# cluster, run the live validation, and publish the results to S3.
#
#   bootstrap.sh <bucket>
#
# Environment (all optional):
#   PER_CAUSE     trials per known root cause     (default 6)
#   UNKNOWN       trials with no known cause      (default 4)
#   DEADLINE_MIN  stop starting trials after this many minutes (default 150)
#
# Runs as root from EC2 user-data. Progress is written to the log and uploaded to
# s3://<bucket>/progress/ every minute, so a failure can be diagnosed without
# logging in to the instance. Results land in s3://<bucket>/results/.
#
# Needs an instance profile that can read and write the bucket. AWS Academy
# Learner Lab provides one (LabInstanceProfile); no IAM roles are created.
set -euo pipefail

BUCKET="${1:?usage: bootstrap.sh <bucket>}"
PER_CAUSE="${PER_CAUSE:-6}"
UNKNOWN="${UNKNOWN:-4}"
DEADLINE_MIN="${DEADLINE_MIN:-150}"

HOME_DIR=/home/ubuntu
REPO="$HOME_DIR/autosre"
RESULTS="$HOME_DIR/results"
LOG=/var/log/autosre-bootstrap.log

mkdir -p "$RESULTS"
chown ubuntu:ubuntu "$RESULTS"
exec > >(tee -a "$LOG") 2>&1

say()  { echo; echo "=== [$(date -u +%H:%M:%S)] $*"; }
as_ubuntu() { sudo -u ubuntu -H -g docker bash -c "$*"; }

publish() {
  aws s3 cp "$LOG" "s3://$BUCKET/progress/bootstrap.log" --only-show-errors || true
  aws s3 sync "$RESULTS" "s3://$BUCKET/results" --only-show-errors || true
}

diagnostics() {
  say "DIAGNOSTICS"
  as_ubuntu "kubectl get pods -A -o wide" || true
  as_ubuntu "kubectl -n autosre describe pods | tail -60" || true
  as_ubuntu "kubectl -n monitoring get events --sort-by=.lastTimestamp | tail -25" || true
  df -h / | tail -2 || true
  free -m || true
}

trap 'rc=$?; say "FAILED (exit $rc) at line $LINENO"; diagnostics; echo FAILED > "$RESULTS/STATUS"; publish; exit $rc' ERR

( while true; do sleep 60; publish; done ) &
echo RUNNING > "$RESULTS/STATUS"

say "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io conntrack curl jq unzip socat python3
usermod -aG docker ubuntu
systemctl enable --now docker

# Scripts may arrive with CRLF line endings or without the executable bit if the
# tarball was built on Windows.
find "$REPO" -name '*.sh' -exec sed -i 's/\r$//' {} +
chmod +x "$REPO"/scripts/*.sh
chown -R ubuntu:ubuntu "$REPO"

say "kubectl, minikube, helm"
curl -fsSL -o /usr/local/bin/kubectl \
  "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
curl -fsSL -o /usr/local/bin/minikube \
  https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
chmod +x /usr/local/bin/kubectl /usr/local/bin/minikube
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
kubectl version --client
minikube version
helm version --short

say "minikube cluster"
# Size the cluster from the host rather than assuming a big instance: AWS Academy
# Learner Lab denies instance types above "large" (2 vCPU / 8 GB), so leave 2 GB
# for the OS and Docker, and cap at what a bigger machine would not need.
MEM_MB=$(( $(free -m | awk '/^Mem:/{print $2}') - 2048 ))
[ "$MEM_MB" -gt 10240 ] && MEM_MB=10240
CPUS=$(nproc); [ "$CPUS" -gt 4 ] && CPUS=4
echo "host: $(nproc) vCPU, $(free -m | awk '/^Mem:/{print $2}') MB -> minikube: $CPUS cpus, ${MEM_MB} MB"
as_ubuntu "minikube start --driver=docker --cpus=$CPUS --memory=${MEM_MB}m"
as_ubuntu "kubectl get nodes"

say "build the service images inside minikube"
as_ubuntu "cd $REPO && ./scripts/build-images.sh"

say "monitoring stack (kube-prometheus-stack)"
as_ubuntu "helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && helm repo update"
as_ubuntu "cd $REPO && helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f monitoring/kube-prometheus-stack-values.yaml -f monitoring/validation-values.yaml \
  --wait --timeout 20m"

say "deploy AutoSRE"
as_ubuntu "cd $REPO && ./scripts/deploy.sh"

say "Grafana dashboard"
as_ubuntu "cd $REPO && ./scripts/install-dashboard.sh"

say "expose Grafana on :3000 (the security group restricts this to one IP)"
as_ubuntu "setsid nohup kubectl -n monitoring port-forward --address 0.0.0.0 svc/monitoring-grafana 3000:80 \
  >/tmp/grafana-forward.log 2>&1 < /dev/null &"
sleep 5

say "pipeline sanity check before the trials"
as_ubuntu "kubectl -n monitoring get prometheusrule,servicemonitor -A | head -20" || true
as_ubuntu "kubectl -n monitoring get secret alertmanager-monitoring-kube-prometheus-alertmanager \
  -o jsonpath='{.data.alertmanager\\.yaml}' | base64 -d | head -40" || true

publish
say "live validation: $PER_CAUSE per cause + $UNKNOWN unknown, deadline ${DEADLINE_MIN} min"
as_ubuntu "cd $REPO/correlation-engine && python3 evaluation/live_validate.py \
  --out $RESULTS --per-cause $PER_CAUSE --unknown $UNKNOWN --deadline-minutes $DEADLINE_MIN"

say "reports"
as_ubuntu "cd $REPO/correlation-engine && python3 evaluation/live_report.py --results $RESULTS" || true
as_ubuntu "cd $REPO/correlation-engine && python3 evaluation/check_dashboard.py \
  > $RESULTS/dashboard_check.txt 2>&1; cat $RESULTS/dashboard_check.txt" || true
as_ubuntu "kubectl get pods -A > $RESULTS/pods.txt 2>&1" || true

echo DONE > "$RESULTS/STATUS"
publish
say "finished - Grafana stays up on :3000 until the instance is stopped"
