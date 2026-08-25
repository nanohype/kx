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

import contextlib
import os
import glob
import io
import subprocess
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

# The tree this gate reads. Overridable so the suite-wide floor can point the
# gate at a fixture it wrote and observe the real exit status, rather than
# asking the gate to describe its own behaviour.
ROOT = pathlib.Path(
    os.environ.get("KX_GATE_ROOT", "") or pathlib.Path(__file__).resolve().parent.parent
)

# The repository this gate ships in, which is not necessarily the tree it checks.
# KX_GATE_ROOT points ROOT at a corpus; the CONFIG under test is always this
# repository's own renovate.json, because the controls prove the gate's logic
# against the shipped rules rather than against whatever a fixture carries.
SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent


def strip_comments(text: str) -> str:
    """Source with `#` comment bodies blanked, quote-aware.

    A `#` opens a comment only at the start of a word and only outside quotes,
    so a URL fragment and a quoted hash both survive.

    Which view a check reads is a per-check decision, not a blanket rule:

      raw       when the thing being looked for IS an annotation. Renovate reads
                whole files, so a question of the form "would Renovate match
                this?" has to be asked of the text Renovate actually sees.
      stripped  when a comment must not be able to satisfy a code reference — a
                pin, a call, a filename something applies.

    Reading one view for both purposes is the defect. A gate that strips
    everywhere goes blind to annotations, which are comments by construction;
    a gate that strips nowhere lets a comment stand in for an implementation.
    """
    out = []
    for line in text.splitlines():
        # An unbalanced quote must not swallow the rest of the line. A word like
        # dont'care opens a quote that never closes, and every `#` after it then
        # reads as quoted — so a comment survives blanking and whatever it
        # mentions counts as code. Scanned twice: if a quote is still open at
        # end of line it was an apostrophe inside a word, so the second pass
        # treats that character as literal.
        buf = []
        for literal_apostrophe in (False, True):
            buf, quote, i = [], None, 0
            while i < len(line):
                c = line[i]
                if quote:
                    buf.append(c)
                    if c == "\\" and quote == '"' and i + 1 < len(line):
                        buf.append(line[i + 1])
                        i += 2
                        continue
                    if c == quote:
                        quote = None
                elif c == "'" and literal_apostrophe:
                    buf.append(c)
                elif c in "\"'":
                    quote = c
                    buf.append(c)
                elif c == "#" and (not buf or buf[-1].isspace()):
                    buf.append(" " * (len(line) - i))
                    quote = None
                    break
                else:
                    buf.append(c)
                i += 1
            if quote is None:
                break
        out.append("".join(buf))
    return "\n".join(out)

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
    # [ \t]+ rather than \s+, and this matters against a comment-blanked view:
    # a blanked comment line is all spaces, so \s would cross the newline and
    # swallow the blanked line above or below the match — reporting a line that
    # is not the one the pin is on. Nothing about that failure announces itself.
    r"^.*(--version[ \t]+\S|[A-Z][A-Z0-9_]*_VERSION=|releases/download/v[0-9]).*$",
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


def manager_scopes(cfg) -> list:
    """One (scope, patterns) pair per manager, scope compiled from its own config."""
    out = []
    for m in cfg.get("customManagers") or []:
        scopes = []
        for raw in m.get("managerFilePatterns") or []:
            # Renovate spells a regex file pattern as /re/; anything else is a
            # glob, which this repo does not use.
            if not (raw.startswith("/") and raw.endswith("/")):
                fail(f"managerFilePatterns entry {raw!r} is not a /regex/ — this gate reads "
                     f"only the regex form that renovate.json uses.")
            scopes.append(re.compile(raw[1:-1]))
        out.append((scopes, compile_patterns({"customManagers": [m]})))
    return out


# The ways an install.sh carries a version, counted independently of the
# customManager patterns so a regex that stops matching cannot also revise the
# target it is measured against.
PIN_MARKERS = ("--version", "releases/download/")


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
        (pins if PINNED.search(strip_comments(pathlib.Path(p).read_text())) else no_chart).append(p)
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
        for hit in [VERSION_SHAPED.search(strip_comments(pathlib.Path(p).read_text()))]
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
        src = strip_comments(pathlib.Path(p).read_text())
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
        # revise the target it is measured against. Counted per pin rather than
        # per file: one watched pin must not vouch for an unwatched pin beside
        # it. `src` is already comment-stripped, so a pin discussed in prose is
        # not a pin.
        # At least one, because this script installs something versioned and that
        # is what put it in this set.
        #
        # Every shape that carries a version, not just the helm flag. A file
        # whose pins are release-URL shaped carries no `--version` at all, so a
        # count taken from that marker alone collapses to the constant 1 and one
        # matched pin then vouches for every unmatched pin beside it — the floor
        # measuring a different quantity than the check examines.
        expected = max(1, sum(
            1 for line in src.splitlines()
            if any(marker in line for marker in PIN_MARKERS)
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
    patterns = compile_patterns(json.loads((SOURCE_ROOT / "renovate.json").read_text()))
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

    # Deleting the config must fail the gate, not empty it. compile_patterns
    # refuses a config with no customManagers — without that, an empty pattern
    # list matches nothing, every file reports zero pins found against zero
    # expected, and the gate passes over a tree it never looked at.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys;"
         "s=importlib.util.spec_from_file_location('rc', sys.argv[1]);"
         "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
         "m.compile_patterns({'customManagers': []})",
         str(pathlib.Path(__file__).resolve())],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0:
        failures.append("a config with no customManagers is accepted")
        print("  ACCEPTED  (positive) a renovate.json with every manager deleted")
    else:
        print("  rejected  (positive) a renovate.json with every manager deleted")

    # A version that appears only in a comment is not a pin, so the file that
    # carries it is correctly reported as carrying none. The opposite reading —
    # treating prose as a pin — makes the exclusion assertion fire on a file
    # that has nothing to watch.
    root, scripts = tree({"s/a": WATCHED,
                          "s/b": '# --version 9.9.9 was the old pin\nkubectl apply -f ./x.yaml\n'})
    if coverage(patterns, scripts, root=root, quiet=True) != 0:
        failures.append("a version in a comment is not a pin")
        print("  ACCEPTED  (positive) a version in a comment is treated as an unwatched pin")
    else:
        print("  spared    (positive) a version that appears only in a comment")

    # A file whose only registry marker is in prose is not pin-bearing. Asserted
    # as a positive, because the failure this guards is a false alarm rather
    # than a miss, and a gate that cries wolf is one people learn to skip.
    root, scripts = tree({"s/a": WATCHED,
                          "s/b": '# this used to `helm repo add` but installs from a path now\n'
                                 'helm upgrade --install b ./chart\n'})
    if coverage(patterns, scripts, root=root, quiet=True) != 0:
        failures.append("a registry marker in a comment is not a pin")
        print("  ACCEPTED  (positive) a `helm repo add` in a comment is treated as a pin")
    else:
        print("  spared    (positive) a registry marker that appears only in a comment")

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

    # SOURCE_ROOT, not ROOT. This control's subject is the tree this gate SHIPS
    # in; ROOT may be pointed at a fixture by the suite-wide floor, and asserting
    # "the shipped tree passes" about a fixture makes the gate exit non-zero for
    # a reason that has nothing to do with the corpus under test — which the
    # floor would then score as the gate catching what it planted.
    scripts = sorted(glob.glob(str(SOURCE_ROOT / "stack/*/*/install.sh")))
    if coverage(patterns, scripts, root=SOURCE_ROOT, quiet=True) != 0:
        failures.append("the shipped tree does not pass")
        print("  ACCEPTED  (control) the shipped tree is rejected")
    else:
        print("  passed    (control) the shipped tree")

    if failures:
        print(f"\nFAIL  {len(failures)} case(s) wrong.")
        return 1
    print(f"\nOK    {len(breaks) + 5} case(s) behave as specified.")
    return 0


# The keys a regex customManager must carry, and the ones Renovate knows about.
# A typo in a required key is silent on both sides: Renovate ignores the manager
# and this gate sees one fewer pattern, so a pin reads as covered by a rule that
# does not exist.
REQUIRED_MANAGER_KEYS = {"customType", "managerFilePatterns", "matchStrings"}
KNOWN_MANAGER_KEYS = REQUIRED_MANAGER_KEYS | {
    "description", "datasourceTemplate", "depNameTemplate", "versioningTemplate",
    "registryUrlTemplate", "currentValueTemplate", "packageNameTemplate",
    "extractVersionTemplate", "autoReplaceStringTemplate", "matchStringsStrategy",
}


def config_is_well_formed(cfg) -> int:
    """Every customManager declares the keys Renovate needs to run it.

    This gate READS renovate.json and applies its regexes, so a malformed
    manager makes the gate assert coverage that Renovate would never provide.
    Nothing else in this repository has ever checked that file is well formed.

    PARTIAL by construction: this is a shape check, not schema validation. It
    cannot tell that a datasourceTemplate names a datasource that does not
    exist, or that a template field references a capture group no matchString
    produces — that needs renovate-config-validator, which is a node tool this
    repository does not otherwise depend on.
    """
    problems = []
    managers = cfg.get("customManagers") or []
    if not managers:
        print("FAIL  renovate.json declares no customManagers.")
        return 1
    for i, mgr in enumerate(managers):
        missing = sorted(REQUIRED_MANAGER_KEYS - set(mgr))
        if missing:
            problems.append(f"customManagers[{i}] is missing {', '.join(missing)} — Renovate "
                            f"ignores a manager it cannot run, so whatever it appears to watch "
                            f"is unwatched.")
        unknown = sorted(set(mgr) - KNOWN_MANAGER_KEYS)
        if unknown:
            problems.append(f"customManagers[{i}] carries unrecognised key(s) "
                            f"{', '.join(unknown)} — most likely a typo for a real one, which "
                            f"Renovate drops silently.")
        for key in ("matchStrings", "managerFilePatterns"):
            if key in mgr and not mgr[key]:
                problems.append(f"customManagers[{i}].{key} is empty.")
    if problems:
        print(f"FAIL  renovate.json is not well formed — {len(problems)} problem(s):")
        for pr in problems:
            print(f"        {pr}")
        return 1
    return 0


def no_dead_managers(cfg, root: pathlib.Path = ROOT) -> int:
    """Every manager matches something somewhere in the real tree.

    Coverage says every pin is watched; it does not say every manager watches
    something. A rule matching nothing costs nothing to keep, so nobody removes
    it, and it reads to the next author as a rule already covering the shape
    they are about to add. Asked of the real corpus only — a synthetic fixture
    exercises one shape by design, so the same question there means nothing.
    """
    tracked = [
        str(f.relative_to(root))
        for f in root.rglob("*")
        if f.is_file() and ".git" not in f.parts
    ]
    dead = []
    for i, (scopes, patterns) in enumerate(manager_scopes(cfg)):
        in_scope = [f for f in tracked if any(s.search(f) for s in scopes)]
        if not in_scope:
            dead.append(f"customManagers[{i}] applies to no file in the tree. Its "
                        f"managerFilePatterns match nothing.")
            continue
        # Raw: this asks whether RENOVATE would match, and Renovate reads whole
        # files. A manager matching only an annotated line is alive.
        texts = [(root / f).read_text(errors="ignore") for f in in_scope]
        for j, pat in enumerate(patterns):
            if not any(pat.search(x) for x in texts):
                dead.append(
                    f"customManagers[{i}].matchStrings[{j}] matches nothing in the "
                    f"{len(in_scope)} file(s) it applies to:\n        {pat.pattern}"
                )
    if dead:
        print(f"FAIL  {len(dead)} customManager pattern(s) match nothing:")
        for d in dead:
            print(f"        {d}")
        print("\n      A rule matching nothing watches nothing. Either the shape it was "
              "written for\n      has left the tree — remove it — or it never matched and "
              "the coverage it\n      appears to provide has never existed.")
        return 1
    return 0


def control_outcomes() -> dict:
    """What the controls actually exercised, for the suite-wide floor.

    Counted by running them and reading the outcome, never by matching source
    text. A floor that decides whether a gate has controls by looking for the
    word "control" is satisfied by a comment saying the controls were removed —
    which is the same defect one level up from the one the controls exist for.

    Both halves matter. A gate that rejects everything is as useless as one that
    rejects nothing, and either count alone passes a one-sided check.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = self_test()
    lines = buf.getvalue().splitlines()
    return {
        "ok": rc == 0,
        # The evidence, not just a tally of it. The floor derives the counts
        # from these lines rather than trusting a number a gate could return
        # without having run anything.
        "lines": lines,
        "rejected": sum(1 for line in lines if any(m in line for m in ('rejected  ',))),
        "accepted": sum(1 for line in lines if any(m in line for m in ('spared    ', 'matched   ', 'passed    '))),
    }


def main() -> int:
    # Always, before reporting on the tree. A gate that has not just proven it
    # rejects is a gate being trusted rather than checked, and a proof behind a
    # flag is a proof a workflow can forget to ask for.
    if self_test() != 0:
        print("\nRefusing to report coverage with a gate that has not proven it rejects.")
        return 1
    print()
    patterns = compile_patterns(json.loads((ROOT / "renovate.json").read_text()))
    cfg = json.loads((ROOT / "renovate.json").read_text())
    corpus = sorted(glob.glob(str(ROOT / "stack/*/*/install.sh")))
    return config_is_well_formed(cfg) or no_dead_managers(cfg) or coverage(patterns, corpus)


if __name__ == "__main__":
    sys.exit(main())
