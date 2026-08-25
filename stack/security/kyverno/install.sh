#!/usr/bin/env bash
# kyverno — admission policy engine
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add kyverno https://kyverno.github.io/kyverno/ --force-update >/dev/null
helm repo update kyverno >/dev/null

kubectl create namespace kyverno --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kyverno kyverno/kyverno \
  --namespace kyverno \
  --version 3.8.2 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
