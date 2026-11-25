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

import ast
import contextlib
import io
import json
import os
import pathlib
import py_compile
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import yaml

# Overridable so the suite-wide floor can point a gate at a fixture it wrote and
# observe the real exit status instead of asking the gate about itself.
ROOT = pathlib.Path(os.environ.get("KX_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parent.parent)

# `helm` as a command word: not part of a path, a longer word, or a flag.
HELM_MENTION = re.compile(r"(?<![\w./-])helm(?![\w-])")

# Where one command ends and the next begins. Splitting on these and reading
# each segment's first word is linear; expressing the same thing as one pattern
# needs a starred group whose body can also match the separator, which is
# exponential on input that alternates the two.
COMMAND_SEP = re.compile(r"\$\(|&&|\|\||[|;&()]")

# A leading `NAME=value`, which is a prefix to the command rather than the
# command. Anchored and matched against a single whitespace-delimited token, so
# it cannot overlap the token that follows it.
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A helm verb: the word directly after the binary.
VERB = re.compile(r"^[a-z][a-z-]*$")

# helm verbs that create, change or remove a release. Each blocks on the
# cluster and so has to say how long it will wait.
HELM_STATEFUL = {"upgrade", "install", "uninstall", "rollback", "delete"}

# helm verbs that cannot change a release. Listed rather than inferred: a verb
# absent from both sets fails the check by name, so the set that decides whether
# a command needs bounding can only grow by an edit somebody reviews.
HELM_READ_ONLY = {
    "repo", "template", "search", "show", "version", "lint", "dependency",
    "history", "status", "get", "list", "plugin", "env", "completion", "package",
    "verify", "pull", "inspect",
}


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(1-indexed first line, command) with `\\`-continuations folded into one.

    Folding first is what makes the classification below total. A recogniser
    applied per physical line cannot see a command whose verb and flags sit on
    different lines, and it drops that command from its denominator without
    saying so — which reads exactly like a tree that has no such command.
    """
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        start, parts = i + 1, []
        while i < len(lines):
            stripped = lines[i].rstrip()
            parts.append(stripped.rstrip("\\").rstrip())
            if not stripped.endswith("\\"):
                break
            i += 1
        out.append((start, " ".join(p for p in parts if p)))
        i += 1
    return out


QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')


def strip_quoted(line: str) -> str:
    """The line with quoted bodies emptied, so prose inside a string is not read
    as shell. `echo "(helm does not upgrade them)"` otherwise presents `helm`
    after a `(` — a command position — and classifies as a helm verb `does`.
    Flag names and operators live outside quotes, so nothing this check reads is
    lost.
    """
    return QUOTED.sub('""', line)


def helm_verb(segment: str) -> str | None:
    """The helm verb this command segment invokes, or None if it invokes no helm.

    Tokenised rather than matched as one pattern. Leading `NAME=value` prefixes
    and `exec` are dropped a token at a time, which is linear in the segment and
    cannot backtrack — the same shape written as a starred group inside a larger
    pattern is exponential on a line alternating assignments and separators. So is
    `deadline N`, the sourced helper every chart-fetching helm call runs under. Its
    program form, which a Taskfile uses, is not read here: an installer sources
    the helper, and a line in another form is reported as one this cannot read.
    """
    tokens = segment.split()
    i = 0
    while i < len(tokens):
        if ENV_ASSIGN.match(tokens[i]) or tokens[i] == "exec":
            i += 1
        elif tokens[i] == "deadline" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            i += 2
        else:
            break
    if i >= len(tokens) or tokens[i] != "helm":
        return None
    if i + 1 >= len(tokens) or not VERB.match(tokens[i + 1]):
        return None
    return tokens[i + 1]


def helm_commands(text: str) -> tuple[list[tuple[int, str, str]], list[tuple[int, str]]]:
    """Every helm command, as (line, verb, command), and every one not parsed.

    The second list is the point. A recogniser matching one spelling reports a
    clean verdict over the commands it happens to match and says nothing about
    the rest, so its denominator shrinks silently — a gate cannot report a blind
    spot using the instrument that has it. Here the broad question (does this
    line invoke helm) and the precise one (which verb) are asked separately, and
    a line where the answers disagree is returned for the caller to fail on.
    """
    found, unparsed = [], []
    for line, raw in logical_lines(text):
        cmd = strip_quoted(raw)
        if not HELM_MENTION.search(cmd):
            continue
        verbs = [v for seg in COMMAND_SEP.split(cmd) if (v := helm_verb(seg))]
        if not verbs:
            unparsed.append((line, raw.strip()))
            continue
        for verb in verbs:
            found.append((line, verb, cmd))
    return found, unparsed


def helm_invocations(text: str) -> list[tuple[int, str]]:
    """Each release-changing helm command as (1-indexed line, folded command).

    Scoped to the command rather than the file: testing the file for `--timeout`
    would accept a helm call with none beside a `kubectl wait --timeout=300s`
    that has nothing to do with it.
    """
    found, _ = helm_commands(text)
    return [(line, cmd) for line, verb, cmd in found if verb in HELM_STATEFUL]


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
    "every helm install is found, and names a timeout": 20,
    "no helm repo add swallows its own failure": 18,
    "every shell script runs on bash 3.2": 30,
    "every scrape surface a values file names is enabled": 1,
    "every file in an addon directory is applied": 25,
    "every gate is observed to reject and to accept": 4,
    "every path named in markdown resolves": 5,
    # Unconditional: one file is the law, not a count sized to this tree.
    "the tree holds content outside the gate directory": 1,
    "every gate refuses when its authority is unavailable": 5,
    # Sized to the corpus, not to the finding: every tracked shell script, which
    # the run prints as its denominator, under a floor only a collapse can breach.
    "no script hands a secret to a command on argv": 30,
    # Three files both fetch and consume. Two is a floor a rename cannot pass
    # through quietly while leaving the class unexamined.
    "every fetched artifact is verified before it is used": 2,
    # Every tracked Python file, the gates and the helpers installers run, which
    # the run prints as its denominator.
    "every Python file compiles": 9,
    # The shell scripts and Taskfiles together, which the run prints as its
    # denominator.
    "no shell script runs Python from a string or stdin": 40,
    # Counts every script and Taskfile command that reaches the cluster, which
    # the run prints as its denominator; set well under it, like the others.
    "nothing kx runs can reach a cluster other than kind-kx": 70,
    # Unconditional: both ports, or the check did not look.
    "localhost:80 and :443 reach Cilium's Ingress": 2,
    # Counts every outbound command and subprocess call, which the run prints;
    # set well under it.
    "every call that leaves this machine is bounded": 200,
}

# The suite itself is a corpus and can collapse the same way. With CHECKS and
# CONTROLS both emptied, every per-check control has nothing to report on and
# the run loop has nothing to iterate, so the whole gate exits 0 having asserted
# nothing at all.
MINIMUM_CHECKS = 16

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
        rel = script.relative_to(root)
        found, unparsed = helm_commands(strip_comments(script.read_text()))

        # A line that invokes helm in a form this parser cannot read is not a
        # line without a helm command, and the two must not be recorded the
        # same way. Reported rather than skipped, because a skip removes it
        # from the denominator and the clean verdict then covers one command
        # fewer with nothing to say so.
        for line, raw in unparsed:
            problems.append(
                f"{rel}:{line} invokes helm in a form this check cannot read, so whether it "
                f"is bounded was never asked: {raw[:80]}"
            )

        for line, verb, cmd in found:
            if verb not in HELM_STATEFUL and verb not in HELM_READ_ONLY:
                problems.append(
                    f"{rel}:{line} runs `helm {verb}`, which is in neither the set of verbs "
                    f"that change a release nor the set that cannot. Whether it needs a "
                    f"--timeout is undecided, so it is reported rather than assumed harmless."
                )
                continue
            if verb not in HELM_STATEFUL:
                continue
            checked += 1
            if not names_token(cmd, "--timeout"):
                problems.append(
                    f"{rel}:{line} runs `helm {verb}` with no --timeout, so it takes helm's "
                    f"implicit 5m — short enough that a cold image pull aborts the slice."
                )
    EXAMINED["every helm install is found, and names a timeout"] = checked
    if not checked:
        problems.append("no install.sh runs a release-changing helm command — the parser and "
                        "the tree disagree, so this check asserted nothing.")
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

# Prefix for a fixture file that is written but not staged. A sibling repository
# checked out into the workspace is present on disk and absent from the tracked
# set, and a fixture that stages it is modelling a different situation than the
# one the gate was scoped for.
UNTRACKED = "!"


OPERATOR_IMAGE = DEPLOY_WITH % "ghcr.io/nanohype/eks-agent-platform/operator:dev"

# The pair check-images holds to one release, by the names its RELEASED_TOGETHER
# table carries. Every check-images fixture renders the pair, for the reason it
# renders the operator image: the gate asserts each rule it carries still has a
# subject in the render it is handed, and a render without the pair fails that
# assertion whatever else it holds.
RELEASED_PAIR = (DEPLOY_WITH % "reg.kyverno.io/kyverno/kyverno:v1.0.0" + "---\n"
                 + DEPLOY_WITH % "ghcr.io/kyverno/readiness-checker:%s")

# The operator Deployment as a render carries it: the image is what identifies it
# among every other Deployment in the stack, and the argument list is the subject.
OPERATOR_DEPLOY_WITH_ARGS = """apiVersion: apps/v1
kind: Deployment
metadata: {name: op}
spec:
  template:
    spec:
      containers:
        - name: c
          image: ghcr.io/nanohype/eks-agent-platform/operator:dev
          args: [%s]
"""

# The manifest shape the flag gate reads, built from the keys it names rather
# than copied from the shipped file, which would make the fixture a second copy
# of a thing that moves.
OPERATOR_FLAG_MANIFEST = json.dumps(
    {
        "upstream": {"repository": "example/upstream", "path": "applicationsets",
                     "ref": "0" * 40},
        "siblings": {
            "eks-agent-platform": {
                "repository": "example/sibling",
                "ref": "0" * 40,
                "used_by": "stack/ai-platform/operator",
                "flagSource": "operators/cmd/main.go",
            }
        },
    },
    indent=2,
) + "\n"

# The shape a single pattern for the command head backtracks on: an assignment,
# then a separator, repeated. Long enough that an exponential parser does not
# finish inside the floor's ceiling, short enough to read.
ADVERSARIAL_PREFIX = 'A=' + '"" A='.join([""] * 40) + '"" helm template x y >/dev/null\n'

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
#
# Keyed by the gate's filename, or by any label when the probe names its script
# in `gate`. A gate holding more than one property takes a probe per property:
# one probe plants one violation, and a gate that stopped checking the second
# property would still reject the first probe's tree.
GATE_PROBES: dict[str, dict] = {
    "check-images.py": {
        "argv": ["--pins", "{root}/render"],
        "names": "r/x:latest",
        # Both trees carry the operator image, because the gate asserts its
        # own exemptions against the render it is handed and an exemption whose
        # subject is absent is a finding in its own right. The fixture differs
        # in exactly the property under test and nothing else.
        "bad": {"render/s.yaml": DEPLOY_WITH % "r/x:latest" + "---\n" + OPERATOR_IMAGE
                + "---\n" + RELEASED_PAIR % "v1.0.0"},
        "good": {"render/s.yaml": DEPLOY_WITH % "r/x:1.2.3" + "---\n" + OPERATOR_IMAGE
                 + "---\n" + RELEASED_PAIR % "v1.0.0"},
    },
    "check-images.py (hand pins)": {
        "gate": "check-images.py",
        "argv": ["--pins", "{root}/render"],
        "names": "readiness-checker renders at v0.1.0",
        # Every reference immutable on both trees, so the only thing left to
        # reject is a hand-pinned tag naming a different release from the
        # chart's own image.
        "bad": {"render/s.yaml": OPERATOR_IMAGE + "---\n" + RELEASED_PAIR % "v0.1.0"},
        "good": {"render/s.yaml": OPERATOR_IMAGE + "---\n" + RELEASED_PAIR % "v1.0.0"},
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
    "check-operator-flags.py": {
        "argv": ["--render", "{root}/render", "--chart-source", "{root}/.sibling"],
        "names": "--disable-aws",
        # The render and the checkout are UNTRACKED: neither is part of a tree
        # under test, and a fixture that stages them models a situation the gate
        # was not scoped for.
        # Two flag definitions on both sides, not one. With a single definition
        # "parsed one flag" and "parsed correctly" are the same observation, so
        # the empty-extraction refusal would be what the good tree exercised.
        # The planted violation is a RENAME rather than a deletion, so the near-
        # miss line has something to print — a rejection that merely says the
        # flag is gone is a weaker report of the same event.
        "bad": {
            "stack/upstream.json": OPERATOR_FLAG_MANIFEST,
            "stack/ai-platform/operator/values.yaml": "extraArgs:\n  - --disable-aws\n",
            UNTRACKED + "render/op.yaml": OPERATOR_DEPLOY_WITH_ARGS % "--disable-aws",
            UNTRACKED + ".sibling/operators/cmd/main.go":
                'flag.BoolVar(&awsDisabled, "aws-disabled", false, "")\n'
                'flag.BoolVar(&leaderElect, "leader-elect", true, "")\n',
        },
        "good": {
            "stack/upstream.json": OPERATOR_FLAG_MANIFEST,
            "stack/ai-platform/operator/values.yaml": "extraArgs:\n  - --disable-aws\n",
            UNTRACKED + "render/op.yaml": OPERATOR_DEPLOY_WITH_ARGS % "--disable-aws",
            UNTRACKED + ".sibling/operators/cmd/main.go":
                'flag.BoolVar(&disableAWS, "disable-aws", false, "")\n'
                'flag.BoolVar(&leaderElect, "leader-elect", true, "")\n',
        },
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
        "its verdict needs kubeconform and the schemas vendored under schemas/, and a "
        "fixture tree could carry them only by copying megabytes of upstream documents "
        "into every probe",
    "vendor-schemas.py":
        "it is the tool that writes schemas/ rather than a gate over the tree, and its one "
        "verdict, --check, fetches each pinned release from proxy.golang.org and "
        "sum.golang.org, so a probe would make this floor non-hermetic",
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

    def script_of(name: str, probe: dict) -> str:
        return probe.get("gate", name)

    for name, probe in sorted(GATE_PROBES.items()):
        if script_of(name, probe) not in present:
            problems.append(
                f"GATE_PROBES names scripts/{script_of(name, probe)}, which is not in the tree — "
                f"a probe that outlasts its gate proves nothing about whatever replaced it."
            )

    observed = 0
    for name, probe in sorted(GATE_PROBES.items()):
        if script_of(name, probe) not in present:
            continue
        if "names" not in probe:
            problems.append(f"{name}: its probe declares no `names`, so a rejection for any "
                            f"reason at all would score as a catch.")
            continue
        if probe["bad"] == probe["good"]:
            problems.append(f"{name}: the probe's bad and good trees are identical, so running "
                            f"them proves nothing.")
            continue
        gate = scripts_dir / script_of(name, probe)
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

    probed = {script_of(name, probe) for name, probe in GATE_PROBES.items()}
    unprobed = [n for n in present
                if n not in probed and n not in UNPROBEABLE
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
    # This table asserts a non-zero exit and no traceback, and carries no
    # `names` field, so a probe is honest only where exactly one refusal is
    # plausible on the shared fixture. The gate resolves --chart-source ahead of
    # every other precondition for that reason: the fixture holds no operator
    # values file and an empty `siblings`, and neither is ever reached.
    "check-operator-flags.py": {
        "authority": "an eks-agent-platform checkout",
        "argv": ["--render", "{root}", "--chart-source", "{root}/not-a-checkout"],
        "env": {},
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
        timeout=60,
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
        rel = script.relative_to(root)
        found, unparsed = helm_commands(strip_comments(script.read_text()))
        for n, raw in unparsed:
            problems.append(
                f"{rel}:{n} invokes helm in a form this check cannot read, so whether it "
                f"suppresses a failure was never asked: {raw[:80]}"
            )
        for n, verb, cmd in found:
            if verb != "repo" or not re.search(r"helm\s+repo\s+add\b", cmd):
                continue
            examined += 1
            # `||` in any spelling, not two literal suffixes. `|| true`, `|| :`,
            # `||true`, `|| echo ...` and `|| /bin/true` are the same construct
            # wearing different clothes, and a check listing the spellings it
            # knows scores every other one as compliant.
            swallowed = (
                "||" in cmd
                or "2>&1" in cmd
                or re.search(r";\s*(true|:)\s*$", cmd)
            )
            if swallowed:
                problems.append(
                    f"{rel}:{n} suppresses `helm repo add`'s failure. "
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
                            capture_output=True, text=True, check=False, timeout=60)
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
    seat that can fix it is not the one being stopped. A fixture tree is a git
    repository for the same reason, so this reads it the same way (see _tree).
    """
    return tracked_files(root, "*.sh")


def tracked_files(root: pathlib.Path, *patterns: str) -> list[pathlib.Path]:
    """The tracked files matching any of `patterns`, for the reason above."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *patterns],
                         capture_output=True, text=True, check=False, timeout=60)
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


# A secret handed to a command as an argument. `kubectl create secret
# --from-literal` is the shape this tree writes; the flag name is the subject
# because the leak is the flag, not the tool.
ARGV_SECRET = re.compile(r"--from-literal\b")


def secrets_stay_off_argv(root: pathlib.Path = ROOT) -> list[str]:
    """No script hands a secret to a command as an argument.

    A process's argv is world-readable on the platforms this workspace targets,
    so `ps` shows a `--from-literal=password=...` to every other process on the
    workstation for as long as the command runs. The value is a live credential
    in both places this tree creates one.

    The rule was already written here, in the comment above the installer that
    builds its Secret on stdin instead. A rule stated at the one site that
    honours it is a rule the next site does not inherit — which is what happened
    two directories away, where a Postgres password went onto argv under a
    comment explaining the translation and saying nothing about the flag.
    """
    problems = []
    scripts = tracked_shell_scripts(root)
    if not scripts:
        return ["found no tracked shell scripts — refusing to report every secret off argv "
                "over an empty set."]
    examined = 0
    for script in scripts:
        examined += 1
        for n, line in enumerate(strip_comments(script.read_text()).splitlines(), 1):
            if ARGV_SECRET.search(line):
                problems.append(
                    f"{script.relative_to(root)}:{n} passes a value with --from-literal, which "
                    f"puts it in this process's argv where `ps` shows it to every other process "
                    f"on the machine. Build the Secret on stdin instead."
                )
    EXAMINED["no script hands a secret to a command on argv"] = examined
    return problems


# A fetch that lands in a file, and the two things this tree then does with one.
FETCHES = re.compile(r"(?<![\w-])curl\b")
CONSUMES = re.compile(r"tar\s+-[a-zA-Z]*x|kubectl\s+apply\b")
VERIFIES = re.compile(r"sha256sum\s+-c|shasum\s+-a\s+256\s+-c")

# `kubectl apply -f <url>` fetches and applies in one step. There is no moment
# between the two in which anything could check what arrived, so this shape is
# the finding rather than a candidate for one — and it is invisible to the pair
# above, which look for a fetch and a use as separate acts.
APPLIES_A_URL = re.compile(r"kubectl\s+apply\b.*?-f\s+\"?https?://")


def applies_a_url(text: str) -> bool:
    """Whether any command applies a manifest straight from a URL.

    Read over folded logical lines, because the invocation this exists for wrote
    its flags on the line after the verb. A pattern that cannot cross a
    continuation drops that command from its corpus and reports the file clean,
    which is the same defect the helm invariants above were widened to remove.
    """
    return any(APPLIES_A_URL.search(cmd) for _, cmd in logical_lines(strip_comments(text)))


def remote_artifacts_are_verified(root: pathlib.Path = ROOT) -> list[str]:
    """Anything downloaded and then unpacked or applied is checked first.

    Two of these run before anything else does: a binary unpacked onto PATH and
    used by a blocking gate, and the CRDs defining every route kind in the
    cluster, applied on the core path of every bring-up. Fetching and consuming
    in one step means a truncated transfer, a proxy error page or a replaced
    asset arrives as the thing itself.

    Scoped to files that BOTH fetch and consume, so a script that only downloads
    something a human reads is not asked for a digest it has no use for. The
    bound is worth stating: this asks whether a file verifies, not whether it
    verifies the right artifact — a digest recorded for one download does not
    vouch for a second one added beside it. What it makes impossible is the
    shape where nothing is checked at all.
    """
    problems = []
    err = require_git(root)
    if err:
        return [err]
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         capture_output=True, text=True, check=False, timeout=60)
    candidates = []
    for rel in (f for f in out.stdout.split("\0") if f):
        if not (rel.endswith(".sh") or rel.endswith(".yml") or rel.endswith(".yaml")):
            continue
        f = root / rel
        if not f.is_file():
            continue
        text = f.read_text(errors="ignore")
        if applies_a_url(text) or (
            FETCHES.search(text) and CONSUMES.search(text)
        ):
            candidates.append((rel, text))
    EXAMINED["every fetched artifact is verified before it is used"] = len(candidates)
    if not candidates:
        return ["no tracked file both fetches and unpacks or applies an artifact — the "
                "parser and the tree disagree, so this check asserted nothing."]
    for rel, text in candidates:
        direct = applies_a_url(text)
        if direct:
            problems.append(
                f"{rel} applies a manifest straight from a URL, so there is no point between "
                f"fetching and applying at which anything could check what arrived. Fetch it "
                f"to a file, verify a recorded digest, then apply the file."
            )
            continue
        if not VERIFIES.search(text):
            problems.append(
                f"{rel} downloads an artifact and then unpacks or applies it without checking "
                f"a digest, so whatever the host served is what runs."
            )
    return problems


def python_files_compile(root: pathlib.Path = ROOT) -> list[str]:
    """Every tracked Python file compiles.

    Compiled by the interpreter running this suite, with py_compile, rather than
    only parsed by the linter: the interpreter is the authority on what it will
    accept, and an installer's helper meets nothing else before a workstation
    runs it. The bytecode is written to a scratch directory, so the tree is left
    exactly as it was found.

    The class this closes shipped once: an installer step whose program sat in a
    shell string, out of reach of every parser, with a backslash inside an
    f-string expression that no Python release accepts. The step failed on the
    first workstation that ran it. The companion invariant below keeps programs
    out of shell strings; this one is what reads them once they are files.
    """
    problems = []
    files = tracked_files(root, "*.py")
    if not files:
        return ["found no tracked Python file — refusing to report every one compiling over "
                "an empty set."]
    version = sys.version.split()[0]
    with tempfile.TemporaryDirectory(prefix="kx-pycompile-") as scratch:
        for n, f in enumerate(files):
            try:
                py_compile.compile(str(f), cfile=os.path.join(scratch, f"{n}.pyc"),
                                   doraise=True)
            except py_compile.PyCompileError as e:
                where = getattr(e.exc_value, "lineno", None)
                at = f":{where}" if where else ""
                problems.append(f"{f.relative_to(root)}{at} does not compile under python "
                                f"{version}: {e.exc_type_name}: "
                                f"{getattr(e.exc_value, 'msg', e.exc_value)}")
    EXAMINED["every Python file compiles"] = len(files)
    return problems


# `python`, `python3` or `python3.N` as a command word.
PYTHON_MENTION = re.compile(r"(?<![\w./-])python(?:3(?:\.\d+)?)?(?![\w.-])")

# Words that may stand before a command without being it, each with a shape it
# opens, CMD marking where the command goes: a reserved word that opens a
# command list or a pipeline, and a precommand that runs its operand. A word
# missing here hides the command behind it from every check that reads command
# positions, so each shape is also a control, and a word is proven by the shape
# added with it.
COMMAND_OPENERS = {
    "if": "if CMD; then :; fi",
    "elif": "if false; then :; elif CMD; then :; fi",
    "then": "if true; then CMD; fi",
    "else": "if false; then :; else CMD; fi",
    "while": "while CMD; do :; done",
    "until": "until CMD; do :; done",
    "do": "for x in a; do CMD; done",
    "!": "! CMD",
    "{": "f() { CMD; }",
    "exec": "exec CMD",
    "command": "command CMD",
    "time": "time CMD",
    "env": "env A=1 CMD",
}

# The openers, and the `- ` of a Taskfile's cmds list.
COMMAND_PREFIX_WORDS = set(COMMAND_OPENERS) | {"-"}

# Interpreter options that take the next word as their value, so that word is
# not the program.
PYTHON_VALUE_OPTIONS = {"--check-hash-based-pycs"}

# Tokens that end the python command with no program named.
COMMAND_ENDS = {"|", "||", "&", "&&", ";", ";;", ")", "("}

# Redirections that make stdin the program when no script came first.
STDIN_REDIRECTS = {"<", "<<", "<<-", "<<<"}

# What code_view writes over a literal's text: a word character, so a quoted
# word stays one word, and one that spells no command a check looks for.
BLANK = "_"


def code_view(line: str) -> str:
    """The line with the text of every string literal blanked and all code kept.

    Not strip_quoted: a command substitution inside double quotes is code, and
    `KEY="$(printf %s "$J" | python3 -c ...)"` is the shape an installer writes
    most often. Blanking every double-quoted body would hide exactly that
    invocation. Blanked rather than removed, so an offset in this view is an
    offset in the original line, and blanked with a character that is not
    whitespace, so a quoted word stays one word: `FOO='a b' python3` is an
    assignment and then a command, and blanking `a b` to spaces would split the
    assignment and leave python3 reading as an argument.
    """
    out: list[str] = []
    stack: list[list] = [["code", 0]]
    i = 0
    while i < len(line):
        c = line[i]
        kind = stack[-1][0]
        if kind == "single":
            out.append(c if c == "'" else BLANK)
            if c == "'":
                stack.pop()
            i += 1
        elif kind == "double":
            if c == "\\" and i + 1 < len(line):
                out.append(BLANK * 2)
                i += 2
            elif c == '"':
                stack.pop()
                out.append(c)
                i += 1
            elif line.startswith("$(", i):
                stack.append(["code", 0])
                out.append("$(")
                i += 2
            else:
                out.append(BLANK)
                i += 1
        elif c == "\\" and i + 1 < len(line):
            out.append(line[i:i + 2])
            i += 2
        else:
            if c == "'":
                stack.append(["single"])
            elif c == '"':
                stack.append(["double"])
            elif c == "(":
                stack[-1][1] += 1
            elif c == ")":
                if stack[-1][1] > 0:
                    stack[-1][1] -= 1
                elif len(stack) > 1:
                    stack.pop()
            out.append(c)
            i += 1
    return "".join(out)


# The program form of the deadline helper, as a Taskfile calls it.
DEADLINE_PROGRAM = "/cluster/deadline.sh"


def command_context(code_prefix: str, raw_prefix: str | None = None) -> tuple[bool, int | None]:
    """(is the next word a command, the deadline it runs under or None) after a prefix.

    `code_prefix` is a code view and `raw_prefix` the same text as written, which
    code_view keeps aligned with it. Separators and word boundaries come from the
    code view, so a `|` inside a string is not one and `NAME='a b'` is one word;
    each word's text comes from the raw prefix, because a runner's own arguments
    can be quoted — `bash '/repo/cluster/deadline.sh' 60`.

    A word is a command when every word before it in its segment is a prefix —
    an assignment, exec, command, time, a keyword, a Taskfile list's `-` — or a
    runner: `xargs` with its flags, and the deadline helper with its limit,
    called as the sourced function or as the program. Reading through a runner
    is what keeps the command it runs in view; a check that stopped at `xargs`
    would accept whatever followed it.
    """
    raw_prefix = code_prefix if raw_prefix is None else raw_prefix
    segment = COMMAND_SEP.split(code_prefix)[-1]
    start = len(code_prefix) - len(segment)
    # Word boundaries from the code view, where a quoted value is one unbroken
    # word, and each word's text from the raw prefix at the same span.
    words = [raw_prefix[start + w.start():start + w.end()] for w in re.finditer(r"\S+", segment)]
    i, limit = 0, None
    while i < len(words):
        word = words[i]
        if word in COMMAND_PREFIX_WORDS or ENV_ASSIGN.match(word):
            i += 1
        elif word == "xargs":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        elif word == "deadline" and i + 1 < len(words) and words[i + 1].isdigit():
            limit, i = int(words[i + 1]), i + 2
        elif (word == "bash" and i + 2 < len(words)
              and words[i + 1].strip("'\"").endswith(DEADLINE_PROGRAM)
              and words[i + 2].isdigit()):
            limit, i = int(words[i + 2]), i + 3
        else:
            return False, limit
    return True, limit


def in_command_position(code_prefix: str, raw_prefix: str | None = None) -> bool:
    """Whether a word following `code_prefix` (a code view) is the command word."""
    return command_context(code_prefix, raw_prefix)[0]


# What a single-letter interpreter option says about where the program comes
# from. Any other letter is a flag that leaves the question open.
PYTHON_LETTER_SOURCES = {"c": "string", "m": "module", "V": "info", "h": "info"}


def python_option(word: str) -> tuple[str | None, int]:
    """One interpreter option: where it says the program is, if it says, and the
    number of words it occupies."""
    if word.startswith("--"):
        if word.startswith(("--version", "--help")):
            return "info", 1
        return None, 2 if word in PYTHON_VALUE_OPTIONS else 1
    for j, letter in enumerate(word[1:]):
        if letter in PYTHON_LETTER_SOURCES:
            return PYTHON_LETTER_SOURCES[letter], 1
        if letter in "WX":
            # Takes a value: the rest of this word, or else the next word.
            return None, 2 if j + 2 == len(word) else 1
    return None, 1


def python_program(rest: str) -> str:
    """Where the python command whose arguments start at `rest` gets its program.

    One of "file", "module" or "info" (a version or help query, which runs no
    program), each of which is fine, or "string" or "stdin", which are the
    finding. Read with shlex, which keeps a quoted script path whole. An unclosed
    quote, which is what `-c '` leaves on the line that opens a multi-line
    program, ends the reading, and the words before it still decide.
    """
    lex = shlex.shlex(rest, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    words: list[str] = []
    with contextlib.suppress(ValueError):
        words.extend(lex)
    source, i = "stdin", 0
    while i < len(words):
        word = words[i]
        if word in COMMAND_ENDS or word in STDIN_REDIRECTS or word == "-":
            break
        if word == "--":
            if i + 1 < len(words) and words[i + 1] not in COMMAND_ENDS:
                source = "file"
            break
        if not word.startswith("-"):
            source = "file"
            break
        said, width = python_option(word)
        if said:
            source = said
            break
        i += width
    return source


def python_runs_from_files(root: pathlib.Path = ROOT) -> list[str]:
    """No shell script or Taskfile runs Python from a string or from stdin.

    A program handed to python3 with -c, or on stdin through a heredoc, lives in
    a shell string: the linter does not read it, nothing compiles it, and it can
    contain no single quote, so it bends around the quoting instead. Workstation
    code is where that matters, because nothing in CI runs an installer — the
    first parser such a program meets is the one on the machine of whoever enables
    the slice. Moved into a .py file, it is linted by the lint job and compiled
    by the invariant above.

    Scoped to tracked shell scripts and Taskfiles, which are the workstation
    side. The workflows are not in scope: every command in them runs on every
    pull request, so a program there cannot rot unseen.
    """
    problems = []
    files = tracked_files(root, "*.sh", "*Taskfile.yaml")
    if not files:
        return ["found no tracked shell script or Taskfile — refusing to report every python3 "
                "invocation running a file over an empty set."]
    for f in files:
        for n, cmd in logical_lines(strip_comments(f.read_text())):
            code = code_view(cmd)
            for m in PYTHON_MENTION.finditer(code):
                if not in_command_position(code[:m.start()], cmd[:m.start()]):
                    continue
                source = python_program(cmd[m.end():])
                if source in ("string", "stdin"):
                    how = "passed with -c" if source == "string" else "read from stdin"
                    problems.append(
                        f"{f.relative_to(root)}:{n} runs a Python program {how}, which no "
                        f"linter reads and nothing compiles. Move it into a .py file beside "
                        f"the script and run that file."
                    )
    EXAMINED["no shell script runs Python from a string or stdin"] = len(files)
    return problems


# The binding every script that reaches the cluster sources, and the prefix every
# Taskfile command that reaches it carries. Both name kx's own kubeconfig, which
# holds the kind-kx context alone.
KUBECONFIG_BINDING = "cluster/kubeconfig.sh"
KUBECONFIG_PREFIX = "KUBECONFIG='{{.ROOT_DIR}}/.kube/config'"
KUBECONFIG_EXPORT = "export " + KUBECONFIG_PREFIX
SOURCES_BINDING = re.compile(r'^\s*(?:\.|source)\s+"\$\{SCRIPT_DIR\}/([^"]+)"\s*$')

# kubectl, kind or helm as a command word. A colon after the word is YAML — a
# heredoc's `kind: ConfigMap` — not a command.
CLUSTER_TOOL = re.compile(r"(?<![\w./-])(kubectl|kind|helm)(?![\w.:-])")

# helm verbs that work from the chart and the local cache alone. Every other
# verb, including a read like `list` or `status`, asks a cluster.
HELM_CLUSTERLESS = {
    "repo", "template", "search", "show", "version", "lint", "dependency", "plugin", "env",
    "completion", "package", "verify", "pull", "inspect",
}


def cluster_commands(text: str) -> list[tuple[int, int, str]]:
    """Every command in shell `text` that reaches a cluster, as (line, offset, folded line).

    The offset is where the tool's name starts in the folded line, so a caller can
    read the command it belongs to.
    """
    found = []
    for n, cmd in logical_lines(strip_comments(text)):
        code = code_view(cmd)
        for m in CLUSTER_TOOL.finditer(code):
            if not in_command_position(code[:m.start()], cmd[:m.start()]):
                continue
            if m.group(1) == "helm":
                after = code[m.end():].split()
                if after and after[0] in HELM_CLUSTERLESS:
                    continue
            found.append((n, m.start(), cmd))
    return found


def scripts_bind_their_kubeconfig(root: pathlib.Path = ROOT) -> list[str]:
    """Nothing kx runs can reach a cluster other than kind-kx.

    kx keeps its cluster's kubeconfig in .kube/config at the repository root, and
    every command that reaches a cluster is pointed at that file rather than at
    whatever the shell's KUBECONFIG or current context names. Without that, a
    `task stack:data:disable` run in a shell pointed at a production cluster
    runs its `helm uninstall` and `kubectl delete ns` there, and the
    bedrock-credentials installer writes a profile's AWS keys into it.

    Two shapes, because Task cannot bind its children: a Taskfile's `env:` never
    overrides a variable the shell already exports.

    - A shell script sources cluster/kubeconfig.sh before its first such command,
      by a path that resolves to that file. The binding points KUBECONFIG at kx's
      file and stops the script unless the context there is kind-kx, so it also
      holds when the script is run without task.
    - A Taskfile command carries KUBECONFIG='{{.ROOT_DIR}}/.kube/config' in the
      command itself, or exports it on an earlier line of the same block.
      ROOT_DIR is task's own path to the root Taskfile, which the environment
      cannot override; run any other way it names a file that does not exist,
      and kubectl refuses rather than falling back to the user's kubeconfig.

    A command that reaches a cluster is kubectl, kind, or a helm verb other than
    the ones that work from the chart and the local cache alone.
    """
    problems = []
    binding = root / KUBECONFIG_BINDING
    examined = 0
    for script in tracked_files(root, "*.sh"):
        if script == binding:
            continue
        lines = strip_comments(script.read_text()).splitlines()
        reaches = cluster_commands(script.read_text())
        if not reaches:
            continue
        examined += 1
        first = reaches[0][0]
        bound = None
        for n, line in enumerate(lines, 1):
            m = SOURCES_BINDING.match(line)
            if m and (script.parent / m.group(1)).resolve() == binding.resolve():
                bound = n
                break
        rel = script.relative_to(root)
        if bound is None:
            problems.append(f"{rel}:{first} reaches a cluster, and the script never sources "
                            f"{KUBECONFIG_BINDING} by a path that resolves to it, so it acts on "
                            f"whatever cluster the shell points at.")
        elif bound > first:
            problems.append(f"{rel}:{first} reaches a cluster before line {bound} sources "
                            f"{KUBECONFIG_BINDING}, so that command acts on whatever cluster "
                            f"the shell points at.")
    if not binding.is_file() and examined:
        problems.append(f"{KUBECONFIG_BINDING} does not exist, so no script can be bound by it.")
    for taskfile in tracked_files(root, "*Taskfile.yaml"):
        tasks = (yaml.safe_load(taskfile.read_text()) or {}).get("tasks") or {}
        for name, task in tasks.items():
            for cmd in (task or {}).get("cmds") or [] if isinstance(task, dict) else []:
                text = cmd.get("cmd") if isinstance(cmd, dict) else cmd
                if not isinstance(text, str):
                    continue
                reaches = cluster_commands(text)
                if not reaches:
                    continue
                examined += 1
                exported = next((n for n, line in enumerate(text.splitlines(), 1)
                                 if line.strip() == KUBECONFIG_EXPORT), None)
                for n, at, folded in reaches:
                    if exported is not None and exported < n:
                        continue
                    segment = COMMAND_SEP.split(folded[:at])[-1].split()
                    if KUBECONFIG_PREFIX not in segment:
                        problems.append(
                            f"{taskfile.relative_to(root)} task `{name}`: "
                            f"`{folded.strip()[:70]}` reaches a cluster without "
                            f"{KUBECONFIG_PREFIX}, so it acts on whatever cluster the shell "
                            f"points at."
                        )
    EXAMINED["nothing kx runs can reach a cluster other than kind-kx"] = examined
    if not examined:
        return ["found no script or Taskfile command that reaches a cluster — refusing to report "
                "every one bound over an empty set."]
    return problems


# The tools whose calls leave this machine, or wait on a daemon that can stop
# answering, as command words.
OUTBOUND_TOOL = re.compile(r"(?<![\w./-])(kubectl|helm|docker|kind|aws|curl|pnpm|npm|npx|yarn)(?![\w.:-])")

# helm verbs that ask a cluster and take no timeout of their own.
HELM_UNBOUNDED_READS = {"list", "status", "history", "get", "test"}

# helm verbs that move a chart to or from a repository or registry, each with a
# call that makes one, which is also its control: no verb joins the set without a
# control proving the check reads it. None bounds the transfer. The --timeout
# that install, upgrade and template take starts once the chart has arrived and
# covers the cluster operations after it, so a stalled fetch waits with no limit
# at all. repo add and repo update are not here, since each bounds its index
# download with --timeout, at two minutes by default.
HELM_TRANSFERS = {
    "install": "helm install a x/a --version 1 --wait --timeout 10m",
    "upgrade": "helm upgrade --install a x/a --version 1 --wait --timeout 10m",
    "template": "helm template a oci://r/c --version 1",
    "pull": "helm pull oci://r/c --version 1",
    "show": "helm show chart oci://r/c --version 1",
    "dependency": "helm dependency build ./chart",
    "push": "helm push c-1.tgz oci://r/o",
}

# kubectl's flags that take the next word as their value, so that word is not
# the verb.
KUBECTL_VALUE_FLAGS = {"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user",
                       "-l", "--selector", "-o", "--output", "--request-timeout", "--timeout"}

# A duration as kubectl and curl spell one: Go's `1h30m`, `90s` or `500ms`, or
# a bare number of seconds.
DURATION = re.compile(r"(\d+(\.\d+)?(h|m|s|ms|us|µs|ns))+|\d+(\.\d+)?")

DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(h|ms|m|s|us|µs|ns)")
DURATION_UNIT = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}


def duration_seconds(value: str) -> float | None:
    """`value` in seconds, or None when it is not a duration DURATION reads."""
    if not DURATION.fullmatch(value):
        return None
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    return sum(float(n) * DURATION_UNIT[unit] for n, unit in DURATION_PART.findall(value))

# The subprocess entry points that block until their child exits.
SUBPROCESS_BLOCKING = {"run", "call", "check_call", "check_output"}


def kubectl_words(rest: str) -> tuple[str, str]:
    """(verb, the word after it) of a kubectl command whose arguments start at `rest`."""
    words = rest.split()
    i = 0
    while i < len(words):
        word = words[i]
        if word in KUBECTL_VALUE_FLAGS:
            i += 2
        elif word.startswith("-") or word == "\\":
            i += 1
        else:
            return word, words[i + 1] if i + 1 < len(words) else ""
    return "", ""


def _bound(segment: str, flag: str, *aliases: str) -> str | None:
    """None when `segment` gives `flag` a positive duration, else why it does not.

    The last value given is the one read, as the tools themselves read a
    repeated flag. Zero is how kubectl's --request-timeout, rollout status's
    --timeout and curl's --max-time each spell "no limit", so a zero is refused
    rather than read as a bound, and so is a value this cannot read as a
    duration: a bound that is named has to be one a reader can see.
    """
    names = "|".join(re.escape(f) for f in (flag, *aliases))
    values = re.findall(rf"(?<![\w-])(?:{names})(?:=|\s+)[\"']?([^\s;|&)\"'`]*)", segment)
    if not values:
        return f"without {flag}"
    value = values[-1]
    if not DURATION.fullmatch(value) or not re.search(r"[1-9]", value):
        return f"with {flag} {value or '(empty)'}, which is no bound"
    return None


def _curl_unbounded(segment: str, rest: str) -> str | None:
    problem = _bound(segment, "--max-time", "-m")
    return f"curl {problem}" if problem else None


def _runner_only(tool: str):
    def check(segment: str, rest: str) -> str | None:
        return f"{tool} has no bound of its own, and this call does not run under deadline"
    return check


def _helm_unbounded(segment: str, rest: str) -> str | None:
    verb = next((w for w in rest.split() if not w.startswith("-")), "")
    if verb in HELM_UNBOUNDED_READS:
        return f"helm {verb} takes no timeout, and this call does not run under deadline"
    if verb in HELM_TRANSFERS:
        return (f"helm {verb} moves a chart with nothing to bound the transfer, and this call "
                f"does not run under deadline")
    return None


def _kubectl_unbounded(segment: str, rest: str) -> str | None:
    verb, after = kubectl_words(rest)
    if verb in ("config", "port-forward") or (verb == "version" and "--client" in segment):
        return None
    name = f"rollout {after}" if verb == "rollout" else verb or "(no verb)"
    waits = verb in ("wait", "delete") or name == "rollout status"
    flags = ("--request-timeout", "--timeout") if waits else ("--request-timeout",)
    problems = [p for p in (_bound(segment, flag) for flag in flags) if p]
    return f"kubectl {name} {' and '.join(problems)}" if problems else None


UNBOUNDED_BY_TOOL = {
    "curl": _curl_unbounded,
    "docker": _runner_only("docker"),
    "kind": _runner_only("kind"),
    "aws": _runner_only("aws"),
    # The JavaScript package runners: a script one runs reaches whatever it
    # reaches, a registry or a gateway, and none takes a bound of its own.
    **{tool: _runner_only(tool) for tool in ("pnpm", "npm", "npx", "yarn")},
    "helm": _helm_unbounded,
    "kubectl": _kubectl_unbounded,
}


def _subprocess_unbounded(call: ast.Call) -> str | None:
    """Why a blocking subprocess call is unbounded, or None.

    `timeout=None` is the default spelled out, and a timeout that is not a
    positive number expires before the child can run; a name or an expression
    is read as the bound it names.
    """
    timeout = next((k.value for k in call.keywords if k.arg == "timeout"), None)
    if timeout is None:
        return "without timeout="
    if isinstance(timeout, ast.Constant):
        value = timeout.value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return f"with timeout={value!r}, which is no bound"
    return None


def _outlasted(segment: str, limit: int) -> str | None:
    """Why a deadline does not bound a command that names a --timeout of its own.

    A deadline at or under that timeout stops the command partway through a wait
    it was told to make: the --timeout no longer says how long it waits, and a
    helm release stopped there is left pending. One the reader cannot evaluate,
    such as a variable, is refused too, for the reason `_bound` refuses one.
    """
    values = re.findall(r"(?<![\w-])--timeout(?:=|\s+)[\"']?([^\s;|&)\"'`]*)", segment)
    if not values:
        return None
    seconds = duration_seconds(values[-1])
    if seconds is None:
        return (f"runs under deadline {limit} with --timeout {values[-1] or '(empty)'}, which "
                f"cannot be read, so whether the deadline outlasts it is unknown")
    if seconds >= limit:
        return f"deadline {limit} does not outlast its own --timeout {values[-1]}"
    return None


def why_unbounded(tool: str, segment: str, limit: int | None) -> str | None:
    """Why one command is unbounded, or None. `segment` is its text up to the next
    separator, read from the folded line so a flag on a continuation counts, and
    `limit` is the deadline it runs under, or None."""
    if limit is not None:
        return _outlasted(segment, limit)
    rest = segment.split(None, 1)[1] if " " in segment else ""
    return UNBOUNDED_BY_TOOL[tool](segment, rest)


def unbounded_in(text: str) -> tuple[int, list[tuple[int, str]]]:
    """(outbound commands found, [(line, why)] for the unbounded ones) in shell `text`."""
    found, problems = 0, []
    for n, cmd in logical_lines(strip_comments(text)):
        code = code_view(cmd)
        for m in OUTBOUND_TOOL.finditer(code):
            is_command, limit = command_context(code[:m.start()], cmd[:m.start()])
            if not is_command:
                continue
            found += 1
            tail = code[m.start():]
            end = next((s.start() for s in COMMAND_SEP.finditer(tail)), len(tail))
            why = why_unbounded(m.group(1), cmd[m.start():m.start() + end], limit)
            if why:
                problems.append((n, why))
    return found, problems


def external_calls_are_bounded(root: pathlib.Path = ROOT) -> list[str]:
    """Every call that leaves this machine names how long it may take.

    A call with no bound waits on an unresponsive API server or a stuck daemon
    until somebody notices, and the step it hangs reads exactly like a slow one.
    Each tool is held to the bound it has:

    - kubectl carries --request-timeout, which bounds each request it makes.
      `wait`, `rollout status` and `delete` carry --timeout as well, since each
      of those goes on waiting on the cluster after its request returns. Each
      bounds what the other cannot: --timeout bounds the wait, but not the
      request that fetches the object before it, which kubectl sends with no
      deadline of its own; and when --request-timeout closes the wait's watch,
      kubectl opens another, so the wait still runs to its --timeout. `config`
      is local and `port-forward` runs until it is stopped, by design.
    - A bound is a positive duration. Zero is how kubectl's --request-timeout,
      `rollout status` and curl each say "no limit", and `timeout=None` is
      subprocess's default spelled out.
    - helm's release verbs carry --timeout, which the invariant above holds.
      `helm repo` verbs default to a two-minute timeout of their own. helm's
      read verbs take none, so they run under deadline.
    - docker, kind, the aws CLI's credential resolution and the JavaScript
      package runners (pnpm, npm, npx, yarn) have no bound of their own, so they
      run under cluster/deadline.sh, sourced or as a program.
    - curl carries --max-time.
    - A Python gate's subprocess call carries timeout=.

    The bound this cannot see: helm fetches a chart over HTTP before any of
    its timeouts start, with Go's dial and handshake limits and no read limit,
    so a stalled chart server is the one wait left unbounded. It is stated
    here rather than wrapped around every install.
    """
    problems = []
    examined = 0
    for path in tracked_files(root, "*.sh"):
        if path == root / "cluster" / "deadline.sh":
            continue
        found, unbounded = unbounded_in(path.read_text())
        examined += found
        problems.extend(f"{path.relative_to(root)}:{n}: {why}." for n, why in unbounded)
    for taskfile in tracked_files(root, "*Taskfile.yaml"):
        tasks = (yaml.safe_load(taskfile.read_text()) or {}).get("tasks") or {}
        for name, task in tasks.items():
            for cmd in (task or {}).get("cmds") or [] if isinstance(task, dict) else []:
                text = cmd.get("cmd") if isinstance(cmd, dict) else cmd
                if isinstance(text, str):
                    found, unbounded = unbounded_in(text)
                    examined += found
                    problems.extend(f"{taskfile.relative_to(root)} task `{name}`: {why}."
                                    for _, why in unbounded)
    for path in tracked_files(root, "*.py"):
        try:
            tree = ast.parse(path.read_text(), str(path))
        except SyntaxError:
            continue  # the compile invariant reports this file
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"):
                examined += 1
                if node.func.attr == "Popen":
                    problems.append(f"{path.relative_to(root)}:{node.lineno}: subprocess.Popen "
                                    f"takes no timeout; use subprocess.run with timeout=.")
                elif node.func.attr in SUBPROCESS_BLOCKING:
                    why = _subprocess_unbounded(node)
                    if why:
                        problems.append(f"{path.relative_to(root)}:{node.lineno}: "
                                        f"subprocess.{node.func.attr} {why}.")
    EXAMINED["every call that leaves this machine is bounded"] = examined
    if not examined:
        return ["found no outbound call in any script, Taskfile or gate — refusing to report "
                "every one bounded over an empty set."]
    return problems


KIND_CONFIG = "cluster/kind-config.yaml"
CILIUM_VALUES = "stack/core/cilium/values.yaml"

# The host ports the cluster's Ingress is reached on. Both land on the one
# shared listener, which serves plain HTTP and TLS on a single port.
INGRESS_HOST_PORTS = (80, 443)


def ingress_ports_reach_cilium(root: pathlib.Path = ROOT) -> list[str]:
    """localhost:80 and :443 reach Cilium's Ingress.

    Cilium serves every Ingress from one shared Envoy listener, on the host
    network of the nodes its values select and on the port they name, and kind
    publishes a host port to a container port of one node. Those facts live in
    two files, and nothing fails when they part: the cluster comes up, every
    Ingress is admitted, and localhost answers with a reset. So each host port's
    mapping is held to the listener's port, on a node the listener binds on.
    """
    try:
        kind = yaml.safe_load((root / KIND_CONFIG).read_text()) or {}
        values = yaml.safe_load((root / CILIUM_VALUES).read_text()) or {}
    except FileNotFoundError as e:
        return [f"{pathlib.Path(e.filename).relative_to(root)} does not exist — refusing to report "
                f"the Ingress ports reachable without it."]
    except yaml.YAMLError as e:
        return [f"{KIND_CONFIG} or {CILIUM_VALUES} does not parse ({e}), so the ports it names "
                f"cannot be read."]
    ingress = values.get("ingressController") or {}
    if not ingress.get("enabled"):
        return [f"{CILIUM_VALUES} does not enable ingressController, so nothing serves Ingress "
                f"behind localhost:80 and :443."]
    host_network = ingress.get("hostNetwork") or {}
    if not host_network.get("enabled"):
        return [f"{CILIUM_VALUES} does not put the Ingress listener on the host network "
                f"(ingressController.hostNetwork.enabled), so no node port carries it for kind "
                f"to map."]
    problems = []
    port = host_network.get("sharedListenerPort")
    if not isinstance(port, int) or isinstance(port, bool):
        problems.append(f"{CILIUM_VALUES} names no ingressController.hostNetwork."
                        f"sharedListenerPort, so the port localhost has to reach is the chart's "
                        f"default rather than one this repository states.")
        port = None
    selector = (host_network.get("nodes") or {}).get("matchLabels") or {}
    for host in INGRESS_HOST_PORTS:
        mapped = [(node, m) for node in kind.get("nodes") or []
                  for m in node.get("extraPortMappings") or [] if m.get("hostPort") == host]
        if not mapped:
            problems.append(f"{KIND_CONFIG} publishes nothing on host port {host}, so localhost:"
                            f"{host} does not reach Cilium's Ingress.")
        for node, m in mapped:
            if port is not None and m.get("containerPort") != port:
                problems.append(f"{KIND_CONFIG} maps localhost:{host} to container port "
                                f"{m.get('containerPort')}, but Cilium's Ingress listens on {port} "
                                f"({CILIUM_VALUES} ingressController.hostNetwork."
                                f"sharedListenerPort).")
            labels = node.get("labels") or {}
            unmatched = [f"{k}={v}" for k, v in selector.items() if str(labels.get(k)) != str(v)]
            if unmatched:
                problems.append(f"{KIND_CONFIG} maps localhost:{host} on a node without "
                                f"{', '.join(unmatched)}, which ingressController.hostNetwork."
                                f"nodes selects, so no listener binds where it lands.")
    EXAMINED["localhost:80 and :443 reach Cilium's Ingress"] = len(INGRESS_HOST_PORTS)
    return problems

CHECKS = [
    ("every helm install is found, and names a timeout", helm_calls_are_bounded),
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
    ("no script hands a secret to a command on argv", secrets_stay_off_argv),
    ("every fetched artifact is verified before it is used", remote_artifacts_are_verified),
    ("every Python file compiles", python_files_compile),
    ("no shell script runs Python from a string or stdin", python_runs_from_files),
    ("nothing kx runs can reach a cluster other than kind-kx", scripts_bind_their_kubeconfig),
    ("localhost:80 and :443 reach Cilium's Ingress", ingress_ports_reach_cilium),
    ("every call that leaves this machine is bounded", external_calls_are_bounded),
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
    "every helm install is found, and names a timeout": [
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
        # One case per spelling that used to fall out of the denominator. Each
        # broken tree carries an UNBOUNDED install written that way, so a check
        # that cannot see the form reports the tree clean and fails the control.
        (
            "an unbounded install using the short -i flag",
            {"stack/s/a/install.sh": "helm upgrade -i a x/a --version 1 --wait\n"},
            {"stack/s/a/install.sh": "helm upgrade -i a x/a --version 1 --wait --timeout 10m\n"},
        ),
        (
            "an unbounded install behind an environment assignment",
            {"stack/s/a/install.sh": "NS=n helm upgrade --install a x/a --version 1 --wait\n"},
            {"stack/s/a/install.sh":
             "NS=n helm upgrade --install a x/a --version 1 --wait --timeout 10m\n"},
        ),
        (
            "an unbounded install behind exec",
            {"stack/s/a/install.sh": "exec helm upgrade --install a x/a --version 1 --wait\n"},
            {"stack/s/a/install.sh":
             "exec helm upgrade --install a x/a --version 1 --wait --timeout 10m\n"},
        ),
        (
            "an unbounded install with two spaces before --install",
            {"stack/s/a/install.sh": "helm upgrade  --install a x/a --version 1 --wait\n"},
            {"stack/s/a/install.sh":
             "helm upgrade  --install a x/a --version 1 --wait --timeout 10m\n"},
        ),
        (
            "an unbounded install whose verb and flags are on different lines",
            {"stack/s/a/install.sh": "helm upgrade \\\n  --install a x/a --version 1 --wait\n"},
            {"stack/s/a/install.sh":
             "helm upgrade \\\n  --install a x/a --version 1 --wait --timeout 10m\n"},
        ),
        (
            "an unbounded `helm install`, which is not an upgrade at all",
            {"stack/s/a/install.sh": "helm install a x/a --version 1 --wait\n"},
            {"stack/s/a/install.sh": "helm install a x/a --version 1 --wait --timeout 10m\n"},
        ),
        (
            "a helm verb in neither set is reported rather than assumed harmless",
            {"stack/s/a/install.sh": BOUNDED + "helm frobnicate a\n"},
            {"stack/s/a/install.sh": BOUNDED + "helm template a x/a\n"},
            "helm frobnicate",
        ),
        (
            "helm named inside a quoted string is not read as a command",
            {"stack/s/a/install.sh": BOUNDED.replace(" --timeout 10m", "")
                                     + 'echo "(helm does not upgrade CRDs)"\n'},
            {"stack/s/a/install.sh": BOUNDED + 'echo "(helm does not upgrade CRDs)"\n'},
        ),
        # A line alternating environment assignments and separators is the input
        # that makes a single pattern for the same job backtrack exponentially.
        # Both trees carry it, so the floor's 180s ceiling is the termination
        # proof and the pair still turns on the violation rather than the input.
        (
            "a line built to make the command parser backtrack",
            {"stack/s/a/install.sh": ADVERSARIAL_PREFIX
                                     + BOUNDED.replace(" --timeout 10m", "")},
            {"stack/s/a/install.sh": ADVERSARIAL_PREFIX + BOUNDED},
        ),
    ],
    "no script hands a secret to a command on argv": [
        (
            "a secret handed to kubectl on the command line",
            {"s.sh": 'kubectl create secret generic x --from-literal=password="$P"\n'},
            {"s.sh": 'printf %s "$P" | kubectl create secret generic x --from-file=password=/dev/stdin\n'},
            "--from-literal",
        ),
        (
            # The rule is about the flag, not about the word `password`: a value
            # named innocuously leaks exactly as far.
            "a --from-literal whose key is not obviously a secret",
            {"s.sh": 'kubectl create secret generic x --from-literal=host="$H"\n'},
            {"s.sh": 'kubectl create secret generic x --from-file=host=/dev/stdin\n'},
        ),
        (
            "a --from-literal inside a comment is not a use",
            {"s.sh": '# built on stdin rather than --from-literal, which leaks to ps\n'
                     'kubectl create secret generic x --from-file=p=/dev/stdin\n',
             "t.sh": 'kubectl create secret generic y --from-literal=p="$P"\n'},
            {"s.sh": '# built on stdin rather than --from-literal, which leaks to ps\n'
                     'kubectl create secret generic x --from-file=p=/dev/stdin\n'},
        ),
    ],
    "every fetched artifact is verified before it is used": [
        (
            "an archive downloaded and unpacked with no digest",
            {"s.sh": 'curl -sSfL -o t.tgz "https://example/t.tgz"\ntar -xz -f t.tgz\n'},
            {"s.sh": 'curl -sSfL -o t.tgz "https://example/t.tgz"\n'
                     'echo "abc  t.tgz" | sha256sum -c -\ntar -xz -f t.tgz\n'},
        ),
        (
            "a manifest fetched and applied with no digest",
            {"s.sh": 'curl -sSfL -o m.yaml "https://example/m.yaml"\nkubectl apply -f m.yaml\n'},
            {"s.sh": 'curl -sSfL -o m.yaml "https://example/m.yaml"\n'
                     'echo "abc  m.yaml" | shasum -a 256 -c -\nkubectl apply -f m.yaml\n'},
        ),
        (
            "a manifest applied straight from a URL, which nothing can verify",
            {"s.sh": 'kubectl apply --server-side -f "https://example/crds.yaml"\n'},
            {"s.sh": 'curl -sSfL -o c.yaml "https://example/crds.yaml"\n'
                     'echo "abc  c.yaml" | sha256sum -c -\nkubectl apply --server-side -f c.yaml\n'},
            "straight from a URL",
        ),
        (
            # A file that downloads something nobody unpacks or applies is not
            # asked for a digest, so the corpus stays the shape the rule is about.
            "a download that is never unpacked or applied is not in the corpus",
            {"s.sh": 'curl -sSfL -o t.tgz "https://example/t.tgz"\ntar -xz -f t.tgz\n'},
            {"s.sh": 'curl -sSfL -o notes.txt "https://example/notes.txt"\ncat notes.txt\n',
             "t.sh": 'curl -sSfL -o t.tgz "https://x"\n'
                     'echo "abc  t.tgz" | sha256sum -c -\ntar -xz -f t.tgz\n'},
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
        # `|| true` is one spelling of a construct with several. A check that
        # lists the spellings it knows scores every other one as compliant, so
        # each is a case here rather than an alternation somebody has to trust.
        (
            "a repo add suppressed with || :",
            {"stack/s/a/install.sh": "helm repo add x https://x || :\n" + BOUNDED},
            {"stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
        ),
        (
            "a repo add suppressed with ||true, no space",
            {"stack/s/a/install.sh": "helm repo add x https://x ||true\n" + BOUNDED},
            {"stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
        ),
        (
            "a repo add whose failure is swallowed by || echo",
            {"stack/s/a/install.sh": "helm repo add x https://x || echo skipped\n" + BOUNDED},
            {"stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
        ),
        (
            "a repo add suppressed with a trailing ; true",
            {"stack/s/a/install.sh": "helm repo add x https://x; true\n" + BOUNDED},
            {"stack/s/a/install.sh": "helm repo add x https://x --force-update >/dev/null\n"
                                     + BOUNDED},
        ),
        (
            "a repo add split across a line continuation, suppressed",
            {"stack/s/a/install.sh": "helm repo add x \\\n  https://x || true\n" + BOUNDED},
            {"stack/s/a/install.sh": "helm repo add x \\\n  https://x --force-update\n"
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
    "every Python file compiles": [
        (
            "a Python file with a syntax error",
            {"a.py": "x = (\n"},
            {"a.py": "x = ()\n"},
            "a.py:1",
        ),
        (
            # The defect this invariant was written after, verbatim in shape: a
            # backslash inside an f-string's expression, which no Python release
            # compiles.
            "a backslash inside an f-string expression",
            {"m.py": 'import sys\nmissing = ["a"]\nsys.exit(f"has no {\\", \\".join(missing)}")\n'},
            {"m.py": 'import sys\nmissing = ["a"]\nsys.exit(f"has no {\', \'.join(missing)}")\n'},
            "m.py:3",
        ),
        (
            "a syntax error in an untracked file is not this repository's",
            {"a.py": "x = ()\n", "b.py": "x = (\n", UNTRACKED + "u.py": "x = (\n"},
            {"a.py": "x = ()\n", UNTRACKED + "u.py": "x = (\n"},
            "b.py",
        ),
        (
            "no Python file at all",
            {"README.md": "nothing here\n"},
            {"a.py": "x = 1\n"},
        ),
    ],
    "no shell script runs Python from a string or stdin": [
        (
            "a program passed to python3 with -c",
            {"s.sh": "python3 -c 'print(1)'\n"},
            {"s.sh": 'python3 "${SCRIPT_DIR}/p.py"\n'},
            "s.sh:1 runs a Python program passed with -c",
        ),
        (
            "a program read from a heredoc, the shape that built a Secret on stdin",
            {"s.sh": "python3 - \"$N\" <<'PY' | kubectl apply -f -\nprint(1)\nPY\n"},
            {"s.sh": 'python3 "${SCRIPT_DIR}/p.py" "$N" | kubectl apply -f -\n'},
            "read from stdin",
        ),
        (
            # The shape an installer writes most often, and the one a reading
            # that blanks every double-quoted body would miss.
            "a -c program inside a quoted command substitution",
            {"s.sh": 'K="$(printf \'%s\' "$J" | python3 -c \'import json\')"\n'},
            {"s.sh": 'K="$(printf \'%s\' "$J" | python3 "${SCRIPT_DIR}/k.py")"\n'},
            "s.sh:1",
        ),
        (
            # A quoted value blanked to spaces would split the assignment and
            # leave python3 reading as one of its arguments.
            "a program behind an assignment whose quoted value holds a space",
            {"s.sh": "LC_ALL='C.UTF-8 x' python3 -c 'print(1)'\n"},
            {"s.sh": "LC_ALL='C.UTF-8 x' python3 \"${SCRIPT_DIR}/p.py\"\n"},
            "s.sh:1",
        ),
        (
            "a multi-line program exec'd from a string, the shape a post-renderer used",
            {"p.sh": "exec python3 -c '\nimport sys\nprint(sys.stdin.read())\n'\n"},
            {"p.sh": 'exec python3 "${SCRIPT_DIR}/p.py"\n'},
            "p.sh:1",
        ),
        (
            "a program on stdin with no script named",
            {"s.sh": "python3 <<'PY'\nprint(1)\nPY\n"},
            {"s.sh": "python3 p.py <<'EOF'\ninput\nEOF\n"},
        ),
        (
            "a program in a Taskfile command",
            {"Taskfile.yaml": "tasks:\n  t:\n    cmds:\n      - python3 -c 'print(1)'\n"},
            {"Taskfile.yaml": "tasks:\n  t:\n    cmds:\n      - python3 scripts/t.py\n"},
            "Taskfile.yaml:4",
        ),
        (
            "a module, an option with a value and prose naming python3 are not programs",
            {"s.sh": 'python3 -m json.tool <f.json\npython3 -W ignore -u p.py\n'
                     'echo "needs python3 -c to work" >&2\n',
             "t.sh": "python3 -Ic 'x'\n"},
            {"s.sh": 'python3 -m json.tool <f.json\npython3 -W ignore -u p.py\n'
                     'echo "needs python3 -c to work" >&2\n'},
            "t.sh:1",
        ),
        (
            "no shell script or Taskfile at all",
            {"README.md": "nothing here\n"},
            {"s.sh": "echo hi\n"},
        ),
    ],
}

# A program behind each word that opens a command. The words are bash grammar
# rather than commands, and a reading that stopped at one would accept every
# program an installer runs as a condition, in a loop or in a function.
CONTROLS["no shell script runs Python from a string or stdin"] += [
    (
        f"a program behind `{word}`",
        {"s.sh": shape.replace("CMD", "python3 -c 'x'") + "\n"},
        {"s.sh": shape.replace("CMD", 'python3 "${SCRIPT_DIR}/p.py"') + "\n"},
        "s.sh:1",
    )
    for word, shape in COMMAND_OPENERS.items()
]


_BINDING = {KUBECONFIG_BINDING: "# the binding\n"}
_SCRIPT_DIR = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
_SOURCES = '. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"\n'
_BOUND_SCRIPT = _SCRIPT_DIR + _SOURCES + "kubectl apply -f x.yaml\n"
_PREFIX = KUBECONFIG_PREFIX + " "


def _taskfile(*cmds: str) -> str:
    """A Taskfile with one task, `d`, running `cmds`."""
    body = "".join(f"      - {c}\n" for c in cmds)
    return f"version: '3'\ntasks:\n  d:\n    cmds:\n{body}"


CONTROLS["nothing kx runs can reach a cluster other than kind-kx"] = [
    (
        "a script that runs kubectl without the binding",
        {**_BINDING, "stack/s/a/install.sh": "kubectl apply -f x.yaml\n"},
        {**_BINDING, "stack/s/a/install.sh": _BOUND_SCRIPT},
        "stack/s/a/install.sh:1",
    ),
    (
        "a script that binds only after its first cluster command",
        {**_BINDING, "stack/s/a/install.sh":
            "helm upgrade --install a x/a --version 1 --timeout 1m\n" + _SCRIPT_DIR + _SOURCES},
        {**_BINDING, "stack/s/a/install.sh":
            _SCRIPT_DIR + _SOURCES + "helm upgrade --install a x/a --version 1 --timeout 1m\n"},
        "before line 3",
    ),
    (
        "a binding sourced by a path that resolves somewhere else",
        {**_BINDING, "stack/s/a/install.sh":
            _SCRIPT_DIR + '. "${SCRIPT_DIR}/../../cluster/kubeconfig.sh"\nkubectl get ns\n'},
        {**_BINDING, "stack/s/a/install.sh": _SCRIPT_DIR + _SOURCES + "kubectl get ns\n"},
        "never sources",
    ),
    (
        "the binding file itself is missing",
        {"stack/s/a/install.sh": _BOUND_SCRIPT},
        {**_BINDING, "stack/s/a/install.sh": _BOUND_SCRIPT},
        "does not exist",
    ),
    (
        "a script that only renders charts needs no binding",
        {**_BINDING, "scripts/r.sh": "helm repo update\nhelm template a x/a\n",
         "stack/s/a/install.sh": "kubectl get ns\n"},
        {**_BINDING, "scripts/r.sh": "helm repo update\nhelm template a x/a\n",
         "stack/s/a/install.sh": _BOUND_SCRIPT},
        "stack/s/a/install.sh:1",
    ),
    (
        # If `kind:` counted as a command, the report would cite line 3.
        "a heredoc's `kind:` is YAML, not a command",
        {**_BINDING, "stack/s/a/install.sh":
            _SCRIPT_DIR + "cat >m.yaml <<EOF\nkind: ConfigMap\nEOF\nkubectl apply -f m.yaml\n"},
        {**_BINDING, "stack/s/a/install.sh":
            _SCRIPT_DIR + "cat >m.yaml <<EOF\nkind: ConfigMap\nEOF\n" + _SOURCES
            + "kubectl apply -f m.yaml\n"},
        "install.sh:5 reaches a cluster",
    ),
    (
        "a Taskfile command that uninstalls without the binding",
        {**_BINDING, "Taskfile.yaml": _taskfile("helm uninstall -n a a || true")},
        {**_BINDING, "Taskfile.yaml": _taskfile(_PREFIX + "helm uninstall -n a a || true")},
        "task `d`",
    ),
    (
        # Each word that opens a command is proven under the Python invariant;
        # this proves the binding reads through one as well.
        "a Taskfile command that asks the cluster as an if condition, without the binding",
        {**_BINDING, "stack/s/a/install.sh": _BOUND_SCRIPT,
         "Taskfile.yaml": _taskfile("if kubectl get ns x; then echo up; fi")},
        {**_BINDING, "stack/s/a/install.sh": _BOUND_SCRIPT,
         "Taskfile.yaml": _taskfile("if " + _PREFIX + "kubectl get ns x; then echo up; fi")},
        "task `d`",
    ),
    (
        "the binding on one side of a pipe does not reach the other",
        {**_BINDING, "Taskfile.yaml": _taskfile(_PREFIX + "kubectl get ns | kubectl apply -f -")},
        {**_BINDING, "Taskfile.yaml":
            _taskfile(_PREFIX + "kubectl get ns | " + _PREFIX + "kubectl apply -f -")},
    ),
    (
        "a Taskfile block that exports the binding after its first command",
        {**_BINDING, "Taskfile.yaml":
            _taskfile("|\n        kubectl get crd\n        " + KUBECONFIG_EXPORT)},
        {**_BINDING, "Taskfile.yaml":
            _taskfile("|\n        " + KUBECONFIG_EXPORT + "\n        kubectl get crd")},
    ),
    (
        "a Taskfile command that runs a bound script needs no binding of its own",
        {**_BINDING, "stack/s/a/install.sh": _BOUND_SCRIPT,
         "Taskfile.yaml": _taskfile("bash stack/s/a/install.sh", "kubectl get ns")},
        {**_BINDING, "stack/s/a/install.sh": _BOUND_SCRIPT,
         "Taskfile.yaml": _taskfile("bash stack/s/a/install.sh")},
        "kubectl get ns",
    ),
    (
        "nothing that reaches a cluster at all",
        {"README.md": "nothing here\n"},
        {**_BINDING, "stack/s/a/install.sh": _BOUND_SCRIPT},
    ),
]


def _ingress(port: int | None = 8080, enabled: bool = True, host_network: bool = True,
             maps: dict[int, int] | None = None,
             node_labels: dict[str, str] | None = None) -> dict[str, str]:
    """kind-config.yaml and cilium's values.yaml: `maps` (host -> container) published on a
    node labelled `node_labels`, and a shared listener on `port` bound on ingress-ready nodes."""
    maps = {80: 8080, 443: 8080} if maps is None else maps
    node_labels = {"ingress-ready": "true"} if node_labels is None else node_labels
    labels = "".join(f'      {k}: "{v}"\n' for k, v in node_labels.items())
    ports = "".join(f"      - {{containerPort: {c}, hostPort: {h}}}\n" for h, c in maps.items())
    listener = f"    sharedListenerPort: {port}\n" if port is not None else ""
    return {
        KIND_CONFIG: ("nodes:\n  - role: control-plane\n"
                      + (f"    labels:\n{labels}" if labels else "")
                      + f"    extraPortMappings:\n{ports}"),
        CILIUM_VALUES: (f"ingressController:\n  enabled: {str(enabled).lower()}\n"
                        f"  hostNetwork:\n    enabled: {str(host_network).lower()}\n{listener}"
                        f'    nodes:\n      matchLabels:\n        ingress-ready: "true"\n'),
    }


CONTROLS["localhost:80 and :443 reach Cilium's Ingress"] = [
    (
        "localhost:80 mapped to a container port other than the shared listener's",
        _ingress(maps={80: 80, 443: 8080}),
        _ingress(),
        "maps localhost:80 to container port 80",
    ),
    (
        "the shared listener moved without its mappings",
        _ingress(port=8081),
        _ingress(),
        "listens on 8081",
    ),
    (
        "no mapping on host port 443",
        _ingress(maps={80: 8080}),
        _ingress(),
        "publishes nothing on host port 443",
    ),
    (
        "the ports mapped on a node the listener does not bind on",
        _ingress(node_labels={"workload": "system"}),
        _ingress(),
        "on a node without ingress-ready=true",
    ),
    (
        "no listener port stated",
        _ingress(port=None),
        _ingress(),
        "names no ingressController.hostNetwork.sharedListenerPort",
    ),
    (
        "the listener taken off the host network",
        _ingress(host_network=False),
        _ingress(),
        "does not put the Ingress listener on the host network",
    ),
    (
        "the Ingress controller switched off",
        _ingress(enabled=False),
        _ingress(),
        "does not enable ingressController",
    ),
]


_DEADLINE = "bash '{{.ROOT_DIR}}/cluster/deadline.sh'"
_BOUNDED_CURL = "curl --max-time 5 https://example\n"

CONTROLS["every call that leaves this machine is bounded"] = [
    (
        "kubectl with no request timeout",
        {"s.sh": "kubectl get ns\n"},
        {"s.sh": "kubectl --request-timeout=60s get ns\n"},
        "kubectl get without --request-timeout",
    ),
    (
        "a delete that can wait on a finalizer forever",
        {"s.sh": "kubectl --request-timeout=60s delete ns x\n"},
        {"s.sh": "kubectl --request-timeout=60s --timeout=5m delete ns x\n"},
        "kubectl delete without --timeout",
    ),
    (
        "a wait is read across its continuation lines",
        {"s.sh": "kubectl --request-timeout=60s wait --for=condition=Ready pod/x \\\n  -n y\n"},
        {"s.sh": "kubectl --request-timeout=60s wait --for=condition=Ready pod/x \\\n"
                 "  --timeout=60s\n"},
        "kubectl wait without --timeout",
    ),
    (
        "a wait bounded by --timeout alone, whose first request has no deadline",
        {"s.sh": "kubectl wait --for=condition=Ready pod/x --timeout=60s\n"},
        {"s.sh": "kubectl --request-timeout=60s wait --for=condition=Ready pod/x --timeout=60s\n"},
        "kubectl wait without --request-timeout",
    ),
    (
        "a rollout status, which waits forever unless told otherwise",
        {"s.sh": "kubectl --request-timeout=60s -n y rollout status deployment/x\n"},
        {"s.sh": "kubectl --request-timeout=60s -n y rollout status deployment/x --timeout=5m\n"},
        "kubectl rollout status without --timeout",
    ),
    (
        "a zero request timeout, which is kubectl's spelling of none",
        {"s.sh": "kubectl --request-timeout=0s get ns\n"},
        {"s.sh": "kubectl --request-timeout=60s get ns\n"},
        "kubectl get with --request-timeout 0s, which is no bound",
    ),
    (
        "a request timeout given as its own word, which is not the verb",
        {"s.sh": "kubectl --request-timeout 0 get ns\n"},
        {"s.sh": "kubectl --request-timeout 60s get ns\n"},
        "kubectl get with --request-timeout 0",
    ),
    (
        # Each word that opens a command is proven under the Python invariant;
        # this proves the bound check reads through one as well.
        "a kubectl asked as an if condition",
        {"s.sh": "kubectl --request-timeout=60s get ns\nif kubectl get ns x; then :; fi\n"},
        {"s.sh": "kubectl --request-timeout=60s get ns\n"
                 "if kubectl --request-timeout=60s get ns x; then :; fi\n"},
        "s.sh:2",
    ),
    (
        "config is local and port-forward runs until stopped, so neither needs a bound",
        {"s.sh": "kubectl config current-context\nkubectl port-forward svc/x 8080:80\n",
         "t.sh": "kubectl get ns\n"},
        {"s.sh": "kubectl config current-context\nkubectl port-forward svc/x 8080:80\n"},
        "t.sh:1",
    ),
    (
        "a kubectl that xargs runs",
        {"s.sh": "kubectl --request-timeout=60s get crd -o name | xargs -r kubectl delete\n"},
        {"s.sh": "kubectl --request-timeout=60s get crd -o name"
                 " | xargs -r kubectl --request-timeout=60s --timeout=5m delete\n"},
        "kubectl delete",
    ),
    (
        "a docker build outside deadline",
        {"s.sh": "docker build -t x .\n"},
        {"s.sh": "deadline 1200 docker build -t x .\n"},
        "docker has no bound",
    ),
    (
        "a kind call in a Taskfile outside the deadline program",
        {"Taskfile.yaml": _taskfile("kind get clusters")},
        {"Taskfile.yaml": _taskfile(_DEADLINE + " 60 kind get clusters")},
        "kind has no bound",
    ),
    (
        "a helm read inside a command substitution, outside deadline",
        {"s.sh": 'x="$(helm status r --namespace n 2>&1)"\n'},
        {"s.sh": 'x="$(deadline 60 helm status r --namespace n 2>&1)"\n'},
        "helm status takes no timeout",
    ),
    (
        "the aws CLI resolving credentials outside deadline",
        {"s.sh": 'J="$(aws configure export-credentials --profile p)"\n'},
        {"s.sh": 'J="$(deadline 120 aws configure export-credentials --profile p)"\n'},
        "aws has no bound",
    ),
    # Each beside a bounded call, so a reader that does not know the runner
    # still has a call to examine and fails open rather than on an empty set.
    (
        "a pnpm script outside deadline, which waits as long as what it calls",
        {"s.sh": _BOUNDED_CURL + "pnpm --filter x --silent conformance\n"},
        {"s.sh": _BOUNDED_CURL + "deadline 600 pnpm --filter x --silent conformance\n"},
        "pnpm has no bound",
    ),
    *(
        (f"{tool} outside deadline", {"s.sh": _BOUNDED_CURL + f"{tool} run probe\n"},
         {"s.sh": _BOUNDED_CURL + f"deadline 600 {tool} run probe\n"}, f"{tool} has no bound")
        for tool in ("npm", "npx", "yarn")
    ),
    (
        "curl with no --max-time",
        {"s.sh": 'curl -sSfL -o x "https://example/x"\n'},
        {"s.sh": 'curl -sSfL --max-time 60 -o x "https://example/x"\n'},
        "curl without --max-time",
    ),
    (
        "curl with a zero -m, which is curl's spelling of none",
        {"s.sh": 'curl -sSfL -m 0 -o x "https://example/x"\n'},
        {"s.sh": 'curl -sSfL -m 2 -o x "https://example/x"\n'},
        "curl with --max-time 0",
    ),
    (
        "a gate's subprocess call with no timeout",
        {"scripts/g.py": "import subprocess\nsubprocess.run(['git', 'status'], check=False)\n"},
        {"scripts/g.py": "import subprocess\n"
                         "subprocess.run(['git', 'status'], check=False, timeout=60)\n"},
        "subprocess.run without timeout=",
    ),
    (
        "a gate's subprocess call that spells out no timeout",
        {"scripts/g.py": "import subprocess\n"
                         "subprocess.run(['git', 'status'], check=False, timeout=None)\n"},
        {"scripts/g.py": "import subprocess\nT = 60\n"
                         "subprocess.run(['git', 'status'], check=False, timeout=T)\n"},
        "timeout=None, which is no bound",
    ),
    (
        "a gate that starts a process it cannot bound",
        {"scripts/g.py": "import subprocess\nsubprocess.Popen(['git', 'status'])\n"},
        {"scripts/g.py": "import subprocess\n"
                         "subprocess.run(['git', 'status'], check=False, timeout=60)\n"},
        "Popen",
    ),
    (
        "nothing outbound at all",
        {"README.md": "nothing here\n"},
        {"s.sh": "curl --max-time 5 https://example\n"},
    ),
]

CONTROLS["every call that leaves this machine is bounded"] += [
    *(
        (f"helm {verb} outside deadline", {"s.sh": shape + "\n"},
         {"s.sh": f"deadline 900 {shape}\n"}, f"helm {verb} moves a chart")
        for verb, shape in HELM_TRANSFERS.items()
    ),
    (
        "a deadline no longer than the install's own --timeout",
        {"s.sh": "deadline 600 " + HELM_TRANSFERS["upgrade"] + "\n"},
        {"s.sh": "deadline 900 " + HELM_TRANSFERS["upgrade"] + "\n"},
        "deadline 600 does not outlast its own --timeout 10m",
    ),
    (
        "a --timeout on a continuation line, held against the deadline before it",
        {"s.sh": "deadline 300 helm upgrade --install a x/a \\\n  --version 1 --wait --timeout 10m\n"},
        {"s.sh": "deadline 900 helm upgrade --install a x/a \\\n  --version 1 --wait --timeout 10m\n"},
        "deadline 300 does not outlast its own --timeout 10m",
    ),
    (
        "a --timeout the reader cannot evaluate, under a deadline",
        {"s.sh": 'deadline 900 helm upgrade --install a x/a --version 1 --wait --timeout "${T}"\n'},
        {"s.sh": "deadline 900 " + HELM_TRANSFERS["upgrade"] + "\n"},
        "cannot be read",
    ),
    (
        "a kubectl wait cut short by the deadline around it",
        {"s.sh": "deadline 60 kubectl --request-timeout=60s wait --for=condition=Ready pod/x"
                 " --timeout=120s\n"},
        {"s.sh": "deadline 180 kubectl --request-timeout=60s wait --for=condition=Ready pod/x"
                 " --timeout=120s\n"},
        "deadline 60 does not outlast its own --timeout 120s",
    ),
    (
        "the deadline program's limit, read from a Taskfile",
        {"Taskfile.yaml": _taskfile(_DEADLINE + " 60 " + HELM_TRANSFERS["upgrade"])},
        {"Taskfile.yaml": _taskfile(_DEADLINE + " 900 " + HELM_TRANSFERS["upgrade"])},
        "deadline 60 does not outlast its own --timeout 10m",
    ),
]

CONTROLS["every helm install is found, and names a timeout"] += [
    (
        "an install under deadline, read as the install it runs",
        {"stack/s/a/install.sh": "helm repo add x https://x\n"
                                 "deadline 900 helm upgrade --install a x/a --version 1 --wait\n"},
        {"stack/s/a/install.sh": "helm repo add x https://x\n"
                                 "deadline 900 helm upgrade --install a x/a --version 1 --wait"
                                 " --timeout 10m\n"},
        "runs `helm upgrade` with no --timeout",
    ),
]


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
    subprocess.run(["git", "init", "-q", str(d)], check=False, capture_output=True, timeout=60)
    if staged:
        subprocess.run(["git", "-C", str(d), "add", "--", *staged], check=False,
                       capture_output=True, timeout=60)
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
