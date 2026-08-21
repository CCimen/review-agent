# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits and pushes to `CCimen/review-agent` `main` are authorized.
- `state.yaml` is authoritative for the active task and current revision. Do not infer active work from this handoff when they disagree.
- The corrected PostgreSQL first-write contract and authoritative migration
  runner are live at `5ef159b`; there is still no PostgreSQL application writer
  or authoritative data. The active slice now adds the read-only runtime
  foundation: typed DSN, bounded pool lifecycle, readiness, metrics, and
  migration health.
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
- The current slice deepens the typed settings and migration owners and adds one
  concrete PostgreSQL runtime module. It must not add application persistence
  operations, configuration/Compose switching, an importer, a compatibility
  path, or SQLite rollback.
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
  single pre-commit peer gates; use it only after the candidate is locally
  stable.
