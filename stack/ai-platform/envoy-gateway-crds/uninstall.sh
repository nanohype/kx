#!/usr/bin/env bash
# envoy-gateway-crds teardown — the counterpart of the `helm uninstall` every
# other addon here gets, since install.sh applies these CRDs without a release.
#
# Run after envoy-gateway is uninstalled, never before: the controller watches
# these kinds, and deleting a CRD deletes every resource of that kind with it,
# so the definitions have to outlive the thing reconciling them.
#
# Selected by group rather than by re-rendering the chart, so teardown needs no
# registry. Nothing else in kx installs gateway.envoyproxy.io; the AI layer's
# kinds are aigateway.envoyproxy.io, which the anchored pattern does not match.
set -euo pipefail

# Listed first and checked, so a failed listing stops here instead of reading
# as "nothing to delete", and grep's status read rather than taken as a
# condition: 1 is no match, which on a slice already torn down is the answer,
# and anything above it is grep failing to answer.
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
