# 0017. What the gates share is written once

## Status

Accepted.

## Context

The integrity floor counts every `scripts/*.py` as a gate, and runs each one
against inputs it must reject and accept
([0014](0014-gates-prove-they-reject.md)). With every file there a gate, a
helper that two gates needed could live only inside each of them, and the
copies drifted apart:

- Two gates read a chart's version out of the same `install.sh` differently:
  one from the helm command that installs the chart, the other from the first
  `--version` anywhere in the file.
- Most scratch directories were made with `mkdtemp` and never removed, so each
  run of the floor left its fixture trees behind.
- A command that outran its timeout raised past the gate, which read as the
  gate breaking rather than as a run that could not finish.
- A scanner report that was not JSON was read as a report with no findings,
  which is what a clean image produces.

## Decision

- A gate is a top-level script, `scripts/*.py`, and the floor counts each one.
- What more than one gate needs lives once, in `scripts/gatelib/`: the chart
  pins an `install.sh` carries, rendered manifests and their pod specs, source
  with its comments blanked, how a gate says why it stopped, and the scratch
  directories it writes. A gate imports it by name, because python3 puts a
  script's own directory first on `sys.path`.
- The library is not a gate. `tests/gates/test_gatelib.py` holds each of its
  functions to its contract, and each of its files is held to every line and
  branch on its own.
- A scratch directory is removed when the block that made it ends, however it
  ends.
- A command that outruns its bound is reported as a run that could not
  complete, under the status each gate gives for one.
- Scanner output the gate cannot read makes an image this run could not scan,
  never a clean one.

## Alternatives

- **A copy in each gate.** What came before, and the copies drifted.
- **An installed package.** Every gate would need an install step before it
  could run, in CI and on a workstation, for code that sits beside it.
- **One script for every gate.** Each gate runs, is probed and fails on its
  own. Folded together, one gate's fault would stop the rest.

## Consequences

- A helper that a second gate needs moves into the library rather than being
  copied.
- A change to the library reaches every gate that imports it at once. The
  floor's probes run each of those gates, and the library's tests run on their
  own.
- The floor's population is the top level of `scripts/` alone, so a gate
  placed under `scripts/gatelib/` would go unprobed.
