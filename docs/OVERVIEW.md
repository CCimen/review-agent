---
sidebar_label: Overview
slug: /overview
title: Overview
description: What Review Agent is and where to read next.
status: current
last_verified: 2026-11-17
---

# Overview

Review Agent is a self-hosted service that reviews GitHub pull requests on
request. A maintainer comments `/review`; the service reviews the pull request's
current commits with a language model whose tools read the pull request and
record findings in the service's database but cannot write to GitHub, and
publishes advisory findings as pull request comments.

It runs as one deployment per environment: a GitHub App for access, PostgreSQL
for state, and [Hermes](https://hermes-agent.nousresearch.com/) as the model
runtime. The model proposes findings. Authorization, the reviewed commits,
stored state, and every GitHub write are handled by code.

| You want to | Read |
| --- | --- |
| Run a first review | [Getting started](./GETTING_STARTED.md) |
| Understand what happens in a review | [How reviews work](./HOW_REVIEWS_WORK.md) |
| Deploy the service | [Deployment](./DEPLOYMENT.md) |
| Register, approve, or share the GitHub App | [GitHub App setup](./GITHUB_APP_PILOT.md) |
| Give a repository its own instructions and decisions | [Repository context](./REPOSITORY_CONTEXT.md) |
| Answer findings and record design decisions | [Feedback and decisions](./FEEDBACK_AND_DECISIONS.md) |
| Check documentation against code changes | [Documentation review](./DOCUMENTATION_REVIEW.md) |
| Change the reviewer's voice or rules | [Behavior ownership](./BEHAVIOR_OWNERSHIP.md) |
| Manage teams, access, and model connections | [Admin panel](./ADMIN_PANEL.md) |
| Operate and recover the service | [Operations](./OPERATIONS.md) |
| Assess the trust model | [Security](./SECURITY.md) |
| See what is and is not supported | [Capabilities](./ROADMAP.md) |
