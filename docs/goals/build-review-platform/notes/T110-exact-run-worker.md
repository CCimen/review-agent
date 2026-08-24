# Exact-run worker proof

## Result

T110 shipped at `aabbb7bda0851b688620dcec3044502f58802a1f`. The image now
contains one concrete serial worker that continues an accepted PostgreSQL review
run through the pinned Hermes chat boundary. The internal API and worker remain
inactive until T107 introduces durable admission and operator controls.

## Behavior

- One process claims and executes one review at a time. Horizontal capacity
  comes from additional processes rather than implicit in-process concurrency.
- One heartbeat thread renews the exact owner and lease generation without
  holding a database connection during the model turn.
- A reclaimed generation receives a new trusted Hermes session identity. Every
  mutable tool entry verifies that exact live job, run, and generation; existing
  operation and publication transactions retain their own atomic guards.
- Transient PostgreSQL and Hermes failures use bounded retry behavior. Lease
  loss wins over failure handling, and graceful stop prevents another claim.
- The worker uses the installed review skill body while Hermes continues to load
  SOUL.md and AGENTS.md natively. A pinned-image CI contract fails if the session
  or context adapter chain changes.

## Validation

- PostgreSQL 17 lifecycle, migration, backup, and restore suite: 125 tests passed.
- Strict Pyright and canonical bundle: 334 tests passed.
- Fresh image build, worker entrypoint, and pinned Hermes adapter contract passed.
- Public docs contract, Node 24 typecheck, and production Docusaurus build passed.
- Claude session `review-agent-t110-worker-proof`, UUID
  `6d0f83ad-a2a9-424e-84c0-c6894ec986f7`, converged from scores 5 and 7 to green
  at score 8.
- Exact-commit GitHub runs `32763967594` and `32763967574` passed, including the
  image smoke and Pages deployment.

## Next boundary

T107 activates durable request admission, old-head cancellation, per-repository
fairness, priority aging, operator queue controls, and the worker/API surface as
one deployment change. It also owns the worker environment, readiness, scaling,
and concise Compose, Dokploy, and OpenShift guidance. T108 remains the sole owner
of a recoverable publication outbox.
