---
sidebar_label: Capabilities
slug: /roadmap
title: Capabilities and boundaries
description: The working Review Agent core and the integrations kept outside it.
status: current
last_verified: 2026-09-02
---

# Capabilities and boundaries

> **Current** — The maintainability-first core described on this page is
> implemented in the repository. Optional integrations have separate product
> and security decisions.

## Core platform

- **Trusted admission:** the GitHub App validates direct comment events,
  requester permission, and selected-repository access before durable work is
  admitted and the current base and head commits are pinned.
- **Evidence-backed review:** Hermes receives bounded read tools, records
  coverage, challenges candidate findings, and keeps incomplete review depth
  visible.
- **Durable execution:** PostgreSQL coordinates fair job claims, retry budgets,
  dead-letter recovery, exact-run continuation, and generation fencing across
  replicated workers.
- **Recoverable publication:** the review tool freezes immutable comment parts;
  a separate publisher writes them to GitHub and recovers ambiguous writes
  without creating duplicate comments.
- **Operator control:** scoped commands cover queue inspection, retries,
  cancellation, run recovery, publication status, backup, restore, feedback,
  and private verification exports.
- **Deployment profiles:** a reviewed profile owns voice, stable rules,
  presentation, and enabled skills. Engine code keeps authorization, tool
  limits, snapshot checks, state transitions, and GitHub writes fixed.
- **Repository context:** one explicit `.review-agent/` package can add team
  instructions and ordered platform or framework facts from the exact base
  commit. The package is bounded, content-addressed, and cannot override the
  deployment contract.
- **Repository decisions:** typed ADR metadata from the exact base commit gives
  the reviewer repository-specific invariants without granting repository files
  control over policy, tools, or severity.
- **Portable deployment:** the repository ships one Compose stack for Docker,
  Dokploy, Coolify, and Portainer plus an arbitrary-UID OpenShift template.

The deployment uses one PostgreSQL database per environment. Hermes keeps its
own profile and session files outside application state.

## Runtime contract

- **Review and reporting:** Bounded PR reads feed Direct PostgreSQL review
  state, Durable PostgreSQL job records, and Repository-scoped exports.
- **Database lifecycle:** Checksum-verified PostgreSQL migrations make schema
  changes explicit and repeatable.
- **State ownership:** One PostgreSQL database per environment. PostgreSQL owns
  application persistence; Hermes `HERMES_HOME` remains separate.
- **Connection safety:** Network and model calls never hold database
  connections.
- **Recovery:** Reviews are activated through signed admission, use exact-run
  continuation, and publish through a recoverable publisher lease.

## Optional extensions

- Repository-specific replacement profiles or remote context-package imports.
- Notification or collaboration channels beyond GitHub.

Each extension needs a concrete operator need, an owner, and a security review.
The core platform does not depend on any of them.

## Outside the reviewer

CodeQL, dependency scanning, SARIF aggregation, and other deterministic scanners
belong in repository CI. Review Agent may discuss risks visible in a pull request,
but it does not replace those controls or become the default merge owner.
