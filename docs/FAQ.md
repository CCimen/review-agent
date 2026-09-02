---
sidebar_label: FAQ
slug: /faq
title: Frequently asked questions
description: Practical answers about access, findings, feedback, storage, and failures.
status: current
last_verified: 2026-09-02
---

# Frequently asked questions

> **Current** — Answers describe the reviewer available now unless they
> explicitly say planned or deferred.

## What does the reviewer inspect?

It reviews behavior introduced or worsened by the exact pull-request base/head
snapshot. It reads bounded metadata, diffs, changed-file lists, and selected file
content. Unchanged code may be supporting evidence, but it is not a license for
unrelated cleanup findings.

## Is it a merge gate?

No. The current deployment is advisory. Teams should measure false positives,
accepted findings, missed-issue feedback, and operational failures before any
separate decision to make review status blocking.

## Can it run contributor code?

No. The live reviewer has no shell, browser, repository writer, delegation, or
arbitrary code execution. It reads through bounded GitHub tools.

## What are `F1`, `F2`, and later-round states?

They are stable finding references. A later review can report a finding as
resolved, still present, returned, or not checked. The new round never rewrites
the historical record of a successfully published prior round.

## How are native suggestions different from the coding-agent brief?

Native suggestions are small, exact, independently safe patches that GitHub can
apply directly. The coding-agent brief groups coordinated changes that need
broader reasoning, edits, or validation. Applying either path still requires CI
and a fresh review round.

## Does feedback affect later reviews?

Two exact decisions can do so directly. `/review false-positive` suppresses the
same stable finding while its code-context hash still matches. `/review
intentional` also requires the same accepted ADR ID and metadata in the current
base snapshot. Changed code, changed ADR metadata, or a superseded ADR requires
a new review.

`/review feedback scope` and `/review feedback missed` record quality evidence
for metrics, replay cases, and private improvement analysis. They do not suppress
findings or rewrite prompts, skills, or policy. Broader reviewer changes remain
human-reviewed and replay-tested before deployment.

[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md) gives the operator
commands and explains which canonical owner should change for each signal.

## What is stored in PostgreSQL?

Review runs, findings, coverage, publication and suggestion state, human
decisions, and review-quality feedback. The database can contain unpublished
findings and maintainer reasons, so back it up and handle exports as sensitive
operator data.

## Does it scan dependencies for CVEs?

The live reviewer does not query a vulnerability database. Repository CI scans
the Python requirements and both npm lockfiles with Trivy, while release CI
scans each exact published platform digest. The model may still discuss a risky
dependency change, but the deterministic scanner owns the CVE verdict.

## Why did a review fail or stop?

Common causes include an unauthorized requester, a disabled or removed
repository, an invalid GitHub App signature, a stale head SHA, GitHub permission
failure, oversized output, or a stalled lifecycle transition. Use the exact
status and
[Operations runbook](./OPERATIONS.md#runbook); do not infer success from a
workflow that merely started.

## How do I serve many repositories or a whole organization?

Deploy once per environment, install the GitHub App on the organization, and
approve that installation once for automatic activation. Current and future
repositories included by the GitHub installation become usable on their first
signed `/review` delivery; requester authorization still gates whether the
review starts. No per-repository deployment command is required. New public App
installations remain locked until an operator approves their exact installation
ID. [Getting started](./GETTING_STARTED.md#serve-many-repositories) walks
through it. One deployment queues reviews across all activated repositories;
[scale workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when wait time grows.

## Can each repository have its own reviewer voice or rules?

Each repository can add engineering principles, review focus, communication
preferences, ordered technical context, and accepted ADRs through its reviewed
[`.review-agent/` package](./REPOSITORY_CONTEXT.md). Review Agent reads that
package from the exact pull-request base commit, so a pull request cannot change
the rules used to review itself.

The deployment profile still owns the neutral identity, review procedure,
evidence gates, severity rules, tools, authorization, and publication. A
repository cannot override those controls. If two repository groups truly need
different core review contracts or model routes, run separate deployments with
reviewed profiles rather than adding repository-name branches to the engine.

## What are `SOUL.md` and profiles, and where do I learn more?

A profile bundles the reviewer's identity (`SOUL.md`), stable review rules
(`workspace/AGENTS.md`), and reviewed skills. [Behavior
ownership](./BEHAVIOR_OWNERSHIP.md) explains each owner and how to create a
profile. The identity mechanism is Hermes' native
[personality and `SOUL.md` ownership](https://hermes-agent.nousresearch.com/docs/user-guide/features/personality);
Hermes also documents [deploying a custom soul](https://hermes-agent.nousresearch.com/docs/guides/use-soul-with-hermes).

## How do I add another repository or change reviewer behavior?

Use [Getting started](./GETTING_STARTED.md) for repository onboarding and
[Repository context](./REPOSITORY_CONTEXT.md) for team-owned focus and project
facts. [Behavior ownership](./BEHAVIOR_OWNERSHIP.md) maps deployment-wide policy
and runtime owners.
