# kx

A local Kubernetes (kind) cluster preloaded with the Helm chart catalog from [eks-gitops](https://github.com/stxkxs/eks-gitops). The chart shapes match production EKS so workloads developed against kx deploy unchanged.

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
export KUBECONFIG=$(kind get kubeconfig --name kx --internal=false | psub)
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
| argo-cd | Installed but idle; UI at `localhost:30080` |

**Opt-in slices** — enable on demand:

| Slice | Charts | Command |
|---|---|---|
| observability | kube-prometheus-stack, loki, tempo, grafana-operator, opencost | `task stack:observability:enable` |
| security | kyverno, falco, trivy-operator | `task stack:security:enable` |
| autoscaling | keda, vpa, goldilocks, descheduler | `task stack:autoscaling:enable` |
| argo-platform | argo-events, argo-rollouts, argo-workflows | `task stack:argo-platform:enable` |
| secrets | external-secrets (kubernetes provider) | `task stack:secrets:enable` |
| data | minio, velero, cloudnative-pg, nats | `task stack:data:enable` |
| data → druid | apache druid (~4.5 GB resident) | `task stack:data:druid:enable` (requires the data slice) |
| ai-platform | kagent (+ CRDs), agentgateway (+ CRDs) | `task stack:ai-platform:enable` |

Each slice has a matching `:disable` target; the core stack stays up. `task stack:all:enable` enables every slice in a single command (excluding druid).

## Layout

```
cluster/   kind config + cluster lifecycle tasks
stack/
  core/    always-on addons
  <slice>/ opt-in addons grouped by use case
Taskfile.yaml
```

Each addon directory contains `install.sh` (an explicit `helm upgrade --install`) and `values.yaml` (deltas from chart defaults).

## Versions

Chart versions are pinned in each `install.sh`. To refresh, run `helm repo update && helm search repo <chart>` and update the pin.
