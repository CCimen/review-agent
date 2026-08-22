# Build the Sundsvall Review Platform

## Objective

Build the maintainable, organization-wide Sundsvall Review Agent described by
the approved refactor plans. Start by publishing clear, attractive operator and
developer documentation, then continue through the maintainability-first
architecture tranches without weakening the proven review contract.

## Goal Kind

`open_ended`

## Current Tranche

Implement the first cohesive PostgreSQL repository, pull-request, immutable
subject, and review-run transaction behind the existing application owner.
The active SQLite runtime, coverage, configuration and Compose switching, and
SQLite deletion remain later implementation slices.

## Approved Sequencing Input

- `/Users/ccimen/Downloads/sundsvall-review-agent-maintainability-first-plan.md`
- `/Users/ccimen/Downloads/sundsvall-general-review-agent-refactor-plan(2)(1).md`

These plans are architectural and sequencing input. Direct repository-owner
decisions supersede suggestions in them. In particular, this new repository is
a clean break with no Eneo aliases or compatibility profile, and security
scanner/Codex Security integration is deferred until the owner resumes it.

## Non-Negotiable Constraints

- Keep one organization-wide platform per environment, not one deployment,
  database, or Hermes profile per repository.
- Preserve the locked-down live reviewer, exact base/head subjects, bounded
  reads, human-governed feedback, stale-head protection, honest coverage, and
  deterministic publication.
- Prioritize a maintainable modular monolith. Add a seam only for a real current
  variation or boundary; do not scaffold a directory tree for appearances.
- The platform owns one municipal `SOUL.md`. Repository-specific review context
  will come from trusted base-branch configuration and `AGENTS.md`, never from a
  PR-head `SOUL.md` or repository-name conditionals.
- PostgreSQL is the approved canonical store: one database per environment,
  never one database per repository. The owner confirmed on 2026-08-21 that the
  reviewer has no production deployment or persisted production review state,
  so do not build a SQLite importer, dual-backend mode, compatibility layer, or
  SQLite rollback path.
- Run the full Python bundle in CI before persistence work. Sequence the narrow
  persistence and transaction contract, migration invariants, PostgreSQL schema
  and runtime ownership, runtime/configuration/Compose switch, and deletion of
  SQLite code separately. Define initial replacement recovery before switching;
  after PostgreSQL writes, rollback uses the previous PostgreSQL-compatible
  application image against the same database. Jobs, leases, and the outbox
  follow later.
- Defer Codex Security, scanner workers, scanner aggregation, and security
  artifact storage. Do not weaken existing security controls.
- Use proportional behavior and contract tests. Do not add broad test matrices
  or tests that only preserve internal wiring.
- The primary agent implements. Subagents perform read-only research or review.
- Commit and push each verified, reviewable slice directly to
  `CCimen/review-agent` `main`, as authorized.
- Treat maintained Sundsvall frontend repositories as evidence and inspiration,
  not as universal policy or a blueprint. Do not mention private inspiration
  sources in product documentation.

## Stop Rule

Stop only when the current tranche audit passes, every safe local action is
blocked, or continuing requires credentials, destructive operations, owner
input, or product strategy the board cannot decide.

Do not stop after discovery or planning when a safe PM implementation task can
be activated. When a tranche completes, select the next maintainability-first
tranche and continue while the overall objective remains active.

## Canonical Board

Machine truth lives at:

`docs/goals/build-review-platform/state.yaml`

If this charter and `state.yaml` disagree, `state.yaml` wins for task status,
active task, receipts, verification freshness, and completion truth.

## Run Command

```text
/goal Follow docs/goals/build-review-platform/goal.md through the next safe verified implementation slice. Continue across tranches unless blocked.
```

## PM Loop

On every continuation:

1. Read this charter and `state.yaml`.
2. Read the two approved plans when their sequencing affects the active task.
3. Work only on the active board task.
4. Use read-only Scouts/Judges in parallel when their questions are independent.
5. Keep implementation with the PM and record a compact task receipt.
6. Run proportional validation and one skeptical peer review at a stable slice.
7. Commit and push the verified slice to `main`.
8. Audit the tranche, then activate the next safe maintainability task unless a
   stop condition applies.
