# PostgreSQL verification and coaching receipt

## Outcome

Provider-neutral verification, candidate reconciliation, and reviewer coaching
now have typed domain owners and explicit transaction-scoped PostgreSQL
operations. Verifier evidence is exact-run scoped, reconciliation freezes when
publication preparation starts, and every coach run retains its exact immutable
candidate evidence. The work remains integration-only; PostgreSQL is not
deployed and the active reviewer still uses SQLite until controlled cutover.

## Published implementation

- Revision: `72f599c1292ecbf7362795adb19d7d994adcc7f5`
- Parent: `22da0acf48dd9bd989f7cfed6965b29c767d5031`
- Domain owners:
  - `bootstrap/plugins/review_agent_tools/domain/verification.py`
  - `bootstrap/plugins/review_agent_tools/domain/coaching.py`
- PostgreSQL owners:
  - `bootstrap/plugins/review_agent_tools/postgres/verification.py`
  - `bootstrap/plugins/review_agent_tools/postgres/coaching.py`
- Existing adapters delegate pure validation:
  - `bootstrap/plugins/review_agent_tools/memory_verification.py`
  - `bootstrap/plugins/review_agent_tools/memory_coach.py`

No initial-schema change was needed. No provider call, analyzer framework,
generic repository, backend interface, dual write, fallback, importer,
application pass-through wrapper, publication caller, or runtime cutover was
added. The plugin installer already copies the complete managed plugin tree.

## Reliability evidence

- A verifier attempt accepts at most one verdict per exact occurrence. Existing
  composite constraints and typed operations reject missing or cross-run
  occurrence and verification identities.
- Reconciliation locks the review-run row `FOR UPDATE`. A publication insert's
  foreign key takes the conflicting key-share lock, so concurrent preparation
  serializes before the freeze check. A guarded two-connection test proves the
  wait and the final `ReconciliationFrozen` result.
- Reconciliation remains mutable only while the run is active and no publication
  exists; its timestamp records the latest pre-publication revision.
- A coach run and exact candidate batch insert in one transaction. Contradictory
  decision/candidate definitions and duplicate candidates fail before or during
  insertion, and the transaction leaves no partial run.
- Later coach runs may reuse a candidate key without overwriting earlier target,
  route, or evidence values.
- Verification validation uses the package's `StrEnum` convention, injected UTC
  timestamps, bounded text, exact SHA-256 IDs, finite confidence, and explicit
  failure/refutation coupling. Invalid values are rejected before persistence.

## Review and validation

- Claude Opus/high session `review-agent-t027-verification-coaching`, canonical
  UUID `532f03ef-507d-4fe5-b2a3-b7876f103b0e`, scored 6, then 7, then converged
  to green at score 8 in iteration 3.
- 69 PostgreSQL 17 contract tests passed. The affected SQLite verification and
  coaching suites passed 15 tests after adding compact negative validation
  coverage.
- Strict Pyright passed with zero diagnostics. The final canonical bundle passed
  567 tests with 60 expected no-DSN skips.
- Documentation checks, TypeScript type-check, isolated production build, live
  Python bundle run `32701966716`, and Publish documentation run `32701966733`
  passed.
- The hosted roadmap names exact publication payload delivery and feedback as the
  next PostgreSQL milestones and still states that PostgreSQL is not deployed.

## Deliberate non-goals and follow-up

Publication, feedback, tools, settings, Compose, deployment, cutover, SQLite
deletion, jobs, and outbox did not change. A real runtime caller does not exist
yet, so direct resolved values call explicit PostgreSQL operations rather than a
fake application wrapper.

When the later PostgreSQL publication caller is introduced, it must own the
failure contract for lock contention. The bounded pool can raise fail-closed
`LockNotAvailable` before an in-flight publication transaction completes; the
application boundary must deliberately choose fail-run or bounded retry and
behavior-test that choice.

The next slice moves surviving publication partitioning, orchestration, and
GitHub delivery behavior out of `review_publisher.py` without changing rendered
bytes or starting PostgreSQL publication persistence.
