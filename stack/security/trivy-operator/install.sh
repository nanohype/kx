#!/usr/bin/env bash
# trivy-operator — workload vulnerability + misconfig + secret scanning
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add aqua https://aquasecurity.github.io/helm-charts/ >/dev/null 2>&1 || true
helm repo update aqua >/dev/null

kubectl create namespace trivy-system --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install trivy-operator aqua/trivy-operator \
  --namespace trivy-system \
  --version 0.35.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
