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
# crds/ directory otherwise. A slice that renders rather than installs —
# `helm template ... >"${VAR}"` into a kubectl apply, as envoy-gateway-crds does
# because its chart is too large to store as a release — is lifted as written,
# minus that trailing redirect: the render goes to this script's pipe, not the
# installer's file. What is guaranteed is narrower: the chart, its version pin,
# and its values file are the installer's. Anything else added to or stripped
# from that transform belongs in this list, so the header stays true as the
# transform widens.
#
# `--self-test` proves the lifting works, over the shapes the tree actually
# contains, and that a slice's render reaches the mount check and the output
# file. This script is the input to check-rendered-schemas.py, so an extraction
# that silently returned the wrong command would give that gate a clean bill of
# health over something the installer never runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Collapse the `helm upgrade --install ...` (or `helm template ...`) continuation
# block to one line. The `deadline N` an installer runs it under is dropped, since
# the render below runs the command itself. On the block's last line, a redirect
# of stdout to a shell variable is dropped: that is where a rendering installer
# sends its output, and here the output belongs to the pipe below. Bracket
# expressions rather than escapes, so BSD awk on a workstation reads the pattern
# the way the runner does.
extract_helm_block() {
  awk '
    /^(deadline [0-9]+ )?helm (upgrade --install|template) / { collecting = 1 }
    collecting {
      line = $0
      sub(/^deadline [0-9]+ /, "", line)
      cont = (line ~ /\\$/)
      sub(/[[:space:]]*\\$/, "", line)
      if (!cont) sub(/[[:space:]]+>[[:space:]]*"?[$][{]?[A-Za-z_][A-Za-z0-9_]*[}]?"?[[:space:]]*$/, "", line)
      printf "%s ", line
      if (!cont) exit
    }
  ' "$1"
}

# The pattern a helm block starts with, for the SKIP guard below. Wider than the
# extractor's anchor (leading whitespace allowed), so the guard sees every block
# extraction could lift and some it could not.
HELM_BLOCK_RE='^[[:space:]]*(deadline[[:space:]]+[0-9]+[[:space:]]+)?helm (upgrade --install|template) '

# Succeeds when an installer applies one of its own manifests: a `kubectl apply
# -f` naming the file beside it, with kubectl's flags allowed on either side of
# the verb, since every kubectl call carries --request-timeout and that flag sits
# between the binary and the verb. The name is matched as written rather than as
# a pattern. Exits 1 when the installer does not apply the file, and with grep's
# own status above that when it could not tell.
applies_manifest() { # install.sh file
  local name flags
  name="$(printf '%s' "$2" | sed 's/[][\.*^$+?(){}|]/\\&/g')"
  flags='([[:space:]]+-[-[:alnum:]]+((=|[[:space:]]+)[^-[:space:]][^[:space:]]*)?)*'
  grep -qE "^[[:space:]]*kubectl${flags}[[:space:]]+apply${flags}[[:space:]]+-f[[:space:]]+\"[\$][{]SCRIPT_DIR[}]/${name}\"" "$1"
}

# Succeeds when the script's helm block renders a chart read from a sibling
# eks-gitops checkout: no --version pin, and a chart argument that is one shell
# variable whose assignment in the same script points at ../eks-gitops/. That is
# a chart this repository does not pin and a CI runner does not have, so there is
# nothing here to render; it is validated in eks-gitops CI, where it lives.
# Anything else — a pinned chart, a literal path, a variable assigned elsewhere —
# fails, so the exemption below cannot stretch over a chart this gate could
# render.
renders_sibling_chart() { # file
  local block arg var re
  block="$(extract_helm_block "$1")"
  [[ -n "$block" && "$block" != *" --version "* ]] || return 1
  arg="$(awk '{ print ($2 == "template") ? $4 : $5 }' <<<"$block")"
  re='^"?[$][{]([A-Za-z_][A-Za-z0-9_]*)[}]"?$'
  [[ "$arg" =~ $re ]] || return 1
  var="${BASH_REMATCH[1]}"
  grep -qE "^${var}=.*[.][.]/eks-gitops/" "$1"
}

# One slice's lifted command, rendered into the mount check, with the render
# kept in out_file for the schema gate (/dev/null when nothing keeps it). The
# mount check reads the pipe rather than a file, so the manifests it judges are
# the ones the render produced, and its verdict is the slice's. pipefail carries
# a render that fails after printing, which the mount check alone would pass.
# The command is eval'd here, so the caller sets the variables it references.
render_and_check() { # slice out_file cmd
  eval "$3" | tee "$2" | python3 "$ROOT/scripts/check-rendered-mounts.py" "$1"
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

  # shellcheck disable=SC2016  # the fixture is shell source; nothing here expands
  printf 'M="$(mktemp)"\nhelm template a oci://r/c \\\n  --version 1.0.0 \\\n  --values "${SCRIPT_DIR}/v.yaml" >"${M}"\nkubectl apply -f "${M}"\n' >"$t/rendered.sh"
  # shellcheck disable=SC2016  # the expected lift, verbatim, unexpanded
  check "a rendering block, minus its redirect" 'helm template a oci://r/c   --version 1.0.0   --values "${SCRIPT_DIR}/v.yaml" ' "$t/rendered.sh"

  printf 'helm repo add x https://x\ndeadline 900 helm upgrade --install a x/a \\\n  --namespace n \\\n  --wait --timeout 10m\n' >"$t/deadline.sh"
  check "a block under deadline, lifted without it" "helm upgrade --install a x/a   --namespace n   --wait --timeout 10m " "$t/deadline.sh"

  # shellcheck disable=SC2016  # the fixture is shell source; nothing here expands
  printf 'M="$(mktemp)"\ndeadline 300 helm template a oci://r/c \\\n  --version 1.0.0 >"${M}"\nkubectl apply -f "${M}"\n' >"$t/deadline-rendered.sh"
  check "a rendering block under deadline, minus its redirect" "helm template a oci://r/c   --version 1.0.0 " "$t/deadline-rendered.sh"

  printf '#!/usr/bin/env bash\nkubectl apply -f x.yaml\n' >"$t/none.sh"
  check "no helm block yields nothing" "" "$t/none.sh"

  # The SKIP guard reads the same blocks: one under deadline is a helm block.
  checks=$((checks + 1))
  rc=0
  grep -qE "$HELM_BLOCK_RE" "$t/deadline.sh" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    echo "FAIL  the SKIP guard does not see a helm block under deadline (grep exited $rc)"
    fails=$((fails + 1))
  else
    echo "  ok  the SKIP guard sees a helm block under deadline"
  fi

  # The sibling-chart classifier the druid exemption rests on: the druid shape
  # passes, and each way a chart could be one this gate can render does not.
  sibling() { # name want(0|1) file
    checks=$((checks + 1))
    local rc=0
    renders_sibling_chart "$3" || rc=$?
    if [[ "$rc" -ne "$2" ]]; then
      echo "FAIL  $1 (classifier returned $rc, want $2)"; fails=$((fails + 1))
    else
      echo "  ok  $1"
    fi
  }
  # shellcheck disable=SC2016  # fixtures are shell source; nothing here expands
  printf 'C="${X:-${SCRIPT_DIR}/../../eks-gitops/catalog/c/chart}"\nhelm template ds "${C}" \\\n  --values v.yaml \\\n  | kubectl apply -f -\n' >"$t/sibling.sh"
  sibling "a sibling eks-gitops chart is classified as one" 0 "$t/sibling.sh"
  # shellcheck disable=SC2016
  printf 'C="${X:-${SCRIPT_DIR}/../../eks-gitops/catalog/c/chart}"\ndeadline 300 helm template ds "${C}" \\\n  --values v.yaml \\\n  | kubectl apply -f -\n' >"$t/sibling-deadline.sh"
  sibling "a sibling chart rendered under deadline is classified as one" 0 "$t/sibling-deadline.sh"
  # shellcheck disable=SC2016
  printf 'C="${X:-${SCRIPT_DIR}/../../eks-gitops/catalog/c/chart}"\nhelm template ds "${C}" \\\n  --version 1.0.0\n' >"$t/sibling-pinned.sh"
  sibling "a pinned chart is not a sibling chart" 1 "$t/sibling-pinned.sh"
  # shellcheck disable=SC2016
  printf 'C="${SCRIPT_DIR}/chart"\nhelm template ds "${C}"\n' >"$t/local.sh"
  sibling "a chart inside this repository is not a sibling chart" 1 "$t/local.sh"
  sibling "a pinned OCI render is not a sibling chart" 1 "$t/rendered.sh"
  sibling "no helm block is not a sibling chart" 1 "$t/none.sh"

  # The match MANIFESTS rests on: an apply of the file beside the installer
  # counts with kubectl's flags around the verb, and a commented-out apply, an
  # apply of another file and a name that matches only as a pattern do not.
  applies() { # name want(0|1) install.sh file
    checks=$((checks + 1))
    local rc=0
    applies_manifest "$3" "$4" || rc=$?
    if [[ "$rc" -ne "$2" ]]; then
      echo "FAIL  $1 (returned $rc, want $2)"; fails=$((fails + 1))
    else
      echo "  ok  $1"
    fi
  }
  # shellcheck disable=SC2016  # fixtures are shell source; nothing here expands
  printf 'kubectl apply -f "${SCRIPT_DIR}/m.yaml"\n' >"$t/apply.sh"
  applies "a plain apply of the manifest" 0 "$t/apply.sh" m.yaml
  # shellcheck disable=SC2016
  printf 'kubectl --request-timeout=60s -n x apply --server-side -f "${SCRIPT_DIR}/m.yaml"\n' >"$t/flags.sh"
  applies "an apply with kubectl's flags around the verb" 0 "$t/flags.sh" m.yaml
  # shellcheck disable=SC2016
  printf '# kubectl apply -f "${SCRIPT_DIR}/m.yaml"\n' >"$t/commented.sh"
  applies "a commented-out apply is not one" 1 "$t/commented.sh" m.yaml
  # shellcheck disable=SC2016
  printf 'kubectl apply -f "${SCRIPT_DIR}/mXyaml"\n' >"$t/pattern.sh"
  applies "a name is matched as written, not as a pattern" 1 "$t/pattern.sh" m.yaml
  applies "an apply of another file is not one" 1 "$t/apply.sh" n.yaml

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

  # What the render loop does with a lifted command, end to end: the render
  # reaches the mount check and its verdict is the slice's, a render that fails
  # fails the slice even when what it printed was clean, and the render lands in
  # the file the schema gate reads. Each case differs from the clean one in the
  # one way it names.
  pod() { # the second mount's path
    printf 'apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers:\n    - name: c\n      image: r/x:1\n      volumeMounts:\n        - {name: a, mountPath: /a}\n        - {name: b, mountPath: %s}\n  volumes:\n    - {name: a, emptyDir: {}}\n    - {name: b, emptyDir: {}}\n' "$1"
  }
  pod /b >"$t/mounts-clean.yaml"
  pod /a >"$t/mounts-twice.yaml"
  composed() { # name want(0|1) cmd
    checks=$((checks + 1))
    local rc=0
    render_and_check fixture "$t/kept.yaml" "$3" >/dev/null 2>&1 || rc=$?
    [[ "$rc" -eq 0 ]] || rc=1
    if [[ "$rc" -ne "$2" ]]; then
      echo "FAIL  $1 (the slice's pipe exited $rc, want $2)"; fails=$((fails + 1))
    else
      echo "  ok  $1"
    fi
  }
  composed "a render with two mounts on one path fails its slice" 1 "cat '$t/mounts-twice.yaml'"
  composed "a render that fails after printing clean output fails its slice" 1 "cat '$t/mounts-clean.yaml'; false"
  composed "a clean render passes its slice" 0 "cat '$t/mounts-clean.yaml'"

  checks=$((checks + 1))
  rm -f "$t/kept.yaml"
  render_and_check fixture "$t/kept.yaml" "cat '$t/mounts-clean.yaml'" >/dev/null 2>&1 || true
  if cmp -s "$t/kept.yaml" "$t/mounts-clean.yaml"; then
    echo "  ok  the render is kept for the schema gate"
  else
    echo "FAIL  the render is not kept in the output file the schema gate reads"; fails=$((fails + 1))
  fi

  [[ "$fails" -eq 0 ]] || { echo "$fails case(s) wrong."; exit 1; }
  echo "OK    $checks case(s) behave as specified."
  exit 0
fi

# Always, before rendering anything. This script feeds the schema gate its
# input, so an extraction that silently lifted the wrong command would hand that
# gate a clean verdict over something the installer never runs — and a proof
# behind a flag is one the workflow can forget to ask for.
if ! "${BASH_SOURCE[0]}" --self-test >/dev/null 2>&1; then
  echo "FAIL  the self-test does not pass — refusing to render." >&2
  "${BASH_SOURCE[0]}" --self-test >&2 || true
  exit 2
fi

# Slices the render loop does not lift, each as `slice:reason`. The reason is
# not a comment: the guard below asserts it against the slice's install.sh.
# - no-helm: the script has no helm block at all, so there is nothing to lift.
#   gateway-api-crds is kubectl-apply only; bedrock-credentials installs no
#   chart, it applies two Kyverno policies and an aggregated ClusterRole, plain
#   manifests listed in MANIFESTS below.
# - sibling-chart: the script does have a helm block, but its chart is read from
#   a sibling eks-gitops checkout rather than pinned here (see
#   renders_sibling_chart). druid renders via helm template → filter → kubectl
#   apply (Helm v4 dropped the exec post-renderer), against the catalog's own
#   chart, which is validated in eks-gitops CI, where it lives.
SKIP=(
  "stack/core/gateway-api-crds:no-helm"
  "stack/data/druid:sibling-chart"
  "stack/ai-platform/bedrock-credentials:no-helm"
)

# Plain manifests a slice applies with `kubectl apply -f`, written into the
# render output unchanged so the schema gate validates them with everything
# else. Rendering is not what they need — there is nothing to template — but
# validating is: bedrock-credentials declares Kyverno's GeneratingPolicy and
# MutatingPolicy, and the schemas for those come from the CRDs the security
# slice's pinned Kyverno chart renders. A kind or field that chart does not
# serve fails there, on the pull request, rather than at `kubectl apply` on a
# workstation. Each entry is `slice:file`, and the slice's install.sh must apply
# the file, so the list cannot validate a manifest the installer never sends.
MANIFESTS=(
  "stack/ai-platform/bedrock-credentials:rbac.yaml"
  "stack/ai-platform/bedrock-credentials:policy.yaml"
)

# The exemptions, asserted rather than described. An entry naming a slice that
# no longer exists silently exempts whatever takes that path next, and an entry
# naming a slice that HAS grown a helm block exempts a render nobody asked to
# skip. Both rot toward permissive, which is why neither is left to a reader.
for entry in "${SKIP[@]}"; do
  s="${entry%%:*}"
  reason="${entry#*:}"
  if [[ ! -f "$ROOT/$s/install.sh" ]]; then
    echo "FAIL  SKIP names $s, which has no install.sh — an exemption that outlives its slice."
    exit 2
  fi
  case "$reason" in
    no-helm)
      # The status is read rather than the condition being taken, because grep
      # has three answers and `if grep` has two. 0 is a match, 1 is a definite
      # no match, and anything above that is grep declining to answer — 2 for a
      # file it cannot read, 127 for a grep that is not there. Written as a
      # condition, every one of those reads as "no match" and the exemption
      # stands unexamined: a guard against an exemption hiding a slice, removed
      # by the absence of the tool it asks with. Only a definite no-match may
      # let this exemption through.
      rc=0
      grep -qE "$HELM_BLOCK_RE" "$ROOT/$s/install.sh" || rc=$?
      if [[ "$rc" -eq 0 ]]; then
        echo "FAIL  SKIP exempts $s as no-helm, but it runs \`helm upgrade --install\` or \`helm template\`"
        echo "      — it would be rendered if it were not exempt, so the exemption is hiding a slice."
        exit 2
      elif [[ "$rc" -ne 1 ]]; then
        echo "FAIL  could not determine whether $s has a helm block — grep exited $rc."
        echo "      The exemption is unexamined, so it cannot be allowed to stand."
        exit 2
      fi
      ;;
    sibling-chart)
      # Fails closed: a grep that cannot answer reads as "not a sibling chart",
      # which rejects the exemption rather than granting it.
      if ! renders_sibling_chart "$ROOT/$s/install.sh"; then
        echo "FAIL  SKIP exempts $s as sibling-chart, but its helm block does not render an"
        echo "      unpinned chart from a sibling eks-gitops checkout — this gate could render it."
        exit 2
      fi
      ;;
    *)
      echo "FAIL  SKIP gives $s the reason '$reason', which the guard does not know how to assert."
      exit 2
      ;;
  esac
done

# The manifest entries, asserted like the exemptions: a file that is gone, or
# one its installer no longer applies, is a validation of something nothing
# installs. Checked before anything renders, so the run stops on it.
for m in "${MANIFESTS[@]}"; do
  slice="${m%%:*}"
  file="${m#*:}"
  if [[ ! -f "$ROOT/$slice/$file" ]]; then
    echo "FAIL  MANIFESTS names $slice/$file, which does not exist."
    exit 2
  fi
  rc=0
  applies_manifest "$ROOT/$slice/install.sh" "$file" || rc=$?
  if [[ "$rc" -eq 1 ]]; then
    echo "FAIL  MANIFESTS names $slice/$file, but $slice/install.sh does not apply it."
    exit 2
  elif [[ "$rc" -ne 0 ]]; then
    echo "FAIL  could not determine whether $slice/install.sh applies $file — grep exited $rc."
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
  for entry in "${SKIP[@]}"; do
    if [[ "$slice" == "${entry%%:*}" ]]; then
      echo "SKIP  $slice (${entry#*:})"
      continue 2
    fi
  done

  block="$(extract_helm_block "$script")"

  if [[ -z "$block" ]]; then
    echo "FAIL  $slice — no helm upgrade --install or helm template block found (update SKIP if intentional)"
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
    render_and_check "$slice" "$out_file" "$cmd"
  ); then
    echo "OK    $slice"
  else
    echo "FAIL  $slice"
    fail=1
  fi
done

# After every chart, so the CRDs these are validated against are already in the
# output. One render file per slice, named the way the helm renders are, and
# cleared first: the output directory is reused when this is re-run, and
# appending to last run's file would validate every manifest twice.
if [[ -n "${KX_RENDER_OUT:-}" ]]; then
  mkdir -p "$KX_RENDER_OUT"
  for m in "${MANIFESTS[@]}"; do
    slice="${m%%:*}"
    rm -f "$KX_RENDER_OUT/${slice//\//__}.yaml"
  done
  for m in "${MANIFESTS[@]}"; do
    slice="${m%%:*}"
    out_file="$KX_RENDER_OUT/${slice//\//__}.yaml"
    if [[ -s "$out_file" ]]; then
      printf -- '---\n' >>"$out_file"
    fi
    cat "$ROOT/$slice/${m#*:}" >>"$out_file"
    echo "OK    $slice/${m#*:} (plain manifest, written for the schema gate)"
  done
fi

exit "$fail"
