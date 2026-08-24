# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T029 is live at `204c2626c305201414b42fc6a9658400f6236151`.
  PostgreSQL now owns immutable exact publication plans, finding outcomes, typed
  delivery parts, short claim/ack transactions, retryable partial delivery, and
  publication-scoped crash recovery. No live caller or backend switch exists.
- T090 is live at `e16163a0d5817a97fd9dd3927089492540294c14`.
  Public product/site identity is “Review Agent”; the strict installer selects a
  trusted profile whose SOUL, AGENTS, and reviewed skills cannot merge fixed
  model, tool, route, authorization, snapshot, persistence, marker, or lifecycle
  settings. `sundsvall-standard` remains the packaged municipal profile.
- T091 is live at `e35946cd2f932058f446c05c1fea26ef7356117e`.
  Model-era context and turn ceilings are gone; changed files, diffs, and source
  reads use pageable or honestly incomplete contracts. One native plugin setting
  owns complete diff/source page-result capacity, while GitHub, storage, request,
  publication, and security boundaries remain explicit.
- T999 completed with `not_complete`; its requirement-by-requirement evidence is
  in `notes/T999-full-platform-audit.md`. T100 is the sole active task and ports
  feedback event processing to one PostgreSQL transaction without switching the
  deployed runtime.

## Execution boundary

- Work only on T100. Reuse the existing PostgreSQL publication, finding, and
  decision owners, and add one concrete feedback owner for the missing
  transaction. Keep it unreachable from the live runtime until T101.
- The direct PostgreSQL decision supersedes SQLite migration or compatibility
  proposals: the product is not in production, so remaining SQLite runtime code
  must be treated as deletion/cutover work, not preserved legacy behavior.
- Keep deferred security-scanner/Codex Security integration out of completion
  while its explicit deferral remains active.
- The ordered remaining core path is T100 PostgreSQL feedback, T101 operational
  parity (historical supersession, failure-status comments, and operator CLI),
  T102 atomic runtime/Compose cutover, T103 controlled recovery proof, T104
  SQLite deletion, T105 durable queue schema, T106 worker lifecycle/recovery,
  T107 supersession/fairness/fast enqueue, T108 publication outbox, and T109
  final audit.

## Continuity

- T029 evidence: real PostgreSQL 83 tests, strict Pyright, 584-test full bundle,
  59 focused active-publication tests, docs contract, and site typecheck passed.
- Claude session `review-agent-t029-postgresql-publications`, UUID
  `92fe6e53-5c85-4375-a8b3-c01c5981f2c4`, converged from score 4 to green at
  score 8 in one correction and resume. Use one resumable Opus/high session at
  each later stable task gate; do not review intermediate edits.
- The runtime caller later owns retry/reap policy for interrupted publication.
  PostgreSQL historical supersession rendering remains required before cutover.
- T090 evidence: strict Pyright, 588-test bundle, 42 focused contracts, docs,
  site typecheck/build, and Claude Opus/high green at score 8. Session
  `review-agent-t090-generic-profile`, UUID
  `caa28055-7ac5-4923-808a-de58403cd5e8`.
- T091 now owns actual source/model capacity separately from fixed provider and
  security ceilings. Claude session `review-agent-t091-capacity-limits`, UUID
  `2a6408f6-5bf1-41da-8e24-bace5b012619`, reached green at score 8 after exact
  pagination, cache, source-envelope, fallback, and serializer-boundary fixes.
- T091 live workflows: Python bundle 32720374050 and documentation build/deploy
  32720374018 passed; Pages was deployed from the same revision.
- Preserve user-owned `refactor-plan1.md`. Stop all Codex and Claude work by
  23:50 Europe/Stockholm; resume at 06:00.
