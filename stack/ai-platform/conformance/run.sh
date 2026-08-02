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

# The ModelGateway CR names the Service, and the operator publishes the
# in-cluster endpoint on status. Read the route contract off the cluster rather
# than assuming it — status.routes[] carries the resolved wire format and base
# URL per route, which is precisely what the probes are here to check.
GATEWAY="$(kubectl get modelgateway -n "${NAMESPACE}" \
  -o jsonpath="{.items[?(@.spec.platformRef.name=='${PLATFORM}')].metadata.name}" | awk '{print $1}')"
if [ -z "${GATEWAY}" ]; then
  echo "no ModelGateway in ${NAMESPACE} references Platform '${PLATFORM}'" >&2
  exit 1
fi

PHASE="$(kubectl get modelgateway "${GATEWAY}" -n "${NAMESPACE}" -o jsonpath='{.status.phase}')"
echo "ModelGateway ${GATEWAY} (Platform ${PLATFORM}) reports phase=${PHASE:-<none>}"
echo "Routes it publishes:"
kubectl get modelgateway "${GATEWAY}" -n "${NAMESPACE}" -o jsonpath='{.status.routes}' || true
echo

# The gateway's Service is in the tenant namespace — envoy-gateway runs in
# GatewayNamespace mode, so there is no shared address. Port-forward it and
# point the probes at localhost; the probes append the /anthropic prefix
# themselves, the same way a tenant app does.
SVC_NS="tenants-${PLATFORM}"
echo "Forwarding ${SVC_NS}/svc/${GATEWAY} to localhost:${LOCAL_PORT} ..."
kubectl port-forward -n "${SVC_NS}" "svc/${GATEWAY}" "${LOCAL_PORT}:8080" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "${PF_PID}" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  curl -s -o /dev/null -m 2 "http://localhost:${LOCAL_PORT}/anthropic/v1/messages" && break
  sleep 1
done

cd "${REPO}"
GATEWAY_ENDPOINT="http://localhost:${LOCAL_PORT}" GATEWAY_ROUTE="${ROUTE}" \
  pnpm --filter @eks-agent/gateway-conformance --silent conformance
