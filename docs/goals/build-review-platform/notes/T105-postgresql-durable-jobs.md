# PostgreSQL durable job foundation

## Result

T105 shipped at `4042ddf0d03fec8d538241775caeb0e3aead12bc`. Each durable
job is a one-to-one extension of an existing review run. `review_runs` remains
the sole owner of request keys, pull-request scope, and exact review-subject
identity; `review_jobs` owns only queue state, attempts, timing, and lease
fencing.

## Behavior

- Composing `review_runs.start_run` and `jobs.enqueue_run` in one short
  transaction makes duplicate acceptance idempotent without a second request
  identity.
- Enqueue uses the existing pull-request serialization row, rechecks run status
  after acquiring it, supersedes older queued work with one set-based update,
  and translates bounded lock contention to `ReviewJobBusy`.
- Claim uses PostgreSQL `FOR UPDATE SKIP LOCKED`, requires the owning run to
  remain active, and atomically increments attempt count and lease generation.
- Requeue-compatible constraints preserve prior generation and start evidence;
  a later head can supersede requeued work without resetting the fence. Leased
  old-head work remains fenced for T106-T107 cooperative recovery.
- The verifier source schema remains its own version-1 contract and is no longer
  coupled to the database migration number.

## Validation

- PostgreSQL 17: 116 tests passed, including concurrent acceptance and claims,
  lock timeouts, terminal-run exclusion, monotonic requeue fencing, queued and
  requeued supersession, migration application, and backup/restore.
- Strict Pyright and canonical bundle: 316 tests passed; replay and YAML checks
  passed.
- Public documentation manifest: nine documents passed. No current public
  behavior changed, so the GitHub Pages content remains accurate without a copy
  update.
- Claude session `review-agent-t105-durable-jobs` verified the ownership rewrite
  and cleared its prior findings. After its convergence limit, the remaining
  two-clause constraint correction was re-sliced into
  `review-agent-t105-constraint-final`, which reached green at score 8.

## Deliberately deferred

The worker process, heartbeat, expiry reaper, retry and dead-letter transitions,
graceful shutdown, cancellation, fairness, fast request acknowledgement,
operator queue controls, and publication outbox remain T106-T108. No queue
framework, broker, generic port, SQLite compatibility, or public runtime wiring
was added.
