#!/usr/bin/env bash
# kube-prometheus-stack — prometheus + grafana + alertmanager + node-exporter + kube-state-metrics
# Stands in for Amazon Managed Prometheus and Amazon Managed Grafana, which
# eks-gitops reaches as external services. kube-state-metrics comes bundled
# here, where eks-gitops pins it as a chart of its own.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo update prometheus-community >/dev/null

kubectl --request-timeout=60s create namespace monitoring --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --version 93.1.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
