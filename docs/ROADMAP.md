---
sidebar_label: Current and planned
slug: /roadmap
title: Current and planned capabilities
description: A clear boundary between the working reviewer and planned platform work.
status: target
last_verified: 2026-08-21
---

# Current and planned capabilities

> **Current and target** — “Available now” is current behavior. Every later
> section is an approved direction, not a deployed capability or release
> promise.

This roadmap describes direction, not release promises. Current behavior is the
behavior documented in the repository and verified by the shipped bundle.

## Available now

- One shared reviewer per environment with an exact repository allowlist.
- Trusted GitHub Actions request workflow and HMAC-signed webhooks.
- Full Python bundle CI for pull requests and changes to `main`.
- A forward-only initial PostgreSQL schema and real PostgreSQL CI contract.
- Bounded PR reads against an exact base/head snapshot.
- Two-pass evidence review with honest coverage reporting.
- SQLite review memory, stable finding references, and repeated review rounds.
- Deterministic GitHub summaries and validated optional native suggestions.
- Human-governed feedback plus private, operator-run learning and verification.
- Central typed runtime settings, a bounded GitHub read client, and typed run and
  finding application owners.
- One fixed Sundsvall profile bundle for reviewer identity and procedure.

## PostgreSQL replacement milestones

The repository owner confirmed on 2026-08-21 that the reviewer has no production
deployment or persisted production review state. Its packaged SQLite runtime is
temporary and disposable. The approved target—not a deployed capability—is one
PostgreSQL database per environment. No per-repository databases will be
introduced, and there will be no permanent dual writes. The initial PostgreSQL
schema and real PostgreSQL integration contract are implemented and run in CI,
but PostgreSQL is not deployed: the active reviewer still uses SQLite.

The remaining milestones are:

1. Add the migration runner and runtime owner, including provider repository ID
   acquisition and repository-scoped finding identity, without changing review
   behavior.
2. Define the initial PostgreSQL replacement recovery path before switching.
   Recovery after PostgreSQL writes uses the previous PostgreSQL-compatible
   application image against the same database.
3. Switch runtime configuration and Compose to PostgreSQL and prove a controlled
   review against a fresh database.
4. Delete the SQLite application persistence, migrations, volume, environment
   settings, and SQLite-only tests after the cutover passes.
5. Add durable jobs and an outbox as separate work after PostgreSQL owns the
   application state.

This clean runtime replacement preserves observable review behavior without
preserving SQLite IDs, schema versions, local test data, or a compatibility
backend. Hermes `HERMES_HOME` state is separate profile-local runtime state and
is not part of this application persistence replacement.

## Planned platform capabilities

- durable jobs, retries, leases, and an outbox for external delivery;
- GitHub App installation tokens and webhook-based repository lifecycle;
- trusted base-branch `.github/review-agent.yaml` and `AGENTS.md` context;
- operator-facing repository and policy management;
- broader notification and collaboration integrations after the core is stable.

## Explicitly deferred

Publication ownership changes, trusted project context and policy overlays,
GitHub App migration, new feedback work, Codex Security, scanner workers, SARIF
aggregation, and security artifact storage are deferred. Existing security
controls stay in place, and deterministic scanners should continue to run
independently in repository CI.
