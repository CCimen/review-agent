---
sidebar_label: Operations
slug: /operations
title: Operations
status: current
last_verified: 2026-11-17
---

# Operations

Day-two operation and recovery. First deployment, credentials and upgrades are
in [Deploy Review Agent](./DEPLOYMENT.md); every environment variable is listed
in the [configuration reference](#configuration-reference).

## Prerequisites

The [deployment guide](./DEPLOYMENT.md#deploy) covers Compose, Dokploy, Coolify,
Portainer and OpenShift. The public review route targets
`review-admission:8644`. Workers reach the private Hermes API on `8642`.
PostgreSQL and Hermes stay off the public network.

The deployment uses two named volumes:

| Volume | Mounted in | Purpose |
| --- | --- | --- |
| `hermes_review_data` | Profile installer (write); Hermes and workers (read) | Hermes config, provider credentials, sessions, managed skills, plugins, and the installed reviewer receipt. Admission derives the same contract from immutable image files and cannot read this volume. |
| `review_postgres_data` | `review-postgres` at `/var/lib/postgresql/data` | PostgreSQL review state. |

Do not run two Hermes gateways against the same `hermes_review_data` volume.

Two one-shot services run before the live services. `review-profile-install`
installs the selected managed profile under `/opt/data`. `review-db-migrate`
waits for PostgreSQL, applies checksum-verified schema migrations, and creates
or rotates the restricted runtime login. It is the only container that receives
both database URLs. Both finish as `Exited (0)`; inspect their logs when startup
stops. On OpenShift, delete and recreate the two initialization Jobs instead of
running preparation inside a live pod. Manual recovery:

```bash
docker compose run --rm --no-deps review-profile-install
docker compose run --rm --no-deps review-db-migrate
docker compose exec hermes-review review-agent-admin database ready
```

### Profile

`REVIEW_AGENT_PROFILE` selects a trusted bundle key under `bootstrap/profiles`;
the packaged default is `default-standard`. There are no aliases for earlier
profile names, and ADRs are read only from `.review-agent/decisions/`. The
installer rejects an unknown key before changing `HERMES_HOME`. A profile owns
`SOUL.md`, `workspace/AGENTS.md`, and the reviewed skills named in
`profile.json`; it cannot change model, route, tool, authorization, snapshot,
persistence, marker or lifecycle settings. Skill files are trusted,
code-reviewed content; `profile.json` validation does not make arbitrary skill
prose safe.

Keep the selected value in deployment configuration. Run outside Compose, the
installer reuses the last selected profile when neither the flag nor the
environment value is set. The installer replaces managed config, SOUL, AGENTS,
skills and plugin files from source control and records their digests with the
Hermes image, model, reasoning effort and tool-result budget. Admission stores
that contract with every review subject. A worker refuses queued work whose
contract differs from the installed one, and the developer is told to request a
fresh review.

### Database URLs

Use one database per environment. The example Compose network keeps PostgreSQL
private and uses separate credentials:

```text
REVIEW_AGENT_DATABASE_URL=postgresql://review_agent:<owner-password>@review-postgres:5432/review_agent
REVIEW_AGENT_RUNTIME_DATABASE_URL=postgresql://review_agent_runtime:<runtime-password>@review-postgres:5432/review_agent
```

Use the same URL-safe owner value for `REVIEW_AGENT_POSTGRES_PASSWORD` and the
first URL. Generate a different runtime value for the second URL. Hex output
from `openssl rand -hex 32` needs no percent-encoding.

## Connect the model provider

Inside the `hermes-review` container:

```bash
hermes plugins list
hermes model
/opt/review-agent-bootstrap/install.sh
```

Use Hermes' wizard to authenticate the provider selected by
`REVIEW_AGENT_MODEL_PROVIDER`. The default `openai-codex` route uses ChatGPT
device-code OAuth and needs no model API key. The only other supported
provider is `anthropic`; Hermes documents that Anthropic OAuth requires Claude
Max plus extra-usage credits, while its API-key route is independent. Rerunning the installer restores `REVIEW_AGENT_MODEL_PROVIDER`,
`REVIEW_AGENT_MODEL` and `REVIEW_AGENT_REASONING_EFFORT` after the wizard stores
credentials. `REVIEW_AGENT_MODEL` has no default; a deployment without it does
not start.

Once the console has saved settings, their model governs new reviews instead of
`REVIEW_AGENT_MODEL`; change it in **Settings**. A model changed there applies
to new requests, and queued reviews keep the route they were admitted with.

An environment change applies after the installer and services restart.
Rerunning the installer with a different provider, model or effort rewrites the
managed Hermes configuration, so reviews queued before it fail with
`review_contract_changed` and must be requested again. Verify:

```bash
curl -fsS http://127.0.0.1:8642/health
hermes status
hermes doctor
hermes plugins list
```

Authentication and billing differ between providers. Use the
[Hermes provider guide](https://hermes-agent.nousresearch.com/docs/integrations/providers),
its [model catalog](https://hermes-agent.nousresearch.com/docs/reference/model-catalog)
for model IDs, and its
[environment reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)
for API-key and custom-endpoint variables. A controlled review after deployment
is the final proof that the credential is entitled to the selected model.

## Health checks

```bash
review-agent-admin doctor
review-agent-admin queues inspect
```

`doctor` reports expired leases, review-capacity exhaustion, and work that has
been due for at least 90 seconds with neither a live worker nor a current lease.
Scheduled retries and quota waits do not trigger the missing-worker check. Dead
letters stay in the queue report as terminal history but do not consume
capacity or make `doctor` and dry-run smoke tests fail.

`doctor` reports `documentation_app` separately from core readiness. A missing
Checks permission or event subscription does not mark a working code-only
deployment unhealthy.

The console **Health** view shows due-work ages, capacity and quota waits, live
worker totals, and recent progress for each queue. **Activity** and
**Pull requests** show each request's recorded phase, scheduled retry or quota
check, and **Waiting for recovery** when a worker lease expires. There is no
completion percentage. Published results with incomplete or unknown coverage are
labeled **Incomplete**; their finding count is not a clean result. GitHub
receives only the final result or failure notice; these reports do not change
GitHub checks or merge requirements.

## GitHub trigger

The App delivers `issue_comment`, `pull_request`, `check_run`, `installation`
and `installation_repositories` events to `/webhooks/github-app`.
`issue_comment` carries `/review` and feedback commands; `pull_request` and
`check_run` drive automatic documentation review and its re-runs; the
installation events keep the repository inventory current. Admission persists
the signed delivery before any provider read. The App worker then checks, through
the private gateway, the sender's current access and the repository's enabled
state before creating a run; see
[who can request a review](./GITHUB_APP_PILOT.md#who-can-request-a-review).
Feedback commands follow the same durable path, and the gateway rechecks the
sender's write or admin permission and the open pull request before anything is
recorded.

Webhooks and inventory sync record repositories but never enable one. A
repository is enabled by `github-app onboard`, or, under an installation
approved for automatic activation, by the first authorized `/review` in it. See
[Approve the installation](./GITHUB_APP_PILOT.md#4-approve-the-installation).

Set `REVIEW_AGENT_FEEDBACK_ENABLED=true` to show the copyable feedback commands
in the review comment.

## Run a review

On an open pull request, including a draft, an authorized user comments:

```text
/review
```

Each `/review` after the previous run reaches a terminal state starts a new
round (`Review 2`, ...), including a rerun of the same base and head. A second
`/review` on the same pull request while a run is active is a duplicate.
Different pull requests run concurrently. Applying a native suggestion does not
mark its finding resolved; run CI and post a new `/review`. Review mechanics are
in [How reviews work](./HOW_REVIEWS_WORK.md).

## Developer feedback

The feedback commands and what each records are in
[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md).

## Runbook

### Queue

```bash
review-agent-admin queues inspect
review-agent-admin jobs list --limit 100
```

`queues inspect` reports webhook intake, review jobs and publication delivery
from the same reader the console uses. Publication counts include final review
comments and pending failure notices.

| Signal | Meaning |
| --- | --- |
| `waiting`, `due`, `delayed` | Waiting work, split by whether its scheduled availability has arrived. Due work may still need a scheduler slot. |
| `oldest_due_age_seconds` | Time since the oldest waiting item became due, excluding its scheduled delay. |
| `oldest_waiting_age_seconds` | Total waiting age; for publications, delivery lag since generation. |
| `next_available_at` | The next scheduled retry or cooldown expiry that makes delayed work due. |
| `live_workers`, `worker_capacity` | Recently reporting worker instances and their advertised capacity, using the returned `worker_stale_after_seconds` threshold. |
| `leased`, `expired_leases` | Claimed work and the subset whose lease expired. |
| `capacity_waiting` | Webhook requests deferred by the last admission capacity check. |
| `cooldown_waiting`, `cooldown_until` | Review jobs waiting for model-account quota and the next quota deadline. GitHub retry deadlines are included in `next_available_at`. |
| `last_progress_at`, `last_heartbeat_at` | The latest persisted start or completion, and the latest heartbeat of leased work. A heartbeat proves liveness, not completion. |
| `last_terminal_reason`, `last_terminal_at` | The latest terminal reason and its time, including rejected webhook requests. |

Idle queues have zero waiting and leased counts and null wait ages.

Release a delayed queued retry, or cancel a job's active run:

```bash
review-agent-admin jobs retry <id>
review-agent-admin jobs cancel <id>
```

`jobs retry` cannot revive failed or dead-letter work: inspect the failure, fix
its cause, and post a new `/review`. `jobs cancel` fences a leased worker by
failing the owning run and reconciling its job in one transaction; it also
retires a publication waiting on a GitHub cooldown.

### Runs

```bash
review-agent-memory runs --repo <org>/<repo> --limit 10
review-agent-memory runs --repo <org>/<repo> --stats
review-agent-memory runs --repo <org>/<repo> --pr <number> --limit 10
review-agent-memory runs --repo <org>/<repo> --pr <number> --stats
```

`--stats` also counts terminal runs by `failure_code`, failed attempts by cause,
and the tokens of attempts that reported usage beside the number of attempts
started. `runs --failed` shows failure-status delivery state, attempt count,
maximum attempts and the last delivery failure.

A healthy run moves through `accepted`, `fetching_pr`, `collecting_diff`,
`reviewing`, `rendering`, `publishing` and `posted`.

### Diagnose a review run

```bash
review-agent-memory runs --run-id <id> > /opt/data/review-memory/run-<id>.json
```

The JSON report covers one run:

- status, `failure_code` and `failure_detail`;
- the requested provider, model and reasoning effort;
- each job (the main job and any parts) with its attempts and lease generation;
- the newest 100 attempts, with their tokens and why each ended;
- part progress and coverage counts, with at most 20 unavailable and 20
  truncated paths.

It contains no review text, comment bodies or requester identities, and stays
under 512 KiB so a tool that can only read container files can open it.

`failure_detail` is at most 300 characters: one of the reviewer's own messages
or Hermes error text, which Hermes redacts. Treat it as untrusted text. When
retries run out, the run's `failure_detail` repeats the last attempt's cause.

Hermes retries each model call up to `agent.api_max_retries` times (8 in the
managed `config.yaml`) with backoff from 2 to 60 seconds, which rides out a
provider overload of about 3 to 4.5 minutes before the turn fails and the worker
schedules a new attempt.

Inspect coverage and publication:

```bash
review-agent-memory coverage --run-id <id>
review-agent-memory publications --repo <org>/<repo> --pr <number>
```

A GitHub comment POST can succeed even when its response reports a transient
failure. Before retrying, the publisher looks for a recent comment with the same
App author, publication marker, part number and body, including on its last
attempt. If delivery cannot be confirmed, the failure status warns that review
comments may already exist; check GitHub before requesting another review.

### Stalled runs

After a crash, mark stale runs failed. The publisher then claims and delivers
their failure statuses:

```bash
review-agent-memory runs --mark-stalled --stale-after-minutes 10 --repo <org>/<repo> --pr <number>
```

The publisher leases each failure status from PostgreSQL, recovers an existing
marker after a crash, retries transient GitHub failures, and stops after the
stored attempt limit. A later review of the same pull request suppresses
obsolete status work. A closed or merged pull request gets no new failure
status; one already posted before the close is kept, not edited. Manual recovery
reopens an exhausted status with a fresh attempt budget.

### Failure codes

Review run attempts (`failure_code`):

| Code | Meaning |
| --- | --- |
| `hermes_agent_error` | Hermes ended the turn early, for example after its model-call retries ran out during a provider overload. |
| `hermes_output_truncated` | The model's output was cut off. |
| `review_not_terminal` | The turn ended before the review recorded a terminal state. |
| `hermes_http_<status>` | Hermes answered with that HTTP status. |
| `hermes_timeout`, `hermes_unreachable` | The worker's request to Hermes timed out or could not connect. |
| `source_unavailable`, `preflight_rejected` | Review source checks failed before the model ran. |

Run and publication states:

| Symptom | Meaning |
| --- | --- |
| `running` with an old heartbeat, or `stalled` | Execution stopped before a terminal state. Mark stale runs failed before retrying. |
| `publish_failed` | GitHub publication was attempted and failed; inspect `failure=`. |
| `body_too_large` | The review could not fit within the per-comment byte budget. |
| `review_contract_changed` | The installed reviewer changed after the review was queued: the profile, skill or plugin bundle (an upgrade), or the managed Hermes configuration (the installer rerun with a different provider, model or effort). A model changed in console **Settings** does not cause this; queued reviews keep their route. Make every service run the same release and installed configuration, then request a fresh review. |
| `stale` | The pull request base or head changed before posting. |
| `pr_not_open` | The pull request was closed or merged during the review. The run stops at its next tool call without a retry or failure comment. |
| `finding_decisions_changed` | A human decision on one of the findings changed while the publication waited. Feedback on the same finding identity in another pull request also counts. Request a fresh `/review`. |
| `finding_groups_changed` | A newer review published its finding groups while this older write was in flight. The newer groups stay current. |

A failed or stale publication never activates its proposed groups, but a comment
already sent to GitHub may still be visible.

Admission (App worker log `status=` and `reason=`):

| Reason | Meaning |
| --- | --- |
| `review_waiting_for_capacity` | `status=received`. The active-job limit is reached; the request is retried every `REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS` without using processing attempts, rechecking authorization and the pull request each time. |
| `review_admission_busy` | Lock contention; uses the normal bounded processing-attempt budget. |
| `review_admission_expired` | `status=failed`. The request waited longer than `REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS`. After fixing capacity or worker availability, post a new `/review`; redelivering the same webhook GUID does not reopen it. |

An expired request leaves a terminal delivery receipt. Code-review receipts
erase their normalized payload; documentation receipts keep their bounded event
envelope until explicit pruning.

GitHub gateway errors name the endpoint, for example
`github_403_get_pull_request`, `github_403_list_issue_comments`,
`github_403_create_issue_comment` or `github_403_create_pull_request_review`.
They identify a missing App permission or an organization approval. The token
scopes are in [Security model](./SECURITY.md#github-credential-boundaries).

`review-agent-memory` errors (one bounded JSON object on standard error, never
the original exception message; argument parsing errors keep argparse usage
output and exit `2`):

| Error code | Retry? | Exit | Operator action |
| --- | --- | ---: | --- |
| `invalid_command_input` | no | 64 | Correct the requested limit or argument. |
| `command_rejected` or `database_operation_failed` | no | 65 | Correct the requested scope; check application health for a database failure. |
| `internal_error` | no | 70 | Report the included exception type. |
| `artifact_io_failed` | no | 74 | Repair the private file path or permissions. |
| `database_unavailable` or `database_busy` | yes | 75 | Retry after PostgreSQL connectivity or contention recovers. |
| `invalid_configuration` or `database_not_ready` | no | 78 | Correct configuration or complete the database migration and readiness steps. |

## Capacity and scaling

Scaling procedures and examples are in
[Scale and operate the queue](./DEPLOYMENT.md#scale-and-operate-the-queue).

### Queue rules

- Total review slots = `review-worker` replicas × `REVIEW_AGENT_WORKER_CONCURRENCY`.
  A worker claims only when a slot is free.
- `REVIEW_AGENT_ACTIVE_JOB_LIMIT` bounds queued, leased and
  publication-waiting reviews. At the limit, the App worker keeps the accepted
  request and retries admission without using processing attempts. Each retry
  reauthorizes through GitHub; the default 300 seconds is about 24 permission
  and snapshot reads per hour per waiting review, plus token exchanges.
- Keep `REVIEW_AGENT_JOB_HEARTBEAT_SECONDS` below half of
  `REVIEW_AGENT_JOB_LEASE_SECONDS`.
- Changing `REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS` applies to
  pending requests.

### GitHub rate limits

GitHub cooldowns keep the `Retry-After` deadline (seconds or HTTP date) and,
when the remaining quota is zero, the `X-RateLimit-Reset` time. Admission,
publication and failure-status retries wait until both the provider deadline and
the normal retry delay have passed. PostgreSQL stores that time, so a restart or
explicit retry cannot bypass it.

- Waiting uses no attempts; failed calls still use the attempt budget.
- A rate limit without usable timing waits at least one minute. A transient
  error without usable timing keeps the normal transport retry.
- Review admission still expires at its age limit, without another GitHub call,
  even if the provider deadline is later. Valid provider deadlines are never
  shortened.
- A newer successful review suppresses an older pending failure status, even in
  cooldown. Feedback acknowledgements honor the provider deadline and have no
  separate age limit. Permanent denials stay terminal.

### Database connections

Plan the maximum pool capacity against the PostgreSQL connection limit:

```text
review-worker replicas × (REVIEW_AGENT_WORKER_CONCURRENCY + 1)
+ review-github-app-worker replicas × 2
+ review-publisher replicas × 2
+ review-admission replicas × 4
+ review-github-gateway replicas × 4
+ hermes-review replicas × 4
+ review-admin replicas × 6 (four application and two authentication connections)
+ operator command processes × 4 (normally one in use)
```

Pools open connections on demand. Keep the total below the database limit with
headroom for maintenance and monitoring. The bundled database allows
`REVIEW_AGENT_POSTGRES_MAX_CONNECTIONS` (default 200); an external database owns
its own limit.

### Worker termination and lease recovery

`SIGTERM` stops a worker from claiming more work. A job that has not yet started
its Hermes request goes back to the queue without using an attempt. A review
already running continues and keeps heartbeating until it finishes or the
platform force-terminates the process after
`REVIEW_AGENT_WORKER_TERMINATION_GRACE_SECONDS`; the worker never writes a
guessed terminal state.

After termination, the last heartbeat stands until its lease expires. Another
worker then requeues the job with a new lease generation, or dead-letters it when
attempts are exhausted. With PostgreSQL healthy, at least one worker alive and
no recovery backlog, the bound from `SIGTERM` to requeue is:

```text
termination grace + lease duration + recovery interval
```

With the defaults, `150 + 120 + 30 = 300 seconds`; from the force-termination
point, 150 seconds. The old Hermes session may still finish upstream, but the
generation fence rejects its late writes. Never shorten recovery by releasing a
lease while that model call can still run.

### Review limits

The reviewer has no repository-size quota. It reads in bounded pages and
reports incomplete coverage when one of these boundaries stops it:

| Boundary | Limit |
| --- | --- |
| Changed files | GitHub lists at most 3,000 files per pull request. If even a one-file page exceeds the response budget, registration stays incomplete. |
| Source files | Read in line pages at the exact revision. Files above 1 MB use the raw Contents response up to a 2 MB per-request memory guard; larger or binary files are unavailable and keep coverage incomplete. A source page carries at most 400 lines. |
| Patches GitHub omits | For an added UTF-8 file up to 1 MB, the gateway builds the patch from head content, up to 200,000 characters. Otherwise the file's diff stays incomplete. |
| Tool results | `plugins.entries.review-agent-tools.settings.result_max_chars` (default 160,000), further held to the Hermes per-result and per-response limits for the session's model (100,000 and 200,000 characters for windows of 166,667 tokens or more). A single-path diff or source page holds at most a seventh of the per-result limit. A batched diff result holds at most a fifth of the smaller of `result_max_chars` and the per-response limit (32,000 by default); one diff call accepts up to 50 paths, and the review procedure allows at most four batch calls in one model response. |
| Historical context | 200 paths per call. |
| Findings | 200 per atomic record; more rejects the record. At most 12 native suggestions per round. |
| Publication | `REVIEW_AGENT_PUBLISH_MAX_BYTES` (65,000 UTF-8 bytes per comment part, including framing and metadata). Larger reviews are split into continuation comments. |
| Diff cache | At most 16 snapshots and 32 MiB per tool process; eviction causes a fresh read, never a skipped file. Debug logs from `review_agent_tools.review.diff_cache` report hits and oversized snapshots without source content. |

An INFO log from `review_agent_tools.review.source_tools` reports
`Java guidance returned` with the run ID and profile digest when the Java
companion is delivered. It confirms delivery to the model, not that every check
was applied.

The managed configuration does not set `agent.max_turns` or
`context_file_max_chars`; Hermes runs turns to completion and derives the
context-file allowance from the model's window. See the upstream
[Hermes configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration).

### Split reviews

A code review whose diff exceeds one session's largest part is reviewed in
parts; [Large pull requests](./HOW_REVIEWS_WORK.md#5-large-pull-requests)
describes how. Documentation reviews are never split.

| Rule | Value |
| --- | --- |
| A review is split above | the largest part |
| Preferred part | about half the context window: about 372,000 diff characters at 500,000 tokens, 192,000 at 272,000 |
| Largest part | below the compression threshold: about 608,000 diff characters at 500,000 tokens, 321,000 at 272,000 |
| Parts in one review | 12 |
| Jobs of one review that run at once | 3, and at most the model connection's concurrency minus one |
| Report one part hands in | 12,307 serialized characters by default (a thirteenth of the smaller of `result_max_chars` and the per-response limit) |
| Changed file without a GitHub patch | counted as 48 characters per changed line |

Cost and accounting:

- A split review runs one short registration session, one session per part, and
  one final session. Model usage grows with the diff.
- Each part counts as one review job against the model connection's and the
  team's concurrency limits and waits for model quota like any review.
- A part that uses up `REVIEW_AGENT_JOB_MAX_ATTEMPTS` fails the whole review;
  nothing partial is published.
- The console shows a split review as one request with usage summed over its
  parts. `review-agent-admin jobs list` names each job's `part`; `0` is the main
  job. Queue health does not count a main job waiting for its parts as waiting
  or due work.
- A file whose own diff exceeds a part, and files after the twelfth part, are
  not read and appear in the coverage details. The gateway log records each
  session's window and each split's part sizes.

## Documentation review operation

[Documentation review](DOCUMENTATION_REVIEW.md) covers manual requests,
automatic opt-in, scope and results. The deployment switch in console
**Settings** defaults off. Team defaults and repository overrides keep their
modes while the deployment switch is off. Changing a team default to Off does not
override an explicit Automatic repository setting.

To stop documentation spending, turn off the deployment switch; for a narrower
stop, set a repository to Off or back to inheritance. Retained results and usage
stay available. The repository console checks the stored installation grant and
offers an exact-revision configuration **Refresh**; neither grants GitHub access
or runs a model.

## Database access and retention

The migration owner is available only to the one-shot database job. It must own
the application schema, have `CREATEROLE`, and be able to grant `CONNECT` on the
target database; it does not need superuser. Live services use the shared
runtime login, which can read and write application tables and sequences but
cannot change the schema or migration ledger.

`database prepare` is idempotent. It checks the effective connection
parameters, applies migrations, and creates or rotates one marked runtime role.
It refuses to adopt an unrelated or privilege-expanded role with the same name,
and finishes by connecting as the runtime login:

```bash
review-agent-admin database prepare
review-agent-admin database ready
```

To rotate the runtime password, update `REVIEW_AGENT_RUNTIME_DATABASE_URL`, run
the one-shot database job with both URLs available, and restart the live
services. To roll back, put the previous password back, run `database prepare`
again, and restart. Existing connections stay valid until they reconnect, so do
either as one maintenance operation. Never give the migration-owner URL to a
live service.

### Retention ownership

Durable history is kept unless this table says otherwise:

| Tables | Owner | Default and exemption |
| --- | --- | --- |
| `schema_migrations` | Release operator | Permanent checksum ledger; never edit or prune. |
| `github_app_installations`, `github_app_repository_access`, `github_app_installation_events`, `github_app_repository_access_events` | Platform operator | Preserve current authorization and its audit history. |
| `github_webhook_deliveries` | Platform operator | Preserve active deliveries. Old terminal `accepted`, `ignored`, `rejected` and `failed` receipts may be pruned only with the bounded command below. |
| `repositories`, `pull_requests`, `review_subjects`, `review_runs`, `review_jobs`, `review_parts`, `review_run_files`, `review_file_reads`, `review_decision_snapshots`, `review_guidance_snapshots` | Repository owner | Preserve review identity, coverage, lifecycle, and the guidance and decision context each run used. |
| `model_executions`, `review_attempt_usage`, `review_attempt_failures` | Platform operator | Preserve each attempt's model account, token usage and failure cause; usage reports and run diagnostics read them. |
| `review_dispatch_turns` | Platform operator | Scheduling state: one row per team with its last claim time. Not history. |
| `worker_instances`, `worker_events` | Platform operator | Liveness only. Workers delete instances not seen for seven days and keep the newest 1,000 events per worker. |
| `documentation_reviews`, `documentation_admissions` | Repository owner | Preserve exact scope, evidence, results and admission provenance with review history. |
| `repository_documentation_configuration` | Repository maintainer | Keep the latest configuration inspection; a later Refresh replaces it. Display state, not review authority. |
| `finding_identities`, `finding_occurrences`, `finding_suggestions`, `finding_decisions`, `intentional_design_evidence`, `decision_audit`, `pull_request_finding_references`, `pull_request_finding_groups`, `finding_group_changes` | Repository owner | Preserve finding history, grouping and human decisions. |
| `publications`, `publication_parts`, `publication_findings` | Repository owner | Preserve exact publication and recovery evidence. |
| `admin_users`, `admin_sessions` | Deployment owner | Preserve console accounts and revocable sessions; treat backups as credential-bearing data. |
| `admin_registration_policy`, `admin_email_settings` | Deployment owner | Preserve the registration policy and email delivery settings. The SMTP password is stored encrypted with `REVIEW_AGENT_EMAIL_SECRET_KEY`. |
| `admin_registration_requests`, `admin_oidc_requests` | Deployment owner | Short-lived: pending registrations and sign-in requests hold only token digests and are deleted on use or expiry. |
| `teams`, `team_members`, `team_repositories`, `repository_requests`, `repository_review_requesters` | Deployment owner | Preserve team access, repository ownership, request decisions and requester grants. A removed requester grant is deleted. |
| `model_connections`, `model_accounts`, `team_model_policies`, `model_login_sessions` | Deployment owner | Preserve model connection registrations, the account bound to each, team routing and the record of console sign-in attempts. Provider credentials are not stored here; they live on the Hermes volume. |
| `integrations`, `integration_teams` | Deployment owner | Preserve application integrations and their team scope, including revoked ones. Only a digest of each credential is stored. |
| `deployment_settings`, `service_settings_loads` | Deployment owner | Preserve every saved settings revision. `service_settings_loads` gains one row per service start and is not pruned. |
| `admin_run_actions` | Deployment owner | Preserve who retried, cancelled or marked a run stalled, and why. |
| `admin_audit_events` | Deployment owner | Preserve access changes and human audit evidence. Successful application read receipts can be pruned only with the bounded integration command below. |
| `review_quality_feedback`, `review_quality_feedback_triage`, `processed_feedback_events` | Quality owner | Preserve feedback, triage and idempotency receipts. |
| `coach_runs`, `coach_candidates`, `coach_intervention_outcomes` | None | No longer read or written. Kept for one release so the previous release still runs after a rollback; a later migration drops them. |
| `verification_runs`, `candidate_verifications`, `candidate_reconciliations` | None | Empty and no longer read or written. Kept for one release so the previous release still runs after a rollback; a later migration drops them. |

Raw webhook bodies are not stored. Terminal documentation deliveries keep their
bounded normalized event envelope until the pruning command removes the
delivery; the admitted run keeps its own provenance receipt.

Preview terminal webhook history before a cutoff. The limit is also capped by
`REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS`:

```bash
review-agent-admin database prune-webhook-deliveries \
  --before 2026-01-01T00:00:00Z \
  --limit 100 \
  --actor "operator:alice" \
  --reason "approved retention window"
```

The JSON receipt reports the matched rows, oldest processed timestamp, and
whether more rows remain. Take and verify a backup, then repeat the command with
`--apply`. Each apply deletes one oldest-first batch; concurrent applies
serialize, so `more` stays accurate. Active deliveries and other tables are
untouched.

Every successful application report read appends an audit event. Review Agent
does not delete these automatically; choose a retention window and cleanup
cadence before enabling scheduled reports. Preview one batch:

```bash
review-agent-admin database prune-integration-reads \
  --before 2026-01-01T00:00:00Z \
  --limit 100 \
  --actor "operator:alice" \
  --reason "approved application read retention window"
```

The receipt identifies `integration_reads` and reports `oldest_recorded_at`.
Only successful `integration_read` events before the cutoff are eligible;
credential creation and revocation, failed reads, human audit events and newer
receipts are kept. Keep any required export or verified backup, then apply with
`--apply`. Each invocation deletes at most one batch; `more: true` means another
remains. Save each CLI receipt with the deployment's maintenance records, and
schedule repeated runs through existing maintenance tooling.

## Backup and recovery

PostgreSQL may contain unpublished findings and human-entered reasons; store
backups securely. Create a logical backup from the Compose host:

```bash
docker compose exec -T review-postgres \
  pg_dump --username=review_agent --dbname=review_agent --format=custom \
  > review-agent.dump
```

Test recovery in a fresh database before relying on a backup. Stop the services
that read or write Review Agent state:

```bash
docker compose stop review-admission review-github-app-worker review-worker \
  review-publisher review-github-gateway hermes-review
```

- **Restore test on the production cluster:** use
  `pg_restore --no-owner --no-privileges --exit-on-error`, then drop the
  temporary database after verification. Copying grants would give the runtime
  role access to another database, which `database prepare` rejects.
- **Production recovery into a separate cluster:** restore with
  `pg_restore --exit-on-error`, set the restored database's owner and runtime
  URLs, run `review-agent-admin database prepare` and
  `review-agent-admin database ready`, then point the environment at the
  restored database and redeploy.

Some migrations change the schema in ways older images cannot read (for
example 016, 029 and 037); older images must not run against the migrated
schema. Before such an
upgrade, drain admission, active reviews and publications, stop every
application service including `review-admin`, and deploy all services from the
same revision. Rolling back requires the pre-upgrade backup with its matching
images; never remove columns or reverse a migration in place.

## Updating and validation

Upgrade through the [deployment procedure](DEPLOYMENT.md#upgrade-and-roll-back-production).
On redeploy, `review-profile-install` refreshes the managed profile and
`review-db-migrate` verifies and applies migrations before the gateway starts.

`HERMES_IMAGE` is pinned to a Hermes release tag and its immutable digest. Change
it only through a reviewed dependency update, never to a moving `latest` or
`main` tag. The main image keeps the Python version that Hermes supports; the
pins are listed in
[RELEASING.md](https://github.com/CCimen/review-agent/blob/main/RELEASING.md#runtime-dependency-pins).

## Quality report and triage

The weekly and monthly flow is in
[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md).

```bash
review-agent-memory quality --days 30
review-agent-memory quality --days 30 --repo <org>/<repo> --json
```

The report shows explicit signal counts, denominators, the current triage
backlog (regardless of the activity window), coverage, and review-contract
cohorts. It does not infer accuracy from missing feedback.

Only an operator classifies a missed issue. Find the feedback row in an
[export](#offline-operator-tools), then append a triage state:

```bash
review-agent-memory triage-feedback <feedback-id> \
  --status actionable \
  --stable-key review-rule.auth-boundary \
  --target-owner review_rule \
  --actor "github:<operator>" \
  --reason "Two independent reviews missed the same authorization boundary."
```

List, show and decide findings:

```bash
review-agent-memory list --repo <org>/<repo>
review-agent-memory show <fingerprint-prefix> --repo <org>/<repo>

review-agent-memory decide <fingerprint> false_positive \
  --repo <org>/<repo> \
  --pr <number> \
  --local-reference F2 \
  --actor "github:alice" \
  --reason "The scope-checked repository binds resource_scope_id before this query." \
  --expires-days 180

review-agent-memory decide <fingerprint> resolved \
  --repo <org>/<repo> \
  --pr <number> \
  --local-reference F2 \
  --actor "github:alice" \
  --reason "Fixed in the latest commit."
```

Other decision values are `accepted_risk`, `duplicate` and `reopen`. Prefer
exact observation IDs or pull-request references (`F2`). Suppression trust rules
are in [Security model](SECURITY.md#human-governed-suppressions).

## Offline operator tools

`export` runs outside the live review path. It reads PostgreSQL and writes one
repository's review history as a private JSON file for an operator, at most
`--row-limit` rows per table (capped by
`REVIEW_AGENT_OPERATOR_EXPORT_MAX_ROWS`). The webhook reviewer never reads the
file, and it publishes nothing. An export can contain maintainer-entered
reasons or repository text; scrub it before committing or sharing, and never
paste a raw database export into a model.

```bash
review-agent-memory export \
  --repo <org>/<repo> \
  --row-limit <per-table-row-limit> \
  --output /opt/data/private-review/export.json
```

## Configuration reference

Every variable the Compose files interpolate. **Required** variables stop
`docker compose` when unset. Settings saved in the admin console override the
corresponding environment defaults; see
[Environment defaults and admin controls](./ADMIN_PANEL.md#environment-defaults-and-admin-controls).
Services read these values at startup.

| Variable | Default | Used by | Meaning |
| --- | --- | --- | --- |
| **Images and platform** | | | |
| `HERMES_IMAGE` | `nousresearch/hermes-agent:v2026.8.31` with the digest pinned in `compose.yaml` | Build argument of the main image | Hermes base image, pinned by tag and digest. |
| `REVIEW_AGENT_IMAGE` | `review-agent:local` | Every service except `review-postgres` and `review-admin` | Main image. In production, the manifest reference from `IMAGE-DIGESTS.txt`. |
| `REVIEW_AGENT_ADMIN_IMAGE` | `review-agent-admin:local` | `review-admin` | Console image. |
| `POSTGRES_IMAGE` | `postgres:17-alpine` | `review-postgres` | Bundled database image; pin its digest in production. |
| `REVIEW_AGENT_INGRESS_NETWORK` | `dokploy-network` | `review-admission`, `review-admin` | External reverse-proxy network. |
| `TZ` | `UTC` | `hermes-review`, `review-admission` | Process time zone. |
| `REVIEW_AGENT_PROFILE` | `default-standard` | `review-profile-install`, `hermes-review`, `review-admission`, `review-github-gateway`, `review-github-app-worker`, `review-admin` | Managed profile key under `bootstrap/profiles`. |
| **Database** | | | |
| `REVIEW_AGENT_POSTGRES_PASSWORD` | required | `review-postgres` | Migration-owner password; the same value as in `REVIEW_AGENT_DATABASE_URL`. |
| `REVIEW_AGENT_DATABASE_URL` | required | `review-db-migrate` | Migration-owner URL. Only the one-shot database job receives it. |
| `REVIEW_AGENT_RUNTIME_DATABASE_URL` | required | `review-db-migrate`; every live service that uses the database, as its `REVIEW_AGENT_DATABASE_URL` | Restricted runtime login. |
| `REVIEW_AGENT_POSTGRES_MAX_CONNECTIONS` | `200` | `review-postgres` | `max_connections` of the bundled database. Size it with the [pool formula](#database-connections). |
| **GitHub App and intake** | | | |
| `REVIEW_AGENT_GITHUB_APP_ID` | required | `review-github-gateway`, `review-admin` | App ID. |
| `REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH` | `./secrets/github-app-private-key.pem` | Secret mounted in `review-github-gateway`, `review-admin` | Host path of the App private key. Never put the PEM contents in the environment. |
| `REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET` | required | `review-admission` | Webhook signature secret. |
| `REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES` | `2097152` | `review-admission`, `review-admin` | Maximum webhook body; 2,097,152 is also the protocol maximum. |
| `REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS` | `8` | `review-admission`, `review-admin` | Concurrent signed requests per admission process. |
| `REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS` | `30` | `review-admission`, `review-admin` | Admission request timeout. |
| `REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS` | `8` | `review-github-gateway`, `review-admin` | Concurrent GitHub responses per gateway process. |
| `API_SERVER_KEY` | required | `hermes-review`, `review-worker`, `review-github-gateway`, `review-provider-control` | Key for the private Hermes API. |
| **Model** | | | |
| `REVIEW_AGENT_MODEL_PROVIDER` | `openai-codex` | `review-profile-install`, `hermes-review`, `review-admission`, `review-github-gateway`, `review-github-app-worker`, `review-admin` | Hermes provider route. |
| `REVIEW_AGENT_MODEL` | required | same as above | Exact model name. |
| `REVIEW_AGENT_REASONING_EFFORT` | `xhigh` | same as above | Reasoning effort. |
| `REVIEW_AGENT_HERMES_TIMEOUT_SECONDS` | `7200` | `review-worker`, `review-admin` | Maximum duration of one Hermes request. |
| `REVIEW_AGENT_HERMES_CONTROL_TOKEN` | required with `compose.providers.yaml` | `hermes-review`, `review-provider-control`, `review-admin` | Token for the provider-connection companion and the loopback Hermes dashboard. |
| `HERMES_DASHBOARD` | `0` | `hermes-review` | Hermes dashboard. `compose.providers.yaml` sets it to `1` on loopback only. |
| **Admission (App worker)** | | | |
| `REVIEW_AGENT_ACTIVE_JOB_LIMIT` | `100` | `review-github-app-worker`, `review-admin` | Maximum queued, leased and publication-waiting reviews. |
| `REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS` | `86400` | `review-github-app-worker`, `review-admin` | Age after which a waiting request fails with `review_admission_expired`. |
| `REVIEW_AGENT_GITHUB_APP_CAPACITY_RETRY_SECONDS` | `300` | `review-github-app-worker`, `review-admin` | Interval between capacity checks for a waiting request. |
| `REVIEW_AGENT_JOB_MAX_ATTEMPTS` | `3` | `review-github-app-worker`, `review-admin` | Attempt budget of each review job. |
| `REVIEW_AGENT_JOB_PRIORITY` | `0` | `review-github-app-worker`, `review-admin` | Priority of new review jobs. |
| `REVIEW_AGENT_POLICY_REVISION` | `policy-v1` | `review-github-app-worker` | Label stored with each review subject and part of its identity (at most 120 characters). Leave at the default. |
| `REVIEW_AGENT_GITHUB_APP_POLL_SECONDS` | `2` | `review-github-app-worker` | Wait between checks when no webhook delivery is ready. |
| `REVIEW_AGENT_GITHUB_APP_RECOVERY_SECONDS` | `30` | `review-github-app-worker` | Interval between recoveries of deliveries whose lease expired. |
| `REVIEW_AGENT_GITHUB_APP_RECOVERY_BATCH_SIZE` | `100` | `review-github-app-worker` | Expired deliveries recovered per pass. |
| `REVIEW_AGENT_GITHUB_APP_DATABASE_BACKOFF_SECONDS` | `1` | `review-github-app-worker` | First wait after a transient database error; doubles on each further error. |
| `REVIEW_AGENT_GITHUB_APP_DATABASE_BACKOFF_MAX_SECONDS` | `30` | `review-github-app-worker` | Upper bound of that wait; must be at least the first wait. |
| **Queue and worker** | | | |
| `REVIEW_AGENT_WORKER_CONCURRENCY` | `4` | `review-worker`, `review-admin` | Simultaneous reviews per worker process. |
| `REVIEW_AGENT_JOB_LEASE_SECONDS` | `120` | `review-worker`, `review-admin` | Review job lease. |
| `REVIEW_AGENT_JOB_HEARTBEAT_SECONDS` | `30` | `review-worker`, `review-admin` | Lease heartbeat; below half the lease. |
| `REVIEW_AGENT_JOB_RETRY_SECONDS` | `30` | `review-worker`, `review-admin` | Delay before retrying a failed attempt. |
| `REVIEW_AGENT_JOB_POLL_SECONDS` | `2` | `review-worker`, `review-admin` | Wait between checks of an idle queue. |
| `REVIEW_AGENT_JOB_RECOVERY_SECONDS` | `30` | `review-worker`, `review-admin` | Interval between expired-lease recoveries. |
| `REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE` | `100` | `review-worker`, `review-admin` | Expired leases recovered per pass. |
| `REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS` | `900` | `review-worker`, `review-admin` | Wait time that offsets one priority point. |
| `REVIEW_AGENT_WORKER_TERMINATION_GRACE_SECONDS` | `150` | `review-worker` (stop grace period) | Maximum drain before the platform force-terminates a worker. OpenShift uses the `WORKER_TERMINATION_GRACE_SECONDS` template parameter. |
| **Publication** | | | |
| `REVIEW_AGENT_PUBLISH_MAX_BYTES` | empty (65,000 in code) | `hermes-review`, `review-publisher`, `review-admin` | UTF-8 bytes per comment part, including framing and metadata; 1,000 to 65,000. |
| `REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS` | `3` | `hermes-review`, `review-admin` | Attempt budget frozen with each publication. |
| `REVIEW_AGENT_PUBLICATION_LEASE_SECONDS` | `120` | `review-publisher`, `review-admin` | Publication lease. |
| `REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS` | `30` | `review-publisher`, `review-admin` | Publication heartbeat. |
| `REVIEW_AGENT_PUBLICATION_RETRY_SECONDS` | `30` | `review-publisher`, `review-admin` | Delay before retrying a failed delivery. |
| `REVIEW_AGENT_PUBLICATION_POLL_SECONDS` | `2` | `review-publisher`, `review-admin` | Wait between checks of an idle publication queue. |
| `REVIEW_AGENT_FEEDBACK_ENABLED` | `false` | `hermes-review`, `review-admin` | Show copyable feedback commands in the review comment. |
| **Operator commands** | | | |
| `REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS` | `100` | `hermes-review`, `review-github-gateway` | Items per JSON inventory or job page; use `--after-id` for the next page. |
| `REVIEW_AGENT_OPERATOR_EXPORT_MAX_ROWS` | `10000` | `hermes-review`, `review-github-gateway` | Rows per table in one private export. |
| **Admin console** (`compose.admin.yaml`) | | | |
| `REVIEW_AGENT_ADMIN_PUBLIC_URL` | required with the overlay | `review-admin` | Public console origin. |
| `REVIEW_AGENT_OIDC_ISSUER`, `REVIEW_AGENT_OIDC_CLIENT_ID`, `REVIEW_AGENT_OIDC_CLIENT_SECRET` | empty | `review-admin` | Organization sign-in; see [Organization sign-in and SCIM](./ADMIN_PANEL.md#organization-sign-in-and-scim). |
| `REVIEW_AGENT_OIDC_NAME` | `Organization sign-in` | `review-admin` | Sign-in button label. |
| `REVIEW_AGENT_SCIM_TOKEN` | empty | `review-admin` | SCIM provisioning token. |
| `REVIEW_AGENT_EMAIL_SECRET_KEY` | empty | `review-admin` | Fernet key that encrypts a saved SMTP password; back it up separately. |
| `REVIEW_AGENT_DOKPLOY_URL`, `REVIEW_AGENT_DOKPLOY_API_KEY`, `REVIEW_AGENT_DOKPLOY_COMPOSE_ID` | empty | `review-admin` | Optional container state in **Health**; see [Dokploy container state](./ADMIN_PANEL.md#dokploy-container-state). |
| **Code graph** (`compose.code-graph.yaml`) | | | |
| `REVIEW_AGENT_CODE_GRAPH_ENABLED` | `false` | `review-github-gateway`, `review-admin` | Enable the [code graph](./CODE_GRAPH.md) tool. |
| `REVIEW_AGENT_CODE_GRAPH_EMBEDDINGS` | `none` | `review-github-gateway`, `review-admin` | `none` or `openai`. |
| `REVIEW_AGENT_CODE_GRAPH_CACHE_BYTES` | `10737418240` | `review-code-graph` | Graph cache storage budget. |
| `REVIEW_AGENT_OPENAI_API_KEY` | empty | `review-github-gateway` | OpenAI key for embeddings. |
