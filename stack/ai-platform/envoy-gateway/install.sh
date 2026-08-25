#!/usr/bin/env bash
# envoy-gateway — the Gateway API control plane the model gateway runs on.
# Same chart and version as the eks-gitops catalog.
#
# Installed after the AI-layer CRDs and before envoy-ai-gateway: the AI layer
# registers a webhook against this control plane and reads its GatewayClass.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm upgrade --install envoy-gateway oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --version 1.8.3 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
