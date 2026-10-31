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

# A dropped connection to minikube's Docker daemon is transient, so retry.
build() {
  local tag="$1" context="$2" attempt
  for attempt in 1 2 3; do
    docker build -t "$tag" "$context" && return 0
    echo "build of ${tag} failed (attempt ${attempt}/3); retrying" >&2
    sleep 5
  done
  return 1
}

for svc in frontend api-gateway transaction-service database; do
  echo "==> building autosre/${svc}:dev"
  build "autosre/${svc}:dev" "services/${svc}"
done

echo "==> building autosre/correlation-engine:dev"
build autosre/correlation-engine:dev correlation-engine

echo "==> done"
# The listing is cosmetic; the builds above already succeeded, so never fail on it.
docker images --filter=reference='autosre/*' --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' \
  || echo "(image listing unavailable; the builds above succeeded)"
