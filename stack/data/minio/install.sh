#!/usr/bin/env bash
# minio — S3-compatible object storage. Used as the velero backend; also available to projects.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add minio https://charts.min.io/ --force-update >/dev/null
helm repo update minio >/dev/null

kubectl --request-timeout=60s create namespace minio --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

helm upgrade --install minio minio/minio \
  --namespace minio \
  --version 5.4.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
