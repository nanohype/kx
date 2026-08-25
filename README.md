# kx

A local Kubernetes (kind) cluster preloaded with the Helm chart catalog from [eks-gitops](https://github.com/nanohype/eks-gitops). The chart shapes match production EKS so workloads developed against kx deploy unchanged.

**AI clients / agents start here:** [`AGENTS.md`](AGENTS.md). For the stack-wide view, see the [Platform Reference](https://github.com/nanohype/nanohype/blob/main/docs/platform-reference.md).

## Prerequisites

```
brew install kind helm kubectl task
```

Docker Desktop or OrbStack must be running.

## Quickstart

```
task up                                  # cluster + core stack
task status                              # nodes, pods, helm releases
task stack:observability:enable          # enable an opt-in slice
task down                                # tear down the cluster
```

Projects target the cluster by convention: pick a namespace and point kubeconfig at the `kind-kx` context.

```
kind export kubeconfig --name kx
kubectl create namespace my-project
kubectl -n my-project apply -f ...
```

## What's installed

**Core** — always on after `task up`:

| Addon | Role |
|---|---|
| cilium | CNI + eBPF networking, kube-proxy replacement, Hubble observability, Gateway API controller |
| gateway-api CRDs | `gateway.networking.k8s.io` CRDs (experimental channel) |
| ingress-nginx | Cluster ingress at `localhost:80` / `localhost:443` |
| cert-manager | TLS certificate issuance |
| trust-manager | CA bundle distribution via the `Bundle` CR |
| metrics-server | Source for `kubectl top` and HPA |
| prometheus-operator-crds | `ServiceMonitor` / `PodMonitor` / `PrometheusRule` CRDs |
| reloader | Pod restart on annotated ConfigMap/Secret change |
| argo-cd | Installed but idle; UI at `http://localhost:30080` (NodePort on loopback, no port-forward needed) |

**Opt-in slices** — enable on demand:

| Slice | Charts | Command |
|---|---|---|
| observability | kube-prometheus-stack, loki, tempo, otel-collector, grafana-operator, opencost | `task stack:observability:enable` |
| security | kyverno, falco, trivy-operator | `task stack:security:enable` |
| autoscaling | keda, vpa, goldilocks, descheduler | `task stack:autoscaling:enable` |
| argo-platform | argo-events, argo-rollouts, argo-workflows | `task stack:argo-platform:enable` |
| secrets | external-secrets (kubernetes provider) | `task stack:secrets:enable` |
| data | minio, velero, cloudnative-pg, nats | `task stack:data:enable` |
| data → druid | apache druid (~4.5 GB resident) | `task stack:data:druid:enable` (requires the data slice) |
| ai-platform | envoy-ai-gateway (+ CRDs), envoy-gateway, eks-agent-platform operator | `task stack:ai-platform:enable` |
| ai-platform → credentials | local AWS identity for the model gateways (needs the security slice) | `task stack:ai-platform:credentials` |

Each slice has a matching `:disable` target; the core stack stays up. `task stack:all:enable` enables every slice in a single command (excluding druid).

## Layout

```
cluster/   kind config, cluster lifecycle tasks, local registry + coredns setup
stack/
  core/    always-on addons
  <slice>/ opt-in addons grouped by use case
scripts/   CI gates (render, schema, mirror, chart provenance, renovate coverage)
tests/     fixtures the gates prove themselves against
Taskfile.yaml
```

`task up` also starts a container registry at `localhost:5001` and points every
node's containerd at it, so `docker push localhost:5001/foo:tag` is pullable
from inside the cluster without a remote registry. `task down` removes it.

Each addon directory contains an `install.sh` (an explicit `helm upgrade --install`, chart version pinned,
with an explicit `--timeout`) and, for the charts that need one, a `values.yaml` of deltas from chart defaults.
A few carry more or fewer: CRD-only installers take chart defaults whole and have no values, and `druid` and
`bedrock-credentials` ship the extra manifests their install applies.

## When something breaks

Every `install.sh` is idempotent, so the first move for a slice that failed partway is to run
`task stack:<slice>:enable` again. The failure modes that are not self-healing:

| Symptom | Cause | Remedy |
|---|---|---|
| `task up` fails immediately | Docker Desktop / OrbStack not running | Start it; kind needs a container runtime |
| A slice enables but pods sit `Pending` | kind node is out of CPU/memory — the data slice and druid are the usual causes | Raise the VM's limits, or disable a slice you are not using |
| `task stack:ai-platform:enable` fails on the operator | No sibling `eks-agent-platform` checkout — the operator image is built locally, not published | Clone it beside `kx`, or set `KX_EKS_AGENT_PLATFORM_DIR` |
| `task stack:data:druid:enable` fails on a missing chart | Druid runs the unmodified `eks-gitops` chart from a sibling checkout | Clone `eks-gitops` beside `kx`, or set `KX_DRUID_CHART_DIR` |
| Bedrock calls return `AccessDeniedException` | The `bedrock-credentials` slice is not installed, the profile's SSO session expired, or the account does not allow Bedrock in the signing region | `aws sso login`, then `task stack:ai-platform:credentials`; `AWS_REGION` picks the signing region (default us-east-1) |
| `bedrock-credentials` refuses to install | Kyverno is absent — the slice is a mutating policy | `task stack:security:enable` first |
| Credentials installed but gateways still fail | Envoy Gateway regenerates the data plane on its own schedule; running proxies predate the Secret | `kubectl delete pod -A -l app.kubernetes.io/managed-by=envoy-gateway,app.kubernetes.io/component=proxy` |
| A chart bump fails `Validate every rendered resource against a schema` | A rendered resource no longer matches its CRD — the API server would reject it too | Read the reported path (e.g. `/spec/endpoints/0/port`); for any kind whose CRD is in the render, this is a real incompatibility |
| ...but the failing kind is a `Cilium*` resource | Cilium registers its CRDs at operator runtime, so that schema comes from the community catalog and is **not** version-matched to the pinned chart | Triage schema skew first — compare against the CRD the running operator registered before treating the manifest as wrong |
| `mirror-check.py check` refuses to run | It reads `eks-gitops` at the exact ref in `stack/upstream.json`, and the checkout is elsewhere | Check that commit out, or ask `freshness` instead, which compares against upstream's default branch |

Recovery of last resort is `task reset` (down, then up) — the cluster holds no state worth
preserving, so rebuilding it is cheaper than debugging it. Anything you needed to keep should
have been in a project namespace with its own manifests.

## Versions

Chart versions are pinned in each `install.sh`. To refresh, run `helm repo update && helm search repo <chart>` and update the pin.
