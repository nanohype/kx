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

import json
import os
import pathlib
import re
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
            elif c in "\"'":
                quote = c
                buf.append(c)
            elif c == "#" and (not buf or buf[-1].isspace()):
                buf.append(" " * (len(line) - i))
                break
            else:
                buf.append(c)
            i += 1
        out.append("".join(buf))
    return "\n".join(out)


EXAMINED: dict[str, int] = {}


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
    EXAMINED["every gate is observed to reject and to accept"] = len(GATE_PROBES)
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
)


def markdown_paths_resolve(root: pathlib.Path = ROOT) -> list[str]:
    """Every repo-relative path named in markdown exists.

    The documentation rule made executable. Prose that names a thing is a claim
    about the world, and the claim most likely to rot is a path: a file moves
    and every document that pointed at it keeps pointing, confidently, at
    nothing.

    Scoped to markdown LINK targets carrying a directory separator, because a
    link is an unambiguous claim: it is a thing the reader clicks. Inline code
    is not. In this tree a backticked `install.sh` names the CONVENTION every
    addon directory follows, and `karpenter.sh/capacity-type` is a label key —
    reading either as a path buries the one real finding under thirty-six that
    are correct prose. The narrow rule is the one that says something.

    Reads the RAW markdown, because in a document the prose IS the target and
    the blanked view would leave nothing to check.
    """
    problems = []
    examined = 0
    docs = sorted(root.glob("*.md")) + sorted(root.glob("docs/**/*.md"))
    if not docs:
        EXAMINED["every path named in markdown resolves"] = 0
        return ["found no markdown at the repository root — refusing to report every path "
                "resolved over an empty set."]
    for doc in docs:
        for n, line in enumerate(doc.read_text().splitlines(), 1):
            for m in MD_LINK.finditer(line):
                if True:
                    ref = m.group(1).strip()
                    if not ref or "/" not in ref or NOT_A_PATH.search(ref):
                        continue
                    # Only a leading ./ is stripped. Stripping "." would turn
                    # .github/workflows/ci.yml into a path that does not exist —
                    # which is what the first version of this reported.
                    rel = ref[2:] if ref.startswith("./") else ref
                    examined += 1
                    if not (root / rel.rstrip("/")).exists():
                        problems.append(
                            f"{doc.relative_to(root)}:{n} names `{ref}`, which does not exist."
                        )
    EXAMINED["every path named in markdown resolves"] = examined
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


CHECKS = [
    ("every helm install names a timeout", helm_calls_are_bounded),
    ("no helm repo add swallows its own failure", repo_adds_do_not_swallow),
    ("every file in an addon directory is applied", addon_files_are_reached),
    ("every gate is observed to reject and to accept", gates_reject_and_accept),
    ("every path named in markdown resolves", markdown_paths_resolve),
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
            {"README.md": "nothing named here\n"},
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


def _tree(files: dict[str, str]) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="kx-integrity-"))
    for rel, body in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
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

    for label in sorted(names - set(CONTROLS) - set(CONTROLLED_ELSEWHERE)):
        print(f"  NO CONTROL  {label} — a check with no positive control cannot be trusted.")
        failures += 1
    for label in sorted(set(CONTROLLED_ELSEWHERE) - names):
        print(f"  ORPHANED    CONTROLLED_ELSEWHERE names {label!r}, which is not a check.")
        failures += 1
    for label in sorted(set(CONTROLS) - names):
        print(f"  ORPHANED    control for {label!r}, which is not a check any more.")
        failures += 1

    by_label = dict(CHECKS)
    for label, cases in sorted(CONTROLS.items()):
        check = by_label.get(label)
        if check is None:
            continue
        for case in cases:
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
                continue
            if check(_tree(clean)):
                print(f"  UNSOUND     {name} — the gate already fails on the clean tree, so "
                      f"failing on the mutation would prove nothing.")
                failures += 1
                continue
            reported = check(_tree(broken))
            if not reported:
                print(f"  FAILS OPEN  {name} — the gate accepted it.")
                failures += 1
            elif must_say and not any(must_say in r for r in reported):
                print(f"  MIS-CITED   {name} — expected {must_say!r} in the report, got:")
                for r in reported:
                    print(f"                {r}")
                failures += 1
            else:
                print(f"  control ok  {name}")
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

    return failures


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
        seen = "" if n is None else f"  [{n} examined]"
        if problems:
            failed += 1
            print(f"FAIL  {label}:{seen}")
            for p in problems:
                print(f"        {p}")
        elif n == 0:
            print(f"  ok  {label}{seen} — nothing in the tree to check, so this asserts "
                  f"nothing yet")
        else:
            print(f"  ok  {label}{seen}")
    return failed


def main() -> int:
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
