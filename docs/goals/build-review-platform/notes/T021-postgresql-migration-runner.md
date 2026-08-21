# Authoritative PostgreSQL migration runner

TL;DR: PostgreSQL schema changes now have one runtime owner. The runner validates
exact migration bytes, serializes concurrent startup, applies pending SQL and
ledger rows atomically, and still allows the previous application image to start
when the database contains newer migrations it does not know.

## Outcome

- Added one synchronous Psycopg runner for discovery, checksum validation,
  advisory locking, transaction ownership, SQL execution, and ledger writes.
- Fixed `READ COMMITTED` as a migration-protocol invariant before the advisory
  lock so a waiting runner sees the prior runner's committed ledger.
- Kept applied migration versions contiguous while accepting database-ahead
  versions for previous-image recovery. Every locally known name and checksum
  remains mandatory.
- Pinned Psycopg 3.3.4 in CI and the runtime image and kept the PostgreSQL test
  container on an ephemeral loopback-only port.
- Updated the public roadmap without claiming a PostgreSQL runtime cutover;
  SQLite remains the active application store.

## Deliberately not changed

No application persistence operation, connection pool, DSN setting, CLI,
Compose cutover, SQLite compatibility path, ORM, jobs, leases, outbox, or
publication refactor was added.

## Evidence

- Implementation revision: `5ef159baabe11df78e2ed854c12631eaef266b79`.
- 23 PostgreSQL 17 contract and migration-runner tests passed, including exact
  checksums, drift rejection, atomic rollback, database-ahead recovery, and two
  barrier-released Repeatable Read runners.
- Strict Pyright and the 518-test bundle passed; 23 PostgreSQL container tests
  were skipped in the non-container portion as intended.
- The pinned runtime image built successfully and imported Psycopg 3.3.4 while
  discovering the bundled migration.
- The resumable skeptical commit gate finished green at score 8 with no
  remaining findings after correcting recovery, isolation, and test-liveness
  defects.
- Live Python bundle run `32514751272` and Publish documentation run
  `32514751286` completed successfully.
- The live roadmap at <https://ccimen.github.io/review-agent/docs/roadmap>
  publishes checksum migration ownership.

## Recovery

A failed migration rolls back its entire transaction. After a successful newer
migration, recovery uses the previous PostgreSQL-compatible application image
against the same database; that image validates all migrations it knows and
accepts a contiguous newer ledger suffix.
