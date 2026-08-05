#!/usr/bin/env bash
# grafana-operator — manage Grafana dashboards/datasources/folders via CRDs
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install grafana-operator grafana/grafana-operator \
  --namespace monitoring \
  --version 5.24.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
