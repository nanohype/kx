#!/usr/bin/env bash
# envoy-gateway-crds teardown. install.sh applies these CRDs without a helm
# release, so there is no `helm uninstall` for them; this deletes them instead.
#
# Run after envoy-gateway is uninstalled, never before: the controller watches
# these kinds, and deleting a CRD deletes every resource of that kind with it,
# so the definitions have to outlive the thing reconciling them. The slice's
# disable orders it that way, but its `helm uninstall ... || true` would carry
# on past a failed uninstall, so the order is checked here rather than trusted.
#
# Selected by group rather than by re-rendering the chart, so teardown needs no
# registry. Nothing else in kx installs gateway.envoyproxy.io; the AI layer's
# kinds are aigateway.envoyproxy.io, which the anchored pattern does not match.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

NS=envoy-gateway-system

# The controller is gone before anything is deleted. Each read is assigned
# before it is tested: a substitution inside `[[ -n ... ]]` that fails is
# ignored by set -e and reads as empty, which here would mean "already gone"
# for a cluster that could not answer. Assigned, a failed read stops the script.
release="$(helm list -n "${NS}" -q --filter '^envoy-gateway$')"
if [[ -n "${release}" ]]; then
  echo "envoy-gateway is still installed — uninstall it first; its CRDs outlive it." >&2
  exit 1
fi
# helm uninstall returns before the Deployment it deleted is gone, so a
# controller that is on its way out is waited for rather than refused.
deploy="$(kubectl get deploy envoy-gateway -n "${NS}" --ignore-not-found \
  -o name --request-timeout=60s)"
if [[ -n "${deploy}" ]]; then
  if ! kubectl wait --for=delete "${deploy}" -n "${NS}" --timeout=120s; then
    echo "the envoy-gateway Deployment is still running — not deleting the CRDs it watches." >&2
    exit 1
  fi
fi

# Listed first and checked, so a failed listing stops here instead of reading
# as "nothing to delete". grep exits 1 when nothing matches, which on a slice
# already torn down is the answer; anything higher means grep itself failed.
crds="$(kubectl get crd -o name --request-timeout=60s)"
rc=0
owned="$(grep '\.gateway\.envoyproxy\.io$' <<<"${crds}")" || rc=$?
if [[ "${rc}" -gt 1 ]]; then
  echo "could not read the CRD list for gateway.envoyproxy.io — grep exited ${rc}" >&2
  exit "${rc}"
fi
if [[ -n "${owned}" ]]; then
  xargs kubectl delete --ignore-not-found --timeout=120s <<<"${owned}"
fi
