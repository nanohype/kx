"""tests/coverage_floors.py: the floors a run is held to, each read in full.

The checker is the last thing between the suite and a green job, so it gets
the scrutiny a gate does: each floor is shown to fail the run when it is not
met, and the population rules that keep a file from going unfloored are shown
to fire.
"""

import tomllib

import pytest

CHECKER = "tests/coverage_floors.py"

FLOORS = {
    "security-path": {"include": ["stack/*"], "per-file": True, "lines": 100, "branches": 100},
    "gates": {"include": ["scripts/*"], "per-file": False, "lines": 75, "branches": 60},
}


@pytest.fixture
def floors(load_script):
    return load_script(CHECKER)


def measured(lines=(10, 10), branches=(4, 4)) -> dict:
    """One file's summary as coverage.py's JSON report writes it."""
    return {"summary": {"covered_lines": lines[0], "num_statements": lines[1],
                        "covered_branches": branches[0], "num_branches": branches[1]}}


FULL = {"stack/a/helper.py": measured(), "scripts/gate.py": measured()}


class TestVerdict:
    def test_every_floor_held(self, floors):
        report, problems = floors.verdict(FULL, FLOORS)
        assert problems == []
        assert report == [
            "  ok    gates, 1 file(s) together: lines 100.0% of 75, branches 100.0% of 60",
            "  ok    stack/a/helper.py: lines 100.0% of 100, branches 100.0% of 100",
        ]

    def test_a_security_path_file_short_of_one_line(self, floors):
        files = {**FULL, "stack/a/helper.py": measured(lines=(9, 10))}
        _, problems = floors.verdict(files, FLOORS)
        assert problems == ["stack/a/helper.py is at lines 90.0% and branches 100.0%, under the "
                            "security-path floor of 100% and 100%."]

    def test_a_per_file_floor_is_not_met_by_an_average(self, floors):
        files = {**FULL, "stack/a/helper.py": measured(lines=(50, 100)),
                 "stack/b/other.py": measured(lines=(150, 150))}
        _, problems = floors.verdict(files, FLOORS)
        assert [p.split(" is at")[0] for p in problems] == ["stack/a/helper.py"]

    def test_the_gates_are_held_together(self, floors):
        files = {**FULL, "scripts/gate.py": measured(lines=(5, 10)),
                 "scripts/other.py": measured(lines=(10, 10))}
        _, problems = floors.verdict(files, FLOORS)
        assert problems == []

    def test_lines_and_branches_are_read_apart(self, floors):
        files = {**FULL, "scripts/gate.py": measured(lines=(10, 10), branches=(2, 4))}
        _, problems = floors.verdict(files, FLOORS)
        assert problems == ["gates, 1 file(s) together is at lines 100.0% and branches 50.0%, "
                            "under the gates floor of 75% and 60%."]

    def test_branches_alone_short_fail_the_floor_too(self, floors):
        files = {**FULL, "scripts/gate.py": measured(lines=(100, 100), branches=(59, 100))}
        _, problems = floors.verdict(files, FLOORS)
        assert len(problems) == 1
        assert "branches 59.0%" in problems[0]

    def test_a_file_no_floor_includes(self, floors):
        files = {**FULL, "tools/loose.py": measured()}
        _, problems = floors.verdict(files, FLOORS)
        assert problems == ["tools/loose.py is measured, but no floor includes it, so nothing "
                            "holds it."]

    def test_a_file_two_floors_include(self, floors):
        overlapping = {**FLOORS,
                       "everything": {"include": ["*"], "per-file": False, "lines": 0,
                                      "branches": 0}}
        _, problems = floors.verdict(FULL, overlapping)
        assert "scripts/gate.py falls under the everything and gates floors; a file is held to " \
               "one." in problems
        assert "stack/a/helper.py falls under the everything and security-path floors; a file " \
               "is held to one." in problems

    def test_a_floor_that_includes_nothing(self, floors):
        _, problems = floors.verdict({"scripts/gate.py": measured()}, FLOORS)
        assert problems == ["the security-path floor includes no measured file, so it holds "
                            "nothing."]

    def test_a_file_with_no_branches_meets_a_branch_floor(self, floors):
        files = {**FULL, "stack/a/helper.py": measured(branches=(0, 0))}
        assert floors.verdict(files, FLOORS)[1] == []

    def test_nothing_to_cover_is_all_covered(self, floors):
        assert floors.percent(0, 0) == 100.0
        assert floors.percent(3, 4) == 75.0


class TestUnusable:
    def test_the_shipped_floors_are_usable(self, floors):
        shipped = floors.floors_in(tomllib.loads(floors.PYPROJECT.read_text()))
        assert floors.unusable(shipped) == []

    def test_the_shipped_floors_are_the_ones_decided(self, floors):
        shipped = floors.floors_in(tomllib.loads(floors.PYPROJECT.read_text()))
        assert {name: (f["per-file"], f["lines"], f["branches"])
                for name, f in shipped.items()} == {
            "security-path": (True, 100, 100),
            "gates": (False, 75, 60),
        }

    def test_no_floors_at_all(self, floors):
        assert floors.floors_in({}) == {}
        assert floors.unusable({}) == [
            "pyproject.toml states no [tool.kx.coverage-floors] tables, so there is no floor "
            "to hold the run to."]

    @pytest.mark.parametrize("change,says", [
        ({"include": []}, "names no files to include"),
        ({"include": "stack/*"}, "names no files to include"),
        ({"include": [""]}, "names no files to include"),
        ({"per-file": "yes"}, "does not say whether it is per-file"),
        ({"lines": True}, "lines is True, not a percentage"),
        ({"lines": "75"}, "lines is '75', not a percentage"),
        ({"branches": -1}, "branches is -1, not a percentage"),
        ({"branches": 101}, "branches is 101, not a percentage"),
    ], ids=["no-globs", "globs-not-a-list", "empty-glob", "per-file-not-bool", "lines-bool",
            "lines-text", "branches-negative", "branches-over-100"])
    def test_a_floor_that_cannot_be_held_to(self, floors, change, says):
        bad = {"gates": {**FLOORS["gates"], **change}}
        problems = floors.unusable(bad)
        assert len(problems) == 1
        assert says in problems[0]


def test_no_data_is_a_run_that_cannot_be_read(floors, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(floors, "ROOT", tmp_path)
    assert floors.main() == 2
    assert "there is no coverage data to read" in capsys.readouterr().out
