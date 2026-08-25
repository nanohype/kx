#!/usr/bin/env bash
# kyverno — admission policy engine
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add kyverno https://kyverno.github.io/kyverno/ >/dev/null 2>&1 || true
helm repo update kyverno >/dev/null

kubectl create namespace kyverno --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kyverno kyverno/kyverno \
  --namespace kyverno \
  --version 3.8.2 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
