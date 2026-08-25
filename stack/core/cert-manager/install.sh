#!/usr/bin/env bash
# cert-manager — TLS certificate issuance. Required by anything using cert-manager.io Certificate CRDs.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add jetstack https://charts.jetstack.io --force-update >/dev/null
helm repo update jetstack >/dev/null

kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --version v1.21.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
