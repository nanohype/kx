# shellcheck shell=bash
# kx's kubeconfig binding. Sourced, never run, by every kx script that reaches
# the cluster, directly after the script sets SCRIPT_DIR:
#
#   . "${SCRIPT_DIR}/../../../cluster/kubeconfig.sh"
#
# kx keeps its cluster's credentials in .kube/config at the root of this
# repository. `task up` writes that file, and it holds the kind-kx context and
# nothing else, so pointing KUBECONFIG at it means nothing a kx script runs can
# reach another cluster — whatever the shell's own KUBECONFIG or current context
# names. The user's kubeconfig is neither read nor written. Without a kind-kx
# context in that file, the script stops here, before the first command that
# would act on a cluster.
#
# A Taskfile cannot do this for the scripts. Task's `env:` never overrides a
# variable the shell already exports, and KUBECONFIG is the one people export
# when they work across clusters, which is exactly when this matters.

KUBECONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.kube/config"
export KUBECONFIG

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl is not on PATH, so kx cannot reach its cluster." >&2
  exit 1
fi
if [ "$(kubectl config current-context 2>/dev/null)" != "kind-kx" ]; then
  echo "ERROR: ${KUBECONFIG} holds no kind-kx context, so there is no kx cluster to act on." >&2
  echo "Run: task up — it creates the cluster, or writes this file for one that already exists." >&2
  exit 1
fi
