#!/usr/bin/env bash
# external-secrets — same chart eks-gitops uses; locally SecretStores use
# the `kubernetes` or `fake` provider rather than the AWS Secrets Manager provider.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add external-secrets https://charts.external-secrets.io --force-update >/dev/null
helm repo update external-secrets >/dev/null

kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --version 2.15.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
