# 0004. Gateways on kind are reached in-cluster

## Status

Accepted.

## Context

Cilium's Gateway API controller gives each Gateway a Service of type
LoadBalancer, `cilium-gateway-<name>`. Nothing on kind assigns such a Service
an address. A Gateway there therefore reports `Accepted=True` and
`Programmed=False`, with reason `AddressNotAssigned`, while its routes answer
on the Service from inside the cluster. kind fixes a cluster's host port
mappings when it creates the cluster, so a host path that needs a new mapping
for every Gateway needs a new cluster too.

## Decision

Gateways stay in-cluster on kind. A Gateway that is Accepted but not
Programmed is the expected state, and its routes are exercised from inside the
cluster, through `cilium-gateway-<name>`. The path from the host is an Ingress,
on `localhost:80` and `localhost:443` ([0003](0003-cilium-serves-ingress.md)).

## Alternatives

- **Host-network Gateways on unprivileged ports.** Each Gateway listener
  would need a host port and a kind mapping of its own. kind cannot add a
  mapping to a running cluster, so every new listener would mean recreating
  the cluster.
- **LoadBalancer addresses from Cilium's LB-IPAM, announced over L2.** The
  Gateway would be Programmed, but its address sits on the kind network. A
  Linux host routes to it, but Docker Desktop runs containers inside a VM, so a
  Mac does not.

## Consequences

- A Gateway's status differs from production's, and only in the Programmed
  condition. Its routes are still served, and testable in-cluster from a test
  pod or a job.
- A project that needs its routes from the host puts an Ingress in front of
  them locally.
