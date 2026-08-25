#!/usr/bin/env bash
# bedrock-credentials — give the local model gateways an AWS identity.
#
# On a real cluster the gateway reaches Bedrock as the tenant: the operator
# points each Platform's EnvoyProxy at the tenant ServiceAccount, which carries
# a Pod Identity association, and the BackendSecurityPolicy names only a region
# — the ambient credential chain. kind has no Pod Identity, so that chain finds
# nothing and every call returns AccessDeniedException.
#
# This resolves the local profile once, stores it where Kyverno can clone it,
# and installs the policy that clones it into each tenant namespace and points
# the gateway's envoy container at it. Credentials are never patched onto a
# running pod: the data plane is generated, so a patch is reconciled away.
#
# Optional slice. Without it the platform still installs and reconciles — only
# a real Bedrock call fails, which is the honest local default.
#
# FIDELITY GAP, deliberate and bounded. On a real cluster each tenant reaches
# Bedrock as itself: a per-tenant IAM role, scoped by the operator to that
# tenant's models. Here there is one credential — whatever ${AWS_PROFILE} holds
# — cloned into every tenants-* namespace, so every local tenant shares one
# identity with the operator's own permissions. Nothing scopes it down, and
# nothing here can: minting per-tenant roles is an AWS write this workspace has
# no business making. So local tenants are NOT isolated from each other on the
# AWS side, and a local Bedrock call is not a test of the production authz path.
# Acceptable because the blast radius is one kind cluster on one workstation and
# the credential is normally a short-lived SSO session. Do not point this at a
# profile whose permissions you would not hand to every namespace at once.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROFILE="${AWS_PROFILE:-default}"
# The region the gateway signs the upstream Bedrock call for. It has to be one
# the profile's account allows Bedrock in: an account carrying a region-lock
# policy answers AccessDeniedException for every other region, which is the same
# error an empty credential chain produces and is therefore indistinguishable
# from the failure this slice exists to remove.
#
# us-east-1 is the default because it is the only region the llm-policy standard
# lists as preferred, and that list is one element so a workload cannot drift
# toward a region a service-control policy denies. Override with AWS_REGION when
# the profile's account allows Bedrock somewhere else.
REGION="${AWS_REGION:-us-east-1}"
SECRET="kx-bedrock-credentials"
SOURCE_NS="kyverno"

# The message names a remedy, so it has to be sure of the cause. `kubectl get`
# exits non-zero for a missing CRD and for an unreachable cluster alike, and
# asserting the first while discarding the evidence sends an operator to install
# Kyverno when nothing is listening.
if ! kyverno_err="$(kubectl get crd clusterpolicies.kyverno.io 2>&1 >/dev/null)"; then
  if printf '%s' "${kyverno_err}" | grep -qiE 'not found|NotFound'; then
    echo "Kyverno is not installed — run 'task stack:security:enable' first." >&2
  else
    echo "Could not determine whether Kyverno is installed — kubectl said:" >&2
    printf '%s\n' "${kyverno_err}" | sort -u | sed 's/^/  /' >&2
    echo "Fix that first; enabling the security slice will not help until kubectl" >&2
    echo "can reach the cluster." >&2
  fi
  exit 1
fi

# Resolved through the CLI rather than read out of ~/.aws, so SSO sessions,
# assumed roles and credential_process all work the same way. A profile whose
# session has expired fails here, with the aws CLI's own message, rather than
# producing a Secret full of empty strings that fails much later as a 403.
echo "Resolving credentials from AWS profile '${PROFILE}' ..."
CREDS_JSON="$(aws configure export-credentials --profile "${PROFILE}" --format process)"
AWS_ACCESS_KEY_ID="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKeyId"])')"
AWS_SECRET_ACCESS_KEY="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["SecretAccessKey"])')"
AWS_SESSION_TOKEN="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("SessionToken",""))')"
AWS_REGION="${REGION}"
# Exported rather than passed as arguments: the Secret below is built from the
# environment so no credential reaches a command line.
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_REGION

kubectl create namespace "${SOURCE_NS}" --dry-run=client -o yaml | kubectl apply -f -

# Built on stdin rather than with --from-literal: that flag puts the secret key
# and the session token in this process's argv, where `ps` shows them to every
# other process on the workstation. python3 does the base64 and the quoting; it
# is already required above to read the credential JSON.
python3 - "${SECRET}" "${SOURCE_NS}" <<'PY' | kubectl apply -f -
import base64, json, os, sys

name, namespace = sys.argv[1], sys.argv[2]
data = {k: base64.b64encode(os.environ[k].encode()).decode() for k in (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION"
)}
print(json.dumps({
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": name, "namespace": namespace},
    "type": "Opaque",
    "data": data,
}))
PY

kubectl apply -f "${SCRIPT_DIR}/policy.yaml"

# Wait for Kyverno to accept the policy. An unready ClusterPolicy silently
# mutates nothing, which is the failure this slice exists to make impossible.
kubectl wait --for=condition=Ready clusterpolicy/inject-local-bedrock-credentials --timeout=60s

echo
echo "── local Bedrock credentials installed ──"
echo "  profile:  ${PROFILE}"
echo "  region:   ${REGION}"
echo "  expires:  $(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Expiration","no expiry (long-lived key)"))')"
echo
echo "Gateways admitted from now on pick the credentials up. Existing ones are"
echo "replaced by Envoy Gateway on its next reconcile; to take them now:"
echo "  kubectl delete pod -A -l app.kubernetes.io/managed-by=envoy-gateway,app.kubernetes.io/component=proxy"
echo
echo "Verify with:  task stack:ai-platform:credentials:verify"
