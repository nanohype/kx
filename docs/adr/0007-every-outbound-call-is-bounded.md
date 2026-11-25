# 0007. Every call that leaves the machine is bounded

## Status

Accepted.

## Context

kx's installers and gates talk to an API server, a container runtime, chart
repositories, registries and cloud endpoints. A call with no bound waits on a
stuck daemon or an unresponsive endpoint until somebody notices, and the step
it hangs reads exactly like a slow one. Several of the tools involved have no
bound of their own. Helm's `--timeout` starts only once the chart has arrived,
so it leaves the chart fetch unbounded. The workstations kx runs on include
macOS, which ships bash 3.2 and no GNU `timeout`.

## Decision

Every call that leaves the machine carries a bound:

- **kubectl:** `--request-timeout` on every call, plus `--timeout` on every
  wait, delete and rollout status, since those can wait on a cluster
  indefinitely.
- **curl:** `--max-time`.
- **Tools with no bound of their own:** docker, kind, the aws CLI, helm's read
  verbs and the JavaScript package runners run under `deadline`, from
  `cluster/deadline.sh`. It is written for bash 3.2, sends TERM and then KILL
  to a command still running at its limit, and returns 124 after naming it.
- **Helm installs and renders:** run under a deadline 300 seconds past their
  own `--timeout`, which bounds the chart fetch and never cuts short a wait
  helm was told to make.
- **The gates:** every `subprocess` call in a tracked Python file carries a
  `timeout`.

`scripts/check-slice-integrity.py` holds all of it as the invariant that every
call that leaves this machine is bounded, including that a deadline outlasts
the command's own timeout.

## Alternatives

- **GNU `timeout`.** It is not on macOS by default, and requiring it would make
  a kx dependency of a coreutils install.
- **Each tool's own defaults.** kubectl has no default request timeout, and
  helm's covers only part of the work.

## Consequences

- A hang fails with the command named and status 124, rather than holding a
  terminal open.
- The limits are generous, so a slow network is not failed for being slow.
- A new call without a bound fails CI, so the rule holds as the tree grows.
