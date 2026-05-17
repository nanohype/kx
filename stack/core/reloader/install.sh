#!/usr/bin/env bash
# reloader — pod restart on ConfigMap/Secret change. Cheap, convenient.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add stakater https://stakater.github.io/stakater-charts >/dev/null 2>&1 || true
helm repo update stakater >/dev/null

helm upgrade --install reloader stakater/reloader \
  --namespace kube-system \
  --version 2.2.11 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
