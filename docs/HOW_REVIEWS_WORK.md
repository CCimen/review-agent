---
sidebar_label: How reviews work
slug: /how-reviews-work
title: How reviews work
description: The trusted path from a review request to deterministic GitHub publication.
status: current
last_verified: 2026-08-27
---

# How reviews work

> **Current** — This page describes the live request, review, persistence, and
> publication path. Optional integrations are listed under
> [Capabilities and boundaries](./ROADMAP.md).

The reviewer separates model reasoning from authorization, state transitions,
and GitHub writes. The model can investigate and propose findings; deterministic
code decides what snapshot is valid and how stored results reach GitHub.

![Four phases of a review: request and authorize, read and review, verify and publish, then re-review with explicit feedback.](../website/static/img/review-lifecycle.webp)

## Request and authorize

An authorized maintainer posts `/review` on a pull request. The GitHub App receives
the signed event, verifies the requester's current repository permission, and
checks that an operator enabled the repository before admitting durable work.
Source reads and publication use short-lived, repository-scoped installation
tokens behind the private gateway.

## Pin the subject

The review begins against one base SHA and one head SHA. Bounded tools read PR
metadata, the changed-file list, diffs, and selected file content for that
snapshot. When `.review-agent/config.toml` exists, deterministic code also reads
the optional `instructions.md` and explicitly indexed context files from that
base commit and stores one immutable snapshot. This guidance may focus the
review and communication style, but it cannot change tools, authorization,
evidence gates, lifecycle, or publication.

If the repository has `.review-agent/decisions.toml`, deterministic code maps
changed paths to typed ADR headers and stores one immutable base-commit
snapshot. The reviewer receives accepted decisions as untrusted evidence. A
missing, invalid, or oversized decision set removes only ADR context; the code
review keeps its full changed-file and source coverage contract.

## Review in two passes

The first pass looks for plausible correctness, security, reliability, and
maintainability problems introduced or worsened by the pull request. The second
pass tries to disprove each candidate with surrounding code, tests, invariants,
and changed behavior. Only independent findings that survive the evidence and
severity gates are recorded.

Coverage remains explicit. Changed paths and source ranges are pageable, and an
oversized path diff returns an exact continuation position. If GitHub's provider
limits or a resource guard still prevents complete inspection, the reviewer
reports incomplete coverage instead of implying a clean or complete review.

## Publish deterministically

Before writing, plugin code verifies that the pull request still has the exact
reviewed head SHA. It renders stored findings into a stable summary and splits
large output predictably. Optional GitHub suggestions are published only when
their range and current content still match and each patch is local and safe to
apply independently.

The model has no arbitrary GitHub mutation tool. It freezes the exact publication
parts in PostgreSQL. A separate publisher claims that durable intent, writes only
those parts through the lease-bound App gateway, and records each GitHub ID independently.
If the process stops after GitHub accepts a write, marker recovery finds the same
object instead of creating a duplicate.

## Durable execution

Each review worker claims PostgreSQL jobs with
`FOR UPDATE SKIP LOCKED`, heartbeats one exact lease generation, and continues
the assigned run through Hermes' authenticated chat API. A reclaimed generation
uses a new request identity and fenced tool session. Retries within one
generation remain idempotent, while PostgreSQL rejects the next mutable tool
entry from an older generation after it loses its lease. Each operation keeps
its own transactional guards. The publisher uses a separate fenced lease so a
slow GitHub request never holds a database transaction. Both worker types can be
replicated; PostgreSQL coordinates claims and recovery.

## Learn from explicit feedback

Authorized maintainers can report false positives, intentional designs, scope
problems, and missed issues. Exact false-positive decisions require the same
finding and code context. Intentional decisions also require the same accepted
ADR metadata in the current base snapshot. Scope and missed-issue commands
record quality evidence for operators. None of these commands rewrites policy
or prompts.

[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md) shows the weekly
and monthly operating flow, plus the repository ADR contract. Private
coach and verification workflows remain outside the live review path and cannot
gate a pull request.

Read [Security](./SECURITY.md) for the trust boundaries and
[Operations](./OPERATIONS.md) for lifecycle states and recovery commands.
