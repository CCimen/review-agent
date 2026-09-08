---
sidebar_label: Admin panel
slug: /admin-panel
title: Review Agent operator console
description: Inspect reviews, manage repository access and finding decisions, and apply deployment policy through an authenticated console.
status: transitional
last_verified: 2026-09-08
---

# Review Agent operator console

The optional console provides Activity, Repositories, Review quality, Health,
and Settings. Sign in with an individual email address and password. Viewers can
read reviews and quality evidence across the deployment. Administrators can
manage accounts, repository access, finding decisions, feedback triage, review
run controls, and deployment policy. GitHub remains the source of review requests
and the destination for published reviews.

This feature is available in the source candidate and has not yet been released.
The current v0.4.0-rc.4 image does not include it. Build both images from the same
candidate checkout, or use a future qualified release that supplies both
`review-agent` and `review-agent-admin` digests. Do not combine this panel with
an older migration image: the current admin API requires PostgreSQL schema 20.

## What the numbers mean

Choose a reporting period of 7, 30, or 90 days. Counts refer to review requests
started in that period:

| Metric | Meaning |
| --- | --- |
| PRs reviewed | Distinct pull requests with at least one published review. |
| Published | Published review requests; reviewing the same PR twice counts twice. |
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

The source candidate exposes the following authenticated endpoints for admin
clients. The frontend uses `admin/openapi.json` and the generated
`admin/src/api.generated.ts` contract.

| Endpoint | Access | Data |
| --- | --- | --- |
| `GET /api/overview` | Viewer or admin | Lifetime and selected-period totals, UTC daily publications, publication latency, recent failure reasons, and reporting review workers and capacity. |
| `GET /api/repositories` | Viewer or admin | Paginated repositories, `total` matching repositories, and aggregate `totals` across every matching repository. |
| `GET /api/pull-requests` | Viewer or admin | Paginated PR groups, total matching PRs, matching and lifetime request counts, and the latest matching request. |
| `GET /api/history` | Viewer or admin | Paginated requests, `total` matching requests before the cursor is applied, and per-request token usage. |
| `GET /api/history/{run_id}` | Viewer or admin | Selected request, original published Markdown and GitHub links, plus up to 20 retained requests for the same PR. `before_id` pages that PR's history; selection is independent of the cursor and reporting period. Missing requests return 404. |
| `GET /api/operations` | Admin | Worker presence and capacity, active leases, and webhook, review, and publication queue counts. |
| `GET /api/operations/events` | Admin | Structured process and review events, with optional `worker_id` and `before_id` filters. |
| `GET /api/users` | Admin | `AccountPage`: `items`, `total`, `admin_count`, `disabled_count`, and `has_more`. |
| `GET /api/access/installations`, `GET /api/access/repositories` | Admin | Bounded GitHub App access inventory and capability state. |
| `POST /api/access/...` | Admin | Installation approval and refresh; repository enablement and disablement. |
| `GET /api/quality`, `GET /api/quality/feedback` | Viewer or admin | Quality cohorts and paginated retained feedback. |
| `GET /api/history/{run_id}/findings`, `GET /api/findings/{fingerprint}` | Viewer or admin | Published finding occurrences and bounded decision history. |
| `POST /api/findings/{fingerprint}/decisions`, `POST /api/quality/feedback/{id}/triage` | Admin | Audited human decisions and feedback triage. |
| `GET /api/history/{run_id}/controls`, `POST /api/history/{run_id}/actions` | Admin | Available run controls, snapshot preconditions, and audit events. |
| `GET /api/settings`, `PUT /api/settings` | Admin | Current policy, bounded revision history, startup records, and conditional saves. |
| `GET /api/deployment` | Admin | Optional Dokploy container state for this application. |
| `GET /api/providers`, `GET /api/providers/models` | Admin | Optional Hermes provider state and model catalog. |
| `POST /api/providers/openai-codex/login`, `GET /api/providers/openai-codex/login/{id}`, `POST /api/providers/openai-codex/login/{id}/cancel` | Admin | Hermes-owned device login lifecycle. |

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
Upgrade the main worker image as well as the admin image after applying schema
18 to begin collecting presence and usage.

## Administrative controls

Repository access lists installations and repositories with cursor pagination.
Approve an installation using explicit repository activation or automatic
activation, refresh its inventory, and enable or disable individual repositories.
These operations use the existing GitHub App authorization and activation owners.
Each change requires a reason and records the signed-in administrator's stable
account ID.

Open **Finding decisions** from a published review to inspect an exact finding
occurrence. A human decision refers to that occurrence even if a later review
contains the same fingerprint. Intentional-by-design decisions still require the
accepted ADR snapshot and matching path. Review quality shows the reporting
cohorts and retained feedback backlog; administrators can classify pending
feedback using the existing triage states.

A request's **Run controls** can release a delayed retry, cancel queued or leased
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
revision into the form; saving it creates a new revision.

Provider, model, and reasoning effort apply when a new GitHub review request is
admitted. The selected route is recorded in its exact review contract and sent
explicitly to Hermes. Already admitted requests, including command redelivery,
retain their recorded route. Workers continue to verify the installed profile,
engine, configuration, image, and result budget before execution.

Other policy values load when their owning services start:

| Values | Service to restart |
| --- | --- |
| Active job limit, capacity retry delay, review attempt budget | `review-github-app-worker` |
| Concurrency, lease, heartbeat, Hermes timeout | `review-worker` |
| Publication size, publication attempt budget, feedback instructions | `hermes-review` |
| Code graph enablement and embedding mode | `review-github-gateway` |

Use the normal deployment procedure to drain and restart services. The console
records the last 50 startup observations and their loaded revision. These are
startup records; worker heartbeats and Dokploy container state provide separate
current-state evidence. Code graph options require the graph overlay, and OpenAI
embeddings require the gateway's existing API key. Profile contents, secrets,
container shutdown grace, and graph cache sizing remain deployment configuration.

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
only bounded provider status, model options, Codex device login, polling, and
cancellation operations on private port `9120`. It authenticates the admin API
with the dedicated token. The Hermes dashboard is not published or attached to
the ingress network. When recreating Hermes, recreate its provider-control
companion in the same Compose operation.

In Settings, connect OpenAI Codex, open the displayed OpenAI verification page,
and enter the one-time code. Hermes stores and refreshes the resulting credential.
Cancellation and expiry end polling in the console. Anthropic authentication uses
`hermes auth add anthropic` from the Hermes container's terminal because the pinned
Hermes version provides that external flow. Connection status does not prove that
a model can complete a review.

Without these optional connections, the console reports their unavailable state;
review history, quality evidence, and database-backed controls remain available.

## Add the service

Follow the normal [deployment and upgrade procedure](DEPLOYMENT.md), including
backup and migrations. The panel is one additional service in the existing
Compose application, with its own image and hostname. It serves its compiled
frontend and API together on port `8090`; Node.js is used only during the build.

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

4. After migrations complete, create the first administrator from the panel
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
   review, then use **Users** to add an account. A viewer should see statistics
   and history without access to user administration.

## Accounts and recovery

Authentication uses FastAPI Users, Argon2 password hashes, and revocable
PostgreSQL sessions. HTTPS cookies are HttpOnly and SameSite Strict and expire
after eight hours. There is no public registration or email service. Share an
initial password privately; users can change it under **Your account**.

An administrator can disable an account or set a replacement password. Account
changes revoke its sessions, and logout revokes the current session. The panel
prevents disabling or demoting the last active administrator. Keep access to a
second administrator for password recovery; the bootstrap command deliberately
does not replace existing accounts.

Users and sessions are included in the existing PostgreSQL backup. Back up the
database before upgrading both images. To remove the panel, remove its route
and optional service while preserving PostgreSQL. Removing the panel does not
require deleting its tables. Follow the existing database recovery procedure
for a full version rollback; do not reverse migrations manually.

The panel currently provides deployment-wide visibility and account management.
It does not provide per-repository roles, review retries, queue cancellation, or
a Kanban board.
