# Threat model

kx runs on one workstation, for the one person who uses it. This document
covers what kx holds on that person's behalf, who can reach each thing, what
bounds it, and what kx accepts as residual risk.

Each surface is taken through STRIDE:

- spoofing;
- tampering;
- repudiation;
- information disclosure;
- denial of service;
- elevation of privilege.

A category is listed under a surface only where it applies there.

## Scope

**In scope:**

- the kind cluster and the containers it runs;
- the ports kx publishes on the workstation;
- the credentials kx stores;
- the artifacts kx fetches: charts, images, manifests and tools;
- the workflows that gate this repository.

**Adversaries considered:**

- someone on the same network as the workstation;
- a workload in the cluster, whether a project's or a compromised addon;
- an upstream publisher, compromised or careless;
- the author of a pull request.

**Not defended against:**

- **The workstation's user, and any process running as them.** kx works for
  them.
- **Anyone who can reach the Docker daemon.** The nodes are containers, so
  etcd, every Secret and every volume can be read from there.
- **Production.** eks-gitops and its cloud account have their own threat model.
  kx holds no production credential, and the only cloud credential it can hold
  is the expiring session [below](#the-bedrock-credentials-slice).

## Published ports and default credentials

**What is exposed.** kx publishes:

- the Ingress listener on `localhost:80` and `localhost:443`;
- the ArgoCD UI on `localhost:30080`;
- Grafana on `localhost:30030`, with the observability slice;
- the local registry on `localhost:5001`.

kind publishes the API server on a loopback port of its own choosing.

Behind those ports sit defaults chosen for one person on one machine:

- Grafana's admin password is `admin`, in
  `stack/observability/kube-prometheus-stack/values.yaml`.
- ArgoCD's UI is plain HTTP (`server.insecure`, in
  `stack/core/argo-cd/values.yaml`). Its admin password is generated, and
  `task argocd:initial-password` prints it.
- The registry has no authentication.

Two services with default credentials publish nothing. The data slice's object
store and Druid (`stack/data/druid/pre-install/secrets.yaml`) are reached
in-cluster or through `kubectl port-forward`, which binds loopback.

| | Threat | Bound |
|---|---|---|
| Spoofing | Anyone who reaches Grafana signs in as its admin with the default password. Anyone who reaches the registry pushes an image under any name. | Every published port binds 127.0.0.1 ([0005](adr/0005-host-ports-bind-to-loopback.md)), and the lint job fails a kind mapping that does not. |
| Tampering | An image pushed to the registry replaces what a project pulls from `localhost:5001`. | Only the workstation can reach the registry. |
| Information disclosure | The ArgoCD UI and Grafana serve plain HTTP, so their sessions and passwords cross the wire unencrypted. | That traffic never leaves the workstation's loopback interface. |
| Elevation of privilege | ArgoCD's admin can create an Application that deploys anything into the cluster. Grafana's admin can change its datasources. | The loopback binding, and ArgoCD's generated password. ArgoCD manages nothing kx installs. |

**Residual.** Loopback does not separate kx from the workstation's own
processes, a browser included: a page the user visits can send requests to a
loopback port. The defaults are accepted because the cluster holds no
production data and no long-lived credential. The one credential it can hold is
a session that expires.

## The kube context

**What is at stake.** Every other cluster the user's kubeconfig can reach,
production included. kx's installers apply manifests, install and uninstall
releases, and delete namespaces. Run against the wrong context, `task up` would
install the core stack into another cluster, and a slice's `disable` would
remove releases from it.

| | Threat | Bound |
|---|---|---|
| Tampering, denial of service | A kx task acts on whichever cluster the shell's `KUBECONFIG` or current context names. | kx keeps its own kubeconfig ([0006](adr/0006-kx-keeps-its-own-kubeconfig.md)). `.kube/config` holds `kind-kx` alone. Every script sources `cluster/kubeconfig.sh`, which points `KUBECONFIG` at that file and stops unless the context is `kind-kx`. Every Taskfile command names the file. `scripts/check-slice-integrity.py` holds every script and task to this. |
| Information disclosure | kind writes its cluster-admin credential into the kubeconfig the user shares across clusters and tools. | kx's credential lives only in `.kube/config`, which git ignores and `task down` deletes. |

**Residual.** None within kx's reach. A command the user types by hand, with
their own kubeconfig, is theirs.

## The bedrock-credentials slice

**What it holds.** The session an AWS profile resolves to: access key, secret
key and session token. It is stored as the Secret
`kyverno/kx-bedrock-credentials` and cloned into every `tenants-*` namespace.
Each model gateway's `ai-gateway-extproc` sidecar, the container that signs the
Bedrock call, carries a reference to its namespace's copy
([0010](adr/0010-bedrock-credentials.md)).

| | Threat | Bound |
|---|---|---|
| Spoofing | Any workload that can read a tenant's copy calls AWS as the profile's identity, with its permissions. | Only an expiring session is accepted. `stack/ai-platform/bedrock-credentials/credentials.py` refuses credentials with no session token, no expiry, or an expiry already past, before the installer writes anything. Every copy stops working when the session does. |
| Tampering | A tenant's copy is edited or deleted by hand. | The GeneratingPolicy keeps every copy equal to the source, and puts back one edited or deleted by hand. |
| Repudiation | Every local tenant calls Bedrock as the same identity, so AWS's records cannot tell local tenants apart. | Accepted, as the fidelity gap below. In production each tenant has its own role. |
| Information disclosure | The keys pass through processes on the workstation. | They never reach a process's argv or environment. The installer hands them to `credentials.py` on stdin, and `credentials.py` writes the Secret manifest to kubectl on stdin. `scripts/check-slice-integrity.py` holds every script to keeping secrets off argv. In the gateway pod, only the signing sidecar carries the reference, and Envoy and the other containers never see the credential. |
| Elevation of privilege | Kyverno's background controller holds cluster-wide Secret access while the slice is installed. | Residual, below. Disabling the slice withdraws the grant. |

Two tasks complete the lifecycle:

- `task stack:ai-platform:credentials:verify` reports when the session expires,
  and fails once it has.
- `task stack:ai-platform:credentials:disable` deletes the policies, the
  copies, the source and Kyverno's grant, in that order.

**Residual: Kyverno's cluster-wide Secret access.** While the slice is
installed, Kyverno's background controller may get, list, watch, create,
update and delete Secrets in every namespace
(`stack/ai-platform/bedrock-credentials/rbac.yaml`).

- It needs the read to clone the source, and the writes to keep each copy.
- RBAC cannot scope a grant to namespaces chosen by a name prefix.
- So anything that can direct that controller can read every Secret in the
  cluster, not only this one. That means a policy someone installs, or a
  compromise of Kyverno itself.

This is accepted for three reasons: the cluster is a local kind cluster with
one user, the credential expires by itself, and disabling the slice withdraws
the grant.

**Fidelity gap.** Every local tenant shares the one identity, with the
profile's own permissions. Unlike production, local tenants are not isolated
from each other on the AWS side, and a local Bedrock call is not a test of the
production authorization path. `stack/ai-platform/bedrock-credentials/install.sh`
states this beside the code it describes.

## Egress from the cluster

| | Threat | Bound |
|---|---|---|
| Information disclosure | A compromised workload sends out whatever it can read. | Cilium enforces a NetworkPolicy wherever one exists, and the policy layer is default-allow (`policyEnforcementMode: default`). A project that needs egress restricted states it in its own NetworkPolicy. |
| Information disclosure | Pods' lookups for external names go to 1.1.1.1 and 8.8.8.8 rather than the network's own resolver ([0016](adr/0016-coredns-public-resolvers.md)). | Accepted. Those resolvers see the names pods look up, and nothing else. |

## The supply chain

**What is at stake.** Everything that runs in the cluster, and every tool CI
runs.

| | Threat | Bound |
|---|---|---|
| Tampering: charts | A pin drifts unnoticed, or a chart changes product or maintainer under the same name. | Every chart from a repository is pinned to an exact version in its `install.sh`. Renovate watches every pin (`scripts/check-renovate-coverage.py`). `scripts/mirror-check.py` holds every pin to the eks-gitops catalog at a recorded ref, and any divergence is declared with a reason. `stack/chart-provenance.json` records what each chart says it is, and a scheduled run reports a chart that has been deprecated or has changed hands. |
| Tampering: images | A floating tag resolves to a different image on the next pull. | `scripts/check-images.py`, run with `--pins`, refuses a floating tag in anything the stack renders. The node image and the registry image are pinned by digest. |
| Tampering: manifests and tools | A release host serves a different file, or a transfer arrives truncated or replaced. | A file a script downloads and then applies or unpacks is checked against a recorded digest first, and so is every tool CI installs. `scripts/check-slice-integrity.py` holds the scripts to it. See the list below. |
| Elevation of privilege | A pinned image carries a known CRITICAL vulnerability. | A weekly scan fails on any CRITICAL that no unexpired, time-boxed exception covers ([0013](adr/0013-image-cve-exceptions.md)), and a red run opens a tracking issue. trivy-operator, in the security slice, scans what a running cluster runs. |

The fetched artifacts checked against a recorded digest:

- the Gateway API CRDs, fetched to a file and checked against a sha256 before
  they reach the API server;
- every binary CI installs, against a sha256;
- the Python tools, installed with `--require-hashes`;
- every action, pinned to a commit;
- the vendored schemas, against their module hash and per-file sha256.

**Residual.**

- **Versions are names, not digests.** A version tag is a name its publisher
  can repoint, and a chart version is one its repository can republish. kx
  records versions rather than digests for its charts and for most images, so a
  republished version changes what installs.
- **Publishers are trusted at the pinned version.** A malicious release that
  carries no known vulnerability passes every check here.
- **Two charts come from sibling checkouts.** Druid's chart is read from the
  eks-gitops checkout beside kx, and the operator's from the eks-agent-platform
  checkout. Each installs as that checkout stands.
- **Only CRITICAL fails the scan.** Severities below it do not fail the
  scheduled scan.

## CI token scope

| | Threat | Bound |
|---|---|---|
| Elevation of privilege | A step, an action or a pull request uses the workflow token to write to the repository. | See "Token permissions" below. |
| Tampering | A later step reads the token from the checkout's git config. | Every checkout sets `persist-credentials: false`. |
| Tampering | A branch name or another expression is substituted into a shell script. | Workflow values reach `run` blocks through the environment, and no `run` block contains a `${{ }}` expression. zizmor audits every workflow and action in the lint job. |
| Tampering | An action changes under the tag a workflow names. | Every action is pinned to a commit, the merge gate's included. |
| Spoofing | A pull request reaches the merge gate without its checks having passed. | The merge gate requires every other job in `.github/workflows/ci.yml`, counts a skipped job as failed, and refuses a job it does not watch. |

**Token permissions.**

- Every workflow declares `permissions: contents: read`.
- The one write is `issues: write`, on the image scan's job, which opens or
  comments on its tracking issue.
- No workflow runs on `pull_request_target`, so a pull request's run holds a
  read-only token.
- No workflow reads a repository secret.

**Residual.** A pinned action is trusted as it was at the commit it is pinned
to. That includes the merge gate's, which lives in the organization's
`.github` repository.
