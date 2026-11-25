#!/usr/bin/env bash
# pytest over tests/ under coverage, held to the floors pyproject.toml states.
#
#   scripts/python-test.sh             # the suite, with the python3 on PATH
#
# pytest reports success over whatever it collected, so a test that stopped
# running reads the same as one that passed. The floors are what notice: a test
# that no longer runs leaves the code it reached unmeasured, and its population
# falls below its floor.
#
# The floors live in pyproject.toml, under [tool.kx.coverage-floors], and
# tests/coverage_floors.py reads them against the run:
#   security-path  the helpers kx's installers run, under stack/. Each handles a
#                  cloud credential or decides what reaches the cluster, so each
#                  file on its own is held to every line and every branch.
#   gates          the gates under scripts/, together: 75% of lines and 60% of
#                  branches, read apart rather than folded into one number.
# Every measured file falls under exactly one floor, so a file added without a
# test fails its floor rather than going unreported.
#
# Exit status: 0 the tests passed and every floor holds; 1 a test failed or a
# floor does not hold; 2 the suite could not be run at all, which is a different
# fact from every test passing.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v git >/dev/null 2>&1; then
  echo "Cannot run: git is not on PATH, and the gates' self-tests build their fixtures"
  echo "as git repositories. No test ran — that is different from every test passing."
  exit 2
fi
if ! command -v kubeconform >/dev/null 2>&1; then
  echo "Cannot run: kubeconform is not on PATH, and the schema gate's self-test runs it."
  echo "No test ran — that is different from every test passing."
  exit 2
fi
for module in pytest coverage; do
  if ! python3 -m "${module}" --version >/dev/null 2>&1; then
    echo "Cannot run: this python3 has no ${module}. Install what the test job installs:"
    echo "  pip install --require-hashes -r .github/test-requirements.txt"
    exit 2
  fi
done

cd "$ROOT" || { echo "Cannot run: $ROOT cannot be entered."; exit 2; }

python3 -m coverage erase
python3 -m coverage run -m pytest
status=$?
case "${status}" in
  0) ;;
  1)
    echo
    echo "FAIL  a test failed. The floors are not read over a failing suite."
    exit 1
    ;;
  5)
    echo "Cannot run: pytest collected no tests under tests/, so nothing was measured."
    exit 2
    ;;
  *)
    echo "Cannot run: pytest exited ${status}, which is a fault in the run rather than"
    echo "a verdict on the tests."
    exit 2
    ;;
esac

echo
python3 -m coverage report
echo
echo "==> the floors in pyproject.toml"
python3 tests/coverage_floors.py
