#!/usr/bin/env bash
# Apache Druid for kx — runs the eks-gitops/catalog/druid chart unmodified, with a local
# filter (post-renderer.sh) that strips EKS-only resources before kubectl apply.
#
# Why kubectl apply instead of `helm upgrade --install`:
#   Helm v4 dropped the arbitrary-executable --post-renderer flag in favor of a plugin model.
#   Rather than ship a helm plugin, this renders → filters → applies. `helm history` is
#   unavailable for druid,
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
#
# Each check names a remedy, so each has to be sure of the cause. `kubectl get`
# exits non-zero for a missing object AND for an unreachable API server, a wrong
# context or a denied request — and discarding its stderr to assert the first
# tells an operator to install something when the real problem is that nothing
# is listening. The message they are given then costs them a search that cannot
# succeed. So the output is kept and shown whenever it is not a plain NotFound.
require() { # description remedy kubectl-args...
  local what="$1" remedy="$2"; shift 2
  local err
  if err="$(kubectl "$@" 2>&1 >/dev/null)"; then
    return 0
  fi
  if printf '%s' "$err" | grep -qiE 'not found|NotFound'; then
    echo "ERROR: ${what} is not installed. ${remedy}" >&2
  else
    echo "ERROR: could not determine whether ${what} is installed — kubectl said:" >&2
    # Deduplicated: kubectl repeats its discovery failure once per retry, and
    # four copies of one line is noise in front of the one fact that matters.
    printf '%s\n' "$err" | sort -u | sed 's/^/  /' >&2
    echo "Fix that first; ${remedy} will not help until kubectl can reach the cluster." >&2
  fi
  exit 1
}

[ -d "${CHART_DIR}" ] \
  || { echo "ERROR: chart not found at ${CHART_DIR}" >&2; exit 1; }
require "CNPG"         "Run: task stack:data:enable" get crd clusters.postgresql.cnpg.io
require "MinIO"        "Run: task stack:data:enable" get ns minio
require "cert-manager" "It is in core; run: task up"  get crd certificates.cert-manager.io

if ! yaml_err="$(python3 -c "import yaml" 2>&1)"; then
  echo "ERROR: the post-renderer needs python3 with PyYAML — python3 said:" >&2
  printf '  %s\n' "$yaml_err" >&2
  echo "Run: pip3 install pyyaml" >&2
  exit 1
fi

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
