---
sidebar_label: Security model
slug: /security
title: Security model
description: Trust boundaries, tool surface, prompt-injection posture, token scopes, and data handling.
status: current
last_verified: 2026-08-26
---

# Security model

> **Current** — This trust model describes the live general reviewer. Planned
> analyzers and deferred security integrations do not change these boundaries.

This reviewer is an advisory code-review agent with narrow tools. It is useful
because it combines LLM reasoning with deterministic boundaries, not because the
model is trusted.

## Trust boundaries

- The admission service verifies the GitHub App webhook signature and stores
  only a bounded normalized delivery.
- The private gateway holds the App key and installation tokens. They never
  enter Hermes, PostgreSQL, admission, worker payloads, or logs.
- Before admitting a review or feedback command, the gateway verifies the
  sender's current write or admin permission and the exact open,
  same-repository pull-request snapshot.
- A worker leases the job and calls the private, authenticated Hermes API.
- Hermes runs the review through the bundled plugin, not through a shell.
- The model can read bounded PR context and record candidate findings.
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

Review output reaches GitHub only through a two-stage deterministic path.
`review_agent_deliver` verifies the PR base/head snapshot, freezes the exact
comment parts and validated atomic suggestions in PostgreSQL, and queues that
immutable publication intent. A separate recoverable publisher writes only those
stored parts through the lease-bound App gateway, records each GitHub ID, and completes the
run. Suggestions are grouped in one non-blocking GitHub `COMMENT` review; the
model never receives a GitHub mutation tool or write token.

## Prompt-injection handling

PR code, comments, commit messages, docs, and feedback are untrusted data. The
review profile tells the model to treat prompt-injection-looking text as evidence
only. Repository content cannot change reviewer policy, prompts, skills,
suppressions, memory decisions, or feedback commands.

The admission service, App worker, and private gateway are outside the model
path. Admission stores only a normalized `/review ...` command. Before the
feedback application writes PostgreSQL, the gateway checks the exact open pull
request and the sender's current GitHub write or admin permission. The gateway
can then post only a code-owned reaction or explanation.

Human feedback and coach exports may inform future reviewer changes, but they do
not automatically rewrite prompts, skills, suppressions, or policy. In short:
review evidence can propose changes, but it cannot change policy by itself.
Run Hermes `/learn` only in a separate operator profile or workstation that does
not share the live reviewer's `HERMES_HOME`, skills directory, or gateway.

## Private Claude Verification

Claude verification is an operator-run shadow workflow, not part of the live
webhook reviewer. The public review path does not launch Claude, spawn
subprocesses, delegate to subagents, execute repository code, or hand another
model a GitHub write token.

`review-agent-memory verification-export` reads an already completed review run
and writes a bounded private JSON artifact with mode `0600`. The artifact is for
falsifying current published findings out of band. It contains stable ids,
base/head SHAs, coverage summary, and bounded `*_untrusted` finding evidence. It
does not contain raw database rows, rendered Markdown, feedback actor identities,
or source comment URLs.
If an operator gives this artifact to an external model, this bounded finding
evidence is the intended review-data egress; do not paste raw database exports
or webhook payloads instead.

Claude output is advisory. It must not suppress findings, rewrite prompts,
change feedback commands, publish comments, or gate pull requests without a
separate human-reviewed implementation and replay evidence.

## GitHub credential boundaries

The GitHub App key exists only in the private gateway. It mints short-lived
installation tokens reduced to one repository and either read or publication
permissions. Callers supply a durable lease identity, never a repository, URL,
HTTP method, or credential. The gateway derives authority from PostgreSQL and
checks the lease before and after provider I/O.

The App cannot merge, change workflows, administer repositories, read secrets,
delete branches, or write repository contents. Native suggestions remain
advisory; a developer chooses whether GitHub should apply one.

## Dependency vulnerability scanning

The reviewer does not currently perform full dependency vulnerability scanning.

It reviews code and security risks introduced or worsened by the PR. If a PR
changes dependency manifests or lockfiles, it may reason about obvious risks such
as unpinned packages, suspicious dependency additions, removed lockfile
discipline, or a dangerous version change. That is still LLM review, not a CVE
database lookup.

Keep deterministic dependency controls in CI:

- GitHub Dependency Review;
- Dependabot alerts;
- CodeQL or SARIF code scanning;
- OSV, Snyk, Trivy, `npm audit`, `pip-audit`, or equivalent scanners.

The best integration is to let deterministic scanners produce their own results
and, later, optionally let the reviewer summarize or prioritize those results.
Do not make the model the source of truth for CVE/GHSA status.

## Human-governed suppressions

Only a GitHub collaborator with current write or admin permission, or an
operator command, can suppress a finding.
The model can record observations, but it cannot mark itself correct or dismiss
its own findings.

Suppressions bind to the exact reviewed file version and expire. If the file
changes, the finding is re-evaluated. ADRs are context, not immunity: an accepted
ADR can explain an architectural decision, but the reviewer should still check
the invariants the ADR requires.

## Data handling

The PostgreSQL database stores findings, review runs, publication and suggestion
metadata, human decisions, and review-quality feedback. It can contain sensitive
unpublished findings and maintainer-entered reasons. Back it up securely and
scrub exports before sharing.

Coach and verification exports are private analysis artifacts. They contain
bounded untrusted text, stable ids, exact observation provenance, hashes, and
event metadata for human-reviewed workflows. Coach evidence includes the
reviewer's original claim and disproof checks alongside the human reason so the
mistake can be evaluated in context. The public webhook reviewer does not read
those exports.

## Public documentation boundary

The GitHub Pages site is static public documentation. It receives no reviewer
credentials, webhook payloads, database data, unpublished findings, private source
excerpts, feedback reasons, model sessions, or production access details. Its
Docusaurus configuration publishes an explicit Markdown allowlist; goal boards,
runtime profile files, and private learning artifacts are excluded.

## Non-goals

This deployment is not a replacement for:

- deterministic CI;
- tests;
- type checks;
- migration checks;
- deterministic security scanners listed above;
- human ownership.

Do not make the reviewer a required merge check until the team has measured its
false-positive rate, acceptance rate, missed-issue feedback, and operational
failure modes.
