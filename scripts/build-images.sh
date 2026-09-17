#!/usr/bin/env bash
# Build every AutoSRE image directly inside minikube's Docker daemon, so the
# cluster can run them without a registry and without pulling.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v minikube >/dev/null 2>&1; then
  echo "minikube not found - see docs/setup-wsl2.md" >&2
  exit 1
fi

echo "==> pointing docker at minikube"
eval "$(minikube docker-env)"

for svc in frontend api-gateway transaction-service database; do
  echo "==> building autosre/${svc}:dev"
  docker build -t "autosre/${svc}:dev" "services/${svc}"
done

echo "==> building autosre/correlation-engine:dev"
docker build -t autosre/correlation-engine:dev correlation-engine

echo "==> done"
docker images --filter=reference='autosre/*' --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}'
