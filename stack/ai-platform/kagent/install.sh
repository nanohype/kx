#!/usr/bin/env bash
# kagent — Kubernetes-native agent runtime.
# Production: eks-agent-platform's AgentFleet / ModelGateway CRs are issued against this.
# Local: same chart, sub-charts (tools, kmcp) disabled by default — opt in per-project.
# UI access: task port-forward:kagent-ui
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl create namespace kagent --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent \
  --namespace kagent \
  --version 0.9.4 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
