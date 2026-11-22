#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update argo >/dev/null

kubectl --request-timeout=60s create namespace argo --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

helm upgrade --install argo-rollouts argo/argo-rollouts \
  --namespace argo \
  --version 2.43.12 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
