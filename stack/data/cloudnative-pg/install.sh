#!/usr/bin/env bash
# cloudnative-pg — postgres operator. Per-project Postgres clusters via the Cluster CR.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
helm repo update cnpg >/dev/null

kubectl create namespace cnpg-system --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install cloudnative-pg cnpg/cloudnative-pg \
  --namespace cnpg-system \
  --version 0.28.2 \
  --values "${SCRIPT_DIR}/values.yaml" \
  --wait
