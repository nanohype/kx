#!/usr/bin/env bash
# Assert the credentials actually reached the container that signs.
#
# The failure this exists for: a mutation that matches nothing. Kyverno reports
# a healthy policy either way — a MutatingPolicy that mutates zero pods is not an
# error to Kyverno, it is a policy with nothing to do. The symptom would surface
# much later as AccessDeniedException from Bedrock, which reads as an IAM
# problem rather than a policy that never fired.
#
# Envoy AI Gateway resolves the AWS credential chain and SigV4-signs inside its
# external processor, so `ai-gateway-extproc` is the container the reference has
# to reach. It is declared as a native sidecar, which puts it under
# initContainers. The envoy container signs nothing, so it is asserted to carry
# no reference: one there was admitted under an earlier policy, and puts the
# credential in a container that never uses it.
#
# It reads the pod spec rather than the process environment because the Envoy
# image is distroless — there is no shell and no `env` binary to exec. The spec
# is sufficient: with the reference present and the Secret in the namespace,
# injection is the kubelet's guarantee, and the GeneratingPolicy is what puts
# the Secret there.
#
# It also reports when the credentials stop working, from the expiry install.sh
# recorded on the source Secret, and fails once that has passed. A gateway whose
# reference reaches an expired session fails every model call with the same
# AccessDeniedException as one whose reference reaches nothing.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../../cluster/kubeconfig.sh
. "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"

SELECTOR="app.kubernetes.io/managed-by=envoy-gateway,app.kubernetes.io/component=proxy"
SECRET="kx-bedrock-credentials"
SOURCE_NS="kyverno"
SIGNER="ai-gateway-extproc"

# The copies follow the source, so the source's expiry is every copy's.
expired=0
if ! recorded="$(kubectl --request-timeout=60s get secret "${SECRET}" -n "${SOURCE_NS}" \
    -o jsonpath='{.metadata.annotations.kx-credentials-expiration}' 2>&1)"; then
  echo "FAIL  could not read ${SOURCE_NS}/${SECRET} — kubectl said:" >&2
  printf '%s\n' "${recorded}" | sed 's/^/      /' >&2
  exit 1
fi
if [ -z "${recorded}" ]; then
  echo "FAIL  ${SOURCE_NS}/${SECRET} records no expiry, so whether it still works is unknown." >&2
  echo "      Re-run task stack:ai-platform:credentials, which records one." >&2
  expired=1
elif when="$(python3 "${SCRIPT_DIR}/credentials.py" remaining "${recorded}")"; then
  echo "ok    credentials expire: ${when}"
else
  echo "FAIL  credentials expire: ${when}" >&2
  expired=1
fi

# read -r rather than mapfile: mapfile is a bash 4 builtin and the documented
# prerequisites install no bash, so on macOS this runs under /bin/bash 3.2.
PODS=()
while IFS= read -r line; do
  [ -n "${line}" ] && PODS+=("${line}")
done < <(
  kubectl --request-timeout=60s get pods -A -l "${SELECTOR}" \
    -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}'
)

if [ "${#PODS[@]}" -eq 0 ]; then
  echo "FAIL: no gateway data-plane pods found (selector: ${SELECTOR})." >&2
  echo "      Either no ModelGateway has reconciled yet, or the labels Envoy" >&2
  echo "      Gateway stamps have changed and the policy selector is stale." >&2
  exit 1
fi

# Every reference to SECRET on one container, as lines. An empty result and a
# container that does not exist are the same answer here, which is why the
# caller checks the container is present before trusting a miss.
refs_on() { # pod ns jsonpath-container-list container-name
  kubectl --request-timeout=60s get pod "$1" -n "$2" \
    -o jsonpath="{range .spec.$3[?(@.name=='$4')].envFrom[*]}{.secretRef.name}{\"\n\"}{end}"
}

names_in() { # pod ns jsonpath-container-list
  kubectl --request-timeout=60s get pod "$1" -n "$2" -o jsonpath="{range .spec.$3[*]}{.name}{\"\n\"}{end}"
}

failed=0
for entry in "${PODS[@]}"; do
  ns="${entry%%/*}"
  pod="${entry##*/}"

  # The signer first. A rename upstream makes the policy's lookup by container
  # name find nothing and add nothing, silently — so a missing container is reported
  # as its own failure rather than folded into a missing reference.
  if ! names_in "${pod}" "${ns}" initContainers | grep -qx "${SIGNER}"; then
    echo "FAIL  ${ns}/${pod} — no ${SIGNER} sidecar under initContainers" >&2
    echo "      The signer container was renamed upstream; policy.yaml looks it up" >&2
    echo "      as ${SIGNER}, finds nothing, and adds nothing." >&2
    failed=1
    continue
  fi
  if ! refs_on "${pod}" "${ns}" initContainers "${SIGNER}" | grep -qx "${SECRET}"; then
    echo "FAIL  ${ns}/${pod} — ${SIGNER} does not reference ${SECRET}" >&2
    failed=1
    continue
  fi

  # Then envoy, which signs nothing and is given no reference.
  if refs_on "${pod}" "${ns}" containers envoy | grep -qx "${SECRET}"; then
    echo "FAIL  ${ns}/${pod} — envoy carries ${SECRET}, which only ${SIGNER} needs" >&2
    failed=1
    continue
  fi
  echo "ok    ${ns}/${pod}  (${SIGNER} only)"
done

if [ "${failed}" -ne 0 ]; then
  echo >&2
  echo "Not every gateway carries the reference where the policy puts it. Pods" >&2
  echo "admitted before the policy, or under an earlier one, keep the spec they" >&2
  echo "were admitted with — delete them and let Envoy Gateway recreate them:" >&2
  echo "  kubectl delete pod -A -l ${SELECTOR}" >&2
fi
if [ "${expired}" -ne 0 ]; then
  echo >&2
  echo "The gateways sign with credentials that no longer work, or that cannot be" >&2
  echo "shown to. Renew the profile's session and reinstall; every copy follows" >&2
  echo "the source Secret:" >&2
  echo "  aws sso login --profile <profile>    # for an SSO profile" >&2
  echo "  task stack:ai-platform:credentials" >&2
fi
if [ "${failed}" -ne 0 ] || [ "${expired}" -ne 0 ]; then
  exit 1
fi

echo
echo "${#PODS[@]} gateway(s) sign with the local Bedrock credentials."
