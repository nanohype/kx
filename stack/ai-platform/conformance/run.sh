#!/usr/bin/env bash
# Send real traffic through a Platform's model gateway and assert the route
# serves the contract it publishes.
#
# Every other check in this workspace reads a resource. `task status` reads
# phases, `credentials:verify` reads a pod spec, the render gate reads
# manifests. All of them pass on a gateway that answers nothing — the resource
# graph is identical whether the data plane serves or refuses. This is the one
# that finds out.
#
# The probes live in eks-agent-platform (packages/gateway-conformance) because
# the contract is that repo's: the operator publishes status.routes[].api and
# a base URL, and the assertion belongs beside the code that can break it. kx
# supplies what that repo's CI cannot — a live cluster and a gateway holding
# credentials. Same seam the operator install uses, so override the checkout
# with KX_EKS_AGENT_PLATFORM_DIR if your repos are not side by side.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PLATFORM="${PLATFORM:?set PLATFORM to the Platform whose gateway to probe}"
ROUTE="${ROUTE:-primary}"
NAMESPACE="${NAMESPACE:-eks-agent-platform}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
REPO="${KX_EKS_AGENT_PLATFORM_DIR:-${SCRIPT_DIR}/../../../../eks-agent-platform}"

if [ ! -d "${REPO}/packages/gateway-conformance" ]; then
  echo "eks-agent-platform not found at ${REPO}" >&2
  echo "set KX_EKS_AGENT_PLATFORM_DIR to your eks-agent-platform checkout" >&2
  exit 1
fi

# The ModelGateway CR names the Service the probes reach. The published routes
# are printed for the operator, not parsed: the suite is told which route to
# exercise through GATEWAY_ROUTE, and what it asserts about the wire format is
# that repository's contract to state. Printing status.routes beside the probe
# result is what makes a wrong ROUTE legible here rather than as an assertion
# failure three layers down.
# Every match, not the first. Taking one of several and saying nothing about
# the rest reports a pass for gateways this run never sent a byte through.
read -r -a GATEWAYS <<<"$(kubectl get modelgateway -n "${NAMESPACE}" \
  -o jsonpath="{.items[?(@.spec.platformRef.name=='${PLATFORM}')].metadata.name}")"
if [ "${#GATEWAYS[@]}" -eq 0 ]; then
  echo "no ModelGateway in ${NAMESPACE} references Platform '${PLATFORM}'" >&2
  exit 1
fi
if [ "${#GATEWAYS[@]}" -gt 1 ]; then
  echo "Platform '${PLATFORM}' has ${#GATEWAYS[@]} ModelGateways: ${GATEWAYS[*]}" >&2
  echo "Probe one at a time — set NAMESPACE and re-run per gateway." >&2
  exit 1
fi
GATEWAY="${GATEWAYS[0]}"

PHASE="$(kubectl get modelgateway "${GATEWAY}" -n "${NAMESPACE}" -o jsonpath='{.status.phase}')"
echo "ModelGateway ${GATEWAY} (Platform ${PLATFORM}) reports phase=${PHASE:-<none>}"
echo "Routes it publishes:"
ROUTES="$(kubectl get modelgateway "${GATEWAY}" -n "${NAMESPACE}" \
  -o jsonpath='{.status.routes}')"
if [ -z "${ROUTES}" ]; then
  echo "  (none — the operator has not published a route for this gateway)" >&2
else
  echo "  ${ROUTES}"
fi
echo

# The gateway's Service is in the tenant namespace — envoy-gateway runs in
# GatewayNamespace mode, so there is no shared address. Port-forward it and
# point the probes at localhost; the probes append the /anthropic prefix
# themselves, the same way a tenant app does.
SVC_NS="tenants-${PLATFORM}"
echo "Forwarding ${SVC_NS}/svc/${GATEWAY} to localhost:${LOCAL_PORT} ..."

# Kept, not discarded. A forward that fails to bind — the port is taken, the
# Service is not there yet — is the most likely reason this script fails, and
# discarding both streams left the reader with a conformance error from the
# suite instead of the one sentence that explains it.
PF_LOG="$(mktemp)"
trap 'rm -f "${PF_LOG}"' EXIT
kubectl port-forward -n "${SVC_NS}" "svc/${GATEWAY}" "${LOCAL_PORT}:8080" >"${PF_LOG}" 2>&1 &
PF_PID=$!
trap 'kill "${PF_PID}" 2>/dev/null || true; rm -f "${PF_LOG}"' EXIT

ready=0
for _ in $(seq 1 30); do
  # The forwarder dying is terminal — retrying for 30s against a dead child
  # only delays the same failure and buries its reason.
  if ! kill -0 "${PF_PID}" 2>/dev/null; then
    echo "port-forward exited before the gateway answered:" >&2
    sed 's/^/      /' "${PF_LOG}" >&2
    exit 1
  fi
  if curl -s -o /dev/null -m 2 "http://localhost:${LOCAL_PORT}/anthropic/v1/messages"; then
    ready=1
    break
  fi
  sleep 1
done

# Falling out of the loop used to run the suite anyway. A probe that cannot
# reach its target has not passed, and saying so here names the gateway rather
# than leaving the suite to report a connection error against a bare port.
if [ "${ready}" -ne 1 ]; then
  echo "gateway ${SVC_NS}/${GATEWAY} did not answer on localhost:${LOCAL_PORT} within 30s." >&2
  echo "port-forward said:" >&2
  sed 's/^/      /' "${PF_LOG}" >&2
  exit 1
fi

cd "${REPO}"
GATEWAY_ENDPOINT="http://localhost:${LOCAL_PORT}" GATEWAY_ROUTE="${ROUTE}" \
  pnpm --filter @eks-agent/gateway-conformance --silent conformance
