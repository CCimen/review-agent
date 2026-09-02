---
sidebar_label: Getting started
slug: /getting-started
title: Getting started
description: Deploy the shared reviewer, approve an installation, and run the first review.
status: current
last_verified: 2026-09-02
---

# Getting started

> **TL;DR:** Approve one trusted GitHub App installation, then let current and
> future repositories activate on their first signed `/review` delivery. Use
> explicit mode only when an operator must approve repositories individually.

Review Agent is deployed once per environment. An organization-managed
installation needs one operator approval, not one command per repository.
Adding a repository does not create another reviewer, database, or profile.
The environment stores application state in one private PostgreSQL database.

## Before you begin

You need access to the deployment and permission to create and install a GitHub
App on the repositories you want to review.

The [deployment guide](./DEPLOYMENT.md) covers App creation, GitHub settings,
Compose platforms, and OpenShift. This page keeps repository onboarding short.

## 1. Install and approve the App

Install the GitHub App with **All repositories** for the recommended
organization-managed mode. Then approve that installation once from the private
gateway:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app approve <installation-id> \
  --actor "github:<operator>" \
  --reason "approved organization-managed reviews"
```

Every new installation starts in explicit mode, so installing a public App does
not grant access to your model capacity. The approval command rechecks the live
App permissions and records the operator decision. The first signed `/review`
delivery then verifies and enables only that exact repository. Requester
authorization still decides whether the review itself may run.

For a narrower allowlist, install with **Only select repositories** and keep the
explicit policy. Run `github-app onboard <owner/repository>` for each repository
the operator approves. [GitHub App setup](./GITHUB_APP_PILOT.md) explains both
modes and the rollback command.

## 2. Add optional repository context

Repositories work without local configuration. To add team instructions,
ordered platform or framework context, and typed design decisions, copy the
starter only when the repository has no `.review-agent/` package. Preserve and
edit an existing package in place, then validate it offline:

```bash
repository_root=/path/to/repository
if [ -e "$repository_root/.review-agent" ]; then
  echo ".review-agent already exists; preserve it and edit it in place."
else
  cp -R examples/repository-context/.review-agent "$repository_root/"
fi
.venv/bin/python tools/review_agent_admin.py repository-context validate \
  "$repository_root"
```

[Repository context](./REPOSITORY_CONTEXT.md) defines which files are loaded,
their order, and the fixed deployment rules they cannot override.

## 3. Run the first review

Open or reuse a pull request and add a new top-level comment:

```text
/review
```

The App sends the signed GitHub event to the reviewer. The published comment records the exact
base/head snapshot and the review's coverage and findings.

After fixing findings, push a commit and post `/review` again. A changed head SHA
creates a new review round while the prior round remains historical context.

## 4. Verify the result

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

## Serve many repositories

One deployment serves an entire organization. In organization-managed mode,
current and future repositories included by the GitHub installation need no
operator command, repository workflow, or duplicated Actions secret. A
developer with current `write` or `admin` permission posts `/review`; the
gateway verifies the exact repository before admitting work.

Every activated repository shares one queue, one PostgreSQL database, and one
neutral reviewer baseline. PostgreSQL serializes reviews within a repository, so
[scale review workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when
cross-repository wait time grows. Teams may add bounded repository-owned
instructions and context without creating another deployment profile.

## Next step

Read [How reviews work](./HOW_REVIEWS_WORK.md) before interpreting the first
result.
