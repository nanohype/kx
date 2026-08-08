#!/usr/bin/env bash
# opentelemetry-collector — the OTLP gateway every tenant chart writes to.
#
# Installs after prometheus/loki/tempo because all three are its export targets:
# the collector starts, fails to reach a backend that is not up yet, and retries,
# but there is no reason to make it.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm repo update open-telemetry >/dev/null

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install otel-collector open-telemetry/opentelemetry-collector \
  --namespace monitoring \
  --version 0.169.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
