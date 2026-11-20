---
sidebar_label: Getting started
slug: /getting-started
title: Getting started
description: Register the GitHub App, deploy once, approve the installation, and request the first review.
status: current
last_verified: 2026-11-19
---

# Getting started

Review Agent is deployed once per environment and serves every repository its
GitHub App installations cover. A repository needs no workflow, secret, or
local configuration.

> [!TIP]
> If Review Agent already serves your repository, go straight to
> [Request a review](#4-request-a-review).

## 1. Register the GitHub App

Register the App and store its numeric ID, webhook secret, and private key, as
described in [GitHub App setup](./GITHUB_APP_PILOT.md#1-register-the-app)
steps 1 and 2. The services read all three when they start.

## 2. Deploy

Follow the [deployment guide](./DEPLOYMENT.md) for Compose, Dokploy, Coolify,
Portainer, or Kubernetes (Helm).

## 3. Install and approve the App

Install the App on all repositories or only selected ones, then approve the
installation in Review Agent. Approval has two modes
([GitHub App setup, step 4](./GITHUB_APP_PILOT.md#4-approve-the-installation)):

- **Automatic** (the default of `github-app approve`): the first signed
  `/review` or feedback command from a permitted user enables a repository. A
  command from anyone else leaves it disabled.
- **Explicit**: an operator enables each repository with `github-app onboard`
  or **Add repository** in the console.

## 4. Request a review

In an open pull request whose branch is in the same repository, add a new
top-level comment:

```text
/review
```

You need GitHub `write` or `admin` permission on the repository, or a requester
grant in the console
([who can request a review](./GITHUB_APP_PILOT.md#who-can-request-a-review)).

The review links the head commit it reviewed and lists coverage and findings.
To review a fix, push it and comment `/review` again. If no comment appears,
follow the [runbook](./OPERATIONS.md#runbook).

## 5. Add repository context (optional)

A `.review-agent/` package in the repository gives the reviewer team
instructions, platform context, and accepted design decisions.
[Repository context](./REPOSITORY_CONTEXT.md#start-with-the-copyable-package)
has the copy and validate commands.

## Serve many repositories

One approved installation can serve every repository the App can reach, once
each is enabled ([step 3](#3-install-and-approve-the-app)). All
repositories share one queue and one database, and reviews of one repository
run one at a time;
[add review workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when waits
grow.

Next: [How reviews work](./HOW_REVIEWS_WORK.md).
