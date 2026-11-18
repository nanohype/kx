#!/usr/bin/env bash
# envoy-gateway-crds — Envoy Gateway's own CRDs (gateway.envoyproxy.io),
# installed ahead of the controller that reconciles them. Same chart as the
# eks-gitops catalog, at the same version as envoy-gateway's gateway-helm: the
# controller and its CRDs are one generation, so the two pins move together.
#
# gateway-helm can install these itself, but only bundled with a Gateway API
# release and that release's safe-upgrades policy, and
# stack/core/gateway-api-crds already owns both. So envoy-gateway runs with
# that bundle off and this addon installs only the Envoy Gateway half (see
# values.yaml).
#
# Rendered and applied rather than installed as a helm release, the way
# upstream's own install instructions for this chart do it. Helm stores a
# release in a Secret, which the API server caps at 1 MiB, and a release holds
# the whole chart: this one carries both Gateway API channels as templates
# beside Envoy Gateway's CRDs, whatever the values turn on, and the stored
# release does not fit. So there is no `helm uninstall` for it either; the
# slice's disable runs uninstall.sh.
#
# Server-side apply because the envoyproxies schema alone is far past the
# 256 KiB last-applied-configuration annotation client-side apply writes. The
# field manager names this addon, so
# `kubectl get crd -o yaml --show-managed-fields` shows who owns them.
#
# --force-conflicts because this addon is the only thing in kx that installs
# this group, and a cluster that ran gateway-helm with its bundle on still has
# these CRDs under helm's manager as well. At the same version the two managers
# share every field, since a conflict arises only where values differ;
# --force-conflicts is what moves those fields to this manager at the next
# version bump, instead of stopping on the first field that changed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Rendered to a file before anything reaches the API server, so a render that
# fails partway applies nothing rather than whatever it had written.
MANIFEST="$(mktemp)"
trap 'rm -f "${MANIFEST}"' EXIT

helm template envoy-gateway-crds oci://docker.io/envoyproxy/gateway-crds-helm \
  --version 1.10.0 \
  --values "${SCRIPT_DIR}/values.yaml" >"${MANIFEST}"

kubectl apply --server-side --force-conflicts --field-manager envoy-gateway-crds \
  --request-timeout=120s -f "${MANIFEST}"

# Established before the controller installs. Without the wait, envoy-gateway's
# install can start its controller while a kind it watches is still being
# registered, and the ordering this slice relies on becomes a race.
kubectl wait --for condition=Established --timeout=120s -f "${MANIFEST}"
