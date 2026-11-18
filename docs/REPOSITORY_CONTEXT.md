---
sidebar_label: Repository context
slug: /repository-context
title: Configure reviews in a repository
description: Add optional team instructions, ordered platform context, typed design decisions, and maintained-document mappings without changing the shared safety contract.
status: current
last_verified: 2026-11-17
---

# Configure reviews in a repository

A repository can add an optional `.review-agent/` package with review
instructions, technical context, accepted design decisions, and documentation
mappings. It is ordinary version-controlled content; no operator change is
needed. A repository without the package gets the deployment's neutral
baseline.

The deployment still owns the reviewer identity, the evidence and security
contract, tools, the model route, and GitHub publication
([Behavior ownership](./BEHAVIOR_OWNERSHIP.md)). Repository content can focus the
review and its wording; it cannot replace authorization, finding gates,
severity rules, tools, or publication.

## The package

```text
.review-agent/
├── config.toml           Turns guidance on and lists context files in order
├── instructions.md       Engineering principles, review focus, and response style
├── context/              Technical facts the reviewer cannot infer from one PR
│   ├── platform.md
│   ├── backend.md
│   └── frontend/framework.md
├── decisions.toml        Index of accepted ADRs and the paths they cover
├── decisions/            Typed ADRs
│   └── ADR-0001-example.md
└── documentation.toml    Maps source areas to maintained documents
```

Every file is optional. `documentation.toml` and the decision files work without
`config.toml`.

## Start with the copyable package

A Review Agent checkout runs its commands from a virtual environment that
holds the checkout and its dependencies at their pinned versions. Create it
once, then copy the starter only when the repository has no package. Edit an
existing package in place. Then validate the target repository. These commands
belong to the revision this page describes; in a checkout of a release, use
the commands in that checkout's copy of this page:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check \
  --constraint requirements/requirements-admin.txt -e .
repository_root=/path/to/repository
if [ -e "$repository_root/.review-agent" ]; then
  echo ".review-agent already exists; preserve it and edit it in place."
else
  cp -R examples/repository-context/.review-agent "$repository_root/"
fi
.venv/bin/review-agent-admin repository-context validate \
  "$repository_root"
```

Without a checkout, run the same validator from the release image inside the
target repository:

```bash
docker run --rm --read-only \
  --network none \
  --entrypoint review-agent-admin \
  --mount type=bind,source="$PWD",target=/repo,readonly \
  ghcr.io/ccimen/review-agent:v0.4.14 \
  repository-context validate /repo
```

The validator needs no GitHub, PostgreSQL, Hermes, model, or secrets. It checks
`documentation.toml` even when the repository has no guidance or ADR index. Its
JSON receipt contains paths, hashes, area IDs, counts, and status, never file
contents.

## Select context files

`config.toml` is the only index of guidance files:

```toml title=".review-agent/config.toml"
version = 1
enabled = true

context = [
  "context/platform.md",
  "context/backend.md",
  "context/frontend/framework.md",
]
```

Review Agent reads `instructions.md` first, if present, then only the listed
context files, in order. Paths are relative to `.review-agent/`, must be under
`context/`, end in `.md`, and appear once. Nothing is discovered by scanning
directories, inferred from repository names, or fetched from elsewhere.

`enabled = false` keeps the files without applying the instructions or context,
and removing `config.toml` opts out of them. A missing listed file or an invalid package drops all
guidance for that review; the code review still runs.

## Write instructions

`instructions.md` holds repository-wide engineering principles, review focus,
and communication style. There is one instruction file; a separate
`personality.md` is not supported. Write rules that survive ordinary
refactors, for example:

- prefer the existing transaction owner for multi-step writes;
- check generated API clients when a public schema changes;
- prioritize maintainability and explicit failure behavior;
- use concise, direct, constructive wording.

Instructions cannot request credentials, change tools or authorization, lower
finding gates, suppress verified problems, or change publication. They can
shape the review's prose. Headings, hidden markers, finding fields, and other
machine-readable text stay in the deployment's fixed English format.

## Add platform and project context

Use `context/` for facts and contracts the reviewer cannot infer from one pull
request: extension points, integration boundaries, coupled settings, required
validation, and failure modes. A service on a shared platform can copy the
platform's context into `context/platform.md` and add its own file; a frontend
can combine framework and design-system files in the order `config.toml`
declares. Composition is copy-and-edit: there is no remote import or central
registry. Keep general review preferences in `instructions.md`.

## Record durable decisions

Store accepted ADRs under `.review-agent/decisions/` and list them in
`.review-agent/decisions.toml`; unlisted files are ignored. Use an ADR when
changing one invariant needs explicit checks and evidence, and a context file
for reusable facts.
[Repository decision contract](./FEEDBACK_AND_DECISIONS.md#repository-decision-contract)
gives the format and limits.

## Get architecture-aware reviews

Every code review checks whether the pull request introduces or worsens an
architecture problem in the changed code: a rule implemented outside its owner,
duplicated policy, hidden coupling, a misleading abstraction, or complexity that
causes a concrete defect or change cost. Without context, the reviewer infers
the intended structure from the diff and the files it reads. Two files make
that intent explicit.

First, describe the structure in a context file and list it in `config.toml`:

```md title=".review-agent/context/architecture.md"
# Architecture

## Owners
- `src/billing/` owns invoices and payment state. Other modules call
  `billing.service`; they never write billing tables directly.
- `src/api/` validates requests and calls services. It holds no business rules.

## Dependency direction
- `api` -> `services` -> `repositories`. Repositories never import services.
- `src/common/` imports nothing from feature modules.

## Cross-cutting contracts
- Every multi-step write runs inside the unit of work in `src/db/uow.py`.
- Background jobs receive the tenant ID explicitly and never read request state.
```

Second, record each invariant that must not change without a decision as an
accepted ADR whose `applies_to` covers the paths it protects. A pull request
that breaks it gets a `design.adr-conflict` finding when the reviewer traces a
real consequence in the changed code.

The reviewer treats both as evidence. It does not assess the whole repository's
architecture, report structure the pull request did not change, or propose
speculative redesigns. Point the repository's `AGENTS.md` or `CLAUDE.md` at the
same architecture file so local coding agents and the review share one
description.

## Map maintained documentation

`.review-agent/documentation.toml` declares which documents explain which
source areas. [Documentation review](./DOCUMENTATION_REVIEW.md) uses it.

```toml title=".review-agent/documentation.toml"
version = 1

[[area]]
id = "configuration"
sources = ["src/config/**", "deploy/**"]
documents = ["docs/configuration.md", "docs/upgrading.md"]
intent = "Keep required settings, defaults, migration order and recovery steps accurate."

[[ignore_changes]]
paths = ["tests/fixtures/internal/**"]
reason = "Internal fixtures are not shipped or published as examples."

[[ignore_documents]]
paths = ["docs/archive/**"]
reason = "These guides intentionally describe previous releases."
```

Format rules:

- Top-level fields are `version` (`1`), `area`, `ignore_changes`, and
  `ignore_documents`. An area has exactly `id` (unique), `sources`, `documents`,
  and `intent`. An exclusion has exactly `paths` and `reason`.
- Paths are normalized and relative to the repository root. Globs support `*`
  and `?` within a path segment and `**` as a whole segment.
- Listed documents must exist, and a document cannot be both listed and ignored.
  The validator also rejects unknown fields, duplicates, paths that escape the
  repository, unsupported glob syntax, and symlinks.
- `ignore_changes` applies only to changes no area maps; an area mapping wins.
  `ignore_documents` says why a document need not match the current version; it
  does not exempt policy files or other checks.

The starter maps a sample source area to its own `context/platform.md` so the
copied package validates. Replace it with the repository's real mappings.

## Preview documentation scope from committed revisions

After committing a change locally, preview its documentation scope without
GitHub, PostgreSQL, a model, or running repository commands:

```bash
.venv/bin/review-agent-admin repository-context docs-scope \
  /path/to/repository \
  --base <target-base-commit> \
  --head <pull-request-head-commit>
```

`--base` and `--head` must be local commits. The command computes their merge
base and prints a JSON receipt with the three commit IDs, changed files and
previous rename paths, selected areas and intents, documents, exclusions,
unmapped paths, proposal status, and incomplete inputs. It reads Git objects
only; uncommitted edits do not affect it.

The base commit's `documentation.toml` is the accepted policy. A head edit is
reported as a proposal and cannot replace or weaken valid base rules. When the
head adds the file for the first time, the command shows the proposed scope but
reports `not_configured`. An invalid head proposal is reported separately while
valid base rules stay active; an invalid base policy reports
`invalid_configuration`.

## Check the package in CI

Copy the [example workflow](https://github.com/CCimen/review-agent/blob/main/examples/workflows/review-agent-context.yml)
to `.github/workflows/`. On every pull request it validates the package and adds
the documentation scope preview to the job summary. Both commands run in the
release image with no network, secrets, model, or App access, and an invalid
package fails the job.

The workflow has no path filter, so every pull request gets a result and it can
be a required check. It does not handle `merge_group` events, so it does not
suit a merge queue. The container runs as the runner user because Git refuses a
checkout owned by another user. Keep the image tag on the release your
organization runs.

## When changes take effect

Review Agent reads the package from the pull request's **base commit** and
stores it with the review run, so a pull request cannot change its own review.
Changes to `.review-agent/` apply once merged, to pull requests based on a
commit that contains them. Protect the package with branch review and
CODEOWNERS.

## Limits

| Item | Limit |
| --- | --- |
| `config.toml` | 16 KiB, 100 lines |
| Context files listed | 10 |
| Lines per instruction or context file | 400 |
| Instructions and context combined, live review | one text page (the per-result limit divided by 7), at most 81,920 characters; 14,285 with the packaged settings and a context window of 166,667 tokens or more |
| Instructions and context combined, offline validator | 22,857 characters (the packaged 160,000-character result limit divided by 7) |
| Stored guidance snapshot | 512 KiB |
| `documentation.toml` | 64 KiB |
| Areas; `ignore_changes`; `ignore_documents` tables | 200 each |
| Patterns per entry; patterns in total | 20; 1,000 |
| Documents per area | 20 |
| Area ID; `intent`; `reason` | 64; 1,000; 500 characters |

The validator accepts more than a live review can use, so keep instructions and
context below the live figure. These limits apply only to the package: guidance
that is too large is left out with a recorded reason, and the pull request is
still reviewed in full.
