#!/usr/bin/env bash
# keda — event-driven autoscaling
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
helm repo update kedacore >/dev/null

kubectl create namespace keda --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --version 2.20.2 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
