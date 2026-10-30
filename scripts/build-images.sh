#!/usr/bin/env bash
set -euo pipefail

echo "==> Building AutoSRE container images (non-root)..."

SERVICES=("frontend" "api-gateway" "transaction_service" "database")

for svc in "${SERVICES[@]}"; do
  echo "--> Building autosre-${svc}..."
  docker build -t "autosre-${svc}:latest" "services/${svc}"
done

echo "==> All service images successfully built."
