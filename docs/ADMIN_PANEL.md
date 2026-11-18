---
sidebar_label: Admin panel
slug: /admin-panel
title: Review Agent operator console
description: Inspect reviews, manage repository access and finding decisions, and apply deployment policy through an authenticated console.
status: current
last_verified: 2026-11-17
---

# Review Agent operator console

The optional console gives teams a workspace for their review activity,
repositories, review quality and membership. Owners and admins also manage
platform access, audit and policy. GitHub remains the source of review requests
and the destination for published reviews.

Run the console from the same release as the main image. The admin API needs
the migrations of that release, so build or pull both images from one revision
and run its migrations before starting the services. Do not roll back the
console image alone; use the coordinated image and database recovery in
[Deployment](./DEPLOYMENT.md#upgrade-and-roll-back-production).

## Add the service

The console is one additional service, `review-admin`, in the existing Compose
application, with its own image and hostname. It serves the compiled frontend
and API together on port `8090`; Node.js is used only during the build. Follow
the normal [deployment and upgrade procedure](DEPLOYMENT.md), including backup
and migrations.

The Helm chart deploys the main runtime only; running the console on Kubernetes
also needs a Deployment, Service, HTTPS ingress and network-policy access to the
private database and gateway, which the chart does not supply.

1. Add these non-secret values to the deployment configuration:

   ```dotenv
   REVIEW_AGENT_ADMIN_IMAGE=review-agent-admin:local
   REVIEW_AGENT_ADMIN_PUBLIC_URL=https://admin.example.org
   ```

   The console uses the existing `REVIEW_AGENT_RUNTIME_DATABASE_URL`. The
   overlay also mounts the GitHub App private key read-only for installation
   approval and inventory refresh. `REVIEW_AGENT_ADMIN_PUBLIC_URL` must exactly
   match the console's HTTPS origin, including a non-default port. Serve it at
   the hostname root, not a URL subpath.

2. Include `compose.admin.yaml` with every Compose operation, together with any
   overlays the deployment already uses. For a source build:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml config --quiet
   docker compose -f compose.yaml -f compose.admin.yaml up -d --build
   ```

   For a published release, set both image variables to their manifest
   references from the same `IMAGE-DIGESTS.txt` (the two images have different
   digests), then run `pull` and `up -d --no-build`. Remove any saved
   `pull_policy: never` override from a local-build setup; it prevents Compose
   from fetching the release image. After an upgrade, compare the version and
   full revision from `/api/version` with the release tag and `SOURCE-SHA.txt`.
   Keep the admin service stopped during migrations and start it with the
   matching workers.

   In Dokploy, keep the console in the same Compose application so it can reach
   the private database network. For a raw Compose deployment, merge the overlay
   into a copy of the current definition:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml config \
     --no-interpolate --no-path-resolution --no-normalize > compose.dokploy.yaml
   ```

   Preserve the existing project name, volumes, mounts, networks and overlays
   when saving the result. Set values in Dokploy Environment. For a Git-based
   deployment, include both Compose files in its command.

3. In Dokploy Domains, route a separate HTTPS hostname to service `review-admin`,
   container port `8090`, path `/`, with the HTTPS redirect enabled. Admission
   keeps its own hostname and port `8644`. Only admission and the console join
   the ingress network; the database, gateway and Hermes stay private.

4. After migrations complete, create the first platform owner from the console
   container's terminal:

   ```bash
   python -m review_agent_tools.admin_auth --email admin@example.org
   ```

   From a local Compose terminal:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml exec review-admin \
     python -m review_agent_tools.admin_auth --email admin@example.org
   ```

   Enter the password at the hidden prompt, never in arguments, environment
   files or chat. Passwords are 15–128 characters. The command creates an
   account only when no accounts exist; restarts never reset passwords.

5. Open the admin hostname and sign in. Check repository counts and a known
   review. Add a Member account under **Users**, add it to a team as Viewer, and
   confirm that it reads that team's repositories without user administration.

The console footer and `GET /api/version` show the release version and source
revision baked into the image; environment variables do not change them. Local
builds report `development` with no source revision. The admin image runs
Python 3.14.7 on Debian 13 without pip, ensurepip or Perl; apply updates by
replacing the image. Published releases attach its npm dependency inventory with
their other [release evidence](SECURITY.md#release-inventories).

## Roles and team scope

Each account has one platform role. Members also hold a role in each team they
belong to.

| Role | Access |
| --- | --- |
| Owner | Everything: all teams, owner/admin accounts, deployment Settings, shared model connections, provider credentials, SMTP and registration, and owner-only audit events. |
| Admin | All teams; Member and Global viewer accounts; teams and repository ownership; GitHub App access; run controls; usage reports; integrations; team model connections and capacity; audit events that are not owner-only. |
| Global viewer | Read access to every team. |
| Member (default) | Access only through team memberships. |
| Team maintainer | Within the team: manage membership, request repositories, manage review requesters, triage feedback, record finding decisions, retry or cancel reviews, choose the team's model, and manage a team-owned model connection. |
| Team viewer | Within the team: read reviews, repositories, quality reports and membership. |

The server enforces scope on every list, total, search, export, direct link and
action. A team, repository, review or connection outside the caller's scope
returns `404` without revealing that it exists. Without `team_id`, a member
sees the union of their teams; owners, admins and global viewers see the whole
deployment. Membership and ownership changes apply on the next request. Team
views omit deployment-wide worker and capacity figures.

Owners and admins create teams under **Teams**. A maintainer, admin or owner
adds an existing active account to a team by its exact email, as Viewer or
Maintainer. Members of one team enter that workspace directly;
members of several teams switch between them. Team and period filters follow
navigation.

For what a team controls and how it maps to model accounts, see
[Team access and model accounts](TEAM_ACCESS_AND_MODEL_ACCOUNTS.md).

## Repositories

### GitHub App access

Every new installation is locked until an owner or admin approves it. Approve it
in **Repositories → Access management** (`POST /api/access/installations/{id}/approve`,
with an activation policy and a reason) or with the CLI; the procedure and the
choice between automatic and explicit activation are in
[Set up and share the GitHub App](GITHUB_APP_PILOT.md#4-approve-the-installation).

GitHub's **All repositories / Only select repositories** grant is separate from
Review Agent's **All accessible / Only explicitly enabled** activation policy.
Automatic activation verifies each repository on its first review request;
manual denials stay blocked. Switching an installation to explicit activation
disables automatically activated repositories and keeps manually enabled ones.
Adding teams does not change an installation's activation policy.

The GitHub App card checks App authentication, required permissions and the
issue-comment event subscription, and links to the App settings and installation
page. **Check live GitHub status** reads one installation from GitHub, including
missing permissions; stored access state is labelled separately. A working App
does not prove webhook delivery or that a model request will succeed.

**Add repository** verifies one exact repository with a metadata-only
installation token before enabling it, in either GitHub scope mode, without
scanning or removing other repositories. Selected installations can also sync
their full repository inventory. **Disable reviews** stops new admission, blocks
automatic reactivation and keeps history. To revoke GitHub access as well,
remove the repository in the installation's GitHub settings; webhooks update the
stored state. GitHub's removal API does not accept installation tokens, so the
console links to GitHub instead
([modifying installed Apps](https://docs.github.com/en/apps/using-github-apps/reviewing-and-modifying-installed-github-apps)).

### Repository requests and ownership

Each repository has at most one owning team. Unassigned repositories are visible
only to owners, admins and global viewers.

1. A maintainer opens the team's **Repositories** tab, chooses **Request a
   repository**, and enters `owner/repository` or its HTTPS GitHub URL.
2. An owner or admin opens **Repository requests** and approves or rejects it.
   A rejection needs a written reason, which the team sees.
3. Approval verifies the current GitHub App grant, assigns ownership, enables
   reviews and records the decision in one transaction. Repeating an approval
   returns the stored decision. A competing owner or a newer revoked grant
   rejects it without partially enabling the repository.

If a repository was renamed while its request was pending, withdraw the request
and submit the current name. Owned repositories keep their identity through the
GitHub repository ID.

Owners and admins use **Repositories → Assign team** for unassigned repositories,
and can transfer a repository to another team or remove it from a team. A
transfer moves access to its retained reviews to the new team. Removal disables
reviews and detaches ownership; history stays available to platform roles.

### Who can request reviews

Users with GitHub `write` or `admin` access to a repository can always request a
review ([who can request a review](GITHUB_APP_PILOT.md#who-can-request-a-review)).
Under **Repositories → Who can request reviews**, a maintainer, admin or owner
can also allow a named personal GitHub account for one enabled repository. The
App verifies the account and stores its GitHub user ID, so a rename keeps the
grant. Listed users can post `/review` and `/review docs`; feedback commands
still need `write` or `admin`. The list grants nothing on GitHub and is separate
from console accounts. Adding and removing entries is audited; removal affects
new requests only.

## Documentation review

An owner switches documentation review on for the deployment in **Settings**.
This is the only switch: no Compose file passes
`REVIEW_AGENT_DOCUMENTATION_REVIEW_ENABLED`. Maintainers then choose Off, Manual or
Automatic in the team's **Documentation** tab. Repositories inherit the team
mode or keep an explicit override, and **Workspace → Documentation** shows which
applies. The console shows the effective mode, the affected repositories and
stale-save conflicts before saving. A repository's **Documentation** settings
show App readiness and a **Refresh** action that reads the default-branch
configuration; each review still resolves its own base policy. See
[documentation review](DOCUMENTATION_REVIEW.md).

## Review history and run actions

Choose a reporting period of 7, 30 or 90 days. Activity totals use each event's
time (publication time for published reviews, completion time for failures);
repository statistics and request lists filter by request start time.

| Metric | Meaning |
| --- | --- |
| PRs reviewed | Distinct pull requests with at least one published review. |
| Published | Reviews published in the period; reviewing the same PR twice counts twice. |
| Failed requests | Failed requests, including those later followed by a successful review. |
| Active now | Queued, running or publishing requests, regardless of age. |
| Latest failures | PRs whose newest request failed within the period. |

The **Pull requests** tab groups requests by pull request; each request has a
direct URL, and returning to the list keeps filters and position. The review
reader shows findings, reviewed commit, duration, coverage limitations and
failure explanations before the review body, and a history of all retained
requests for that PR. Execution details list the base commit, attempts,
timestamps, failure codes and diff inventory. A later success marks an earlier
failure as recovered. Publication does not mean the PR is approved.

The reader shows the stored original publication only after every part was
delivered, with links to the GitHub review comments and any suggested changes.
Later GitHub edits are not reflected. The body is bounded at 200,000 characters
and 100 GitHub links, with truncation marked. Markdown is sanitized; images and
form controls are omitted. The page refreshes about every ten seconds.

Each request shows recorded tokens (total, prompt, completion) and how many
started attempts reported usage. Missing usage shows **Not reported**; a partial
total shows **Partial**. A grouped PR row shows its latest request's usage, not
a total. Token counts come from Hermes responses, deduplicated per job and lease
generation, and include retries. They do not show billing, remaining quota or
cost, and earlier reviews are not backfilled.

**Finding decisions**, opened from a published review, records a human decision
on one exact finding occurrence. Intentional-by-design decisions require the
accepted ADR snapshot and matching path ([feedback and decisions](FEEDBACK_AND_DECISIONS.md)).
**Review quality** shows reporting cohorts and the feedback backlog; maintainers
and platform admins classify pending feedback.

**Review actions** on a request can release a delayed retry, cancel queued or
leased work, or fail stale work after checking its heartbeat and lease (stale
recovery needs an owner or admin). The confirmation captures the job generation,
status and availability time; a concurrent change rejects the action. Terminal
reviews and jobs awaiting publication cannot be cancelled. The state change and
its audit event commit together.

### Usage by team, repository and GitHub user

Owners and admins open **Administration → Usage** to rank review requests by
team, repository or requesting GitHub login, over 7, 30 or 90 days, by requests,
recorded tokens or published reviews. Choosing a GitHub user filters every
grouping until cleared. **Refresh** updates the report; it does not poll.
Maintainers, global viewers and integration credentials cannot read it.

- Requests are selected by start time (`[start, end)`). Each accepted request
  counts once; ignored triggers and duplicate deliveries add nothing. Published
  and failed counts reflect the current outcome; tokens include every reported
  attempt, even if reported after the period. Statistics, by contrast, count
  publications and tokens at their event time.
- Totals cover all matching groups before pagination. Repository and user counts
  are distinct within the scope, so do not sum them across groups. Requests
  without a saved login form an explicit unknown group.
- Team attribution follows current repository ownership: a transfer moves the
  repository's retained usage. Logins are grouped case-insensitively; a renamed
  GitHub account can appear under several logins.

## Audit log

The audit log records who changed an account, membership, repository request,
ownership, finding decision, run state or setting, with time, outcome and
reason. Console actions record their operation without asking for a written
reason; API clients send a reason field. Only owners and admins can open it, including a team's audit tab.
Owner/admin account changes and sensitive platform changes are visible to owners
only. Passwords, tokens and provider credentials are never recorded. The journal
is part of the PostgreSQL backup; it is not a tamper-proof external archive.

1. Choose a purpose and enter a justification of 10–500 characters. Access lasts
   30 minutes for the selected team or all teams. Every view and export records
   the actor, purpose, justification, filters, event count and range.
2. Search matches all supplied words in actor email, subject, reason, action and
   change details. Filter by action, outcome, actor account ID or time (`since`
   inclusive, `until` exclusive). Events are listed newest first; **View JSON**
   and **Copy JSON** show an event's exact values.
3. **Export matching events** downloads up to 1,000 events as JSON, CSV, JSON
   Lines or OpenTelemetry OTLP JSON; **Export next 1,000** continues. Exports read
   committed data per request and are not a point-in-time snapshot.
4. **End audit access** closes the session. Expiry, leaving the page or a change
   to the account's permissions requires a new justification.

Events keep the actor's email as it was at the time, with the stable account ID
and role. CSV prefixes formula-like cells with an apostrophe; use JSON or JSON
Lines for exact text. OTLP files follow the
[OpenTelemetry JSON encoding](https://opentelemetry.io/docs/specs/otlp/) and
[log data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/):
resource `service.name=review-agent-admin`, scope `review_agent.audit`,
`review_agent.audit.*` attributes and the reason as the body. To forward one,
post it to an authorized collector's OTLP/HTTP `/v1/logs` endpoint with
`Content-Type: application/json`; the console does not forward automatically.

API clients call `POST /api/audit/access` with `purpose` (`incident_investigation`,
`access_review`, `support`, `routine_review` or `other`) and `reason`, adding
`team_id` to the query for team access. Send the returned `id` as
`X-Audit-Access-ID` on every list or export in the same scope; it is bound to the
account and its current permissions. Exports return `X-Audit-Count` and
`X-Audit-Next-Before-ID`; pass the latter as `before_id` with the same filters.
`POST /api/audit/access/{id}/end` ends access early.

## Settings

### Save and apply policy

Settings starts from the deployment's environment values. Each save creates an
immutable PostgreSQL revision with actor and reason, and the latest revision
then owns those values. A concurrent edit returns a conflict and keeps your
draft. **Restore to editor** loads an older revision; saving it creates a new
one. Advanced scheduling, recovery, delivery and request bounds are under
**Advanced operational settings**.

The model is an exact name. The field suggests the models Hermes lists for the
shared account when provider control is configured; the provider accepts or
rejects the name when a review runs, and no other model is substituted. Provider,
model and reasoning effort apply when a new request is admitted and are frozen
into that request. Other values load when their service starts:

| Values | Service to restart |
| --- | --- |
| Active job limit, capacity retry delay, admission age, review attempt budget, default priority | `review-github-app-worker` |
| Concurrency, lease, heartbeat, retries, polling, recovery, priority aging, Hermes timeout | `review-worker` |
| Publication size, publication attempt budget, feedback instructions | `hermes-review` |
| Publication size, lease, heartbeat, retries, polling | `review-publisher` |
| Webhook payload bound, request concurrency, timeout | `review-admission` |
| Code graph enablement, embedding mode, GitHub gateway concurrency | `review-github-gateway` |

Drain and restart services with the normal deployment procedure. Settings shows
the last 50 service startups and the revision each loaded. To return to earlier
values, load an earlier revision and save it. An older application cannot read
revisions that contain newer fields, so roll back images and database together.

### Environment defaults and admin controls

Environment values are bootstrap defaults. After the first save, the saved
revision overrides these variables for every service that reads policy; a
revision saved before a control existed keeps the environment default for it.

| Managed in Settings | Variables |
| --- | --- |
| Model route, frozen per request | `REVIEW_AGENT_MODEL_PROVIDER`, `REVIEW_AGENT_MODEL`, `REVIEW_AGENT_REASONING_EFFORT` |
| Admission | `REVIEW_AGENT_ACTIVE_JOB_LIMIT`, `REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS`, `REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS`, `REVIEW_AGENT_JOB_MAX_ATTEMPTS`, `REVIEW_AGENT_JOB_PRIORITY` |
| Review worker | `REVIEW_AGENT_WORKER_CONCURRENCY`, `REVIEW_AGENT_JOB_LEASE_SECONDS`, `REVIEW_AGENT_JOB_HEARTBEAT_SECONDS`, `REVIEW_AGENT_HERMES_TIMEOUT_SECONDS`, `REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS`, `REVIEW_AGENT_JOB_RETRY_SECONDS`, `REVIEW_AGENT_JOB_POLL_SECONDS`, `REVIEW_AGENT_JOB_RECOVERY_SECONDS`, `REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE` |
| Publication | `REVIEW_AGENT_PUBLISH_MAX_BYTES`, `REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS`, `REVIEW_AGENT_PUBLICATION_LEASE_SECONDS`, `REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS`, `REVIEW_AGENT_PUBLICATION_RETRY_SECONDS`, `REVIEW_AGENT_PUBLICATION_POLL_SECONDS` |
| Features | `REVIEW_AGENT_FEEDBACK_ENABLED`, `REVIEW_AGENT_CODE_GRAPH_ENABLED`, `REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS`, documentation review (Settings only) |
| Request bounds | `REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES` (at most 2,097,152), `REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS`, `REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS`, `REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS` |

Code graph options also need the graph overlay, and OpenAI embeddings the
gateway's API key. Everything else stays deployment configuration and is never
stored in a revision: images, profile, networks, database credentials, GitHub
App identity and key, provider secrets and graph cache size. The admin service
itself reads these deployment-only values:

| Variable | Purpose |
| --- | --- |
| `REVIEW_AGENT_RUNTIME_DATABASE_URL` | Database connection (passed as the service's database URL). |
| `REVIEW_AGENT_ADMIN_PUBLIC_URL` | Exact HTTPS origin; required. |
| `REVIEW_AGENT_ADMIN_IMAGE` | Console image reference. |
| `REVIEW_AGENT_GITHUB_APP_ID` and the mounted App private key | Installation approval, status and inventory. |
| `REVIEW_AGENT_PROFILE` | Installed review profile used when enabling repositories. |
| `REVIEW_AGENT_OIDC_ISSUER`, `REVIEW_AGENT_OIDC_CLIENT_ID`, `REVIEW_AGENT_OIDC_CLIENT_SECRET`, `REVIEW_AGENT_OIDC_NAME`, `REVIEW_AGENT_SCIM_TOKEN` | [Organization sign-in and SCIM](#organization-sign-in-and-scim). |
| `REVIEW_AGENT_EMAIL_SECRET_KEY` | Encrypts the saved SMTP password ([self-registration](#self-registration)). |
| `REVIEW_AGENT_DOKPLOY_URL`, `REVIEW_AGENT_DOKPLOY_COMPOSE_ID`, `REVIEW_AGENT_DOKPLOY_API_KEY` | [Dokploy container state](#dokploy-container-state). |
| `REVIEW_AGENT_HERMES_CONTROL_TOKEN` | Provider control, from `compose.providers.yaml` ([managed model connections](#managed-model-connections)). |
| `REVIEW_AGENT_CONNECTIONS_FILE` | Catalog of additional model runtimes ([provision another connection](#provision-another-connection)). |

Every variable, its default and the service that reads it are listed in the
[configuration reference](OPERATIONS.md#configuration-reference).

## Managed model connections

A model connection is a Hermes runtime with its own credential storage. The
deployment has one shared connection; an owner can register more and assign one
to a team ([what this means for teams](TEAM_ACCESS_AND_MODEL_ACCOUNTS.md)).
Review Agent owns permission checks, assignment, scheduling and audit; Hermes
owns authentication, credential refresh and provider calls. The browser never
receives OAuth access or refresh tokens.

### Enable provider control

Include `compose.providers.yaml` after `compose.admin.yaml`, set a separate
random `REVIEW_AGENT_HERMES_CONTROL_TOKEN` in deployment secrets, and keep all
overlays on later updates:

```bash
docker compose -f compose.yaml -f compose.admin.yaml -f compose.providers.yaml config --quiet
```

The overlay enables the Hermes dashboard on `127.0.0.1:9119` and a
`review-provider-control` service in the Hermes network namespace on private
port `9120`. That service accepts only the control token and exposes provider
status, account quota, model options, runtime diagnostics and Codex device login.
It also reads Hermes' `/health/detailed`, `/v1/capabilities`,
`/v1/review-agent/status` and `/v1/review-agent/quota/{provider}` over loopback
with `API_SERVER_KEY`, which is not passed to the admin service. Recreate Hermes
and its provider-control service in the same Compose operation. Health shows
these engine checks separately from worker heartbeats and container state; they
do not call the model provider. See the
[pinned Hermes API contract](https://github.com/NousResearch/hermes-agent/blob/v2026.8.31/website/docs/user-guide/features/api-server.md).

Without provider control or Dokploy, the console reports those parts as
unavailable; history, quality and database-backed controls still work.

### Connect an account

1. Open **Model connections** and select the connection. Pause it, let active
   work finish, and finish or cancel queued reviews.
2. Choose **Connect Codex**, open the OpenAI verification page and enter the
   one-time code. Only the person who started the login can poll or cancel it;
   their permission is rechecked on each step. A reload resumes the pending
   login; the one-time code is not stored in the database.
3. The connection records the account and stays paused. Add the models teams may
   choose (Hermes suggests the account's models), then choose **Enable**.

For Anthropic, configure an authorized API key through Hermes's terminal, then
choose **Record current accounts** while the connection is paused and drained.
The console offers no Claude consumer-plan login and reports no Anthropic quota.

Each connection holds at most one account per provider. Changing the account
increments its recorded revision. The worker and Hermes check the frozen
connection, account revision, lease, model and reasoning before inference, so
queued reviews never adopt a replacement account. Pausing after a claim but
before inference returns the job without using an attempt. Implicit provider
fallback is disabled (`auxiliary.allow_fallback: false` in the managed profile).

If a login times out or is interrupted, the connection needs attention. An owner
stops the old Hermes process, its dashboard and provider-control service,
restarts the complete runtime, and confirms recovery before recording accounts.

Who manages a connection: owners manage the shared connection; admins and the
owning team's maintainers manage a team-owned connection (login, enable, pause,
record accounts). Owners register connections. Owners, and admins for a
team-owned connection, edit allowed models and concurrency and retire a paused,
drained, unassigned connection. Admins assign a connection and set team capacity
in the team's **Models** tab; maintainers pick the team's model and reasoning
from the connection's allowed choices or inherit the deployment default.

### Quota and concurrent reviews

Each Codex connection shows the account's plan, actual window durations,
remaining percentage, reset times, limit buckets and available reset credits, as
reported by Hermes. Unknown values stay unknown; a failed refresh keeps the last
observation marked stale. Reset credits are read-only. Quota includes other uses
of the same provider account.

Hermes refreshes quota after five minutes while the page or a worker uses the
connection, allows a manual refresh (`?refresh=true` on
`GET /api/model-connections/{id}/quota/{provider}`) once per 30 seconds, and
backs off failed reads from 30 seconds to five minutes. Provider reads time out
after 15 seconds and are limited to 256 KiB. Review start waits at most five
seconds for a refresh, then uses the cached observation.

When Codex reports that account usage is disallowed, the review returns to its
queue without using an attempt and all queued work on that account waits for one
shared deadline, shown in history and in Operations' delayed totals. After the
deadline, a fresh quota check decides; only a fresh allowance clears the wait.

**Maximum concurrent reviews** applies per connection and per team (across
connections); both default to four. Lowering a cap lets active work finish and
limits new claims. Worker capacity is an additional bound, and one repository
runs one review at a time. Eligible teams take turns, with priority aging within
a team. Unfinished remote executions keep their capacity until Hermes reports
completion; lease expiry or cancellation does not release them. If the
unfinished count persists after reviews stop, an owner pauses the connection,
restarts its complete runtime, and chooses **Recover after a runtime restart**.

### Provision another connection

Teams that share an account use the shared runtime. Independent credentials need
another Hermes runtime, worker and provider-control service using the same
application image, managed profile, database and network roles. The console
registers these services; it does not create containers.

1. Give the runtime its own empty credential volume at `/opt/data` and run the
   profile installer against it. Its worker mounts the same volume read-only. Do
   not copy another runtime's credential store or supply ambient credentials.
2. Set `REVIEW_AGENT_MODEL_CONNECTION` to one stable key, such as `payments`, on
   both the Hermes process and its worker. Point the worker's
   `REVIEW_AGENT_HERMES_CHAT_URL` at that runtime's private
   `/v1/review-agent/review` endpoint. Give the runtime, worker and companion a
   matching dedicated `API_SERVER_KEY`.
3. Configure its loopback dashboard and provider-control service as in
   `compose.providers.yaml`, with a separate control token, in that runtime's
   network namespace. The admin service receives only the control token.
4. Mount a JSON catalog read-only into `review-admin`, set
   `REVIEW_AGENT_CONNECTIONS_FILE` to its path, store the referenced token in
   the admin service's secrets, and restart the admin service:

   ```json
   [
     {
       "key": "payments",
       "control_url": "http://hermes-payments:9120",
       "token_env": "PAYMENTS_HERMES_CONTROL_TOKEN"
     }
   ]
   ```

5. As an owner, choose **Add connection**, select the runtime, name it, select
   the owning team and define allowed models. Connect or record its account,
   enable it, then assign it in the team's **Models** tab.

The catalog accepts at most 1,000 entries in 256 KiB, with unique keys and
control origins. Console users cannot supply endpoints or tokens. Retiring a
connection keeps its audit and usage history and requires a drained queue and
no assigned teams; then remove its services through the deployment platform.

## Application integrations

Owners and admins give an application read access under **Settings →
Integrations**: up to 100 teams, or **Entire deployment**, which includes
unassigned repositories and future teams. Outcome metadata and aggregate reports
are included; **Allow published review content** is a separate permission.
Integrations cannot manage users, reviews, providers, settings or deployment.

1. Choose the scope and an expiry, and create the integration.
2. Copy the credential into the consumer's secret manager. It is shown once;
   Review Agent stores only its SHA-256 digest. If it is lost, revoke the
   integration and create another.
3. To rotate or change access, create a replacement, update the consumer, and
   revoke the old one. Permissions are fixed at creation.

Expiry and revocation are checked in every request; a read already running may
finish, and downloaded data cannot be retracted. Creation, revocation and every
successful read are audited without the credential or review text. Each read
adds an audit event, so set a retention window for read receipts with the
[operator retention command](./OPERATIONS.md#retention-ownership) before
enabling scheduled consumers.

| Endpoint | Result |
| --- | --- |
| `GET /api/v1/overview` | Activity, publication timing, token telemetry and its recording gaps. |
| `GET /api/v1/repositories` | Repository counts and activity, in ascending repository ID order. |
| `GET /api/v1/reviews` | Review outcome, exact subject identifiers, usage and coverage, in descending run ID order. |
| `GET /api/v1/reviews/{run_id}/content` | Bounded published text and GitHub links; needs the content permission. |
| `GET /api/v1/quality` | Recorded quality signals, cohorts and explicit denominators. |
| `GET /api/v1/openapi.json` | The integration OpenAPI schema. |

Send `Authorization: Bearer <credential>`. A browser session does not work on
these endpoints, and the credential does not work on console endpoints. The
console's **API reference** includes these routes with Swagger's **Authorize**
control.

```sh
curl --fail-with-body --get \
  --header "Authorization: Bearer ${REVIEW_AGENT_INTEGRATION_TOKEN:?}" \
  --data-urlencode 'start=2026-09-01T00:00:00Z' \
  --data-urlencode 'end=2026-09-08T00:00:00Z' \
  'https://reviews.example.org/api/v1/overview'
```

- Reports require `start` and `end` with offsets, a positive interval of at most
  366 days, `[start, end)`, answered in UTC. `window_days` rounds up to whole
  days. Metrics match the console; the `active` review filter ignores the
  interval.
- Responses wrap the report in `data` with `metrics_version: 1`, `generated_at`
  and `evidence_scope: "retained_records"`. Missing token evidence is null.
- Page limits are 1–100. Send `next_after_id` as `after_id` (repositories) or
  `next_cursor` as `before_id` (reviews), and keep the first page's
  `watermark_id`, interval, scope and filters. The watermark excludes newer IDs
  but is not a snapshot: reread an overlapping interval and deduplicate by ID.
- `team_id` narrows a grant, never widens it. A repository transfer removes
  access under the old team's grant, including older review text.
- Errors: `401` with a Bearer challenge for an invalid or expired credential,
  `403` without the content permission, `404` for scope or content outside the
  grant, `422` for invalid bounds, `503` when the database is unavailable.
  Responses use `Cache-Control: no-store`.

## Console API

The console uses the same authenticated API it documents. Open **API reference**
in the console, or `/api/docs`, for Swagger UI under the current session; the
schema is at `/api/openapi.json`. The frontend contract is `admin/openapi.json`
and the generated `admin/src/api.generated.ts`.

| Endpoint | Access | Data |
| --- | --- | --- |
| `GET /api/version` | Signed-in users | Release version and full source revision. |
| `GET /api/overview` | Team reader or global role | Lifetime and period totals, UTC daily publications, publication latency, recent failure reasons, workers and capacity. |
| `GET /api/usage` | Owner or admin | Grouped usage and totals. Supports `team_id`, `repository`, case-insensitive exact `requester`, `days` or `start`/`end`, `dimension=team\|repository\|requester`, `sort=requests\|total_tokens\|published_requests`, literal `search`, `limit` and `offset`. |
| `GET /api/repositories` | Team reader or global role | Paginated repositories with documentation modes, `total` and aggregate `totals`. |
| `GET /api/pull-requests` | Team reader or global role | Paginated PR groups with matching and lifetime request counts and the latest matching request. |
| `GET /api/history` | Team reader or global role | Paginated requests, `total` before the cursor, token usage and quota waits. |
| `GET /api/history/{run_id}` | Team reader or global role | One request, its published Markdown and GitHub links, and up to 20 requests for the same PR (`before_id` pages them). |
| `GET /api/operations` | Owner or admin | Worker presence and capacity, active leases, and webhook, review and publication queue counts. |
| `GET /api/operations/events` | Owner or admin | Process and review events, filtered by `worker_id` and `before_id`. |
| `GET /api/email`, `PUT /api/email` | Owner | Redacted SMTP settings; save an audited revision. |
| `POST /api/email/test` | Owner | Send a test email to the signed-in owner. |
| `GET /api/registration`, `PUT /api/registration` | Owner | Self-registration allowlist, with a revision precondition. |
| `GET /api/users` | Owner or admin | `items`, `total`, `admin_count`, `disabled_count`, `has_more`. |
| `GET /api/access/installations`, `GET /api/access/repositories` | Owner or admin | GitHub App access inventory and capability state. |
| `GET /api/access/connection` | Owner or admin | Live App authentication, permission and event checks, and App links. |
| `GET /api/access/installations/{id}/status` | Owner or admin | Live installation scope, status, permission gaps and settings URL. |
| `POST /api/access/installations/{id}/approve` | Owner or admin | Approve a locked installation with `{policy, reason}`. |
| `POST /api/access/installations/{id}/sync` | Owner or admin | Refresh a selected installation's repository inventory. |
| `POST /api/access/repositories/{id}/enable`, `POST /api/access/repositories/{id}/disable` | Owner or admin | Enable or disable reviews for one repository. |
| `POST /api/access/repositories/onboard` | Owner or admin | Verify and enable one named repository with `{repository, profile, reason}`. |
| `GET /api/repositories/{id}/review-requesters` | Scoped reader, owner or admin | Additional requesters with `can_manage`, `limit` and `after_user_id`. |
| `POST /api/repositories/{id}/review-requesters` | Team maintainer, owner or admin | Verify a personal GitHub account from `{login}` and grant it review requests. |
| `POST /api/repositories/{id}/review-requesters/{github_user_id}/revoke` | Team maintainer, owner or admin | Revoke that grant; GitHub write/admin access still authorizes. |
| `GET /api/quality`, `GET /api/quality/feedback` | Team reader or global role | Quality cohorts (at most 200 rows, truncation flagged) and retained feedback. |
| `GET /api/history/{run_id}/findings`, `GET /api/findings/{fingerprint}` | Team reader or global role | Published finding occurrences and decision history. |
| `POST /api/findings/{fingerprint}/decisions`, `POST /api/quality/feedback/{id}/triage` | Team maintainer, owner or admin | Audited finding decisions and feedback triage. |
| `GET /api/history/{run_id}/controls`, `POST /api/history/{run_id}/actions` | Team maintainer, owner or admin | Run controls and their preconditions; stale-run recovery needs owner or admin. |
| `GET /api/settings`, `PUT /api/settings` | Owner | Current policy, revision history, startup records and conditional saves. |
| `GET /api/deployment` | Owner | Dokploy container state. |
| `GET /api/providers`, `GET /api/providers/models` | Owner | Hermes provider state and model catalog. |
| `GET /api/providers/runtime` | Owner | Hermes readiness, version, active agents, shutdown state, API support and default model. |
| `GET /api/model-connections`, `GET /api/model-connections/{id}` | Scoped member, owner or admin | Shared or team connections, recorded accounts and permitted controls. |
| `POST /api/model-connections`, `GET /api/model-connections/runtimes` | Owner | Register a runtime from the catalog. |
| `GET /api/model-connections/{id}/models` | Scoped member, owner or admin | Models Hermes lists for the connection's accounts. |
| `PATCH /api/model-connections/{id}`, `POST /api/model-connections/{id}/retire` | Owner; admin for a team connection | Edit allowed models and concurrency, or retire a connection. |
| `POST /api/model-connections/{id}/enabled`, `POST /api/model-connections/{id}/reconcile` | Owner; admin or owning maintainer for a team connection | Pause or enable dispatch, or record an account change. Recovery after an interrupted operation needs an owner and a runtime restart. |
| `GET /api/model-connections/{id}/runtime` | Connection manager | Redacted account observation from the Hermes process. |
| `GET /api/model-connections/{id}/quota/{provider}` | Scoped member, owner or admin | Cached quota; `refresh=true` requests a rate-limited refresh. |
| `POST /api/model-connections/{id}/login`, `GET …/login/{operation}`, `POST …/{operation}/poll`, `POST …/{operation}/cancel` | Connection manager; operations bound to their initiator | Codex device login through Hermes. |
| `GET /api/teams/{id}/model-policy`, `PUT /api/teams/{id}/model-policy` | Team member reads; maintainer or admin writes | The team's model route. Connection and capacity changes need an admin. |
| `GET /api/teams`, `GET /api/teams/{id}` | Team reader or global role | Teams and the caller's team role. |
| `POST /api/teams`, `PATCH /api/teams/{id}` | Owner or admin | Create a team or change its metadata. |
| `GET /api/teams/{id}/members` | Team reader or global role | Membership list. |
| `PUT /api/teams/{id}/members`, `POST /api/teams/{id}/members/{user_id}/remove` | Team maintainer, owner or admin | Add, change or remove a member with a reason. |
| `GET /api/teams/{id}/repositories`, `GET /api/repository-requests` | Team reader or global role | Owned repositories and requests. |
| `POST /api/teams/{id}/repository-requests`, `POST /api/repository-requests/{id}/withdraw` | Team maintainer, owner or admin | Submit or withdraw a request. |
| `POST /api/repository-requests/{id}/approve`, `POST /api/repository-requests/{id}/reject` | Owner or admin | Approve or reject a request. |
| `PUT /api/repository-ownership/{id}`, `POST /api/teams/{id}/repositories/{repository_id}/remove` | Owner or admin | Assign or transfer ownership, or remove a repository from a team. |
| `POST /api/audit/access`, `POST /api/audit/access/{id}/end` | Owner or admin | Start or end justified audit access. |
| `GET /api/teams/{id}/events`, `GET /api/audit` | Owner or admin with `X-Audit-Access-ID` | Keyset-paginated events; `/api/audit` filters by text, action, actor, outcome, time and team. |
| `GET /api/audit/export` | Owner or admin with `X-Audit-Access-ID` | Up to 1,000 events as JSON, CSV, JSON Lines or OTLP JSON. |

Report parameters:

- Overview, repositories, PR groups and history accept `days` (1–90, default
  30) or both `start` and `end` as RFC 3339 timestamps with offsets. An explicit
  range overrides `days`, spans at most 366 days, includes the start and
  excludes the end. Responses return `window_start` and `window_end`.
- Overview counts each metric at its own event time: request start, GitHub
  publication, failure completion, token recording. Median and p95
  request-to-publication include queueing, retries and delivery. Lifetime counts
  cover retained records since `retained_since`.
- List limits are 1–100. Repository, usage and account offsets stop at 10,000;
  history, PR groups and events use descending ID cursors.
- Finding, review and feedback responses say whether the caller can act on that
  resource; clients must still handle access changing before submission.

## Health

The Health page combines worker presence, queues, engine checks and, when
configured, container state.

- Workers send a heartbeat every 30 seconds, including while idle, and show as
  `unresponsive` after 90 seconds without one; that does not prove the container
  stopped. A graceful shutdown reports `draining`, then `stopped`. Lists show at
  most 100 workers. `active_leases` is null when several retained processes
  share a lease-owner name. Only processes that report to this database appear.
- Queue `due` means the availability time has passed; repository scheduling and
  authorization may still prevent a claim. `delayed`, `next_available_at`,
  `oldest_waiting_at`, expired leases and failed work separate a backlog from a
  missing consumer.
- Structured events record process lifecycle and review handoff with run and job
  IDs, never stdout, prompts, model reasoning or provider responses. Each
  instance keeps its latest 1,000 events; instances unseen for seven days are
  removed with their events. Use Dokploy for full container logs.

### Dokploy container state

Set `REVIEW_AGENT_DOKPLOY_URL` to the Dokploy origin,
`REVIEW_AGENT_DOKPLOY_COMPOSE_ID` to this stack's ID, and
`REVIEW_AGENT_DOKPLOY_API_KEY` to a server-side API key with the narrowest read
access your Dokploy installation supports, then restart `review-admin`. The
browser receives only container identity, state, status and a dashboard link; it
cannot select another application or run Dokploy operations. See the
[Dokploy API](https://docs.dokploy.com/docs/api).

## Organization sign-in and SCIM

The console supports one generic OpenID Connect provider and a basic SCIM 2.0
Users API. Both are off by default, and local email/password sign-in stays
available. Keep a working local owner account before enabling either.

Register a confidential OIDC client with this callback, replacing the origin with
`REVIEW_AGENT_ADMIN_PUBLIC_URL`:

```text
https://reviews.example.com/api/auth/oidc/callback
```

Use the authorization code flow with PKCE S256, `client_secret_basic` and a
query-mode callback. The ID token must contain `openid email` claims, with
`email_verified: true` for first linking or self-registration. The issuer's
HTTPS discovery document must name that exact issuer and HTTPS authorization,
token and JWKS endpoints. RSA, RSA-PSS and ECDSA signatures are accepted;
unsigned and symmetric ones are rejected. Discovery and keys are fetched on each
sign-in, so key rotation needs no restart. Provider responses are limited to
256 KiB and token strings to 16 KiB.

Set these on `review-admin` from the deployment's secret store; the admin
overlay passes them through:

| Variable | Value |
| --- | --- |
| `REVIEW_AGENT_OIDC_ISSUER` | Exact HTTPS issuer URL, including its path and any trailing slash. |
| `REVIEW_AGENT_OIDC_CLIENT_ID` | Registered client ID. |
| `REVIEW_AGENT_OIDC_CLIENT_SECRET` | Client secret. |
| `REVIEW_AGENT_OIDC_NAME` | Login button label; defaults to `Organization sign-in`. |
| `REVIEW_AGENT_SCIM_TOKEN` | Optional random credential of 32–512 characters; setting it enables SCIM for the configured issuer. |

Generate the SCIM credential in your secret manager, or with
`openssl rand -hex 32` in a private terminal, and give it only to the directory
connector. It cannot sign in to the console or read reports. To rotate it,
replace the value and restart the admin service. Incomplete identity
configuration prevents startup.

### Link accounts and sign in

- An existing account signs in with its password, opens **Your account**, and
  chooses **Link** under Organization sign-in. The provider must return the same
  verified email. Email matching alone never links an account. A pending link
  expires after five minutes or when the session or account access changes.
- SCIM-created accounts link on their first organization sign-in with a matching
  verified email. They start as Members with no team access; provider group and
  role claims never grant access. Accounts promoted to a privileged platform role
  must link explicitly.
- After linking, the issuer and subject identify the account; a later email
  change does not select another account. Changing the issuer does not migrate
  linked identities.
- OIDC sign-in issues the same eight-hour console session as password sign-in.
  Provider tokens are not stored. Logout does not sign the user out of the
  provider.

### Configure directory provisioning

Use `https://reviews.example.com/scim/v2` as the base URL with
`Authorization: Bearer <credential>` and `application/scim+json`. Discovery is
at `ServiceProviderConfig`, `ResourceTypes` and `Schemas`. The only resource is
**User**:

| Attribute | Meaning |
| --- | --- |
| `userName` | Required email address; unique, case-insensitive. |
| `externalId` | Optional directory identifier, up to 255 characters, unique within this directory. |
| `active` | Boolean, default true. False disables password and OIDC sign-in and revokes sessions. |

`POST Users` creates an account. `GET Users` and `GET Users/{id}` return only
accounts this connector created. `PUT Users/{id}` replaces the mutable fields;
`PATCH` accepts add and replace for them and remove for `externalId`, committed
together. Lists support `startIndex`, `count` up to 100, and the exact filters
`userName eq "person@example.com"` or `externalId eq "directory-id"`. Groups,
bulk requests, sorting, ETags and the enterprise extension are not supported.
The connector never writes passwords, platform roles or team memberships.

SCIM cannot adopt existing local accounts or change privileged accounts.
Duplicates return `409` with `scimType: uniqueness`. `DELETE` revokes sessions,
disables the account and hides it from SCIM, keeping its audit and team history;
deleted identifiers and emails are not reused. Requests are limited to 64 KiB
and 100 PATCH operations. The contract follows
[OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html),
[RFC 7643](https://www.rfc-editor.org/rfc/rfc7643.html) and
[RFC 7644](https://www.rfc-editor.org/rfc/rfc7644.html).

### Verify and recover

1. Confirm local owner sign-in.
2. Provision a test Member through SCIM, sign in through OIDC, and confirm that
   access appears only after team assignment.
3. Deactivate it through SCIM and confirm that its open session loses access.
4. Check the audit log for provisioning, linking, sign-in and deactivation.

If the provider is unavailable, use password sign-in. To turn identity
integration off, clear the OIDC and SCIM variables and restart the admin
service; local accounts, sessions and identity records stay. Disable affected
accounts if their sessions must end too.

## Self-registration

Owners open **Users & roles → Registration allowlist**, add allowed email
domains or addresses (one per line, up to 100 per list) and enable
self-registration. It starts disabled with empty lists, and empty lists admit
nobody. `example.org` and `@example.org` both allow that exact domain;
subdomains are listed separately. Matching is case-insensitive.

Email registration needs SMTP:

1. Set `REVIEW_AGENT_EMAIL_SECRET_KEY` in the console's deployment secrets and
   restart the console once, before saving an SMTP password. Generate the key
   with the command below and back it up separately from the database.

   ```sh
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

2. As an owner, open **Settings → Email delivery (SMTP)** and enter the server,
   port, sender and optional username and password. Choose STARTTLS (usually
   587) or implicit TLS (usually 465); both verify the certificate.
3. Save, then **Send test to me**. Tests work while delivery is disabled and are
   limited to one per minute. Enable delivery when the test arrives. Changes take
   effect without a restart.

The password is stored encrypted and never returned. Leave the field blank to
keep it; changing the server, port, TLS mode or username requires re-entering
it. For an unauthenticated relay, clear the username and choose **Remove saved
password**; no key is needed. If the key is lost, restore it or save the
password again. The console sends only registration and test emails; there is
no email password reset.

When registration is open, **Register** appears on the login page. An allowed
address receives a link to choose a password. Links expire after 30 minutes,
work once, and carry the token in the URL fragment; the server stores only its
hash and checks the allowlist again before creating the account. One request
per address per minute; a new link replaces the old one. The console sends at
most 30 emails per minute and holds at most 1,000 pending addresses. Responses
do not reveal whether an address is allowed or registered.

A configured OIDC provider can also register allowed addresses without SMTP when
its identity carries a verified email. Both methods create Members without team
access. Closing registration or removing an allowlist entry affects new accounts
only.

## Accounts and recovery

Sign-in uses FastAPI Users with Argon2 password hashes and revocable PostgreSQL
sessions. Session cookies are HTTPS, HttpOnly and SameSite Strict, and expire
after eight hours. Users change their password under **Your account**; for
accounts an administrator creates, share the initial password privately.
Password resets are done by an administrator.

Owners disable or reset any account; admins manage Member and Global viewer
accounts. Account changes revoke sessions, and the last active owner cannot be
demoted or disabled. Keep a second owner for recovery; the bootstrap command
never replaces existing accounts.

Accounts and sessions are in the PostgreSQL backup. To remove the console,
remove its route and service and keep the database; its tables can stay. For a
version rollback, follow the database recovery procedure; do not reverse
migrations by hand.

The console's application pool uses at most four connections and sixteen
waiting queries; its authentication pool uses two connections. Include both in
the deployment's PostgreSQL connection budget.
