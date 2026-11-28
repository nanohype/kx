#!/usr/bin/env bash
# Apache Druid for kx — runs the eks-gitops/catalog/druid chart unmodified, with a local
# filter (post-renderer.py) that strips EKS-only resources before kubectl apply.
#
# Why kubectl apply instead of `helm upgrade --install`:
#   Helm v4 dropped the arbitrary-executable --post-renderer flag in favor of a plugin model.
#   Rather than ship a helm plugin, this renders → filters → applies. `helm history` is
#   unavailable for druid, but disable is just `kubectl delete ns druid` (which sweeps the
#   CNPG cluster and every other resource).
#
# The Python this needs lives in post-renderer.py and metadata-secret.py beside this file
# rather than in quoted strings here, so the lint job reads it and the integrity gate
# compiles it. Nothing in CI runs this installer, so a program embedded in it would meet
# its first parser on a workstation.
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
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

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
  if err="$(kubectl --request-timeout=60s "$@" 2>&1 >/dev/null)"; then
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

# The post-renderer run over an empty render, before anything is created. It is
# the program step 5 runs, so this proves that program starts — PyYAML imports
# and the file compiles under this python3 — rather than proving something
# adjacent to it. Its own message names the remedy.
if ! render_err="$(python3 "${SCRIPT_DIR}/post-renderer.py" </dev/null 2>&1 >/dev/null)"; then
  echo "ERROR: the post-renderer cannot run — python3 said:" >&2
  printf '  %s\n' "$render_err" >&2
  exit 1
fi

# The runtime strings the chart reads, composed before anything is created. helm
# would replace the catalog's string with values-local.yaml's rather than merge
# them, so runtime-values.py writes each as the catalog's with kx's lines after
# it, and step 5 layers that file last. A catalog that no longer sets one of the
# strings kx adds to stops here, with the string named.
RUNTIME_VALUES="$(mktemp)"
trap 'rm -f "${RUNTIME_VALUES}"' EXIT
python3 "${SCRIPT_DIR}/runtime-values.py" "${CHART_BASE_VALUES}" "${SCRIPT_DIR}/values-local.yaml" \
  >"${RUNTIME_VALUES}"

kubectl --request-timeout=60s create namespace druid --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

# ---- 1. Postgres for metadata (CNPG) ----
echo "==> creating Postgres metadata cluster (CNPG)"
kubectl --request-timeout=60s apply -f "${SCRIPT_DIR}/pre-install/postgres.yaml"
echo "==> waiting for Postgres to be Ready (up to 5min)..."
kubectl --request-timeout=60s -n druid wait cluster/druid-metadata --for=condition=Ready --timeout=300s

# ---- 2. Translate CNPG-generated app credentials into the chart-expected secret name + keys ----
echo "==> translating CNPG creds → kx-ds-druid-metadata secret"
# Re-keyed on stdin rather than with --from-literal. That flag puts the Postgres
# password in this process's argv, where `ps` shows it to every other process on
# the workstation for as long as the command runs. CNPG's own Secret already
# holds these values base64-encoded, so metadata-secret.py copies them across
# without decoding them into an argument list at all. The name is the one the
# chart helper derives from the release step 5 renders: hostedId kx, release ds.
kubectl --request-timeout=60s -n druid get secret druid-metadata-app -o json \
  | python3 "${SCRIPT_DIR}/metadata-secret.py" kx-ds-druid-metadata druid \
  | kubectl --request-timeout=60s apply -f -

# ---- 3. Admin + system credential secrets ----
kubectl --request-timeout=60s apply -f "${SCRIPT_DIR}/pre-install/secrets.yaml"

# ---- 4. MinIO buckets ----
echo "==> ensuring MinIO buckets exist"
kubectl --request-timeout=60s --timeout=5m delete job -n druid druid-minio-buckets --ignore-not-found
kubectl --request-timeout=60s apply -f "${SCRIPT_DIR}/pre-install/minio-buckets-job.yaml"
kubectl --request-timeout=60s -n druid wait --for=condition=Complete job/druid-minio-buckets --timeout=120s

# ---- 5. render chart, filter EKS-only resources, apply ----
echo "==> rendering Druid chart through post-renderer + applying"
deadline 300 helm template ds "${CHART_DIR}" \
  --namespace druid \
  --values "${CHART_BASE_VALUES}" \
  --values "${SCRIPT_DIR}/values-local.yaml" \
  --values "${RUNTIME_VALUES}" \
  | python3 "${SCRIPT_DIR}/post-renderer.py" \
  | kubectl --request-timeout=60s apply --server-side --field-manager kx-druid -n druid -f -

echo
echo "==> waiting for Druid pods to be Ready (Druid startup probes are slow — up to 10min)"
kubectl --request-timeout=60s -n druid rollout status statefulset/kx-ds-druid-coordinator --timeout=10m
kubectl --request-timeout=60s -n druid rollout status statefulset/kx-ds-druid-overlord    --timeout=10m
kubectl --request-timeout=60s -n druid rollout status statefulset/kx-ds-druid-historical  --timeout=10m
kubectl --request-timeout=60s -n druid rollout status deployment/kx-ds-druid-broker       --timeout=10m
kubectl --request-timeout=60s -n druid rollout status deployment/kx-ds-druid-router       --timeout=10m

echo
echo "Druid is up."
echo "  Router UI:        kubectl -n druid port-forward svc/kx-ds-druid-router 8088:8088"
echo "  Coordinator UI:   kubectl -n druid port-forward svc/kx-ds-druid-coordinator 8281:8281"
echo "  Admin password:   admin / admin (see pre-install/secrets.yaml)"
