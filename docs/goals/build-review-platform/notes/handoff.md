# Goal Maker Handoff

`state.yaml` is authoritative. This note only orients the next continuation.

## Current state

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T101 is complete across `e9042e5a33f0ac57dfab82c286188aef6452c09d`
  and `a7de8fa03742d00babda8265ed65f881aac1ea01`. PostgreSQL owns
  historical supersession, failure-status state, explicit operator decisions,
  bounded repeatable-read reporting/export, stale recovery, publication and
  coverage inspection, verifier input, and coach receipts.
- T102 is the sole active task: atomically switch review, publication, feedback,
  operator, and Compose callers to PostgreSQL.
- Public identity is “Review Agent.” `sundsvall-standard` remains a selectable
  municipal profile, not the product identity. Model-era review-depth ceilings
  are gone; pageable or honestly incomplete contracts own large inputs.

## T102 execution boundary

- Use the existing concrete PostgreSQL runtime and operation modules. Replace
  `REVIEW_AGENT_DB`, the SQLite initializer, and the SQLite application volume
  with `REVIEW_AGENT_DATABASE_URL`, the authoritative migration service,
  readiness, and explicit bounded-pool startup/shutdown ownership.
- Keep Hermes `HERMES_HOME` state separate and unchanged.
- Preserve exact tool responses, snapshot protection, finding references,
  publication bytes, feedback authorization, failure status, supersession,
  explicit decision targeting, and bounded exports.
- When enabling export version 16, reject `complete: false` in the same change;
  accepting version 16 alone would make partial exports look complete.
- No backend selector, SQLite fallback, importer, dual write, compatibility
  schema, or rollback window. The product has no production SQLite data.
- Never hold a PostgreSQL connection across GitHub or model calls. Preserve
  short feedback transactions and publication lock ordering; recover explicitly
  if a lock timeout follows an external GitHub post.
- Update current-state operations/security/public docs and deploy Pages from the
  same reviewed revision.

## Remaining order

T102 atomic cutover → T103 controlled recovery proof → T104 SQLite deletion →
T105 durable queue schema → T106 worker lifecycle/recovery → T107
supersession/fairness/fast enqueue → T108 publication outbox → T109 final audit.
Security-scanner and Codex Security integrations remain explicitly deferred.

## Verification continuity

- T100: PostgreSQL 91 tests, strict Pyright, 606-test bundle, docs/site, and
  Claude Opus/high green 8. Session `review-agent-t100-postgresql-feedback`.
- T101: PostgreSQL 101 tests, strict Pyright, 616-test bundle, 24 docs contracts,
  and two Claude Opus/high gates green at 8. Sessions
  `review-agent-t101-publication-parity` and
  `review-agent-t101-operator-parity` (UUID
  `e3825c18-a74f-44f0-b9f0-ba335ca4a71e`). Exact-commit Python run
  `32733756788` and Pages run `32733756671` passed.
- Preserve user-owned `refactor-plan1.md`.
- Stop all Codex and Claude work by 23:50 Europe/Stockholm; resume at 06:00.

