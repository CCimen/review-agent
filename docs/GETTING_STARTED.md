---
sidebar_label: Getting started
slug: /getting-started
title: Getting started
description: Deploy the shared reviewer, add a repository, and run the first review.
status: current
last_verified: 2026-08-24
---

# Getting started

> **Current** — This procedure uses the current shared deployment, repository
> allowlist, fine-grained tokens, and protected GitHub Actions workflow.

Review Agent is deployed once per environment and serves an explicit
repository allowlist. Adding a repository changes configuration around that
shared platform; it does not create another reviewer, database, or profile.
The environment stores application state in one private PostgreSQL database.

## Before you begin

You need access to the Docker Compose or Dokploy deployment, permission to
create repository-scoped GitHub tokens, and permission to add an Actions
workflow, four Actions secrets, and one Actions variable to the repository.

The [deployment guide](./DEPLOYMENT.md) covers token creation, GitHub settings,
Compose platforms, and OpenShift. This page keeps repository onboarding short.

## 1. Allow the repository

Add the exact, case-insensitive `owner/repository` name to the comma-separated
`REVIEW_AGENT_ALLOWED_REPOSITORIES` deployment setting. A blank allowlist denies
all repositories.

Make sure the three current GitHub tokens can access the repository:

- `GITHUB_READ_TOKEN` for PR metadata, diffs, and file reads;
- `REVIEW_AGENT_PUBLISH_GH_TOKEN` for review comments and suggestions;
- `REVIEW_AGENT_FEEDBACK_GH_TOKEN` for the optional feedback path.

Keep each token repository-scoped and preserve the role-specific permissions in
the [deployment permission table](./DEPLOYMENT.md#github-tokens).

## 2. Install the trusted workflow

Copy
[`examples/github/ai-review-request.yml`](https://github.com/CCimen/review-agent/blob/main/examples/github/ai-review-request.yml)
to `.github/workflows/ai-review-request.yml` on the repository's default branch.
Protect this workflow with CODEOWNERS or a repository ruleset.

Configure these repository Actions secrets:

```text
HERMES_REVIEW_URL
HERMES_WEBHOOK_SECRET
HERMES_REVIEW_FEEDBACK_URL
HERMES_REVIEW_FEEDBACK_SECRET
```

Set the `AI_REVIEW_ALLOWED_USERS` Actions variable to a comma-separated list of
trusted GitHub usernames. Empty means deny all. The workflow also requires the
requester to be an `OWNER`, `MEMBER`, or `COLLABORATOR`.

## 3. Run the first review

Open or reuse a pull request and add a new top-level comment:

```text
/review
```

The workflow must already exist on the default branch. It sends a signed,
minimal request to the reviewer. The published comment records the exact
base/head snapshot and the review's coverage and findings.

After fixing findings, push a commit and post `/review` again. A changed head SHA
creates a new review round while the prior round remains historical context.

## 4. Verify the result

- Confirm the summary identifies the expected base and head SHAs.
- Open each cited file link and assess the finding in context.
- Apply only suggestions that are independently safe; use the coding-agent brief
  for coordinated changes.
- If feedback is enabled, use the deterministic `/review false-positive`,
  `/review feedback scope`, or `/review feedback missed` commands described in
  [Operations](./OPERATIONS.md#developer-feedback).

If no comment appears, follow the [failure runbook](./OPERATIONS.md#runbook)
rather than retrying blindly.

## Next step

Read [How reviews work](./HOW_REVIEWS_WORK.md) before interpreting the first
result.
