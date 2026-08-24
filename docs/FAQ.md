---
sidebar_label: FAQ
slug: /faq
title: Frequently asked questions
description: Practical answers about access, findings, feedback, storage, and failures.
status: current
last_verified: 2026-08-24
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

## Does feedback automatically teach or suppress the reviewer?

No. Human feedback is stored as evidence. It cannot rewrite prompts, skills, or
policy automatically, and the model cannot dismiss its own findings. Operators
evaluate repeated evidence through private, reviewed learning workflows.

## What is stored in PostgreSQL?

Review runs, findings, coverage, publication and suggestion state, human
decisions, and review-quality feedback. The database can contain unpublished
findings and maintainer reasons, so back it up and handle exports as sensitive
operator data.

## Does it scan dependencies for CVEs?

No. It may reason about dependency changes in a PR, but it does not query a
vulnerability database or replace deterministic CI scanners. Scanner and Codex
Security integrations are deferred.

## Why did a review fail or stop?

Common causes include an unauthorized requester, repository allowlist denial,
invalid HMAC signature, a stale head SHA, GitHub permission failure, oversized
output, or a stalled lifecycle transition. Use the exact status and
[Operations runbook](./OPERATIONS.md#runbook); do not infer success from a
workflow that merely started.

## How do I add another repository or change reviewer behavior?

Use [Getting started](./GETTING_STARTED.md) for repository onboarding and
[Behavior ownership](./BEHAVIOR_OWNERSHIP.md) for deployment-wide policy and
runtime owners.
