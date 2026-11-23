#!/usr/bin/env bash
# bedrock-credentials — give the local model gateways an AWS identity.
#
# On a real cluster the gateway reaches Bedrock as the tenant: the operator
# points each Platform's EnvoyProxy at the gateway's own ServiceAccount
# (<platform>-gateway), which it binds to the tenant role with a Pod Identity
# association, and the BackendSecurityPolicy names only a region
# — the ambient credential chain. kind has no Pod Identity, so that chain finds
# nothing and every call returns AccessDeniedException.
#
# This resolves the local profile once, stores it where Kyverno can clone it,
# and installs two policies: a GeneratingPolicy that clones it into each tenant
# namespace, and a MutatingPolicy that points the gateway's signing sidecar (and
# its envoy container) at the clone. Credentials are never patched onto a
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
# the credential is a session that stops working by itself: credentials with no
# expiry are refused. The residual risk, Kyverno's cluster-wide Secret access
# while this slice is installed, is in docs/threat-model.md. Do not point this
# at a profile whose permissions you would not hand to every namespace at once.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
# shellcheck source=../../../cluster/deadline.sh
. "${SCRIPT_DIR}/../../../cluster/deadline.sh"

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
#
# Both kinds policy.yaml declares, asked for by name: a Kyverno old enough to
# serve neither is a Kyverno this slice cannot install into, and the remedy for
# that is the same one.
if ! kyverno_err="$(kubectl --request-timeout=60s get crd generatingpolicies.policies.kyverno.io mutatingpolicies.policies.kyverno.io 2>&1 >/dev/null)"; then
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
# session has expired fails here, with the aws CLI's own message, and a profile
# that resolves to no key fails in credentials.py, rather than either producing
# a Secret of empty strings that fails much later as a 403.
#
# The JSON stays in this shell and reaches python3 through printf, a builtin, so
# no credential is ever an argument to a command or a variable in the
# environment every later command inherits.
echo "Resolving credentials from AWS profile '${PROFILE}' ..."
CREDS_JSON="$(deadline 120 aws configure export-credentials --profile "${PROFILE}" --format process)"

# Credentials with no expiry are refused. This Secret is cloned into every
# tenants-* namespace, and only a session stops working there by itself: a
# long-lived access key reports no expiry, and neither does a session the aws
# CLI gave none for. Checked here, before anything is written to the cluster,
# and again by the manifest step. What the check prints is when the session
# stops working, which the Secret records.
EXPIRES="$(printf '%s' "${CREDS_JSON}" | python3 "${SCRIPT_DIR}/credentials.py" check)"

kubectl --request-timeout=60s create namespace "${SOURCE_NS}" --dry-run=client -o yaml | kubectl --request-timeout=60s apply -f -

# Built on stdin rather than with --from-literal: that flag puts the secret key
# and the session token in this process's argv, where `ps` shows them to every
# other process on the workstation. credentials.py does the base64 and the
# quoting, and records the expiry as an annotation for the verify task.
printf '%s' "${CREDS_JSON}" \
  | python3 "${SCRIPT_DIR}/credentials.py" manifest "${SECRET}" "${SOURCE_NS}" "${REGION}" \
  | kubectl --request-timeout=60s apply -f -

# ── Retire the ClusterPolicy this slice used to install ────────────────────
#
# Kyverno 1.20 no longer executes kyverno.io/v1 policies, and its chart refuses
# to upgrade while one exists, so a cluster that ran an earlier version of this
# slice has to lose it before the security slice can move. It is retired here,
# by the slice that installed it, before its replacements go in.
#
# Its copies go with it, explicitly. A synchronizing generate rule deletes what
# it made when its policy is deleted, but in the background and with nothing
# here to wait on — and the GeneratingPolicy below will not overwrite a Secret it
# does not own. Left to race, the old cleanup can land after the new backfill
# skipped a namespace, and that tenant ends with no credentials at all. Deleted
# by the label the old rule stamped, so a copy is gone before anything is asked
# to make a new one, and nothing else in the namespace is touched: the rule-name
# label is one only the ClusterPolicy wrote, since a GeneratingPolicy has no
# rules to name.
#
# The kind is looked up rather than assumed: a Kyverno that no longer serves
# clusterpolicies.kyverno.io has no legacy policy to retire, and `kubectl delete`
# of a kind the server does not know is an error rather than a no-op. The CRD is
# asked for by name, and only a NotFound answer counts as absent, for the reason
# the Kyverno check above gives.
LEGACY_POLICY="inject-local-bedrock-credentials"
if legacy_err="$(kubectl --request-timeout=60s get crd clusterpolicies.kyverno.io 2>&1 >/dev/null)"; then
  kubectl --request-timeout=60s --timeout=5m delete clusterpolicies.kyverno.io "${LEGACY_POLICY}" --ignore-not-found
elif ! printf '%s' "${legacy_err}" | grep -qiE 'not found|NotFound'; then
  echo "Could not determine whether the old ClusterPolicy kind is served — kubectl said:" >&2
  printf '%s\n' "${legacy_err}" | sort -u | sed 's/^/  /' >&2
  exit 1
fi
kubectl --request-timeout=60s --timeout=5m delete secrets --all-namespaces \
  -l "generate.kyverno.io/policy-name=${LEGACY_POLICY},generate.kyverno.io/rule-name=clone-bedrock-credentials" \
  --field-selector "metadata.name=${SECRET}" --ignore-not-found

# The grant first, so the background controller can read the source and write
# the copies by the time the GeneratingPolicy asks it to backfill.
kubectl --request-timeout=60s apply -f "${SCRIPT_DIR}/rbac.yaml"
kubectl --request-timeout=60s apply -f "${SCRIPT_DIR}/policy.yaml"

# Wait for Kyverno to accept both policies. An unready policy silently mutates
# or generates nothing, which is the failure this slice exists to make
# impossible. A CEL policy reports readiness as a boolean rather than as a
# condition named Ready, so the wait reads that field.
kubectl --request-timeout=60s wait --for=jsonpath='{.status.conditionStatus.ready}'=true \
  generatingpolicies.policies.kyverno.io/clone-local-bedrock-credentials \
  mutatingpolicies.policies.kyverno.io/inject-local-bedrock-credentials \
  --timeout=60s

echo
echo "── local Bedrock credentials installed ──"
echo "  profile:  ${PROFILE}"
echo "  region:   ${REGION}"
echo "  expires:  ${EXPIRES}"
echo
echo "Gateways admitted from now on pick the credentials up. Existing ones are"
echo "replaced by Envoy Gateway on its next reconcile; to take them now:"
echo "  kubectl delete pod -A -l app.kubernetes.io/managed-by=envoy-gateway,app.kubernetes.io/component=proxy"
echo
echo "Verify with:  task stack:ai-platform:credentials:verify"
