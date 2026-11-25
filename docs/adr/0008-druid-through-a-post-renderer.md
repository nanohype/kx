# 0008. Druid runs the production chart through a post-renderer

## Status

Accepted.

## Context

The Druid chart lives in the eks-gitops catalog itself, at
`catalog/druid/chart/`, rather than in a chart repository. It renders resources
that exist only on EKS: Karpenter's NodePool and EC2NodeClass, an
ExternalSecret against AWS Secrets Manager, and node selectors on EKS and
Karpenter labels. Its helper hard-codes `amd64` and `linux` node selectors,
which values cannot remove. kx should run that chart as production does rather
than a copy that drifts from it. Helm v4 dropped the `--post-renderer` flag
that ran an arbitrary executable, in favour of plugins.

## Decision

`stack/data/druid/install.sh` renders the chart unmodified from a sibling
eks-gitops checkout, or from the path `KX_DRUID_CHART_DIR` names, with
`helm template`. It pipes the render through `stack/data/druid/post-renderer.py`
and applies the result with kubectl.

- The post-renderer drops the NodePool, EC2NodeClass and ExternalSecret kinds,
  and strips the EKS, Karpenter, architecture and OS node selectors.
- `stack/data/druid/metadata-secret.py` reshapes the Secret CloudNativePG
  generates into the one the chart's helper expects.
- `stack/data/druid/values-local.yaml` points Druid's deep storage at the
  in-cluster object store.

Druid is an opt-in inside the data slice, and its own task removes it by
deleting the `druid` namespace.

## Alternatives

- **A copy of the chart with local edits.** It drifts from production with
  every upstream change, and the drift is invisible.
- **A helm post-renderer plugin.** It would make a plugin installed on the
  workstation part of every Druid install.
- **Values alone.** They cannot remove the selectors the chart's helper
  hard-codes. Its `amd64` selector leaves Druid unschedulable on the arm64 nodes
  kind runs on Apple Silicon.

## Consequences

- Druid runs the production chart's templates unmodified, and the local
  differences are the post-renderer's skip list and the local values.
- There is no helm release: `helm history` has nothing for Druid, and removing
  it means deleting its namespace, which takes the CloudNativePG cluster with
  it.
- When the upstream chart adds an EKS-only resource, the post-renderer's skip
  list grows to match. `CLAUDE.md` states that drift policy.
