#!/usr/bin/env bash
# ingress-nginx — cluster ingress at http://localhost / https://localhost via control-plane hostPort mapping.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx --force-update >/dev/null
helm repo update ingress-nginx >/dev/null

kubectl create namespace ingress-nginx --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --version 4.15.1 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 10m
