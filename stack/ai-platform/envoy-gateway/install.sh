#!/usr/bin/env bash
# envoy-gateway — the Gateway API control plane the model gateway runs on.
# Same chart and version as the eks-gitops catalog.
#
# Installed after the AI-layer CRDs and before envoy-ai-gateway: the AI layer
# registers a webhook against this control plane and reads its GatewayClass.
#
# --server-side=false keeps this install off the Gateway API CRDs. The chart's
# crds subchart bundles a copy of them beside Envoy Gateway's own CRDs, and
# stack/core/gateway-api-crds already owns them at its own pinned release.
# Helm installs a chart's crds/ by create and skips a definition that already
# exists. Under server-side apply that create is an apply, which never answers
# "already exists": the bundled copy is applied over core's, every field where
# the two releases differ is a conflict with core's field manager, and the
# install stops at the first CRD. Client-side, the bundled Gateway API copy is
# skipped and Envoy Gateway's own CRDs, which nothing else installs, are
# created. Helm applies crds/ on first install only, so the flag matters there;
# an upgrade keeps it so a release never alternates between apply methods.
#
# The values stay eks-gitops's, `crds` included. Turning the subchart off drops
# Envoy Gateway's CRDs with it, and their standalone chart, gateway-crds-helm,
# renders them as templates — a release too large for the Secret Helm stores it
# in, which the API server caps at 1 MiB.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm upgrade --install envoy-gateway oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --version 1.10.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --server-side=false \
  --wait --timeout 10m
