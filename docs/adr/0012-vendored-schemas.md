# 0012. The schema gate reads schemas vendored from the pinned releases

## Status

Accepted.

## Context

The render gate validates every resource the slices render, including custom
resources. Schemas fetched at run time make a blocking verdict depend on a
third party's availability, and on a catalog that need not match the versions
kx pins. Cilium's CRDs are a sharp case: its chart does not ship them, because
its operator registers them at run time, so a render holds nothing to validate
a CiliumNetworkPolicy against.

## Decision

`schemas/` holds what the gate validates against, at the versions kx pins:
Kubernetes' own OpenAPI document for the built-in kinds, at the node image's
Kubernetes version, and Cilium's CRDs, at the Cilium chart's version.

- `scripts/vendor-schemas.py` takes both verbatim from the Go module of the
  pinned release, as proxy.golang.org serves it. It keeps them only after the
  module's hash matches the one sum.golang.org records for that release.
- `schemas/sources.json` records the module, that hash and each file's sha256.
  `scripts/check-rendered-schemas.py` refuses a vendored file that no longer
  matches its record.
- `scripts/vendor-schemas.py` runs on a workstation when a pin moves. The
  render gate names that command when the node image or the Cilium chart has
  moved past what is vendored, and `--check` fetches every recorded release
  again and compares.

## Alternatives

- **kubeconform's default schema location and a community CRD catalog.** Both
  are fetched over the network on the blocking path, and the catalog is not
  matched to the Cilium version kx pins.
- **Skipping kinds with no schema.** Custom resources would pass unvalidated,
  and nothing would say so.

## Consequences

- CI reads no schema over the network, and a schema verdict is a fact about
  the commit.
- Moving the node image or the Cilium chart carries a schema refresh in the
  same change.
- The transparency log's signature is not verified. The recorded hash is read
  over TLS from sum.golang.org itself.
