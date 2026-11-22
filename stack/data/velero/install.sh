#!/usr/bin/env bash
# velero — backup/restore. Same Velero as eks-gitops; backend swapped to in-cluster minio.
# Requires the minio install to have completed first (Taskfile orders this).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts --force-update >/dev/null
helm repo update vmware-tanzu >/dev/null

kubectl --request-timeout=60s create namespace velero --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

helm upgrade --install velero vmware-tanzu/velero \
  --namespace velero \
  --version 12.2.5 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
