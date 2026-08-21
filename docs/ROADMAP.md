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
- Bounded PR reads against an exact base/head snapshot.
- Two-pass evidence review with honest coverage reporting.
- SQLite review memory, stable finding references, and repeated review rounds.
- Deterministic GitHub summaries and validated optional native suggestions.
- Human-governed feedback plus private, operator-run learning and verification.
- Central typed runtime settings, a bounded GitHub read client, and typed run and
  finding application owners.
- One fixed Sundsvall profile bundle for reviewer identity and procedure.

## PostgreSQL migration sequence

The repository owner confirmed on 2026-08-21 that the reviewer has no production
deployment or persisted production review state. Its packaged SQLite runtime is
temporary and disposable. The approved target—not a deployed capability—is one
PostgreSQL database per environment. No per-repository databases will be
introduced, and there will be no permanent dual writes.

1. Define one PostgreSQL schema and transaction boundary plus the invariants
   that behavior and integration tests must preserve.
2. Add the PostgreSQL runtime owner without changing review behavior.
3. Define and test initial replacement recovery before switching. After
   PostgreSQL accepts writes, rollback means redeploying the previous
   PostgreSQL-compatible application image against the same PostgreSQL database,
   never switching back to SQLite.
4. Switch runtime configuration and Compose to PostgreSQL, then delete the
   SQLite runtime, schema, migrations, and SQLite-only tests after the
   replacement passes. Jobs, leases, and an outbox follow as separate slices
   after the canonical store is established.

This clean runtime replacement preserves observable review behavior without
preserving SQLite IDs, schema versions, local test data, or a compatibility
backend.

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
