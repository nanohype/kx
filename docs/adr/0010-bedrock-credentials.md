# 0010. Local Bedrock calls sign with an expiring session, from the signing sidecar

## Status

Accepted.

## Context

In production a tenant's model gateway reaches Bedrock as the tenant. The
operator binds the gateway's ServiceAccount to the tenant's role with a Pod
Identity association, and the BackendSecurityPolicy names only a region. kind
has no Pod Identity, so the credential chain finds nothing and every call
returns `AccessDeniedException`.

Envoy AI Gateway resolves credentials and signs with SigV4 in its external
processor, the `ai-gateway-extproc` sidecar, rather than in Envoy. Envoy
Gateway generates each gateway's data plane from its EnvoyProxy, so a patch to
a running pod is reconciled away.

## Decision

An opt-in slice, `stack/ai-platform/bedrock-credentials`, gives the local
gateways one AWS identity: the profile the workstation resolves.

- **Only an expiring session is accepted.** `credentials.py` refuses
  credentials with no session token, with no expiry, or with an expiry already
  past, before anything is written, so every copy stops working when the
  session does.
- **The keys stay off argv.** They reach `credentials.py` on stdin, and the
  Secret manifest reaches kubectl on stdin, so no process's argv on the
  workstation carries them.
- **Kyverno distributes the credential.** The Secret is stored in the
  `kyverno` namespace. A GeneratingPolicy clones it into every `tenants-*`
  namespace and keeps the copies equal to it, and a MutatingPolicy adds a
  reference to it to the `ai-gateway-extproc` sidecar alone, at admission.
- **Expiry and removal are tasks.** `task stack:ai-platform:credentials:verify`
  reports when the session expires and fails once it has.
  `task stack:ai-platform:credentials:disable` removes the policies, the
  copies, the source and Kyverno's grant.

## Alternatives

- **Long-lived access keys, allowed by an override.** A copy in every tenant
  namespace that never stops working is the exposure the expiry exists to
  bound.
- **Adding the reference to Envoy as well.** Envoy signs nothing, so it would
  widen what can read the credential for no gain. This is revisited if a
  release moves signing back into the proxy.
- **Per-tenant roles.** Minting them is an AWS write that a local workspace has
  no business making.

## Consequences

- Every local tenant shares one identity, with the profile's own permissions.
  Local tenants are not isolated from each other on the AWS side, and a local
  Bedrock call does not test the production authorization path.
- While the slice is installed, Kyverno's background controller holds
  cluster-wide access to Secrets, because RBAC cannot scope a grant to
  namespaces chosen by a name prefix. [The threat model](../threat-model.md)
  records that residual risk.
- When the session expires, the gateways stop signing until the credentials
  task runs again after a fresh login.
