#!/usr/bin/env bash
# cert-manager — TLS certificate issuance. Required by anything using cert-manager.io Certificate CRDs.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add jetstack https://charts.jetstack.io --force-update >/dev/null
helm repo update jetstack >/dev/null

kubectl --request-timeout=60s create namespace cert-manager --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --version v1.21.3 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
