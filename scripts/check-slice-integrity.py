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

import importlib.util
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

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
            if "--timeout" not in cmd:
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
            if entry.name not in text:
                what = "directory" if entry.is_dir() else "file"
                problems.append(
                    f"{entry.relative_to(root)} — nothing names this {what} outside a comment, "
                    f"so nothing applies it and it ships as documentation that reads as config."
                )
    EXAMINED["every file in an addon directory is applied"] = examined
    return problems


def gates_reject_and_accept(root: pathlib.Path = ROOT) -> list[str]:
    """Every gate in the suite is RUN, and observed to reject and to accept.

    Not read. A floor that decides whether a gate still has controls by looking
    at its source is satisfied by a comment saying the controls were removed —
    the same defect one level up from the one the controls exist for. This
    imports each gate and calls the contract, so prose cannot satisfy it.

    Both halves, because a gate that rejects everything is exactly as useless as
    one that rejects nothing and either count alone passes a one-sided check.
    """
    problems = []
    # Discovered, not listed. A hardcoded roster is a second place to forget a
    # gate, and a roster naming a file that has left the tree is the exemption
    # defect one level up.
    gates = sorted(
        g for g in (root / "scripts").glob("*.py")
        if g.name != pathlib.Path(__file__).name
    )
    if not gates:
        return ["found no gate scripts under scripts/ — refusing to report a proven suite "
                "over an empty set."]

    for gate in gates:
        rel = gate.relative_to(root)
        spec = importlib.util.spec_from_file_location(gate.stem.replace("-", "_"), gate)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001 - any import failure is a dead gate
            problems.append(f"{rel} does not import: {type(e).__name__}: {e}")
            continue
        report = getattr(module, "control_outcomes", None)
        if report is None:
            problems.append(f"{rel} exposes no control_outcomes() — nothing can observe "
                            f"whether its controls still run.")
            continue
        try:
            outcome = report()
        except Exception as e:  # noqa: BLE001 - a control suite that raises proves nothing
            problems.append(f"{rel} control_outcomes() raised {type(e).__name__}: {e}")
            continue
        if not outcome.get("ok"):
            problems.append(f"{rel} controls do not pass.")
        if not outcome.get("rejected"):
            problems.append(f"{rel} controls exercised no rejection — the gate was never "
                            f"observed refusing anything.")
        if not outcome.get("accepted"):
            problems.append(f"{rel} controls exercised no acceptance — a gate that refuses "
                            f"everything is as useless as one that refuses nothing.")
    EXAMINED["every gate is observed to reject and to accept"] = len(gates)
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


CHECKS = [
    ("every helm install names a timeout", helm_calls_are_bounded),
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
    "every gate is observed to reject and to accept": [
        (
            "a gate whose controls were deleted and replaced by a comment saying so",
            {"scripts/check-x.py": "# the positive controls were removed\n"},
            {"scripts/check-x.py":
                "import contextlib, io\n"
                "def self_test():\n"
                "    print('  rejected  a break'); print('  passed    a clean tree'); return 0\n"
                "def control_outcomes():\n"
                "    buf = io.StringIO()\n"
                "    with contextlib.redirect_stdout(buf): rc = self_test()\n"
                "    lines = buf.getvalue().splitlines()\n"
                "    return {'ok': rc == 0,\n"
                "            'rejected': sum(1 for x in lines if 'rejected' in x),\n"
                "            'accepted': sum(1 for x in lines if 'passed' in x)}\n"},
        ),
        (
            "a gate that refuses everything",
            {"scripts/check-x.py":
                "def control_outcomes():\n"
                "    return {'ok': True, 'rejected': 3, 'accepted': 0}\n"},
            {"scripts/check-x.py":
                "def control_outcomes():\n"
                "    return {'ok': True, 'rejected': 3, 'accepted': 2}\n"},
        ),
        (
            "a gate that refuses nothing",
            {"scripts/check-x.py":
                "def control_outcomes():\n"
                "    return {'ok': True, 'rejected': 0, 'accepted': 2}\n"},
            {"scripts/check-x.py":
                "def control_outcomes():\n"
                "    return {'ok': True, 'rejected': 3, 'accepted': 2}\n"},
        ),
        (
            "no gate scripts at all",
            {"README.md": "nothing here\n"},
            {"scripts/check-x.py":
                "def control_outcomes():\n"
                "    return {'ok': True, 'rejected': 3, 'accepted': 2}\n"},
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


def controls() -> int:
    """Prove every check rejects the violation it exists to catch."""
    names = {label for label, _ in CHECKS}
    failures = 0

    for label in sorted(names - set(CONTROLS)):
        print(f"  NO CONTROL  {label} — a check with no positive control cannot be trusted.")
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
    return failures


def run(root: pathlib.Path = ROOT) -> int:
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
