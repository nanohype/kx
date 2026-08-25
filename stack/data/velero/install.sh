#!/usr/bin/env bash
# velero — backup/restore. Same Velero as eks-gitops; backend swapped to in-cluster minio.
# Requires the minio install to have completed first (Taskfile orders this).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update vmware-tanzu >/dev/null

kubectl create namespace velero --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install velero vmware-tanzu/velero \
  --namespace velero \
  --version 12.1.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
