# Generalize the Pull-Request Review Platform

## Objective

Use the validated Eneo reviewer baseline to build a maintainable,
organization-wide review platform for Sundsvalls kommun without carrying Eneo
branding or compatibility identifiers into the new product, and without
weakening its security, snapshot, feedback, or deterministic-publication
guarantees.

## Goal Kind

`open_ended`

## Current Tranche

Discover the exact Eneo-specific coupling, remove it in bounded mechanical
slices beginning with the repository allowlist, verify each slice without
changing review behavior, and audit the result before beginning persistence or
deployment redesign.

## Non-Negotiable Constraints

- Keep the current Eneo deployment and source repository unchanged.
- Treat the new repository as a clean Sundsvall platform: remove Eneo branding,
  identifiers, and environment names instead of retaining compatibility aliases.
- The refactor plan is architectural input; direct owner decisions supersede its
  suggested transition compatibility where they differ.
- Preserve the locked-down live reviewer, exact base/head subjects, bounded read
  tools, human-governed feedback, stale-head protection, and deterministic
  publication path.
- Use one canonical owner per concept; reuse, move, merge, or delete before
  creating new modules or seams.
- Keep repository-specific behavior in versioned policy/profile configuration,
  never `if repository == ...` branches.
- Separate mechanical naming changes from behavior, schema, PostgreSQL, GitHub
  App, scanner, and Slack changes.
- Work directly on `CCimen/review-agent` `main` in small commits, as authorized by
  the repository owner.
- Use selective behavior and contract tests only. Reuse the existing replay and
  bundle checks; do not add tests for internal wiring or a speculative matrix.
- Do not introduce Redis, Kafka, Kubernetes, per-repository deployments,
  per-repository databases, or per-repository Hermes profiles without measured
  evidence and a later explicit decision.

## Current Architecture Direction

- One staging stack and one production stack.
- One PostgreSQL review-domain database per environment, introduced only after
  generic identity and domain ownership are stable.
- One organization GitHub App with short-lived installation tokens, introduced
  after the durable application model exists.
- Profiles represent agent identities and trust boundaries, not repositories.
- PostgreSQL jobs, leases, and an outbox are the initial scaling mechanism.
- Scanners and Slack remain later adapters with separate credentials and trust
  boundaries.

## Stop Rule

Stop when the tranche audit passes, all safe local work is blocked, or continuing
would require owner input, credentials, destructive operations, or product
strategy the board cannot decide.

Do not stop after planning, discovery, or Judge selection when a safe Worker task
can be activated.

## Canonical Board

Machine truth lives at:

`docs/goals/generalize-review-platform/state.yaml`

If this charter and `state.yaml` disagree, `state.yaml` wins for task status,
active task, receipts, verification freshness, and completion truth.

## Run Command

```text
/goal Follow docs/goals/generalize-review-platform/goal.md through the first safe verified implementation slice. Do not stop after planning unless blocked.
```

## PM Loop

On every `/goal` continuation:

1. Read this charter.
2. Read `state.yaml`.
3. Work only on the active board task.
4. Assign Scout, Judge, Worker, or PM according to the task.
5. Write a compact task receipt.
6. Update the board.
7. If Judge selects a safe Worker task with `allowed_files`, `verify`, and
   `stop_if`, activate it and continue unless blocked.
8. Finish only with a Judge or PM audit receipt that maps current receipts and
   verification back to this objective.
