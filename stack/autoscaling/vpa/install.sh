#!/usr/bin/env bash
# vpa — Vertical Pod Autoscaler (recommender-only by default; flip updater on per-project)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add fairwinds-stable https://charts.fairwinds.com/stable >/dev/null 2>&1 || true
helm repo update fairwinds-stable >/dev/null

kubectl create namespace vpa --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install vpa fairwinds-stable/vpa \
  --namespace vpa \
  --version 4.12.5 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
