#!/usr/bin/env bash
# cilium — CNI, kube-proxy replacement, hubble observability.
# Must be installed first or nodes stay NotReady (kind default CNI is disabled in cluster/kind-config.yaml).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm repo update cilium >/dev/null

helm upgrade --install cilium cilium/cilium \
  --namespace kube-system \
  --version 1.19.6 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
