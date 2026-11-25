# 0015. The datapath carries a memory limit and no CPU limit

## Status

Accepted.

## Context

The Cilium chart sets `resources: {}` for every component. Left unset, the
agent and its Envoy proxy run unbounded, and on kind they compete with every
workload on the node, which is the usual reason pods sit Pending after a heavy
slice comes up. A CPU limit has a cost of its own on the datapath: CFS
throttling on the agent or on Envoy adds packet-processing latency to every pod
on the node.

## Decision

`stack/core/cilium/values.yaml` splits Cilium's components by class:

- **The datapath gets a memory limit only.** `cilium-agent` and `cilium-envoy`
  carry a memory limit and a CPU request, and deliberately no CPU limit, because
  throttling them degrades every pod's traffic.
- **Everything else gets a full pair.** The operator, Hubble relay and UI, and
  the init containers carry request-and-limit pairs, because throttling them
  delays observability or start-up rather than packets.
- **The numbers are sized for kind.** They fit one kind cluster on a
  workstation, where every millicore requested is one a project cannot
  schedule. The shape follows eks-gitops, and the numbers do not.

## Alternatives

- **Full request-and-limit pairs everywhere.** It would bound the agent's CPU
  at the price of throttled packets on every pod.
- **The chart's unbounded defaults.** The datapath would compete freely with
  the workloads it serves.

## Consequences

- A runaway agent is stopped by its memory limit, and a busy one is never
  throttled.
- A policy that requires CPU limits cluster-wide has to exclude `kube-system`,
  where Cilium runs, because capping the datapath is the degradation this split
  avoids.
