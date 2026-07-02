# kx

Local Kubernetes (kind) that mirrors the chart catalog from [`eks-gitops`](https://github.com/nanohype/eks-gitops). Daily-driver for any build that targets the same stack in production.

## Operating model

- **Workspace, not curriculum.** No `labs/`, no walkthroughs, no "break it on purpose" exercises. The user lives in this cluster across many projects; learning happens through use.
- **Always-complete.** `task up` brings the cluster and the core stack online. No "now install cert-manager yourself."
- **Core + opt-in slices.** Core (always on): gateway-api CRDs, cilium (with Gateway API enabled), prometheus-operator-crds, cert-manager, trust-manager, metrics-server, ingress-nginx, reloader, argo-cd (idle). Opt-in slices in `stack/<slice>/`: observability, security, autoscaling, argo-platform, secrets, data, ai-platform.
- **Convention-based project attachment.** kx has no project registry. Projects live wherever they live and target the `kind-kx` context with their own namespace.
- **ArgoCD installed but idle.** It's part of the stack to learn against, but it does not own the addon lifecycle. No App-of-Apps Application is created by `task up`.

## Substitutes are invisible

When an eks-gitops addon is AWS-specific, the local equivalent goes in the most natural directory under its own name (ingress-nginx is `stack/core/ingress-nginx`, not `stack/substitutes/...`). The swap is documented inside the addon's `install.sh`, not on the README. The user-facing stack mirrors production names.

Local mappings (kept here for future Claude, not surfaced to user):

| eks-gitops | kx | Note |
|---|---|---|
| aws-load-balancer-controller | ingress-nginx (+ Cilium Gateway API) | Different product, same role |
| external-secrets (AWS SM) | external-secrets (kubernetes provider) | Same chart, swap provider |
| external-dns (Route53) | (not installed by default; coredns RFC2136 if needed) | Add to data/ slice if a project requires it |
| velero (S3) | velero + minio | Same Velero, S3-compatible backend |
| karpenter / karpenter-resources | (not applicable on kind) | No EC2 to provision |
| grafana-agent (remote-write → Amazon Managed Prometheus) + kube-state-metrics | kube-prometheus-stack | In-cluster Prometheus/Alertmanager/Grafana replace AMP + Amazon Managed Grafana; kube-state-metrics and node-exporter ship bundled in the stack instead of as standalone charts |
| grafana-operator (reconciles into external Amazon Managed Grafana) | grafana-operator | Same chart; dashboards/datasources reconcile into the kube-prometheus-stack Grafana instead of an external instance |

eks-agent-platform mirror (these are direct mirrors, not substitutes):

| eks-agent-platform | kx | Note |
|---|---|---|
| kagent (ns kagent) | stack/ai-platform/kagent | Same OCI chart, kagent-tools + kmcp sub-charts disabled by default locally |
| agentgateway (ns agentgateway) | stack/ai-platform/agentgateway | Same OCI chart, single replica, no Bedrock PrivateLink config |
| operator (ns eks-agent-platform) | stack/ai-platform/operator | Built from the sibling eks-agent-platform checkout + kind-loaded (image isn't published); `--disable-aws`, self-signed webhook issuer, cilium netpol. Override the repo path with `KX_EKS_AGENT_PLATFORM_DIR` |
| nvidia-gpu-operator / nvidia-dra-driver / aws-neuron-device-plugin | (not applicable) | No GPUs on kind |

eks-gitops catalog mirror — `stack/data/druid/` runs the production chart (`eks-gitops/catalog/druid/chart/`) **unmodified** via a post-renderer that strips EKS-only resources:

- **Chart drift policy:** when the upstream chart changes (eks-gitops), re-render and verify the post-renderer still cleanly removes what it expects. The post-renderer skips kinds `NodePool`, `EC2NodeClass`, `ExternalSecret` and strips nodeSelector labels in the set `karpenter.k8s.aws/instance-family`, `karpenter.sh/capacity-type`, `eks.amazonaws.com/nodegroup`. If the upstream chart adds new EKS-only resources, extend the skip list.
- **Secret shape:** the chart's helper expects a Secret named `<hostedId>-<release>-druid-metadata` with keys `username/password/host/dbname`. `install.sh` reads CNPG's generated `druid-metadata-app` Secret and rewrites it into this shape — do not change naming on either side without updating both.
- **S3 endpoint:** `values-local.yaml`'s `runtime:` adds `druid.s3.endpoint.url=http://minio.minio.svc.cluster.local:9000` + path-style + http. If MinIO moves or the service name changes, update that block.

## File conventions

Every addon directory has exactly two files:
- `install.sh` — explicit `helm repo add` + `helm upgrade --install` with version pinned. Idempotent. Read top-to-bottom by the user before running.
- `values.yaml` — local-only deltas from chart defaults. **Do not copy values from eks-gitops** — those assume IRSA, ENI, NLB, etc.

The exception is `prometheus-operator-crds` which has no values.

## Taskfile model

- Top-level: `task up | down | reset | status | stack:* | port-forward:*`
- Per-slice: `stack/<slice>/Taskfile.yaml` provides `enable` and `disable` targets that loop over the slice's addons
- Slice enable runs `install.sh` per addon in order; disable runs `helm uninstall` per addon

## What NOT to do

- Don't add helmfile, helm-secrets, helm wrappers, or "easier" automation — explicit install scripts are the point
- Don't copy eks-gitops values wholesale into a `values-local.yaml` — extract only what makes sense locally, into `values.yaml`
- Don't add ArgoCD App-of-Apps or ApplicationSets that point at the local stack — argo-cd is idle by design
- Don't add a `labs/`, `tutorial/`, `lessons/` or similar curriculum directory — see [[feedback-workspace-not-curriculum]]
- Don't add a `substitutes/` directory or surface the cloud/local swap as a feature — see [[feedback-no-substitutes-surface]]
- Don't add CI workflows — this is a personal cluster, not a product
- Don't pin chart versions from memory — `helm search repo <chart>` and pin to current at scaffold time
- Don't speculatively add charts. The conscious-omissions list is in `/Users/bs/.claude/plans/greedy-herding-umbrella.md` — these are NOT missing, they were intentionally excluded for a web/AI/infra/devops focus (not ML). Items to skip until a specific project needs them: Vault, Crossplane, Flux, Tekton, Linkerd, Istio, Harbor, Longhorn, Rook-Ceph, Kubeflow, KServe, Seldon, Triton, vLLM, Kueue, JupyterHub, LiteLLM, Langfuse, Ollama, Qdrant, Weaviate, Milvus.

## Related repos

- [`landing-zone`](https://github.com/nanohype/landing-zone) — Tofu/Terragrunt cloud infra (the cloud-side of "task up")
- [`eks-gitops`](https://github.com/nanohype/eks-gitops) — production ArgoCD/Helm config kx mirrors
