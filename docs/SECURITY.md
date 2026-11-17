---
sidebar_label: Security model
slug: /security
title: Security model
description: Trust boundaries, tool surface, prompt-injection posture, token scopes, and data handling.
status: current
last_verified: 2026-11-17
---

# Security model

Review Agent is an advisory code reviewer with narrow tools. The model is not
trusted: deterministic code owns authorization, persistence and every GitHub
write.

## Trust boundaries

- The admission service verifies the GitHub App webhook signature and stores
  only a bounded normalized delivery.
- The private gateway holds the App key and installation tokens. They never
  enter Hermes, PostgreSQL, admission, worker payloads, or logs.
- Before admitting a manual review, the gateway verifies the signed sender's
  stable GitHub ID against the repository's additional-user list, or checks
  current GitHub write or admin permission. Feedback always requires GitHub
  write or admin permission. Both paths verify the exact open, same-repository
  pull-request snapshot. Console grants do not change GitHub permissions.
- A worker leases the job and calls the private, authenticated Hermes API.
- Hermes runs the review through the bundled plugin, not through a shell.
- The model can read bounded pull-request context and record candidate findings.
- Deterministic plugin code owns memory writes, publication, feedback parsing,
  and GitHub mutations.

## Tool surface

The live reviewer does not receive:

- a shell;
- repository write access;
- a general GitHub mutation tool;
- a browser;
- delegation;
- arbitrary code execution;
- access to private coach artifacts under `review-learning/`.

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
path. Admission stores only a normalized `/review ...` command. Before the
feedback application writes PostgreSQL, the gateway checks the exact open pull
request and the sender's current write or admin permission. The gateway can then
post only a code-owned reaction or explanation.

Human feedback and coach exports can inform later reviewer changes, but they
never rewrite prompts, skills, suppressions or policy by themselves. Run Hermes
`/learn` only in a separate operator profile or workstation that does not share
the live reviewer's `HERMES_HOME`, skills directory or gateway.

## GitHub credential boundaries

The GitHub App key exists only in the private gateway. The gateway mints
short-lived installation tokens restricted to one repository and one purpose.
Callers supply a durable lease identity, never a repository, URL, HTTP method or
credential. The gateway derives authority from PostgreSQL and checks the lease
before and after provider I/O.

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

Only a GitHub collaborator with current write or admin permission, or an
operator command, can suppress a finding. The model can record observations, but
it cannot mark itself correct or dismiss its own findings.

A suppression applies only while the finding's code context is unchanged, and
can carry an expiry date. When the code changes, the finding is evaluated again.
ADRs are context, not immunity: an accepted ADR can explain an architectural
decision, but the reviewer still checks the invariants the ADR requires.

## Data handling

PostgreSQL stores findings, review runs, publication and suggestion metadata,
human decisions, and review-quality feedback. It can contain unpublished
findings and maintainer-entered reasons. Back it up securely and scrub exports
before sharing.

Coach and verification exports are private files that operators create with
[offline tools](./OPERATIONS.md#offline-operator-tools) outside the review path.
They contain bounded untrusted text, stable IDs, provenance, hashes and event
metadata; the live reviewer does not read them. An operator who passes one to an
external model sends that bounded evidence to it; never send raw database exports
or webhook payloads instead.

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
profile files and private learning artifacts are excluded.

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
