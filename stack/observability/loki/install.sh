#!/usr/bin/env bash
# loki — log aggregation. Single-binary mode for kx.
#
# The chart comes from grafana-community, not grafana. The OSS Loki chart moved
# there, forked at 6.55.0; what is still published at grafana/helm-charts is the
# Grafana Enterprise Logs chart. That one is not marked deprecated and still
# installs, so a pin left on the original repository resolves to a different
# product with nothing reporting it — the description is the only field that
# differs.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add grafana-community https://grafana-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update grafana-community >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install loki grafana-community/loki \
  --namespace monitoring \
  --version 18.7.5 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
