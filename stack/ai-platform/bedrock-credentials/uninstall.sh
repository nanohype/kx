#!/usr/bin/env bash
# Withdraw the local Bedrock credentials and the rights they needed.
#
# The policies go first, then the copies by name. A GeneratingPolicy that
# synchronizes puts back a copy deleted while it exists, so deleting the copies
# under a live policy only has them recreated; and deleting the policy does not
# take its copies with it, because Kyverno leaves a cloned resource in place when
# the policy that cloned it goes. Removing the policy stops both, and then the
# copies can simply be named — which is also what keeps a live AWS session token
# from waiting on background-controller work this script has no barrier for,
# while the last step revokes the Secret grant that controller would need.
#
# Order: the policies that would make or restore copies, then the copies, then
# the source, then the grant. Every step is reached even if an earlier one finds
# nothing.
set -euo pipefail

SECRET="kx-bedrock-credentials"
SOURCE_NS="kyverno"
GENERATOR="clone-local-bedrock-credentials"
MUTATOR="inject-local-bedrock-credentials"
GRANT="kyverno:bedrock-credentials-secrets"

# Every namespace the clone rule targets. `|| true` on the listing because a
# cluster with no tenant namespaces is the normal case, not an error.
#
# read -r rather than mapfile, for the reason verify.sh states beside it:
# mapfile is a bash 4 builtin and the documented prerequisites install no bash,
# so this runs under /bin/bash 3.2 on macOS. This script is the first command of
# `credentials:disable`, which is the first command of `disable` — aborting here
# stops the whole slice teardown before a single delete, leaving the credential
# and its clones exactly where they were.
TENANTS=()
while IFS= read -r ns; do
  [ -n "${ns}" ] && TENANTS+=("${ns}")
done < <(
  kubectl get namespaces -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep '^tenants-' || true
)

# Each policy is deleted only where its kind is served. This script is the first
# command of the slice's `disable`, which runs whether or not the credentials
# task ever did, and on a cluster where the security slice is already gone
# `kubectl delete` of an unknown kind is an error that would stop the teardown
# before it began. Only a NotFound answer for the CRD counts as not served; any
# other failure is a cluster this script cannot read, and it stops rather than
# reporting a teardown it did not do.
delete_if_served() { # crd kind-plural name
  local err
  if err="$(kubectl get crd "$1" 2>&1 >/dev/null)"; then
    kubectl delete "$2" "$3" --ignore-not-found
  elif ! printf '%s' "${err}" | grep -qiE 'not found|NotFound'; then
    echo "Could not determine whether $1 is served — kubectl said:" >&2
    printf '%s\n' "${err}" | sort -u | sed 's/^/  /' >&2
    exit 1
  fi
}
delete_if_served generatingpolicies.policies.kyverno.io \
  generatingpolicies.policies.kyverno.io "${GENERATOR}"
delete_if_served mutatingpolicies.policies.kyverno.io \
  mutatingpolicies.policies.kyverno.io "${MUTATOR}"
# The ClusterPolicy an earlier version of this slice installed, on a cluster
# that never re-ran install.sh to retire it.
delete_if_served clusterpolicies.kyverno.io clusterpolicies.kyverno.io "${MUTATOR}"

if [ "${#TENANTS[@]}" -gt 0 ]; then
  echo "Removing the credential clone from ${#TENANTS[@]} tenant namespace(s) ..."
  for ns in "${TENANTS[@]}"; do
    kubectl delete secret "${SECRET}" -n "${ns}" --ignore-not-found
  done
fi

kubectl delete secret "${SECRET}" -n "${SOURCE_NS}" --ignore-not-found
kubectl delete clusterrole "${GRANT}" --ignore-not-found

# The policies are gone, so a namespace created after this carries no copy.
# Anything left is a copy this script could not see — report it rather than
# reporting a clean teardown over a set that may have grown underneath us.
# The listing failing and the listing being empty are different facts: one means
# nothing is left, the other means nobody looked. Reporting a clean teardown for
# the second is the vacuous pass this script exists to refuse.
if ! left_raw="$(kubectl get secrets -A \
  -o jsonpath="{range .items[?(@.metadata.name=='${SECRET}')]}{.metadata.namespace}{\"\n\"}{end}" \
  2>&1)"; then
  echo "WARNING: could not list Secrets cluster-wide to confirm the teardown — kubectl said:" >&2
  printf '%s\n' "${left_raw}" | sort -u | sed 's/^/  /' >&2
  echo "Re-run this once kubectl can reach the cluster; clones may remain." >&2
  exit 1
fi
LEFT=()
while IFS= read -r ns; do
  [ -n "${ns}" ] && LEFT+=("${ns}")
done <<<"${left_raw}"
if [ "${#LEFT[@]}" -gt 0 ]; then
  echo "WARNING: ${SECRET} still present in:" >&2
  printf '  %s\n' "${LEFT[@]}" >&2
  echo "Delete these by hand — the policy that would have made or restored them is gone." >&2
  exit 1
fi

echo "Local Bedrock credentials withdrawn."
