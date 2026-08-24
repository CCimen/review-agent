---
sidebar_label: Current and planned
slug: /roadmap
title: Current and planned capabilities
description: A clear boundary between the working reviewer and planned platform work.
status: target
last_verified: 2026-08-24
---

# Current and planned capabilities

> **Current and target** — “Available now” describes the shipped repository.
> Later sections describe approved direction, not release promises.

## Available now

- One shared reviewer per environment with an exact repository allowlist.
- Trusted GitHub Actions request workflow and HMAC-signed webhooks.
- Bounded PR reads against an exact base/head snapshot.
- Two-pass evidence review with explicit incomplete-coverage reporting.
- Deterministic GitHub summaries and validated optional native suggestions.
- Human-governed feedback plus private, operator-run learning and verification.
- One PostgreSQL database per environment for review runs, coverage, findings,
  decisions, publication, feedback, verification, reconciliation, and coaching.
- Checksum-verified PostgreSQL migrations, bounded role-specific connection
  pools, readiness checks, and PostgreSQL 17 integration tests.
- Direct PostgreSQL review, feedback, publication, stale-run recovery, and
  operator command paths. Network and model calls never hold database
  connections.
- Repository-scoped exports with an operator-selected per-table row budget.
- One validated deployment-profile selector with `sundsvall-standard` as the
  shipped municipal profile. Profiles own voice, stable rules, presentation,
  and an explicit reviewed-skill list; runtime security invariants remain in
  the engine.

PostgreSQL is the only application persistence backend. The project has no
backend selector, dual write, fallback, import bridge, or compatibility layer.
Hermes `HERMES_HOME` remains separate profile-local runtime state.

## Remaining reliability work

1. Add a durable PostgreSQL job lifecycle with leases, fencing, heartbeats,
   retries, dead-letter handling, supersession, and fair bounded concurrency.
2. Acknowledge review requests after durable enqueue so a gateway interruption
   cannot lose accepted work.
3. Persist publication intent and exact payloads atomically, then deliver them
   through a recoverable outbox worker without holding database connections
   during GitHub calls.

## Planned platform capabilities

- durable jobs, retries, leases, and an outbox for external delivery;
- GitHub App installation tokens and webhook-based repository lifecycle;
- trusted base-branch `.github/review-agent.yaml` and `AGENTS.md` context;
- operator-facing repository and policy management;
- broader notification and collaboration integrations after the core is stable.

## Explicitly deferred

Trusted project context and policy overlays, GitHub App migration, Codex
Security, scanner workers, SARIF aggregation, and security artifact storage are
deferred. Existing security controls stay in place, and deterministic scanners
should continue to run independently in repository CI.
