# 0006. kx keeps its own kubeconfig

## Status

Accepted.

## Context

kx's installers apply manifests, install and uninstall helm releases, and
delete namespaces. The same person also works against other clusters,
production among them, and moves between them by exporting `KUBECONFIG` or
switching their current context. By default, `kind create cluster` writes its
context into that person's kubeconfig and makes it current. A script that acts
on "the current context" then acts on whichever cluster was current when it
ran.

## Decision

kx keeps its cluster's credentials in `.kube/config` at the repository root,
and nowhere else.

- kind writes that file when `task up` creates the cluster. For a cluster that
  already exists, `task up` exports the file again with `kind export
  kubeconfig`. The file holds the `kind-kx` context alone.
- Every script that reaches the cluster sources `cluster/kubeconfig.sh`. It
  exports `KUBECONFIG` as that file and stops unless its current context is
  `kind-kx`, before the first command that would act on a cluster.
- Every Taskfile command that reaches the cluster carries `KUBECONFIG` itself.
  A Taskfile's `env:` cannot do it, because Task never overrides a variable the
  shell already exports.
- `task down` deletes the file along with the cluster.
- `scripts/check-slice-integrity.py` holds every script and task to this, as
  the invariant that nothing kx runs can reach a cluster other than `kind-kx`.

kx never reads, writes or switches the context of the user's own kubeconfig.

## Alternatives

- **`--context kind-kx` on every command.** One missed flag acts on the
  current context, and nothing would stop kind from writing the user's
  kubeconfig in the first place.
- **Checking the current context before acting.** The user's context is free
  to change between the check and the command, and the check still leaves kx's
  credentials in a file that holds every other cluster's.

## Consequences

- No kx task or script can reach a cluster other than its own, whatever the
  shell's `KUBECONFIG` or current context names.
- To use kubectl against kx by hand, a person points `KUBECONFIG` at
  `.kube/config`, as the README's quickstart shows.
- `task up` adopts a cluster that an earlier kx created with its context in the
  user's kubeconfig, by exporting this file for it.
