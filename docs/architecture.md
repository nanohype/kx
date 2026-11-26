# Architecture

kx is a kind cluster on one workstation, running the charts the
[eks-gitops](https://github.com/nanohype/eks-gitops) catalog runs in
production, so a project developed against it deploys unchanged to a real
cluster. This document covers:

- the cluster;
- the stack installed into it;
- who owns what once it runs;
- the gates that hold the repository to its own rules.

The reasons behind each part are in the [decision records](adr/README.md). What
kx holds on its user's behalf, and what bounds it, is in the
[threat model](threat-model.md).

## The cluster

`task up` creates a kind cluster named `kx` from `cluster/kind-config.yaml`.
It has one control-plane node and two workers, on a node image pinned by digest
to the Kubernetes version of the production target. kind's own CNI and
kube-proxy are off, because Cilium replaces both, so the nodes stay NotReady
until the core stack installs it.

Two scripts finish the cluster:

- `cluster/registry.sh` runs a registry container at `localhost:5001` and
  points every node's containerd at it. An image pushed there can be pulled
  inside the cluster without a remote registry.
- `cluster/coredns-patch.sh` points CoreDNS at public resolvers. The resolver a
  kind node inherits from Docker Desktop cannot be reached from the pod network
  ([0016](adr/0016-coredns-public-resolvers.md)).

The cluster's credentials live in `.kube/config` at the repository root, with
the `kind-kx` context alone. Every task and script names that file, and
nothing in kx reads, writes or switches the context of the user's own
kubeconfig ([0006](adr/0006-kx-keeps-its-own-kubeconfig.md)). `task down`
deletes the cluster, that file and the registry container.

The host reaches the cluster on these ports, each bound to 127.0.0.1
([0005](adr/0005-host-ports-bind-to-loopback.md)):

| Host port | Serves |
|---|---|
| `localhost:80`, `localhost:443` | Cilium's shared Ingress listener ([0003](adr/0003-cilium-serves-ingress.md)) |
| `localhost:30080` | the ArgoCD UI |
| `localhost:30030` | Grafana, once the observability slice is enabled |
| `localhost:5001` | the local registry |

kind publishes the API server itself, on a loopback port of its own choosing.

## The stack

Every addon is a directory under `stack/<slice>/<addon>/`. It holds an
`install.sh` that runs one pinned `helm upgrade --install`, and a `values.yaml`
of local deltas from the chart's defaults where the addon needs one
([0001](adr/0001-explicit-install-scripts.md)). The installers are the whole
lifecycle mechanism. No helmfile, wrapper or GitOps controller stands between a
reader and what runs.

### Slices

- **Core** is always on. `task up` installs it through `stack/Taskfile.yaml`,
  in dependency order:
  1. the Gateway API CRDs, which Cilium's Gateway API controller registers
     against at startup;
  2. Cilium, the CNI and the Ingress and Gateway API controller, which brings
     the nodes Ready;
  3. the prometheus-operator CRDs, which any chart creating a ServiceMonitor
     relies on;
  4. cert-manager and trust-manager;
  5. metrics-server;
  6. reloader and ArgoCD.
- **Opt-in slices** group addons by use: observability, security,
  autoscaling, argo-platform, secrets, data and ai-platform.
  - Each has a `stack/<slice>/Taskfile.yaml`. Its `enable` runs the installers
    in order, and its `disable` removes them.
  - `task stack:all:enable` and `task stack:all:disable` act on every slice.
  - The [README](../README.md#whats-installed) lists each slice's addons and
    the task that enables it.

### Inside a slice

Order inside a slice follows the same dependency rule: CRDs before the
controllers that watch them, and a controller before anything it reconciles.

Two addons cannot be helm releases, and their installers say why:

- **Druid** runs the production chart from the eks-gitops catalog unmodified.
  It is rendered with `helm template`, filtered by
  `stack/data/druid/post-renderer.py` and applied with kubectl
  ([0008](adr/0008-druid-through-a-post-renderer.md)).
- **envoy-gateway-crds** renders its chart and applies it server-side, because
  the stored release would not fit the Secret helm keeps it in. The slice's
  disable runs its `uninstall.sh` instead of `helm uninstall`
  ([0009](adr/0009-oversized-charts-apply-server-side.md)).

Where production uses an AWS service kind cannot run, the local equivalent
takes the directory that role would occupy, under its own name, and its
installer says what it stands in for
([0002](adr/0002-substitutes-keep-production-names.md)). The full mapping is in
[CLAUDE.md](../CLAUDE.md).

## Who owns what

- **kx owns the platform.** The namespaces its addons install into are kx's,
  and the installers are what change them. A slice's `disable` is the way to
  take one out.
- **ArgoCD is installed and idle.** It runs in core, so a project can be tested
  against it. Nothing kx installs is an ArgoCD Application, an App-of-Apps or
  an ApplicationSet.
- **A project owns its namespace.** kx keeps no registry of projects. A project
  targets the `kind-kx` context with a namespace of its own, and keeps what it
  needs in its own manifests. No node carries a host mount, so `task down`
  takes everything in the cluster with it.
- **The operator owns tenant namespaces.** With the ai-platform slice, the
  eks-agent-platform operator reconciles each Platform into a `tenants-<name>`
  namespace. Envoy Gateway runs each tenant's gateway in that namespace, as it
  does in production. The operator is built from a sibling eks-agent-platform
  checkout and loaded into kind, since its image is not published.
- **The credentials slice owns one Secret and its copies.** With
  bedrock-credentials enabled, Kyverno clones one AWS session into every tenant
  namespace and points each gateway's signing sidecar at it
  ([0010](adr/0010-bedrock-credentials.md)).

## The gate layer

The repository holds itself to rules a reader cannot check by eye, and each rule
is a gate under `scripts/`. Every gate runs without a cluster.

| Gate | Holds |
|---|---|
| `scripts/render-check.sh` | Every slice renders with `helm template` at its pinned version, and no rendered container mounts two volumes on one path (`scripts/check-rendered-mounts.py`). |
| `scripts/check-rendered-schemas.py` | Every rendered resource validates against the schemas under `schemas/`, vendored from the pinned Kubernetes and Cilium releases ([0012](adr/0012-vendored-schemas.md)). |
| `scripts/check-images.py` | With `--pins`, every image a pinned chart renders names a version or a digest rather than a floating tag, and every tag pinned by hand names its chart's release. With `--cves`, on a schedule, no image carries an uncovered CRITICAL ([0013](adr/0013-image-cve-exceptions.md)). |
| `scripts/check-operator-flags.py` | Every flag the ai-platform slice hands the operator is one its binary defines. |
| `scripts/mirror-check.py` | Every chart pin matches the eks-gitops catalog at the ref `stack/upstream.json` records, or is a divergence declared there with a reason. |
| `scripts/check-renovate-coverage.py` | Every chart pin is watched by a Renovate manager. |
| `scripts/check-chart-deprecation.py` | Every chart pin has a record in `stack/chart-provenance.json`, and every record a pin. On a schedule, no pinned chart has been deprecated or has changed hands upstream. |
| `scripts/kyverno-test.sh` | The Kyverno policies behave as the suites under `tests/kyverno` specify. |
| `scripts/check-slice-integrity.py` | The tree's own invariants, and that every gate above rejects what it exists to reject ([0014](adr/0014-gates-prove-they-reject.md)). |
| `scripts/check-prose-voice.py` | Advisory, not a gate. It lists the prose a change adds, for a second reader. |

`scripts/check-slice-integrity.py` is the floor under the rest. Before it
reports on the tree, it runs a control for each of its invariants: a fixture it
must reject and one it must accept. It also runs every gate against inputs that
gate must reject and accept, and checks that each gate refuses when the tool
or source its verdict depends on is missing. Its invariants include these:

- every outbound call is bounded ([0007](adr/0007-every-outbound-call-is-bounded.md));
- nothing kx runs can reach a cluster other than `kind-kx`;
- every file in an addon directory is applied;
- every fetched artifact is verified before it is used;
- every path named in markdown resolves.

Every tool a workflow installs is pinned to a version and checked against a
recorded digest:

- the Python tools, from the hash-locked requirement files under `.github/`,
  with `--require-hashes`;
- helm, through `.github/actions/helm`;
- each other binary, from its release, with a sha256.

### When the gates run

The gates split by the kind of question they answer
([0011](adr/0011-blocking-and-scheduled-checks.md)):

- **Blocking.** `.github/workflows/ci.yml` runs on every pull request and every
  push to `main`. It asks only questions whose answer is a fact about the
  commit. A merge gate job requires every other job, and counts a skipped one
  as failed.
- **Scheduled.** Four workflows run weekly, and on demand. Each asks a question
  whose answer changes without this repository changing:
  - `.github/workflows/chart-provenance.yml`: has a pinned chart been
    deprecated or changed hands?
  - `.github/workflows/mirror-freshness.yml`: has the catalog moved past the
    pin?
  - `.github/workflows/image-cves.yml`: does a pinned image carry a CRITICAL?
  - `.github/workflows/sibling-freshness.yml`: has eks-agent-platform moved
    past its pin, and would the move render?

## The records the gates read

The facts the gates hold the tree to live in files beside the code. Each file
carries a note on how to change it:

| File | Records |
|---|---|
| `stack/upstream.json` | the eks-gitops ref kx mirrors, the sibling repositories' refs, and every declared divergence from the catalog |
| `stack/chart-provenance.json` | what each pinned chart says it is |
| `stack/image-cve-exceptions.json` | each accepted CRITICAL, with its reason and expiry |
| `schemas/sources.json` | where each vendored schema came from, and its sha256 |
| `renovate.json` | the managers that watch every pin |
