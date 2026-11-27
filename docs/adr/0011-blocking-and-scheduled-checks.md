# 0011. CI blocks on facts about the commit and schedules the rest

## Status

Accepted.

## Context

Some questions kx asks of itself have answers fixed by the commit under test:
whether every slice renders, whether every pin is watched and recorded, and
whether the pins match the catalog at the recorded ref. Others change without
this repository changing. A maintainer deprecates a chart, the catalog moves
past a pin, a vulnerability is published against a pinned image overnight, or
the eks-agent-platform sibling moves on. A required check that answers the
second kind turns red on pull requests that did not cause the change, and a
check that is often red for no fault of the change is soon ignored.

## Decision

- **`.github/workflows/ci.yml`** runs on every pull request and every push to
  `main`, and asks only questions about the commit. A merge gate job requires
  every other job, and counts a skipped job as failed.
- **Questions about the world** run weekly, and on demand, in workflows of
  their own:
  - `chart-provenance.yml`: whether a pinned chart has been deprecated or has
    changed hands upstream.
  - `mirror-freshness.yml`: whether the eks-gitops catalog has moved past the
    pin in `stack/upstream.json`.
  - `image-cves.yml`: whether a pinned image carries a CRITICAL that no
    exception covers.
  - `sibling-freshness.yml`: whether eks-agent-platform has moved past its pin,
    and whether moving to it would still render.
- Nothing in CI needs a running cluster or a cloud credential. What needs a
  cluster stays a local `task`.

## Alternatives

- **Everything on the blocking path.** Every pull request would carry the
  world's changes as its own failures.
- **Not asking the time-dependent questions.** Pins would fall behind, and
  images would collect vulnerabilities, with nothing to say so.

## Consequences

- A red scheduled run is a task to pick up, not a broken build. Each workflow
  states what its red run means and what to do about it.
- Where `ci.yml` reads another repository, it reads it at the ref
  `stack/upstream.json` records, never at its default branch, so a merge there
  cannot redden a pull request here.
- A schedule-only job cannot live in `ci.yml`: the merge gate refuses a job it
  does not watch, and counts a skipped dependency as a failure.
- The image scan's red run opens one tracking issue, or comments on it when it
  is already open, so a red run is seen without anyone watching the schedule.
