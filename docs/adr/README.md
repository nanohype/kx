# Architecture decision records

Each record states one decision that shapes kx: the problem it answers, what was
decided, what was weighed against it, and what follows from it. The code still
carries its reasons beside the lines they explain. A record is where a decision
spanning several files is stated once, so that it can be read, questioned and
replaced as one thing.

| Record | Decision |
|---|---|
| [0001](0001-explicit-install-scripts.md) | Every addon is an explicit install script with a pinned chart |
| [0002](0002-substitutes-keep-production-names.md) | A local substitute keeps the production name |
| [0003](0003-cilium-serves-ingress.md) | Cilium serves Ingress from one host-network listener |
| [0004](0004-gateways-stay-in-cluster.md) | Gateways on kind are reached in-cluster |
| [0005](0005-host-ports-bind-to-loopback.md) | Every published port binds to loopback |
| [0006](0006-kx-keeps-its-own-kubeconfig.md) | kx keeps its own kubeconfig |
| [0007](0007-every-outbound-call-is-bounded.md) | Every call that leaves the machine is bounded |
| [0008](0008-druid-through-a-post-renderer.md) | Druid runs the production chart through a post-renderer |
| [0009](0009-oversized-charts-apply-server-side.md) | A chart too large to be a release is applied server-side |
| [0010](0010-bedrock-credentials.md) | Local Bedrock calls sign with an expiring session, from the signing sidecar |
| [0011](0011-blocking-and-scheduled-checks.md) | CI blocks on facts about the commit and schedules the rest |
| [0012](0012-vendored-schemas.md) | The schema gate reads schemas vendored from the pinned releases |
| [0013](0013-image-cve-exceptions.md) | A CRITICAL in a pinned image fails unless a time-boxed exception covers it |
| [0014](0014-gates-prove-they-reject.md) | Every gate is observed to reject |
| [0015](0015-datapath-memory-limits-only.md) | The datapath carries a memory limit and no CPU limit |
| [0016](0016-coredns-public-resolvers.md) | CoreDNS forwards external names to public resolvers |

## Writing one

A record is `NNNN-short-title.md`, numbered after the last, with these sections:

- **Status** — Accepted, or Superseded by the record that replaces it. A
  superseded record stays, so the reasoning behind what came before stays
  readable.
- **Context** — the problem, and the constraints any answer has to meet.
- **Decision** — what kx does, stated as the rule it follows.
- **Alternatives** — what else answers the problem, and why it does not here.
- **Consequences** — what follows, the costs included.

A decision that changes is a new record that supersedes the old one, rather
than an edit to it.
