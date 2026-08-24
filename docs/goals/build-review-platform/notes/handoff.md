# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T027 is live at `72f599c1292ecbf7362795adb19d7d994adcc7f5`.
  PostgreSQL verification, reconciliation, and coaching remain integration-only;
  the active reviewer still uses SQLite until the controlled cutover.
- T028 is active for refactor-plan1.md Phase 3 Slice 3A: split the existing
  publication god-module into concrete partition, application, and GitHub owners
  while preserving byte-equivalent output and the active lifecycle.

## Execution boundary

- Move existing behavior before adding behavior. Keep `review_renderer.py` pure,
  put deterministic partitioning in `publication_partition.py`, GitHub delivery
  in `github/publication.py`, and lifecycle orchestration in
  `review_publication_application.py`.
- Preserve markers, suggestions, retry classification, stored-ID-first recovery,
  supersession, failure status, public tool JSON, and SQLite persistence behavior.
- Do not start PostgreSQL publication writes, feedback, settings, Compose,
  deployment, runtime cutover, SQLite deletion, jobs, or outbox in T028.
- Ponytail lite remains active: prefer moves, reuse, and deletion of duplicate
  ownership; do not add a generic port, gateway framework, or pass-through layer.

## Continuity

- T027 evidence: 69 PostgreSQL tests, 15 affected SQLite behavior tests, strict
  Pyright, 567 bundle tests, docs checks/build, live Python run `32701966716`,
  and docs run `32701966733` passed. The hosted roadmap is current.
- Claude session `review-agent-t027-verification-coaching`, UUID
  `532f03ef-507d-4fe5-b2a3-b7876f103b0e`, converged from scores 6 and 7 to
  green at score 8 in three passes.
- Carry to the later PostgreSQL runtime caller: reconciliation/publication lock
  contention can surface as fail-closed `LockNotAvailable` under the pool's
  two-second timeout. That caller must explicitly own fail-run versus bounded
  retry and pin the decision at its application boundary.
- Read `goal.md`, `state.yaml`, this handoff, refactor-plan1.md Phase 3, and the
  T028 source paths before editing. Preserve user-owned `refactor-plan1.md`.
- Start one new resumable Claude Opus/high session for T028's stable commit gate.
  Stop all Codex and Claude work by 23:50 Europe/Stockholm; resume at 06:00.
