#!/usr/bin/env bash
# loki — log aggregation. Single-binary mode for kx.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install loki grafana/loki \
  --namespace monitoring \
  --version 7.0.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
