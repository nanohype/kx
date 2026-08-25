#!/usr/bin/env bash
# argo-cd — installed but idle. No App-of-Apps applied by kx.
# UI: http://localhost:30080 once the core stack is up (NodePort, published on loopback)
# Password: task argocd:initial-password
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
helm repo update argo >/dev/null

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install argocd argo/argo-cd \
  --namespace argocd \
  --version 10.3.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
