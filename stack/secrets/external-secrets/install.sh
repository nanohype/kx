#!/usr/bin/env bash
# external-secrets — same chart eks-gitops uses; locally SecretStores use
# the `kubernetes` or `fake` provider rather than the AWS Secrets Manager provider.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo update external-secrets >/dev/null

kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --version 2.8.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
