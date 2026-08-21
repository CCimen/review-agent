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
- Bounded PR reads against an exact base/head snapshot.
- Two-pass evidence review with honest coverage reporting.
- SQLite review memory, stable finding references, and repeated review rounds.
- Deterministic GitHub summaries and validated optional native suggestions.
- Human-governed feedback plus private, operator-run learning and verification.

## Next maintainability work

The immediate work improves ownership inside the existing modular monolith
without changing review behavior:

1. centralize typed runtime settings;
2. consolidate concrete GitHub read ownership;
3. extract the review application workflow from transport and storage details;
4. separate publication planning from GitHub delivery;
5. define trusted, versioned policy resolution and base-branch project context.

SQLite remains in place until these application boundaries are stable.

## Planned platform capabilities

- PostgreSQL persistence and explicit transactional boundaries;
- durable jobs, retries, leases, and an outbox for external delivery;
- GitHub App installation tokens and webhook-based repository lifecycle;
- trusted base-branch `.github/review-agent.yaml` and `AGENTS.md` context;
- operator-facing repository and policy management;
- broader notification and collaboration integrations after the core is stable.

## Explicitly deferred

Codex Security, scanner workers, SARIF aggregation, dependency-vulnerability
artifact storage, and similar security integrations are deferred. Existing
security controls stay in place, and deterministic scanners should continue to
run independently in repository CI.
