"""Reading rendered manifests."""

from __future__ import annotations

from collections.abc import Iterator

import yaml

# The kinds that carry a pod template, and so containers.
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicaSet", "Pod"}


class ManifestLoader(yaml.SafeLoader):
    """SafeLoader that tolerates a bare `=` in rendered CRDs.

    prometheus-operator-crds ships an Alertmanager matchType enum listing the
    match operators, and `- =` unquoted is YAML 1.1's "default value" tag rather
    than the string. SafeLoader refuses it outright, taking the whole document
    with it — and a document lost while reading the render is one no gate
    examines, which is the failure the gates exist to prevent.
    """


ManifestLoader.add_constructor(
    "tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node)
)


def pod_specs(doc: dict) -> Iterator[tuple[str, dict]]:
    """(name, pod spec) for every pod a manifest describes, by its workload's name."""
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
