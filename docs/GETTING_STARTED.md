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

The published review names the base and head commits it covers, its coverage,
and its findings. After fixing findings, push a commit and comment `/review`
again. If no comment appears, follow the
[runbook](./OPERATIONS.md#runbook).

## 4. Add repository context (optional)

To give the reviewer team instructions, platform context, and accepted design
decisions, copy the starter package when the repository has no `.review-agent/`
directory, then validate it offline:

```bash
cp -R examples/repository-context/.review-agent /path/to/repository/
.venv/bin/python tools/review_agent_admin.py repository-context validate \
  /path/to/repository
```

See [Repository context](./REPOSITORY_CONTEXT.md).

## Serve many repositories

One approved installation covers its current and future repositories. Each
repository is enabled the first time an authorized user posts `/review` in it.
All repositories share one queue and one PostgreSQL database; reviews of the
same repository run one at a time, so
[add review workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when wait
time across repositories grows.

Next: [How reviews work](./HOW_REVIEWS_WORK.md).
