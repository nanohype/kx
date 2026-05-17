#!/usr/bin/env bash
# minio — S3-compatible object storage. Used as the velero backend; also available to projects.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add minio https://charts.min.io/ >/dev/null 2>&1 || true
helm repo update minio >/dev/null

kubectl create namespace minio --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install minio minio/minio \
  --namespace minio \
  --version 5.4.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
