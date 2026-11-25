#!/usr/bin/env bash
# descheduler — rebalance pods periodically (runs as Deployment, polls + evicts)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add descheduler https://kubernetes-sigs.github.io/descheduler/ --force-update >/dev/null
helm repo update descheduler >/dev/null

helm upgrade --install descheduler descheduler/descheduler \
  --namespace kube-system \
  --version 0.37.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
