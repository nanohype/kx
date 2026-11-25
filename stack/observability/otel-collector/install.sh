#!/usr/bin/env bash
# opentelemetry-collector — the OTLP gateway every tenant chart writes to.
#
# Installs after prometheus/loki/tempo because all three are its export targets:
# the collector starts, fails to reach a backend that is not up yet, and retries,
# but there is no reason to make it.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts --force-update >/dev/null
helm repo update open-telemetry >/dev/null

kubectl --request-timeout=60s create namespace monitoring --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install otel-collector open-telemetry/opentelemetry-collector \
  --namespace monitoring \
  --version 0.178.3 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
