#!/usr/bin/env bash
# eks-agent-platform operator — the control plane that reconciles
# Platform / AgentFleet / ModelGateway / Tenant CRs. Direct mirror of
# eks-agent-platform, installed after Envoy AI Gateway: a ModelGateway
# reconciles into a Gateway plus AIGatewayRoute / AIServiceBackend resources,
# so those kinds have to exist first. An AgentFleet needs nothing extra — each
# agent becomes a Deployment of the tenant's own image.
#
# Local: the operator image isn't published, so it's built from the sibling
# eks-agent-platform checkout and kind-loaded. --disable-aws skips the real
# IRSA/KMS reconcile (no AWS locally); the webhook cert comes from a
# self-signed ClusterIssuer via the cert-manager installed in kx core.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Sibling eks-agent-platform checkout. Resolves relative to kx's repo location
# (kx/stack/ai-platform/operator → ../../../../eks-agent-platform), mirroring
# the druid catalog reference. Override via env if your repos live elsewhere.
OPERATOR_REPO="${KX_EKS_AGENT_PLATFORM_DIR:-${SCRIPT_DIR}/../../../../eks-agent-platform}"
IMAGE="ghcr.io/nanohype/eks-agent-platform/operator:dev"
CLUSTER="${KX_CLUSTER_NAME:-kx}"

if [ ! -d "${OPERATOR_REPO}/charts/operator" ]; then
  echo "eks-agent-platform not found at ${OPERATOR_REPO}" >&2
  echo "set KX_EKS_AGENT_PLATFORM_DIR to your eks-agent-platform checkout" >&2
  exit 1
fi

# Build for the kind node arch (the host arch — kind nodes match the host) and
# load it into the cluster; the image is never pushed to a registry locally.
echo "Building operator image ${IMAGE} from ${OPERATOR_REPO}/operators ..."
docker build -t "${IMAGE}" --build-arg VERSION=dev "${OPERATOR_REPO}/operators"
kind load docker-image "${IMAGE}" --name "${CLUSTER}"

kubectl create namespace eks-agent-platform --dry-run=client -o yaml | kubectl apply -f -

# Helm installs a chart's crds/ on first install and never touches them again
# on upgrade. So a CRD added after this cluster's first install never arrives,
# and the operator watches a kind the API server has never heard of — which
# blocks that controller's cache sync forever. The controller never starts, its
# CRs keep reporting whatever status they last had, and nothing in the cluster
# says why. That is how a Platform sat Ready two generations stale with no
# NetworkPolicy in its namespace.
#
# Applying them before the upgrade is what makes `task stack:ai-platform:enable`
# idempotent across a chart that grows.
echo "Applying CRDs (helm does not upgrade them) ..."
kubectl apply -f "${OPERATOR_REPO}/charts/operator/crds/"

helm upgrade --install eks-agent-platform-operator "${OPERATOR_REPO}/charts/operator" \
  --namespace eks-agent-platform \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 180s
