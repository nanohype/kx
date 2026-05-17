#!/usr/bin/env bash
# agentgateway — model/tool gateway. Same chart as eks-agent-platform production.
# Locally: ClusterIP, single replica. Route CRs (agentgateway.dev/v1alpha1) define routing.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl create namespace agentgateway --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install agentgateway oci://ghcr.io/agentgateway/agentgateway/charts/agentgateway \
  --namespace agentgateway \
  --version 1.0.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
