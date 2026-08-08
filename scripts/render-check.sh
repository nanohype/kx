#!/usr/bin/env bash
# Clusterless render gate over every helm-based slice.
#
# Each slice's install.sh pins a chart version and carries the values file;
# this script extracts exactly that helm invocation and runs it as
# `helm template`, so CI catches a values/schema mismatch on a chart bump
# without a kind cluster. kubectl steps in the scripts (waits, labels,
# secrets) are install-time concerns and are not exercised here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Slices with no `helm upgrade --install` block:
# - gateway-api-crds is kubectl-apply only
# - druid renders via helm template → filter → kubectl apply (Helm v4 dropped
#   the exec post-renderer); its chart is validated in eks-gitops CI, where it lives
# - bedrock-credentials installs no chart: it applies a Kyverno policy and an
#   aggregated ClusterRole, both plain manifests the yaml lint already covers
SKIP=("stack/core/gateway-api-crds" "stack/data/druid" "stack/ai-platform/bedrock-credentials")

# Register every chart repo the scripts reference, once, then refresh
# exactly those (a bare `helm repo update` would also touch unrelated repos
# in the runner's helm config).
repos=()
while read -r line; do
  eval "$line" >/dev/null 2>&1 || true
  repos+=("$(awk '{print $4}' <<<"$line")")
done < <(grep -h '^helm repo add ' "$ROOT"/stack/*/*/install.sh | sort -u)
helm repo update "${repos[@]}" >/dev/null

fail=0
for script in "$ROOT"/stack/*/*/install.sh; do
  slice="${script#"$ROOT"/}"
  slice="${slice%/install.sh}"
  for s in "${SKIP[@]}"; do
    if [[ "$slice" == "$s" ]]; then
      echo "SKIP  $slice (no helm invocation)"
      continue 2
    fi
  done

  # Collapse the `helm upgrade --install ...` continuation block to one line.
  block="$(awk '
    /^helm upgrade --install/ { collecting = 1 }
    collecting {
      line = $0
      cont = (line ~ /\\$/)
      sub(/[[:space:]]*\\$/, "", line)
      printf "%s ", line
      if (!cont) exit
    }
  ' "$script")"

  if [[ -z "$block" ]]; then
    echo "FAIL  $slice — no helm upgrade --install block found (update SKIP if intentional)"
    fail=1
    continue
  fi

  cmd="${block/helm upgrade --install/helm template}"
  cmd="${cmd// --wait/}"
  cmd="${cmd// --hide-notes/}"

  # Top-level VAR=... assignments from the script (e.g. the operator slice's
  # OPERATOR_REPO sibling-checkout path) — the helm block may reference them.
  # SCRIPT_DIR is excluded: the scripts derive it from BASH_SOURCE, which
  # doesn't survive eval; this script sets it to the slice dir itself.
  assignments="$(grep -E '^[A-Z_]+=' "$script" | grep -v '^SCRIPT_DIR=' || true)"

  # The render is piped into the mount check rather than discarded. A chart that
  # starts providing a volume a values file already hand-rolls renders two mounts
  # on one path, which the API server rejects and `helm template` does not — so
  # exit status alone cannot see it. pipefail (set above) carries either failure.
  if (
    # shellcheck disable=SC2030,SC2034  # consumed by the eval'd block
    SCRIPT_DIR="$(dirname "$script")"
    [[ -n "$assignments" ]] && eval "$assignments"
    eval "$cmd" | python3 "$ROOT/scripts/check-rendered-mounts.py" "$slice"
  ); then
    echo "OK    $slice"
  else
    echo "FAIL  $slice"
    fail=1
  fi
done

exit "$fail"
