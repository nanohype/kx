#!/usr/bin/env bash
# falco — runtime security monitoring via eBPF
# The modern_ebpf driver needs a kernel with CO-RE BPF support — the Docker Desktop and OrbStack VMs provide one.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add falcosecurity https://falcosecurity.github.io/charts >/dev/null 2>&1 || true
helm repo update falcosecurity >/dev/null

kubectl create namespace falco --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install falco falcosecurity/falco \
  --namespace falco \
  --version 9.1.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
