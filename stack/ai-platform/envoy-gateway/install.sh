#!/usr/bin/env bash
# envoy-gateway — the Gateway API control plane the model gateway runs on.
# Same chart and version as the eks-gitops catalog.
#
# Installed after both CRD addons and before envoy-ai-gateway: the AI layer
# registers a webhook against this control plane and reads its GatewayClass.
#
# No CRDs from this chart: values.yaml sets crds.enabled: false, as the catalog
# does. Its crds subchart would install Envoy Gateway's own CRDs together with a
# bundled Gateway API release and that release's safe-upgrades policy, and
# stack/core/gateway-api-crds owns those at its own pinned release and channel.
# Envoy Gateway's CRDs come from envoy-gateway-crds, which the slice installs
# first. With the subchart off, `helm template --include-crds` renders no CRD and
# no admission policy from this chart.
#
# No --server-side flag, so helm's default apply method, like every other addon
# here. Forcing client-side apply matters only for a chart's crds/ directory,
# which helm installs by create on first install and skips where a definition
# already exists; under server-side apply that create becomes an apply over
# whatever is there. With the subchart off there is no crds/ directory to
# install, and none of the chart's templates is an object another installer
# owns. A release first installed client-side keeps that method on upgrade,
# because helm reuses the previous release's method when none is given.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm upgrade --install envoy-gateway oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --version 1.10.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
