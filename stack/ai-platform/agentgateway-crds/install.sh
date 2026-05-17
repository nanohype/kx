#!/usr/bin/env bash
# agentgateway CRDs — installed out-of-band before the agentgateway chart so the controller
# can find its watched resources (AgentgatewayPolicy, AgentgatewayBackend) on startup.
# Without these CRDs the controller hangs at "waiting for sync" and fails its startup probe.
set -euo pipefail

kubectl create namespace agentgateway --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install agentgateway-crds oci://ghcr.io/agentgateway/agentgateway/charts/agentgateway-crds \
  --namespace agentgateway \
  --version 1.0.1 \
  --wait
