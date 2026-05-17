#!/usr/bin/env bash
# kx — local container registry attached to the kind network.
# Lets you `docker push localhost:5001/foo:tag` and have the cluster pull from `localhost:5001/foo:tag`.
# Standard kind-with-registry pattern. Idempotent.

set -euo pipefail

REG_NAME="kx-registry"
REG_PORT="5001"

# Ensure the registry container exists and is running. Three states to handle:
#   - doesn't exist        → docker run
#   - exists, stopped      → docker start (e.g. prior `task up` failed mid-flight)
#   - exists, running      → no-op
if ! docker inspect "${REG_NAME}" >/dev/null 2>&1; then
  docker run -d --restart=always \
    -p "127.0.0.1:${REG_PORT}:5000" \
    --network bridge \
    --name "${REG_NAME}" \
    registry:2 >/dev/null
elif [ "$(docker inspect -f '{{.State.Running}}' "${REG_NAME}")" != 'true' ]; then
  docker start "${REG_NAME}" >/dev/null
fi

# Connect the registry to the kind network so nodes can reach it by name.
if ! docker network inspect kind 2>/dev/null | grep -q "\"Name\": \"${REG_NAME}\""; then
  docker network connect "kind" "${REG_NAME}" 2>/dev/null || true
fi

# Drop the per-registry hosts.toml on every node so containerd 2.x routes
# pulls of localhost:5001/* to the in-network kx-registry container.
# (Paired with the `config_path = "/etc/containerd/certs.d"` patch in kind-config.yaml.)
for node in $(kind get nodes --name kx); do
  docker exec "${node}" mkdir -p "/etc/containerd/certs.d/localhost:${REG_PORT}"
  docker exec -i "${node}" sh -c "cat > /etc/containerd/certs.d/localhost:${REG_PORT}/hosts.toml" <<EOF
[host."http://${REG_NAME}:5000"]
EOF
done

# Document the registry to the cluster per containerd KEP-1755.
kubectl --context kind-kx apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REG_PORT}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

echo "registry: localhost:${REG_PORT} (container: ${REG_NAME})"
