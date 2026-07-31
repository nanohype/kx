#!/usr/bin/env bash
# envoy-ai-gateway-crds — the AIGatewayRoute / AIServiceBackend /
# BackendSecurityPolicy definitions, installed ahead of the controllers that
# reconcile them. Same chart and version as the eks-gitops catalog.
#
# CRDs are their own release because the AI layer's chart does not ship them:
# installing the controller first leaves it watching kinds the API server has
# never heard of, which reports healthy and reconciles nothing.
set -euo pipefail

helm upgrade --install envoy-ai-gateway-crds oci://docker.io/envoyproxy/ai-gateway-crds-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --version 1.0.0 \
  --wait
