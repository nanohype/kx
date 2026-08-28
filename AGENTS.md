# kx — agent entry point

You're an AI client (or the author of one) about to run a chart locally before deploying to production. This file gets you running in five minutes. For the wider picture — how this repo fits into the nanohype stack — read the [Platform Reference](https://github.com/nanohype/nanohype/blob/main/docs/platform-reference.md).

## What this repo gives you

A local Kubernetes (kind) cluster preloaded with the Helm chart catalog from [`eks-gitops`](https://github.com/nanohype/eks-gitops). Chart shapes match production EKS, so workloads developed against kx deploy unchanged to a real cluster.

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
kind export kubeconfig --name kx
kubectl create namespace my-project
helm install my-app /path/to/<app>/chart -n my-project -f /path/to/<app>/chart/values-development.yaml
```

## Contract surface

kx is the **mirror** of `eks-gitops`. Cloud-portable addons install from the same chart under the same name; AWS-specific addons have local equivalents behind the same operational surface. Two differences to know about:

- **Identity**: kx doesn't run IRSA. Pods that expect AWS credentials get them via mounted env vars or `~/.aws/credentials` — `aws.platformRoleArn` is set to `""` in local dev values, omitting the SA annotation entirely.
- **Druid post-renderer**: `stack/data/druid/` runs the production chart (`eks-gitops/catalog/druid/chart/`) unmodified through a post-renderer that strips EKS-only resources (Karpenter `NodePool`/`EC2NodeClass`, `ExternalSecret`) and EKS node-selector labels.

A chart that runs under kx is one that renders and schedules against the same
chart shapes production pins. That is what kx proves and it is narrower than it
sounds: `scripts/mirror-check.py` holds the two sides to the same chart PIN and
deliberately never compares values, so an upstream change to resource bounds, a
security context or a replica count leaves every check here green. Anything a
chart needs from AWS is exercised nowhere in this workspace.

The eks-agent-platform pin is bounded the same way and one step further.
`scripts/check-operator-flags.py` holds the flags `stack/ai-platform/operator`
hands the binary to a checkout at the pinned ref, because the chart takes them
through an untyped `extraArgs` array that helm renders verbatim — the one values
path a render cannot grade. On the blocking path nothing observes what that
repository's default branch contains: the pinned ref is checked out, rendered,
and held to those flags, and all three are facts about the commit under test.
`sibling-freshness.yml` asks the other question on a schedule — it reports how
far the pin is behind and fails when a bump would not hold, which is the half a
distance count only gestures at.

## Add an addon to the local stack

1. Check the same addon's shape in [`eks-gitops/addons/<category>/<name>/`](../eks-gitops/addons/) — that's the production reference.
2. Add `kx/stack/<slice>/<name>/` with an `install.sh` (explicit `helm repo add` + `helm upgrade --install`, version pinned at add time, explicit `--timeout`) and, unless the chart's defaults are taken whole, a `values.yaml` of local-only deltas — don't copy eks-gitops values; those assume IRSA, ENI, NLB. Extra files are for manifests that `install.sh` applies itself; nothing else belongs in the directory.
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
task stack:ai-platform:enable   # builds the operator image from the sibling checkout, kind-loads it, applies the CRDs and installs the chart with kx's values
kubectl apply -f /path/to/<app>/platform.yaml
```

The operator reconciles Namespace, ResourceQuota, NetworkPolicy, AppProject — same as production. The AWS-side reconcile (IAM role creation, KMS grants, S3 bucket policies) is switched off, not attempted and failed: `stack/ai-platform/operator/values.yaml` passes `--disable-aws`, because there is no Pod Identity association on the operator pod to authenticate it.

## Conventions

- `task up` always produces a cluster named `kx` with the `kind-kx` kubeconfig context
- Default node count: 1 control-plane + 2 workers
- Local ingress lands at `localhost:80` / `localhost:443` via ingress-nginx host-port mapping
- `task status` is the canonical "what's running" command — don't kubectl-fish for it
- `task down` deletes the cluster and the local registry container. No node carries a host mount, so everything in the cluster goes with it — a project keeps what it needs in its own manifests

## Pointers

- [`README.md`](README.md) — full repo overview, quickstart, addon list
- [`Taskfile.yaml`](Taskfile.yaml) — every supported workflow as a task
- [`CLAUDE.md`](CLAUDE.md) — Claude Code session instructions
- [Platform Reference](https://github.com/nanohype/nanohype/blob/main/docs/platform-reference.md) — the stack-wide view
- [`eks-gitops/AGENTS.md`](../eks-gitops/AGENTS.md) — the production catalog this mirrors
