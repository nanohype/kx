#!/usr/bin/env bash
# kube-prometheus-stack — prometheus + grafana + alertmanager + node-exporter + kube-state-metrics
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --version 88.2.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
