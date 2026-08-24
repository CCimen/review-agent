---
sidebar_label: Operations
slug: /operations
title: Operations
status: current
last_verified: 2026-08-24
---

# Operations

> **Current**: Use this page for day-two operation and recovery. Use
> [Deploy Review Agent](./DEPLOYMENT.md) for credentials and first deployment.

This document owns runtime limits, persistent state, recovery, and operator
commands.

## GitHub Tokens

Use separate fine-grained tokens for read, publication, and feedback. The
[deployment guide](./DEPLOYMENT.md#create-the-credentials) shows the GitHub UI
steps and organization approval path.

| Token env var | Required permissions | Purpose |
| --- | --- | --- |
| `GITHUB_READ_TOKEN` | Contents read, Pull requests read, Metadata read | PR metadata, diff, and file reads. |
| `REVIEW_AGENT_PUBLISH_GH_TOKEN` | Metadata read, Pull requests read/write | Create, update, and delete PR summary comments and publish native suggested changes. |
| `REVIEW_AGENT_FEEDBACK_GH_TOKEN` | Issues read/write, Metadata read, Pull requests read | Add feedback reactions and read PR/comment state. |

The publisher tries `GITHUB_READ_TOKEN` for read paths first and uses
`REVIEW_AGENT_PUBLISH_GH_TOKEN` for comment and review writes. The publisher token
does not need Contents write or Issues write: GitHub accepts Pull requests write
for comments on pull requests, and only the developer's GitHub action creates a
commit from a proposed patch. Endpoint-specific failures such as
`github_403_get_pull_request`, `github_403_list_issue_comments`,
`github_403_create_issue_comment`, or
`github_403_create_pull_request_review` identify the
missing permission or org approval path.

## Runtime Configuration

`.env.example` contains the deployable defaults. These settings control queue
load and worker behavior:

| Name | Required | Default | Notes |
| --- | --- | --- | --- |
| `REVIEW_AGENT_ACTIVE_JOB_LIMIT` | no | `100` | Maximum queued plus leased jobs. Admission returns HTTP 429 at capacity. |
| `REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS` | no | `900` | Wait time that offsets one priority point. |
| `REVIEW_AGENT_JOB_MAX_ATTEMPTS` | no | `3` | Attempt budget before dead letter. |
| `REVIEW_AGENT_JOB_LEASE_SECONDS` | no | `120` | Lease duration for one worker generation. |
| `REVIEW_AGENT_JOB_HEARTBEAT_SECONDS` | no | `30` | Heartbeat period; it must stay below half the lease. |
| `REVIEW_AGENT_HERMES_TIMEOUT_SECONDS` | no | `7200` | Maximum duration of one Hermes API request. |
| `REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS` | no | `8` | Concurrent signed admission requests per process. |
| `REVIEW_AGENT_PUBLISH_MAX_BYTES` | no | `60000` | Bytes per GitHub comment part, not a finding cap. |

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
| Source files | Files are read in line pages. Files above 1 MB use the raw Contents response up to GitHub's 100 MB endpoint boundary; larger or binary files return a terminal unavailable state and keep coverage incomplete. The last immutable revision-keyed file is cached, bounding retained source bytes at one provider-sized file while avoiding a full network refetch for each sequential page. | [GitHub's repository-contents contract](https://docs.github.com/en/rest/repos/contents#get-repository-content), bounded process memory, and bounded repeated network work. |
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
| `hermes_review_data` | `hermes-review` at `/opt/data` | Hermes config, Codex OAuth state, sessions, managed skills, and plugins. |
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

Manual recovery only:

```bash
/opt/review-agent-bootstrap/install.sh --force-agents
review-agent-database migrate
review-agent-database ready
```

Run those commands inside the `hermes-review` container, then restart the
service.

## Connect Codex

Inside the `hermes-review` container:

```bash
hermes plugins list
hermes auth add openai-codex
/opt/review-agent-bootstrap/install.sh
```

Complete the ChatGPT device-code login with the intended subscription account.
The managed profile, rather than the interactive model picker, owns
`openai-codex`, `gpt-5.6-sol`, and `xhigh`. Restart the service and verify:

```bash
curl -fsS http://127.0.0.1:8642/health
hermes status
hermes doctor
hermes plugins list
```

Inside the `hermes-review-feedback` container:

```bash
curl -fsS http://127.0.0.1:8645/ready
review-agent-feedback-bridge verify-config
```

## GitHub Trigger

The [deployment guide](./DEPLOYMENT.md#configure-github-actions) owns workflow
installation, secret creation, and the username allowlist. The secret mapping is:

```text
HERMES_REVIEW_URL=https://review.example.org/webhooks/review-agent
HERMES_WEBHOOK_SECRET=<same value as REVIEW_AGENT_WEBHOOK_SECRET>
HERMES_REVIEW_FEEDBACK_URL=https://review-feedback.example.org/webhooks/review-agent-feedback
HERMES_REVIEW_FEEDBACK_SECRET=<same value as REVIEW_AGENT_FEEDBACK_WEBHOOK_SECRET>
```

The workflow grants `issues: write` and `pull-requests: write` to its built-in
token for the non-blocking eyes reaction on an accepted PR comment. GitHub's
Actions integration returned `403 Resource not accessible by integration` for
this PR-comment reaction when only `issues: write` was granted, even though the
REST path is under issue comments. Keep both permissions unless a production
workflow run proves the pull-request permission is no longer required.
Webhook secrets are scoped to the dispatch step and are not inherited by the
reaction step. The workflow does not check out PR code. It sends only repository
name, PR number, requester, and request id to admission. The workflow must exist on
the repository's default branch before an `issue_comment` event can start it.

Set `REVIEW_AGENT_FEEDBACK_ENABLED=true` in Dokploy if the rendered review comment
should show the copyable feedback commands documented below.

## Run A Review

On any open pull request, including a draft, an allowlisted maintainer comments:

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

/review feedback scope F2 because <why this finding is in the diff but outside the intended PR scope>

/review feedback missed because <what concrete issue was missed and where>
```

`false-positive` is a durable finding decision. `feedback scope` records
author-intent or stacked-branch scope confusion without suppressing the finding.
`feedback missed` records review-quality feedback for metrics, replay cases, and
private reviewer-improvement analysis.

Successful feedback receives a `+1` reaction. Invalid, stale, not-current, or
unsupported commands receive a `confused` reaction and one deterministic
explanation. Intentional-design and accepted-risk decisions remain CLI or
governance actions until there is deterministic ADR validation for PR comments.

## Runbook

Inspect active queue work:

```bash
review-agent-database jobs --limit 100
```

Release a delayed queued retry or cancel its active run:

```bash
review-agent-database retry-job --job-id <id>
review-agent-database cancel-job --job-id <id>
```

`retry-job` cannot revive failed or dead-letter work. Post a new `/review` after
you correct a terminal failure. `cancel-job` fences a leased worker by failing
the owning run and reconciling its job in one transaction.

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

Mark stale runs and publish their deterministic failure-status comments:

```bash
review-agent-memory runs --publish-failure-status --stale-after-minutes 10 --repo <org>/<repo> --pr <number>
```

This command exits with status 1 if any GitHub status comment fails to publish.
Its JSON output identifies each failed run so the operator can inspect the cause
and retry the same bounded command.

Common states:

| Symptom | Meaning |
| --- | --- |
| `running` with an old heartbeat | Review execution stopped before reaching a terminal state. |
| `publish_failed` | GitHub publication was attempted and failed; inspect `failure=`. |
| `body_too_large` | Review could not fit within the configured per-comment byte budget. |
| `stale` | PR base or head changed before posting. |
| `stalled` or old `running` | Run heartbeat stopped; mark stale runs failed before retrying. |

The run ledger includes `phase`, `heartbeat`, and `failure`. A healthy run moves
through `accepted`, `fetching_pr`, `collecting_diff`, `reviewing`, `rendering`,
`publishing`, and `posted`.

## Memory And Decisions

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

Test recovery in a fresh database before relying on a backup. Stop the reviewer
and feedback services, restore with `pg_restore --exit-on-error`, run
`review-agent-database migrate` and `review-agent-database ready`, then point the
environment at the restored database and redeploy. Recovery never converts or
imports another database backend.

## Private Reviewer-Improvement Exports

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
authoritative: only an explicit Codex reconciliation decision for the same run
can drop a recorded candidate before publication.

The schema is provider-neutral (`provider`, `model`, `mode`, `status`) so future
profiles can choose Codex-only, advisory verification, or gated verification.
This repository does not launch Claude from the webhook reviewer in the current
slice; adding that runner is a separate reviewed runtime change.

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

Hermes v2026.8.3 supports the managed GPT-5.6 model configuration used here.
The managed profile still configures `gpt-5.6-sol` directly so deployment does
not depend on an interactive picker. A controlled review after deployment is
the final proof that the subscription is entitled to the model and the OAuth
route accepts it. The Hermes image does not bundle the standalone Codex CLI;
this service uses Hermes' `openai-codex` provider directly.

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
