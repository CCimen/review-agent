---
sidebar_label: Getting started
slug: /getting-started
title: Getting started
description: Deploy once, approve one GitHub App installation, and request the first review.
status: current
last_verified: 2026-11-17
---

# Getting started

Review Agent is deployed once per environment and serves every repository its
GitHub App installations cover. A repository needs no workflow, secret, or
local configuration.

## 1. Deploy

Follow the [deployment guide](./DEPLOYMENT.md) for Compose, Dokploy, Coolify,
Portainer, or Kubernetes (Helm).

## 2. Install and approve the GitHub App

Register and install the App, then approve the installation once:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app approve <installation-id> \
  --actor "github:<operator>" \
  --reason "approved organization-managed reviews"
```

[GitHub App setup](./GITHUB_APP_PILOT.md) covers registration, the two
activation modes, and how another organization can use your deployment.

## 3. Request a review

In an open pull request, add a new top-level comment:

```text
/review
```

The published review links the head commit it reviewed and reports its coverage
and findings. The base commit is stored with the run; the
[admin console](./ADMIN_PANEL.md) shows it in the request's execution details.
After fixing findings, push a commit and comment `/review` again. If no comment
appears, follow the
[runbook](./OPERATIONS.md#runbook).

## 4. Add repository context (optional)

To give the reviewer team instructions, platform context, and accepted design
decisions, copy the starter package when the repository has no `.review-agent/`
directory, then validate it offline with the release image, from inside that
repository:

```bash
cp -R examples/repository-context/.review-agent /path/to/repository/
cd /path/to/repository
docker run --rm --read-only --network none \
  --entrypoint review-agent-admin \
  --mount type=bind,source="$PWD",target=/repo,readonly \
  ghcr.io/ccimen/review-agent:<release> \
  repository-context validate /repo
```

See [Repository context](./REPOSITORY_CONTEXT.md).

## Serve many repositories

One approved installation covers the repositories GitHub lets the App reach:
all current and future ones, or only the selected ones. Each repository is
enabled by the first `/review` command posted in it; a review starts only if
the commenter
[may request one](./GITHUB_APP_PILOT.md#who-can-request-a-review).
All repositories share one queue and one PostgreSQL database; reviews of the
same repository run one at a time, so
[add review workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when wait
time across repositories grows.

Next: [How reviews work](./HOW_REVIEWS_WORK.md).
