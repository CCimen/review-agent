# Read-only PostgreSQL runtime foundation

TL;DR: The reviewer now has a typed, bounded PostgreSQL runtime owner that opens
explicitly, proves connection and migration readiness, exposes current pool
capacity, and fails closed. It performs no application read or write, and the
active reviewer still uses SQLite.

## Outcome

- Deepened the typed settings owner with a required PostgreSQL URL contract.
  Deployment URLs must name an explicit TCP host and database; implicit libpq
  environment defaults and Unix-socket URLs are deliberately rejected.
- Added one concrete runtime module with a 1–4 connection pool, at most eight
  waiting callers, explicit one-shot open/close lifecycle, and no import-time
  connection.
- Kept pooled connections idle in autocommit mode so each future operation owner
  must create its own short transaction.
- Fixed UTC, reviewer application name, statement, lock, and idle-transaction
  safeguards on every pooled connection and verify them during readiness.
- Added read-only migration inspection under the migration advisory lock.
  Startup rejects unavailable, pending, drifting, or concurrently migrating
  databases while accepting a contiguous database-ahead suffix for the previous
  PostgreSQL-compatible image.
- Exposed only the pool gauges with a current consumer: open state, bounds,
  current size, availability, and waiting requests. Speculative cumulative
  metric names were deleted before commit.

## Deliberately not changed

No registry, pull-request, subject, run, or coverage operation was added. There
is no pool consumer, startup retry policy, CLI, Compose cutover, SQLite edit,
dual write, fallback, importer, ORM, backend interface, retry framework, job,
lease, outbox, or publication change.

The published `postgres_migrations` package was not mechanically moved into the
new `postgres` operations package. They have distinct canonical ownership and no
duplicate logic; moving T021 during this behavior slice would weaken its receipt
and reviewability.

## Evidence

- Implementation revision: `0dafdf636e270f1f2c3b6e5d696ce99e9837a37d`.
- 30 PostgreSQL 17 tests passed, including network-free construction,
  unavailable startup, exact session invariants, pool bounds, pending and drift
  failure, advisory-lock contention, and database-ahead recovery.
- Strict Pyright and the 526-test bundle passed; 28 PostgreSQL container tests
  were skipped in the non-container portion as intended.
- The final runtime image built and imported Psycopg 3.3.4, psycopg-pool 3.3.1,
  and `PostgreSQLRuntime` from the shipped bootstrap tree.
- Public documentation checks, Docusaurus typecheck/build, and all nine built
  routes passed.
- Claude Opus/high session `review-agent-t022-runtime-foundation` scored 6 on
  the first pass, identified two verified blockers, and finished green at score
  8 on the second pass with no blockers.
- Live Python bundle run `32517163480` and Publish documentation run
  `32517163559` completed successfully.
- <https://ccimen.github.io/review-agent/docs/roadmap> publishes the read-only
  runtime foundation while continuing to state that SQLite is active.

## Recovery and next-owner note

A failed startup closes and spends its runtime object; a caller must construct a
new runtime before another open attempt. No database object is changed by
readiness.

`PostgreSQLNotReady` currently covers both conditions that may clear (pending or
concurrent migration) and fatal image/database mismatches (checksum drift or
session-invariant failure). T022 has no retrying consumer, so it deliberately
does not invent that policy. The first pool consumer must add a typed distinction
before retrying and must not branch on exception message text.
