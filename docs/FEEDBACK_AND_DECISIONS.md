---
sidebar_label: Feedback and decisions
slug: /feedback-and-decisions
title: Feedback and design decisions
description: Use review feedback to improve reviewer quality and record repository decisions that later changes must respect.
status: current
last_verified: 2026-08-27
---

# Feedback and design decisions

> **Current**: Feedback collection, statistics, private coaching, and typed
> repository decision context work now. `/review intentional` remains planned
> until the App can bind that feedback to the exact stored ADR snapshot.

Review feedback has two jobs. It corrects one finding when the code disproves it,
and it gives operators evidence for later reviewer improvements. Teams should
record durable design intent in the repository rather than asking PostgreSQL to
become a second documentation store.

## What each feedback command does

Post a new top-level pull-request comment. Use the F reference from the latest
review.

| Command | Immediate effect | Later use |
| --- | --- | --- |
| `/review false-positive F2 because ...` | Suppresses the same finding while its code-context hash still matches. | Highlights evidence rules or replay cases that may need work. |
| `/review feedback scope F2 because ...` | Records scope confusion. It does not suppress the finding. | Shows where PR structure or repository context caused poor review scope. |
| `/review feedback missed because ...` | Records a concrete issue the review missed. | Shows gaps in source coverage, tools, or review rules. |

The App reacts with `+1` after it stores valid feedback. It reacts with
`confused` and posts one explanation for stale references or invalid commands.
A changed code context requires a fresh review.

## Review quality without self-modifying prompts

Review Agent keeps behavior changes behind human review. Use this operating
rhythm before changing the profile or code.

### Each week

Run global statistics in the operator container:

```bash
review-agent-memory runs --stats --days 30
review-agent-memory stats
```

Repeat both commands with `--repo <org>/<repo>` for repositories with failures,
repeat findings, or quality feedback. The output contains counts, so compare it
with review volume and coverage. A repository with more reviews will tend to
produce more feedback.

### Each month

Run the private coach for one repository with enough evidence:

```bash
review-agent-memory coach-run \
  --repo <org>/<repo> \
  --output-dir /opt/data/private-review/coach-run
```

`no_change` means stop. A proposal requires repeated independent episodes for
the same stable finding identity. Review the proposal, add a focused replay, and
change the canonical owner through a normal pull request. Do not run Hermes
`/learn` in the live reviewer profile.

### Send each signal to its owner

| Signal | Inspect first | Possible change after repeated evidence |
| --- | --- | --- |
| False positive | Original claim and disproof checks | Review procedure, evidence rule, or replay fixture |
| Scope confusion | PR structure and repository decision mapping | Contributor guidance or repository context |
| Missed issue | Coverage record and available tools | Source tool, review rule, or replay fixture |
| Intentional design | Accepted ADR and affected paths | ADR mapping or a narrow design replay |
| Accepted risk | Owner, expiry, and remediation plan | Governance record or planned engineering work |

Do not rank developers or repositories from raw feedback counts. The coach
exports omit actor identities, and teams should keep that privacy property when
they build internal reports.

## Current intentional-design decisions

An operator can record an intentional design decision against an existing
finding:

```bash
review-agent-memory decide <fingerprint> intentional_by_design \
  --repo <org>/<repo> \
  --pr <number> \
  --local-reference F2 \
  --adr-id ADR-0007 \
  --actor "github:alice" \
  --reason "The accepted retrieval-budget decision requires these coupled values." \
  --expires-days 180
```

The current command stores the ADR ID but does not read or validate the ADR.
The operator must confirm that the ADR exists, remains accepted, and applies to
the finding. PR comments cannot create intentional-design or accepted-risk
decisions yet.

## Repository decision contract

At the start of a review, the reviewer loads matching accepted ADR metadata from
the exact pull request base SHA. It stores one immutable snapshot with the run
and shows a short receipt in the published review. Repository content cannot
change review policy, severity, tools, prompts, or suppression rules.

One root index maps changed paths to ADR files:

```toml title=".review-agent/decisions.toml"
version = 1

[[decision]]
id = "ADR-0007"
adr_path = "docs/decisions/ADR-0007-rag-chunking.md"
applies_to = ["src/rag/**", "tests/rag/**"]
```

Each ADR starts with a short TOML block. Keep longer rationale below the block
for human readers.

```md title="docs/decisions/ADR-0007-rag-chunking.md"
+++
id = "ADR-0007"
title = "Keep the retrieval inputs inside the embedding budget"
status = "accepted"
invariant = "Chunk size 200, overlap 40, and top-k 8 form one retrieval budget for a 512-token embedding input."
on_change = [
  "Recalculate the assembled context budget.",
  "Run the long-document retrieval evaluation.",
  "Check truncation and overlap behavior."
]
evidence = "docs/evaluations/rag-retrieval-baseline.md"
origin_pr = 418
+++

# Context

Describe the user problem, alternatives, and consequences for maintainers.
```

The runtime parses `id`, `title`, `status`, `invariant`, `on_change`, and the
optional `evidence`, `origin_pr`, and `supersedes` fields. Keep the opening
marker on line 1 and close the block within 60 lines.

The fixed guards protect this optional metadata path. The index accepts 200
entries and 1,000 path patterns, and a run loads at most 10 matching typed ADR
headers. Parsed field lengths keep the begin response within its 160 KiB result
budget. The 200-entry index still covers path matching against GitHub's
3,000-file pull-request response ceiling. These guards do not limit pull-request
size and do not cap changed files, source reads, or review depth. The App ignores
only ADR evidence for that run when the metadata exceeds a guard. It reports the
reason and completes the code review.

Use CODEOWNERS and branch review for `.review-agent/decisions.toml` and
`docs/decisions/`. The index owns path relevance. The ADR owns the decision and
its human rationale. Git history and the pull request remain the source for who
approved a decision and when; the typed block does not duplicate authorship.

## What a pull-request author should see

The reviewer must trace a real code path before reporting an ADR conflict. The
ADR supplies an invariant to check, not proof that the change is wrong.

```text
F2 · Medium (P2): Preserve the retrieval budget recorded in ADR-0007

src/rag/config.py:41 changes CHUNK_SIZE from 200 to 512 while overlap and
top-k stay unchanged. docs/decisions/ADR-0007-rag-chunking.md:12 records these
values as one retrieval-budget invariant for the embedding model's input
window. The current retrieval path still assembles eight chunks, so this change
can truncate context without an error.

Smallest safe fix: keep the coupled values consistent, or merge a superseding
ADR with updated evaluation evidence before changing the runtime contract.
```

The reviewer uses a normal finding with `rule_id = design.adr-conflict`. It does
not add a separate governance report or name decision authors.

## Update or retire a decision

Create a new ADR when the invariant changes. Set the old ADR status to
`superseded`, add the replacement link in its narrative, and point the index at
the new accepted ADR. Merge that decision before the behavior change when the
change needs the new contract.

An ADR edit on the current pull-request head remains a proposal. The review uses
the accepted base snapshot. If a team supersedes an ADR after a review but before
someone submits feedback, the feedback remains tied to the older review snapshot;
the next `/review` loads the new decision.

## Current and next delivery boundary

The runtime now:

- loads one bounded snapshot from the exact base SHA;
- gives the reviewer typed metadata as untrusted evidence;
- publishes the snapshot status and matching ADR IDs;
- continues the source review when GitHub or ADR parsing fails.

The next slice validates `/review intentional` against the snapshot that
produced the published finding. It will store the ADR path, metadata hash, and
base SHA with the audit record. The App will suppress a later occurrence only
when both the code-context hash and accepted ADR metadata hash still match.

The first release will not add a dashboard, repository instruction injection,
directory listing, symbol graph, vector database, author inference, or automatic
policy promotion.
