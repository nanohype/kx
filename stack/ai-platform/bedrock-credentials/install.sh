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
# us-east-1, and deliberately not the substrate's region. The estate pins every
# venture account to one home region with the `guardrail-region-lock` SCP —
# stxkxs/landing-zone, iac/components/aws/organization/main.tf, attached to the
# Ventures OU. us-east-1 is the anchor by design, not by accident: CloudFront's
# ACM certificates must be issued there regardless, so anchoring anywhere else
# would need a permanent carve-out for the one thing every venture does. The
# `home_region` variable carries the full reasoning — read it there rather than
# trusting this restatement.
#
# A gateway signing for any other region gets AccessDeniedException: the exact
# error this slice exists to remove, arriving from the guardrail instead of from
# the empty credential chain. eks-agent-platform's substrate is still us-west-2,
# so this is the one value kx does not mirror. That gap closes when the substrate
# moves to the home region, not by widening the SCP.
REGION="${AWS_REGION:-us-east-1}"
SECRET="kx-bedrock-credentials"
SOURCE_NS="kyverno"

if ! kubectl get crd clusterpolicies.kyverno.io >/dev/null 2>&1; then
  echo "Kyverno is not installed — run 'task stack:security:enable' first." >&2
  exit 1
fi

# Resolved through the CLI rather than read out of ~/.aws, so SSO sessions,
# assumed roles and credential_process all work the same way. A profile whose
# session has expired fails here, with the aws CLI's own message, rather than
# producing a Secret full of empty strings that fails much later as a 403.
echo "Resolving credentials from AWS profile '${PROFILE}' ..."
CREDS_JSON="$(aws configure export-credentials --profile "${PROFILE}" --format process)"
ACCESS_KEY="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKeyId"])')"
SECRET_KEY="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["SecretAccessKey"])')"
SESSION_TOKEN="$(printf '%s' "${CREDS_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("SessionToken",""))')"

kubectl create namespace "${SOURCE_NS}" --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic "${SECRET}" \
  --namespace "${SOURCE_NS}" \
  --from-literal=AWS_ACCESS_KEY_ID="${ACCESS_KEY}" \
  --from-literal=AWS_SECRET_ACCESS_KEY="${SECRET_KEY}" \
  --from-literal=AWS_SESSION_TOKEN="${SESSION_TOKEN}" \
  --from-literal=AWS_REGION="${REGION}" \
  --dry-run=client -o yaml | kubectl apply -f -

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
echo "Verify with:  bash ${SCRIPT_DIR}/verify.sh"
