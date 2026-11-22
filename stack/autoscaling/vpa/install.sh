#!/usr/bin/env bash
# vpa — Vertical Pod Autoscaler (recommender-only by default; flip updater on per-project)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add fairwinds-stable https://charts.fairwinds.com/stable --force-update >/dev/null
helm repo update fairwinds-stable >/dev/null

kubectl --request-timeout=60s create namespace vpa --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

helm upgrade --install vpa fairwinds-stable/vpa \
  --namespace vpa \
  --version 5.2.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
