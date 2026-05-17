#!/usr/bin/env bash
# Helm post-renderer for the eks-gitops Druid chart, applied at kx install time only.
# Three responsibilities — all to bridge production-shaped chart to a local kind cluster:
#
#   1. Drop Karpenter resources (NodePool, EC2NodeClass). kind has no Karpenter, applying these
#      against missing CRDs fails the install.
#   2. Drop ExternalSecret resources. They reference an AWS Secrets Manager ClusterSecretStore
#      that doesn't exist locally; install.sh creates the equivalent K8s Secrets directly.
#   3. Strip karpenter/eks-specific labels from pod nodeSelectors. Otherwise pods stay Pending
#      because no kind node has `karpenter.sh/capacity-type=on-demand` etc.
#
# Reads rendered manifests from stdin, writes filtered manifests to stdout.
# (Heredoc would consume stdin; using -c so python keeps sys.stdin as the pipe input.)
set -euo pipefail

exec python3 -c '
import sys, yaml

SKIP_KINDS = {"NodePool", "EC2NodeClass", "ExternalSecret"}
STRIP_NODE_LABELS = {
    # Karpenter / EKS-only labels
    "karpenter.k8s.aws/instance-family",
    "karpenter.sh/capacity-type",
    "eks.amazonaws.com/nodegroup",
    # Hard-coded arch/os labels in the chart helper (amd64/linux) — wrong on Apple Silicon
    # kind nodes (arm64). kind clusters are always single-arch and single-OS; the container
    # image arch is what gets checked at pull time anyway.
    "kubernetes.io/arch",
    "kubernetes.io/os",
}

def strip(obj):
    if isinstance(obj, dict):
        ns = obj.get("nodeSelector")
        if isinstance(ns, dict):
            for k in list(ns.keys()):
                if k in STRIP_NODE_LABELS:
                    del ns[k]
            if not ns:
                del obj["nodeSelector"]
        for v in obj.values():
            strip(v)
    elif isinstance(obj, list):
        for v in obj:
            strip(v)

def strip_embedded_yaml_in_configmap(cm):
    """Druid Overlord reads a pod-template ConfigMap whose `data` value is itself a YAML doc
    embedded as a string. Parse, strip, re-emit."""
    data = cm.get("data") or {}
    for k, v in list(data.items()):
        if not isinstance(v, str): continue
        try:
            inner = yaml.safe_load(v)
        except Exception:
            continue
        if not isinstance(inner, dict): continue
        strip(inner)
        data[k] = yaml.safe_dump(inner, default_flow_style=False)

docs = list(yaml.safe_load_all(sys.stdin))
out = [d for d in docs if d and d.get("kind") not in SKIP_KINDS]
for d in out:
    strip(d)
    if d.get("kind") == "ConfigMap" and "task-base" in (d.get("metadata", {}).get("name") or ""):
        strip_embedded_yaml_in_configmap(d)
print(yaml.safe_dump_all(out, default_flow_style=False))
'
