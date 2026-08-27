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

Review Agent is deployed once per environment and serves repositories that an
operator explicitly enables after App installation. Adding a repository changes
configuration around that shared platform; it does not create another reviewer,
database, or profile.
The environment stores application state in one private PostgreSQL database.

## Before you begin

You need access to the deployment and permission to create and install a GitHub
App on the repositories you want to review.

The [deployment guide](./DEPLOYMENT.md) covers App creation, GitHub settings,
Compose platforms, and OpenShift. This page keeps repository onboarding short.

## 1. Install and enable the repository

Install the GitHub App with **Only select repositories** and choose the
repository. Then onboard it from the private gateway:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app onboard <owner/repository> \
  --actor "github:<operator>"
```

The command reconciles GitHub access and records the operator's approval for the
named repository. A blank enabled set denies all review requests. [GitHub App
setup](./GITHUB_APP_PILOT.md) shows the container-specific command.

The onboarding command replaces the lower-level installation-ID sync and
repository-ID enable commands. Run it again after changing the App's selected
repositories; it is safe to repeat.

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
  `/review intentional`, `/review feedback scope`, or `/review feedback missed`
  commands described in
  [Operations](./OPERATIONS.md#developer-feedback).

If no comment appears, follow the [failure runbook](./OPERATIONS.md#runbook)
rather than retrying blindly.

## Onboard many repositories

One deployment serves an entire organization. Select another repository in the
App installation and run `github-app onboard` for its full name. No repository
workflow or duplicated Actions secrets are required.

Every onboarded repository shares one queue, one PostgreSQL database, and one
reviewer profile. PostgreSQL serializes reviews within a repository, so
[scale review workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when
cross-repository wait time grows. Per-repository voice or rules are not
supported yet; see [Behavior ownership](./BEHAVIOR_OWNERSHIP.md) for what a
profile controls deployment-wide.

## Next step

Read [How reviews work](./HOW_REVIEWS_WORK.md) before interpreting the first
result.
