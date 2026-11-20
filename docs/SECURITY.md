---
sidebar_label: Security model
slug: /security
title: Security model
description: Trust boundaries, tool surface, prompt-injection posture, token scopes, and data handling.
status: current
last_verified: 2026-11-18
---

# Security model

Review Agent is an advisory code reviewer with narrow tools. The model is not
trusted: deterministic code owns authorization, persistence and every GitHub
write.

## Trust boundaries

- The admission service verifies the GitHub App webhook signature and stores
  only a bounded normalized delivery.
- The private gateway holds the App key and the installation tokens for
  review and publication. The optional admin panel also mounts the key and
  uses metadata-only installation tokens to verify repositories
  ([credential boundaries](#github-credential-boundaries)). The key and tokens
  never enter Hermes, PostgreSQL, admission, worker payloads, or logs. What is
  stored is listed under [Data handling](#data-handling).
- Before admitting a manual review, the gateway verifies the signed sender's
  stable GitHub ID against the repository's additional-user list, or checks
  current GitHub write or admin permission. Feedback always requires GitHub
  write or admin permission. Both paths verify the exact open, same-repository
  pull-request snapshot. Console grants do not change GitHub permissions.
- A worker leases the job and calls the private, authenticated Hermes API.
- Hermes runs the review through the bundled plugin, not through a shell.
- The model can read bounded pull-request context and record candidate findings.
- Deterministic plugin code validates each tool call. What it persists goes to
  PostgreSQL: recorded findings, coverage and the frozen publication.
- A separate publisher sends the frozen publication to GitHub through the
  gateway, which performs the write.
- Feedback commands are parsed at admission and recorded by the App worker,
  outside the model path.

## Tool surface

The live reviewer does not receive:

- a shell;
- repository write access;
- a general GitHub mutation tool;
- a browser;
- delegation;
- arbitrary code execution.

The exact tool list is in
[How reviews work](./HOW_REVIEWS_WORK.md#3-what-the-reviewer-can-do).

Review output reaches GitHub only through a two-stage deterministic path.
`review_agent_deliver` verifies the pull-request base and head, freezes the exact
comment parts and validated suggestions in PostgreSQL, and queues that immutable
publication. A separate publisher writes only those stored parts through the
lease-bound App gateway, records each GitHub ID, and completes the run.
Suggestions are grouped in one non-blocking GitHub `COMMENT` review. The model
never receives a GitHub mutation tool or a write token.

## Prompt-injection handling

Pull-request code, comments, commit messages, docs and feedback are untrusted
data. The review profile tells the model to treat prompt-injection-looking text
as evidence only. Repository content cannot change reviewer policy, prompts,
skills, suppressions, memory decisions or feedback commands.

The admission service, App worker and private gateway are outside the model
path. Admission does not store the webhook body. For each signed delivery it
handles, it stores a bounded record:

- **Comment command:** repository, installation, the sender's GitHub ID and
  login, pull request, and parsed command; feedback also keeps the sender's
  reason.
- **Installation event:** account, permissions, and, on install, the
  repository list.
- **Documentation event:** the pull request or check run, and its sender.

When a delivery reaches a terminal state, its normalized payload is cleared,
except that a documentation delivery keeps its bounded envelope, including the
sender. A receipt with the repository, installation, event, and body hash stays
in the same row until an operator prunes it; see
[Retention ownership](./OPERATIONS.md#retention-ownership).

Before feedback is written, the gateway checks the exact open pull request and
the sender's current write or admin permission. It can then post only a
code-owned reaction or explanation.

Human feedback can inform later reviewer changes, but it never rewrites
prompts, skills or policy by itself. An authorized `false-positive` or
`intentional` command does record a suppression of that one finding; see
[Human-governed suppressions](#human-governed-suppressions).
Run Hermes `/learn` only in
a separate operator profile or workstation that does not share the live
reviewer's `HERMES_HOME`, skills directory or gateway.

## GitHub credential boundaries

The GitHub App key is mounted into the private gateway and, when the optional
[admin panel](./ADMIN_PANEL.md) is deployed, into the admin service, which uses
it, with metadata-only installation tokens, to read App and installation status
and to verify a repository before enabling it. No other service receives it.

The gateway mints short-lived installation tokens restricted to one repository
and one purpose. For lease-bound review, feedback and publication operations,
callers supply a durable lease identity, never a repository, URL, HTTP method
or credential. The gateway derives authority from PostgreSQL and checks the
lease before and after provider I/O. The operator smoke test is different: its
caller names a repository and pull request, must present `API_SERVER_KEY`, and
is accepted only for a repository that is already enabled.

| Token purpose | Permissions | Used for |
| --- | --- | --- |
| `review_read` | Contents read, Issues read, Pull requests read, Metadata read | Reading the exact pull-request source. |
| `publication` | Issues write, Pull requests write, Metadata read | Review comments, suggestions and feedback acknowledgements. |
| `documentation_publication` | Checks write, Issues write, Pull requests write, Metadata read | Documentation checks and their overflow report comments. |

The App cannot merge, change workflows, administer repositories, read secrets,
delete branches, or write repository contents. Native suggestions stay advisory;
a developer decides whether GitHub applies one.

## Multiple organizations on one deployment

One deployment can serve App installations from many GitHub accounts. Each new
installation stays locked until an operator approves it; see
[Share the App with other organizations](./GITHUB_APP_PILOT.md#share-the-app-with-other-organizations).

- All installations share one PostgreSQL database and one job queue.
- Console teams scope what people can see and change. They are access control
  within one trusted operator's deployment, not isolation between hostile
  tenants: the deployment operator and anyone with database access can read
  every organization's data.
- Code and diffs are sent to the deployment's model provider. A team can use its
  own model connection; otherwise every installation shares the deployment's
  model route.

Run a separate deployment when organizations must not share an operator or a
database.

## Human-governed suppressions

Only a human decision suppresses a finding. There are three paths, and each
checks a different authority:

- **GitHub feedback command.** The gateway checks the sender's current GitHub
  write or admin permission and the exact open pull request. The App worker
  then records the decision.
- **Console.** The console requires a signed-in platform owner or admin, or a
  maintainer of the team that owns the repository, and writes the decision to
  the audit log. Console roles are not GitHub permissions.
- **Operator CLI** (`review-agent-memory decide`). It runs with the
  deployment's database credentials, checks no GitHub or console permission,
  and records `--actor` as typed. Whoever can run commands in the container can
  record a decision.

The model can record observations, but it cannot mark itself correct or dismiss
its own findings.

A suppression applies only while the finding's code context is unchanged and
the decision has not expired. Every suppressing decision expires, after 180
days unless another period is given; see
[What each command changes](./FEEDBACK_AND_DECISIONS.md#what-each-command-changes).
When the code changes or the decision expires, the finding is evaluated again.
ADRs are context, not immunity: an accepted ADR can explain an architectural
decision, but the reviewer still checks the invariants the ADR requires.

## Data handling

PostgreSQL stores findings, review runs, publication and suggestion metadata,
human decisions, and review-quality feedback. It can contain unpublished
findings and maintainer-entered reasons. Back it up securely and scrub exports
before sharing.

A repository export is a private file that an operator creates with the
[offline tool](./OPERATIONS.md#offline-operator-tools) outside the review path.
The live reviewer does not read it. Never send a raw database export or a
webhook payload to an external model.

## Dependency vulnerability scanning

The reviewer is not a dependency or CVE scanner. The model never queries a
vulnerability database or decides whether a CVE passes CI. When a pull request
changes manifests or lockfiles, the model may point out obvious risks such as
unpinned or suspicious packages, but that is model review, not a database
lookup. Keep deterministic tools such as GitHub Dependency Review, Dependabot,
CodeQL, OSV, Snyk, `npm audit` or `pip-audit` as the source of truth.

The project's own images are scanned with a pinned Trivy policy before release,
and each release attaches SBOMs and vulnerability reports.

## Release inventories

The vulnerability policy, dependency pins, and the inventories and evidence
attached to each release are in
[RELEASING.md](https://github.com/CCimen/review-agent/blob/main/RELEASING.md#release-evidence).

## Public documentation boundary

The GitHub Pages site is static public documentation. It receives no reviewer
credentials, webhook payloads, database data, unpublished findings, private
source excerpts, feedback reasons, model sessions, or production access details.
Its Docusaurus configuration publishes an explicit Markdown allowlist; runtime
profile files are excluded.

## Non-goals

This deployment is not a replacement for:

- deterministic CI;
- tests;
- type checks;
- migration checks;
- deterministic security scanners;
- human ownership.

Do not make the reviewer a required merge check until the team has measured its
false-positive rate, acceptance rate, missed-issue feedback, and operational
failure modes.
