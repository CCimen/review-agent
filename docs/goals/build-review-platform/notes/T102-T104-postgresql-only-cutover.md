# PostgreSQL-only Review Agent cutover

## Result

T102, T103, and T104 shipped together at
`4474d71fce7c970c4f9577931e8f926fe2d51219` because the product has not entered
production and has no SQLite application data to preserve. PostgreSQL is now the
only Review Agent application store. There is no backend selector, importer,
fallback, dual write, or SQLite rollback path; Hermes-owned profile/session
state remains separate.

## Ownership and deletion

- `REVIEW_AGENT_DATABASE_URL` and the bounded PostgreSQL runtime own every live
  review, publication, feedback, reporting, verification, coaching, and operator
  connection.
- Compose owns an internal PostgreSQL service, profile installation, authoritative
  migration, readiness, and dependent service startup.
- The existing application modules continue to separate provider I/O from short
  database transactions.
- Fifteen SQLite persistence/publication modules and eighteen implementation-detail
  test modules were deleted. Pure validation, rendering, identity, suggestion,
  authorization, replay, and deterministic publication contracts remain.

## Recovery and validation

- Real PostgreSQL 17 contract: 104 tests passed, including concurrent lifecycle,
  publication process-death recovery, suggestion-before-summary failure behavior,
  feedback, reporting, and operator paths.
- Backup/restore: `pg_dump` and `pg_restore` into a fresh instance preserved the
  migration ledger and a known `recovery/probe` application row; migration and
  readiness then passed against the restore.
- Bundle: strict Pyright and 304 tests passed; replay and YAML contracts passed.
- Documentation: nine-document public contract, Compose interpolation, and a clean
  Node 24 Docusaurus production build passed.
- Claude session `review-agent-t102-postgresql-cutover` reached green at score 8
  after verifying the bounded render-correction edge, typed publication-part IDs,
  restored tool-boundary coverage, and PostgreSQL-only runbooks.

## Deliberately deferred

Durable jobs, worker lifecycle/reaping, fairness and fast enqueue, and the
publication outbox remain T105-T108. Scanner and Codex Security integrations
remain deferred.
