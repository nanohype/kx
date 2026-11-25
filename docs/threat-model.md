# Threat model

kx runs on one workstation, for the one person who uses it. This document names
what it holds on that person's behalf, what bounds each thing it holds, and what
it accepts as residual risk.

## The bedrock-credentials slice

**What it holds.** The session an AWS profile resolves to — access key, secret
key and session token — as the Secret `kyverno/kx-bedrock-credentials`, cloned
into every `tenants-*` namespace and referenced by each model gateway's
`ai-gateway-extproc` sidecar, the container that signs the Bedrock call.

**What bounds it.**

- Only a session is accepted. `stack/ai-platform/bedrock-credentials/credentials.py`
  refuses credentials with no session token, no expiry, or an expiry already
  past, before the installer writes anything, so every copy stops working when
  the session does.
- The keys never reach a process's argv or environment on the workstation: the
  installer hands them to `credentials.py` on stdin, and it writes the Secret
  manifest to kubectl on stdin.
- Only the signing sidecar carries the reference. Envoy and the other
  containers in the gateway pod never see the credential.
- `task stack:ai-platform:credentials:verify` reports when the session expires,
  and fails once it has.
- `task stack:ai-platform:credentials:disable` deletes the policies, the copies,
  the source and the grant below, in that order.

**Residual risk: Kyverno's cluster-wide Secret access.** While the slice is
installed, Kyverno's background controller may get, list, watch, create, update
and delete Secrets in every namespace
(`stack/ai-platform/bedrock-credentials/rbac.yaml`). It needs the read to clone
the source and the writes to keep each copy, and RBAC cannot scope a grant to
namespaces chosen by a name prefix. Anything that can direct that controller —
a policy someone installs, or a compromise of Kyverno itself — can therefore
read every Secret in the cluster, not only this one. Accepted because the
cluster is a local kind cluster with one user, the credential expires by itself,
and disabling the slice withdraws the grant.

**Fidelity gap.** Every local tenant shares the one identity, with the profile's
own permissions. Unlike production, local tenants are not isolated from each
other on the AWS side, and a local Bedrock call is not a test of the production
authz path. `stack/ai-platform/bedrock-credentials/install.sh` states this
beside the code it describes.
