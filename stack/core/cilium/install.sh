#!/usr/bin/env bash
# cilium — CNI, kube-proxy replacement, hubble observability, and the cluster's
# Ingress and Gateway API controller.
# Must be installed first or nodes stay NotReady (kind default CNI is disabled in cluster/kind-config.yaml).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add cilium https://helm.cilium.io --force-update >/dev/null
helm repo update cilium >/dev/null

# ── Retire ingress-nginx ───────────────────────────────────────────────────
#
# Cilium serves Ingress here. A cluster brought up while ingress-nginx did still
# runs that release: its DaemonSet holds the control-plane node's host ports,
# and its admission webhook validates every Ingress the cluster admits. It is
# removed by the addon that took its role, before that addon installs.
#
# Only a NotFound answer counts as absent. helm exits non-zero for an
# unreachable cluster as well, and reading that as "not installed" would install
# Cilium's controller beside a release that is still serving.
retired_nginx=false
if nginx_status="$(deadline 60 helm status ingress-nginx --namespace ingress-nginx 2>&1)"; then
  echo "Retiring the ingress-nginx release that Cilium's Ingress controller replaces ..."
  helm uninstall ingress-nginx --namespace ingress-nginx --wait --timeout 5m
  retired_nginx=true
elif ! printf '%s' "${nginx_status}" | grep -q 'release: not found'; then
  echo "ERROR: could not determine whether ingress-nginx is installed — helm said:" >&2
  printf '%s\n' "${nginx_status}" | sed 's/^/  /' >&2
  exit 1
fi
kubectl --request-timeout=60s delete namespace ingress-nginx --ignore-not-found --timeout=5m

deadline 900 helm upgrade --install cilium cilium/cilium \
  --namespace kube-system \
  --version 1.19.6 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m

if [ "${retired_nginx}" = true ]; then
  echo
  echo "NOTE: this cluster served localhost:80 and :443 through ingress-nginx, by port"
  echo "      mappings kind fixed when it created the cluster. Cilium's Ingress is reached"
  echo "      through the mappings cluster/kind-config.yaml declares now, which only a new"
  echo "      cluster has. Run: task reset"
fi
