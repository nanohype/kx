#!/usr/bin/env bash
# envoy-ai-gateway — the AI-aware layer over envoy-gateway.
# Same chart and version as the eks-gitops catalog.
#
# Last of the three: it registers a webhook against the envoy-gateway control
# plane and reads its GatewayClass, and its CRDs are installed separately by
# the crds slice.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

deadline 900 helm upgrade --install envoy-ai-gateway oci://docker.io/envoyproxy/ai-gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --version 1.2.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m

# The GatewayClass the operator's Gateways name. Neither chart ships one, so
# without this every ModelGateway reconciles into a Gateway no controller
# claims. See gatewayclass.yaml.
kubectl --request-timeout=60s apply -f "${SCRIPT_DIR}/gatewayclass.yaml"
