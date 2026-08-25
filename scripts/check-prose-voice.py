#!/usr/bin/env python3
"""Surface the prose a change ADDS, for the author to re-read against the rules.

    scripts/check-prose-voice.py <base-ref>   # advisory, diff-scoped
    scripts/check-prose-voice.py --self-test

Advisory by construction: it exits 0 whatever it finds, and it is not a merge
gate. That is the shape the documentation-voice standard asks for, and the
reason is in the standard: conformance is read, not matched. A pattern finds the
phrasing its author already thought of, and the same violation arrives in as
many wordings as there are authors — so a gate asserting prose conformance would
claim a coverage it does not have, which is the failure this repository is built
to avoid.

What it does instead is put the added prose and the rules in front of a reader
at the moment the two readings of a sentence still coincide, which is when the
author cannot tell them apart and a second reader can.

The four markers it does flag are the subset the standard calls enforceable:
verification tallies, prose addressed to an agent, bare TODO/FIXME, and internal
issue or PR references. The rest of the output is the added prose itself, listed
without a verdict.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Prose surfaces. A change to a values file or a Taskfile carries prose as
# surely as a markdown file does, which is the scope the standard names.
PROSE_LINE = re.compile(r"^\+\s*(#|//|--\s)|^\+\s*[A-Za-z(\"'`]|^\+\s*desc:|^\+\s*description:")
CODE_ONLY = re.compile(r"^\+\s*[a-zA-Z_.\"'-]+\s*[:=]\s*\S+\s*$")

SELF = "scripts/check-prose-voice.py"

MARKERS = [
    (
        # Spelled-out numbers as well as digits: the standard's own example of a
        # tally that survived two readings is "five silent drops alongside eight
        # warnf calls", which carries no digit at all.
        re.compile(
            r"\b(all|both|each of|every one of)\s+\d+\b"
            # Up to two words between the count and the noun: the tally is
            # "five SILENT drops", and requiring adjacency misses every tally
            # whose author reached for an adjective.
            # `\d+` only for the small spelled-out words' noun list: "two files"
            # names a convention, "34 files" reports a measurement, and the
            # difference is the digit.
            r"|\b\d+\s+"
            r"(?:[a-z-]+\s+){0,2}"
            r"(findings?|hits?|files?|cases?|drops?|calls?|occurrences?|shapes?|problems?|"
            r"resources?|manifests?|slices?|pins?|violations?|jobs?|gates?|scripts?)\b"
            # Spelled-out counts only where the noun is unambiguously a finding.
            r"|\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+"
            r"(?:[a-z-]+\s+){0,2}(findings?|hits?|occurrences?|drops?|violations?)\b",
            re.I,
        ),
        "verification tally — state the requirement, or the command that answers it",
    ),
    (
        re.compile(r"\bfor (a )?future (Claude|agent|session)\b|\bkept here for\b|"
                   r"\bas discussed\b|\bwe decided\b|\bturns out\b|\bafter some digging\b"),
        "session narration / prose addressed to an agent",
    ),
    (
        re.compile(r"\b(TODO|FIXME|XXX|HACK)\b"),
        "bare marker with no owner",
    ),
    (
        # A repo-qualified issue reference. The name half must look like a repo
        # slug — lowercase, hyphenated — so that PKCS#12, RFC#7231 and other
        # ALL-CAPS standard names do not match. The standard names PKCS#12
        # specifically as a measured false positive of the naive pattern.
        re.compile(r"(?<![A-Za-z0-9])[a-z][a-z0-9._-]*#\d+\b"
                   r"|\b(PR|pull request|issue|ticket)\s+#?\d+\b"),
        "internal provenance — state the behaviour the reference described",
    ),
]

RULES = """
  timeless-scope        Does the sentence assert a change against an UNSTATED past?
  provenance-is-a-field A count, a measurement or how it was verified is not a sentence.
                        Prefer the invariant a gate already enforces.
  rationale-survives    Would this still be worth reading if the repo had always been this way?
  no-self-defense       State the constraint; the constraint is the argument. A word like
                        "deliberately" is fine when a reason follows it, and a defect when
                        the sentence stops at the assertion.
  product-voice         A value from one estate presented as the product's shape.
  named-things-resolve  Every path, flag, command and field named here must exist.
"""


def added_prose(base: str) -> list[tuple[str, str]]:
    """(file, line) for every prose-looking line the diff adds."""
    diff = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD"],
        capture_output=True, text=True, check=False,
    )
    if diff.returncode != 0:
        print(f"check-prose-voice: git diff against {base} failed:\n{diff.stderr}", file=sys.stderr)
        return []
    out, path = [], "?"
    for line in diff.stdout.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            # This file quotes the markers it looks for, so it matches itself on
            # every run. Excluding it keeps the output about the change.
            if path == SELF:
                path = None
        elif path is None:
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            if PROSE_LINE.search(line) and not CODE_ONLY.match(line):
                out.append((path, line[1:].rstrip()))
    return out


def flag(lines: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    return [
        (path, text, why)
        for path, text in lines
        for pattern, why in MARKERS
        if pattern.search(text)
    ]


def self_test() -> int:
    """Prove the markers match what they claim and spare what they should."""
    should_flag = [
        "# that holds for all 34 of them",
        "# five silent drops alongside eight warnf calls",
        "# kept here for future Claude, not surfaced to the user",
        "# TODO: wire this up",
        "# the hold recorded in eks-gitops#207 exists to prevent this",
        "# 456 findings on an unchanged tree",
    ]
    should_pass = [
        "# the endpoint has to be one the profile's account allows Bedrock in",
        "# Deliberately a one-element list — the SCP denies everything else",
        "# requires the provider at 5.2 or later",
        "# diverged from abc1234, but nothing that changes this schema",
        "# PKCS#12 bundles are not supported by this chart",
        "# a series that stops updating means the objective is not being met",
    ]
    failures = 0
    for text in should_flag:
        if not flag([("x", text)]):
            print(f"  MISSED    {text}")
            failures += 1
        else:
            print(f"  flagged   {text}")
    for text in should_pass:
        hits = flag([("x", text)])
        if hits:
            print(f"  FALSE +   {text}  ({hits[0][2]})")
            failures += 1
        else:
            print(f"  spared    {text}")
    if failures:
        print(f"\nFAIL  {failures} case(s) wrong.")
        return 1
    print(f"\nOK    {len(should_flag) + len(should_pass)} case(s) behave as specified.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    lines = added_prose(base)
    if not lines:
        print(f"check-prose-voice: this change adds no prose against {base}.")
        return 0

    print(f"check-prose-voice: {len(lines)} prose line(s) added against {base}.")
    print("Advisory — nothing here fails the build. Re-read them against:")
    print(RULES)

    flagged = flag(lines)
    if flagged:
        print("  Lines matching an enforceable marker. A hit is a prompt to look,")
        print("  not a verdict — these patterns over-match by design:\n")
        for path, text, why in flagged:
            print(f"    {path}\n      {text.strip()}\n      -> {why}\n")
    else:
        print("  No line matched an enforceable marker. That is not a pass:")
        print("  the worst instances contain none of the marker words.\n")

    print("  Every prose line this change adds:\n")
    for path, text in lines:
        print(f"    {path}: {text.strip()[:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
