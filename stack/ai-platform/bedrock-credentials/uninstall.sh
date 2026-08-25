#!/usr/bin/env bash
# Withdraw the local Bedrock credentials and the rights they needed.
#
# The clones are deleted here rather than left to Kyverno. A generate rule with
# synchronize does retract what it created when its policy goes, but that is
# background-controller work with no barrier this script can wait on, and the
# last step of a teardown revokes the very Secret grant that controller needs to
# do it. Racing a live AWS session token against a permission this script is
# removing is not a trade worth making when the clones can simply be named.
#
# Order: copies, then the policy that would make more, then the source, then the
# grant. Every step is reached even if an earlier one finds nothing.
set -euo pipefail

SECRET="kx-bedrock-credentials"
SOURCE_NS="kyverno"
POLICY="inject-local-bedrock-credentials"
GRANT="kyverno:bedrock-credentials-secrets"

# Every namespace the clone rule targets. `|| true` on the listing because a
# cluster with no tenant namespaces is the normal case, not an error.
mapfile -t TENANTS < <(
  kubectl get namespaces -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep '^tenants-' || true
)

if [ "${#TENANTS[@]}" -gt 0 ]; then
  echo "Removing the credential clone from ${#TENANTS[@]} tenant namespace(s) ..."
  for ns in "${TENANTS[@]}"; do
    kubectl delete secret "${SECRET}" -n "${ns}" --ignore-not-found
  done
fi

kubectl delete clusterpolicy "${POLICY}" --ignore-not-found
kubectl delete secret "${SECRET}" -n "${SOURCE_NS}" --ignore-not-found
kubectl delete clusterrole "${GRANT}" --ignore-not-found

# The clone rule is gone, so a namespace created after this carries no copy.
# Anything left is a copy this script could not see — report it rather than
# reporting a clean teardown over a set that may have grown underneath us.
mapfile -t LEFT < <(
  kubectl get secrets -A \
    -o jsonpath="{range .items[?(@.metadata.name=='${SECRET}')]}{.metadata.namespace}{\"\n\"}{end}" \
    2>/dev/null || true
)
if [ "${#LEFT[@]}" -gt 0 ]; then
  echo "WARNING: ${SECRET} still present in:" >&2
  printf '  %s\n' "${LEFT[@]}" >&2
  echo "Delete these by hand — the policy that would have retracted them is gone." >&2
  exit 1
fi

echo "Local Bedrock credentials withdrawn."
