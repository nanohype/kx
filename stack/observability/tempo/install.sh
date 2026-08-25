#!/usr/bin/env bash
# tempo — distributed tracing backend. Single-binary mode for kx.
#
# The chart comes from grafana-community, not grafana. grafana/helm-charts
# deprecated it and named that repository as the destination; the fork carries
# the full history, so this is the same chart with the same topology, renumbered
# at the fork point (1.24.4 -> 2.x).
#
# tempo-distributed is not the alternative it looks like — it was deprecated in
# the same move. There is no single-binary chart left at the original repo to
# fall back to.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add grafana-community https://grafana-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana-community >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install tempo grafana-community/tempo \
  --namespace monitoring \
  --version 2.2.3 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
