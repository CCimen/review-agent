---
title: Team access and model accounts
slug: /team-access-and-model-accounts
description: What a team can see and do in the console, how repositories belong to teams, and how a team's reviews reach a model account.
status: current
last_verified: 2026-11-17
---

# Team access and model accounts

A team owns repositories, decides who in the console can see their reviews, and
can route those reviews to its own model account. Procedures are in the
[admin panel](ADMIN_PANEL.md) guide.

## Teams and repositories

A team is a named group of console accounts. An account can belong to several
teams, as **Maintainer** or **Viewer** in each. Platform owners and admins see
every team; global viewers can read every team; ordinary members see only their
own teams. A team the caller does not belong to returns `404`.

Each repository has at most one owning team:

- A maintainer requests a repository for the team. An owner or admin approves
  it, which checks the GitHub App grant, assigns the team and enables reviews in
  one step. Owners and admins can also assign, transfer or remove ownership
  directly.
- A transfer moves access to the repository's retained reviews to the new team.
- A repository without a team is visible only to owners, admins and global
  viewers, and its reviews use the deployment's default model route.

Team membership grants nothing on GitHub and does not decide
[who can request a review](GITHUB_APP_PILOT.md#who-can-request-a-review).

## What team members can do

| Action | Viewer | Maintainer |
| --- | --- | --- |
| Read the team's reviews, findings, quality reports, repositories and members | Yes | Yes |
| See the team's model connection and its account quota | Yes | Yes |
| Add, change or remove team members (existing active accounts, by exact email) | | Yes |
| Request repositories and withdraw requests | | Yes |
| Allow additional GitHub users to request reviews on a team repository | | Yes |
| Triage feedback and record finding decisions | | Yes |
| Retry or cancel the team's reviews | | Yes |
| Choose the team's model and reasoning effort from the allowed choices | | Yes |
| Connect, pause, enable or record the account of a team-owned connection | | Yes |

Only owners and admins approve repository requests, assign connections, set
team capacity, and read audit and usage reports. Membership changes take effect
on the next request.

## Model connections

A model connection is a Hermes runtime with its own provider credentials. Every
deployment has one shared connection. An owner can register more, each owned by
one team ([provision another connection](ADMIN_PANEL.md#provision-another-connection)).

When a review request is admitted, Review Agent resolves the route from the
repository's owning team:

1. **Connection:** the team's assigned connection, or the shared connection if
   none is assigned. An admin assigns it; a team-owned connection can serve only
   its own team.
2. **Model:** the team's chosen provider, model and reasoning effort, selected
   from the choices the connection allows. Without a team choice, the
   deployment's model from **Settings** (or its environment values before the
   first save).

The route is frozen into the request: later changes apply to new requests only,
queued reviews never switch account, and nothing falls back to another
connection or provider. A paused connection stops new dispatch.

## Limits

- **Team capacity:** at most four of a team's reviews run at once, across all
  its connections. Admins can change the cap. The team recorded at admission is
  counted.
- **Connection capacity:** at most four reviews per connection by default.
  Owners set it for the shared connection; owners and admins for a team-owned one.
- **Repository:** one review runs per repository at a time.
- **Fairness:** eligible teams take turns claiming work, with priority aging
  within each team.
- **Quota:** teams that share a connection share its Codex account quota, which
  also counts any other use of that account. When Codex reports the account
  exhausted, its queued reviews wait without using an attempt
  ([quota and concurrent reviews](ADMIN_PANEL.md#quota-and-concurrent-reviews)).

## Isolation

Team scope is access control inside one deployment and one database. All teams
share the same services, PostgreSQL database and job queue; the server filters
every query and action by team, and a dedicated connection keeps a team's
provider credentials and quota apart. It is not isolation between mutually
hostile tenants: owners, admins, global viewers and anyone with database access
can read every team's data. Use separate deployments where that boundary is
required.
