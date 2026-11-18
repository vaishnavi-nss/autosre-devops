#!/usr/bin/env bash
# Run on the lab instance as root (for example through SSM), after bring-up.sh. Brings the cluster to the latest commit of the branch without Jenkins:
# pulls the checkout, builds the images into Minikube, applies the manifests and the alert rules, forces the SAFE defaults, restarts the services and the
# fault console. Same steps as the Jenkinsfile's Build/Deploy stages. It never turns anything live on.
set -uo pipefail
OWNER="${OWNER:-ubuntu}"
DIR="/home/$OWNER/autosre-console"
as() { sudo -u "$OWNER" -H env KUBECONFIG="/home/$OWNER/.kube/config" "$@"; }
say() { echo "$(date -u +%T) $*"; }

as git -C "$DIR" pull -q 2>&1 | tail -1
say "commit: $(as git -C "$DIR" log --oneline -1)"
say "building images"
as bash -lc "cd $DIR && eval \$(minikube docker-env) && bash scripts/build-images.sh" 2>&1 | tail -4
say "applying manifests and alert rules"
as bash -c "cd $DIR && kubectl apply -f k8s/namespace.yaml && kubectl apply -f k8s/ && kubectl apply -f monitoring/alert-rules.yaml" 2>&1 | tail -3
as kubectl -n autosre set env deploy/remediation-dispatcher AUTO_REMEDIATE=false COOLDOWN_SECONDS- VERIFY_TIMEOUT_SECONDS- >/dev/null 2>&1
as kubectl -n autosre set env deploy/transaction-service CHAOS_ENABLED=false >/dev/null 2>&1
as kubectl -n autosre set env deploy/database CHAOS_ENABLED=false >/dev/null 2>&1
as kubectl -n autosre set env deploy/correlation-engine SECOND_OPINION_MODE=shadow ESCALATE_ON_AMBIGUITY- >/dev/null 2>&1
for d in frontend api-gateway transaction-service database correlation-engine second-opinion remediation-dispatcher; do as kubectl -n autosre rollout restart "deploy/$d" >/dev/null 2>&1; done
for d in frontend api-gateway transaction-service database correlation-engine second-opinion remediation-dispatcher loadgen; do as kubectl -n autosre rollout status "deploy/$d" --timeout=300s 2>&1 | tail -1; done
as kubectl -n autosre get pvc --no-headers 2>&1 | awk '{print $1,$2}' | tr '\n' ' '; echo
systemctl restart autosre-fault-console autosre-pf-frontend 2>&1 | tail -1
A=$(as kubectl -n autosre get deploy remediation-dispatcher -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name=='AUTO_REMEDIATE')].value}")
say "AUTO_REMEDIATE=$A"
say "UPDATE DONE"
