# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits and pushes to `CCimen/review-agent` `main` are authorized.
- The corrected PostgreSQL contract, migration runner, runtime foundation,
  cohesive registry-to-review-run operations, and normalized review coverage
  are live at `efcd7a2`. The new operations are integration-only; no tool or
  runtime cutover has occurred.
- T024 is complete. T025 is active for rename-stable finding identity, batched
  PostgreSQL occurrences and local references, and exact repeat-review context.
  It must preserve the current application admission policy without porting
  suggestions, decisions, publication, or active SQLite behavior.
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
- At cutover, the adapter must map the current changed-file classification into
  the typed `FileDomain` and `ReviewMode` values before pool checkout. Delete the
  SQLite `CoverageState`/`DiffState` literals, `FileSide` alias, and classification
  helpers when their final SQLite consumers are removed; do not keep parallel
  vocabularies after PostgreSQL becomes active.
- Coverage writes hold a shared run lock and may make supersession wait up to the
  existing two-second `lock_timeout`. The active runtime adapter must map an
  exhausted `LockNotAvailable` into its retry or user-facing busy contract; do
  not lengthen the transaction or hide the bounded contention.
- Publication module splitting, jobs, outbox, trusted project context and
  policy, scanners/Codex Security, and GitHub App work remain deferred.

## Continuity

- Read `goal.md`, `state.yaml`, `notes/handoff.md`, repository instructions, and
  the active task's exact source paths. Verify the recorded revision and live
  branch before editing.
- The repository owner requested Claude Opus/high for every future pre-commit
  peer gate and hard architecture question. Use one resumable session per
  stable slice and do not repeat it for unchanged or mechanical follow-ups.
- All Codex and Claude work must stop by 00:10 Europe/Stockholm and may resume
  at 07:00 Europe/Stockholm. Do not start a unit that risks crossing the stop
  boundary.
