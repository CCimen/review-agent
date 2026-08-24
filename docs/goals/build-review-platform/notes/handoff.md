# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T028 is live at `682736e7c43ce222c3fccbb4da997e1966b6df8b`.
  Publication partitioning, lifecycle orchestration, environment composition,
  and GitHub delivery now have separate owners with byte-equivalent behavior.
- T029 is active for refactor-plan1.md Phase 3 Slice 3B: persist one immutable
  exact publication plan, parts, and finding outcomes in PostgreSQL, then prove
  short claim/ack transactions and crash-safe delivery recovery. The active
  reviewer remains on SQLite; no runtime switch occurs in T029.

## Execution boundary

- Reuse the pure renderer and partition owner from T028. Add typed PostgreSQL
  publication operations around cohesive preparation, claim, acknowledge,
  failure, stale, and recovery transitions; do not create a generic store or
  backend switch.
- Preparation must atomically persist the exact rendered body/blocks/hash,
  relational current/closed/not-checked outcomes, and exact structured part
  payloads/hashes. A PostgreSQL connection must never remain held during GitHub
  calls.
- Direct external-ID recovery is primary; marker recovery handles a lost
  acknowledgement. Prove a process death after GitHub success cannot produce a
  duplicate delivery.
- Keep feedback, settings, Compose, deployment, runtime cutover, SQLite deletion,
  jobs, and outbox out of T029.
- Ponytail lite remains active: deepen the publication owners already created;
  do not add a provider framework, generic repository, or speculative policy
  surface.

## Continuity

- T028 evidence: 141 affected tests, strict Pyright, the 569-test full bundle,
  parent/candidate byte-equivalence digest `2fc1c734131260060c4fbd8118c709c2885039002bd47b9dce09380c6a1d17a4`,
  live Python run `32705578215`, and docs run `32705578272` passed.
- Claude session `review-agent-t028-publication-ownership`, UUID
  `3fe3d43d-80be-41a4-9064-8b264e561757`, converged from score 7 to green at
  score 8. Start one new resumable Opus/high session for T029's stable gate.
- T029 must decide whether partition failures get a publication-domain error
  instead of the GitHub transport error, and reassess failure-status ownership
  plus duplicate stale guards after PostgreSQL lifecycle ownership is concrete.
  Keep the historical literal `\\n` correction as a separate visible-output
  change. Keep GitHub read-client consolidation separate.
- Carry T027's runtime decision: reconciliation lock contention can surface as
  fail-closed `LockNotAvailable` under the two-second pool timeout. The later
  runtime caller must own fail-run versus bounded retry.
- Product direction: public engine/product naming becomes generic “Review
  Agent”; `sundsvall-standard` remains the first shipped municipal profile. A
  later bounded task owns simple Hermes-native profile selection/customization
  for `SOUL.md`, language, presentation, rules, and reviewed skills. Fixed
  authorization, tool, snapshot, persistence, publication, and security
  invariants remain non-configurable. Do not build a generic plugin framework.
- Preserve user-owned `refactor-plan1.md`. Stop all Codex and Claude work by
  23:50 Europe/Stockholm; resume at 06:00.
