---
sidebar_label: Repository context
slug: /repository-context
title: Configure reviews in a repository
description: Add optional team instructions, ordered platform context, and typed design decisions without changing the shared safety contract.
status: current
last_verified: 2026-09-02
---

# Configure reviews in a repository

> **TL;DR:** Copy `examples/repository-context/.review-agent/` into a repository,
> edit the English Markdown files, list context files in `config.toml`, and run
> the offline validator from a Review Agent checkout or release image.
> Repositories without this directory continue to use the neutral deployment
> baseline.

Repository owners can add reviewed project knowledge without asking the Review
Agent operator to create another deployment profile. The package is ordinary
version-controlled repository content.

## Understand the ownership boundary

The deployment owns controls that must stay consistent across every repository.
Each repository owns only its optional review focus and accepted project facts:

```text
Deployment
├── bootstrap/profiles/default-standard/
│   ├── SOUL.md                 Neutral reviewer identity and baseline tone
│   ├── workspace/AGENTS.md     Non-overridable review and evidence contract
│   └── skills/review-agent-pr/ Review procedure
└── Environment
    ├── Provider and model
    ├── Reasoning effort
    ├── Secrets
    └── Capacity settings

Repository
└── .review-agent/
    ├── config.toml             Enables the package and orders context files
    ├── instructions.md         Engineering principles, focus, and response style
    ├── context/
    │   ├── backend.md
    │   ├── platform.md
    │   ├── frontend/framework.md
    │   └── ui/design-system.md
    ├── decisions.toml          Explicit ADR index
    └── decisions/              Typed, accepted repository ADRs
```

The environment can be Dokploy, OpenShift, Docker Compose, or another supported
container platform. None of the product defaults depend on one organization or
one deployment provider.

The effective review combines the neutral deployment contract, repository
instructions, context files in their declared order, indexed decisions, and the
exact pull request snapshot. Repository content is additive: it cannot replace
the deployment's authorization, evidence, severity, tooling, or publication
rules.

## See the repository package

```text
.review-agent/
├── config.toml
├── instructions.md
├── context/
│   ├── platform.md
│   ├── backend.md
│   └── frontend/
│       └── framework.md
├── decisions.toml
└── decisions/
    └── ADR-0001-example.md
```

The deployment still owns the neutral reviewer identity, security and evidence
contract, tools, model route, lifecycle, and GitHub publication. Repository
content can focus the review and its wording; it cannot weaken those controls.

## Start with the copyable package

From a Review Agent checkout with its documented virtual environment, copy the
starter and validate the target repository:

```bash
cp -R examples/repository-context/.review-agent /path/to/repository/
.venv/bin/python tools/review_agent_admin.py \
  repository-context validate /path/to/repository
```

A repository owner who does not keep a Review Agent checkout can run the same
offline validator from the release image while standing in the target
repository:

```bash
docker run --rm --read-only \
  --entrypoint review-agent-admin \
  --mount type=bind,source="$PWD",target=/repo,readonly \
  ghcr.io/ccimen/review-agent:v0.2.0-rc.1 \
  repository-context validate /repo
```

The validator is offline: it does not need GitHub, PostgreSQL, Hermes, a model,
or secrets. Its JSON receipt contains paths, hashes, counts, and validation
status, never Markdown bodies.

## Select files explicitly

`config.toml` is the only context index:

```toml title=".review-agent/config.toml"
version = 1
enabled = true

context = [
  "context/platform.md",
  "context/backend.md",
  "context/frontend/framework.md",
]
```

Review Agent reads the optional fixed `instructions.md` first, then only the
context files listed in this array, in this exact order. Paths are relative to
`.review-agent/`, must stay under `context/`, and must end in `.md`. It does not
scan directories, infer frameworks from repository names, fetch remote
packages, or load unlisted Markdown.

Set `enabled = false` to keep the package in the repository without applying
its instructions or context. Removing `config.toml` opts out and uses only the
deployment baseline. A missing indexed file or invalid package disables the
whole optional guidance snapshot for that review; the normal code review still
runs.

## Write one instruction layer

Use `instructions.md` for repository-wide engineering principles, review focus,
and communication style. A separate `personality.md` is not part of version 1;
one repository-owned instruction layer keeps ownership clear.

Good instructions are specific enough to influence a review but broad enough
to survive ordinary refactors. For example:

- prefer the existing transaction owner for multi-step writes;
- check generated API clients when a public schema changes;
- prioritize maintainability and explicit failure behavior;
- use concise, direct, constructive wording.

Instructions cannot request credentials, change tools or authorization, lower
finding gates, suppress verified problems, or redefine publication behavior.
They may guide the review prose when that guidance is compatible with the fixed
contract. Public headings, hidden markers, finding fields, operator errors, and
other machine-readable wording remain deployment-owned English protocol.

## Compose platform and project context

Use `context/` for facts and contracts the reviewer cannot reliably infer from
one pull request. A service built on a shared backend platform can copy the
platform context into `context/platform.md`, then add service-specific context
in another file. A frontend can combine framework, starter-kit, and UI-system
files in the order declared by `config.toml`.

This is copy-and-edit composition, not inheritance. There is no remote import
resolver or central package registry. Teams can update a shared starter and copy
its reviewed package into a new repository without coupling every review to an
external repository or network request.

Keep context factual: supported extension points, integration boundaries,
known coupled settings, required validation, and failure modes. Keep general
review preferences in `instructions.md`.

## Record durable decisions

Store accepted ADRs under `.review-agent/decisions/` and list them in
`.review-agent/decisions.toml`. Review Agent ignores unlisted decision files.
The full typed format and feedback lifecycle are documented in [Feedback and
design decisions](./FEEDBACK_AND_DECISIONS.md#repository-decision-contract).

Decisions are separate from general context. Use an ADR when changing one
accepted invariant needs explicit downstream checks and evidence. Use a context
file when the information is a reusable platform fact or review aid.

## Know when changes become active

Review Agent reads the package from the pull request's exact **base commit** and
stores an immutable snapshot with the review run. A pull request cannot change
its own active review instructions. Changes to `.review-agent/` become active
after they are reviewed and merged, when a later pull request uses that commit
as its base.

Protect `config.toml`, `instructions.md`, `context/`, `decisions.toml`, and
`decisions/` with normal branch review and CODEOWNERS where appropriate.

The current contract accepts ADRs only under `.review-agent/decisions/`; it has
no compatibility path for other locations. New deployments should follow the
clean initialization sequence in
[Operations](./OPERATIONS.md#start-the-repository-context-release-cleanly).

## Capacity is not review depth

The package is optional model context, so it is bounded to avoid accidental
multi-megabyte GitHub reads, database rows, RAM use, and prompt expansion. A
configuration can list at most 10 context files; each file is limited to 400
lines. The combined instructions and context use the smaller of the plugin's
JSON-safe text-page capacity (`result_max_chars / 7`) and 81,920 characters.
With the packaged 160,000-character result budget, repository guidance can use
22,857 characters. The storage ceiling is derived from the 512 KiB immutable
snapshot contract with space reserved for paths, hashes, and lifecycle
metadata. These guards do not limit pull request size, changed files, diff
pagination, source reads, findings, or review depth.

If a package exceeds the guidance capacity, the review records a bounded reason,
omits the optional guidance for that run, and continues the complete review
contract.
