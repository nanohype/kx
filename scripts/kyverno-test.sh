#!/usr/bin/env bash
# `kyverno test` over tests/kyverno, with a floor on what it evaluated.
#
#   scripts/kyverno-test.sh            # the suite, with the kyverno on PATH
#
# The CLI exits 0 over a directory holding no tests, printing "No test yamls
# available" — the status a passing suite gets. A renamed directory, a narrowed
# path or a fixture set that stopped matching would all report success, so the
# run is held to a count of rows that evaluated a resource: the summary's total
# less the rows printed as Excluded, which the CLI counts as passes.
#
# Exit status: 0 every row passed and the floor holds; 1 a row failed or the
# floor does not hold; 2 the suite could not be run at all, which is a different
# fact from every policy passing.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS="tests/kyverno"

# Rows that evaluated a resource. Set to what the suite holds — the clone into
# two tenant namespaces, eight pods, and the three manifests the contract suite
# validates — so a fixture that stops reaching its policy fails here rather than
# passing as one fewer row.
MIN_EVALUATED=13

if ! command -v kyverno >/dev/null 2>&1; then
  echo "Cannot run: kyverno is not on PATH. No policy was tested — that is"
  echo "different from every policy passing."
  exit 2
fi

cd "$ROOT" || { echo "Cannot run: $ROOT cannot be entered."; exit 2; }

if [ ! -d "$TESTS" ]; then
  echo "Cannot run: $TESTS does not exist, so there is nothing to test."
  exit 2
fi

# No legacy policy anywhere in the tree. Kyverno 1.20 does not execute a
# kyverno.io policy, and chart 3.10's pre-upgrade hook refuses the upgrade while
# one exists on the cluster, so a manifest declaring one is a slice that breaks
# the security slice's next bump. The suite below cannot see it: it tests only
# the policies its Test files name. Read off git's index rather than a walk, so a
# scratch file in a working tree is not a finding and a tracked one is; git grep
# exits 1 for no match, and anything above 1 is a search that did not happen.
legacy="$(git -C "$ROOT" grep -lE '^apiVersion:[[:space:]]*["'\'']?kyverno\.io/' -- '*.yaml' '*.yml' 2>&1)"
grc=$?
if [ "$grc" -eq 0 ]; then
  echo "FAIL  a tracked manifest declares a legacy kyverno.io policy:"
  printf '%s\n' "$legacy" | sed 's/^/        /'
  echo "      Kyverno 1.20 does not run these; write it as a policies.kyverno.io kind."
  exit 1
elif [ "$grc" -ne 1 ]; then
  echo "Cannot run: git grep exited $grc searching for legacy kyverno.io manifests:"
  printf '%s\n' "$legacy" | sed 's/^/  /'
  exit 2
fi

echo "kyverno $(kyverno version 2>/dev/null | awk '/^Version:/ {print $2}')"
out="$(kyverno test "$TESTS" 2>&1)"
rc=$?
printf '%s\n' "$out"

# A load error is printed and the run carries on, so a policy file the CLI could
# not read leaves only the rows that did not need it — and those can all pass.
if printf '%s\n' "$out" | grep -q '^[[:space:]]*ERROR:'; then
  echo
  echo "FAIL  the CLI reported a load error above. Every row that needed what it"
  echo "      could not load is missing from the table, not failing in it."
  exit 1
fi

summary="$(printf '%s\n' "$out" | sed -n 's/^Test Summary: \([0-9][0-9]*\) tests passed and \([0-9][0-9]*\) tests failed.*/\1 \2/p' | tail -n 1)"
if [ -z "$summary" ]; then
  echo
  echo "FAIL  no 'Test Summary' line — the CLI ran nothing it reported on. An"
  echo "      unreported run is not a passing one."
  exit 1
fi
passed="${summary% *}"
failed="${summary#* }"
excluded="$(printf '%s\n' "$out" | grep -c '│ Pass[[:space:]]*│ Excluded' || true)"
evaluated=$((passed + failed - excluded))

if [ "$rc" -ne 0 ] || [ "$failed" -ne 0 ]; then
  echo
  echo "FAIL  $failed row(s) failed (kyverno exited $rc)."
  exit 1
fi

if [ "$evaluated" -lt "$MIN_EVALUATED" ]; then
  echo
  echo "FAIL  $evaluated row(s) evaluated a resource ($passed passed, $excluded of them"
  echo "      Excluded); the floor is $MIN_EVALUATED. A row that stopped reaching its"
  echo "      policy passes as Excluded, so a shrinking count is the only sign of it."
  exit 1
fi

echo
echo "OK    $evaluated row(s) evaluated a resource and passed, $excluded excluded as expected."
