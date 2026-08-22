# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits and pushes to `CCimen/review-agent` `main` are authorized.
- `state.yaml` is authoritative for the active task and current revision. Do not infer active work from this handoff when they disagree.
- The corrected PostgreSQL contract, migration runner, runtime foundation, and
  cohesive registry-to-review-run operations are live at `e2af211`. The new
  operations are integration-only; no tool or runtime cutover has occurred.
- T023 is complete. T024 is activated for the next work window but no T024
  implementation has begun. It owns normalized changed-file inventory and
  content-read coverage without porting the SQLite JSON algorithm.
- The SQLite runtime remains current until the controlled PostgreSQL cutover; public operator guidance must continue to say so.

## Execution boundary

- The primary agent implements; subagents are read-only.
- Keep the process lean: proportional behavior-first and full validation, one skeptical peer gate at a stable candidate, one implementation commit, and one compact receipt/audit update where practical.
- PostgreSQL is the approved clean replacement: one database per environment.
  The owner confirmed no production deployment or persisted production review
  state. `goal.md` owns the no-legacy and recovery constraints; `docs/ROADMAP.md`
  owns the public sequence.
- Keep jobs, leases, and the outbox as later separate slices.
- The migration runner owns discovery and checksums, one PostgreSQL advisory
  lock, transaction control, ledger insertion, and database-ahead recovery.
- The runtime foundation owns the typed TCP DSN, explicit one-shot bounded pool,
  connection safeguards, readiness, migration health, and current pool gauges.
  It has no application operation or process startup consumer.
- Before a future consumer retries `PostgreSQLNotReady`, add a typed distinction
  between transient pending/concurrent migration and fatal drift/invariant
  failure. Do not branch on error-message text.
- The application owner now creates validated, versioned, and hashed review
  subjects before PostgreSQL checkout and composes the registry/run transaction.
- T024 must preserve registration versus inspection truth, range deduplication,
  concurrent inserts, and explicit incomplete inventory without holding a pool
  connection during GitHub reads.
- Publication module splitting, jobs, outbox, trusted project context and
  policy, scanners/Codex Security, and GitHub App work remain deferred.

## Continuity

- Read `goal.md`, `state.yaml`, `notes/handoff.md`, repository instructions, and
  the active task's exact source paths. Verify the recorded revision and live
  branch before editing.
- The repository owner requested Claude Opus/high for every future pre-commit
  peer gate and hard architecture question. Start one resumable T024 session
  only after its candidate is locally stable.
- All Codex and Claude work must stop by 00:10 Europe/Stockholm and may resume
  at 07:00 Europe/Stockholm. Do not start a unit that risks crossing the stop
  boundary.
