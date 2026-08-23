# PostgreSQL finding memory receipt

## Outcome

PostgreSQL now has rename-stable, repository-scoped finding identities, atomic
batched occurrences, deterministic pull-request-local references, and bounded
same-pull-request repeat history behind the existing finding application owner.
This remains integration-only: the active reviewer still uses SQLite.

## Published implementation

- Revision: `153132da36ad2a590e4b0df0abec5acb12f77b8a`
- Parent: `16afd073fc77d003b7940d8c34dcb023e07ac846`
- Exact paths:
  - `bootstrap/plugins/review_agent_tools/domain/finding.py`
  - `bootstrap/plugins/review_agent_tools/postgres/findings.py`
  - `bootstrap/plugins/review_agent_tools/postgres_migrations/001_initial.sql`
  - `bootstrap/plugins/review_agent_tools/review_finding_application.py`
  - `scripts/check_postgres_schema.sh`
  - `tests/test_postgres_findings.py`
  - `docs/ROADMAP.md`

The domain module owns canonical fingerprints, admission, typed values, and
batch identity uniqueness. The PostgreSQL module owns short transaction-scoped
identity, occurrence, reference, lookup, and repeat-history operations. The
application seam validates the full batch and trusted context hashes before
pool checkout. No backend interface, ORM, per-row query loop, dual write,
fallback, or runtime cutover was added.

## Reliability evidence

- Case-only symbol or anchor drift resolves to one canonical identity.
- `last_seen_at` advances monotonically across same-repository pull requests.
- Pull-request lock contention returns typed `FindingRunBusy` after the bounded
  lock timeout; concurrent batches still allocate unique F-references.
- Exact retries are idempotent, conflicting retries roll back atomically, and
  inactive run, wrong head, unregistered path, and missing fingerprint failures
  are typed.
- The same-PR history query has a supporting
  `(pull_request_id, finding_id, observed_at DESC)` index before first deploy.

## Review and validation

- Claude Opus/high session `review-agent-t025-finding-memory`, canonical UUID
  `904bb84d-3abf-4853-91e6-a970a37dae6d`, moved from changes required at score
  5 in iteration 2 to green at score 8 in iteration 3.
- 57 PostgreSQL 17 schema, migration, lifecycle, concurrency, and finding tests
  passed after the fixes.
- Strict Pyright passed with zero diagnostics; 16 affected application and
  module tests passed.
- The canonical bundle passed 552 tests with 49 expected no-DSN skips before
  the final shared duplicate-validator extraction; the affected PostgreSQL
  suite and Pyright passed again afterward.
- Documentation contract, clean Docusaurus type-check/build, and all nine built
  routes passed.
- Live Python bundle run `32665661348` and Publish documentation run
  `32665661300` succeeded. The hosted roadmap was verified after deployment.

## Deliberate non-goals and follow-up

Suggestions, decisions, suppression, publication, verification, coaching,
tools, deployment, settings, and the active SQLite path did not change. The
next slice owns optional suggestion persistence and context-matched human
decisions. At cutover, document that a conflicting same-run regenerated batch
must recover through a new review run rather than mutating durable evidence.
