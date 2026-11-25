#!/usr/bin/env python3
"""The coverage floors pyproject.toml states, read against the last run.

    python3 -m coverage run -m pytest
    python3 tests/coverage_floors.py

Each table under [tool.kx.coverage-floors] names files by glob and the line
and branch coverage they are held to: each file on its own when `per-file` is
true, the files together when it is false. Every file coverage measured falls
under exactly one floor. A file under none is held to nothing, and a file under
two is held to whichever a reader looks at, so either fails the run.

Lines and branches are read apart. coverage.py's own total folds the two into
one number, where every line and half the branches read as a respectable figure
that is neither of the two things a floor here states.

Exits 0 when every floor holds, 1 when one does not, and 2 when there is
nothing to read it against: no coverage module, no data, or no usable floors.
"""

from __future__ import annotations

import fnmatch
import json
import os
import pathlib
import sys
import tempfile
import tomllib

try:
    import coverage
except ImportError:
    # Reported by main() as a run that cannot be read, rather than as a crash.
    coverage = None

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def floors_in(config: dict) -> dict[str, dict]:
    """The [tool.kx.coverage-floors] tables of a parsed pyproject.toml."""
    return config.get("tool", {}).get("kx", {}).get("coverage-floors") or {}


def unusable(floors: dict[str, dict]) -> list[str]:
    """Why `floors` cannot be held to, or nothing when every floor is usable."""
    if not floors:
        return ["pyproject.toml states no [tool.kx.coverage-floors] tables, so there is no "
                "floor to hold the run to."]
    problems = []
    for name, floor in sorted(floors.items()):
        include = floor.get("include")
        if not isinstance(include, list) or not include or not all(
                isinstance(g, str) and g for g in include):
            problems.append(f"the {name} floor names no files to include.")
        if not isinstance(floor.get("per-file"), bool):
            problems.append(f"the {name} floor does not say whether it is per-file.")
        for kind in ("lines", "branches"):
            value = floor.get(kind)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                problems.append(f"the {name} floor's {kind} is {value!r}, not a percentage.")
    return problems


def percent(covered: int, total: int) -> float:
    """Coverage of `total` things, `covered` of them reached. Nothing to cover is all covered."""
    return 100.0 if total == 0 else 100.0 * covered / total


def verdict(files: dict[str, dict], floors: dict[str, dict]) -> tuple[list[str], list[str]]:
    """(the lines to report, the problems) for coverage.py's JSON `files` against `floors`."""
    owners = {f: sorted(name for name, floor in floors.items()
                        if any(fnmatch.fnmatch(f, glob) for glob in floor["include"]))
              for f in files}
    problems = []
    for f, names in sorted(owners.items()):
        if not names:
            problems.append(f"{f} is measured, but no floor includes it, so nothing holds it.")
        elif len(names) > 1:
            problems.append(f"{f} falls under the {' and '.join(names)} floors; a file is held "
                            f"to one.")
    report = []
    for name, floor in sorted(floors.items()):
        members = sorted(f for f, names in owners.items() if names == [name])
        if not members:
            problems.append(f"the {name} floor includes no measured file, so it holds nothing.")
            continue
        for group in ([[f] for f in members] if floor["per-file"] else [members]):
            summaries = [files[f]["summary"] for f in group]
            lines = percent(sum(s["covered_lines"] for s in summaries),
                            sum(s["num_statements"] for s in summaries))
            branches = percent(sum(s.get("covered_branches", 0) for s in summaries),
                               sum(s.get("num_branches", 0) for s in summaries))
            label = group[0] if floor["per-file"] else f"{name}, {len(group)} file(s) together"
            held = lines >= floor["lines"] and branches >= floor["branches"]
            report.append(f"  {'ok  ' if held else 'FAIL'}  {label}: lines {lines:.1f}% of "
                          f"{floor['lines']}, branches {branches:.1f}% of {floor['branches']}")
            if not held:
                problems.append(f"{label} is at lines {lines:.1f}% and branches {branches:.1f}%, "
                                f"under the {name} floor of {floor['lines']}% and "
                                f"{floor['branches']}%.")
    return report, problems


def main() -> int:
    if coverage is None:
        print("Cannot run: this python3 has no coverage module, so there is no run to read.")
        return 2
    floors = floors_in(tomllib.loads(PYPROJECT.read_text()))
    reasons = unusable(floors)
    if reasons:
        for r in reasons:
            print(f"Cannot run: {r}")
        return 2
    # Coverage names files relative to the working directory, and the floors'
    # globs are relative to the repository root.
    os.chdir(ROOT)
    cov = coverage.Coverage(config_file=str(PYPROJECT))
    cov.load()
    with tempfile.TemporaryDirectory(prefix="kx-coverage-") as tmp:
        out = pathlib.Path(tmp) / "coverage.json"
        try:
            cov.json_report(outfile=str(out))
        except coverage.exceptions.NoDataError:
            print("Cannot run: there is no coverage data to read. Run the suite under "
                  "`coverage run -m pytest` first.")
            return 2
        files = json.loads(out.read_text())["files"]
    report, problems = verdict(files, floors)
    print("\n".join(report))
    if problems:
        print(f"\nFAIL  {len(problems)} floor problem(s):")
        for p in problems:
            print(f"        {p}")
        return 1
    print(f"\nEvery floor holds, over {len(files)} measured file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
