#!/usr/bin/env bash
# envoy-gateway — the Gateway API control plane the model gateway runs on.
# Same chart and version as the eks-gitops catalog.
#
# Installed after the AI-layer CRDs and before envoy-ai-gateway: the AI layer
# registers a webhook against this control plane and reads its GatewayClass.
#
# --server-side=false keeps this install off every Gateway API CRD core's pin
# defines. The chart's crds subchart bundles a copy of them beside Envoy
# Gateway's own CRDs, and stack/core/gateway-api-crds owns them at its own
# pinned release. Helm installs a chart's crds/ one file at a time, by create,
# and skips a file whose create answers "already exists". Under server-side
# apply that create is an apply, which never answers "already exists": the
# bundled copy is applied over core's, every field where the two releases
# differ is a conflict with core's field manager, and the install stops at the
# first CRD. Client-side, the bundled Gateway API file is skipped, and Envoy
# Gateway's own CRDs, which nothing else installs, are created. Helm creates
# every definition in a file before deciding to skip it, so a kind the bundle
# carries and core's pin lacks is still created, at the bundle's release.
#
# Helm applies crds/ on first install only, so that is where the flag matters.
# On upgrade it selects the apply method for the chart's templates alone, and
# both methods apply them: `helm upgrade` otherwise reuses the previous
# release's method, so a release first installed server-side moves to
# client-side on its next upgrade.
#
# The values are eks-gitops's, `crds` included. Turning the subchart off drops
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
