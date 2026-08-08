#!/usr/bin/env python3
"""Every chart pin in the stack is watched by Renovate.

Renovate ships no manager that reads a version out of a shell script, and this
repo keeps every pin in `install.sh` by design. The gap that closes is a
customManager in renovate.json — and a customManager whose regex matches nothing
is valid config that watches nothing. `renovate-config-validator` passes it
happily, because the schema is fine and the pattern is never run against a file.

So this reads the regexes out of renovate.json — the shipped ones, not a copy —
applies them to the tree, and fails if any pin is unmatched. Edit the regex and
break coverage, and this says so.

It also rejects regex constructs Renovate cannot run. Renovate uses RE2, which
has no backreferences and no lookaround; Python's `re` has both. A pattern using
them would pass this check and silently match nothing in production.
"""

from __future__ import annotations

import glob
import json
import pathlib
import re
import sys

# Valid in Python's re, absent from RE2. A pattern using one of these would pass
# locally and match nothing when Renovate runs it.
RE2_UNSUPPORTED = [
    (r"\(\?=", "lookahead (?=...)"),
    (r"\(\?!", "negative lookahead (?!...)"),
    (r"\(\?<=", "lookbehind (?<=...)"),
    (r"\(\?<!", "negative lookbehind (?<!...)"),
    (r"\\[1-9]", "backreference"),
    (r"\\k<", "named backreference"),
]

ROOT = pathlib.Path(__file__).resolve().parent.parent


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    sys.exit(1)


def main() -> int:
    cfg = json.loads((ROOT / "renovate.json").read_text())
    managers = cfg.get("customManagers") or []
    if not managers:
        fail("renovate.json declares no customManagers, so no chart pin in "
             "stack/*/*/install.sh is watched by anything.")

    patterns = []
    for m in managers:
        for s in m.get("matchStrings") or []:
            for probe, label in RE2_UNSUPPORTED:
                if re.search(probe, s):
                    fail(f"matchString uses {label}, which RE2 does not support. "
                         f"Renovate would match nothing:\n      {s}")
            # RE2 and Python spell named groups differently: `(?<name>)` vs
            # `(?P<name>)`. renovate.json necessarily carries the RE2 form, so it
            # is translated here to run under Python.
            #
            # Safe only because the lookbehind forms `(?<=` and `(?<!` were
            # rejected above — after that check, a remaining `(?<` can only be a
            # named group. Reorder these two and the translation corrupts a
            # lookbehind into a group named `=`.
            try:
                patterns.append(re.compile(re.sub(r"\(\?<(?=[A-Za-z_])", "(?P<", s)))
            except re.error as e:
                fail(f"matchString does not compile: {e}\n      {s}")

    pins = [
        p for p in sorted(glob.glob(str(ROOT / "stack/*/*/install.sh")))
        if "--version" in pathlib.Path(p).read_text()
    ]
    if not pins:
        fail("found no install.sh carrying a --version pin — refusing to report "
             "full coverage over an empty set.")

    unmatched = []
    covered = []
    for p in pins:
        src = pathlib.Path(p).read_text()
        hit = next((m for m in (pat.search(src) for pat in patterns) if m), None)
        if hit is None:
            unmatched.append(str(pathlib.Path(p).relative_to(ROOT)))
            continue
        g = hit.groupdict()
        if not g.get("depName") or not g.get("currentValue"):
            unmatched.append(f"{pathlib.Path(p).relative_to(ROOT)} (matched, empty capture)")
            continue
        covered.append((g["depName"], g["currentValue"]))

    if unmatched:
        print(f"FAIL  {len(unmatched)} chart pin(s) are not watched by any customManager:")
        for u in unmatched:
            print(f"        {u}")
        print("\n      A pin no manager matches is a version nothing will ever tell you "
              "about.\n      Extend the matchStrings in renovate.json, or explain the "
              "exemption here.")
        return 1

    print(f"OK    {len(covered)} chart pin(s) in stack/*/*/install.sh, all matched by a "
          f"Renovate customManager.")
    for dep, ver in sorted(covered):
        print(f"        {dep:<40} {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
