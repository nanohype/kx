#!/usr/bin/env bash
# falco — runtime security monitoring via eBPF
# The modern_ebpf driver needs a kernel with CO-RE BPF support — the Docker Desktop and OrbStack VMs provide one.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add falcosecurity https://falcosecurity.github.io/charts --force-update >/dev/null
helm repo update falcosecurity >/dev/null

kubectl --request-timeout=60s create namespace falco --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

helm upgrade --install falco falcosecurity/falco \
  --namespace falco \
  --version 9.2.2 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
