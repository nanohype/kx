#!/usr/bin/env bash
# Gateway API CRDs (experimental channel).
# Installed out-of-band so Cilium's gatewayAPI feature can register its controller against them
# and so any workload that references HTTPRoute / Gateway / GatewayClass applies cleanly.
# Upstream: https://github.com/kubernetes-sigs/gateway-api
set -euo pipefail

GATEWAY_API_VERSION="v1.5.1"

# Experimental channel — superset of standard. Required by Cilium's Gateway API controller,
# which references TLSRoute v1alpha2 (still alpha → ships only in the experimental channel).
#
# Gateway API ships a ValidatingAdmissionPolicy `safe-upgrades.gateway.networking.k8s.io` that
# blocks switching an installed CRD from one channel to another. If standard-channel CRDs are
# already present, drop them (along with the policy) so the experimental install lands clean.
# Safe on first install (no-op deletes). On re-runs of experimental, this is a no-op too —
# the policy permits experimental→experimental updates.
if kubectl get crd gatewayclasses.gateway.networking.k8s.io \
   -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/channel}' 2>/dev/null \
   | grep -qx standard; then
  echo "==> existing gateway-api CRDs are standard-channel; removing before experimental install"
  kubectl delete validatingadmissionpolicybinding safe-upgrades.gateway.networking.k8s.io --ignore-not-found
  kubectl delete validatingadmissionpolicy        safe-upgrades.gateway.networking.k8s.io --ignore-not-found
  kubectl get crd -o name | grep '\.gateway\.networking\.k8s\.io$' | xargs -r kubectl delete
fi

# Server-side apply: HTTPRoute's experimental-channel OpenAPI schema exceeds the 256KB
# last-applied-configuration annotation limit of client-side apply.
# --request-timeout because this is a network fetch on the core path: every
# `task up` runs it, and an unreachable release host would otherwise hang the
# install with no output rather than failing with one.
kubectl apply --server-side --force-conflicts --request-timeout=120s \
  -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/experimental-install.yaml"
