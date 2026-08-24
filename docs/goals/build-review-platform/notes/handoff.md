# Goal Maker Handoff

`state.yaml` is authoritative. This note only orients the next continuation.

## Current state

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T102-T104 are complete at `4474d71fce7c970c4f9577931e8f926fe2d51219`.
  PostgreSQL application persistence and controlled backup/restore are proven.
- T105 is complete at `4042ddf0d03fec8d538241775caeb0e3aead12bc`.
  Durable jobs are one-to-one extensions of run-owned acceptance identity, with
  atomic fenced claim, queued supersession, and recovery-safe lease history.
- T106 is the sole active task: add fenced heartbeat, retry/dead-letter,
  bounded expiry recovery, and atomic two-way run/job reconciliation without
  deploying a worker or enabling the Hermes API server.
- Public identity is “Review Agent.” `sundsvall-standard` remains a selectable
  municipal profile, not the product identity. Model-era review-depth ceilings
  are gone; pageable or honestly incomplete contracts own large inputs.

## T106 execution boundary

- Keep review_runs as outcome owner and review_jobs as execution-state owner;
  compose their short transactions in review_run_application with pull-request,
  run, then job lock ordering and set-based sweep reconciliation.
- Every claim consumes attempt budget. Add no execution key, recovery counter,
  metrics surface, reaper daemon, scheduler, broker, or queue port.
- Keep API_SERVER_ENABLED=false. T110 owns the non-deployed worker and pinned
  Hermes continuation proof; T107 owns activation, durable ingress, fairness,
  cancellation, and operator controls; T108 owns publication outbox behavior.
- Update the public roadmap after T106 without claiming that a worker is live.

## Remaining order

T106 atomic lifecycle/recovery → T110 non-deployed worker proof → T107
activation/supersession/fairness/fast enqueue → T108 publication outbox → T109
final audit.
Security-scanner and Codex Security integrations remain explicitly deferred.

## Verification continuity

- T100: PostgreSQL 91 tests, strict Pyright, 606-test bundle, docs/site, and
  Claude Opus/high green 8. Session `review-agent-t100-postgresql-feedback`.
- T101: PostgreSQL 101 tests, strict Pyright, 616-test bundle, 24 docs contracts,
  and two Claude Opus/high gates green at 8. Sessions
  `review-agent-t101-publication-parity` and
  `review-agent-t101-operator-parity` (UUID
  `e3825c18-a74f-44f0-b9f0-ba335ca4a71e`). Exact-commit Python run
  `32733756788` and Pages run `32733756671` passed.
- T102-T104: PostgreSQL 104 tests, strict Pyright, 304-test bundle, docs/Compose/
  site checks, and Claude Opus/high green 8. Session
  `review-agent-t102-postgresql-cutover` (UUID
  `71ee5913-d171-444c-a608-0df179d463b5`). Exact-commit Python run
  `32745614488`, Pages run `32745614610`, and board run `32745849829` passed.
- T105: PostgreSQL 116 tests, strict Pyright, 316-test bundle, nine-document
  manifest, and final Claude Opus/high green 8. Sessions
  `review-agent-t105-durable-jobs` (UUID
  `fbf41768-47f2-4631-8941-518111bd54f3`) and the re-sliced constraint gate
  `review-agent-t105-constraint-final` (UUID
  `1d4c3167-0193-4c9e-9632-e9a137aa0cfd`). Exact-commit run `32750859418`
  passed all required jobs.
- Preserve user-owned `refactor-plan1.md`.
- Stop all Codex and Claude work by 23:50 Europe/Stockholm; resume at 06:00.
