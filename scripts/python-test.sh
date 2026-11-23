#!/usr/bin/env bash
# pytest over tests/ under coverage, with a floor on each population it measures.
#
#   scripts/python-test.sh             # the suite, with the python3 on PATH
#
# pytest reports success over whatever it collected, so a test that stopped
# running reads the same as one that passed. The floors are what notice: a test
# that no longer runs leaves the code it reached unmeasured, and the coverage of
# its population falls below the floor.
#
# Two populations, two floors:
#   stack/    the helpers kx's installers run. They handle a cloud credential or
#             decide what reaches the cluster, so every line and every branch.
#   scripts/  the gates, measured through their own self-tests and held to
#             GATE_FLOOR below. A ratchet: the run fails when the gates' coverage
#             falls below the floor, and when it has risen a whole point past it
#             without the floor following, so a gain cannot be given back later
#             without a diff that lowers the number.
#
# Exit status: 0 the tests passed and both floors hold; 1 a test failed or a
# floor does not hold; 2 the suite could not be run at all, which is a different
# fact from every test passing.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The gates' coverage in whole percent, rounded down, as of the last change that
# moved it. Raised with the coverage, never lowered.
GATE_FLOOR=66

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

failed=0

echo
echo "==> stack/: the installer helpers, at every line and every branch"
python3 -m coverage report --include='stack/*' --fail-under=100 || failed=1

echo
echo "==> scripts/: the gates, at their floor of ${GATE_FLOOR}%"
python3 -m coverage report --include='scripts/*' --fail-under="${GATE_FLOOR}" || failed=1

total="$(python3 -m coverage report --include='scripts/*' --format=total)"
if [[ ! "${total}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  echo "Cannot run: read the gates' coverage as '${total}', which is not a percentage."
  exit 2
fi
if awk -v t="${total}" -v f="${GATE_FLOOR}" 'BEGIN { exit !(t >= f + 1) }'; then
  echo "FAIL  the gates' coverage is ${total}%, a whole point past GATE_FLOOR=${GATE_FLOOR}."
  echo "      Raise GATE_FLOOR in scripts/python-test.sh to the whole percent at or below"
  echo "      ${total}, so the gain cannot be given back without a diff that lowers it."
  failed=1
fi

if [ "${failed}" -ne 0 ]; then
  exit 1
fi
echo
echo "Every installer helper is covered at every line and branch, and the gates hold"
echo "their floor at ${total}% against ${GATE_FLOOR}%."
