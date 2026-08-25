#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null

kubectl create namespace argo --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install argo-rollouts argo/argo-rollouts \
  --namespace argo \
  --version 2.41.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
