#!/usr/bin/env bash
# reloader — pod restart on ConfigMap/Secret change. Cheap, convenient.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add stakater https://stakater.github.io/stakater-charts --force-update >/dev/null
helm repo update stakater >/dev/null

deadline 900 helm upgrade --install reloader stakater/reloader \
  --namespace kube-system \
  --version 2.2.20 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
