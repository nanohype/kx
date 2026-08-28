# kx — agent entry point

You're an AI client (or the author of one) about to run a chart locally before deploying to production. This file gets you running in five minutes. For the wider picture — how this repo fits into the nanohype stack — read the [Platform Reference](https://github.com/nanohype/nanohype/blob/main/docs/platform-reference.md).

## What this repo gives you

A local Kubernetes (kind) cluster preloaded with the Helm chart catalog from [`eks-gitops`](https://github.com/nanohype/eks-gitops). Chart shapes match production EKS, so workloads developed against kx deploy unchanged to a real cluster.

Use it to:

- Render and run an `<app>/chart/` locally before shipping it
- Iterate on a new chart without burning a real EKS cluster's reconcile budget
- Smoke-test a `Platform` CR + `BudgetPolicy` CR pair before applying them to production
- Validate `AgentFleet` workloads end-to-end against the operator built from your
  `eks-agent-platform` working tree — a `:dev` image, kind-loaded, started with
  `--disable-aws`. Exercising the code you are editing is the point; it is not the
  released artifact and the AWS reconcile it skips is not exercised here.

## Quickstart

```sh
brew install kind helm kubectl go-task  # one-time; `task` is Taskwarrior, a different program
# Docker Desktop or OrbStack must be running — kind builds its nodes as containers.
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

kx is the **mirror** of `eks-gitops`. Cloud-portable addons install from the same chart under the same name; AWS-specific addons have local equivalents behind the same operational surface. `stack/upstream.json` carries every declared divergence with its reason, and
`scripts/mirror-check.py` fails on any that is undeclared. Two of them change how
a workload you bring here behaves:

- **Identity**: there is no Pod Identity on kind. On a real cluster a workload reaches AWS
  as itself — the operator points each Platform's data plane at a ServiceAccount carrying a
  Pod Identity association, and the credential chain resolves from there. Locally that chain
  finds nothing, so an AWS call fails until the opt-in `bedrock-credentials` slice clones a
  profile into each `tenants-*` namespace. That slice states its own fidelity gap: one
  credential is shared by every local tenant, so local tenants are not isolated from each
  other on the AWS side and a local Bedrock call is not a test of the production authz path.
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
4. Record the pin in `stack/chart-provenance.json` — repo URL and a description. `scripts/check-chart-deprecation.py` fails a pin with no record, and a record naming no pin.
5. If the catalog does not pin this chart, declare the divergence in `stack/upstream.json` with a reason and a `kind` of `kx-only`. `scripts/mirror-check.py` fails on any difference from the catalog that is not declared.
6. Run `task stack:<slice>:enable` to apply.

Steps 4 and 5 are not bookkeeping. A pin added without them reddens two required
jobs, and the reason each gate gives names the file to edit.

## Run an app's chart locally

```sh
cd /path/to/<app>/
kubectl create namespace tenants-my-team

# Pre-flight: render and read it before applying anything
helm template my-app chart -n tenants-my-team -f chart/values-development.yaml

helm install my-app chart -n tenants-my-team -f chart/values-development.yaml
```

A chart that binds AWS identity through a ServiceAccount annotation will not get credentials
here, and annotating a ServiceAccount with a role ARN is not the estate's mechanism in any
case. Everything that does not reach AWS runs unchanged; everything that does needs the
`bedrock-credentials` slice above.

## Run a Platform CR locally

The `eks-agent-platform` operator can run on kx. Install it via the operator's Helm chart:

```sh
task stack:ai-platform:enable   # builds the operator image from the sibling checkout, kind-loads it, applies the CRDs and installs the chart with kx's values
kubectl apply -f /path/to/<app>/platform.yaml
```

A Platform reconciles without AWS, but its model gateway answers nothing until the
gateway holds a credential. To take it end to end:

```sh
task stack:security:enable                  # bedrock-credentials needs Kyverno
task stack:ai-platform:credentials          # clone a local AWS profile into each tenant namespace
task stack:ai-platform:credentials:verify   # assert every gateway data plane carries it
task stack:ai-platform:conformance PLATFORM=<name>   # send real traffic through the gateway
```

The conformance target is the only check here that sends a byte. Everything else —
`task status`, the credentials verify, the render gate — reads a resource, and a
gateway that answers nothing looks identical to a working one in all of them.

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
