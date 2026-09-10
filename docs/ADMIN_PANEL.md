---
sidebar_label: Admin panel
slug: /admin-panel
title: Review Agent operator console
description: Inspect reviews, manage repository access and finding decisions, and apply deployment policy through an authenticated console.
status: current
last_verified: 2026-09-10
---

# Review Agent operator console

The optional console provides team workspaces for review activity, repositories,
quality evidence, and membership. Owners and admins also manage platform access
and inspect the audit log. Platform settings and provider credentials belong to
owners. GitHub remains the source of review requests and the destination for
published reviews.

The console requires a qualified release that supplies both
`review-agent` and `review-agent-admin` digests. Do not combine this panel with
an older migration image: the current admin API requires PostgreSQL schema 28.
Releases through v0.4.0-rc.4 do not include the
console. For a source build, build both images from the same checkout and run
its migrations before starting the services.

After this upgrade, do not roll back the console image alone. Earlier consoles
interpret ordinary accounts as global viewers and do not enforce team access or
the owner/admin boundary. Use a forward fix, or the coordinated image and verified
database recovery procedure in [Deployment](./DEPLOYMENT.md#upgrade-and-roll-back-production).

## Teams, roles, and repository ownership

Each repository has one owning team. Accounts receive a platform role separately
from their role in each team:

| Role | Access |
| --- | --- |
| Owner | All teams and platform operations, including privileged accounts, provider credentials, settings, and the complete console audit log. |
| Admin | All teams, repository ownership and approval, operational controls, and member/global-viewer account management. Cannot change owner/admin accounts or sensitive platform settings. |
| Member | Access through team memberships. A team viewer reads review data; a team maintainer also manages membership, requests repositories, triages feedback, records finding decisions, and retries or cancels reviews. |
| Global viewer | Explicit read access across the deployment. Retained for existing viewer accounts during migration. |

New accounts default to Member. Create a team in **Teams**, then add an existing
active account by its exact email as Viewer or Maintainer. One-team members enter
that workspace automatically and see its name without a selector. Members in
several teams can search and switch teams. Owners and admins can view all teams
or select one; team and period filters follow navigation. Platform administration
pages identify their scope even when a team filter is retained for navigation.

Team permissions apply on the server to lists, search, totals, quality cohorts,
direct links, and actions. Members cannot inspect unassigned repositories. A
repository transfer moves access to its retained review content to the new team.
Membership and ownership changes invalidate the affected accounts' cached views;
subsequent server operations always resolve current access. Team views omit
deployment-wide worker and capacity figures.

In a team's Repositories tab, maintainers submit `owner/repository` or its HTTPS
GitHub URL. Owners and admins approve or reject requests in
**Repository requests**. Approval verifies the current GitHub App grant, assigns
ownership, enables reviews, and records the decision in one database transaction.
Repeating the same approval returns the stored decision. A competing owner or
newer revoked grant rejects approval without partially enabling the repository.
If the repository was renamed while a request was pending, withdraw it and submit
its current name. Existing owned repositories retain identity through their
GitHub repository ID.

For retained repositories without an owner, use **Repositories → Assign team**
to select the owning team. This preserves history and the current activation state.
Owners and admins can transfer a repository to another team, or remove it from a
team. Removal disables reviews and detaches ownership while retaining history
for platform access. GitHub grants remain independently managed through the App
installation. Adding teams does not change an installation's existing automatic
activation policy.

**Audit log** records who changed an account, membership, repository request,
ownership, review decision, run state, or settings, together with a timestamp
and reason. Admins see team, repository, and ordinary account changes regardless
of the actor's role. Owner/admin account changes and sensitive platform changes
are visible only to owners. Only owners and admins can open audit pages, including
the audit tab within a team. Passwords,
tokens, and provider credentials are omitted. The journal is part of the normal
database backup; it is not a tamper-proof external log archive.

### Search and export audit events

Ordinary console actions record their operation automatically; they do not ask
for a written reason. Viewing audit logs still requires the purpose and
justification described below. API clients retain their existing reason fields.

Before opening the log, select a purpose and enter a justification of 10–500
characters. Access lasts 30 minutes for the selected team or all teams. Each view
and export records the actor, purpose, justification, filters, event count, and
returned event range. Owner access records remain owner-only because their
filters can refer to privileged events. **End audit access** closes the session;
expiry or a change to the account's permissions requires a new justification.
Leaving the page clears the displayed records and requires a new justification
on the next visit.

Use **View JSON** on an event to inspect its complete formatted JSON, including
the actor, reason, outcome, and change details. **Copy JSON** copies the exact
event values.

Search matches all supplied words in the actor email, subject, reason, action,
and recorded change details. Narrow results by action, outcome, actor account ID,
or time. The form uses your local time and sends timezone-aware timestamps;
`since` is inclusive and `until` is exclusive. Team audit tabs apply the same
filters within that team. Global audit covers all teams. Events appear newest
first, with **Older events** for the next page.

New events preserve the actor email as recorded at the time of the action,
alongside the stable account ID and role. Migration 23 copies the current account
email into older events where available; it cannot reconstruct earlier email
changes. Search uses PostgreSQL's full-text index, with no separate search service.

Choose **Export matching events** to download JSON, CSV, JSON Lines, or
OpenTelemetry OTLP JSON. A file contains up to 1,000 matching events and follows
the same owner/admin visibility rules as the page. When more events remain, use
**Export next 1,000**. Files start with the newest matching events independently
of the page currently displayed. For API clients, pass the response's
`X-Audit-Next-Before-ID` as `before_id` with the same filters. `X-Audit-Count`
reports the number of events in the file. Each page reflects committed data when
that request runs; exports are not a point-in-time database snapshot.

API clients first call `POST /api/audit/access` with a `purpose` and `reason`.
Purposes are `incident_investigation`, `access_review`, `support`, `routine_review`,
and `other`. Include `team_id` in the query for team access. Send the returned
`id` in `X-Audit-Access-ID` on every list or export request, using the same team
scope. The ID is bound to the authenticated account and its current permissions;
it does not replace the login session. The legacy team events endpoint also
requires it. Call `POST /api/audit/access/{id}/end` in the same scope to end access
early. Access grants and reads use the existing journal and its operation index.

JSON includes the event list and continuation cursor. JSON Lines contains one
event per line. CSV quotes fields and prefixes formula-like cells with an
apostrophe so spreadsheet applications treat them as text; use JSON or JSON Lines
when exact text preservation is required.

OTLP files use the [OpenTelemetry JSON encoding](https://opentelemetry.io/docs/specs/otlp/)
and [log data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/):
resource `service.name=review-agent-admin`, scope `review_agent.audit`, nanosecond
event and observation timestamps, numeric severity, an event name, and typed
`review_agent.audit.*` attributes. The reason is the log body. An operation UUID
correlates related audit records; it is not presented as a trace or span ID.
An administrator can forward the file to an authorized collector's OTLP/HTTP
`/v1/logs` endpoint using `Content-Type: application/json`. The console prepares
the file without contacting an external collector. Automatic forwarding and
application tracing are not configured by this feature.

## What the numbers mean

Choose a reporting period of 7, 30, or 90 days. Activity totals use each event’s
time: publication time for published reviews and completion time for failures.
Repository statistics and request lists filter by request start time:

| Metric | Meaning |
| --- | --- |
| PRs reviewed | Distinct pull requests with at least one published review. |
| Published | Reviews published in the period; reviewing the same PR twice counts twice. |
| Failed requests | Historical failed requests, including those followed by a successful review. |
| Active now | Queued, running, or publishing requests, regardless of their age. |
| Latest failures | PRs whose newest request failed within the selected period. |

Activity opens the request list. Its Pull requests tab groups matching requests
under each pull request before pagination; Statistics shows aggregate reports.
Open a PR's latest matching request to read its published review. The overview
shows findings and coverage alongside publication status; filtered historical
results are labeled accordingly. The reader has a compact history of all retained
requests for that PR, including requests outside the overview filters. Selecting
another request replaces the review. On narrow screens, use the request selector.
Each request has a direct URL; returning to history preserves its filters and
list position.

The reader displays the stored original publication after every publication part
has been delivered. It includes exact links to the GitHub review comments and any
separately published suggested changes. Later GitHub edits and discussion are not
reflected in the stored copy. Generated drafts and incomplete publications are
not displayed as published reviews. Retention may remove an older request.

Findings, reviewed commit, duration, coverage limitations, and failure explanations
appear before the review body. Execution details contain the base commit, worker
attempts, timestamps, failure codes, and diff inventory. A later success marks an
earlier failure as recovered without removing it from history. Published reviews
may have incomplete coverage; publication does not mean that a PR is approved.
The page refreshes about every ten seconds while open and shows read errors
with a retry control. Repository tiles aggregate all repositories matching the
search, including rows on other pages. Account tiles cover all accounts; the
email and role filters currently apply to the displayed account page.

## Reporting and operations API

The source candidate exposes the following authenticated endpoints. Open
**API reference** in the console, or `/api/docs`, for the self-hosted Swagger UI.
It uses the current account session and the same server permissions. The schema
is available at `/api/openapi.json`. The frontend uses `admin/openapi.json` and the generated
`admin/src/api.generated.ts` contract.

| Endpoint | Access | Data |
| --- | --- | --- |
| `GET /api/version` | Signed-in users | Baked release version and full source revision; the console footer shows the version and short revision. |
| `GET /api/overview` | Team reader or global role | Lifetime and selected-period totals, UTC daily publications, publication latency, recent failure reasons, and reporting review workers and capacity. |
| `GET /api/repositories` | Team reader or global role | Paginated repositories, `total` matching repositories, and aggregate `totals` across every matching repository. |
| `GET /api/pull-requests` | Team reader or global role | Paginated PR groups, total matching PRs, matching and lifetime request counts, and the latest matching request. |
| `GET /api/history` | Team reader or global role | Paginated requests, `total` matching requests before the cursor is applied, per-request token usage, and recorded account quota waits. |
| `GET /api/history/{run_id}` | Team reader or global role | Selected request, original published Markdown and GitHub links, plus up to 20 retained requests for the same PR. `before_id` pages that PR's history; selection is independent of the cursor and reporting period. Missing requests return 404. |
| `GET /api/operations` | Owner or admin | Worker presence and capacity, active leases, and webhook, review, and publication queue counts. |
| `GET /api/operations/events` | Owner or admin | Structured process and review events, with optional `worker_id` and `before_id` filters. |
| `GET /api/email`, `PUT /api/email` | Owner | Read redacted SMTP settings or save an audited revision. |
| `POST /api/email/test` | Owner | Test the saved revision by sending to the signed-in owner. |
| `GET /api/registration`, `PUT /api/registration` | Owner | Read or update the self-registration allowlist with a revision precondition. |
| `GET /api/users` | Owner or admin | `AccountPage`: `items`, `total`, `admin_count`, `disabled_count`, and `has_more`. |
| `GET /api/access/installations`, `GET /api/access/repositories` | Owner or admin | Bounded GitHub App access inventory and capability state. |
| `GET /api/access/connection` | Owner or admin | Live App authentication, permission and event checks, plus App management links. |
| `GET /api/access/installations/{id}/status` | Owner or admin | Live installation scope, status, permission gaps, and its GitHub settings URL. |
| `POST /api/access/repositories/onboard` | Owner or admin | Verify and enable one named repository using `{repository, profile, reason}`. |
| `POST /api/access/...` | Owner or admin | Installation approval and selected-inventory refresh; repository enablement and disablement. |
| `GET /api/quality`, `GET /api/quality/feedback` | Team reader or global role | Quality cohorts and paginated retained feedback. |
| `GET /api/history/{run_id}/findings`, `GET /api/findings/{fingerprint}` | Team reader or global role | Published finding occurrences and bounded decision history. |
| `POST /api/findings/{fingerprint}/decisions`, `POST /api/quality/feedback/{id}/triage` | Team maintainer, owner or admin | Audited human decisions and feedback triage. |
| `GET /api/history/{run_id}/controls`, `POST /api/history/{run_id}/actions` | Team maintainer, owner or admin | Available run controls, snapshot preconditions, and audit events. Stalled-run recovery requires owner/admin access. |
| `GET /api/settings`, `PUT /api/settings` | Owner | Current policy, bounded revision history, startup records, and conditional saves. |
| `GET /api/deployment` | Owner | Optional Dokploy container state for this application. |
| `GET /api/providers`, `GET /api/providers/models` | Owner | Optional Hermes provider state and model catalog. |
| `GET /api/providers/runtime` | Owner | Optional bounded Hermes readiness checks, version, active agents, shutdown state, API support, and default model. |
| `GET /api/model-connections`, `GET /api/model-connections/{id}` | Scoped member, owner or admin | Shared or team-owned connections, recorded accounts, and permitted controls. |
| `POST /api/model-connections`, `GET /api/model-connections/runtimes` | Owner | Register a pre-provisioned runtime from the server's trusted catalog. |
| `PATCH /api/model-connections/{id}`, `POST /api/model-connections/{id}/retire` | Owner; admin for a team-owned connection | Update allowed model choices or retire a paused, drained, unassigned connection. |
| `POST /api/model-connections/{id}/enabled`, `POST /api/model-connections/{id}/reconcile` | Owner; owning team maintainer or admin for a dedicated connection | Pause/enable dispatch or record an account change after draining. Recovery after an interrupted remote operation requires an owner and runtime restart. |
| `GET /api/model-connections/{id}/runtime` | Connection manager | Redacted current account observation from the owning Hermes process. |
| `GET /api/model-connections/{id}/quota/{provider}` | Scoped member, owner or admin | Cached account quota with freshness, actual windows and buckets; `refresh=true` requests a rate-limited refresh. |
| `POST /api/model-connections/{id}/login`, `GET /api/model-connections/{id}/login/{operation}`, `POST …/{operation}/poll`, `POST …/{operation}/cancel` | Connection manager; login operations bound to their initiator | Audited Hermes-owned Codex device login. The old global provider-login routes are removed. |
| `GET /api/teams/{id}/model-policy`, `PUT /api/teams/{id}/model-policy` | Team member for reads; maintainer or platform administrator for writes | Inherited or explicitly selected allowed model route. Connection assignment and team concurrency changes require a platform administrator. |
| `GET /api/teams`, `GET /api/teams/{id}` | Team reader or global role | Searchable teams and the current account's team role. |
| `POST /api/teams`, `PATCH /api/teams/{id}` | Owner or admin | Team creation and conditional metadata changes. |
| `GET /api/teams/{id}/members` | Team reader or global role | Bounded membership list. |
| `PUT /api/teams/{id}/members`, `POST /api/teams/{id}/members/{user_id}/remove` | Team maintainer, owner or admin | Add, change, or remove membership with a reason. |
| `GET /api/teams/{id}/repositories`, `GET /api/repository-requests` | Team reader or global role | Owned repositories and request decisions. |
| `POST /api/teams/{id}/repository-requests`, `POST /api/repository-requests/{id}/withdraw` | Team maintainer, owner or admin | Submit or withdraw a pending request. |
| `POST /api/repository-requests/{id}/approve`, `POST /api/repository-requests/{id}/reject` | Owner or admin | Verify and approve, or reject, a pending request. |
| `PUT /api/repository-ownership/{id}`, `POST /api/teams/{id}/repositories/{repository_id}/remove` | Owner or admin | Conditional ownership assignment/transfer, or removal. |
| `POST /api/audit/access`, `POST /api/audit/access/{id}/end` | Owner or admin | Start justified audit access for 30 minutes in one scope, or end it early. |
| `GET /api/teams/{id}/events`, `GET /api/audit` | Owner or admin with justified access | Keyset-paginated events filtered by audience; requires `X-Audit-Access-ID`. `/api/audit` supports text, action, actor, outcome, time and team filters. Each read is recorded. |
| `GET /api/audit/export` | Owner or admin with justified access | Up to 1,000 matching events as JSON, CSV, JSON Lines or OTLP JSON, with count and continuation headers. Requires `X-Audit-Access-ID` and records the export. |

Repository reports accept optional `team_id`. Without it, a member receives the
union of their teams; owners, admins, and explicit global viewers receive the
deployment view. A foreign resource returns 404 without revealing its owner.
Finding details, review details, and feedback rows report whether the current
account can act on that exact resource. Clients must still handle access changing
before submission.

Overview, repositories, PR groups, and history accept either `days` (1–90; default 30), or
both `start` and `end` as RFC 3339 timestamps with timezone offsets. Explicit
ranges override `days`, span at most 366 days, include the start, and exclude the
end. Responses return the normalized `window_start` and `window_end`; clients
should use those fields for custom-range labels. Existing repository and history
filters remain based on request start time. The history `active` filter and
current-work counters include all current work regardless of the time window.

Overview counts use the event each metric describes: requests use their start
time, published reviews use the successful GitHub publication time, failures use
completion time, and token totals use the time Hermes usage was recorded.
Request-to-publication median and p95 include queueing, retries, and delivery.
The daily series groups publications at UTC midnight and omits empty days.
Lifetime counts cover retained records; `retained_since` identifies the earliest
retained review request. They do not reconstruct activity from before this
installation or deleted records.

Workers send a heartbeat every 30 seconds, including while idle. Presence becomes
`unresponsive` after 90 seconds without a heartbeat; this is not proof that a
container stopped. A graceful shutdown reports `draining`, then `stopped`.
`active_leases` counts unexpired assignments and is null if multiple retained
processes share a lease-owner name. Worker lists contain at most 100 instances
and signal truncation. Counts reflect processes that report to this database;
an empty list does not prove that no older workers are running.

Queue `due` means the availability deadline has passed; repository scheduling
and authorization may still prevent a claim. `delayed`, `next_available_at`,
`oldest_waiting_at`, expired leases, and failed work help distinguish a backlog
from a missing consumer. Provider cooldowns that are not stored in these queues
are not inferred. Structured events record process lifecycle and review handoff
with run and job IDs. They contain no raw stdout, prompts, model reasoning, or
provider response bodies. Use Dokploy or OpenShift for full container logs.
Each instance retains its latest 1,000 events. Heartbeats remove instances unseen
for seven days and their events in bounded batches; this does not remove reviews
or token totals.

Token usage is collected from the pinned Hermes chat response for new review
attempts. `prompt_tokens`, `completion_tokens`, and `total_tokens` include the
provider calls reported by Hermes during that request. Storage deduplicates by
job and lease generation, so reading the same response twice does not add tokens
twice. Per-review usage sums recorded attempts, including retries. Compare
`reported_attempts` with `started_attempts` to see recording gaps; the latter
counts job leases, some of which may finish before calling Hermes. Equal counts
mean each lease reported usage, not independently verified provider billing.

Missing, invalid, all-zero, oversized, or interrupted responses remain unknown;
no reported usage is represented by null, not zero. A telemetry failure does not
change a review's durable outcome. Earlier reviews are not backfilled. Codex
subscription billing, remaining quota, and monetary cost cannot be inferred from
these token counts.

List limits are 1–100. Repository and account offsets are bounded at 10,000;
history and events use descending ID cursors. PR groups use the ID of their
latest matching request, with totals calculated before the cursor is applied.
The review reader bounds its body at 200,000 characters and its GitHub links at
100. It marks any truncation and links to GitHub for the complete result. Markdown
is sanitized before rendering; embedded images and form controls are omitted.
The users endpoint now returns an
object instead of an array, so deploy the generated frontend and API together.
Upgrade the main worker image as well as the admin image after applying the
current schema 27, so workers and the console use the same presence, usage,
team-access, and integration contracts.

## Application integrations

Platform administrators can give an application read access from **Settings →
Integrations**. Select up to 100 teams, or explicitly grant **Entire deployment**
access. Deployment-wide grants include unassigned repositories and future teams.
Outcome metadata and aggregate reports are available by default; **Allow published
review content** is a separate permission. Integrations cannot manage users,
reviews, providers, settings, or deployment.

Choose an expiry, then create the integration. Copy the
credential into the consuming application's secret manager. It is shown once;
Review Agent stores only its SHA-256 digest. The console does not retain the
credential in its shared query cache or browser storage. If the response is lost
or the credential is misplaced, revoke that integration and create a replacement.

Permissions are fixed at creation. To rotate or change access, create a replacement,
update the consumer, and revoke the old integration. Expiry and revocation are
checked in every reporting transaction, without an authorization cache. Subsequent
reads fail after revocation; an already-running request may finish. Neither action
can retract data already downloaded by a consumer. Creation, revocation and
successful reads enter the existing audit journal with the integration ID and
operation name, UTC interval and applicable resource filters, without the
credential or review text. Before enabling scheduled consumers, choose a retention
window for their read receipts and configure bounded cleanup using the
[operator retention command](./OPERATIONS.md#retention-ownership). Each successful
read produces an audit event; polling frequency affects database and index growth.
Access changes and other audit events remain preserved.

The versioned HTTP contract uses the existing admin host and TLS configuration:

| Endpoint | Result |
| --- | --- |
| `GET /api/v1/overview` | Activity, publication timing, token telemetry and its recording gaps. |
| `GET /api/v1/repositories` | Repository counts and activity, in ascending repository ID order. |
| `GET /api/v1/reviews` | Review outcome, exact subject identifiers, usage and coverage metadata, in descending run ID order. |
| `GET /api/v1/reviews/{run_id}/content` | Bounded published text and GitHub links, requiring the separate content grant. |
| `GET /api/v1/quality` | Recorded quality signals, cohorts and explicit denominators. |
| `GET /api/v1/openapi.json` | The integration-only OpenAPI schema. |

Send the credential as `Authorization: Bearer …`. A browser session cannot
substitute for it on these endpoints, and the credential cannot authenticate
console administration endpoints. The console's **API reference** also includes
the integration routes and Swagger's **Authorize** control. Authorization is not
saved between page visits.

For example, with a credential supplied by the consumer's secret manager:

```sh
curl --fail-with-body --get \
  --header "Authorization: Bearer ${REVIEW_AGENT_INTEGRATION_TOKEN:?}" \
  --data-urlencode 'start=2026-09-01T00:00:00Z' \
  --data-urlencode 'end=2026-09-08T00:00:00Z' \
  'https://reviews.example.org/api/v1/overview'
```

Reports require explicit `start` and `end` timestamps with offsets, for a positive
interval no longer than 366 days. The interval is `[start, end)` and responses use
UTC. `window_days` is the interval length rounded up to whole 24-hour days; use
the timestamps for its exact length. Metric definitions match the console:
repository activity counts requests started in the interval, while Overview's
publication and token totals use their recorded event times. The `active` review
filter reports current active work regardless of the interval. Quality reports
reflect retained recorded signals, not an independent measure of review accuracy.

Each response wraps the shared report in `data`, with `metrics_version: 1`,
`generated_at`, and `evidence_scope: "retained_records"`. Missing token evidence
stays null. Compare started and reported attempts to assess recording gaps;
Overview also supplies the earliest retained run time. Equal telemetry counts do
not prove complete provider billing. Coverage, content and cohort truncation keep
their explicit flags from the console contract.

Page limits are 1–100. For repositories, send `next_after_id` as `after_id`; for
reviews, send `next_cursor` as `before_id`. Retain the first page's `watermark_id`
and the same interval, scope and filters on later pages. The watermark excludes
newer IDs; it is **not a database snapshot**. Outcomes can change between pages,
and retention, ownership transfers or late-committing transactions can change
which rows are visible. Consumers doing incremental synchronization should reread
an overlapping interval and deduplicate by stable IDs. Do not use page totals as
a promise of a fixed export. Repository transfers remove access under the old
team grant on subsequent reads, including access to older review text.

An optional `team_id` narrows a grant; it never widens one. Invalid or expired
credentials return `401` with a Bearer challenge, missing content permission
returns `403`, and unavailable scope or content returns `404`. Invalid bounds
return `422`; transient database unavailability returns a redacted `503`.
Responses use `Cache-Control: no-store`. Clients should stop using a revoked
credential and use bounded retries for transient failures.

This contract is HTTP-only. An MCP adapter can reuse it when a concrete consumer
needs one; it does not require another reporting database or identity service.

## Administrative controls

Repository access separates GitHub's **All repositories / Only select repositories**
grant from Review Agent's **All accessible / Only explicitly enabled** activation
policy. Automatic activation verifies each repository on its first review request;
manual denials remain blocked. Switching to explicit activation disables automatically
activated repositories and preserves manually enabled ones.

The GitHub App card verifies App authentication, required permissions, and the
issue-comment event subscription. It links to the owning account's App settings
and installation page. **Check live GitHub status** reads an installation directly
from GitHub, including its settings link and missing permissions. Stored access
state is labeled separately. An authenticated App does not prove webhook delivery
or that a model request will succeed.

**Add repository** verifies one exact repository with a metadata-only installation
token before enabling it. This works with both GitHub scope modes and does not
scan or remove other repositories. Selected installations can also sync their
complete repository inventory. Every change records the
signed-in administrator's stable account ID.

**Disable reviews** stops new review admission and blocks automatic reactivation.
It retains historical records. To revoke provider access as well, remove the
repository through the installation's GitHub settings; webhooks update Review
Agent's stored access. GitHub owns provider-scope changes. Its removal API does
not accept installation tokens, so the console links to GitHub and does not add
PAT authentication. See [modifying installed Apps](https://docs.github.com/en/apps/using-github-apps/reviewing-and-modifying-installed-github-apps).

Open **Finding decisions** from a published review to inspect an exact finding
occurrence. A human decision refers to that occurrence even if a later review
contains the same fingerprint. Intentional-by-design decisions still require the
accepted ADR snapshot and matching path. Review quality shows the reporting
cohorts and retained feedback backlog; team maintainers and platform admins can
classify pending feedback using the existing triage states.

A request's **Review actions** can release a delayed retry, cancel queued or leased
work, or fail stale work after checking its heartbeat and lease. The confirmation
captures the job generation, status, and availability timestamp. A concurrent
change rejects the action and requires a refreshed confirmation. Terminal reviews
and jobs already awaiting publication cannot be cancelled through this control.
The lifecycle change and audit event commit together.

## Save and apply policy

Settings starts with the deployment's environment values. Saving a policy creates
an immutable PostgreSQL revision with an actor and reason. The latest saved
revision then owns the displayed policy values. Concurrent edits return a conflict
rather than overwriting a newer revision. **Restore to editor** loads an older
revision into the form; saving it creates a new revision. The editor does not
poll while you work. A conflicting save preserves the draft until you explicitly
reload the saved settings. Model suggestions come from Hermes; supported model
IDs can still be entered directly.

Provider, model, and reasoning effort apply when a new GitHub review request is
admitted. The selected route is recorded in its exact review contract and sent
explicitly to Hermes. Already admitted requests, including command redelivery,
retain their recorded route. Workers continue to verify the installed profile,
engine, configuration, image, and result budget before execution.

Other policy values load when their owning services start:

| Values | Service to restart |
| --- | --- |
| Active job limit, capacity retry delay, admission age, review attempt budget and default priority | `review-github-app-worker` |
| Concurrency, lease, heartbeat, retries, polling, recovery, priority aging and Hermes timeout | `review-worker` |
| Publication size, publication attempt budget and feedback instructions | `hermes-review` |
| Publication size, lease, heartbeat, retries and polling | `review-publisher` |
| Webhook payload bound, request concurrency and timeout | `review-admission` |
| Code graph enablement, embedding mode and GitHub gateway concurrency | `review-github-gateway` |

Use the normal deployment procedure to drain and restart services. The console
records the last 50 startup observations and their loaded revision. These are
startup records; worker heartbeats and Dokploy container state provide separate
current-state evidence. Code graph options require the graph overlay, and OpenAI
embeddings require the gateway's existing API key. Profile contents, secrets,
container shutdown grace, and graph cache sizing remain deployment configuration.

### Environment defaults and admin controls

All variables in `.env.example` have an owner below. Environment values remain
bootstrap defaults for managed policy; after a save, the revision owns those
values. Older revisions continue using environment defaults for controls they
predate. Advanced scheduling, recovery, delivery, and request bounds are grouped
under **Advanced operational settings**.

| Variables | Owner |
| --- | --- |
| `REVIEW_AGENT_MODEL_PROVIDER`, `REVIEW_AGENT_MODEL`, `REVIEW_AGENT_REASONING_EFFORT` | Settings; frozen into each new review request. |
| `REVIEW_AGENT_ACTIVE_JOB_LIMIT`, `REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS`, `REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS`, `REVIEW_AGENT_JOB_MAX_ATTEMPTS`, `REVIEW_AGENT_JOB_PRIORITY` | Settings; admission policy at service startup. |
| `REVIEW_AGENT_WORKER_CONCURRENCY`, `REVIEW_AGENT_JOB_LEASE_SECONDS`, `REVIEW_AGENT_JOB_HEARTBEAT_SECONDS`, `REVIEW_AGENT_HERMES_TIMEOUT_SECONDS` | Settings; review worker policy at startup. |
| `REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS`, `REVIEW_AGENT_JOB_RETRY_SECONDS`, `REVIEW_AGENT_JOB_POLL_SECONDS`, `REVIEW_AGENT_JOB_RECOVERY_SECONDS`, `REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE` | Settings; scheduling and recovery at startup. |
| `REVIEW_AGENT_PUBLISH_MAX_BYTES`, `REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS`, `REVIEW_AGENT_PUBLICATION_LEASE_SECONDS`, `REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS`, `REVIEW_AGENT_PUBLICATION_RETRY_SECONDS`, `REVIEW_AGENT_PUBLICATION_POLL_SECONDS` | Settings; review publication policy and publisher startup. |
| `REVIEW_AGENT_FEEDBACK_ENABLED`, `REVIEW_AGENT_CODE_GRAPH_ENABLED`, `REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS` | Settings; reviewer or gateway startup. The graph service and its credentials must already be deployed. |
| `REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES`, `REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS`, `REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS`, `REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS` | Settings; incoming-request and gateway bounds at startup. The webhook protocol maximum remains 2,097,152 bytes. |
| `REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS`, `REVIEW_AGENT_OPERATOR_EXPORT_MAX_ROWS` | Operator CLI environment; bounds for that command's output. The console has its own fixed API pagination limits. |
| `REVIEW_AGENT_PROFILE` | Deployed profile identity and packaged review contract. Changing it requires a matching installed profile. Repository activation selects from installed profiles. |
| `HERMES_IMAGE`, `REVIEW_AGENT_IMAGE`, `REVIEW_AGENT_ADMIN_IMAGE`, `POSTGRES_IMAGE` | Deployment platform; image selection and upgrade/rollback. |
| `REVIEW_AGENT_INGRESS_NETWORK`, `REVIEW_AGENT_ADMIN_PUBLIC_URL`, `TZ`, `PYTHONUNBUFFERED`, `HERMES_DASHBOARD` | Deployment platform; networks, trusted origin and process setup. The provider overlay owns its loopback dashboard settings. |
| `REVIEW_AGENT_POSTGRES_MAX_CONNECTIONS`, `REVIEW_AGENT_WORKER_TERMINATION_GRACE_SECONDS`, `REVIEW_AGENT_CODE_GRAPH_CACHE_BYTES` | Deployment platform; PostgreSQL capacity, container termination and the isolated graph service's storage budget. |
| `REVIEW_AGENT_POSTGRES_PASSWORD`, `REVIEW_AGENT_DATABASE_URL`, `REVIEW_AGENT_RUNTIME_DATABASE_URL` | Deployment secrets; database bootstrap and service credentials. |
| `REVIEW_AGENT_GITHUB_APP_ID`, `REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH`, `REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET` | Deployment identity, secret mount and webhook verification. The console reports connection state without returning these values. |
| `REVIEW_AGENT_DOKPLOY_URL`, `REVIEW_AGENT_DOKPLOY_COMPOSE_ID`, `REVIEW_AGENT_DOKPLOY_API_KEY` | Deployment integration binding and credential; fixes which external application the admin service may inspect. |
| `REVIEW_AGENT_OPENAI_API_KEY`, `REVIEW_AGENT_HERMES_CONTROL_TOKEN`, `API_SERVER_KEY` | Deployment secrets; embedding, provider-companion and Hermes API credentials. They are never stored in policy revisions or sent to the browser. |

When upgrading this candidate, back up PostgreSQL and apply migrations through 27 before
starting the matching admin frontend/API. Existing admins become owners; existing
viewers retain explicit global read access. New accounts default to team-scoped
membership. Repository ownership starts unassigned, and existing GitHub activation
policy is preserved. Assign repositories before converting global viewers to
members. Restore policy by loading an earlier
revision into the editor and saving it. Use a coordinated application
rollback with the normal database backup procedure; an older application cannot
read revisions containing newly introduced fields.

## Dokploy container state

The Health page can read container state for one configured Compose application.
Set `REVIEW_AGENT_DOKPLOY_URL` to the Dokploy origin,
`REVIEW_AGENT_DOKPLOY_COMPOSE_ID` to this stack's ID, and
`REVIEW_AGENT_DOKPLOY_API_KEY` to a server-side API credential with the required
read access. Use the narrowest account permissions your Dokploy installation
supports. Restart `review-admin` after configuring the connection.

The browser receives only container identity, state, status, and a dashboard link.
It cannot select another application or submit arbitrary Dokploy operations.
The current integration reads state and opens Dokploy for deployment actions.
An MCP connection used by a development assistant is independent of the console's
runtime credential. See the [Dokploy API](https://docs.dokploy.com/docs/api).

## Provider connections

Include `compose.providers.yaml` after `compose.admin.yaml` and configure a
separate random `REVIEW_AGENT_HERMES_CONTROL_TOKEN` in deployment secrets. Build
the main and admin images from the same candidate checkout. Retain all overlays
when applying subsequent updates:

```bash
docker compose -f compose.yaml -f compose.admin.yaml -f compose.providers.yaml config --quiet
```

The overlay enables the pinned Hermes dashboard on `127.0.0.1:9119`. A small
`review-provider-control` service shares the Hermes network namespace and exposes
only bounded provider status, account quota, model options, runtime diagnostics, Codex device
login, polling, and cancellation operations on private port `9120`. It authenticates the admin API
with the dedicated token. The Hermes dashboard is not published or attached to
the ingress network. When recreating Hermes, recreate its provider-control
companion in the same Compose operation.

The companion also receives the existing `API_SERVER_KEY` to read Hermes'
`/health/detailed`, `/v1/capabilities`, `/v1/review-agent/status`, and the fixed
`/v1/review-agent/quota/{provider}` endpoints over loopback. This bearer key
is not passed to the admin service. The companion projects fixed readiness
statuses, version, agent count, shutdown state, API support and default model;
it omits raw diagnostics, paths, credentials, commands and process details.
Health labels these engine checks separately from worker heartbeats, container
state, and provider authentication. They do not call the model provider or imply
that a review will succeed. See the [pinned Hermes API contract](https://github.com/NousResearch/hermes-agent/blob/v2026.8.31/website/docs/user-guide/features/api-server.md).

Open **Model connections** and select the shared or team-owned connection.
Pause it, finish active work, and cancel or finish queued reviews before replacing
its account. Start **Connect Codex**, open the displayed OpenAI verification page,
and enter the one-time code. Hermes stores and refreshes the credential. Only the
person who started the login can poll or cancel it, and their permission is
rechecked on each operation. A browser reload can resume that person's pending
operation; the one-time challenge is not stored in the database.

Successful login records the account and leaves the connection paused. Review its
state and choose **Enable** explicitly. A timeout or interrupted operation leaves
the connection requiring attention. An owner must stop the old Hermes process,
its dashboard/login service and provider-control companion, restart the complete
runtime, and confirm that recovery before recording its accounts. The API also
requires a different Hermes process instance. Worker lease expiry alone cannot
prove that remote inference or authentication stopped.

One account per provider is allowed in each connection. Multiple pool entries or
a profile inheriting another home's credentials block managed execution. Codex
account identity survives token refresh; changing the account increments its
recorded revision. The worker and native Hermes endpoint check the frozen
connection, account revision, lease, model, and reasoning before inference.
Queued reviews never adopt a replacement account. Pausing after a claim but
before inference returns the unstarted job without consuming its attempt.

For Anthropic, use an authorized API-key integration through Hermes's terminal
and then **Record current accounts** while the connection is paused and drained.
The console does not offer Claude consumer OAuth login or infer account identity
from an opaque consumer token. See [provider authentication constraints](TEAM_ACCESS_AND_MODEL_ACCOUNTS.md#oauth-and-quota-visibility).
Recorded accounts and live credential observations are labelled separately;
neither proves that a model can complete a review.

In a team's **Models** tab, administrators assign its connection. Maintainers can
select from that connection's allowed model and reasoning choices or inherit the
deployment default. The effective values and their source are shown together.
These changes apply to new requests; each admitted request retains its model
route and admission-time team attribution. Implicit provider fallback is disabled.
The image applies a source-checked patch to Hermes's auxiliary fallback owner;
the managed profile sets `auxiliary.allow_fallback: false`. Image checks exercise
missing and exhausted credentials without contacting a provider. When upgrading
Hermes, update and verify this patch before building a release candidate.

### Quota and concurrent reviews

Each verified connection shows Codex account quota from Hermes, including the
provider's actual window durations, remaining percentage, reset timestamps, plan,
limit buckets, and available reset-credit count. Unknown values remain unknown;
failed refreshes retain the last successful observation and label it stale.
Anthropic API-key quota is not reported by this integration. Reset credits are
read-only; the console cannot redeem them.

Quota includes other uses of the same provider account. Team review activity and
recorded tokens are separate measurements. Requests to
`GET /api/model-connections/{id}/quota/{provider}` use the connection's read
permissions and recheck its account revision after the remote response. Add
`?refresh=true` for a manual refresh. Hermes coalesces refreshes per account,
normally refreshes after five minutes while the page or worker is using the
connection, and limits manual refreshes to once per 30 seconds. Failed reads back
off from 30 seconds to five minutes. Provider reads have a 15-second timeout and
256 KiB response bound; the console receives only the redacted projection.
Review startup waits at most five seconds for this refresh, then uses the cached
observation and any durable account wait while the refresh continues.

The platform owner can set **Maximum concurrent reviews** in any connection
editor; platform admins can edit team-owned connections. Platform administrators
can set a team's cap in its **Models** tab; that
cap applies across connections. Both default to four. Lowering a cap lets active
work finish and limits new claims immediately. Worker process capacity remains an
additional bound. Eligible teams take turns, with priority aging within each team.

When Codex explicitly reports that account usage is disallowed, native execution
returns the unstarted review to its queue without spending an attempt. All queued
work on that account shares one wait deadline. History shows the quota wait and
the next check time; Operations includes it in delayed queue totals. An elapsed
reset or wait permits another quota check, not automatic recovery. A fresh
provider allowance clears the wait. A failed check preserves known exhaustion;
an account with no quota observation uses the ordinary execution error handling.
Additional model-specific buckets are displayed, but the scheduler does not
guess which model names they govern. Quota cannot reserve capacity against other
applications using the same provider account.

Claimed jobs and unfinished remote executions both reserve team and connection
capacity. Lease expiry, cancellation, or a failed local run cannot release a
remote execution that might still be running. The native response releases that
reservation; interrupted runtimes require the existing explicit recovery flow.
The connection page shows unfinished executions and explains their capacity
reservation. If that count persists after reviews stop, including after a failed
database write at completion, an owner must pause the connection, stop and restart
its complete runtime, then use **Recover after a runtime restart**. A newly
reported runtime instance alone does not prove that the previous process stopped.

### Provision another connection

Teams sharing an account reuse the shared runtime. Independent credentials need
an additional Hermes runtime, worker, and provider-control companion, using the
same application image, managed profile, database, and existing network roles.
The console registers these services; it does not provision containers.

1. Give the runtime its own empty credential volume at `/opt/data` and run the
   existing profile installer against that volume. Its worker mounts that same
   volume read-only for the installed contract. Do not copy another runtime's
   credential store or supply ambient credentials from a different connection.
2. Set `REVIEW_AGENT_MODEL_CONNECTION` to one stable key, such as `payments`, on
   both the Hermes process and its worker. Point the worker's
   `REVIEW_AGENT_HERMES_CHAT_URL` at that runtime's private
   `/v1/review-agent/review` endpoint. Give that runtime, worker, and companion a
   matching dedicated `API_SERVER_KEY`.
3. Configure its loopback dashboard and provider companion as in
   `compose.providers.yaml`, with a separate control token. Keep the companion
   in that runtime's network namespace and its control port private. The admin
   service receives the control token, without the Hermes API key or volume.
4. Mount a JSON catalog read-only into `review-admin` and set
   `REVIEW_AGENT_CONNECTIONS_FILE` to its container path. Store the referenced
   token in the admin service's deployment secrets, then restart the admin service:

   ```json
   [
     {
       "key": "payments",
       "control_url": "http://hermes-payments:9120",
       "token_env": "PAYMENTS_HERMES_CONTROL_TOKEN"
     }
   ]
   ```

5. As an owner, choose **Add connection**, select that runtime, name its connection,
   select the owning team, and define permitted model choices. Connect or record
   its account, enable it, then assign it in the team's Models tab.

The catalog accepts at most 1,000 entries in 256 KiB. Runtime keys and control
origins must be unique. Console users cannot supply endpoints or control tokens.
Retiring a connection preserves its audit and usage history and requires its
queue to be drained and its teams reassigned. Remove the retired services through
the deployment platform after accounting for retained credentials and backups.

Without these optional connections, the console reports their unavailable state;
review history, quality evidence, and database-backed controls remain available.

## Add the service

Follow the normal [deployment and upgrade procedure](DEPLOYMENT.md), including
backup and migrations. The panel is one additional service in the existing
Compose application, with its own image and hostname. It serves its compiled
frontend and API together on port `8090`; Node.js is used only during the build.

The supplied console overlay works with Docker Compose and platforms that run
Compose applications. The current OpenShift template deploys the main runtime
only. Installing the console there also requires its Deployment, Service,
HTTPS Route, and network-policy access to the private database and gateway;
these resources are not supplied by that template.

Both release images carry the same release tag and source revision, with
different immutable image digests. The console footer and `GET /api/version`
identify the running admin build. This metadata is baked into the image;
deployment environment variables do not change it. Source checkouts and local
builds default to `development` with no claimed source revision. A local build
is not a qualified release.

The admin image uses Python 3.14.7 on Debian 13. Its runtime omits pip, ensurepip,
and Perl after dependency installation; apply updates by replacing the image.
The main image retains the Python runtime
supported by the pinned Hermes image; see [runtime updates](OPERATIONS.md#updating-and-validation).
The admin build also includes an npm dependency inventory outside the served
web assets. Qualified releases attach the inventory with their other
[release evidence](SECURITY.md#release-inventories).

1. Add these non-secret values to the deployment configuration:

   ```dotenv
   REVIEW_AGENT_ADMIN_IMAGE=review-agent-admin:local
   REVIEW_AGENT_ADMIN_PUBLIC_URL=https://admin.example.org
   ```

   The panel uses the existing `REVIEW_AGENT_RUNTIME_DATABASE_URL`. The overlay
   also mounts the existing GitHub App private key read-only for installation
   approval and inventory refresh. Provider credentials remain in Hermes.
   `REVIEW_AGENT_ADMIN_PUBLIC_URL` must exactly match its HTTPS origin, including
   a non-default port if used. Serve it at the hostname root, not a URL subpath.

2. Include `compose.admin.yaml` with every Compose operation. Retain any other
   overlays already used by the deployment. For a source build:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml config --quiet
   docker compose -f compose.yaml -f compose.admin.yaml up -d --build
   ```

   For a qualified release that includes the panel, set both image variables to
   their respective manifest references from the same `IMAGE-DIGESTS.txt`, then
   use `pull` and `up -d --no-build`. The two images have different digests.
   After an upgrade, compare the console version and full revision from
   `/api/version` with the release tag and `SOURCE-SHA.txt`. Keep the admin
   service stopped during migrations and start it with the matching workers.

   In Dokploy, keep the panel in the same Compose application so it can reach
   the existing private database network. For a raw Compose deployment, merge
   the overlay into a copy of the deployment's current definition:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml config \
     --no-interpolate --no-path-resolution --no-normalize > compose.dokploy.yaml
   ```

   Preserve the existing project name, volumes, mounts, networks, and overlays
   when saving the result. Set values in Dokploy Environment. For a Git-based
   deployment, include both Compose files in its command. Apply the normal
   Deploy procedure after changing the source revision.

3. In Dokploy Domains, route a separate HTTPS hostname to service `review-admin`,
   container port `8090`. Keep its path as `/` and enable the HTTPS redirect.
   Admission retains its own hostname and port `8644`. Only admission and the
   panel join the shared ingress network; database, gateway, and Hermes remain
   private. A separate Dokploy application is unnecessary.

4. After migrations complete, create the first platform owner from the panel
   container's terminal:

   ```bash
   python -m review_agent_tools.admin_auth --email admin@example.org
   ```

   With a local Compose terminal, the equivalent is:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml exec review-admin \
     python -m review_agent_tools.admin_auth --email admin@example.org
   ```

   Enter the password at the hidden prompts. Do not put it in command arguments,
   environment files, or chat. Use 15–128 characters. The command creates an
   account only when no accounts exist; service restarts never reset passwords.

5. Open the admin hostname and sign in. Check repository counts and a known
   review, then use **Users** to add a Member account. Add it to a team as Viewer
   and verify that it can read that team’s repositories without user administration.

## Organization sign-in and SCIM

The source candidate supports one generic OpenID Connect provider and a basic
SCIM 2.0 Users API. Both are disabled by default. Local email/password sign-in
remains available. Keep a working local owner account before enabling either
feature. MobilityGuard is the intended first provider; live interoperability has
not yet been verified.

Apply migrations through 28 with the matching worker and console images. Register a
confidential OIDC client with this exact callback, replacing the origin with
`REVIEW_AGENT_ADMIN_PUBLIC_URL`:

```text
https://reviews.example.com/api/auth/oidc/callback
```

Use authorization code flow, PKCE S256, `client_secret_basic` client
authentication, and a query-mode callback. Include `openid email` claims in the
ID token, with `email_verified: true` for first linking or self-registration. The issuer's HTTPS
discovery document must identify that exact issuer and provide HTTPS authorization,
token and JWKS endpoints. RSA, RSA-PSS and ECDSA signed ID tokens are supported;
unsigned and symmetric signatures are rejected. Discovery and keys are fetched
for each sign-in, so key rotation does not require a service restart. Provider
responses are limited to 256 KiB and token strings to 16 KiB.

Set these values on `review-admin`, using the deployment's secret store for the
client secret and provisioning credential. The Compose admin overlay passes
them through. They do not belong in repository files or team settings.

| Variable | Value |
| --- | --- |
| `REVIEW_AGENT_OIDC_ISSUER` | Exact HTTPS issuer URL, including its path and trailing slash if present. |
| `REVIEW_AGENT_OIDC_CLIENT_ID` | Registered client ID. |
| `REVIEW_AGENT_OIDC_CLIENT_SECRET` | Client secret. |
| `REVIEW_AGENT_OIDC_NAME` | Login button label; defaults to `Organization sign-in`. |
| `REVIEW_AGENT_SCIM_TOKEN` | Optional dedicated random credential of 32–512 characters. Set this to enable SCIM with the configured issuer. |

Generate the SCIM credential in your secret manager, or use `openssl rand -hex 32`
in a private terminal. Give it only to the directory provisioning connector.
It cannot sign in to the console or authorize reporting integrations. Rotate it
by replacing the configured value and restarting the admin service; the old
credential then stops working. Incomplete identity configuration prevents startup.

### Link accounts and sign in

Existing accounts sign in with their local password, open **Your account**, and
choose **Link** under Organization sign-in. The provider must return the same
verified email as the local account. Email matching by itself never links an
existing local account. A pending link expires after five minutes and also
becomes unusable when the initiating session is revoked or account access changes.

SCIM-created accounts may link on their first organization sign-in using their
matching verified email. They start as Members with no team access. Assign teams
and roles in the console. Provider group and role claims never grant access.
Accounts promoted to a privileged platform role require explicit linking.

After linking, the issuer and subject identify the account; a later provider
email change does not select another local account. An existing link cannot be
replaced by matching a different subject to the same email. Changing issuer
configuration does not migrate linked identities or directory ownership.

Sign-in requests are bound to a temporary browser cookie and consumed once.
Successful OIDC sign-in issues the existing eight-hour console session, with the
same logout, deactivation and authorization rules as password sign-in. Only the
identity link is retained; provider access and refresh tokens are not stored.
Logout ends the console session and does not sign the user out of the provider.

### Configure directory provisioning

Use `https://reviews.example.com/scim/v2` as the SCIM base URL and send
`Authorization: Bearer <credential>`. Requests and responses use
`application/scim+json`; a browser Origin header is not required.
Discovery is available at `ServiceProviderConfig`, `ResourceTypes`, and `Schemas`.
The supported resource is **User**, with this mutable contract:

| Attribute | Meaning |
| --- | --- |
| `userName` | Required email address; map the directory's account email here. Unique without case sensitivity. |
| `externalId` | Optional directory identifier, up to 255 characters. Unique within this configured directory and independent of the OIDC subject. |
| `active` | Boolean, defaulting to true. False disables both password and OIDC sign-in and revokes existing sessions. |

Paths below are relative to the SCIM base URL. `POST Users` creates an account.
`GET Users` and `GET Users/{id}` return only accounts created through this
directory connector. `PUT Users/{id}` replaces
the supported mutable fields. `PATCH` accepts add/replace for those fields and
remove for `externalId`, with all operations committed together. Other attributes
in create/replace documents are ignored; unsupported PATCH paths are rejected.
The connector does not write passwords, platform roles or team memberships.

Lists support `startIndex`, `count` up to 100, and exact filters
`userName eq "person@example.com"` or `externalId eq "directory-id"`. A zero
count returns the total without resources. Groups, bulk requests, sorting,
versioned ETags, and the enterprise User extension are not supported.

SCIM cannot adopt existing local accounts or change accounts with a privileged
platform role. Duplicates return `409` with `scimType: uniqueness`. `DELETE`
revokes sessions, disables the account and removes it from SCIM results, while
retaining its local account, audit and team history. Deleted account identifiers
and emails are not recycled by provisioning. Requests are limited to 64 KiB;
PATCH accepts at most 100 operations.

These contracts use [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html),
[SCIM's User schema](https://www.rfc-editor.org/rfc/rfc7643.html), and the
[SCIM protocol](https://www.rfc-editor.org/rfc/rfc7644.html). Configure the
connector to the advertised subset before enabling synchronization.

### Verify and recover

First verify local owner sign-in. Use a test directory account to provision a
Member, sign in through OIDC, and confirm that access appears only after team
assignment. Deactivate it through SCIM and verify that its existing browser
session loses access. Check the console audit journal for provisioning, linking,
sign-in and deactivation events.

If the provider is unavailable, use local password sign-in. A failed or expired
OIDC request must be restarted from the login or account page. To disable identity
integration, clear the OIDC and SCIM variables and restart the admin service.
This preserves local accounts, existing console sessions and identity records;
disable affected accounts if their sessions must also be revoked. Migration 27
is additive: retain its data during recovery and follow the coordinated image
and database recovery procedure when changing versions.

## Self-registration

Owners can open **Users & roles → Registration allowlist**, add allowed email
domains or individual addresses, and enable self-registration. It starts disabled
with empty lists. Enter one value per line, up to 100 in each list. `sundsvall.se`
and `@sundsvall.se` both allow that exact domain; subdomains must be listed
separately. Matching is case-insensitive, and either list can admit an address.
Empty lists admit nobody. Saves are audited, and stale saves must be reloaded.

For email/password registration, open **Settings → Email delivery (SMTP)** as an
owner. Enter your existing provider's server, port, sender address, and optional
username and password. Select STARTTLS (usually port 587) or implicit TLS (usually
465); both verify the server certificate. Save the settings, then use **Send test
to me** to send to your account email. Test delivery works while delivery is
disabled, so you can check it before enabling registration. Saved SMTP changes
take effect immediately; no OIDC configuration or service restart is needed.

Before saving an SMTP password, set `REVIEW_AGENT_EMAIL_SECRET_KEY` in the console
service's deployment secrets and restart that service once. Generate the key with:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Back up this key separately from the database. The database stores the encrypted
password; API responses and audit events never return it. Leave the password
field blank to retain it. Changing the server, port, TLS mode, or username requires
re-entering the password. To use an unauthenticated relay, clear the username and
select **Remove saved password**. Such relays do not require the encryption key.
If the key is lost or replaced, restore it or save the SMTP password again with
the new key. Existing account sign-in remains available when email delivery fails.

Delivery starts disabled. Owners can pause it without erasing the configuration.
Saves reject stale revisions, and test emails are limited to one per minute.
This integration sends registration verification and owner-requested test emails;
it adds no email-based password reset or mail queue.

When registration is open, **Register** appears on the login card. An allowed
address receives a link to choose a password and finish registration. Links expire
after 30 minutes and work once; the allowlist is checked again before creating the
account. The server stores a hash of each token, and the link carries the token in
its URL fragment so it is not sent in page requests. Requesting a link does not
create an account or reserve an existing account's credentials.

Wait one minute before requesting another email. A new link replaces the previous
one. Delivery errors preserve that cooldown and can be retried after a minute.
Requests are limited to 30 emails per minute across the console and 1,000 pending
addresses; expired requests are removed when another eligible request arrives.
Response status and body do not disclose whether a particular address is allowed
or already registered. Delivery time may differ for an eligible address.

A configured organization sign-in provider can also register allowed addresses
without SMTP when its signed identity contains a verified email. Existing local
accounts must still link their identity explicitly from **Your account**.

Both registration methods create Members with no automatic team access. Owners
and admins assign teams afterward. Removing an allowlist entry or closing
registration affects new accounts only; use the existing account controls to
disable a user or revoke access. Migration 28 is additive; retain its tables when
rolling back images, following the coordinated deployment recovery procedure.

## Accounts and recovery

Authentication uses FastAPI Users, Argon2 password hashes, and revocable
PostgreSQL sessions. HTTPS cookies are HttpOnly and SameSite Strict and expire
after eight hours. Self-registration is optional as described above. For accounts
created by an administrator, share the initial password privately; users can
change it under **Your account**. Password resets remain administrator-managed.

Owners can disable or reset any account; admins can manage Member and Global
viewer accounts. Account changes revoke sessions, and logout revokes the current
session. The panel protects the last active owner from demotion or disablement.
Keep a second owner for privileged-account recovery. The bootstrap command
deliberately does not replace existing accounts.

Users and sessions are included in the existing PostgreSQL backup. Back up the
database before upgrading both images. To remove the panel, remove its route
and optional service while preserving PostgreSQL. Removing the panel does not
require deleting its tables. Follow the existing database recovery procedure
for a full version rollback; do not reverse migrations manually.

The console shares the existing PostgreSQL runtime. Its application pool permits
at most four connections and sixteen waiting queries; the authentication pool
permits two connections without overflow. Include both in the deployment’s
connection budget. Team, request, and audit pages use bounded pages. Quality
cohorts return at most 200 rows and signal truncation. Swagger assets are built
separately and load only when the API reference is opened.
