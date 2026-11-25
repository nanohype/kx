# shellcheck shell=bash
# deadline SECONDS COMMAND [ARGUMENT...]
#
# Runs COMMAND with a bound on how long it may take, for the tools that have no
# bound of their own: docker, kind, helm's read verbs, and the aws CLI resolving
# credentials, which can wait on an SSO or STS endpoint its own timeouts do not
# cover. kubectl takes --request-timeout and curl takes --max-time, so those
# carry their own flag instead. helm's installs and renders run under a deadline
# as well: their --timeout starts once the chart has arrived, so it bounds the
# wait on the cluster and leaves the chart fetch before it unbounded. Their limit
# outlasts that --timeout, by the five minutes kx allows the transfer, so the
# deadline never stops a wait helm was told to make. A command with no bound
# waits on a stuck daemon or an unresponsive API server until somebody notices,
# and the step it hangs reads exactly like a slow one.
#
# Sourced by a script that needs it, after the kubeconfig binding:
#
#   . "${SCRIPT_DIR}/../../../cluster/deadline.sh"
#   deadline 600 docker build ...
#
# and run as a program from a Taskfile, where a function cannot be defined:
#
#   bash '{{.ROOT_DIR}}/cluster/deadline.sh' 60 helm list -A
#
# Written for bash 3.2, which macOS ships, and for a machine without GNU
# timeout, which macOS does not ship. The command keeps this shell's stdin,
# stdout and stderr, so it works in a pipeline, beside a heredoc and inside a
# command substitution. A command still running at the deadline gets TERM, then
# KILL five seconds later, and deadline returns 124 — the status GNU timeout
# uses — after naming the command it stopped.

deadline() {
  local limit="$1"
  shift
  # The watchdog records that it fired before it signals, so a command that
  # dies of the TERM is told apart from one that exited on its own — an exit
  # status cannot say which, since a command may exit 143 by itself.
  local mark
  mark="$(mktemp -d "${TMPDIR:-/tmp}/kx-deadline.XXXXXX")"
  # stdin named explicitly: a background command in a non-interactive shell
  # otherwise reads /dev/null, and a heredoc meant for it would vanish.
  "$@" <&0 &
  local pid=$!
  # The watchdog writes nowhere, so a command substitution around deadline is
  # not held open by its sleep after the command has finished. Its sleep runs as
  # a child it waits on, so stopping the watchdog stops the sleep with it rather
  # than leaving one behind for the rest of the limit.
  #
  # Both sleeps are waited on rather than run in the foreground, because bash
  # defers a trap until a foreground command finishes: a watchdog stopped during
  # its grace period would otherwise hold deadline for the rest of it.
  (
    nap=""
    trap 'kill "${nap}" 2>/dev/null; exit 0' TERM
    sleep "${limit}" &
    nap=$!
    : >"${mark}/armed"
    wait "${nap}"
    : >"${mark}/fired"
    kill -TERM "${pid}" 2>/dev/null || exit 0
    sleep 5 &
    nap=$!
    wait "${nap}"
    kill -KILL "${pid}" 2>/dev/null || true
  ) >/dev/null 2>&1 &
  local watchdog=$!
  local status=0
  wait "${pid}" || status=$?
  # Stopped only once it is armed — its trap set and its sleep's pid recorded —
  # so the sleep is always its own to stop. Stopped any earlier, a watchdog
  # caught between starting the sleep and recording it would leave it behind.
  while [ ! -e "${mark}/armed" ] && kill -0 "${watchdog}" 2>/dev/null; do
    sleep 0.05
  done
  kill "${watchdog}" 2>/dev/null || true
  wait "${watchdog}" 2>/dev/null || true
  if [ -e "${mark}/fired" ]; then
    rm -rf "${mark}"
    echo "deadline: '$*' did not finish within ${limit}s and was stopped." >&2
    return 124
  fi
  rm -rf "${mark}"
  return "${status}"
}

# Run as a program rather than sourced: bound the command line it was given.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  deadline "$@"
fi
