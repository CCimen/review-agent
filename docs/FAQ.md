---
sidebar_label: FAQ
slug: /faq
title: Frequently asked questions
description: Short answers about access, sharing, review scope, feedback, storage, and failures, with links to the pages that own each topic.
status: current
last_verified: 2026-11-17
---

# Frequently asked questions

## My organization already runs Review Agent. What does my team need to do?

Nothing to install. Comment `/review` on a pull request in a repository covered
by the organization's GitHub App installation. You need GitHub write or admin
permission, or a grant in the console's **Who can request reviews** list; see
[Who can request a review](./GITHUB_APP_PILOT.md#who-can-request-a-review). If
the repository is not enabled yet, a team maintainer can request it from the
team's **Repositories** tab in the [admin console](./ADMIN_PANEL.md). An
optional [`.review-agent/` package](./REPOSITORY_CONTEXT.md) adds team
instructions, architecture context, and accepted ADRs.

## Does it review automatically when a pull request opens?

No. Code review starts when an authorized user comments `/review`; comments from
bots, including GitHub Actions, are ignored. Comment `/review` again after
pushing fixes. Documentation review can run automatically when a team or
repository selects Automatic mode; see
[Documentation review](./DOCUMENTATION_REVIEW.md).

## Can another organization use our GitHub App?

Yes. Make the App public, have the other organization install it, and approve
that installation as an operator. Every installation shares the deployment's
database, job queue, and model route, unless a team has its own model
connection. See
[Share the App with other organizations](./GITHUB_APP_PILOT.md#share-the-app-with-other-organizations).

## How do I serve many repositories or a whole organization?

Deploy once, install the App on the organization, and approve the installation
once. Repositories then activate on their first authorized `/review`. See
[Set up and share the GitHub App](./GITHUB_APP_PILOT.md), and
[scale workers](./DEPLOYMENT.md#scale-and-operate-the-queue) when queue wait
time grows.

## Is it a merge gate?

No. Reviews are advisory.

## Can it run contributor code?

No. The reviewer has no shell, browser, repository write access, delegation, or
code execution. It reads the pull request through bounded GitHub tools. See
[Security](./SECURITY.md).

## Does it use RAG or reranking?

The reviewer reads the diff and the files it asks for. The optional
[code graph](./CODE_GRAPH.md) is a retrieval path that returns up to ten
locations per query. Nothing reranks findings or context. See
[How reviews work](./HOW_REVIEWS_WORK.md).

## Does it find security problems?

Yes, within the pull request. Every code review applies security-audit guidance
to the changed code and reports vulnerabilities and flawed security logic the
pull request introduces or worsens. It does not run exploits, query a CVE
database, audit unchanged code, or certify a repository as secure; keep
deterministic scanners in CI. See [How reviews work](./HOW_REVIEWS_WORK.md).

## Can it review architecture?

For the code a pull request changes, yes: rules implemented outside their owner,
duplicated policy, hidden coupling, and conflicts with accepted ADRs. Describe
the intended structure in the repository's
[`.review-agent/` package](./REPOSITORY_CONTEXT.md) so the review can check
against it. It does not assess the whole repository.

## Does feedback affect later reviews?

`/review false-positive` and `/review intentional` suppress the same finding in
later rounds while the code and ADR still match. `/review feedback scope` and
`/review feedback missed` record evidence only. See
[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md).

## Can each repository have its own reviewer voice or rules?

Each repository can add review focus, communication preferences, technical
context, and accepted ADRs through its [`.review-agent/`
package](./REPOSITORY_CONTEXT.md), read from the pull request's base commit. The
deployment profile owns identity, procedure, evidence gates, severity, tools,
authorization, and publication; see
[Behavior ownership](./BEHAVIOR_OWNERSHIP.md).

## What is stored in PostgreSQL?

Review runs, findings, coverage, publication state, human decisions, and
feedback. The database can contain unpublished findings and maintainer reasons,
so back it up and treat exports as sensitive. See [Security](./SECURITY.md).

## Why did a review fail or stop?

Common causes are an unauthorized requester, a disabled repository, a stale head
SHA, or a GitHub permission failure. Check the run's failure code in the
[Operations runbook](./OPERATIONS.md#runbook).

## Can we follow reviews in Slack or Microsoft Teams?

Review Agent publishes only to GitHub. GitHub's own apps can relay those
publications to a channel:

- Slack: `/github subscribe <owner>/<repo> reviews comments`
- Microsoft Teams: `@GitHub subscribe <owner>/<repo> reviews` (comments are on
  by default)

Code reviews arrive as comments and reviews. Documentation results that do not
change open findings appear only as a GitHub check, which these subscriptions
do not relay. Subscribe in a channel whose members can already read the
repository, because relayed comments contain review content. Starting a review
from Slack or Teams is not supported.
