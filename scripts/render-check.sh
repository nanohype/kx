#!/usr/bin/env bash
# Clusterless render gate over every helm-based slice.
#
# Each slice's install.sh pins a chart version and carries the values file; this
# script lifts that helm invocation and runs it as `helm template`, so CI catches
# a values/schema mismatch on a chart bump without a kind cluster. kubectl steps
# in the scripts (waits, labels, secrets) are install-time concerns and are not
# exercised here.
#
# NOT the same command the installer runs, and the difference is deliberate:
# `--wait` and `--hide-notes` are stripped because they are meaningless to
# `helm template`, and `--include-crds` is added because helm omits a chart's
# crds/ directory otherwise. The header used to claim it extracted the invocation
# "exactly", which was already untrue of the two stripped flags before
# --include-crds made it a third — a claim slightly false is one nobody re-reads
# when they widen it. What is guaranteed is narrower and worth stating plainly:
# the chart, its version pin, and its values file are the installer's.
#
# `--self-test` proves the lifting works, over the shapes the tree actually
# contains. This script is the input to check-rendered-schemas.py, so an
# extraction that silently returned the wrong command would give that gate a
# clean bill of health over something the installer never runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Collapse the `helm upgrade --install ...` continuation block to one line.
extract_helm_block() {
  awk '
    /^helm upgrade --install/ { collecting = 1 }
    collecting {
      line = $0
      cont = (line ~ /\\$/)
      sub(/[[:space:]]*\\$/, "", line)
      printf "%s ", line
      if (!cont) exit
    }
  ' "$1"
}

# Prove the lifting handles the shapes in the tree, and reports nothing rather
# than something wrong when there is no block. Runs before the render in CI.
if [[ "${1:-}" == "--self-test" ]]; then
  t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
  fails=0

  check() { # name expected file
    got="$(extract_helm_block "$3")"
    if [[ "$got" != "$2" ]]; then
      echo "FAIL  $1"; echo "        want: [$2]"; echo "        got:  [$got]"
      fails=$((fails + 1))
    else
      echo "  ok  $1"
    fi
  }

  printf 'helm repo add x https://x\nhelm upgrade --install a x/a \\\n  --namespace n \\\n  --version 1.2.3\n' >"$t/multi.sh"
  check "multi-line continuation" "helm upgrade --install a x/a   --namespace n   --version 1.2.3 " "$t/multi.sh"

  printf 'helm upgrade --install a x/a --version 1.2.3\n' >"$t/single.sh"
  check "single line" "helm upgrade --install a x/a --version 1.2.3 " "$t/single.sh"

  printf 'helm upgrade --install a oci://ghcr.io/o/c \\\n  --version 0.1.0\nkubectl apply -f x.yaml\n' >"$t/oci.sh"
  check "stops at the end of the block" "helm upgrade --install a oci://ghcr.io/o/c   --version 0.1.0 " "$t/oci.sh"

  printf '#!/usr/bin/env bash\nkubectl apply -f x.yaml\n' >"$t/none.sh"
  check "no helm block yields nothing" "" "$t/none.sh"

  # The one that matters: a trailing kubectl line must not be swallowed into the
  # command. An extractor that ran to EOF would return something that still looks
  # like a helm invocation and renders, so the schema gate downstream would grade
  # output the installer never produces.
  got="$(extract_helm_block "$t/oci.sh")"
  if [[ "$got" == *kubectl* ]]; then
    echo "FAIL  extraction swallowed a following kubectl line"; fails=$((fails + 1))
  else
    echo "  ok  trailing kubectl excluded"
  fi

  [[ "$fails" -eq 0 ]] || { echo "$fails extraction failure(s)."; exit 1; }
  echo "OK    extraction behaves as specified over 5 shapes."
  exit 0
fi

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

  block="$(extract_helm_block "$script")"

  if [[ -z "$block" ]]; then
    echo "FAIL  $slice — no helm upgrade --install block found (update SKIP if intentional)"
    fail=1
    continue
  fi

  cmd="${block/helm upgrade --install/helm template}"
  cmd="${cmd// --wait/}"
  cmd="${cmd// --hide-notes/}"
  # --include-crds because `helm template` omits a chart's crds/ directory by
  # default, and helm installs those separately at release time. Without it the
  # render is missing exactly the definitions the schema gate needs: trivy-operator
  # ships twelve CRDs there, so its own ClusterComplianceReports had nothing to
  # validate against and the mount check never saw those documents either.
  cmd="${cmd} --include-crds"

  # Top-level VAR=... assignments from the script (e.g. the operator slice's
  # OPERATOR_REPO sibling-checkout path) — the helm block may reference them.
  # SCRIPT_DIR is excluded: the scripts derive it from BASH_SOURCE, which
  # doesn't survive eval; this script sets it to the slice dir itself.
  assignments="$(grep -E '^[A-Z_]+=' "$script" | grep -v '^SCRIPT_DIR=' || true)"

  # The render is piped into the mount check rather than discarded. A chart that
  # starts providing a volume a values file already hand-rolls renders two mounts
  # on one path, which the API server rejects and `helm template` does not — so
  # exit status alone cannot see it. pipefail (set above) carries either failure.
  # KX_RENDER_OUT keeps the rendered manifests instead of discarding them, so a
  # second pass can validate them against schemas. The schema gate cannot run as
  # a per-slice pipe like the mount check: a custom resource in one slice is
  # defined by a CRD shipped in another, so nothing can be validated until every
  # slice has rendered. Unset, behaviour is unchanged.
  out_file=/dev/null
  if [[ -n "${KX_RENDER_OUT:-}" ]]; then
    mkdir -p "$KX_RENDER_OUT"
    out_file="$KX_RENDER_OUT/${slice//\//__}.yaml"
  fi

  if (
    # shellcheck disable=SC2030,SC2034  # consumed by the eval'd block
    SCRIPT_DIR="$(dirname "$script")"
    [[ -n "$assignments" ]] && eval "$assignments"
    eval "$cmd" | tee "$out_file" | python3 "$ROOT/scripts/check-rendered-mounts.py" "$slice"
  ); then
    echo "OK    $slice"
  else
    echo "FAIL  $slice"
    fail=1
  fi
done

exit "$fail"
