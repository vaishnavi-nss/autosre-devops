#!/usr/bin/env bash
# Run on the lab instance as root (for example through SSM) after the lab has been stopped and started again.
# Starts Minikube, waits for every AutoSRE deployment, forces the SAFE defaults (so a run that was cut off cannot leave live remediation
# or fault injection switched on), restarts the page forwards and prints the state. It never turns anything live on.
set -uo pipefail
OWNER="${OWNER:-ubuntu}"
as() { sudo -u "$OWNER" -H env KUBECONFIG="/home/$OWNER/.kube/config" "$@"; }
say() { echo "$(date -u +%T) $*"; }

say "minikube status: $(as minikube status --format '{{.Host}}/{{.Kubelet}}/{{.APIServer}}' 2>&1 | tr '\n' ' ')"
if ! as minikube status >/dev/null 2>&1; then
  say "starting minikube"
  as minikube start --driver=docker 2>&1 | tail -3
fi
for _ in $(seq 1 60); do as kubectl get nodes >/dev/null 2>&1 && break; sleep 5; done
say "node: $(as kubectl get nodes --no-headers 2>&1 | awk '{print $1,$2}')"

# Safe defaults, exactly as the launcher forces them.
as kubectl -n autosre set env deploy/remediation-dispatcher AUTO_REMEDIATE=false COOLDOWN_SECONDS- VERIFY_TIMEOUT_SECONDS- >/dev/null 2>&1
as kubectl -n autosre set env deploy/transaction-service CHAOS_ENABLED=false >/dev/null 2>&1
as kubectl -n autosre set env deploy/database CHAOS_ENABLED=false >/dev/null 2>&1
as kubectl -n autosre set env deploy/correlation-engine SECOND_OPINION_MODE=shadow ESCALATE_ON_AMBIGUITY- >/dev/null 2>&1
as kubectl -n autosre scale deploy/transaction-service --replicas=1 >/dev/null 2>&1
for d in frontend api-gateway transaction-service database correlation-engine second-opinion remediation-dispatcher loadgen; do
  as kubectl -n autosre rollout status "deploy/$d" --timeout=300s 2>&1 | tail -1
done
say "monitoring: $(as kubectl -n monitoring get pods --no-headers 2>&1 | awk '{print $1":"$3}' | cut -c1-40 | tr '\n' ' ')"

systemctl restart autosre-pf-frontend autosre-pf-grafana autosre-pf-prometheus autosre-fault-console 2>&1 | tail -1
sleep 8
for p in 8081 3000 9090 8082 8080; do printf "port %s -> HTTP " "$p"; curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:$p/" || echo fail; done
A=$(as kubectl -n autosre get deploy remediation-dispatcher -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='AUTO_REMEDIATE')].value}")
say "AUTO_REMEDIATE=$A"
as kubectl -n autosre get deploy --no-headers | awk '{print $1,$2}' | tr '\n' ' '; echo
say "BRING-UP DONE"
