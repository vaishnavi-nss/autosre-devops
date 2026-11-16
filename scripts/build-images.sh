#!/usr/bin/env bash
set -euo pipefail

echo "==> Checking Minikube..."

if ! command -v minikube >/dev/null 2>&1; then
    echo "ERROR: Minikube is not installed or not in PATH."
    exit 1
fi

if ! minikube status >/dev/null 2>&1; then
    echo "ERROR: Minikube is not running."
    exit 1
fi

echo "==> Configuring Minikube Docker environment..."
eval "$(minikube -p minikube docker-env)"

echo "==> Building all AutoSRE images..."

docker build -t autosre/frontend:dev services/frontend
docker build -t autosre/api-gateway:dev services/api-gateway
docker build -t autosre/transaction-service:dev services/transaction_service
docker build -t autosre/database:dev services/database
docker build -t autosre/correlation-engine:dev correlation-engine
docker build -t autosre/second-opinion:dev second-opinion
# The dispatcher image needs the playbooks, so it builds from the repository root.
docker build -t autosre/remediation-dispatcher:dev -f remediation-dispatcher/Dockerfile .

echo "==> All seven AutoSRE images successfully built."