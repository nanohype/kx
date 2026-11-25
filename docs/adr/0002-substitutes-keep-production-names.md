# 0002. A local substitute keeps the production name

## Status

Accepted.

## Context

Some production addons exist only on AWS: the load balancer controller, the
Secrets Manager provider for external-secrets, Velero's S3 backend, Karpenter.
kind cannot run them, so kx either omits the role or fills it with something
local. A project built against kx should meet the stack under the names it will
meet in production, so that what it learns transfers.

## Decision

Where a role has a local equivalent, the equivalent lives in the directory that
role would occupy, under its own name. The installer says what it stands in for
and why, beside the code that differs. The full mapping is the table in
`CLAUDE.md`.

- The role of the AWS load balancer controller is filled by Cilium's Ingress and
  Gateway API controllers, in `stack/core/cilium`.
- external-secrets is the same chart with the kubernetes provider, in
  `stack/secrets/external-secrets`.
- Velero is the same chart with an S3-compatible store in the cluster, in
  `stack/data/velero`.
- Where no local equivalent exists (Karpenter, the GPU device plugins), the
  role is absent and the mapping table says so.

There is no `substitutes` directory, and the swap is not presented as a feature.

## Alternatives

- **A directory of substitutes.** It makes the difference prominent, and it
  teaches the wrong layout: a project would learn paths that exist nowhere in
  production.
- **Omitting every AWS-only role.** Simpler, but a project could then not
  exercise Ingress, external secrets or backups locally at all.

## Consequences

- A reader of an installer learns the production name and the local difference
  in one place.
- The difference is invisible from the directory tree, so it has to be stated
  where it is made. `CLAUDE.md` holds the mapping, and each installer states its
  own swap.
