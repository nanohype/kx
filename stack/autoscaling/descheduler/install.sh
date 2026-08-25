#!/usr/bin/env bash
# descheduler — rebalance pods periodically (runs as Deployment, polls + evicts)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add descheduler https://kubernetes-sigs.github.io/descheduler/ --force-update >/dev/null
helm repo update descheduler >/dev/null

helm upgrade --install descheduler descheduler/descheduler \
  --namespace kube-system \
  --version 0.36.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
