# kx — agent entry point

You're an AI client (or the author of one) about to run a chart locally before deploying to production. This file gets you running in five minutes. For the wider picture — how this repo fits into the nanohype stack — read the [Platform Reference](https://github.com/nanohype/nanohype/blob/main/docs/platform-reference.md).

## What this repo gives you

A local Kubernetes (kind) cluster preloaded with the Helm chart catalog from [`eks-gitops`](../eks-gitops/). Chart shapes match production EKS, so workloads developed against kx deploy unchanged to a real cluster.

Use it to:

- Render and run an `<app>/chart/` locally before shipping it
- Iterate on a new chart without burning a real EKS cluster's reconcile budget
- Smoke-test a `Platform` CR + `BudgetPolicy` CR pair before applying them to production
- Validate `AgentFleet` workloads work end-to-end with the same operator binary that runs in production

## Quickstart

```sh
brew install kind helm kubectl task    # one-time
task up                                # cluster + core stack
task status                            # see what's running
task stack:observability:enable        # enable an opt-in slice
task down                              # tear down
```

Target the local cluster:

```sh
export KUBECONFIG=$(kind get kubeconfig --name kx --internal=false | psub)
kubectl create namespace my-project
helm install my-app /path/to/<app>/chart -n my-project -f /path/to/<app>/chart/values-development.yaml
```

## Contract surface

kx is the **mirror** of `eks-gitops`. Cloud-portable addons install from the same chart under the same name; AWS-specific addons have local equivalents behind the same operational surface. Two differences to know about:

- **Identity**: kx doesn't run IRSA. Pods that expect AWS credentials get them via mounted env vars or `~/.aws/credentials` — `aws.platformRoleArn` is set to `""` in local dev values, omitting the SA annotation entirely.
- **Druid post-renderer**: `stack/data/druid/` runs the production chart (`eks-gitops/catalog/druid/chart/`) unmodified through a post-renderer that strips EKS-only resources (Karpenter `NodePool`/`EC2NodeClass`, `ExternalSecret`) and EKS node-selector labels.

If a chart works under kx, it should work on a real EKS cluster after the IRSA annotation is plumbed in.

## Add an addon to the local stack

1. Check the same addon's shape in [`eks-gitops/addons/<category>/<name>/`](../eks-gitops/addons/) — that's the production reference.
2. Add `kx/stack/<slice>/<name>/` with exactly two files: `install.sh` (explicit `helm repo add` + `helm upgrade --install`, version pinned at add time) and `values.yaml` (local-only deltas from chart defaults — don't copy eks-gitops values; those assume IRSA, ENI, NLB).
3. Wire the addon into `kx/stack/<slice>/Taskfile.yaml`'s `enable` (install) and `disable` (uninstall) commands.
4. Run `task stack:<slice>:enable` to apply.

## Run an app's chart locally

```sh
cd /path/to/<app>/
# Pre-flight: ensure the chart's local-dev values omit AWS-only annotations
helm template my-app chart -f chart/values-development.yaml | kubectl apply -f -

# Or via helm install once the namespace exists
kubectl create namespace tenants-my-team
helm install my-app chart -n tenants-my-team -f chart/values-development.yaml
```

The chart's `aws.platformRoleArn: ""` in local-dev values means the conditional in `serviceaccount.yaml` skips the `eks.amazonaws.com/role-arn` annotation. Pods run as the SA without IRSA — which is fine for everything that doesn't actually need to hit AWS.

## Run a Platform CR locally

The `eks-agent-platform` operator can run on kx. Install it via the operator's Helm chart:

```sh
helm install eks-agent-platform /path/to/eks-agent-platform/charts/operator/ -n eks-agent-platform --create-namespace
kubectl apply -f /path/to/<app>/platform.yaml
```

The operator reconciles Namespace, ResourceQuota, NetworkPolicy, AppProject — same as production. The AWS-side reconcile (IAM role creation, KMS grants, S3 bucket policies) is skipped when the operator can't reach AWS (no IRSA on the operator pod itself in local mode).

## Conventions

- `task up` always produces a cluster named `kx` with the `kind-kx` kubeconfig context
- Default node count: 1 control-plane + 2 workers
- Local ingress lands at `localhost:80` / `localhost:443` via ingress-nginx host-port mapping
- `task status` is the canonical "what's running" command — don't kubectl-fish for it
- Tear-down with `task down` cleans the cluster; data on `kind/`-mounted volumes is wiped

## Pointers

- [`README.md`](README.md) — full repo overview, quickstart, addon list
- [`Taskfile.yaml`](Taskfile.yaml) — every supported workflow as a task
- [`CLAUDE.md`](CLAUDE.md) — Claude Code session instructions
- [Platform Reference](https://github.com/nanohype/nanohype/blob/main/docs/platform-reference.md) — the stack-wide view
- [`eks-gitops/AGENTS.md`](../eks-gitops/AGENTS.md) — the production catalog this mirrors
