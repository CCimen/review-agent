# Atomic job lifecycle and recovery

## Result

T106 shipped at `a73b6a2ee265231712bb912784f5392c2a9aff3a`. PostgreSQL now
owns an exact, bounded execution lifecycle for each one-to-one review job while
the review run remains the canonical outcome owner.

## Behavior

- Heartbeat and failure transitions require the exact lease owner and generation.
- Every claim consumes attempt budget; retry exhaustion becomes a dead-letter
  outcome and releases the active review run.
- Expired leases are recovered in bounded, set-based batches using run-before-job
  lock order and an expiry recheck.
- Run completion, failure, and supersession reconcile a queued or leased job in
  the same transaction. A terminal run cannot strand executable work, and a
  terminal job cannot leave an active run holding the per-pull-request slot.
- Cross-table composition lives in `review_run_application.py`; the table modules
  retain their single-table ownership and do not import each other.

## Validation

- PostgreSQL 17 lifecycle, migration, backup, and restore suite: 123 tests passed.
- Strict Pyright and canonical bundle: 323 tests passed.
- Public docs contract, Node 24 typecheck, and production Docusaurus build passed.
- Claude session `review-agent-t106-worker-architecture` completed its stable
  commit gate at green, score 8, with no blockers.

## Next boundary

T110 must prove exact-run worker continuation and process-death safety before any
worker or internal API surface is activated. Its first implementation step must
also close the reviewed enqueue locking and stale-fence contract gaps that become
reachable when a worker is introduced.
