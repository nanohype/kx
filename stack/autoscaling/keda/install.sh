#!/usr/bin/env bash
# keda — event-driven autoscaling
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
helm repo update kedacore >/dev/null

kubectl --request-timeout=60s create namespace keda --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --version 2.21.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
