#!/usr/bin/env bash
# Assert the credentials actually reached the gateways.
#
# The failure this exists for: a selector that matches nothing. Kyverno reports
# a healthy policy either way — a ClusterPolicy that mutates zero pods is not an
# error to Kyverno, it is a policy with nothing to do. The symptom would surface
# much later as AccessDeniedException from Bedrock, which reads as an IAM
# problem rather than a policy that never fired.
#
# So this checks the value, not the health: every gateway data-plane pod's envoy
# container references the Secret, and finding no gateways at all is itself a
# failure, because a verify that passes vacuously is worse than none.
#
# It reads the pod spec rather than the process environment because the Envoy
# image is distroless — there is no shell and no `env` binary to exec. The spec
# is sufficient: with the reference present and the Secret in the namespace,
# injection is the kubelet's guarantee, and the clone rule is what puts the
# Secret there.
set -euo pipefail

SELECTOR="app.kubernetes.io/managed-by=envoy-gateway,app.kubernetes.io/component=proxy"
SECRET="kx-bedrock-credentials"

mapfile -t PODS < <(
  kubectl get pods -A -l "${SELECTOR}" \
    -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null
)

if [ "${#PODS[@]}" -eq 0 ]; then
  echo "FAIL: no gateway data-plane pods found (selector: ${SELECTOR})." >&2
  echo "      Either no ModelGateway has reconciled yet, or the labels Envoy" >&2
  echo "      Gateway stamps have changed and the policy selector is stale." >&2
  exit 1
fi

failed=0
for entry in "${PODS[@]}"; do
  ns="${entry%%/*}"
  pod="${entry##*/}"
  refs="$(kubectl get pod "${pod}" -n "${ns}" \
    -o jsonpath='{range .spec.containers[?(@.name=="envoy")].envFrom[*]}{.secretRef.name}{"\n"}{end}' 2>/dev/null)"
  if printf '%s\n' "${refs}" | grep -qx "${SECRET}"; then
    echo "ok    ${ns}/${pod}"
  else
    echo "FAIL  ${ns}/${pod} — envoy container does not reference ${SECRET}" >&2
    failed=1
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
echo "${#PODS[@]} gateway(s) carry the local Bedrock credentials."
