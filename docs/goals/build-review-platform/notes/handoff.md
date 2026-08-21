# Goal Maker Handoff

This is a non-authoritative conversational snapshot. If it conflicts with
`state.yaml`, `state.yaml` wins.

## Current Direction

- Last explicit user instruction: continue without stopping, use subagents only
  for read-only research, follow both approved Sundsvall refactor plans, keep
  testing proportional, and keep security integration deferred.
- Latest verified outcome: the Docusaurus Pages site is live; typed settings and
  resource ownership are centralized; the concrete bounded GitHub read client
  was delivered at `3c1072bd09b0a1e09eb1bb95edac323429ba4a77` and audited
  complete. The overall platform goal remains active.
- Unfinished action: implement active task T008, the frozen review-run lifecycle
  and coverage application owner, then verify, peer-review, commit, push, audit,
  and continue the approved maintainability sequence.

## Orientation

- Work in `/Users/ccimen/Documents/ChatGPT/Review Agent` on
  `CCimen/review-agent` `main`; direct main commits and pushes are authorized.
- Read `goal.md`, `state.yaml`, both approved plan files named by the board, and
  the T008 scope before acting. Verify repository state against
  `continuity.last_verified_revision`.
- The primary agent implements. Subagents are read-only researchers. Keep
  scanner/Codex Security, PostgreSQL, jobs, GitHub App, policy overlays,
  publication, and feedback out of T008.
- Apply behavior-first tests proportionately and run the required single
  skeptical Codex peer gate at a stable candidate.

## Recent Conversation Tail

- The resource-lifetime cleanup closed SQLite and HTTPError resources without
  behavior changes and was pushed as `c59b6ac`.
- Two read-only plan audits independently selected the concrete GitHub read
  client as the next A2 slice. It was pushed as `3c1072b`; a first peer gate
  requested stronger bounded-read and 404/406 proofs, and the resumed gate was
  green at score 8 after focused test-only changes.
- A Scout and Judge then selected T008: move review-run and coverage coordination
  into one concrete application module while retaining existing persistence
  owners and all public behavior.
