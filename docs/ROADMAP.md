---
sidebar_label: Capabilities
slug: /roadmap
title: Capabilities and limits
description: What Review Agent does today and what it does not do.
status: current
last_verified: 2026-11-17
---

# Capabilities and limits

## What it does

| Capability | Summary | Details |
| --- | --- | --- |
| Code review on request | `/review` reviews the pull request's current base and head commits and publishes advisory findings | [How reviews work](./HOW_REVIEWS_WORK.md) |
| Security review | Every code review traces untrusted input across trust boundaries in the changed code | [How reviews work](./HOW_REVIEWS_WORK.md#4-the-review) |
| Large pull requests | A diff too large for one session is reviewed in up to twelve parts and merged into one review | [How reviews work](./HOW_REVIEWS_WORK.md#5-large-pull-requests) |
| Re-review and feedback | Earlier findings are re-checked; maintainers can mark a finding false positive or intentional | [Feedback and decisions](./FEEDBACK_AND_DECISIONS.md) |
| Repository context | A `.review-agent/` package adds instructions, platform context, and accepted design decisions | [Repository context](./REPOSITORY_CONTEXT.md) |
| Documentation review | `/review docs`, or an automatic mode, checks whether a pull request leaves mapped documents inaccurate | [Documentation review](./DOCUMENTATION_REVIEW.md) |
| Code graph (optional) | Structural and semantic lookups of related code | [Code graph](./CODE_GRAPH.md) |
| Admin console (optional) | Teams, roles, repository access, model connections, usage, audit, OIDC and SCIM | [Admin panel](./ADMIN_PANEL.md) |
| Many organizations | One deployment serves any number of approved GitHub App installations | [GitHub App setup](./GITHUB_APP_PILOT.md#share-the-app-with-other-organizations) |
| Deployment | One Compose stack for Docker, Dokploy, Coolify, and Portainer, and an OpenShift template | [Deployment](./DEPLOYMENT.md) |

State lives in one PostgreSQL database per environment. Jobs and publication
are lease-based, so workers and publishers can run in several replicas and an
interrupted review resumes from stored state.

## What it does not do

- **Block merges.** Reviews are advisory.
- **Start code reviews automatically.** A code review starts when an authorized
  user comments `/review`. Only documentation review has an automatic mode.
- **Review pull requests from forks.**
- **Run code.** Nothing from the pull request is executed, and no tests run.
- **Scan dependencies or the whole repository.** Keep CodeQL, dependency
  scanning, tests, and type checks in CI. Security review covers what the pull
  request changes.
- **Publish anywhere but GitHub.** GitHub's own Slack and Microsoft Teams apps
  can relay the comments; see the
  [FAQ](./FAQ.md#can-we-follow-reviews-in-slack-or-microsoft-teams).
- **Load a different reviewer profile per repository.** One deployment uses one
  profile; repositories customize through their `.review-agent/` package.
