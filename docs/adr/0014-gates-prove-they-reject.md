# 0014. Every gate is observed to reject

## Status

Accepted.

## Context

A gate that has stopped rejecting looks exactly like one that passes. A
pattern that no longer matches, a population that went empty, or a tool that
was not on PATH each produces a green job. Read from the outside, a vacuous
pass over nothing and a real pass over everything are the same output.

## Decision

`scripts/check-slice-integrity.py` holds the tree's invariants, and holds every
gate in the repository to having been seen to work.

- **Controls run first, every time.** Every invariant has controls: a broken
  fixture it must reject and a clean one it must accept, each built as a real
  git repository. Every run executes them before it reports on the tree, and
  refuses to report if one fails. There is no self-test flag to forget.
- **Every gate is probed.** Each gate is run against an input it must reject
  and one it must accept.
- **Every gate refuses without its authority.** A gate whose verdict depends on
  a tool or a source is run with that authority missing, and it must refuse,
  saying so, rather than pass.
- **No invariant passes over nothing.** Each one states the least it must have
  examined, so a corpus that falls away fails instead of passing over less.
- **It reads code, not comments, where that is the question.** An invariant
  that asks whether code references something reads a view of the source with
  comment bodies blanked, so a comment cannot stand in for the code.

## Alternatives

- **Self-tests behind a flag.** A workflow can stop passing the flag, and
  nothing notices.
- **Trusting a green job.** That is the failure this record exists to prevent.

## Consequences

- A change that blinds a gate fails CI in the same run.
- A new gate or invariant arrives with controls that show it rejecting, and
  with a probe of its own.
- The floor is large, and it is the place to read what each rule is held to.
