# 0005. Every published port binds to loopback

## Status

Accepted.

## Context

kx publishes a few ports on the workstation: the Ingress listener on 80 and
443, the ArgoCD UI on 30080, Grafana on 30030 and the local registry on 5001.
What sits behind them is configured for one person on one machine. Grafana's
admin password is `admin`, ArgoCD's UI runs without TLS, and the registry has
no authentication. kind publishes an `extraPortMappings` entry on every
interface unless the entry names a `listenAddress`. On a shared network that
puts each of those services in front of anyone on it.

## Decision

Every host port kx publishes binds to 127.0.0.1:

- each `extraPortMappings` entry in `cluster/kind-config.yaml` names
  `listenAddress: "127.0.0.1"`;
- `cluster/registry.sh` publishes the registry on `127.0.0.1:5001`.

`scripts/check-slice-integrity.py` fails a kind mapping, or a port a script
or Taskfile publishes with `docker run` or `docker create`, that does not bind
loopback.

A service with a default credential and no host port, such as the data slice's
object store or Druid, is reached in-cluster or through `kubectl port-forward`,
which binds loopback by default.

## Alternatives

- **Real credentials and TLS for every UI.** Each one is a secret to create,
  store and rotate for a workspace with one user. Binding loopback removes the
  exposure those defaults would otherwise carry.
- **Binding every interface behind a host firewall.** Its safety would then
  depend on configuration outside kx, which kx can neither see nor check.

## Consequences

- Nothing kx publishes is reachable from another machine.
- Loopback does not separate kx from the workstation's own processes, a
  browser included. The defaults are accepted on that basis, and
  [the threat model](../threat-model.md) states what remains.
- A new port mapping that leaves out `listenAddress`, or a container published
  without `127.0.0.1`, fails CI.
