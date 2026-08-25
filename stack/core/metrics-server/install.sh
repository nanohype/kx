#!/usr/bin/env bash
# metrics-server — feeds `kubectl top` and HPA. Required for autoscaling slice.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update >/dev/null
helm repo update metrics-server >/dev/null

helm upgrade --install metrics-server metrics-server/metrics-server \
  --namespace kube-system \
  --version 3.13.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
