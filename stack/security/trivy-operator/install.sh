#!/usr/bin/env bash
# trivy-operator — workload vulnerability + misconfig + secret scanning
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add aqua https://aquasecurity.github.io/helm-charts/ --force-update >/dev/null
helm repo update aqua >/dev/null

kubectl --request-timeout=60s create namespace trivy-system --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

helm upgrade --install trivy-operator aqua/trivy-operator \
  --namespace trivy-system \
  --version 0.38.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
