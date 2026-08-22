# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits and pushes to `CCimen/review-agent` `main` are authorized.
- `state.yaml` is authoritative for the active task and current revision. Do not infer active work from this handoff when they disagree.
- The corrected PostgreSQL first-write contract, migration runner, and read-only
  runtime foundation are live at `0dafdf6`; there is still no PostgreSQL
  application writer or authoritative data.
- The repository owner resumed work on 2026-08-22. T023 is active and owns the
  first cohesive PostgreSQL registry, immutable-subject, and review-run
  transaction without a tool/runtime cutover.
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
- The later runtime-adapter slice must create the validated, versioned, and
  hashed `review_subjects.resolved_config` aggregate before its first subject
  insert; all three fields are deliberately required and have no default.
- Publication module splitting, jobs, outbox, trusted project context and
  policy, scanners/Codex Security, and GitHub App work remain deferred.

## Continuity

- Read `goal.md`, `state.yaml`, `notes/handoff.md`, repository instructions, and
  the active task's exact source paths. Verify the recorded revision and live
  branch before editing.
- The repository owner requested Claude Opus/high instead of Codex for future
  pre-commit peer gates. Resume `review-agent-t023-lifecycle` after the candidate
  is locally stable; do not start a duplicate session.
- All Codex and Claude work must stop by 00:10 Europe/Stockholm and may resume
  at 07:00 Europe/Stockholm. Do not start a unit that risks crossing the stop
  boundary.
