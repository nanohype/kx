#!/usr/bin/env bash
# falco — runtime security monitoring via eBPF
# NOTE: may not load kernel module on all Docker hosts; modern-bpf works on most modern Linux kernels.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add falcosecurity https://falcosecurity.github.io/charts >/dev/null 2>&1 || true
helm repo update falcosecurity >/dev/null

kubectl create namespace falco --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install falco falcosecurity/falco \
  --namespace falco \
  --version 8.0.5 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
