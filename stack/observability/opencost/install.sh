#!/usr/bin/env bash
# opencost — Kubernetes cost monitoring (reads from in-cluster prometheus)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add opencost https://opencost.github.io/opencost-helm-chart >/dev/null 2>&1 || true
helm repo update opencost >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install opencost opencost/opencost \
  --namespace monitoring \
  --version 2.5.29 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
