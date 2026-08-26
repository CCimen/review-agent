---
sidebar_label: Getting started
slug: /getting-started
title: Getting started
description: Deploy the shared reviewer, add a repository, and run the first review.
status: current
last_verified: 2026-08-26
---

# Getting started

> **Current** — One GitHub App installation owns admission, source access, and
> deterministic publication for explicitly enabled repositories.

Review Agent is deployed once per environment and serves an explicit
repository allowlist. Adding a repository changes configuration around that
shared platform; it does not create another reviewer, database, or profile.
The environment stores application state in one private PostgreSQL database.

## Before you begin

You need access to the deployment and permission to create and install a GitHub
App on the repositories you want to review.

The [deployment guide](./DEPLOYMENT.md) covers App creation, GitHub settings,
Compose platforms, and OpenShift. This page keeps repository onboarding short.

## 1. Install and enable the repository

Install the GitHub App with **Only select repositories**, choose the repository,
then reconcile and enable it with the commands in [GitHub App
setup](./GITHUB_APP_PILOT.md). Installation makes the repository available;
the separate enable step is the operator approval boundary. A blank enabled set
denies all review requests.

## 2. Run the first review

Open or reuse a pull request and add a new top-level comment:

```text
/review
```

The App sends the signed GitHub event to the reviewer. The published comment records the exact
base/head snapshot and the review's coverage and findings.

After fixing findings, push a commit and post `/review` again. A changed head SHA
creates a new review round while the prior round remains historical context.

## 3. Verify the result

- Confirm the summary identifies the expected base and head SHAs.
- Open each cited file link and assess the finding in context.
- Apply only suggestions that are independently safe; use the coding-agent brief
  for coordinated changes.
- If feedback is enabled, use the deterministic `/review false-positive`,
  `/review feedback scope`, or `/review feedback missed` commands described in
  [Operations](./OPERATIONS.md#developer-feedback).

If no comment appears, follow the [failure runbook](./OPERATIONS.md#runbook)
rather than retrying blindly.

## Onboard many repositories

One deployment serves an entire organization. Select additional repositories in
the App installation, reconcile the inventory, and enable each repository
explicitly. No repository workflow or duplicated Actions secrets are required.

Every onboarded repository shares one queue, one PostgreSQL database, and one
reviewer profile. PostgreSQL serializes reviews within a repository, so
[scale review workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when
cross-repository wait time grows. Per-repository voice or rules are not
supported yet; see [Behavior ownership](./BEHAVIOR_OWNERSHIP.md) for what a
profile controls deployment-wide.

## Next step

Read [How reviews work](./HOW_REVIEWS_WORK.md) before interpreting the first
result.
