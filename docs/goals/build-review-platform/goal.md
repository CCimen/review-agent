# Build the Review Agent Platform

## Objective

Build a maintainable, reusable Review Agent platform, with
`sundsvall-standard` as the first shipped deployment profile for Sundsvalls
kommun. Start by publishing clear operator and developer documentation, then
continue through the maintainability-first architecture tranches without
weakening the proven review contract.

## Goal Kind

`open_ended`

## Current Tranche

Close the three pilot blockers verified against `v0.1.0-rc.2`: render the
managed model/profile contract from one explicit environment mapping, prevent
credentialed clients from following cross-origin redirects, and make GitHub
source/publication retry classification reflect the actual provider failure.
Keep the correction inside the existing configuration and transport owners;
later release, migration, module-splitting, failure-cleanup, and command phases
remain deferred.

## Approved Sequencing Input

- `/Users/ccimen/Downloads/sundsvall-review-agent-maintainability-first-plan.md`
- `/Users/ccimen/Downloads/sundsvall-general-review-agent-refactor-plan(2)(1).md`
- Local `refactor-plan1.md`, SHA-256
  `53349848017a9fead8cc7e0c4cf0abb69f66bb34e7b941fb7bedf8b0f4d810e0`

These plans are advisory architectural and sequencing input; each active
`state.yaml` task is self-contained. Direct repository-owner
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
- Keep the engine and public product identity organization-neutral. Ship
  `sundsvall-standard` as the initial profile rather than hard-coding Sundsvall
  into engine behavior or the product name.
- A deployment profile owns the Hermes `SOUL.md`, stable review rules, procedure,
  language and presentation choices, and explicitly enabled reviewed skills.
  Authorization, tool limits, exact-snapshot enforcement, durable state,
  deterministic publication, and security ceilings remain fixed engine/runtime
  invariants that a profile cannot weaken.
- Make profile customization a later bounded operator workflow using Hermes'
  native `HERMES_HOME` files and existing profile bundle. Do not build a generic
  plugin framework, template language, dynamic code loader, or administration
  UI without demonstrated need. Repository-specific context will come from the
  trusted base branch and `AGENTS.md`, never a PR-head `SOUL.md` or
  repository-name conditionals.
- PostgreSQL is the approved canonical store: one database per environment,
  never one database per repository. The owner confirmed on 2026-08-21 that the
  reviewer has no production deployment or persisted production review state,
  so do not build a SQLite importer, dual-backend mode, compatibility layer, or
  SQLite rollback path.
- Run the full Python bundle in CI before persistence work. Sequence the narrow
  persistence and transaction contract, migration invariants, PostgreSQL schema
  and runtime ownership, runtime/configuration/Compose switch, and deletion of
  SQLite code separately. Define initial replacement recovery before switching.
  At the first PostgreSQL write, recover by restoring and redeploying the same
  compatible revision; from the next PostgreSQL revision onward, rollback may
  use the previous compatible image against the same database. Jobs, leases,
  and the outbox follow later.
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
tranche and continue while the overall objective remains active. A completed
goal is reopened only when the repository owner approves and defines a new
tranche.

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
8. Audit the tranche, then activate the next safe maintainability task only
   while the goal remains active.
