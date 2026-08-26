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

- GitHub Actions accepts only comments that start with `/review` or `@review`.
- The workflow requires trusted GitHub association: `OWNER`, `MEMBER`, or
  `COLLABORATOR`.
- `AI_REVIEW_ALLOWED_USERS` must include the requester. Empty means deny all.
- GitHub Actions sends a minimal HMAC-signed request to the admission service.
- The optional GitHub App route verifies its own HMAC secret and stores only a
  bounded normalized delivery. Its private key is mounted read-only into the
  isolated App worker, not Hermes, PostgreSQL, admission, or logs.
- Admission verifies the signature, allowlist, request identity, and current
  GitHub PR snapshot before it commits the run and queue job together.
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
stored parts with its dedicated token, records each GitHub ID, and completes the
run. Suggestions are grouped in one non-blocking GitHub `COMMENT` review; the
model never receives a GitHub mutation tool or write token.

## Prompt-injection handling

PR code, comments, commit messages, docs, and feedback are untrusted data. The
review profile tells the model to treat prompt-injection-looking text as evidence
only. Repository content cannot change reviewer policy, prompts, skills,
suppressions, memory decisions, or feedback commands.

The admission service and feedback bridge are outside the model path. The
feedback bridge refetches the authoritative GitHub comment, parses only
supported `/review ...` commands, authorizes the
numeric GitHub actor id, writes PostgreSQL through the feedback application,
and posts only a reaction or a short deterministic explanation.

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

## GitHub token boundaries

Use separate repository-scoped tokens. [Operations](OPERATIONS.md) owns the
exact permission matrix.

None of these tokens need repository administration, workflow write, package
write, secrets access, branch deletion, or contents write.

The publisher token needs Pull requests read/write for both the PR summary and
native review suggestions. It does not need Issues write or Contents write and
cannot commit those patches through the review flow. A developer chooses whether
to apply an individual suggestion or a selected batch in GitHub, and that human
action creates the commit.

If GitHub returns `Resource not accessible by personal access token`, inspect the
endpoint-specific failure in `review-agent-memory publications --json`. Most
runtime 403s are missing org approval or missing Issues/Pull requests permission
on a fine-grained token.

The [GitHub App admission pilot](./GITHUB_APP_PILOT.md) requests read-only
Metadata, Contents, Issues, and Pull requests access for one selected repository.
It validates current installation and requester access before admission. The
pilot does not give an installation token to Hermes or the publisher and does
not remove the three current scoped service tokens.

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

Only allowlisted human feedback or an operator command can suppress a finding.
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
