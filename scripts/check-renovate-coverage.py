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
import tempfile

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

# What makes a script pin-bearing. Classified by what the script installs, not
# by whether it contains the string this gate looks for: filtering on
# `--version` would derive the verdict from the set that survived the filter,
# so a script pinning in any other shape drops out of the population and is
# reported as neither covered nor unmatched.
#
# `helm repo add` and `oci://` are the two chart shapes. `releases/download/` is
# the third: gateway-api-crds applies a release manifest by URL, which is a real
# pin carrying none of the chart markers.
PINNED = re.compile(r"helm repo add|oci://|releases/download/")

# What a version looks like in a script claimed to carry none. Deliberately
# wider than PINNED — its job is to catch a pin PINNED failed to recognise.
VERSION_SHAPED = re.compile(
    r"^.*(--version\s+\S|[A-Z][A-Z0-9_]*_VERSION=|releases/download/v[0-9]).*$",
    re.M,
)


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")
    sys.exit(1)


def compile_patterns(cfg) -> list:
    """The shipped matchStrings, RE2-checked and translated to run under Python."""
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
    return patterns


def coverage(patterns, scripts, root=None, quiet=False) -> int:
    """0 when every pin in `scripts` is watched; 1 with a report when one is not."""
    root = root or ROOT
    say = (lambda *a, **k: None) if quiet else print
    if not scripts:
        say("FAIL  found no install.sh under stack/*/*/ — refusing to report coverage "
            "over an empty set.")
        return 1

    pins, no_chart = [], []
    for p in scripts:
        (pins if PINNED.search(pathlib.Path(p).read_text()) else no_chart).append(p)
    if not pins:
        say("FAIL  no install.sh pins a version — refusing to report full coverage "
            "over an empty set.")
        return 1

    # The excluded set, checked rather than described. Saying "these carry no
    # version to watch" is a measurement that was true when written; a script
    # growing a pin in a shape PINNED does not know lands here and the sentence
    # becomes a confident false statement printed by the gate whose whole
    # purpose is proving no pin is unwatched. So the claim is asserted.
    unwatched = [
        (str(pathlib.Path(p).relative_to(root)), hit.group(0).strip())
        for p in no_chart
        for hit in [VERSION_SHAPED.search(pathlib.Path(p).read_text())]
        if hit
    ]
    if unwatched:
        say(f"FAIL  {len(unwatched)} install.sh excluded as carrying no version, but one is "
              f"present:")
        for rel, evidence in unwatched:
            say(f"        {rel}: {evidence}")
        say("\n      Either PINNED does not recognise the shape this script pins in, or the\n"
              "      script really carries no pin and VERSION_SHAPED is too broad. Teach the\n"
              "      one that is wrong — do not widen the exclusion.")
        return 1

    unmatched = []
    covered = []
    for p in pins:
        src = pathlib.Path(p).read_text()
        rel = str(pathlib.Path(p).relative_to(root))

        # Every pin in the file, not the first one. `search` returns one match, so
        # a file pinning two charts would have its second pin unwatched AND invisible,
        # in the gate whose entire purpose is proving no pin is unwatched. The
        # count below is derived per pin rather than per file, so that holds
        # however many charts one script pins.
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
        say(f"FAIL  {len(unmatched)} chart pin(s) are not watched by any customManager:")
        for u in unmatched:
            say(f"        {u}")
        say("\n      A pin no manager matches is a version nothing will ever tell you "
              "about.\n      Extend the matchStrings in renovate.json, or explain the "
              "exemption here.")
        return 1

    say(f"OK    {len(covered)} chart pin(s) in stack/*/*/install.sh, all matched by a "
          f"Renovate customManager.")
    if no_chart:
        say(f"      {len(no_chart)} install.sh carry no version, asserted rather than assumed:")
        for p in no_chart:
            say(f"        {pathlib.Path(p).relative_to(root)}")
    for dep, ver in sorted(covered):
        say(f"        {dep:<40} {ver}")
    return 0


def self_test() -> int:
    """Prove the gate still rejects, over the shapes the tree actually contains.

    Every case runs the shipped matchStrings against a synthetic tree rather
    than a copy of them, so editing renovate.json into something that matches
    nothing fails here. The control at the end runs the real tree: a suite whose
    breaks are all caught but whose subject no longer passes proves nothing.
    """
    patterns = compile_patterns(json.loads((ROOT / "renovate.json").read_text()))
    failures = []

    def tree(files):
        d = pathlib.Path(tempfile.mkdtemp(prefix="kx-renovate-"))
        for rel, body in files.items():
            f = d / "stack" / rel / "install.sh"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
        return d, sorted(str(x) for x in d.glob("stack/*/*/install.sh"))

    WATCHED = ('helm repo add x https://x.example\n'
               'helm upgrade --install a x/a --version 1.2.3\n')

    breaks = [
        ("a chart pin in a shape no manager matches",
         {"s/a": 'helm repo add x https://x.example\nhelm upgrade --install a x/a --ver 1.2.3\n'}),
        ("a second pin in a file whose first pin matches",
         {"s/a": WATCHED + 'helm upgrade --install b x/b --version 9.9.9\n'}),
        ("a version in a script classified as carrying none",
         {"s/a": WATCHED, "s/b": 'FOO_VERSION="v1.2.3"\nkubectl apply -f ./local.yaml\n'}),
        ("no install.sh at all", {}),
        ("install.sh present but none pinning anything",
         {"s/a": 'kubectl apply -f ./local.yaml\n'}),
    ]
    for label, files in breaks:
        root, scripts = tree(files)
        if coverage(patterns, scripts, root=root, quiet=True) == 0:
            failures.append(label)
            print(f"  ACCEPTED  {label}   <-- not caught")
        else:
            print(f"  rejected  {label}")

    # The release-URL shape, which is why the third manager exists. Asserted as
    # a positive: a break-only suite passes just as happily when every pattern
    # matches nothing.
    root, scripts = tree({"s/a": WATCHED, "s/b":
        'GW_VERSION="v1.5.1"\nkubectl apply -f '
        '"https://github.com/kubernetes-sigs/gateway-api/releases/download/${GW_VERSION}/x.yaml"\n'})
    if coverage(patterns, scripts, root=root, quiet=True) != 0:
        failures.append("a release-URL pin is watched")
        print("  ACCEPTED  (positive) a release-URL pin is reported unwatched")
    else:
        print("  matched   (positive) a version applied by release URL")

    scripts = sorted(glob.glob(str(ROOT / "stack/*/*/install.sh")))
    if coverage(patterns, scripts, quiet=True) != 0:
        failures.append("the shipped tree does not pass")
        print("  ACCEPTED  (control) the shipped tree is rejected")
    else:
        print("  passed    (control) the shipped tree")

    if failures:
        print(f"\nFAIL  {len(failures)} case(s) wrong.")
        return 1
    print(f"\nOK    {len(breaks) + 2} case(s) behave as specified.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    patterns = compile_patterns(json.loads((ROOT / "renovate.json").read_text()))
    return coverage(patterns, sorted(glob.glob(str(ROOT / "stack/*/*/install.sh"))))


if __name__ == "__main__":
    sys.exit(main())
