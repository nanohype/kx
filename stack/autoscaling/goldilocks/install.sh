#!/usr/bin/env bash
# goldilocks — VPA-recommendation dashboard
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add fairwinds-stable https://charts.fairwinds.com/stable --force-update >/dev/null
helm repo update fairwinds-stable >/dev/null

kubectl --request-timeout=60s create namespace vpa --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install goldilocks fairwinds-stable/goldilocks \
  --namespace vpa \
  --version 11.2.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
