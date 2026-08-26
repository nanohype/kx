#!/usr/bin/env python3
"""Invariants about the tree, each closing a class rather than an instance.

    scripts/check-slice-integrity.py

There is no --self-test flag. Every run executes the positive controls first and
refuses to report on the tree unless they pass, so there is no CI step to forget
and no flag a script can silently ignore. A gate that tests itself whenever it
runs cannot drift from the workflow that calls it.

  1. Every `helm upgrade --install` names an explicit `--timeout`.
  2. Every file in an addon directory is reached by that addon's install.sh or
     its slice Taskfile.

Both read source text, and both read the comment-blanked view, because both ask
whether something is REFERENCED BY CODE — a helm flag on a real invocation, a
filename something applies. The thing standing where a missing implementation
should be is usually a comment saying so, so a gate matching over comments
passes in exactly the case it exists to catch: "we no longer apply orphan.yaml"
would otherwise prove orphan.yaml is applied.

The view is a per-check decision rather than a rule for the file. A check
looking for an ANNOTATION — a lint suppression, an update-tool directive, a
coverage pragma — must read the raw text, because annotations are comments by
construction and stripping blinds the gate to its own vocabulary. Neither check
here looks for one. Comment bodies are blanked rather than dropped, so line
numbers and column offsets survive and a file:line citation stays true.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

# Overridable so the suite-wide floor can point a gate at a fixture it wrote and
# observe the real exit status instead of asking the gate about itself.
ROOT = pathlib.Path(os.environ.get("KX_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parent.parent)

HELM_INSTALL = re.compile(r"^[ \t]*helm upgrade --install\b", re.M)


def helm_invocations(text: str) -> list[tuple[int, str]]:
    """Each `helm upgrade --install` command as (1-indexed line, folded command).

    Scoped to the command rather than the file for both halves. Anchoring to
    column 0 would miss an indented invocation and, worse, skip the whole file
    on the non-match. Testing the file for `--timeout` would accept a helm call
    with none beside a `kubectl wait --timeout=300s` that has nothing to do
    with it.
    """
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        if HELM_INSTALL.match(lines[i]):
            start, cmd = i + 1, []
            while i < len(lines):
                stripped = lines[i].rstrip()
                cmd.append(stripped.rstrip("\\"))
                if not stripped.endswith("\\"):
                    break
                i += 1
            out.append((start, " ".join(cmd)))
        i += 1
    return out


def strip_comments(text: str) -> str:
    """Source with `#` comment bodies blanked, quote-aware.

    A `#` opens a comment only at the start of a word and only outside quotes.
    Naive removal would take the fragment identifier out of a URL and the `#` out
    of a quoted string, and both appear in this tree. Blanked rather than
    dropped so every line keeps its length.
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


EXAMINED: dict[str, int] = {}

# A floor on what each invariant EXAMINED, set well under the real count.
#
# Zero is the obvious vacuous run and the rare one. The common one is a corpus
# that collapsed to a handful — a glob that stopped matching, a rename that put a
# directory out of reach, a filter that grew one clause too many — where the
# check still runs, still finds nothing wrong, and still reports success. Nothing
# in a clean verdict distinguishes "looked at everything" from "looked at two of
# forty", which is why the count is printed; printing it is not gating on it.
#
# Set well under the real count on purpose. A floor at the real count fails on
# every ordinary addition and gets raised until it means nothing, so it is
# calibrated to catch a corpus falling away rather than a single item leaving.
# Raise one when the tree has grown enough that it can no longer fail.
MINIMUM_EXAMINED = {
    "every helm install names a timeout": 20,
    "no helm repo add swallows its own failure": 18,
    "every shell script runs on bash 3.2": 30,
    "every scrape surface a values file names is enabled": 1,
    "every file in an addon directory is applied": 25,
    "every gate is observed to reject and to accept": 3,
    "every path named in markdown resolves": 5,
    # Unconditional: one file is the law, not a count sized to this tree.
    "the tree holds content outside the gate directory": 1,
    "every gate refuses when its authority is unavailable": 4,
}

# The suite itself is a corpus and can collapse the same way. With CHECKS and
# CONTROLS both emptied, every per-check control has nothing to report on and
# the run loop has nothing to iterate, so the whole gate exits 0 having asserted
# nothing at all.
MINIMUM_CHECKS = 9

# And the control table, which is the corpus that licenses every other verdict
# in this file. `names - set(CONTROLS)` gates on a label having a KEY; what the
# controls examine is CASES EXECUTED. A key whose list is empty satisfies the
# first and contributes nothing to the second, so emptying every list leaves the
# suite printing that each invariant is proven to reject having run no proof at
# all. Case count was also the one quantity here that was never printed.
MINIMUM_CONTROL_CASES = 30


def names_token(haystack: str, token: str) -> bool:
    """Whether `token` appears as a whole token rather than inside a longer one.

    Substring containment is the wrong test for both callers here and escaped
    both of them: `my-values.yaml` in an installer vouched for a `values.yaml`
    nothing applies, and `--timeout-seconds` satisfied a check for `--timeout`.
    A token ends at anything that is not a name character, so the boundary is
    what distinguishes the two.
    """
    return re.search(r"(?<![\w.-])" + re.escape(token) + r"(?![\w.-])", haystack) is not None


def helm_calls_are_bounded(root: pathlib.Path = ROOT) -> list[str]:
    """helm's own default is five minutes, which is the wrong number here.

    A cold kind cluster pulling kube-prometheus-stack or cilium exceeds it
    routinely, and every install script runs under `set -euo pipefail`, so the
    implicit default aborts a slice midway through. The number matters less than
    it being chosen: an install that waits has to say how long.
    """
    problems = []
    scripts = sorted(root.glob("stack/*/*/install.sh"))
    if not scripts:
        return ["found no install.sh under stack/*/*/ — refusing to report every helm "
                "call bounded over an empty set."]
    checked = 0
    for script in scripts:
        for line, cmd in helm_invocations(strip_comments(script.read_text())):
            checked += 1
            if not names_token(cmd, "--timeout"):
                problems.append(
                    f"{script.relative_to(root)}:{line} runs `helm upgrade --install` with no "
                    f"--timeout, so it takes helm's implicit 5m — short enough that a cold "
                    f"image pull aborts the slice."
                )
    EXAMINED["every helm install names a timeout"] = checked
    if not checked:
        problems.append("no install.sh runs `helm upgrade --install` — the parser and the tree "
                        "disagree, so this check asserted nothing.")
    return problems


def addon_files_are_reached(root: pathlib.Path = ROOT) -> list[str]:
    """A manifest in an addon directory that nothing applies is dead.

    This is the check that catches a manifest sitting beside the one it
    duplicates, applied by nothing, carrying a header explaining why it exists.

    Reached means named by the addon's own install.sh or by its slice Taskfile —
    a script a task runs directly is reached, and narrowing this to install.sh
    alone would report the slice's verify and conformance scripts as dead.
    """
    problems = []
    installs = sorted(root.glob("stack/*/*/install.sh"))
    if not installs:
        return ["found no install.sh under stack/*/*/ — refusing to report every addon file "
                "reached over an empty set."]
    examined = 0
    for install in installs:
        addon = install.parent
        taskfile = addon.parent / "Taskfile.yaml"
        text = strip_comments(install.read_text())
        if taskfile.is_file():
            text += "\n" + strip_comments(taskfile.read_text())
        # rglob, not iterdir. A directory name is not an oracle for what is
        # inside it: naming `pre-install/` in an installer vouched for every
        # file under it, so a manifest could be added there and reach nothing
        # while the gate stayed green. The name of a directory is checked as
        # well, because a directory nothing names is dead whatever it holds.
        for entry in sorted(addon.rglob("*")):
            if entry.name == "install.sh" and entry.parent == addon:
                continue
            examined += 1
            if not names_token(text, entry.name):
                what = "directory" if entry.is_dir() else "file"
                problems.append(
                    f"{entry.relative_to(root)} — nothing names this {what} outside a comment, "
                    f"so nothing applies it and it ships as documentation that reads as config."
                )
    EXAMINED["every file in an addon directory is applied"] = examined
    return problems


DEPLOY_WITH = """apiVersion: apps/v1
kind: Deployment
metadata: {name: x}
spec:
  template:
    spec:
      containers:
        - name: c
          image: %s
"""

# The locally-built operator image, which check-images exempts from both the
# immutability rule and the scan. A render without it makes those exemptions
# look stale.
# Built with json.dumps rather than written as a literal: a hand-escaped regex
# inside a JSON string inside a Python string loses a backslash layer at every
# hop, and the fixture then fails to parse — which the floor correctly reported
# as the gate refusing its own clean tree.
RENOVATE_ONE_MANAGER = json.dumps(
    {
        "customManagers": [
            {
                "customType": "regex",
                "managerFilePatterns": [r"/^stack/.+/install\.sh$/"],
                "matchStrings": [
                    r"helm repo add \S+ (?<registryUrl>https?://\S+)"
                    r"[\s\S]*?helm upgrade --install \S+ \S+/(?<depName>[a-zA-Z0-9._-]+)"
                    r"[\s\S]*?--version (?<currentValue>\S+)"
                ],
                "datasourceTemplate": "helm",
            }
        ]
    },
    indent=2,
) + "\n"

WATCHED_PIN = (
    "helm repo add w https://w --force-update >/dev/null\n"
    "helm upgrade --install w w/w --version 1.0.0\n"
)

OPERATOR_IMAGE = DEPLOY_WITH % "ghcr.io/nanohype/eks-agent-platform/operator:dev"

BOUNDED_INSTALL = (
    "helm repo add x https://x --force-update >/dev/null\n"
    "helm upgrade --install a x/a --version 1.2.3 --wait --timeout 10m\n"
)

# What the floor feeds each gate, and how to invoke it. The gate's author
# chooses the fixture; the FLOOR writes it to disk, runs the gate as a real
# process, and reads the exit status. Nothing the gate reports about itself is
# consulted, because that is testimony — a four-line gate returning a literal
# {"ok": True, "lines": [...]} satisfied the two previous versions of this
# floor with no checks behind it.
GATE_PROBES: dict[str, dict] = {
    "check-images.py": {
        "argv": ["--pins", "{root}/render"],
        "names": "r/x:latest",
        # Both trees carry the operator image, because the gate asserts its
        # own exemptions against the render it is handed and an exemption whose
        # subject is absent is a finding in its own right. The fixture differs
        # in exactly the property under test and nothing else.
        "bad": {"render/s.yaml": DEPLOY_WITH % "r/x:latest" + "---\n" + OPERATOR_IMAGE},
        "good": {"render/s.yaml": DEPLOY_WITH % "r/x:1.2.3" + "---\n" + OPERATOR_IMAGE},
    },
    "check-renovate-coverage.py": {
        "argv": [],
        "names": "stack/s/a/install.sh",
        # Both trees carry a slice the manager DOES match, so the manager is
        # never dead and the gate cannot reject for that unrelated reason. They
        # differ only in whether a second slice pins in a shape no manager
        # matches — which is the violation this probe plants, and which the
        # rejection has to name.
        "bad": {"renovate.json": RENOVATE_ONE_MANAGER,
                "stack/s/w/install.sh": WATCHED_PIN,
                "stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                        "helm upgrade --install a x/a --ver 1.2.3\n"},
        "good": {"renovate.json": RENOVATE_ONE_MANAGER,
                 "stack/s/w/install.sh": WATCHED_PIN},
    },
    "check-chart-deprecation.py": {
        "argv": [],
        "names": "a",
        "bad": {"stack/s/a/install.sh": BOUNDED_INSTALL,
                "stack/chart-provenance.json": '{"charts": {}}\n'},
        "good": {"stack/s/a/install.sh": BOUNDED_INSTALL,
                 "stack/chart-provenance.json":
                     '{"charts": {"a": {"repo": "https://x", "description": "d",'
                     ' "deprecated": false}}}\n'},
    },
}


# Gates this floor cannot supply an input to, with the reason. Recorded rather
# than silently skipped, and asserted: an entry naming a gate that has left the
# tree fails. These are observed by their own controls on every run, which is
# weaker — a gate grading a fixture it wrote is testimony about a narrower
# claim than a gate handed an input it did not choose.
# Why a gate's VERDICT cannot be exercised from a fixture. Judgement only:
# whether the same gate refuses when what it reads is missing is a separate
# question, asked by AUTHORITY_PROBES above, and an entry here grants no
# exemption from it.
UNPROBEABLE: dict[str, str] = {
    "check-rendered-mounts.py":
        "reads a manifest stream on stdin rather than a path, so a probe would drive a "
        "different interface than the one render-check.sh uses",
    "check-rendered-schemas.py":
        "its verdict needs kubeconform and a reachable schema host, so a probe would make "
        "this floor non-hermetic and fail on a network outage rather than on a defect",
    "mirror-check.py":
        "compares against an eks-gitops checkout, which a fixture tree cannot stand in for "
        "without reimplementing the catalog's ApplicationSet shapes",
    "check-prose-voice.py":
        "diffs against a git ref, so a probe would need a fixture that is a git repository "
        "with history",
}


def gates_reject_and_accept(root: pathlib.Path = ROOT) -> list[str]:
    """Every probed gate is RUN by this floor, against trees this floor wrote.

    The floor supplies the input and reads the exit status. It does not ask a
    gate how it did, because anything a subject authors about its own behaviour
    is testimony rather than observation — the two previous versions of this
    check consumed a count and then a list of lines, and a four-line gate
    returning either as a literal satisfied both with nothing behind it.

    Both directions, and they cannot be satisfied by the same evidence: the
    verdicts are two exit codes from two different trees.
    """
    problems = []
    scripts_dir = root / "scripts"
    present = sorted(g.name for g in scripts_dir.glob("*.py")) if scripts_dir.is_dir() else []
    if not present:
        return ["found no gate scripts under scripts/ — refusing to report a proven suite "
                "over an empty set."]

    for name in sorted(GATE_PROBES):
        if name not in present:
            problems.append(
                f"GATE_PROBES names scripts/{name}, which is not in the tree — a probe that "
                f"outlasts its gate proves nothing about whatever replaced it."
            )

    observed = 0
    for name, probe in sorted(GATE_PROBES.items()):
        if name not in present:
            continue
        if "names" not in probe:
            problems.append(f"{name}: its probe declares no `names`, so a rejection for any "
                            f"reason at all would score as a catch.")
            continue
        if probe["bad"] == probe["good"]:
            problems.append(f"{name}: the probe's bad and good trees are identical, so running "
                            f"them proves nothing.")
            continue
        gate = scripts_dir / name
        verdict, output = {}, {}
        for kind in ("bad", "good"):
            fixture = _tree(probe[kind])
            argv = [a.format(root=fixture) for a in probe["argv"]]
            env = dict(os.environ, KX_GATE_ROOT=str(fixture))
            try:
                proc = subprocess.run([sys.executable, str(gate), *argv], capture_output=True,
                                      text=True, timeout=180, check=False, env=env)
                verdict[kind] = proc.returncode
                output[kind] = proc.stdout + proc.stderr
            except subprocess.TimeoutExpired:
                problems.append(f"{name}: did not finish within 180s on its {kind} fixture.")
                verdict[kind] = None
        if verdict.get("bad") == 0:
            problems.append(
                f"{name}: exited 0 on a tree built to violate the invariant it names. This "
                f"floor wrote that tree and ran the gate over it — the gate accepted it."
            )
        # Exit status alone is not proof the gate found what this floor planted:
        # it could be refusing the fixture for an unrelated reason, and scoring
        # that as a catch is the same credulity the floor exists to remove one
        # level down. The rejection has to name the mutation.
        # A crash also exits non-zero. Without this, a gate that blows up on the
        # bad fixture — and whose traceback happens to quote the fixture path —
        # is recorded as having caught what was planted. That is exit-code-
        # conflates-causes occurring inside the thing built to check for it.
        for kind in ("bad", "good"):
            if "Traceback (most recent call last)" in output.get(kind, ""):
                problems.append(
                    f"{name}: crashed on its {kind} fixture rather than reporting a verdict. "
                    f"A traceback exits non-zero like a rejection does, so this cannot count "
                    f"as one."
                )

        names = probe.get("names")
        if names and verdict.get("bad") not in (0, None) and names not in output.get("bad", ""):
            problems.append(
                f"{name}: rejected the bad tree without naming {names!r}. It refused the "
                f"fixture for some other reason, so this proves nothing about the violation "
                f"the floor planted."
            )
        if verdict.get("good") not in (0, None):
            problems.append(
                f"{name}: exited {verdict['good']} on the same tree with the violation removed. "
                f"A gate that refuses everything is as useless as one that refuses nothing."
            )
        if verdict.get("bad") is not None and verdict.get("good") is not None:
            observed += 1

    unprobed = [n for n in present
                if n not in GATE_PROBES and n not in UNPROBEABLE
                and n != pathlib.Path(__file__).name]
    if unprobed:
        problems.append(
            "no probe for " + ", ".join(f"scripts/{n}" for n in unprobed) +
            " — a gate this floor cannot run is a gate nothing observes. Add a probe, or "
            "record in UNPROBEABLE why it cannot take one."
        )
    for name in sorted(UNPROBEABLE):
        if name not in present:
            problems.append(f"UNPROBEABLE names scripts/{name}, which is not in the tree.")
    # Observations, not the size of the table. len(GATE_PROBES) is a property of
    # this file: it reports the same number over a tree with no gates in it at
    # all, so it can satisfy a floor while nothing has been run. A probe counts
    # once both of its verdicts came back from an actual subprocess.
    #
    # This invariant is exempt from the gates-only-tree rule, and the exemption
    # is the reason rather than a convenience: its subject IS the gate directory,
    # so on a tree holding nothing else that tree is its complete corpus and a
    # clean verdict over it is honest. The floor still separates the two cases —
    # it counts probes that produced two verdicts, so "this is my whole corpus"
    # reads differently from "I reached fewer gates than I should have".
    EXAMINED["every gate is observed to reject and to accept"] = observed
    return problems


# A repo-relative path in prose, as markdown link targets and as inline code.
# Anchored per line with [ \t] rather than \s for the reason stated above.
MD_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")

# Not a repo-relative path, though it has the shape of one. Each is a thing the
# reader resolves somewhere other than this tree.
NOT_A_PATH = re.compile(
    r"^(https?:|mailto:|#|~)"          # links out, in-page anchors, a home dir
    r"|^\.\./"                          # a sibling checkout, which may not be cloned
    r"|[*{}<>=]"                       # a glob, a placeholder, or a key=value
    r"|^-"                             # a flag
    r"|^/"                             # an absolute path or a JSON pointer
    r"|:[0-9/]"                        # a URL or a host:port
    r"|\.\.\."                         # an ellipsis, which is a placeholder like <slice>
)

# Inline code, the other way a document names a path. Ambiguous where a link is
# not, so it is read through FIRST_SEGMENT_IS_REAL below.
MD_CODE = re.compile(r"`([^`\n]+)`")


# Where the gates themselves live. Content here is the suite's own source, and a
# denominator made of it is a gate measuring itself.
GATE_DIR = "scripts"

# Whether a gate can LOOK, which is a different question from whether it can
# JUDGE, and the one that goes unasked.
#
# UNPROBEABLE records why a gate's verdict cannot be exercised from a fixture.
# Every reason in it is about judgement, and granting the exemption on that axis
# silently granted it on this one: a gate may be unable to judge inside a
# fixture and still be required to refuse when the thing it reads is not there.
# A gate reporting "this change adds no prose" on every pull request was exempt
# on the first axis and had never been asked about the second.
#
# The verdict required here is narrow and it is not a judgement about the tree:
# a non-zero exit, and a sentence rather than a traceback. A crash also exits
# non-zero, and a gate that blames the interpreter for a missing precondition
# has not reported anything.
AUTHORITY_PROBES: dict[str, dict] = {
    "check-prose-voice.py": {
        "authority": "git history reaching a merge base",
        "argv": ["origin/a-ref-that-does-not-exist"],
        "env": {},
        "empty_path": False,
    },
    "check-rendered-schemas.py": {
        "authority": "the kubeconform binary",
        "argv": ["{root}"],
        "env": {},
        "empty_path": True,
    },
    "check-images.py": {
        "authority": "the trivy binary",
        "argv": ["--cves", "{root}"],
        "env": {},
        "empty_path": True,
    },
    "mirror-check.py": {
        "authority": "an eks-gitops checkout",
        "argv": ["check"],
        "env": {"EKS_GITOPS_DIR": "{root}/not-a-checkout"},
        "empty_path": False,
    },
}


# Fixture gates for the authority control. Synthetic on purpose: the control has
# to distinguish a gate that refuses without its authority from one that reports
# clean, and the difference must be the only thing that varies between the two
# trees.
# The manifest shape mirror-check reads, so removing its checkout is the only
# thing the probe removes. Built from the keys the gate names rather than copied
# from the shipped file, which would make this fixture a second copy of a thing
# that moves.
AUTHORITY_FIXTURE_MANIFEST = json.dumps(
    {
        "upstream": {"repository": "example/upstream", "path": "applicationsets",
                     "ref": "0" * 40},
        "divergences": [],
        "siblings": {},
        "crdInstallers": {},
    },
    indent=2,
) + "\n"

REFUSES = ('import sys\n'
           'print("cannot reach what this gate reads", file=sys.stderr)\n'
           'sys.exit(1)\n')
REPORTS_CLEAN = ('import sys\n'
                 'print("nothing to report")\n'
                 'sys.exit(0)\n')


def _authority_tree(reporting_clean: str | None = None) -> dict[str, str]:
    """Every gate AUTHORITY_PROBES names, refusing, except one named to report clean."""
    return {
        f"{GATE_DIR}/{name}": (REPORTS_CLEAN if name == reporting_clean else REFUSES)
        for name in AUTHORITY_PROBES
    }


def gates_refuse_without_their_authority(root: pathlib.Path = ROOT) -> list[str]:
    """A gate whose authority is unavailable refuses instead of reporting clean.

    The authority is whatever a gate reads that the commit does not contain: a
    binary, a sibling checkout, git history. It is present on a developer
    machine and can be absent in CI, which is what makes this class invisible
    where it is introduced and live where it matters.

    Exercised by removing the authority and reading the exit status, because the
    shape being checked for is a gate that runs, finds nothing, and reports
    success — there is no skip branch for a reader to find.
    """
    problems = []
    scripts_dir = root / GATE_DIR
    present = sorted(g.name for g in scripts_dir.glob("*.py")) if scripts_dir.is_dir() else []
    if not present:
        return [f"found no gate scripts under {GATE_DIR}/ — refusing to report that gates "
                f"refuse over an empty set."]
    for name in sorted(AUTHORITY_PROBES):
        if name not in present:
            problems.append(f"AUTHORITY_PROBES names {GATE_DIR}/{name}, which is not in the "
                            f"tree — a probe that outlasts its gate proves nothing.")
    observed = 0
    with tempfile.TemporaryDirectory() as empty_bin:
        for name, probe in sorted(AUTHORITY_PROBES.items()):
            if name not in present:
                continue
            # Everything the gate needs except the one authority being removed.
            # A fixture that also withholds an unrelated precondition tests that
            # precondition instead, and scores the answer against this one.
            fixture = _tree({
                "stack/s/a/install.sh": BOUNDED,
                "stack/upstream.json": AUTHORITY_FIXTURE_MANIFEST,
            })
            argv = [a.format(root=fixture) for a in probe["argv"]]
            env = dict(os.environ, KX_GATE_ROOT=str(fixture))
            env.update({k: v.format(root=fixture) for k, v in probe["env"].items()})
            if probe["empty_path"]:
                env["PATH"] = empty_bin
            try:
                proc = subprocess.run([sys.executable, str(scripts_dir / name), *argv],
                                      capture_output=True, text=True, timeout=180,
                                      check=False, env=env)
            except subprocess.TimeoutExpired:
                problems.append(f"{name}: did not finish within 180s without {probe['authority']}.")
                continue
            out = proc.stdout + proc.stderr
            if proc.returncode == 0:
                problems.append(
                    f"{name}: exited 0 with {probe['authority']} unavailable. It reported a "
                    f"verdict on a change it could not read."
                )
            elif "Traceback (most recent call last)" in out:
                problems.append(
                    f"{name}: crashed rather than refusing when {probe['authority']} was "
                    f"unavailable. A traceback exits non-zero like a refusal does, so it "
                    f"cannot count as one — the precondition wants naming."
                )
            else:
                observed += 1
    EXAMINED["every gate refuses when its authority is unavailable"] = observed
    return problems


def tree_has_content_outside_the_gates(root: pathlib.Path = ROOT) -> list[str]:
    """A tree that is only the gate scripts is not a tree this suite can report on.

    The unconditional half of the floor, and the two halves have to stay apart.

    Every minimum in MINIMUM_EXAMINED is sized to this repository: true here,
    not true of a fixture, and not a statement about trees in general. This one
    is a law about any tree, which is what makes it the half that catches a
    checkout containing nothing but the gates — where every gate runs, finds no
    violation in its own source, and reports success on a denominator built from
    its own comments.

    Collapsed into one repo-sized number, the same floor rejects the
    deliberately small fixtures the controls are built from, and a working suite
    is then reported as one that fails everything.

    Reads tracked files where the tree is a repository, because a build artefact
    is present on a developer machine and absent from a fresh checkout, and a
    law that a cache directory can satisfy is not one.
    """
    err = require_git(root)
    if err:
        EXAMINED["the tree holds content outside the gate directory"] = 0
        return [err]
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=False,
    )
    paths = [pathlib.PurePosixPath(line) for line in tracked.stdout.splitlines() if line]
    outside = [str(f) for f in paths if f.parts and f.parts[0] != GATE_DIR]
    EXAMINED["the tree holds content outside the gate directory"] = len(outside)
    if not outside:
        return [f"every tracked file is under {GATE_DIR}/ — this is the suite's own source, so "
                f"a clean verdict here reports that the gates do not violate themselves."]
    return []


def markdown_paths_resolve(root: pathlib.Path = ROOT) -> list[str]:
    """Every repo-relative path named in markdown exists.

    The documentation rule made executable. Prose that names a thing is a claim
    about the world, and the claim most likely to rot is a path: a file moves
    and every document that pointed at it keeps pointing, confidently, at
    nothing.

    Two views, because a document names paths two ways and they need different
    rules. A LINK target is an unambiguous claim — a thing the reader clicks —
    so any target carrying a separator is checked. INLINE CODE is ambiguous:
    `karpenter.sh/capacity-type` is a label key and `username/password/host` is
    prose, so a token is read as a path only when its first segment is a real
    entry at the repository root.

    That discriminator bounds what this can catch, and the bound is worth
    stating: it sees a file moving inside a directory that still exists, and it
    does not see a whole top-level directory being removed, because a token
    whose first segment is absent cannot be told apart from a label key or from
    a directory the documentation says must never exist. Checking link targets
    unconditionally is what covers the second case, and a link is the right way
    to write a claim that must be checked that way.

    Reads the RAW markdown, because in a document the prose IS the target and
    the blanked view would leave nothing to check.

    The ellipsis exclusion is narrower than what it replaced, and the difference
    is worth stating rather than leaving for someone to rediscover. It exists so
    that prose naming a path PRECISELY BECAUSE IT MUST NOT EXIST — the
    `stack/substitutes/...` this repository tells you not to create — is not
    read as a claim that the path does. That is a complete answer for the case
    it was built for.

    What it costs is link coverage: a markdown link target containing an
    ellipsis was checked before and is skipped now. No such link exists in this
    tree, so nothing currently depends on it, and the wider rule is not
    reinstated because doing so would trade a real false positive for a
    hypothetical true one. The check is sufficient here rather than equivalent
    to what came before.
    """
    problems = []
    examined = 0
    docs = sorted(root.glob("*.md")) + sorted(root.glob("docs/**/*.md"))
    if not docs:
        EXAMINED["every path named in markdown resolves"] = 0
        return ["found no markdown at the repository root — refusing to report every path "
                "resolved over an empty set."]
    tops = {entry.name for entry in root.iterdir()}

    def check(ref: str, doc: pathlib.Path, n: int) -> None:
        nonlocal examined
        # Only a leading ./ is stripped. Stripping "." would turn
        # .github/workflows/ci.yml into a path that does not exist —
        # which is what the first version of this reported.
        rel = ref[2:] if ref.startswith("./") else ref
        examined += 1
        if not (root / rel.rstrip("/")).exists():
            problems.append(f"{doc.relative_to(root)}:{n} names `{ref}`, which does not exist.")

    for doc in docs:
        for n, line in enumerate(doc.read_text().splitlines(), 1):
            for m in MD_LINK.finditer(line):
                ref = m.group(1).strip()
                if ref and "/" in ref and not NOT_A_PATH.search(ref):
                    check(ref, doc, n)
            for m in MD_CODE.finditer(line):
                ref = m.group(1).strip()
                if not ref or "/" not in ref or NOT_A_PATH.search(ref):
                    continue
                if ref.split("/")[0] in tops:
                    check(ref, doc, n)

    EXAMINED["every path named in markdown resolves"] = examined
    # Printing the denominator is not gating on it. Markdown that names no path
    # at all is a tree this rule cannot speak about, and saying "every path
    # resolves" over nothing is the vacuous pass the whole suite exists to
    # refuse. The earlier floor counted markdown FILES, which is not the
    # quantity examined: this tree has three of them and, under the link-only
    # rule it replaced, zero paths — so it reported success having checked
    # nothing.
    if not problems and examined == 0:
        return ["markdown is present but names no repo-relative path — refusing to report "
                "every path resolved over an empty set."]
    return problems


REPO_ADD = re.compile(r"^[ \t]*helm repo add\b.*$", re.M)


def repo_adds_do_not_swallow(root: pathlib.Path = ROOT) -> list[str]:
    """No `helm repo add` suppresses its own failure.

    `helm repo add` exits 0 when the alias already maps to the same URL, so the
    idempotent case never needed suppressing. It fails on exactly one thing: the
    alias existing against a DIFFERENT url — which is the case that must not be
    ignored, because the install below then pulls a chart of the same name from
    somewhere else. `|| true` suppressed only that.

    `--force-update` is the shape that works: it resolves the collision in this
    repository's favour, leaves the idempotent case at 0, and still exits
    non-zero on an unreachable or malformed repository. Verified against helm
    directly rather than assumed.
    """
    problems = []
    scripts = sorted(root.glob("stack/*/*/install.sh"))
    if not scripts:
        return ["found no install.sh under stack/*/*/ — refusing to report every repo add "
                "unsuppressed over an empty set."]
    examined = 0
    for script in scripts:
        for n, line in enumerate(strip_comments(script.read_text()).splitlines(), 1):
            if not REPO_ADD.match(line):
                continue
            examined += 1
            if "|| true" in line or "2>&1" in line:
                problems.append(
                    f"{script.relative_to(root)}:{n} suppresses `helm repo add`'s failure. "
                    f"The only thing it can fail on is the alias already pointing somewhere "
                    f"else, which is the one case worth hearing about."
                )
    EXAMINED["no helm repo add swallows its own failure"] = examined
    if not examined:
        problems.append("no install.sh runs `helm repo add` — the parser and the tree "
                        "disagree, so this check asserted nothing.")
    return problems


# Constructs bash 3.2 does not have. macOS ships /bin/bash 3.2.57 and the
# documented prerequisites install no newer bash, so any of these aborts at
# runtime on the platform this workspace targets — and shellcheck, which parses
# rather than executes, does not object.
BASH4_ONLY = [
    (re.compile(r"(?<![\w-])(mapfile|readarray)\b"), "mapfile/readarray", "a `while IFS= read -r` loop"),
    (re.compile(r"(?<![\w-])coproc\b"), "coproc", "an explicit background process with a fifo"),
    (re.compile(r"declare\s+-A\b"), "declare -A", "parallel indexed arrays, or a case statement"),
    (re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^|,,)"), "${var^^} / ${var,,}", "tr"),
]


SCRAPE_KEY = re.compile(r"^(\s*)(serviceMonitor|podMonitor|prometheusRule):\s*$")


def scrape_surfaces_are_on(root: pathlib.Path = ROOT) -> list[str]:
    """A values file that names a scrape surface enables it.

    prometheus-operator-crds is a core addon, so these CRDs exist on every kx
    cluster and the resource applies whether or not the observability slice is
    up — inert until a Prometheus reads it, picked up the moment one arrives.
    Leaving one off makes the scrape a step the reader has to know to take,
    which is the opt-in this workspace's always-complete rule exists to avoid.

    The rule was stated in two values files and enforced in neither, which is
    how the class stays open: a third addon added with the surface off ships
    silently, and the two comments become a memorial.
    """
    problems = []
    values = sorted(root.glob("stack/*/*/values*.yaml"))
    if not values:
        return ["found no values files under stack/*/*/ — refusing to report every scrape "
                "surface enabled over an empty set."]
    examined = 0
    for path in values:
        lines = strip_comments(path.read_text()).splitlines()
        for n, line in enumerate(lines):
            m = SCRAPE_KEY.match(line)
            if not m:
                continue
            examined += 1
            indent, key = m.group(1), m.group(2)
            # The block's own keys, to the first line at or below its indent.
            block = []
            for nxt in lines[n + 1:]:
                if nxt.strip() and not nxt.startswith(indent + " "):
                    break
                block.append(nxt)
            body = "\n".join(block)
            if "enabled:" in body and "enabled: true" not in body:
                problems.append(
                    f"{path.relative_to(root)}:{n + 1} sets {key}.enabled to something other "
                    f"than true. The CRD is core, so this applies on every cluster and is "
                    f"inert until a Prometheus reads it — leaving it off makes the scrape a "
                    f"step the reader has to know to take."
                )
    EXAMINED["every scrape surface a values file names is enabled"] = examined
    return problems


def require_git(root: pathlib.Path) -> str | None:
    """git is an authority, not merely a convenience.

    Two checks here scope their population to the tracked set, so git is not
    something this suite runs alongside its work — it is what tells the suite
    what it is meant to examine. An authority that is absent must fail, exactly
    as a missing sibling checkout does, and it must fail by saying so: without
    this the first `git` call raises FileNotFoundError, which exits non-zero like
    a refusal and names the binary rather than what could not be determined.

    A tree that is not a repository is the same failure by a different route. The
    fallback that used to cover it walked the filesystem instead, silently
    grading whatever happened to be in the directory — which is the behaviour
    scoping to the tracked set was introduced to stop.
    """
    if shutil.which("git") is None:
        return ("git is not on PATH. This suite scopes what it examines to the tracked set, "
                "so without git it cannot determine its own population and any verdict it "
                "gave would be over an unknown corpus.")
    inside = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return (f"{root} is not a git repository, so the tracked set does not exist and the "
                f"population this suite examines is undefined.")
    return None


def tracked_shell_scripts(root: pathlib.Path) -> list[pathlib.Path]:
    """The shell scripts THIS repository owns.

    `git ls-files`, because a bare `**/*.sh` walk grades whatever else happens to
    be in the working directory. In the render job that is a sibling repository
    checked out into the workspace to render one slice against — so the gate
    examined 47 files where the tree has 43, reported a real bash-4 construct in
    somebody else's installer, and failed this repository's build for it.

    A gate that grades a neighbour is wrong even when its finding is right: the
    seat that can fix it is not the one being stopped. Falls back to a walk for
    a fixture tree, which is not a git repository.
    """
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "*.sh"],
                         capture_output=True, text=True, check=False)
    return sorted(root / f for f in out.stdout.split("\0") if f)


def shell_runs_on_bash_3(root: pathlib.Path = ROOT) -> list[str]:
    """No tracked shell script uses a construct bash 3.2 lacks.

    This class was described in prose in two files and enforced in none, and a
    second instance shipped anyway — into a teardown script whose sibling, in
    the same directory and the same change, carries the comment explaining why
    the construct cannot be used. That teardown aborted before its first delete
    on a stock mac, stranding the credential it exists to remove.

    A comment naming a defect class is a memorial, not a control.
    """
    problems = []
    scripts = tracked_shell_scripts(root)
    if not scripts:
        return ["found no shell scripts — refusing to report bash 3.2 compatibility over an "
                "empty set."]
    examined = 0
    for script in scripts:
        examined += 1
        for n, line in enumerate(strip_comments(script.read_text()).splitlines(), 1):
            for pattern, what, instead in BASH4_ONLY:
                if pattern.search(line):
                    problems.append(
                        f"{script.relative_to(root)}:{n} uses {what}, which bash 3.2 does not "
                        f"have — use {instead}. shellcheck parses rather than runs, so it "
                        f"does not object."
                    )
    EXAMINED["every shell script runs on bash 3.2"] = examined
    return problems


CHECKS = [
    ("every helm install names a timeout", helm_calls_are_bounded),
    ("no helm repo add swallows its own failure", repo_adds_do_not_swallow),
    ("every shell script runs on bash 3.2", shell_runs_on_bash_3),
    ("every scrape surface a values file names is enabled", scrape_surfaces_are_on),
    ("every file in an addon directory is applied", addon_files_are_reached),
    ("every gate is observed to reject and to accept", gates_reject_and_accept),
    ("every path named in markdown resolves", markdown_paths_resolve),
    ("the tree holds content outside the gate directory",
     tree_has_content_outside_the_gates),
    ("every gate refuses when its authority is unavailable",
     gates_refuse_without_their_authority),
]

# One control per check, introducing the exact violation that check exists to
# catch. A check shipped without one fails the run, and a control naming a check
# that no longer exists fails it too, so the suite cannot quietly shrink.
#
# `clean` is the same tree without the violation. Asserting the gate is clean
# BEFORE mutating is what makes a non-zero exit mean anything — without it the
# gate might have been failing for an unrelated reason the whole time.
# A gate satisfying the contract honestly: its controls run, and the lines they
# produced come back as the evidence the floor counts.
GOOD_GATE = (
    "def control_outcomes():\n"
    "    lines = ['  rejected  a break', '  passed    a clean tree']\n"
    "    return {'ok': True, 'lines': lines}\n"
)

BOUNDED = ('helm repo add x https://x\n'
           'helm upgrade --install a x/a --version 1 --wait --timeout 10m\n')

CONTROLS = {
    "every helm install names a timeout": [
        (
            "a helm install with no --timeout",
            {"stack/s/a/install.sh": BOUNDED.replace(" --timeout 10m", "")},
            {"stack/s/a/install.sh": BOUNDED},
        ),
        (
            "a --timeout in a comment after an apostrophe in a word",
            {"stack/s/a/install.sh":
             "helm upgrade --install a x/a --version 1 --wait \\\n"
             "  --set note=dont'care  # --timeout 10m is set by the caller\n"},
            {"stack/s/a/install.sh":
             "helm upgrade --install a x/a --version 1 --wait --timeout 10m \\\n"
             "  --set note=dont'care\n"},
        ),
        (
            "a --timeout that appears only in a comment",
            {"stack/s/a/install.sh": "# TODO: add --timeout here one day\n"
                                     + BOUNDED.replace(" --timeout 10m", "")},
            {"stack/s/a/install.sh": BOUNDED},
        ),
        (
            "an INDENTED helm install with no --timeout",
            {"stack/s/a/install.sh": "if true; then\n  helm upgrade --install a x/a --wait\nfi\n"},
            {"stack/s/a/install.sh": "if true; then\n  helm upgrade --install a x/a --wait "
                                     "--timeout 10m\nfi\n"},
        ),
        (
            "a violation under three comment lines is cited at its own line",
            {"stack/s/a/install.sh": "# one\n# two\n# three\n"
                                     "helm upgrade --install a x/a --version 1 --wait\n"},
            {"stack/s/a/install.sh": "# one\n# two\n# three\n" + BOUNDED},
            "install.sh:4",
        ),
        (
            "a longer flag containing --timeout does not satisfy it",
            {"stack/s/a/install.sh":
             "helm upgrade --install a x/a --version 1 --wait --timeout-seconds 30\n"},
            {"stack/s/a/install.sh": BOUNDED},
        ),
        (
            "an unbounded helm install beside an unrelated --timeout",
            {"stack/s/a/install.sh": BOUNDED.replace(" --timeout 10m", "")
                                     + "kubectl wait --for=condition=Ready --timeout=300s pod/x\n"},
            {"stack/s/a/install.sh": BOUNDED
                                     + "kubectl wait --for=condition=Ready --timeout=300s pod/x\n"},
        ),
        (
            "no install.sh at all",
            {"README.md": "nothing here\n"},
            {"stack/s/a/install.sh": BOUNDED},
        ),
    ],
    "every path named in markdown resolves": [
        (
            "a markdown link to a file that does not exist",
            {"README.md": "see [the installer](stack/nope/install.sh) for details\n"},
            {"README.md": "see [the gate](scripts/check-slice-integrity.py) for details\n",
             "scripts/check-slice-integrity.py": "x\n"},
        ),
        (
            "a link target that does not exist",
            {"README.md": "run [the script](scripts/does-not-exist.py) first\n"},
            {"README.md": "run [the script](scripts/real.py) first\n", "scripts/real.py": "x\n"},
        ),
        (
            "a violation cited at its own line",
            {"README.md": "one\ntwo\nthree\nsee [it](stack/nope/install.sh)\n"},
            {"README.md": "one\ntwo\nthree\nsee [it](stack/yes/install.sh)\n",
             "stack/yes/install.sh": "x\n"},
            "README.md:4",
        ),
        (
            "a dot-prefixed path that does exist is not reported",
            {"README.md": "see [ci](.github/workflows/nope.yml)\n"},
            {"README.md": "see [ci](.github/workflows/ci.yml)\n",
             ".github/workflows/ci.yml": "x\n"},
        ),
        (
            "no markdown at all",
            {"scripts/x.py": "x\n"},
            {"README.md": "see `scripts/real.py`\n", "scripts/real.py": "x\n"},
        ),
        (
            "markdown that names no path at all",
            {"README.md": "prose with no path in it\n", "scripts/real.py": "x\n"},
            {"README.md": "see `scripts/real.py`\n", "scripts/real.py": "x\n"},
        ),
        (
            "an inline-code path that does not exist",
            {"README.md": "run `scripts/does-not-exist.py`\n", "scripts/real.py": "x\n"},
            {"README.md": "run `scripts/real.py`\n", "scripts/real.py": "x\n"},
        ),
        (
            "a label key in inline code is not read as a path",
            {"README.md": "`scripts/gone.py` and `karpenter.sh/capacity-type`\n",
             "scripts/real.py": "x\n"},
            {"README.md": "`scripts/real.py` and `karpenter.sh/capacity-type`\n",
             "scripts/real.py": "x\n"},
        ),
        (
            "an ellipsis placeholder is not read as a path",
            {"README.md": "`scripts/gone.py`, never `stack/substitutes/...`\n",
             "scripts/real.py": "x\n"},
            {"README.md": "`scripts/real.py`, never `stack/substitutes/...`\n",
             "scripts/real.py": "x\n"},
        ),
        (
            "an inline-code path whose first segment is not a repo entry is not a path",
            {"README.md": "`scripts/gone.py` and `username/password/host`\n",
             "scripts/real.py": "x\n"},
            {"README.md": "`scripts/real.py` and `username/password/host`\n",
             "scripts/real.py": "x\n"},
        ),
    ],
    "every gate refuses when its authority is unavailable": [
        (
            "a gate that reports clean with its authority unavailable",
            _authority_tree(reporting_clean="check-prose-voice.py"),
            _authority_tree(),
            "exited 0",
        ),
    ],
    "the tree holds content outside the gate directory": [
        (
            "a tree that is only the gate scripts",
            {"scripts/check-x.py": "x\n"},
            {"scripts/check-x.py": "x\n", "stack/s/a/install.sh": BOUNDED},
        ),
    ],
    "no helm repo add swallows its own failure": [
        (
            "a repo add suppressed with || true",
            {"stack/s/a/install.sh": "helm repo add x https://x >/dev/null 2>&1 || true\n"
                                     + BOUNDED},
            {"stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
        ),
        (
            "a repo add whose stderr is redirected away",
            {"stack/s/a/install.sh": "helm repo add x https://x 2>&1 >/dev/null\n" + BOUNDED},
            {"stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
        ),
        (
            "a suppression cited at its own line",
            {"stack/s/a/install.sh": "# one\n# two\n"
                                     "helm repo add x https://x >/dev/null 2>&1 || true\n"
                                     + BOUNDED},
            {"stack/s/a/install.sh": "# one\n# two\n"
                                     "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
            "install.sh:3",
        ),
        (
            "no install.sh at all",
            {"README.md": "nothing here\n"},
            {"stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
        ),
    ],
    "every shell script runs on bash 3.2": [
        (
            "mapfile, the construct that shipped despite being described twice",
            {"s.sh": "mapfile -t X < <(echo a)\n"},
            {"s.sh": "while IFS= read -r x; do :; done < <(echo a)\n"},
            "s.sh:1",
        ),
        (
            "an associative array",
            {"s.sh": "declare -A M\n"},
            {"s.sh": "M_keys=()\n"},
        ),
        (
            "case modification on a parameter",
            {"s.sh": 'echo "${x^^}"\n'},
            {"s.sh": 'echo "$x" | tr a-z A-Z\n'},
        ),
        (
            "the construct named only in a comment",
            {"s.sh": "mapfile -t X < <(echo a)\n"},
            {"s.sh": "# mapfile is deliberately not used here\necho ok\n"},
        ),
        (
            "a bash-4 construct in a sibling checkout is NOT this repo's finding",
            # Both trees carry the violation only outside the tracked set, as a
            # real sibling checkout does: written to disk, never staged. What
            # separates the two trees is the tracked file, which is the only
            # thing this gate is scoped to examine.
            {"s.sh": "declare -A M\n", "!.sibling/other/x.sh": "declare -A M\n"},
            {"s.sh": "echo ok\n", "!.sibling/other/x.sh": "declare -A M\n"},
            "s.sh:1",
        ),
        (
            "no shell scripts at all",
            {"README.md": "nothing\n"},
            {"s.sh": "echo ok\n"},
        ),
    ],
    "every scrape surface a values file names is enabled": [
        (
            "a serviceMonitor left disabled",
            {"stack/s/a/values.yaml": "metrics:\n  serviceMonitor:\n    enabled: false\n"},
            {"stack/s/a/values.yaml": "metrics:\n  serviceMonitor:\n    enabled: true\n"},
            "values.yaml:2",
        ),
        (
            "a podMonitor left disabled",
            {"stack/s/a/values.yaml": "podMonitor:\n  enabled: false\n"},
            {"stack/s/a/values.yaml": "podMonitor:\n  enabled: true\n"},
        ),
        (
            "no values files at all",
            {"README.md": "nothing\n"},
            {"stack/s/a/values.yaml": "podMonitor:\n  enabled: true\n"},
        ),
    ],
    "every file in an addon directory is applied": [
        (
            "an addon file nothing applies",
            {"stack/s/a/install.sh": BOUNDED, "stack/s/a/orphan.yaml": "kind: ClusterRole\n"},
            {"stack/s/a/install.sh": BOUNDED + 'kubectl apply -f "${SCRIPT_DIR}/orphan.yaml"\n',
             "stack/s/a/orphan.yaml": "kind: ClusterRole\n"},
        ),
        (
            "a file inside a named directory that nothing applies",
            {"stack/s/a/install.sh": BOUNDED + 'kubectl apply -f "${SCRIPT_DIR}/pre/a.yaml"\n',
             "stack/s/a/pre/a.yaml": "kind: A\n",
             "stack/s/a/pre/orphan.yaml": "kind: B\n"},
            {"stack/s/a/install.sh": BOUNDED + 'kubectl apply -f "${SCRIPT_DIR}/pre/a.yaml"\n',
             "stack/s/a/pre/a.yaml": "kind: A\n"},
            "pre/orphan.yaml",
        ),
        (
            "a filename that is a substring of one the installer does apply",
            {"stack/s/a/install.sh": BOUNDED + 'kubectl apply -f "${SCRIPT_DIR}/my-values.yaml"\n',
             "stack/s/a/my-values.yaml": "a: 1\n",
             "stack/s/a/values.yaml": "b: 2\n"},
            {"stack/s/a/install.sh": BOUNDED + 'kubectl apply -f "${SCRIPT_DIR}/my-values.yaml"\n',
             "stack/s/a/my-values.yaml": "a: 1\n"},
            "values.yaml",
        ),
        (
            "an addon file named only in a comment",
            {"stack/s/a/install.sh": BOUNDED + "# we no longer apply orphan.yaml\n",
             "stack/s/a/orphan.yaml": "kind: ClusterRole\n"},
            {"stack/s/a/install.sh": BOUNDED + 'kubectl apply -f "${SCRIPT_DIR}/orphan.yaml"\n',
             "stack/s/a/orphan.yaml": "kind: ClusterRole\n"},
        ),
        (
            "no install.sh at all",
            {"README.md": "nothing here\n"},
            {"stack/s/a/install.sh": BOUNDED},
        ),
    ],
}


# Prefix for a fixture file that is written but not staged. A sibling repository
# checked out into the workspace is present on disk and absent from the tracked
# set, and a fixture that stages it is modelling a different situation than the
# one the gate was scoped for.
UNTRACKED = "!"


def _tree(files: dict[str, str]) -> pathlib.Path:
    """A fixture tree, as a real repository.

    Tracked, because checks scoped to `git ls-files` examine the tracked set and
    a fixture outside that set tests nothing. A plain directory sends those
    checks down their fallback instead, so the path that runs in CI is the one
    path no control covers — and a fixture planted where the gate does not look
    reads as the gate failing to reject, which is a defect reported against a
    gate that is behaving correctly.

    Files are staged by name. `git ls-files` reads the index, so staging is what
    puts them in the population; no commit is needed.
    """
    d = pathlib.Path(tempfile.mkdtemp(prefix="kx-integrity-"))
    staged = []
    for key, body in files.items():
        untracked = key.startswith(UNTRACKED)
        rel = key[len(UNTRACKED):] if untracked else key
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
        if not untracked:
            staged.append(rel)
    subprocess.run(["git", "init", "-q", str(d)], check=False, capture_output=True)
    if staged:
        subprocess.run(["git", "-C", str(d), "add", "--", *staged], check=False,
                       capture_output=True)
    return d


# Checks controlled outside the (broken, clean) table, with the reason. The
# table cannot express this one: every synthetic tree is "bad" to it, because
# GATE_PROBES names real gates a fixture does not contain. Asserted below — an
# entry naming a check that no longer exists fails, like every other exemption
# in this repository.
CONTROLLED_ELSEWHERE = {
    "every gate is observed to reject and to accept":
        "controlled at the end of controls(), by probing a throwaway gate that reads its "
        "input and one that ignores it. It cannot probe ITSELF — every fixture tree it "
        "writes lacks the gates GATE_PROBES names, so its own clean tree can never be "
        "clean — and that is a stated limit rather than a covered case",
}


def controls() -> int:
    """Prove every check rejects the violation it exists to catch."""
    names = {label for label, _ in CHECKS}
    failures = 0

    if len(CHECKS) < MINIMUM_CHECKS:
        print(f"  SUITE       {len(CHECKS)} check(s), below the floor of {MINIMUM_CHECKS}. "
              f"An empty suite passes every control it has and asserts nothing.")
        failures += 1
    for label in sorted(names - set(MINIMUM_EXAMINED)):
        print(f"  NO FLOOR    {label} — a check with no minimum can report a clean verdict "
              f"over an empty corpus.")
        failures += 1
    for label in sorted(set(MINIMUM_EXAMINED) - names):
        print(f"  ORPHANED    MINIMUM_EXAMINED names {label!r}, which is not a check.")
        failures += 1

    for label in sorted(names - set(CONTROLS) - set(CONTROLLED_ELSEWHERE)):
        print(f"  NO CONTROL  {label} — a check with no positive control cannot be trusted.")
        failures += 1
    for label in sorted(k for k, v in CONTROLS.items() if not v):
        print(f"  NO CONTROL  {label} — its control list is empty, which proves exactly as "
              f"much as having no entry at all.")
        failures += 1
    for label in sorted(set(CONTROLLED_ELSEWHERE) - names):
        print(f"  ORPHANED    CONTROLLED_ELSEWHERE names {label!r}, which is not a check.")
        failures += 1
    for label in sorted(set(CONTROLS) - names):
        print(f"  ORPHANED    control for {label!r}, which is not a check any more.")
        failures += 1

    # The floor, proven by running the whole suite over a tree far below every
    # minimum. A floor that lives in a table and is never exercised is a comment
    # about a floor: it cannot tell you it stopped firing, and the run that
    # would have needed it is the run that finds out.
    # git as an authority, exercised rather than described. Both routes to an
    # undefined population: the binary missing, and a tree that is not a
    # repository. Neither may reach a verdict.
    real_path = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as no_git:
        os.environ["PATH"] = no_git
        try:
            absent = require_git(pathlib.Path(no_git))
        finally:
            os.environ["PATH"] = real_path
    not_a_repo = require_git(pathlib.Path(tempfile.mkdtemp(prefix="kx-not-a-repo-")))
    if not absent or "git is not on PATH" not in absent:
        print("  GIT OPEN    require_git accepted a PATH with no git on it.")
        failures += 1
    elif not not_a_repo or "not a git repository" not in not_a_repo:
        print("  GIT OPEN    require_git accepted a directory that is not a repository.")
        failures += 1
    else:
        print("  control ok  git absent and not-a-repository are both refused, by name")

    # The missing-denominator branch, exercised. It cannot fire from the real
    # tree — every check records a count — so without this it is a branch that
    # has never run, which is the same standing as a floor nobody has seen
    # reject.
    saved_checks, saved_floors = list(CHECKS), dict(MINIMUM_EXAMINED)
    CHECKS.append(("a check that records no denominator", lambda root: []))
    MINIMUM_EXAMINED["a check that records no denominator"] = 1
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            silent_count = run(_tree({"stack/s/a/install.sh": BOUNDED}))
    finally:
        CHECKS[:] = saved_checks
        MINIMUM_EXAMINED.clear()
        MINIMUM_EXAMINED.update(saved_floors)
    if "reported clean without recording" not in buf.getvalue():
        print("  COUNT OPEN  a check reporting clean with no denominator was not refused.")
        failures += 1
    else:
        print("  control ok  a check that records no denominator is refused, not exempted")
    del silent_count

    # Measured as a DIFFERENCE, because every simpler form of this control passes
    # for the wrong reason. A tree small enough to sit below every floor also
    # violates invariants that have nothing to do with floors, so "the suite
    # failed" is satisfied by floors that gate nothing; and "the output mentions
    # a floor" is satisfied by a floor downgraded to a warning, which still
    # prints. Running the same tree with the floors removed isolates what they
    # contribute to the verdict, which is the only thing that matters about them.
    starved_tree = _tree({
        "stack/s/a/install.sh": BOUNDED,
        "README.md": "see `stack/s/a/install.sh`\n",
    })
    buf = io.StringIO()
    saved = dict(MINIMUM_EXAMINED)
    with contextlib.redirect_stdout(buf):
        with_floors = run(starved_tree)
        MINIMUM_EXAMINED.clear()
        try:
            without_floors = run(starved_tree)
        finally:
            MINIMUM_EXAMINED.update(saved)
    if with_floors <= without_floors:
        print(f"  FLOOR OPEN  a tree below every minimum fails {with_floors} invariant(s) with "
              f"the floors and {without_floors} without them. The floors change no verdict, so "
              f"they are reporting rather than gating.")
        failures += 1
    else:
        print(f"  control ok  the examined-floor rejects a collapsed corpus "
              f"[{with_floors} invariant(s) fired, {with_floors - without_floors} of them "
              f"only because of a floor]")

    by_label = dict(CHECKS)
    # Three quantities, because the interesting one is the smallest. `seen` is
    # cases entered, `accounted` is cases that recorded a verdict either way, and
    # `proven` is cases that completed a proof. Counting at the top of the loop
    # measures cases SEEN, which is a property of the table rather than of what
    # ran — the same substitution as counting gates shipped instead of controls
    # completed. Every count below happens after the last assertion, so a path
    # that leaves early cannot contribute to it.
    seen = accounted = proven = 0
    for label, cases in sorted(CONTROLS.items()):
        check = by_label.get(label)
        if check is None:
            # Loud, not skipped. This is unreachable while the orphan check above
            # holds, and "unreachable because something else is true" is the
            # argument rather than the guarantee — a silent continue is exactly
            # the shape being guarded against here.
            print(f"  UNRESOLVED  {label} — has {len(cases)} control case(s) and no check to "
                  f"run them against, so none of them can prove anything.")
            failures += 1
            seen += len(cases)
            continue
        for case in cases:
            seen += 1
            name, broken, clean = case[0], case[1], case[2]
            must_say = case[3] if len(case) > 3 else None
            # Assert the mutation happened, by comparing the fixtures rather
            # than by trusting the verdict. A mutation that silently fails to
            # mutate hands the gate an unbroken tree, the gate correctly passes
            # it, and the pass is recorded as proof the control works — a
            # failure that reads as success and that inspection does not catch.
            if broken == clean:
                print(f"  NOT MUTATED {name} — the broken and clean fixtures are identical, "
                      f"so this control proves nothing.")
                failures += 1
                accounted += 1
                continue
            if check(_tree(clean)):
                print(f"  UNSOUND     {name} — the gate already fails on the clean tree, so "
                      f"failing on the mutation would prove nothing.")
                failures += 1
                accounted += 1
                continue
            reported = check(_tree(broken))
            if not reported:
                print(f"  FAILS OPEN  {name} — the gate accepted it.")
                failures += 1
                accounted += 1
            elif must_say and not any(must_say in r for r in reported):
                print(f"  MIS-CITED   {name} — expected {must_say!r} in the report, got:")
                for r in reported:
                    print(f"                {r}")
                failures += 1
                accounted += 1
            else:
                print(f"  control ok  {name}")
                # Last statement in the body, after reject, name-the-mutation and
                # citation have all been checked. This counts a proof, not a case.
                proven += 1
                accounted += 1
    # The floor's own control, which the (broken, clean) table cannot express:
    # every synthetic tree is "bad" to that check, because GATE_PROBES names
    # real gates a fixture tree does not contain. So this writes a throwaway
    # gate that genuinely inspects its tree, probes it through the same
    # machinery, and observes the exit codes — including the case that matters,
    # a gate that ignores its input and always succeeds.
    # The honest gate names what it found, because the floor now requires a
    # rejection to identify the planted violation rather than merely occur.
    honest = ("import os, pathlib, sys\n"
              "root = pathlib.Path(os.environ['KX_GATE_ROOT'])\n"
              "if (root / 'BAD').exists():\n"
              "    print('found BAD')\n"
              "    sys.exit(1)\n"
              "sys.exit(0)\n")
    liar = "import sys\nsys.exit(0)\n"
    # Crashes only on the bad fixture, and names it — so exit status and the
    # name rule both read as a clean catch. Only the traceback check sees it.
    crasher = ("import os, pathlib, sys\n"
               "root = pathlib.Path(os.environ['KX_GATE_ROOT'])\n"
               "if (root / 'BAD').exists():\n"
               "    raise RuntimeError('BAD')\n"
               "sys.exit(0)\n")
    for label, body, expect_caught in (("a gate that reads its input", honest, False),
                                       ("a gate that ignores its input", liar, True),
                                       ("a gate that crashes on the bad fixture", crasher, True)):
        probe = {"argv": [], "names": "BAD",
                 "bad": {"scripts/check-probe.py": body, "BAD": "x"},
                 "good": {"scripts/check-probe.py": body}}
        GATE_PROBES["check-probe.py"] = probe
        try:
            fixture_root = _tree({**probe["bad"]})
            (fixture_root / "scripts").mkdir(exist_ok=True)
            reported = gates_reject_and_accept(fixture_root)
            caught = bool([p for p in reported
                           if "exited 0 on a tree built to violate" in p
                           or "crashed on its" in p])
        finally:
            GATE_PROBES.pop("check-probe.py")
        if caught != expect_caught:
            print(f"  WRONG       {label} — {'caught' if caught else 'missed'}, expected "
                  f"{'a catch' if expect_caught else 'clean'}")
            failures += 1
        else:
            print(f"  {'caught  ' if expect_caught else 'allowed '}  {label}")

    # The half that a table emptied of cases does not reach: a case entered and
    # left without recording anything. Emptying the table is caught by the case
    # floor below; a single control slipping out of the loop is not, and the
    # closed half looks like the whole thing.
    silent = seen - accounted
    if silent:
        print(f"  SILENT SKIP {silent} control case(s) left the loop without proving or "
              f"failing anything, so this run licenses nothing.")
        failures += 1

    return failures + _report_control_total(proven)


def run(root: pathlib.Path = ROOT) -> int:
    # Cleared, not accumulated. A check that returns before setting its
    # denominator would otherwise print a stale one from an earlier call —
    # a count that describes a different run, which is the accurate-as-of
    # defect this repository keeps finding in prose, in a number.
    EXAMINED.clear()
    failed = 0
    for label, check in CHECKS:
        problems = check(root)
        n = EXAMINED.get(label)
        # The denominator, always. A check that found nothing to look at and a
        # check that looked at everything print the same clean line without it,
        # and the first is the one that needs saying.
        floor = MINIMUM_EXAMINED.get(label)
        seen = "" if n is None else f"  [{n} examined]"
        if problems:
            failed += 1
            print(f"FAIL  {label}:{seen}")
            for p in problems:
                print(f"        {p}")
        elif floor is not None and n is None:
            # A missing denominator is not a denominator of zero. It is a check
            # that did not say, and the difference matters in the direction that
            # bites: `n is not None` in the comparison below made an absent count
            # SKIP the floor and print a clean line, so the one value that means
            # "nothing was recorded" was the one value no floor applied to.
            #
            # Every check sets its count today, which is why this has never
            # fired — and unreachable-because-something-else-is-true is the
            # argument rather than the guarantee.
            failed += 1
            print(f"FAIL  {label}:")
            print(f"        reported clean without recording how much it examined, so its "
                  f"floor of {floor} could not be applied to anything. A count that was "
                  f"never taken has to read as a violation, not as an exemption.")
        elif n is not None and floor is not None and n < floor:
            # Not a warning. A clean verdict over a corpus this small is the
            # thing the floor exists to refuse, so it exits the way a violation
            # does.
            failed += 1
            print(f"FAIL  {label}:{seen}")
            print(f"        examined {n}, below the floor of {floor}. The corpus this "
                  f"invariant reads has fallen away, so a clean verdict over it says "
                  f"nothing. Restore what it reads, or lower the floor deliberately.")
        else:
            print(f"  ok  {label}{seen}")
    return failed


def _report_control_total(ran: int) -> int:
    """The denominator for the controls, printed and gated on."""
    print(f"\n  {ran} control case(s) ran.")
    if ran < MINIMUM_CONTROL_CASES:
        print(f"  BELOW FLOOR the control table holds {ran} case(s), under the floor of "
              f"{MINIMUM_CONTROL_CASES}. Every verdict below rests on these, so a table this "
              f"small cannot license them.")
        return 1
    return 0


def main() -> int:
    # Before the controls, not inside the checks. The controls build fixture
    # repositories and the checks scope to the tracked set, so git is upstream of
    # both: its absence is a fact about this entire run rather than about one
    # invariant, and discovering it partway through means a traceback out of
    # whichever call reached it first.
    err = require_git(ROOT)
    if err:
        print(f"check-slice-integrity: {err}", file=sys.stderr)
        print("Refusing to report on a population that cannot be determined.", file=sys.stderr)
        return 1
    bad = controls()
    if bad:
        print(f"\ncheck-slice-integrity: {bad} control(s) wrong. Refusing to report on the "
              f"tree with a gate that has not proven it rejects.")
        return 1
    print()
    if run():
        print("\ncheck-slice-integrity: the tree does not hold.")
        return 1
    print(f"\ncheck-slice-integrity: {len(CHECKS)} invariant(s) hold, each proven to reject.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
