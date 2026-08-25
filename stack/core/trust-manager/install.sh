#!/usr/bin/env bash
# trust-manager — distributes CA bundles into namespaces via the Bundle CR.
# Pairs with cert-manager. Useful for internal CAs in dev (self-signed Issuers).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add jetstack https://charts.jetstack.io --force-update >/dev/null
helm repo update jetstack >/dev/null

kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install trust-manager jetstack/trust-manager \
  --namespace cert-manager \
  --version v0.24.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m \
  --hide-notes
