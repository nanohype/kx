#!/usr/bin/env bash
# Patches CoreDNS to forward external queries to public DNS (1.1.1.1, 8.8.8.8) instead of
# the kubelet's /etc/resolv.conf inherited from the Docker Desktop VM.
#
# Why: kind nodes inherit Docker Desktop's DNS (192.168.65.254), which is only reachable
# from the host network namespace — not from cilium-managed pod network. Pods that try to
# resolve external names (raw.githubusercontent.com, public OCI registries, etc.) time out.
# Forwarding directly to public DNS sidesteps the Docker Desktop DNS bridge entirely.
#
# Idempotent: kubectl apply + rollout restart can run repeatedly.
set -euo pipefail

kubectl -n kube-system apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: coredns
  namespace: kube-system
data:
  Corefile: |
    .:53 {
        errors
        health {
           lameduck 5s
        }
        ready
        kubernetes cluster.local in-addr.arpa ip6.arpa {
           pods insecure
           fallthrough in-addr.arpa ip6.arpa
           ttl 30
        }
        prometheus :9153
        forward . 1.1.1.1 8.8.8.8 {
           max_concurrent 1000
        }
        cache 30 {
           disable success cluster.local
           disable denial cluster.local
        }
        loop
        reload
        loadbalance
    }
EOF

# Only roll the deployment if a coredns pod is already Ready. On a fresh `task up` the
# cluster has no CNI yet (cilium is installed by stack:core), so coredns pods are Pending
# and a rollout-restart would block forever. The coredns reload plugin in the Corefile
# above will pick up ConfigMap changes automatically once the pods do come up.
if kubectl -n kube-system get pods -l k8s-app=kube-dns \
     -o jsonpath='{.items[*].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null \
     | grep -q "True"; then
  kubectl -n kube-system rollout restart deployment/coredns
  kubectl -n kube-system rollout status  deployment/coredns --timeout=60s
  echo "coredns: rolled with new Corefile (external DNS → 1.1.1.1 + 8.8.8.8)"
else
  echo "coredns: ConfigMap applied; restart deferred (no Ready pod yet — coredns will load it after cilium comes up)"
fi
