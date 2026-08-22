# PostgreSQL review lifecycle operations

TL;DR: The application owner can now create a repository, pull request, exact
review subject, and review run in one short PostgreSQL transaction. Request
retries are idempotent, concurrent starts retain one active run, and lifecycle
transitions fail explicitly. The active reviewer still uses SQLite; no runtime
cutover or dual write was introduced.

## Outcome

- Added typed immutable review-subject values with recursive JSON validation,
  canonical serialization, schema versioning, and an exact SHA-256 digest.
- Added operation-level PostgreSQL registry writes keyed by stable provider
  repository identity. Repository renames preserve pull requests and history,
  while name collisions fail explicitly without damaging the outer transaction.
- Added one PostgreSQL review-run owner for trigger-key idempotency,
  single-active-run serialization, exact-subject supersession, monotonic phase
  changes, and completed, failed, or superseded terminal states.
- Added one application composition function that resolves inputs before pool
  checkout and commits repository, pull request, subject, and run together.
- Made run lifecycle timestamps database-owned at statement execution time, so
  an older overlapping transaction can safely supersede a newer committed run.
- Preserved workload failures such as deadlocks, query cancellation, and lock
  timeout instead of relabelling them as database unavailability.

## Deliberately not changed

No tool, process startup, settings, Compose, SQLite, coverage, finding,
publication, feedback, job, outbox, or deployment path changed. The provider
repository ID remains a pre-acquired input; trusted `base.repo.id` acquisition
and the backend switch belong to the controlled runtime cutover.

## Evidence

- Implementation revision: `e2af211a7b70f86dca2bf29ab705982dc4ec99f6`.
- 41 PostgreSQL 17 tests passed, including stable rename identity,
  repository-scoped pull requests, immutable-subject corruption detection,
  whole-transaction rollback, request-key idempotency, bounded concurrent
  starts, overlapping-transaction supersession, and invalid transitions.
- Strict Pyright and all 537 bundle tests passed; the non-container bundle had
  37 expected PostgreSQL-without-DSN skips.
- Documentation typecheck, production build, and all nine built routes passed.
- Claude Opus/high session `review-agent-t023-lifecycle` found and cleared one
  transaction-start timestamp blocker, then finished the implementation gate
  green at score 8 on iteration 3. Its two non-blocking refinements—statement-
  scoped timestamps and lock-timeout pass-through—were applied before the
  implementation commit and verified in the green iteration 4 receipt gate.
- Live Python bundle run `32566104359` and Publish documentation run
  `32566104351` completed successfully.
- <https://ccimen.github.io/review-agent/docs/roadmap> publishes the cohesive
  repository-to-review-run milestone and still states that SQLite is active.

## Recovery and next-owner note

The operations are not connected to the active reviewer, so runtime recovery is
unchanged. A failed operation rolls back its complete PostgreSQL transaction;
there is no SQLite fallback or partial run to repair.

The next owner is normalized changed-file and content-read coverage. Registration
must remain distinguishable from actual diff or source inspection, duplicate
ranges must deduplicate, concurrent inserts must not lose coverage, and an
incomplete file inventory must never claim complete review coverage.
