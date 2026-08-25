#!/usr/bin/env bash
# prometheus-operator-crds — installs the ServiceMonitor / PodMonitor / PrometheusRule CRDs
# so deploys that reference them succeed even before the observability slice is enabled.
# No values; this chart is CRDs only.
set -euo pipefail

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo update prometheus-community >/dev/null

helm upgrade --install prometheus-operator-crds prometheus-community/prometheus-operator-crds \
  --namespace kube-system \
  --version 31.0.0 \
  --wait --timeout 10m
