#!/usr/bin/env bash
# kyverno — admission policy engine
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add kyverno https://kyverno.github.io/kyverno/ --force-update >/dev/null
helm repo update kyverno >/dev/null

kubectl --request-timeout=60s create namespace kyverno --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install kyverno kyverno/kyverno \
  --namespace kyverno \
  --version 3.10.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
