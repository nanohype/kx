# 0009. A chart too large to be a release is applied server-side

## Status

Accepted.

## Context

Helm stores each release in a Secret, which the API server caps at 1 MiB, and a
release holds the whole chart. Envoy Gateway's CRD chart,
`gateway-crds-helm`, carries both Gateway API channels as templates beside
Envoy Gateway's own CRDs, whatever its values turn on, and the stored release
does not fit. The schema of the largest of its CRDs is also past the 256 KiB
`last-applied-configuration` annotation that client-side `kubectl apply`
writes. The Gateway API CRDs themselves belong to
`stack/core/gateway-api-crds`, so only the Envoy Gateway half is wanted here.

## Decision

`stack/ai-platform/envoy-gateway-crds/install.sh` renders the chart with
`helm template`, with the Gateway API half off, to a file first, so that a
render failing partway applies nothing. It applies that file with
`kubectl apply --server-side --force-conflicts` under a field manager named
after the addon, and waits for every CRD to be Established before the
controller installs. Upstream's own install instructions for the chart do the
same.

The slice's disable runs `stack/ai-platform/envoy-gateway-crds/uninstall.sh`
in place of `helm uninstall`. It deletes the `gateway.envoyproxy.io` CRDs by
group, and only once the controller that watches them is gone.

## Alternatives

- **A helm release.** It does not fit in the Secret helm stores it in.
- **Client-side apply.** The annotation it writes would exceed its own limit.
- **Letting the controller's chart install its CRDs.** It installs them only
  bundled with a Gateway API release, which the core stack already owns.

## Consequences

- There is no helm release to list, upgrade or roll back. `task status` counts
  the group's CRDs instead, and
  `kubectl get crd -o yaml --show-managed-fields` shows the addon as their
  owner.
- `--force-conflicts` moves fields that a different manager holds to this one
  at the next version bump, rather than stopping on the first field that
  changed.
- A chart that cannot be a release follows the same shape: render, apply
  server-side, and ship an `uninstall.sh` for the slice's disable to run.
