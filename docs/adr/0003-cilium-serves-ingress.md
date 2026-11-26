# 0003. Cilium serves Ingress from one host-network listener

## Status

Accepted.

## Context

In production, the AWS load balancer controller turns an Ingress into a load
balancer. kind has no cloud load balancer, so a local controller has to serve
Ingress, and the host has to reach it at `localhost:80` and `localhost:443`.
Cilium is already the cluster's CNI and already runs Envoy on every node for
its Gateway API controller. kind fixes a cluster's port mappings when it
creates the cluster, so whatever the host reaches has to sit on a known node at
a known port.

## Decision

Cilium's Ingress controller serves every Ingress, configured in
`stack/core/cilium/values.yaml`:

- `ingressController.default` makes `cilium` the default IngressClass, so an
  Ingress that names no class is served.
- `loadbalancerMode: shared` serves every Ingress from one Envoy listener.
- That listener binds on the host network of the node labelled `ingress-ready`,
  at the port `sharedListenerPort` names. It is unprivileged, so Envoy needs no
  capability to bind it.
- `cluster/kind-config.yaml` labels the control-plane node `ingress-ready` and
  maps both `localhost:80` and `localhost:443` to that one port, on loopback.
  The listener serves plain HTTP and TLS alike, and tells them apart by the TLS
  handshake.

`scripts/check-slice-integrity.py` holds the kind mapping and the listener's
port and node selector to each other. `stack/core/cilium/install.sh` uninstalls
an ingress-nginx release left on an existing cluster, and stops on any answer
from helm other than NotFound.

## Alternatives

- **ingress-nginx.** A second proxy beside the Envoy that Cilium already runs,
  and a project its maintainers have retired.
- **A NodePort Service in front of the shared listener.** It works too, with
  two node ports to pin, one for HTTP and one for TLS, and a Service hop on the
  way in. Host-network mode needs one port, on the node the mapping already
  targets.
- **LoadBalancer addresses from Cilium's LB-IPAM, announced over L2.** The
  addresses sit on the kind network. A Linux host routes to it, but Docker
  Desktop runs containers inside a VM, so a Mac does not.

## Consequences

- An Ingress is reachable from the host on `localhost:80` and `localhost:443`
  as soon as the core stack is up. An Ingress naming the `nginx` class is not
  served.
- An Ingress carries no address in its status, because nothing on kind
  allocates a load-balancer IP. A tool that waits for an address waits forever.
- TLS passthrough is per Ingress, set with the
  `ingress.cilium.io/tls-passthrough` annotation.
- A cluster created while ingress-nginx held the host ports keeps the old
  mapping until `task reset` recreates it, and the README's troubleshooting
  table says so.
