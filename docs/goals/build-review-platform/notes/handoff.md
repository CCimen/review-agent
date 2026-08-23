# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits and pushes to `CCimen/review-agent` `main` are authorized.
- The corrected PostgreSQL contract, migration runner, runtime foundation,
  cohesive registry-to-review-run operations, normalized review coverage, and
  stable finding memory are live at `153132d`. The new operations are
  integration-only; no tool or runtime cutover has occurred.
- T025 is complete. T026 is active for best-effort PostgreSQL suggestions and
  context-matched human decisions. Move and reuse the existing pure suggestion
  validation, keep database transactions separate from trusted head-file reads,
  and preserve active SQLite observable behavior without dual writes.
- The SQLite runtime remains current until the controlled PostgreSQL cutover; public operator guidance must continue to say so.

## Execution boundary

- The primary agent implements; subagents are read-only.
- Keep validation proportional and use one skeptical peer gate per stable
  candidate.
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
- At cutover, the adapter must map the current changed-file classification into
  the typed `FileDomain` and `ReviewMode` values before pool checkout. Delete the
  SQLite `CoverageState`/`DiffState` literals, `FileSide` alias, and classification
  helpers when their final SQLite consumers are removed; do not keep parallel
  vocabularies after PostgreSQL becomes active.
- Coverage writes hold a shared run lock and may make supersession wait up to the
  existing two-second `lock_timeout`. The active runtime adapter must map an
  exhausted `LockNotAvailable` into its retry or user-facing busy contract; do
  not lengthen the transaction or hide the bounded contention.
- Finding writes hold the same pull-request lifecycle lock with
  `FOR NO KEY UPDATE`; lock timeout maps to typed `FindingRunBusy`. Canonical
  symbol and anchor values are case-folded because they are identity fields,
  not display copy. A conflicting same-run regenerated batch recovers through a
  new review run rather than mutating durable occurrence evidence.
- Publication module splitting, jobs, outbox, trusted project context and
  policy, scanners/Codex Security, and GitHub App work remain deferred.

## Continuity

- Read `goal.md`, `state.yaml`, `notes/handoff.md`, repository instructions, and
  the active task's exact source paths. Verify the recorded revision and live
  branch before editing.
- The repository owner requested Claude Opus/high for every future pre-commit
  peer gate and hard architecture question. Use one resumable session per
  stable slice and do not repeat it for unchanged or mechanical follow-ups.
- All Codex and Claude work must stop by 23:50 Europe/Stockholm and may resume
  at 06:00 Europe/Stockholm. Do not start a unit that risks crossing the stop
  boundary.
