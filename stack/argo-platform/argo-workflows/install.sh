#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update argo >/dev/null

kubectl create namespace argo --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install argo-workflows argo/argo-workflows \
  --namespace argo \
  --version 1.0.23 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
