#!/usr/bin/env python3
"""No container mounts two volumes on one path.

Reads a rendered manifest stream on stdin. Exists because `helm template`
cannot answer this question: mountPath uniqueness is an API-server validation
rule, not a schema constraint, so a chart renders a duplicate happily and exits
0. The pod is then rejected at apply time — or, on a cluster that already has
the workload, the rollout wedges and the old pod keeps serving, which is worse
because nothing looks broken.

The way this surfaces in practice is a chart growing a volume a values file was
already hand-rolling. Loki did exactly that: with persistence off the chart
started mounting its own emptyDir at /var/loki, which is what the local values
had been supplying for itself, and the two collided. Rendering stayed green
through the whole thing.

    helm template ... | check-rendered-mounts.py <label>
    check-rendered-mounts.py --self-test
"""

from __future__ import annotations

import sys

import yaml

WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicaSet", "Pod"}


# A bare `=` is YAML 1.1's "default value" key, and SafeLoader refuses to
# construct it. Real manifests contain one: prometheus-operator-crds ships a CRD
# with `- =` in an enum. Without this the parser raises on that whole slice, and
# the honest options are to fail the gate on an unrelated chart or to swallow the
# document and check nothing. Reading it as the string it looks like is neither.
yaml.SafeLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)


def pod_specs(doc):
    """Every pod spec in a manifest, with a path describing where it came from."""
    kind = doc.get("kind")
    if kind not in WORKLOADS:
        return
    name = (doc.get("metadata") or {}).get("name", "?")
    if kind == "Pod":
        yield name, doc.get("spec") or {}
    elif kind == "CronJob":
        spec = (((doc.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {})
        yield name, ((spec.get("template") or {}).get("spec") or {})
    else:
        yield name, (((doc.get("spec") or {}).get("template") or {}).get("spec") or {})


def duplicates(stream: str) -> list[str]:
    found = []
    for doc in yaml.safe_load_all(stream):
        if not isinstance(doc, dict):
            continue
        for name, spec in pod_specs(doc):
            if not spec:
                continue
            containers = (spec.get("initContainers") or []) + (spec.get("containers") or [])
            for c in containers:
                if not isinstance(c, dict):
                    continue
                paths = [
                    m.get("mountPath")
                    for m in (c.get("volumeMounts") or [])
                    if isinstance(m, dict) and m.get("mountPath")
                ]
                dupes = sorted({p for p in paths if paths.count(p) > 1})
                for d in dupes:
                    owners = [
                        m.get("name")
                        for m in (c.get("volumeMounts") or [])
                        if isinstance(m, dict) and m.get("mountPath") == d
                    ]
                    found.append(
                        f"{doc.get('kind')}/{name} container {c.get('name')}: "
                        f"{len(owners)} volumes mounted at {d} ({', '.join(map(str, owners))})"
                    )
    return found


SELF_TEST_CASES = [
    (
        "two volumes on one path",
        """
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: loki}
spec:
  template:
    spec:
      containers:
        - name: loki
          volumeMounts:
            - {name: storage, mountPath: /var/loki}
            - {name: data, mountPath: /var/loki}
""",
        True,
    ),
    (
        "the duplicate is in an initContainer",
        """
apiVersion: apps/v1
kind: Deployment
metadata: {name: x}
spec:
  template:
    spec:
      initContainers:
        - name: setup
          volumeMounts:
            - {name: a, mountPath: /w}
            - {name: b, mountPath: /w}
      containers:
        - name: main
          volumeMounts:
            - {name: a, mountPath: /w}
""",
        True,
    ),
    (
        "same path in two DIFFERENT containers is legal",
        """
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: ok}
spec:
  template:
    spec:
      containers:
        - name: one
          volumeMounts: [{name: a, mountPath: /shared}]
        - name: two
          volumeMounts: [{name: a, mountPath: /shared}]
""",
        False,
    ),
    (
        "a container with no volumeMounts at all",
        """
apiVersion: apps/v1
kind: Deployment
metadata: {name: bare}
spec:
  template:
    spec:
      containers:
        - name: c
          volumeMounts: null
""",
        False,
    ),
    (
        "a CronJob's nested pod spec is reached",
        """
apiVersion: batch/v1
kind: CronJob
metadata: {name: cj}
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: c
              volumeMounts:
                - {name: a, mountPath: /d}
                - {name: b, mountPath: /d}
""",
        True,
    ),
]


def self_test() -> int:
    failures = []
    for label, manifest, should_flag in SELF_TEST_CASES:
        flagged = bool(duplicates(manifest))
        if flagged != should_flag:
            failures.append(label)
            verdict = "flagged" if flagged else "missed"
            print(f"  WRONG     {label} — {verdict}, expected {'a flag' if should_flag else 'clean'}")
        else:
            print(f"  {'caught  ' if should_flag else 'allowed '}  {label}")
    if failures:
        print(f"\nFAIL  {len(failures)} case(s) wrong.")
        return 1
    print(f"\nOK    all {len(SELF_TEST_CASES)} cases correct.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    label = sys.argv[1] if len(sys.argv) > 1 else "(stdin)"
    stream = sys.stdin.read()
    # A stream that carried manifests but parsed to nothing means the parser and
    # the render disagree, and the check silently examined an empty set. That is
    # the failure this whole file exists to avoid, so it is an error, not a pass.
    if stream.strip() and not [d for d in yaml.safe_load_all(stream) if isinstance(d, dict)]:
        print(f"FAIL  {label} — rendered output parsed to zero manifests; the check examined nothing.")
        return 1
    hits = duplicates(stream)
    if hits:
        print(f"FAIL  {label} — a container mounts two volumes on one path:")
        for h in hits:
            print(f"        {h}")
        print("      Kubernetes rejects this; the render does not. Usually a values file is "
              "hand-rolling\n      a volume the chart has since started providing itself.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
