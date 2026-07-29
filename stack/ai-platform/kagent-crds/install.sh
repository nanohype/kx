#!/usr/bin/env bash
# kagent CRDs — installed out-of-band per kagent chart guidance.
# Must precede the kagent chart so the controller can find its CRs at startup.
set -euo pipefail

kubectl create namespace kagent --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kagent-crds oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds \
  --namespace kagent \
  --version 0.9.11 \
  --wait
