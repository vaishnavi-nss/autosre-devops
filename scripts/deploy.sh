#!/usr/bin/env bash
# Deploy the AutoSRE chain to the current kubectl context.
# Run scripts/build-images.sh first.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> namespace"
kubectl apply -f k8s/namespace.yaml

echo "==> services"
kubectl apply -f k8s/

echo "==> waiting for rollouts"
for svc in database transaction-service api-gateway frontend correlation-engine second-opinion; do
  kubectl -n autosre rollout status "deployment/${svc}" --timeout=120s
done

# These need the Prometheus Operator CRDs, which come from kube-prometheus-stack.
if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then
  echo "==> monitoring"
  kubectl apply -f monitoring/servicemonitor.yaml -f monitoring/alert-rules.yaml
else
  echo "!! Prometheus Operator CRDs not found - skipping ServiceMonitor and alert rules."
  echo "   Install kube-prometheus-stack first, see docs/setup-wsl2.md."
fi

echo
echo "==> deployed"
kubectl -n autosre get pods,svc
echo
echo "Open the frontend with:  minikube service frontend -n autosre"
