#!/usr/bin/env bash
# argo-cd — installed but idle. No App-of-Apps applied by kx.
# UI: http://localhost:30080 once the core stack is up (NodePort, published on loopback)
# Password: task argocd:initial-password
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update argo >/dev/null

kubectl --request-timeout=60s create namespace argocd --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --version 10.20.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
