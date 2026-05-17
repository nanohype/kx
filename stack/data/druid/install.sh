#!/usr/bin/env bash
# Apache Druid for kx — uses your eks-gitops/catalog/druid chart unmodified, with a local
# filter (post-renderer.sh) that strips EKS-only resources before kubectl apply.
#
# Why kubectl apply instead of `helm upgrade --install`:
#   Helm v4 dropped the arbitrary-executable --post-renderer flag in favor of a plugin model.
#   Rather than ship a helm plugin, we render → filter → apply. We lose `helm history` for druid,
#   but disable is just `kubectl delete ns druid` (which sweeps CNPG cluster + all resources).
#
# Heaviest single addition to kx. Allow 5–10 minutes on first install.
# Resident footprint after Ready: ~4.5GB.
#
# Prereqs (verified at start):
#   - core (cert-manager, prometheus-operator-crds)
#   - data slice enabled (CNPG operator + MinIO)
#
# Bring up:  task stack:data:druid:enable
# Tear down: task stack:data:druid:disable
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Path to the eks-gitops Druid chart. Resolves relative to kx's repo location
# (kx/stack/data/druid → ../../../../eks-gitops). Override via env if your repos
# don't sit side-by-side under a common parent.
CHART_DIR="${KX_DRUID_CHART_DIR:-${SCRIPT_DIR}/../../../../eks-gitops/catalog/druid/chart}"
CHART_BASE_VALUES="${KX_DRUID_CHART_VALUES:-${SCRIPT_DIR}/../../../../eks-gitops/catalog/druid/values.yaml}"

# ---- prereqs ----
[ -d "${CHART_DIR}" ] \
  || { echo "ERROR: chart not found at ${CHART_DIR}"; exit 1; }
kubectl get crd clusters.postgresql.cnpg.io >/dev/null 2>&1 \
  || { echo "ERROR: CNPG not installed. Run: task stack:data:enable"; exit 1; }
kubectl get ns minio >/dev/null 2>&1 \
  || { echo "ERROR: MinIO not installed. Run: task stack:data:enable"; exit 1; }
kubectl get crd certificates.cert-manager.io >/dev/null 2>&1 \
  || { echo "ERROR: cert-manager not installed (it's in core; run: task up)"; exit 1; }
python3 -c "import yaml" 2>/dev/null \
  || { echo "ERROR: post-renderer needs python3 + PyYAML. Run: pip3 install pyyaml"; exit 1; }

kubectl create namespace druid --dry-run=client -o yaml | kubectl apply -f -

# ---- 1. Postgres for metadata (CNPG) ----
echo "==> creating Postgres metadata cluster (CNPG)"
kubectl apply -f "${SCRIPT_DIR}/pre-install/postgres.yaml"
echo "==> waiting for Postgres to be Ready (up to 5min)..."
kubectl -n druid wait cluster/druid-metadata --for=condition=Ready --timeout=300s

# ---- 2. Translate CNPG-generated app credentials into the chart-expected secret name + keys ----
echo "==> translating CNPG creds → kx-ds-druid-metadata secret"
PG_USER=$(kubectl -n druid get secret druid-metadata-app -o jsonpath='{.data.username}' | base64 -d)
PG_PASS=$(kubectl -n druid get secret druid-metadata-app -o jsonpath='{.data.password}' | base64 -d)
PG_HOST=$(kubectl -n druid get secret druid-metadata-app -o jsonpath='{.data.host}' | base64 -d)
PG_DB=$(kubectl -n druid get secret druid-metadata-app -o jsonpath='{.data.dbname}' | base64 -d)
kubectl -n druid create secret generic kx-ds-druid-metadata \
  --from-literal=username="${PG_USER}" \
  --from-literal=password="${PG_PASS}" \
  --from-literal=host="${PG_HOST}" \
  --from-literal=dbname="${PG_DB}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ---- 3. Admin + system credential secrets ----
kubectl apply -f "${SCRIPT_DIR}/pre-install/secrets.yaml"

# ---- 4. MinIO buckets ----
echo "==> ensuring MinIO buckets exist"
kubectl delete job -n druid druid-minio-buckets --ignore-not-found
kubectl apply -f "${SCRIPT_DIR}/pre-install/minio-buckets-job.yaml"
kubectl -n druid wait --for=condition=Complete job/druid-minio-buckets --timeout=120s

# ---- 5. render chart, filter EKS-only resources, apply ----
echo "==> rendering Druid chart through post-renderer + applying"
helm template ds "${CHART_DIR}" \
  --namespace druid \
  --values "${CHART_BASE_VALUES}" \
  --values "${SCRIPT_DIR}/values-local.yaml" \
  | bash "${SCRIPT_DIR}/post-renderer.sh" \
  | kubectl apply --server-side --field-manager kx-druid -n druid -f -

echo
echo "==> waiting for Druid pods to be Ready (Druid startup probes are slow — up to 10min)"
kubectl -n druid rollout status statefulset/kx-ds-druid-coordinator --timeout=10m
kubectl -n druid rollout status statefulset/kx-ds-druid-overlord    --timeout=10m
kubectl -n druid rollout status statefulset/kx-ds-druid-historical  --timeout=10m
kubectl -n druid rollout status deployment/kx-ds-druid-broker       --timeout=10m
kubectl -n druid rollout status deployment/kx-ds-druid-router       --timeout=10m

echo
echo "Druid is up."
echo "  Router UI:        kubectl -n druid port-forward svc/kx-ds-druid-router 8088:8088"
echo "  Coordinator UI:   kubectl -n druid port-forward svc/kx-ds-druid-coordinator 8281:8281"
echo "  Admin password:   admin / admin (see pre-install/secrets.yaml)"
