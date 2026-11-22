#!/usr/bin/env python3
"""Filter the eks-gitops Druid chart's render for a local kind cluster.

    helm template ... | python3 post-renderer.py | kubectl apply -f -

Reads rendered manifests on stdin and writes the filtered manifests to stdout.
Applied at kx install time only. Three responsibilities, all bridging a
production-shaped chart to a kind cluster:

  1. Drop Karpenter resources (NodePool, EC2NodeClass). kind has no Karpenter,
     and applying them against missing CRDs fails the install.
  2. Drop ExternalSecret resources. They reference an AWS Secrets Manager
     ClusterSecretStore that does not exist locally; install.sh creates the
     equivalent Kubernetes Secrets directly.
  3. Strip Karpenter- and EKS-specific labels from pod nodeSelectors, which
     otherwise leave pods Pending because no kind node carries
     `karpenter.sh/capacity-type=on-demand` and the like.

Empty input is valid and produces an empty render, which is how install.sh
checks that this program can run before it changes anything on the cluster.
"""

import sys

try:
    import yaml
except ImportError:
    sys.exit("druid: the post-renderer needs PyYAML, which this python3 cannot import. "
             "Run: pip3 install pyyaml")

SKIP_KINDS = {"NodePool", "EC2NodeClass", "ExternalSecret"}

STRIP_NODE_LABELS = {
    # Karpenter / EKS-only labels.
    "karpenter.k8s.aws/instance-family",
    "karpenter.sh/capacity-type",
    "eks.amazonaws.com/nodegroup",
    # The chart helper hard-codes amd64/linux, which is wrong on Apple Silicon
    # kind nodes (arm64). A kind cluster is single-arch and single-OS, and the
    # container runtime checks an image's arch at pull time anyway.
    "kubernetes.io/arch",
    "kubernetes.io/os",
}


def strip(obj):
    """Remove STRIP_NODE_LABELS from every nodeSelector under obj, in place."""
    if isinstance(obj, dict):
        selector = obj.get("nodeSelector")
        if isinstance(selector, dict):
            for key in list(selector):
                if key in STRIP_NODE_LABELS:
                    del selector[key]
            if not selector:
                del obj["nodeSelector"]
        for value in obj.values():
            strip(value)
    elif isinstance(obj, list):
        for value in obj:
            strip(value)


def strip_embedded_yaml_in_configmap(cm):
    """Strip the pod template the Druid Overlord reads out of a ConfigMap.

    Each `data` value there is itself a YAML document embedded as a string, so it
    is parsed, stripped and re-emitted. A value that is not a YAML mapping is
    left exactly as it was.
    """
    data = cm.get("data") or {}
    for key, value in list(data.items()):
        if not isinstance(value, str):
            continue
        try:
            inner = yaml.safe_load(value)
        except yaml.YAMLError:
            continue
        if not isinstance(inner, dict):
            continue
        strip(inner)
        data[key] = yaml.safe_dump(inner, default_flow_style=False)


def render(stream) -> str:
    """The filtered render of every YAML document in stream."""
    docs = list(yaml.safe_load_all(stream))
    out = [d for d in docs if d and d.get("kind") not in SKIP_KINDS]
    for doc in out:
        strip(doc)
        name = (doc.get("metadata") or {}).get("name") or ""
        if doc.get("kind") == "ConfigMap" and "task-base" in name:
            strip_embedded_yaml_in_configmap(doc)
    return yaml.safe_dump_all(out, default_flow_style=False)


if __name__ == "__main__":
    print(render(sys.stdin))
