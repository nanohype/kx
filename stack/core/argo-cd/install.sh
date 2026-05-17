#!/usr/bin/env bash
# argo-cd — installed but idle. No App-of-Apps applied by kx.
# UI: task port-forward:argocd  (https://localhost:30080)
# Password: task argocd:initial-password
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --version 9.5.14 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
