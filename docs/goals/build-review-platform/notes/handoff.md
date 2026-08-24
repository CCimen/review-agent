# Goal Maker Handoff

`state.yaml` is authoritative.

## Current state

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T102-T104 are complete at `4474d71fce7c970c4f9577931e8f926fe2d51219`.
  PostgreSQL application persistence and controlled backup/restore are proven.
- T105 is complete at `4042ddf0d03fec8d538241775caeb0e3aead12bc`.
  Durable jobs are one-to-one extensions of run-owned acceptance identity, with
  atomic fenced claim, queued supersession, and recovery-safe lease history.
- T106 is complete at `a73b6a2ee265231712bb912784f5392c2a9aff3a`.
  Exact heartbeat, bounded retry/dead-letter recovery, and atomic two-way
  run/job reconciliation are proven without activating a worker.
- T110 is complete at `aabbb7bda0851b688620dcec3044502f58802a1f`.
  One non-deployed serial worker now has exact-run continuation, isolated
  heartbeats, bounded retry, graceful stop, and pinned Hermes generation-fence
  proof.
- T107 is the sole active task: activate durable admission and the proven worker
  with old-head cancellation, per-repository fairness, priority aging, operator
  queue controls, and concise deployment/readiness guidance.
- Public identity is “Review Agent.” `sundsvall-standard` remains a selectable
  municipal profile, not the product identity. Model-era review-depth ceilings
  are gone; pageable or honestly incomplete contracts own large inputs.

## T107 execution boundary

- Activate the authenticated internal Hermes API and worker only with real
  durable ingress and readiness. Reuse the concrete worker and PostgreSQL job
  owner; do not add Celery, ARQ, Redis, a scheduler, or a generic queue port.
- A duplicate request must acknowledge quickly and idempotently. A newer head
  must cancel queued work and make leased old-head work unable to publish.
- Start with one active review per repository and a bounded global queue. Own
  fairness and priority aging in the PostgreSQL claim query; tune concurrency
  only from measured queue age and provider capacity.
- Extend the existing operator CLI with queue inspection/retry/cancel behavior.
  Document worker variables, health/readiness, scaling, Docker Compose, Dokploy,
  and arbitrary-UID OpenShift without adding Helm or another deployment layer.
- Keep publication serialization and delivery where they are. T108 owns the
  outbox and must not be pulled into activation.

## Remaining order

T107 activation/supersession/fairness/fast enqueue → T108 publication outbox →
T109 final audit.
Security-scanner and Codex Security integrations remain explicitly deferred.

## Verification continuity

- Older exact-commit evidence remains on each completed task receipt in
  `state.yaml`; this handoff keeps only the job/worker sequence needed by T107.
- T105: PostgreSQL 116 tests, strict Pyright, 316-test bundle, nine-document
  manifest, and final Claude Opus/high green 8. Sessions
  `review-agent-t105-durable-jobs` (UUID
  `fbf41768-47f2-4631-8941-518111bd54f3`) and the re-sliced constraint gate
  `review-agent-t105-constraint-final` (UUID
  `1d4c3167-0193-4c9e-9632-e9a137aa0cfd`). Exact-commit run `32750859418`
  passed all required jobs.
- T106: PostgreSQL 123 tests, strict Pyright, 323-test bundle, public docs/site
  checks, and Claude Opus/high green 8. Session
  `review-agent-t106-worker-architecture` (UUID
  `3925a59c-148d-4367-a6ae-7e5f4c4ca436`). Source revision
  `a73b6a2ee265231712bb912784f5392c2a9aff3a` passed exact-commit CI.
- T110: PostgreSQL 125 tests, strict Pyright, 334-test bundle, fresh image and
  pinned Hermes adapter checks, public docs/site, and Claude Opus/high green 8.
  Session `review-agent-t110-worker-proof` (UUID
  `6d0f83ad-a2a9-424e-84c0-c6894ec986f7`). Exact-commit Python/image run
  `32763967594` and Pages run `32763967574` passed.
- Preserve user-owned `refactor-plan1.md`.
- Stop all Codex and Claude work by 23:50 Europe/Stockholm; resume at 06:00.
