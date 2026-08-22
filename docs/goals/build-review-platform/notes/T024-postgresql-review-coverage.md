# PostgreSQL Review Coverage

## TL;DR

PostgreSQL now owns normalized changed-file inventory, diff observations, and
source-read ranges for the future reviewer. Coverage remains honest under
partial enumeration and concurrent writes. This is an integration-only
milestone: the active reviewer still uses SQLite, and no runtime or tool
cutover occurred.

## Outcome

- Added typed changed-file, diff-state, file-side, domain, and review-mode
  values in the review domain owner.
- Added one batched PostgreSQL inventory operation, normalized range writes,
  diff observations, and a fail-closed coverage summary.
- Kept source reads distinct from diff availability and supporting-context
  paths distinct from changed paths.
- Made exact inventory definitions idempotent, conflicting definitions atomic,
  complete diff observations non-regressing, and concurrent exact range writes
  deduplicate safely.
- Kept network access outside the persistence API and validated requests before
  a pool checkout.

## Deliberate non-goals

- No changes to tools, the active SQLite runtime, findings, publication,
  settings, Compose, or deployment.
- No JSON range store, dual-backend interface, fallback, importer, or generic
  repository abstraction.
- No GitHub loader or callback accepted by the PostgreSQL operation owner.

## Evidence

- Implementation revision: `efcd7a27b057715a7a538cd98fc45323c0c14d12`.
- Forty-seven real PostgreSQL 17 tests passed, including dishonest inventory
  rollback, source/diff separation, complete-state non-regression, concurrent
  range deduplication, and lifecycle lock ordering.
- Strict Pyright passed with no diagnostics; the canonical bundle passed all
  543 tests with 42 expected no-database skips.
- The clean documentation type-check, build, and all nine built-route checks
  passed.
- Claude Opus/high session `review-agent-t024-coverage` moved from changes
  required at score 5 to green at score 8 after typed ownership, failure-mode,
  and proportional behavior-test corrections.
- The same session passed this receipt commit gate green at score 8 on
  iteration 3 with no blocker.
- Live Python bundle run `32571316208` and Publish documentation run
  `32571316250` succeeded. The hosted roadmap states both the new PostgreSQL
  operation and that SQLite remains active.
- `refactor-plan1.md` remained user-owned and unchanged at SHA-256
  `53349848017a9fead8cc7e0c4cf0abb69f66bb34e7b941fb7bedf8b0f4d810e0`.

## Recovery

The new operations are not live runtime dependencies. Each operation uses a
short transaction and rolls back on conflict or invalid lifecycle state. There
is no SQLite fallback path to maintain or remove for this slice.

## Next owner

The next slice deepens the finding application owner with repository-scoped,
rename-stable fingerprints, batched PostgreSQL identities and occurrences,
pull-request-local references, and exact repeat-review history. Suggestions,
decisions, publication, and runtime cutover remain later work.
