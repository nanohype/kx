#!/usr/bin/env bash
# grafana-operator — manage Grafana dashboards/datasources/folders via CRDs
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

helm repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null
helm repo update grafana >/dev/null

kubectl --request-timeout=60s create namespace monitoring --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

deadline 900 helm upgrade --install grafana-operator grafana/grafana-operator \
  --namespace monitoring \
  --version 5.26.0 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
