#!/usr/bin/env bash
# Gateway API CRDs (experimental channel).
# Installed out-of-band so Cilium's gatewayAPI feature can register its controller against them
# and so any workload that references HTTPRoute / Gateway / GatewayClass applies cleanly.
# Upstream: https://github.com/kubernetes-sigs/gateway-api
set -euo pipefail

GATEWAY_API_VERSION="v1.5.1"
# The digest of the manifest that version publishes. `kubectl apply -f <url>`
# fetches and applies in one step, so without this the CRDs that define every
# route kind in the cluster are whatever the release host serves at the moment
# of the call — on the core path of every `task up`, before anything else is
# installed. Checked here rather than trusted, the same way ci.yml checks
# kubeconform before putting it on PATH.
GATEWAY_API_SHA256="64ec76609a6ac885e0405dea79ca509c229fa019d342f0857aa8b6bdc8b8ba92"

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

# Fetched to a file and verified before it reaches the API server, rather than
# applied from the URL. --max-time because this is a network fetch on the core
# path: every `task up` runs it, and an unreachable release host would otherwise
# hang the install with no output rather than failing with one.
MANIFEST="$(mktemp)"
trap 'rm -f "${MANIFEST}"' EXIT
curl -sSfL --max-time 120 --retry 3 --retry-connrefused --retry-all-errors \
  -o "${MANIFEST}" \
  "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/experimental-install.yaml"

# shasum on macOS, sha256sum on the runner. Whichever is present, an unverified
# apply is not a fallback: with neither tool the install stops, because applying
# a cluster's route definitions unchecked is the thing this guards against.
if command -v sha256sum >/dev/null 2>&1; then
  echo "${GATEWAY_API_SHA256}  ${MANIFEST}" | sha256sum -c - >/dev/null
elif command -v shasum >/dev/null 2>&1; then
  echo "${GATEWAY_API_SHA256}  ${MANIFEST}" | shasum -a 256 -c - >/dev/null
else
  echo "neither sha256sum nor shasum is on PATH; refusing to apply an unverified" >&2
  echo "gateway-api manifest — these CRDs define every route kind in the cluster." >&2
  exit 1
fi

# Server-side apply: HTTPRoute's experimental-channel OpenAPI schema exceeds the 256KB
# last-applied-configuration annotation limit of client-side apply.
kubectl apply --server-side --force-conflicts --request-timeout=120s -f "${MANIFEST}"
