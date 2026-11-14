#!/usr/bin/env bash
# Confirm a request travels the whole chain inside the cluster.
set -euo pipefail

echo "==> calling the frontend from inside the cluster"
kubectl -n autosre run smoke-test \
  --rm -i --restart=Never --image=curlimages/curl:8.11.1 -- \
  curl -sS --max-time 10 http://frontend.autosre.svc.cluster.local:8000/

echo
echo "Expect nested \"downstream\" objects ending at the database service."
