#!/usr/bin/env bash
# Assert the credentials actually reached the container that signs.
#
# The failure this exists for: a mutation that matches nothing. Kyverno reports
# a healthy policy either way — a ClusterPolicy that mutates zero pods is not an
# error to Kyverno, it is a policy with nothing to do. The symptom would surface
# much later as AccessDeniedException from Bedrock, which reads as an IAM
# problem rather than a policy that never fired.
#
# Envoy AI Gateway resolves the AWS credential chain and SigV4-signs inside its
# external processor, so `ai-gateway-extproc` is the container the reference has
# to reach. It is declared as a native sidecar, which puts it under
# initContainers. The envoy container carries the same reference and needs no
# AWS identity for the Bedrock call, so it is checked second and separately:
# a pod where envoy is patched and extproc is not still fails every model call,
# and asserting only envoy would report that pod as healthy.
#
# It reads the pod spec rather than the process environment because the Envoy
# image is distroless — there is no shell and no `env` binary to exec. The spec
# is sufficient: with the reference present and the Secret in the namespace,
# injection is the kubelet's guarantee, and the clone rule is what puts the
# Secret there.
set -euo pipefail

SELECTOR="app.kubernetes.io/managed-by=envoy-gateway,app.kubernetes.io/component=proxy"
SECRET="kx-bedrock-credentials"
SIGNER="ai-gateway-extproc"

# read -r rather than mapfile: mapfile is a bash 4 builtin and the documented
# prerequisites install no bash, so on macOS this runs under /bin/bash 3.2.
PODS=()
while IFS= read -r line; do
  [ -n "${line}" ] && PODS+=("${line}")
done < <(
  kubectl get pods -A -l "${SELECTOR}" \
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
  kubectl get pod "$1" -n "$2" \
    -o jsonpath="{range .spec.$3[?(@.name=='$4')].envFrom[*]}{.secretRef.name}{\"\n\"}{end}"
}

names_in() { # pod ns jsonpath-container-list
  kubectl get pod "$1" -n "$2" -o jsonpath="{range .spec.$3[*]}{.name}{\"\n\"}{end}"
}

failed=0
for entry in "${PODS[@]}"; do
  ns="${entry%%/*}"
  pod="${entry##*/}"

  # The signer first. A rename upstream makes the strategic-merge `(name):` key
  # match nothing and add nothing, silently — so a missing container is reported
  # as its own failure rather than folded into a missing reference.
  if ! names_in "${pod}" "${ns}" initContainers | grep -qx "${SIGNER}"; then
    echo "FAIL  ${ns}/${pod} — no ${SIGNER} sidecar under initContainers" >&2
    echo "      The signer container was renamed upstream; policy.yaml's" >&2
    echo "      \`(name): ${SIGNER}\` patch matches nothing and adds nothing." >&2
    failed=1
    continue
  fi
  if ! refs_on "${pod}" "${ns}" initContainers "${SIGNER}" | grep -qx "${SECRET}"; then
    echo "FAIL  ${ns}/${pod} — ${SIGNER} does not reference ${SECRET}" >&2
    failed=1
    continue
  fi

  # Then envoy, which needs no AWS identity today. A miss here is not a signing
  # failure, so it is reported without failing the run.
  if refs_on "${pod}" "${ns}" containers envoy | grep -qx "${SECRET}"; then
    echo "ok    ${ns}/${pod}  (${SIGNER} + envoy)"
  else
    echo "ok    ${ns}/${pod}  (${SIGNER}; envoy carries no reference)"
  fi
done

if [ "${failed}" -ne 0 ]; then
  echo >&2
  echo "The policy did not mutate every gateway. Pods admitted before the policy" >&2
  echo "was installed keep their original spec — delete them and let Envoy" >&2
  echo "Gateway recreate them:" >&2
  echo "  kubectl delete pod -A -l ${SELECTOR}" >&2
  exit 1
fi

echo
echo "${#PODS[@]} gateway(s) sign with the local Bedrock credentials."
