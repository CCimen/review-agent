# Goal Maker Handoff

`state.yaml` is authoritative. This note only orients the next continuation.

## Current state

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T102-T104 are complete at `4474d71fce7c970c4f9577931e8f926fe2d51219`.
  PostgreSQL is the only Review Agent application store; controlled
  backup/restore is proven and the SQLite implementation is deleted.
- T105 is the sole active task: add the minimal PostgreSQL durable-job schema,
  idempotent enqueue, and atomic lease-generation claim contract.
- Public identity is “Review Agent.” `sundsvall-standard` remains a selectable
  municipal profile, not the product identity. Model-era review-depth ceilings
  are gone; pageable or honestly incomplete contracts own large inputs.

## T105 execution boundary

- Add only the durable job fields required for idempotent enqueue, atomic claim,
  lease generation, and queued old-head supersession.
- Reuse PostgreSQL row locking and existing repository, pull-request, and exact
  review-subject identities. Do not add a generic queue port or external broker.
- Keep worker execution, heartbeat/reaping, retry classification, graceful
  shutdown, fairness, and publication outbox behavior in T106-T108.
- Prove concurrent claim ownership and invalid transitions on real PostgreSQL;
  update public docs only where current behavior changes.

## Remaining order

T105 durable queue schema → T106 worker lifecycle/recovery → T107
supersession/fairness/fast enqueue → T108 publication outbox → T109 final audit.
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
  `71ee5913-d171-444c-a608-0df179d463b5`). Exact-commit CI and Pages checks are
  pending.
- Preserve user-owned `refactor-plan1.md`.
- Stop all Codex and Claude work by 23:50 Europe/Stockholm; resume at 06:00.
