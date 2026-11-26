# 0016. CoreDNS forwards external names to public resolvers

## Status

Accepted.

## Context

CoreDNS forwards names outside the cluster to the resolver in the node's
`/etc/resolv.conf`. A kind node inherits Docker Desktop's resolver, which
answers only from the host's network namespace and not from the pod network
Cilium manages. A pod resolving a public name, such as a registry or a release
host, times out.

## Decision

`cluster/coredns-patch.sh` runs as part of `task up`. It replaces the CoreDNS
Corefile so that names outside the cluster are forwarded to 1.1.1.1 and
8.8.8.8, while names in `cluster.local` still resolve in-cluster. CoreDNS's
`reload` plugin picks the change up. The script restarts CoreDNS itself only
when a CoreDNS pod is already Ready, since on a fresh cluster no pod can be
Ready before Cilium is installed.

## Alternatives

- **The inherited resolver.** It is unreachable from pods, which is the
  problem.
- **CoreDNS on the host network.** It would reach the inherited resolver, at
  the price of a `kube-system` that differs in shape from every real cluster's.

## Consequences

- Pods resolve public names wherever the workstation's network lets DNS reach
  those resolvers. A network that blocks outbound DNS to them leaves pods
  without public names.
- Pods do not resolve names that only the workstation's resolver knows, such
  as a VPN's internal zones.
- DNS queries for external names leave the workstation for Cloudflare and
  Google rather than the network's own resolver.
  [The threat model](../threat-model.md) counts that as disclosure.
