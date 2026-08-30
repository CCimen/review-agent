---
sidebar_label: Operations
slug: /operations
title: Operations
status: current
last_verified: 2026-08-28
---

# Operations

> **Current**: Use this page for day-two operation and recovery. Use
> [Deploy Review Agent](./DEPLOYMENT.md) for credentials and first deployment.

This document owns runtime limits, persistent state, recovery, and operator
commands.

## GitHub credentials

The private GitHub gateway holds the App key and mints short-lived installation
tokens for one repository and one purpose at a time.

| Credential | Required permissions | Purpose |
| --- | --- | --- |
| GitHub App read token | Contents read, Issues read, Pull requests read, Metadata read | Exact PR source reads. |
| GitHub App publication token | Issues write, Pull requests write, Metadata read | Deterministic comments, reviews, suggestions, and feedback acknowledgements. |

The gateway mints each token for one enabled repository and one operation.
Endpoint-specific failures such as
`github_403_get_pull_request`, `github_403_list_issue_comments`,
`github_403_create_issue_comment`, or
`github_403_create_pull_request_review` identify the
missing permission or org approval path.

## Runtime Configuration

`.env.example` contains the deployable defaults. These settings control queue
load and worker behavior:

All values are optional and have deployable defaults:

- `REVIEW_AGENT_ACTIVE_JOB_LIMIT` — default `100`. Maximum queued, leased, and
  publication-waiting reviews; admission returns HTTP 429 at capacity.
- `REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS` — default `900`. Wait time that
  offsets one priority point.
- `REVIEW_AGENT_JOB_MAX_ATTEMPTS` — default `3`. Review-worker attempt budget.
- `REVIEW_AGENT_WORKER_CONCURRENCY` — default `4`. Maximum simultaneous reviews
  per worker process. The dispatcher claims only when a slot is free, so pending
  work cannot build an in-memory executor queue. Total cross-repository slots
  equal this value multiplied by the worker replica count.
- `REVIEW_AGENT_JOB_LEASE_SECONDS` / `REVIEW_AGENT_JOB_HEARTBEAT_SECONDS` —
  defaults `120` / `30`. Keep the heartbeat below half the lease.
- `REVIEW_AGENT_HERMES_TIMEOUT_SECONDS` — default `7200`. Maximum duration of
  one Hermes request.
- `REVIEW_AGENT_WORKER_TERMINATION_GRACE_SECONDS` — default `150` for Compose.
  OpenShift uses the matching `WORKER_TERMINATION_GRACE_SECONDS` template
  parameter. This is the maximum drain before the platform force-terminates a
  worker; it is deliberately independent of the longer review timeout.
- `REVIEW_AGENT_PUBLICATION_MAX_ATTEMPTS` — default `3`. Attempt budget frozen
  with each publication before the publisher claims it.
- `REVIEW_AGENT_PUBLICATION_LEASE_SECONDS` /
  `REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS` — defaults `120` / `30`.
- `REVIEW_AGENT_OPERATOR_PAGE_MAX_ITEMS`: default `100`. This bounds one JSON
  inventory or job page. Use `--after-id` to read the next installation or
  repository page.
- `REVIEW_AGENT_OPERATOR_EXPORT_MAX_ROWS`: default `10000`. This bounds rows
  from each table in one private operator export; raise it when a repository's
  history is larger, rather than treating it as a review-depth limit.
- `REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS` — default `8`. Concurrent
  signed admission requests per process.
- `REVIEW_AGENT_PUBLISH_MAX_BYTES` — default `60000`. Bytes per GitHub comment
  part, not a finding cap.
- `REVIEW_AGENT_POSTGRES_MAX_CONNECTIONS` — default `200` for the bundled
  Compose database. External PostgreSQL deployments own this setting.

Plan maximum database pool capacity with this formula:

```text
worker replicas × (worker concurrency + 1)
+ admission replicas × 4
+ Hermes replicas × 4
+ publisher replicas × 2
+ feedback replicas × 4
+ one connection per concurrent operator command
```

Pools open connections on demand. Keep the configured maximum below the
database limit with headroom for maintenance and monitoring.

### Worker termination and lease recovery

`SIGTERM` stops a worker from entering another claim cycle. A database claim
already in flight may still commit. At the task-entry activation boundary, the
worker checks the stop signal again. If stopping, it returns the exact live
lease to the queue without consuming a review attempt and does not start Hermes
for that job. If PostgreSQL is temporarily unavailable, Hermes still does not
start and the lease enters the existing expiry recovery path. After the
activation boundary, the review is active: the worker does not cancel or detach
the active Hermes request and continues heartbeating that exact lease. A request
that finishes within the platform grace completes normally. If it remains
blocked, Compose or OpenShift force-terminates the process after the configured
grace; the worker does not write a guessed terminal state.

After process termination, the last heartbeat remains authoritative until its
lease expires. Another worker checks for expired work at the recovery interval
and either requeues it with a new lease generation or dead-letters an exhausted
attempt. Recovery polling continues while every execution slot is occupied.
For one abandoned lease, when PostgreSQL is healthy, at least one worker
survives, and the bounded recovery batch has no backlog, the upper bound from
`SIGTERM` to requeue or dead-letter processing is:

```text
termination grace + lease duration + recovery interval
```

With the defaults, this is `150 + 120 + 30 = 300 seconds`. From the actual
force-termination point, the bound is 150 seconds. The old Hermes session may
still finish upstream, but the generation fence rejects late writes after the
job is reclaimed. Never shorten recovery by releasing a lease while that model
call can still run.

Use one database per environment. The example Compose network keeps PostgreSQL
private and uses this service-local URL shape:

```text
postgresql://review_agent:<url-safe-password>@review-postgres:5432/review_agent
```

Use the same URL-safe value for `REVIEW_AGENT_POSTGRES_PASSWORD` and the URL
password. A hex value from `openssl rand -hex 32` needs no percent-encoding.

## Capacity And Incomplete Coverage

The reviewer has no repository-size or model-era source-reading quota. It uses
bounded pages and reports incomplete coverage when an external or safety
boundary prevents inspection. Retained bounds have one of four owners:

| Boundary | Owner and behavior | Why it remains |
| --- | --- | --- |
| Changed-file enumeration | GitHub returns at most 3,000 files for one pull request. A lower per-response byte budget retries with smaller pages; if even a one-file page does not fit, registration remains explicitly incomplete. | [GitHub's pull-request files contract](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files) and bounded network memory. |
| Source files | Files are read in line pages. Files above 1 MB use the raw Contents response up to a 2 MB per-request memory guard; larger or binary files return a terminal unavailable state and keep coverage incomplete. Each page is fetched at the exact review revision and bounded independently. | [GitHub's repository-contents contract](https://docs.github.com/en/rest/repos/contents#get-repository-content), bounded gateway memory, and exact-subject validation. This is a per-file safety boundary, not a limit on PR size or total review depth. |
| Diff and source output | `plugins.entries.review-agent-tools.settings.result_max_chars` defaults to 160,000 characters and can be raised for larger-context models. The plugin derives a JSON-safe text page from that one budget and enforces it on diff and source page responses. A diff response returns `next_start_char`, `path_total_chars`, and `diff_source` for exact continuation. A source response also carries at most 400 lines; low-newline source that crosses the character boundary is reported truncated and is not recorded as a complete line read. | One native Hermes plugin setting owns and enforces the complete page-result budget without rejecting unrelated memory or delivery payloads. Diff pages can continue without a total code limit; an indivisible source line may remain incomplete rather than flood model context. |
| Historical context | One call accepts the same 200 paths returned by a changed-path page. The review procedure processes further pages in additional calls. | Bounded database/result work per call; not a repository limit. |
| Findings and suggestions | One atomic record transaction accepts at most 200 findings. Exceeding it rejects the record rather than dropping findings. At most 12 independent native suggestions are retained, while every accepted finding remains in the summary and coding-agent brief. | Bounded atomic persistence and GitHub head reads; suggestions are optional delivery metadata. |
| Publication | `REVIEW_AGENT_PUBLISH_MAX_BYTES` defaults to 60,000 bytes and is constrained to GitHub-safe part sizes. Larger reviews are deterministically split. | Provider delivery size, not a finding or review-depth limit. |
| Webhook and stored payloads | Request bodies, persisted JSON aggregates, text fields, database pools, timeouts, and retry counts remain bounded. | Denial-of-service protection, typed storage integrity, and predictable resource use. |

The managed configuration deliberately does not set `agent.max_turns` or
`context_file_max_chars`. Current Hermes runs turns to completion by default and
derives the context-file allowance from the selected model's context window.
The configured compression ratio and per-response plugin capacity remain
operator-owned resource controls for the pinned model. See the upstream
[Hermes configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
and [context-file behavior](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files).

SHA-256 identifiers, commit SHA grammar, local `F<number>` references, exact
snapshot matching, authorization, and publication markers are protocols or
security invariants, not capacity settings. Deployment profiles cannot change
them.

## Deployment Topology

The [deployment guide](./DEPLOYMENT.md#deploy) covers Compose, Dokploy, Coolify,
Portainer, and OpenShift. The public review route targets
`review-admission:8644`. Workers reach the private Hermes API on `8642`.
PostgreSQL and Hermes stay off the public network.

## Persistent State

The deployment uses two named volumes:

| Volume | Mounted in | Purpose |
| --- | --- | --- |
| `hermes_review_data` | Profile installer (write); Hermes and workers (read) | Hermes config, provider credentials, sessions, managed skills, plugins, and the installed reviewer receipt. Admission derives the same contract from immutable image files and cannot read this volume. |
| `review_postgres_data` | `review-postgres` at `/var/lib/postgresql/data` | PostgreSQL review state. |

Do not run two Hermes gateways against the same `hermes_review_data` volume.

Two one-shot services run before the live services. `review-profile-install`
installs the selected managed profile under `/opt/data`. `review-db-migrate`
waits for PostgreSQL and applies checksum-verified schema migrations. Both
should finish as `Exited (0)`; inspect their logs when startup stops.

Set `REVIEW_AGENT_PROFILE` to a trusted bundle key under
`bootstrap/profiles`; the packaged default is `sundsvall-standard`. The init
service rejects an unknown key before changing `HERMES_HOME`. A profile owns
`SOUL.md`, `workspace/AGENTS.md`, and the reviewed skills named in
`profile.json`; it cannot merge model, route, tool, authorization, snapshot,
persistence, marker, or lifecycle settings into the managed configuration.
Skill files are trusted, code-reviewed profile content; `profile.json` validation
does not make arbitrary skill prose safe.
Keep the selected value in deployment configuration so redeploys remain
explicit. When invoking the installer outside Compose, its receipt reuses the
last selected profile if neither the flag nor environment value is supplied.
The installer replaces managed config, SOUL, AGENTS, skills, and plugin files
from source control. It records their exact digests together with the pinned
Hermes image, model, reasoning effort, and tool-result budget. Admission stores
that compact contract with every review subject. A worker refuses queued work
before calling Hermes when its installed contract differs, and the normal
durable failure-status path tells the developer to request a fresh review.

Manual recovery only:

```bash
/opt/review-agent-bootstrap/install.sh
review-agent-admin database migrate
review-agent-admin database ready
```

Run those commands inside the `hermes-review` container, then restart the
service.

## Connect The Model Provider

Inside the `hermes-review` container:

```bash
hermes plugins list
hermes model
/opt/review-agent-bootstrap/install.sh
```

Use Hermes' wizard to authenticate the provider selected by
`REVIEW_AGENT_MODEL_PROVIDER`. The default `openai-codex` route uses ChatGPT
device-code OAuth and needs no model API key. Hermes also supports Anthropic,
API-key providers, and custom endpoints. Hermes documents that Anthropic OAuth
requires Claude Max plus extra-usage credits; its API-key route is independent.
The deployment variables `REVIEW_AGENT_MODEL_PROVIDER`, `REVIEW_AGENT_MODEL`, and
`REVIEW_AGENT_REASONING_EFFORT` remain the review contract; rerunning the
installer restores them after the wizard stores credentials. A selection
change applies after the installer and services restart. Already queued reviews
fail closed and should be requested again. Verify:

```bash
curl -fsS http://127.0.0.1:8642/health
hermes status
hermes doctor
hermes plugins list
```

## GitHub trigger

The App receives `issue_comment` events at `/webhooks/github-app`. Admission
persists the signed delivery before any provider read. The App worker then
checks the sender's current permission and the repository's enabled state before
creating a run. Feedback commands follow the same durable path. The gateway
rechecks the sender's current write or admin permission and the open pull
request before the feedback application records anything.

Set `REVIEW_AGENT_FEEDBACK_ENABLED=true` in Dokploy if the rendered review comment
should show the copyable feedback commands documented below.

Registration, reconciliation, explicit enablement, and verification live in the
[GitHub App setup guide](./GITHUB_APP_PILOT.md). Reconciliation never enables a
new or restored repository automatically.

## Run A Review

On any open pull request, including a draft, a collaborator with write or admin
permission comments:

```text
/review
```

After fixing findings, push the fix commit and comment `/review` again. Every
explicit request after the previous run reaches a terminal state creates a new
chronological review round such as `Review 2`, including a deliberate rerun of
the same base/head snapshot.

For an exact, independently safe local fix, the reviewer may publish a native
GitHub suggestion in **Files changed**. All suggestions for one review round are
grouped in one non-blocking `COMMENT` review instead of separate timeline
comments. Review and apply them individually, or add only the patches you want to
GitHub's suggestion batch and commit that selection together. Coordinated fixes
remain in the copyable coding-agent brief. After either path, run CI and post a
fresh top-level `/review`; applying a suggestion does not mark its finding
resolved. To keep the native review scannable, one round publishes at most 12
highest-priority, non-overlapping atomic patches.

Different PRs may run concurrently. A second `/review` on the same PR is treated
as a duplicate while a run is active.

## Developer Feedback

Post feedback as a new top-level PR comment. Do not edit an old command and do
not reply inside an inline diff thread.

```text
/review false-positive F2 because <what code, guard, or invariant disproves it>

/review intentional F2 ADR-0007 because <why the accepted ADR requires this design>

/review feedback scope F2 because <why this finding is in the diff but outside the intended PR scope>

/review feedback missed because <what concrete issue was missed and where>
```

`false-positive` is a durable finding decision. A later review suppresses the
same stable finding only while its code-context hash still matches. `intentional`
adds the exact accepted ADR ID, metadata hash, path, and base snapshot to that
rule; code or ADR changes require a fresh review. `feedback scope` records
author-intent or stacked-branch scope confusion without suppressing the finding.
`feedback missed` records review-quality feedback for metrics, replay cases, and
private reviewer-improvement analysis. Quality feedback never rewrites the live
reviewer's prompts, skills, or policy.

Successful feedback receives a `+1` reaction. Invalid, stale, not-current, or
unsupported commands receive a `confused` reaction and one deterministic
explanation. A stale intentional command tells the maintainer to run `/review`
after the ADR is accepted, then use the latest F reference. Accepted-risk
decisions remain governance actions.

## Runbook

Inspect active queue work:

```bash
review-agent-admin queues inspect
review-agent-admin jobs list --limit 100
```

Release a delayed queued retry or cancel its active run:

```bash
review-agent-admin jobs retry <id>
review-agent-admin jobs cancel <id>
```

`jobs retry` cannot revive failed or dead-letter work. Dead letters remain in
the queue report as terminal history, but they do not consume capacity or make
`doctor` and dry-run smoke tests fail. Inspect the failure, correct its cause,
and post a new `/review`. `cancel-job` fences a leased worker by failing the
owning run and reconciling its job in one transaction.

Inspect recent runs:

```bash
review-agent-memory runs --repo <org>/<repo> --limit 10
review-agent-memory runs --repo <org>/<repo> --stats
```

Inspect publication state:

```bash
review-agent-memory publications --repo <org>/<repo> --pr <number>
```

Inspect coverage for one run:

```bash
review-agent-memory coverage --run-id <id>
```

Mark stale runs failed after a crash:

```bash
review-agent-memory runs --mark-stalled --stale-after-minutes 10 --repo <org>/<repo> --pr <number>
```

The ordinary publisher also delivers deterministic terminal failure statuses.
It leases each status from PostgreSQL, recovers an existing marker after a
crash, retries transient GitHub failures, and stops after the stored attempt
limit. A later review for the same pull request suppresses obsolete status work.
Manual recovery reopens an exhausted status with a fresh stored attempt budget.

For bounded manual recovery, mark stale runs. The ordinary publisher then claims
and delivers their queued statuses:

```bash
review-agent-memory runs --mark-stalled --stale-after-minutes 10 --repo <org>/<repo> --pr <number>
```

`runs --failed` exposes the
status delivery state, attempt count, maximum attempts, and last delivery failure.

Common states:

| Symptom | Meaning |
| --- | --- |
| `running` with an old heartbeat | Review execution stopped before reaching a terminal state. |
| `publish_failed` | GitHub publication was attempted and failed; inspect `failure=`. |
| `body_too_large` | Review could not fit within the configured per-comment byte budget. |
| `review_contract_changed` | The queued provider/model/effort contract differs from the installed reviewer. Confirm the three model variables match across services, rerun the profile installer, restart the services, and request a fresh review. |
| `stale` | PR base or head changed before posting. |
| `stalled` or old `running` | Run heartbeat stopped; mark stale runs failed before retrying. |

The run ledger includes `phase`, `heartbeat`, and `failure`. A healthy run moves
through `accepted`, `fetching_pr`, `collecting_diff`, `reviewing`, `rendering`,
`publishing`, and `posted`.

## Memory And Decisions

Generate the weekly quality report:

```console
review-agent-memory quality --days 30
review-agent-memory quality --days 30 --repo <org>/<repo> --json
```

The Markdown default shows explicit signal counts, denominators, current triage
backlog, coverage, and persisted review-contract cohorts. It does not infer
accuracy from missing feedback. The activity window does not hide an older
current triage backlog.

Only an operator classifies a missed issue. Export the repository to identify
the feedback row, then append a triage state:

```console
review-agent-memory triage-feedback <feedback-id> \
  --status actionable \
  --stable-key review-rule.auth-boundary \
  --target-owner review_rule \
  --actor "github:<operator>" \
  --reason "Two independent reviews missed the same authorization boundary."
```

See [Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md) for the owner
vocabulary, private-export boundary, and state-to-coach rules.

List findings:

```bash
review-agent-memory list --repo <org>/<repo>
```

Show one finding:

```bash
review-agent-memory show <fingerprint-prefix> --repo <org>/<repo>
```

Prefer exact observation ids or PR-local references when recording decisions:

```bash
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

Other decision values are `accepted_risk`, `duplicate`, and `reopen`. Security
owns the suppression trust rules in [docs/SECURITY.md](SECURITY.md).

## Backup And Recovery

Back up PostgreSQL securely. It may contain unpublished findings and
human-entered reasons. Create a logical backup from the Compose host:

```bash
docker compose exec -T review-postgres \
  pg_dump --username=review_agent --dbname=review_agent --format=custom \
  > review-agent.dump
```

Test recovery in a fresh database before relying on a backup. Stop the
application services that read or write Review Agent state:

```bash
docker compose stop review-admission review-github-app-worker review-worker \
  review-publisher review-github-gateway hermes-review
```

Restore with `pg_restore --exit-on-error`, run
`review-agent-admin database migrate` and `review-agent-admin database ready`, then point the
environment at the restored database and redeploy. Recovery never converts or
imports another database backend.

## Private Reviewer-Improvement Exports

[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md) gives the shorter
weekly and monthly operating flow. Use the commands below for private exports,
diagnosis, and proposal details.

After argument parsing succeeds, `review-agent-memory` writes successful
receipts to standard output and one bounded JSON error to standard error on
failure. It never includes the original exception message. Argument parsing
errors retain standard argparse usage output and exit `2`.

| Error code | Retry? | Exit | Operator action |
| --- | --- | ---: | --- |
| `invalid_command_input` | no | 64 | Correct the requested limit or argument. |
| `command_rejected`, `database_operation_failed`, or a specific `export_*` / artifact code | no | 65 | Correct the requested scope or artifact; inspect application health for a database operation failure. |
| `internal_error` | no | 70 | Report the included exception type; no exception message is emitted. |
| `artifact_io_failed` | no | 74 | Repair the private file path or permissions. |
| `database_unavailable` or `database_busy` | yes | 75 | Retry after PostgreSQL connectivity or contention recovers. |
| `invalid_configuration` or `database_not_ready` | no | 78 | Correct configuration or complete the documented database migration/readiness steps. |

Export the registry:

```bash
review-agent-memory export \
  --repo <org>/<repo> \
  --row-limit <per-table-row-limit> \
  --output /opt/data/private-review/export.json
```

Generate a learning report:

```bash
review-agent-memory learning-report \
  --export /opt/data/private-review/export.json \
  --repo <org>/<repo> \
  --output /opt/data/private-review/learning-candidates.md
```

Generate a bounded coach bundle:

```bash
review-agent-memory coach-export \
  --export /opt/data/private-review/export.json \
  --repo <org>/<repo> \
  --after-decision-id 0 \
  --after-feedback-id 0 \
  --output /opt/data/private-review/coach-export.json
```

Generate a bounded private verification bundle for one completed review run:

```bash
review-agent-memory verification-export \
  --run-id <id> \
  --output /opt/data/private-review/verification/run-<id>.json
```

This is the private verifier slice. The export is a shadow artifact, not a live
review step. It does not publish comments. The bundle contains stable
run/publication ids, exact base/head SHAs, coverage summary, and bounded
`*_untrusted` evidence for the current published findings. A maintainer may hand
it to Claude or another private review tool and ask for falsification. Verifier
output can be stored in PostgreSQL for audit, but raw verifier verdicts are not
authoritative: only an explicit reconciliation decision for the same run can
drop a recorded candidate before publication.

The schema is provider-neutral (`provider`, `model`, `mode`, `status`) so future
profiles can choose one review provider, advisory verification, or gated
verification. This repository does not launch a second verifier from the
webhook reviewer; adding that runner is a separate reviewed runtime change.

Do not paste raw database exports into an LLM. Use `verification-export` for
review-finding falsification and `coach-export` for reviewer-improvement
signals.

Run the private coach directly from the live database so a raw export is not
left on disk:

```bash
review-agent-memory coach-run \
  --repo <org>/<repo> \
  --output-dir /opt/data/private-review/coach-run
```

The result is deliberately conservative. `no_change` means stop. A `propose`
result requires repeated independent episodes for the same stable finding and
includes the reviewer's original claim and checks beside the human
counter-evidence.

For a `propose` result, scrub `SUMMARY.md`, copy it to a separate operator
workstation or Hermes profile, and run:

```text
/learn ~/coach-review/SUMMARY.md; draft the smallest reviewer lesson and preserve the human-governed replay gate
/skills pending
/skills diff <id>
```

Do not run `/learn` in the live reviewer profile. The separate profile must not
share its `HERMES_HOME`, skills directory, or gateway. Keep
`skills.write_approval` on and treat the staged diff as a draft: add a focused
replay, port the validated lesson into the canonical repository owner, deploy
normally, then use `/skills reject <id>`. Never feed `/learn` raw comments, raw
database exports, or unsanitized session transcripts.

Validate replay fixtures:

```bash
review-agent-memory validate-replay review-learning/replay
```

The public webhook reviewer does not read `review-learning/`. Coach exports are
private LLM input artifacts. They can contain bounded maintainer-entered reasons
or repository text, so scrub them before committing or sharing.

## Updating And Validation

`HERMES_IMAGE` is pinned to the Hermes v2026.8.3 release tag and its immutable
multi-platform digest in `.env.example`, `compose.yaml`, and `Dockerfile`.
Update both the human-readable tag and digest through a reviewed dependency
bump. Never replace this with the moving `latest` or `main` tag.

Hermes v2026.8.3 owns the supported provider integrations. The Review Agent
renders the deployment-selected provider, model, and effort into managed config
so runtime behavior does not depend on a mutable interactive choice. A
controlled review after deployment is the final proof that the provider
credential is entitled to the selected model. Follow the current Hermes
provider guide when changing routes because authentication and billing differ
between providers. Use the official [Hermes provider
guide](https://hermes-agent.nousresearch.com/docs/integrations/providers) as
the current authority. Use its [model
catalog](https://hermes-agent.nousresearch.com/docs/reference/model-catalog)
for model IDs and its [environment
reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)
for API-key and custom-endpoint variables.

After a source update, redeploy. `review-profile-install` refreshes the managed
profile, and `review-db-migrate` verifies and applies PostgreSQL migrations
before the gateway starts.

Run local bundle checks:

```bash
./scripts/check_bundle.sh
```

The checks cover Python imports, strict type checks, unit tests, replay fixtures,
and YAML. They do not prove Dokploy routing, GitHub org token approval, ChatGPT
OAuth state, or repository rules.
