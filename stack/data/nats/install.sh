#!/usr/bin/env bash
# nats — message broker. Single-node, no JetStream by default; flip on per-project.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add nats https://nats-io.github.io/k8s/helm/charts/ >/dev/null 2>&1 || true
helm repo update nats >/dev/null

kubectl create namespace nats --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install nats nats/nats \
  --namespace nats \
  --version 2.14.4 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
