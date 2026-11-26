"""check-slice-integrity.py's control table, one pytest case per control.

controls() runs the whole table and reports a total. Here each control is its
own test, built from the same table, so a control that stops discriminating is
named by pytest rather than folded into a count, and can be run alone with -k.
Each case holds the control to what controls() holds it to: its two fixtures
differ, the check passes the clean one, rejects the broken one, and names what
it rejected when the case says what that is.
"""

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "check-slice-integrity.py"

# Loaded at collection, because the table is what the cases are generated from.
# No bytecode: the floor walks addon directories on disk, and a cache written
# beside a gate is a file nothing applies.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("kx_slice_integrity_controls", GATE)
integrity = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = integrity
_spec.loader.exec_module(integrity)

CASES = [(label, case) for label, cases in sorted(integrity.CONTROLS.items())
         for case in cases]


def test_the_table_is_the_one_controls_runs():
    assert len(CASES) >= integrity.MINIMUM_CONTROL_CASES
    assert {label for label, _ in CASES} <= {label for label, _ in integrity.CHECKS}


@pytest.mark.parametrize("label,case", CASES, ids=[f"{label} | {case[0]}" for label, case in CASES])
def test_the_control_rejects_its_violation_and_passes_its_clean_tree(label, case):
    check = dict(integrity.CHECKS)[label]
    _name, broken, clean = case[:3]
    must_say = case[3] if len(case) > 3 else None
    assert broken != clean, "the broken and clean fixtures are identical, so this proves nothing"
    assert check(integrity._tree(clean)) == [], "the check already fails on the clean tree"
    reported = check(integrity._tree(broken))
    assert reported, "the check accepted the violation"
    if must_say is not None:
        assert any(must_say in r for r in reported), f"no report names {must_say!r}: {reported}"
