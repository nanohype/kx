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
# crds/ directory otherwise. What is guaranteed is narrower: the chart, its
# version pin, and its values file are the installer's. Anything else added to or
# stripped from that transform belongs in this list, so the header stays true as
# the transform widens.
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
  checks=0

  check() { # name expected file
    checks=$((checks + 1))
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
  checks=$((checks + 1))
  got="$(extract_helm_block "$t/oci.sh")"
  if [[ "$got" == *kubectl* ]]; then
    echo "FAIL  extraction swallowed a following kubectl line"; fails=$((fails + 1))
  else
    echo "  ok  trailing kubectl excluded"
  fi

  [[ "$fails" -eq 0 ]] || { echo "$fails extraction failure(s)."; exit 1; }
  echo "OK    $checks shape(s) extract as specified."
  exit 0
fi

# Always, before rendering anything. This script feeds the schema gate its
# input, so an extraction that silently lifted the wrong command would hand that
# gate a clean verdict over something the installer never runs — and a proof
# behind a flag is one the workflow can forget to ask for.
if ! "${BASH_SOURCE[0]}" --self-test >/dev/null 2>&1; then
  echo "FAIL  the extraction self-test does not pass — refusing to render." >&2
  "${BASH_SOURCE[0]}" --self-test >&2 || true
  exit 2
fi

# Slices with no `helm upgrade --install` block:
# - gateway-api-crds is kubectl-apply only
# - druid renders via helm template → filter → kubectl apply (Helm v4 dropped
#   the exec post-renderer); its chart is validated in eks-gitops CI, where it lives
# - bedrock-credentials installs no chart: it applies a Kyverno policy and an
#   aggregated ClusterRole, both plain manifests the yaml lint already covers
SKIP=("stack/core/gateway-api-crds" "stack/data/druid" "stack/ai-platform/bedrock-credentials")

# The exemptions, asserted rather than described. An entry naming a slice that
# no longer exists silently exempts whatever takes that path next, and an entry
# naming a slice that HAS grown a helm block exempts a render nobody asked to
# skip. Both rot toward permissive, which is why neither is left to a reader.
for s in "${SKIP[@]}"; do
  if [[ ! -f "$ROOT/$s/install.sh" ]]; then
    echo "FAIL  SKIP names $s, which has no install.sh — an exemption that outlives its slice."
    exit 2
  fi
  # The status is read rather than the condition being taken, because grep has
  # three answers and `if grep` has two. 0 is a match, 1 is a definite no match,
  # and anything above that is grep declining to answer — 2 for a file it cannot
  # read, 127 for a grep that is not there. Written as a condition, every one of
  # those reads as "no match" and the exemption stands unexamined: a guard
  # against an exemption hiding a slice, removed by the absence of the tool it
  # asks with. Only a definite no-match may let an exemption through.
  rc=0
  grep -qE '^[[:space:]]*helm upgrade --install' "$ROOT/$s/install.sh" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    echo "FAIL  SKIP names $s, but it does run \`helm upgrade --install\` — it would be rendered"
    echo "      if it were not exempt, so the exemption is hiding a slice from this gate."
    exit 2
  elif [[ "$rc" -ne 1 ]]; then
    echo "FAIL  could not determine whether $s runs \`helm upgrade --install\` — grep exited $rc."
    echo "      The exemption is unexamined, so it cannot be allowed to stand."
    exit 2
  fi
done

# An unmatched glob expands to the literal pattern, so every consumer below would
# read a path that does not exist and fail on whichever tool opened it first.
# That direction is safe by accident: it depends on those tools erroring rather
# than skipping, and a consumer that skipped would report a clean render over no
# slices. Assert the corpus before anything reads it.
shopt -s nullglob
scripts=("$ROOT"/stack/*/*/install.sh)
shopt -u nullglob
if [[ ${#scripts[@]} -eq 0 ]]; then
  echo "FAIL  no install.sh under stack/*/*/ — refusing to report a clean render over no slices."
  exit 2
fi

# helm is an authority, not a convenience: this gate reaches its verdict BY
# rendering, so without helm there is no verdict to give. Unasserted, the first
# invocation exits 127 naming the binary — non-zero, so the direction is right,
# but it names what was missing rather than what could not be determined, and
# the failure lands partway through a loop that has already printed OK lines.
if ! command -v helm >/dev/null 2>&1; then
  echo "FAIL  helm is not on PATH. This gate renders every slice to reach its verdict, so"
  echo "      without helm it cannot report on any of them."
  exit 2
fi

# Register every chart repo the scripts reference, once, then refresh
# exactly those (a bare `helm repo update` would also touch unrelated repos
# in the runner's helm config).
repos=()
while read -r line; do
  eval "$line" >/dev/null 2>&1 || true
  repos+=("$(awk '{print $4}' <<<"$line")")
done < <(grep -h '^helm repo add ' "${scripts[@]}" | sort -u)
helm repo update "${repos[@]}" >/dev/null

fail=0
for script in "${scripts[@]}"; do
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
  # ships its CRDs there, so its own ClusterComplianceReports would have nothing
  # to validate against and the mount check would never see those documents.
  cmd="${cmd} --include-crds"

  # Top-level VAR=... assignments from the script (e.g. the operator slice's
  # OPERATOR_REPO sibling-checkout path) — the helm block may reference them.
  # SCRIPT_DIR is excluded: the scripts derive it from BASH_SOURCE, which
  # doesn't survive eval; this script sets it to the slice dir itself.
  #
  # Assignments containing a command substitution are excluded too, and that is
  # a correctness rule rather than a precaution: this runs on a clusterless CI
  # runner, so a `$(docker ...)`, `$(kubectl ...)` or `$(git ...)` lifted out of
  # an installer executes here against a machine that has none of those things.
  # A helm block cannot reference one of these anyway — a chart reference
  # resolved by asking the local docker daemon is not a pin this gate could
  # check. Reported rather than dropped, because a silent exclusion is how a
  # gate ends up rendering something other than what it claims.
  # shellcheck disable=SC2016  # `$(` is the pattern to match, not one to expand
  assignments="$(grep -E '^[A-Z_]+=' "$script" | grep -v '^SCRIPT_DIR=' | grep -v '\$(' || true)"
  # shellcheck disable=SC2016  # same pattern, matched rather than expanded
  runtime_assignments="$(grep -E '^[A-Z_]+=' "$script" | grep -v '^SCRIPT_DIR=' | grep '\$(' || true)"
  if [[ -n "$runtime_assignments" ]]; then
    while IFS= read -r a; do
      echo "      note: ${slice} — not lifting ${a%%=*}=, it runs a command at install time"
    done <<<"$runtime_assignments"
  fi

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
