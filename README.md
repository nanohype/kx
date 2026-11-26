# kx

A local Kubernetes (kind) cluster preloaded with the Helm chart catalog from [eks-gitops](https://github.com/nanohype/eks-gitops). The chart shapes match production EKS so workloads developed against kx deploy unchanged.

**AI clients / agents start here:** [`AGENTS.md`](AGENTS.md). For the stack-wide view, see the [Platform Reference](https://github.com/nanohype/nanohype/blob/main/docs/platform-reference.md).

## Prerequisites

```
brew install kind helm kubectl go-task
```

`task` is Taskwarrior and conflicts with go-task; the binary this repo's Taskfiles need comes from `go-task`.

Docker Desktop or OrbStack must be running — kind builds its nodes as containers.

## Quickstart

```
task up                                  # cluster + core stack
task status                              # nodes, pods, helm releases
task stack:observability:enable          # enable an opt-in slice
task down                                # tear down the cluster
```

kx keeps its cluster's kubeconfig in `.kube/config` at the root of this repository and never reads, writes or
switches the context of yours. Every task and every script is bound to that file, so no kx command can act on
another cluster, whatever your shell's `KUBECONFIG` or current context names.

Projects target the cluster by convention: pick a namespace and point kubectl at kx's kubeconfig.

```
export KUBECONFIG=/path/to/kx/.kube/config   # stays current across task reset
kubectl create namespace my-project
kubectl -n my-project apply -f ...
```

`kind export kubeconfig --name kx` merges the `kind-kx` context into your own kubeconfig instead, and makes it
the current context there. Run it again after `task reset`, which gives the API server a new address.

## What's installed

**Core** — always on after `task up`:

| Addon | Role |
|---|---|
| cilium | CNI + eBPF networking, kube-proxy replacement, Hubble observability, and the Ingress and Gateway API controller — Ingress at `localhost:80` / `localhost:443`, its `cilium` class the default |
| gateway-api CRDs | `gateway.networking.k8s.io` CRDs (experimental channel) |
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
| ai-platform | envoy-ai-gateway (+ CRDs), envoy-gateway (+ CRDs), eks-agent-platform operator | `task stack:ai-platform:enable` |
| ai-platform → credentials | local AWS identity for the model gateways (needs the security slice) | `task stack:ai-platform:credentials` |

Each slice has a matching `:disable` target; the core stack stays up. `task stack:all:enable` enables every slice in a single command (excluding druid).

## Layout

```
cluster/   kind config, cluster lifecycle tasks, local registry + coredns setup
docs/      architecture, threat model, and the decision records under adr/
stack/
  core/    always-on addons
  <slice>/ opt-in addons grouped by use case
scripts/   CI gates (render, schema, operator flags, mirror, chart provenance, renovate coverage), the library they share under gatelib/, and the Kyverno and Python test runners
schemas/   what the schema gate holds resources to, vendored from the pinned Kubernetes and Cilium releases
tests/     fixtures the gates prove themselves against, the Kyverno policy suites, and the Python tests (task test:python)
Taskfile.yaml
```

`task up` also starts a container registry at `localhost:5001` and points every
node's containerd at it, so `docker push localhost:5001/foo:tag` is pullable
from inside the cluster without a remote registry. `task down` removes it.

Each addon directory contains an `install.sh` (an explicit `helm upgrade --install`, chart version pinned,
with an explicit `--timeout` and a deadline past it that bounds the chart fetch, and a bound on every other call that leaves the machine) and, for the charts that need one, a `values.yaml` of deltas from chart defaults.
A few carry more or fewer: CRD-only installers take chart defaults whole and have no values, and `druid` and
`bedrock-credentials` ship the extra manifests their install applies and the Python helpers it runs.

## When something breaks

Every `install.sh` is idempotent, so the first move for a slice that failed partway is to run
`task stack:<slice>:enable` again. The failure modes that are not self-healing:

| Symptom | Cause | Remedy |
|---|---|---|
| `task up` fails immediately | Docker Desktop / OrbStack not running | Start it; kind needs a container runtime |
| A task or script stops with `.kube/config holds no kind-kx context` | There is no cluster, `task down` removed its kubeconfig, or the cluster was created by a kx that kept its context in your own kubeconfig | `task up` — it creates the cluster, or writes `.kube/config` for one that already exists |
| `localhost:80` / `:443` reset the connection on a cluster that once ran ingress-nginx | kind fixes port mappings when it creates a cluster, and that cluster's still point at the ports ingress-nginx held | `task reset` |
| A slice enables but pods sit `Pending` | kind node is out of CPU/memory — the data slice and druid are the usual causes | Raise the VM's limits, or disable a slice you are not using |
| A Gateway reports `Programmed=False`, reason `AddressNotAssigned` | Cilium gives each Gateway a LoadBalancer Service, and nothing on kind assigns one an address. The Gateway is still Accepted | Expected on kind, where Gateways are reached in-cluster through their Service, `cilium-gateway-<name>`. From the host, use an Ingress on `localhost:80` / `:443` |
| `task stack:ai-platform:enable` fails on the operator | No sibling `eks-agent-platform` checkout — the operator image is built locally, not published | Clone it beside `kx`, or set `KX_EKS_AGENT_PLATFORM_DIR` |
| `task stack:data:druid:enable` fails on a missing chart | Druid runs the unmodified `eks-gitops` chart from a sibling checkout | Clone `eks-gitops` beside `kx`, or set `KX_DRUID_CHART_DIR` |
| Bedrock calls return `AccessDeniedException` | The `bedrock-credentials` slice is not installed, the profile's session expired (`task stack:ai-platform:credentials:verify` reports when it does), or the account does not allow Bedrock in the signing region | `aws sso login`, then `task stack:ai-platform:credentials`; `AWS_REGION` picks the signing region (default us-east-1) |
| `bedrock-credentials` refuses to install | Kyverno is absent, or older than the GeneratingPolicy and MutatingPolicy kinds the slice declares | `task stack:security:enable` first |
| `bedrock-credentials` refuses the profile's credentials | They carry no expiry or session token — a long-lived access key, or a session the aws CLI reported no expiry for — or the session has already expired. A copy in every tenant namespace has to stop working by itself | Use an SSO or assumed-role profile, renewed (`aws sso login`) |
| A Kyverno chart upgrade fails on the `kyverno-check-legacy-policies` hook | Kyverno 1.20 no longer runs `kyverno.io/v1` policies, and an earlier `bedrock-credentials` install left its ClusterPolicy behind | `task stack:ai-platform:credentials` once — it retires the old policy and its copies before installing the replacements — then retry the upgrade |
| Credentials installed but gateways still fail | Envoy Gateway regenerates the data plane on its own schedule; running proxies predate the Secret | `kubectl delete pod -A -l app.kubernetes.io/managed-by=envoy-gateway,app.kubernetes.io/component=proxy` |
| A chart bump fails `Validate every rendered resource against a schema` | A rendered resource no longer matches its schema — the API server would reject it too | Read the reported path (e.g. `/spec/endpoints/0/port`). The schema is the render's own CRD, Cilium's vendored at its pin, or Kubernetes' own at the node image's version, so this is a real incompatibility |
| The schema gate refuses with `is vendored, but kx pins` | The node image in `cluster/kind-config.yaml` or Cilium's chart pin moved, and the schemas are vendored per release | `scripts/vendor-schemas.py kubernetes <version>` or `scripts/vendor-schemas.py cilium <version>`, then commit `schemas/` |
| `mirror-check.py check` refuses to run | It reads `eks-gitops` at the exact ref in `stack/upstream.json`, and the checkout is elsewhere | Check that commit out, or ask `freshness` instead, which compares against upstream's default branch |
| The weekly `image cves` run is red | Its job summary names the state. **findings**: a CRITICAL nothing covers, or an exception that expired or matches nothing. **could not run**: the scanner or a registry could not answer, which says nothing about the images | For findings, move the chart pin to a fixed image, or record a time-boxed exception in `stack/image-cve-exceptions.json` with the reason. For could not run, fix the scan's reach |

Recovery of last resort is `task reset` (down, then up) — the cluster holds no state worth
preserving, so rebuilding it is cheaper than debugging it. Anything you needed to keep should
have been in a project namespace with its own manifests.

## Versions

Chart versions are pinned in each `install.sh`. To refresh, run `helm repo update && helm search repo <chart>` and update the pin.
