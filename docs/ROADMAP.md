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
- A corrected first-write PostgreSQL schema, checksum-verifying migration
  runner, read-only PostgreSQL runtime foundation, and real PostgreSQL CI
  contract.
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
schema, migration runner, and read-only runtime foundation are implemented and
run in CI, but PostgreSQL is not deployed: the active reviewer still uses
SQLite. The foundation owns the typed database URL, explicitly opened bounded
pool, connection safeguards, readiness, migration health, and pool metrics; it
does not perform application reads or writes.

Four gates protect the first authoritative PostgreSQL write:

- **Stable finding identity:** fingerprints must use stable local finding fields,
  while every full or abbreviated lookup is scoped by the repository's internal
  ID. This remains part of the finding runtime slice.
- **Request idempotency:** the schema now requires one globally unique durable
  request key for each review command.
- **Checksum migration ownership:** one advisory-locked runner verifies exact
  migration checksums and owns the migration transaction and ledger insertion.
- **Publication provenance:** the schema now records every current, resolved,
  invalidated, suppressed, and not-checked outcome with its exact source
  occurrence and review run.

The remaining milestones are:

1. Add the bounded provider repository ID acquisition, pull-request, subject,
   run, and coverage operations. No network or model call may hold a database
   connection.
2. Port stable repository-scoped finding identity and governance, followed by
   exact publication payload delivery and feedback transactions.
3. Define the initial PostgreSQL replacement recovery path before switching.
   Recovery after PostgreSQL writes uses the previous PostgreSQL-compatible
   application image against the same database.
4. Switch runtime configuration and Compose to PostgreSQL and prove a controlled
   review against a fresh database.
5. Delete the SQLite application persistence, migrations, volume, environment
   settings, and SQLite-only tests after the cutover passes.
6. Add durable jobs and an outbox as separate work after PostgreSQL owns the
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
