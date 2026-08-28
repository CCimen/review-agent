# Multi-repository pilot receipt

## Result

The deployed App-only candidate processed three selected repositories through
one PostgreSQL-backed deployment without mixing authorization, review state, or
publication state.

## Live evidence

- `doctor` reported one active App installation, three enabled repositories,
  healthy private services, and a drained queue.
- Dry-run smoke tests pinned and authorized `CCimen/eneo-testcicd` PR 6 and
  `CCimen/docs-test` PR 1 before any model call or GitHub write.
- Three simultaneous `/review` commands produced `active=3`, `leased=3`, and
  `queued=0`; the other repositories completed while the slower review remained
  active.
- `CCimen/eneo-testcicd` found the seeded path escape and retained the same F1
  identity on re-review.
- `CCimen/docs-test` first produced a clean result, then ignored an untrusted
  repository `SOUL.md` and found the seeded shell injection on the changed head.
- `CCimen/review-agent` remained clean on its corrected validation head.
- Every current publication posted once on its exact repository and head. Prior
  publications were retained and marked superseded.
- A controlled scope-feedback command received `+1`; the quality report stored
  one scope signal with zero suppressions and explicit denominators.
- Final queues had no active, queued, leased, pending, posting, or expired work.

## Direct correction

Live preflight exposed that terminal dead-letter history permanently failed
`doctor` and dry-run smoke tests. Readiness now keeps that history visible while
blocking only capacity exhaustion and recoverable expired work. The deployed
final check is ready with one historical dead-letter record.

## Evidence boundary

This proves selected-repository isolation, three-way concurrency, exact-head
reviewing, deterministic publication, feedback storage, and deployment-owned
SOUL behavior. It does not prove sustained throughput for 100 repositories,
arm64 runtime support, recovery under injected crashes, or an alternate model
provider. Those claims require their own measured workload.
