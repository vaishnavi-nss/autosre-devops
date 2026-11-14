#!/usr/bin/env bash
# Load the AutoSRE Grafana dashboard into a cluster running kube-prometheus-stack.
#
# Grafana's sidecar watches for ConfigMaps labelled grafana_dashboard=1 and
# imports whatever JSON they carry, so there is nothing to click through. Safe to
# re-run: it replaces the ConfigMap, and Grafana picks up the new version.
set -euo pipefail

cd "$(dirname "$0")/.."

kubectl -n monitoring create configmap autosre-dashboard \
  --from-file=autosre.json=monitoring/grafana-dashboards/autosre.json \
  --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml \
  | kubectl apply -f -

echo "dashboard installed - Grafana imports it within about a minute."
echo "open it with:  kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80"
echo "then browse to http://localhost:3000/d/autosre-main"
