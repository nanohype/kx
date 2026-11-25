#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update argo >/dev/null

kubectl --request-timeout=60s create namespace argo --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install argo-events argo/argo-events \
  --namespace argo \
  --version 2.4.28 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
