#!/usr/bin/env python3
"""Three invariants about the tree, each closing a class rather than an instance.

    scripts/check-slice-integrity.py             # gate
    scripts/check-slice-integrity.py --self-test # prove it still rejects

Each check here was a defect found once and fixed everywhere. Fixing every
instance leaves the class open: the next slice added reintroduces it, and the
sweep that found it is not repeatable. What closes a class is a gate, so these
are the three that can be stated as a property of the tree.

  1. Every `helm upgrade --install` names an explicit `--timeout`.
  2. Every gate script supports `--self-test`, and CI runs it.
  3. Every file in an addon directory is reached by that addon's `install.sh`.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELM_INSTALL = re.compile(r"^helm upgrade --install\b", re.M)


def helm_calls_are_bounded(root: pathlib.Path = ROOT) -> list[str]:
    """helm's own default is five minutes, which is the wrong number here.

    A cold kind cluster pulling kube-prometheus-stack or cilium exceeds it
    routinely, and every install script runs under `set -euo pipefail`, so the
    implicit default aborts a slice midway through. The number matters less than
    it being chosen: an install that waits has to say how long.
    """
    problems = []
    for script in sorted(root.glob("stack/*/*/install.sh")):
        text = script.read_text()
        if not HELM_INSTALL.search(text):
            continue
        if "--timeout" not in text:
            rel = script.relative_to(root)
            problems.append(
                f"{rel} runs `helm upgrade --install` with no --timeout, so it takes helm's "
                f"implicit 5m — short enough that a cold image pull aborts the slice."
            )
    return problems


def gates_prove_they_reject(root: pathlib.Path = ROOT) -> list[str]:
    """A gate that cannot fail reports the same thing as a gate that passes.

    Both halves are checked: the script has to support the flag, and CI has to
    run it. Either alone is the failure — a self-test nothing invokes rots, and
    a CI step naming a flag the script ignores exits 0 forever.
    """
    problems = []
    ci_path = root / ".github" / "workflows" / "ci.yml"
    ci = ci_path.read_text() if ci_path.is_file() else ""
    if not ci:
        return [f"{ci_path.relative_to(root)} does not exist — cannot tell whether any gate is proven."]

    gates = sorted(root.glob("scripts/check-*.py")) + [root / "scripts" / "render-check.sh"]
    for gate in gates:
        rel = gate.relative_to(root)
        if not gate.is_file():
            problems.append(f"{rel} is named as a gate but does not exist.")
            continue
        text = gate.read_text()
        if "--self-test" not in text:
            problems.append(f"{rel} has no --self-test, so nothing proves it can still reject.")
            continue
        if f"{rel} --self-test" not in ci:
            problems.append(
                f"{rel} has a --self-test that {ci_path.relative_to(root)} never runs. A proof "
                f"nothing invokes is a proof that rots."
            )
    return problems


# Files an addon directory may carry that its install.sh does not name. Each is
# reached by something other than a literal reference, and the reason is here so
# an entry cannot be added without one.
UNREFERENCED_BY_DESIGN = {
    "values.yaml": "passed by --values \"${SCRIPT_DIR}/values.yaml\", which the reference "
                   "check below does match — listed so a values-less addon is not a finding",
}


def addon_files_are_reached(root: pathlib.Path = ROOT) -> list[str]:
    """A manifest in an addon directory that nothing applies is dead.

    This is the check that would have caught an RBAC file sitting beside the
    policy it duplicated, unapplied, with a header explaining why it existed.
    A file whose name appears nowhere is not configuration, it is a document
    that looks like configuration.

    Reached means named by the addon's own install.sh or by its slice Taskfile —
    a script a task runs directly is reached, and narrowing this to install.sh
    alone would report the slice's verify and conformance scripts as dead.
    """
    problems = []
    for install in sorted(root.glob("stack/*/*/install.sh")):
        addon = install.parent
        taskfile = addon.parent / "Taskfile.yaml"
        text = install.read_text()
        if taskfile.is_file():
            text += taskfile.read_text()
        for entry in sorted(addon.iterdir()):
            if entry.name == "install.sh":
                continue
            if entry.is_dir():
                # A directory is reached if the installer names it at all.
                if entry.name not in text:
                    problems.append(
                        f"{entry.relative_to(root)}/ is not named by {install.relative_to(root)}"
                    )
                continue
            if entry.name in UNREFERENCED_BY_DESIGN and entry.name in text:
                continue
            if entry.name not in text:
                problems.append(
                    f"{entry.relative_to(root)} is not named by {install.relative_to(root)} — "
                    f"nothing applies it, so it ships as documentation that reads as config."
                )
    return problems


CHECKS = [
    ("every helm install names a timeout", helm_calls_are_bounded),
    ("every gate proves it can still reject", gates_prove_they_reject),
    ("every file in an addon directory is applied", addon_files_are_reached),
]


def run() -> int:
    failed = 0
    for label, check in CHECKS:
        problems = check()
        if problems:
            failed += 1
            print(f"FAIL  {label}:")
            for p in problems:
                print(f"        {p}")
        else:
            print(f"  ok  {label}")
    if failed:
        print(f"\ncheck-slice-integrity: {failed} invariant(s) do not hold.")
        return 1
    print(f"\ncheck-slice-integrity: {len(CHECKS)} invariant(s) hold.")
    return 0


def self_test() -> int:
    """Drive each check against a tree built to break it.

    The checks read ROOT, so the breaks are applied by pointing ROOT at a
    synthetic tree — the shipped functions run, not restatements of them.
    """
    failures = 0

    def tree(files):
        d = pathlib.Path(tempfile.mkdtemp(prefix="kx-integrity-"))
        for rel, body in files.items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
        return d

    BOUNDED = ('helm repo add x https://x\n'
               'helm upgrade --install a x/a --version 1 --wait --timeout 10m\n')

    cases = [
        ("a helm install with no --timeout", helm_calls_are_bounded,
         {"stack/s/a/install.sh": 'helm upgrade --install a x/a --version 1 --wait\n'}, True),
        ("a helm install that names one", helm_calls_are_bounded,
         {"stack/s/a/install.sh": BOUNDED}, False),
        ("an addon file nothing applies", addon_files_are_reached,
         {"stack/s/a/install.sh": BOUNDED, "stack/s/a/orphan.yaml": "kind: ClusterRole\n"}, True),
        ("an addon file the installer names", addon_files_are_reached,
         {"stack/s/a/install.sh": BOUNDED + 'kubectl apply -f "${SCRIPT_DIR}/policy.yaml"\n',
          "stack/s/a/policy.yaml": "kind: ClusterPolicy\n"}, False),
        ("an addon script only the slice Taskfile runs", addon_files_are_reached,
         {"stack/s/a/install.sh": BOUNDED,
          "stack/s/a/verify.sh": "echo ok\n",
          "stack/s/Taskfile.yaml": "cmds:\n  - bash {{.TASKFILE_DIR}}/a/verify.sh\n"}, False),
        ("an addon script neither names", addon_files_are_reached,
         {"stack/s/a/install.sh": BOUNDED,
          "stack/s/a/orphan.sh": "echo ok\n",
          "stack/s/Taskfile.yaml": "cmds:\n  - true\n"}, True),
        ("a values.yaml passed by --values", addon_files_are_reached,
         {"stack/s/a/install.sh": 'helm upgrade --install a x/a --timeout 10m '
                                  '--values "${SCRIPT_DIR}/values.yaml"\n',
          "stack/s/a/values.yaml": "replicas: 1\n"}, False),
        ("a gate with no --self-test", gates_prove_they_reject,
         {"scripts/check-x.py": "print('hi')\n", "scripts/render-check.sh": "#--self-test\n",
          ".github/workflows/ci.yml": "run: scripts/render-check.sh --self-test\n"}, True),
        ("a gate whose --self-test CI never runs", gates_prove_they_reject,
         {"scripts/check-x.py": "if '--self-test' in sys.argv: pass\n",
          "scripts/render-check.sh": "#--self-test\n",
          ".github/workflows/ci.yml": "run: scripts/render-check.sh --self-test\n"}, True),
        ("a gate proven in CI", gates_prove_they_reject,
         {"scripts/check-x.py": "if '--self-test' in sys.argv: pass\n",
          "scripts/render-check.sh": "#--self-test\n",
          ".github/workflows/ci.yml": "run: scripts/check-x.py --self-test\n"
                                      "run: scripts/render-check.sh --self-test\n"}, False),
    ]

    for label, check, files, should_flag in cases:
        flagged = bool(check(tree(files)))
        if flagged != should_flag:
            print(f"  WRONG     {label} — {'flagged' if flagged else 'missed'}, "
                  f"expected {'a flag' if should_flag else 'clean'}")
            failures += 1
        else:
            print(f"  {'caught  ' if should_flag else 'allowed '}  {label}")

    if failures:
        print(f"\nFAIL  {failures} case(s) wrong.")
        return 1
    print(f"\nOK    {len(cases)} case(s) behave as specified; the shipped tree is checked above.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    return run()


if __name__ == "__main__":
    sys.exit(main())
