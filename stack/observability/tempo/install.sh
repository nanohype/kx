#!/usr/bin/env bash
# tempo — distributed tracing backend. Single-binary mode for kx.
# Note: `grafana/tempo` (single-binary) is marked deprecated by upstream but still functional.
# When/if it stops working, switch to `grafana/tempo-distributed` with a minimal microservices layout.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install tempo grafana/tempo \
  --namespace monitoring \
  --version 1.24.4 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
