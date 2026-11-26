# 0001. Every addon is an explicit install script with a pinned chart

## Status

Accepted.

## Context

kx installs the charts the production catalog runs, and its user lives in the
result across many projects. When something misbehaves, the question is what
was installed and how, and the answer has to be readable without learning a
tool. Production manages the same charts with ArgoCD Applications, which suits
a fleet reconciled from git and does not suit a workstation, where the loop is
"change a value, run it, look".

## Decision

Every addon is a directory under `stack/<slice>/<addon>/` holding an
`install.sh` and, where it overrides chart defaults, a `values.yaml`.

- `install.sh` adds its chart repository and runs one `helm upgrade --install`
  with the chart version pinned and an explicit `--timeout`. It is idempotent,
  and it is meant to be read top to bottom before it is run.
- `values.yaml` holds local deltas from the chart's defaults, never production
  values, which assume IRSA, ENI networking and load balancers kind does not
  have.
- A slice's `Taskfile.yaml` runs its installers in dependency order on
  `enable`, and uninstalls them in reverse on `disable`.
- ArgoCD is part of the core stack and idle: no Application, App-of-Apps or
  ApplicationSet points at anything kx installs.

## Alternatives

- **helmfile, or a wrapper that generates the helm calls.** Declarative
  release state is convenient, but it adds a tool and a computed layer between
  the reader and the command that ran, which is the thing this workspace exists
  to make plain.
- **ArgoCD owning the addons, as in production.** The local loop would become
  commit, push and wait for a sync, and two owners would contend for the same
  releases whenever someone also ran an installer by hand. ArgoCD stays
  installed so a project can be tested against it.

## Consequences

- Any installer runs on its own (`bash stack/core/cilium/install.sh`), and what
  it did is the script.
- The installers repeat one shape. Gates hold that shape instead of a
  framework: `scripts/check-slice-integrity.py` requires an explicit timeout on
  every install, a repository add that does not swallow its own failure, and
  every file in an addon directory to be applied by its installer.
- Renovate watches every pin (`scripts/check-renovate-coverage.py`), and
  `scripts/mirror-check.py` holds each one to the production catalog, so a pin
  cannot drift silently.
- Nothing reconciles drift continuously. Re-running an installer converges its
  addon, and `task status` lists what is installed.
