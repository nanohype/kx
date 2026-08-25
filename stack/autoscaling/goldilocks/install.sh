#!/usr/bin/env bash
# goldilocks — VPA-recommendation dashboard
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add fairwinds-stable https://charts.fairwinds.com/stable --force-update >/dev/null
helm repo update fairwinds-stable >/dev/null

kubectl create namespace vpa --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install goldilocks fairwinds-stable/goldilocks \
  --namespace vpa \
  --version 10.5.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
