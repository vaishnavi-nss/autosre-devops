#!/usr/bin/env bash
# Run on the lab instance: list the metric names Prometheus has that match a pattern (extended regex), e.g.  bash prom-metrics.sh 'kube_endpoint|paused'
set -uo pipefail
OWNER="${OWNER:-ubuntu}"
sudo -u "$OWNER" env KUBECONFIG="/home/$OWNER/.kube/config" kubectl get --raw \
  /api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090/proxy/api/v1/label/__name__/values \
  | python3 -c 'import json,re,sys; pat=re.compile(sys.argv[1]); print("\n".join(n for n in json.load(sys.stdin)["data"] if pat.search(n)))' "${1:-kube_}"
