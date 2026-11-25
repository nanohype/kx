"""Every gate's own self-test, run in this process so coverage measures the gate.

A gate proves it can still reject before it reports: its main() runs a
self-test first, against fixtures it builds. Those self-tests are the gates'
test suite, and running them here measures how much of each gate they reach,
which scripts/python-test.sh holds to a floor that only moves up.

The self-test is the entry point here, not main(). main() goes on to read the
real tree or a render, and coverage measured over those would move whenever the
tree did rather than when a gate or its self-test changed.
"""

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# The function each gate runs to prove itself, which returns zero when every case
# behaved as specified. check-slice-integrity and check-images call theirs
# controls(); the rest call theirs self_test().
SELF_TESTS = {
    "check-chart-deprecation.py": "self_test",
    "check-images.py": "controls",
    "check-operator-flags.py": "self_test",
    "check-prose-voice.py": "self_test",
    "check-rendered-mounts.py": "self_test",
    "check-rendered-schemas.py": "self_test",
    "check-renovate-coverage.py": "self_test",
    "check-slice-integrity.py": "controls",
    "mirror-check.py": "self_test",
}


# Scripts with no self-test of their own, each with the module here that tests
# it. vendor-schemas.py writes schemas/ rather than judging the tree, so it has
# no verdict to prove before reporting one; its tests serve it a release.
TESTED_HERE = {
    "vendor-schemas.py": "test_vendor_schemas.py",
}


def test_every_gate_is_run_here():
    gates = {path.name for path in (ROOT / "scripts").glob("*.py")}
    reached = set(SELF_TESTS) | set(TESTED_HERE)
    assert gates - reached == set(), (
        "a script under scripts/ is neither self-tested nor tested here, so nothing measures "
        "it; add it to SELF_TESTS with the function that proves it, or to TESTED_HERE with "
        "its tests")
    assert reached - gates == set(), "a table here names a script that is not under scripts/"
    assert set(SELF_TESTS) & set(TESTED_HERE) == set(), "a script is listed in both tables"
    here = pathlib.Path(__file__).parent
    assert [m for m in TESTED_HERE.values() if not (here / m).is_file()] == []


@pytest.mark.parametrize("gate", sorted(SELF_TESTS))
def test_the_gate_proves_itself(load_script, gate):
    module = load_script(f"scripts/{gate}")
    assert getattr(module, SELF_TESTS[gate])() == 0
