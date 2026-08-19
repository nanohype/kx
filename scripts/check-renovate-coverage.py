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

    # Classify by what a script installs, not by whether it happens to contain the
    # string this gate looks for. Filtering on `--version` derives the verdict from
    # the set that survived the filter: a script pinning a remote chart in a form
    # without that literal drops out of the population and is reported as neither
    # covered nor unmatched.
    #
    # `helm repo add` or `oci://` means the chart comes from a registry and its
    # version is a pin something must watch. A script with neither installs from a
    # local path or installs no chart, so there is no version to track — those are
    # excluded by name below rather than silently.
    remote = re.compile(r"helm repo add|oci://")
    scripts = sorted(glob.glob(str(ROOT / "stack/*/*/install.sh")))
    if not scripts:
        fail("found no install.sh under stack/*/*/ — refusing to report coverage "
             "over an empty set.")

    pins, no_chart = [], []
    for p in scripts:
        (pins if remote.search(pathlib.Path(p).read_text()) else no_chart).append(p)
    if not pins:
        fail("no install.sh installs a chart from a registry — refusing to report "
             "full coverage over an empty set.")

    unmatched = []
    covered = []
    for p in pins:
        src = pathlib.Path(p).read_text()
        rel = str(pathlib.Path(p).relative_to(ROOT))

        # Every pin in the file, not the first one. `search` returns one match, so
        # a file pinning two charts had its second pin unwatched AND invisible —
        # in the gate whose entire purpose is proving no pin is unwatched. Every
        # install.sh happens to pin exactly one chart today, which is what let
        # per-file stand in for per-pin; nothing enforced that and the count below
        # is what now does.
        found = {}
        for pat in patterns:
            for m in pat.finditer(src):
                g = m.groupdict()
                dep, ver = g.get("depName"), g.get("currentValue")
                if not dep or not ver:
                    unmatched.append(f"{rel} (matched, empty capture)")
                    continue
                # Keyed, because the two managers can both match one block — the
                # OCI pattern and the repo-add pattern are not disjoint by
                # construction — and a dedup by capture keeps that from reading
                # as two covered pins.
                found[(dep, ver)] = True

        # The independent count of what SHOULD have matched. Derived from the file
        # rather than from the matcher, so a regex that stops matching cannot also
        # revise the target it is measured against. Comment lines excluded: a pin
        # discussed in prose is not a pin.
        # At least one, because this script installs a chart from a registry and
        # that is what put it in this set. Counting only `--version` lines would
        # take the floor from the same marker the patterns need: a chart carrying
        # its version in an OCI ref rather than a flag counts zero, and `0 < 0`
        # passes over a pin nothing watches.
        expected = max(1, sum(
            1 for line in src.splitlines()
            if "--version" in line and not line.lstrip().startswith("#")
        ))
        if len(found) < expected:
            unmatched.append(
                f"{rel} ({expected} pin(s) present, {len(found)} matched by a customManager)"
            )
            continue
        covered.extend(found)

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
    if no_chart:
        print(f"      {len(no_chart)} install.sh install no chart from a registry and carry "
              f"no version to watch:")
        for p in no_chart:
            print(f"        {pathlib.Path(p).relative_to(ROOT)}")
    for dep, ver in sorted(covered):
        print(f"        {dep:<40} {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
