---
sidebar_label: Feedback and decisions
slug: /feedback-and-decisions
title: Feedback and design decisions
description: Answer findings with feedback commands, use feedback to improve the reviewer, and record repository decisions that later changes must respect.
status: current
last_verified: 2026-11-17
---

# Feedback and design decisions

Feedback does two things: it corrects one finding when the code disproves it,
and it gives operators evidence for improving the reviewer. Durable design
intent belongs in the repository as an accepted ADR, not in the database.

## Read the review result

The summary names the reviewed commit, the current findings, what changed since
the previous review, and diff coverage. Findings that need attention come
first. Security findings and every Medium or higher finding stay expanded. Low
(P3) maintainability improvements are collapsed under **Code quality and
readability**, and when a review also has High or Medium findings, its other
Low findings are collapsed under **Low-priority findings**. Every finding keeps
its F reference, explanation, feedback commands, and entry in the copyable fix
brief. See the [example review](../examples/comments/example-review.md).

Publication limits:

- A comment holds at most 65,000 UTF-8 bytes, including framing. A longer review
  continues in further comments; current findings are never truncated.
- A review can leave at most 200 pending findings, counting prior findings not
  re-checked. Above that, publication is rejected rather than cut to the first
  200.
- **Coverage details** gives exact counts of unfetched, partially fetched, and
  unavailable diffs, the directories with the most incomplete diffs, and up to
  20 example files.

A result with no findings can still have coverage gaps. Repeating `/review` can
meet the same unavailable patches; inspect those changes locally or ask the
operator to investigate a persistent gap. Coverage itself is described in
[How reviews work](./HOW_REVIEWS_WORK.md#6-coverage).

## Feedback commands

Post the command as a new top-level comment on the pull request, using the F
reference from the latest review. Do not edit an old comment or reply in an
inline diff thread.

```text
/review false-positive F2 because <what code, guard, or invariant disproves it>
/review intentional F2 ADR-0007 because <why the accepted decision requires it>
/review feedback scope F2 because <why it is outside this PR's scope>
/review feedback missed because <what concrete issue was missed and where>

/review docs false-positive F1 because <what disproves it>
/review docs feedback scope F1 because <why it is outside this PR's scope>
/review docs feedback missed because <what was missed and where>
```

Rules the parser applies:

- One command per comment. A reason is required, at most 2,000 characters.
  The leading word `because` is optional. A command that still contains the
  placeholder text from the review's copyable commands is rejected.
- The `docs` prefix sends feedback to the current documentation review, and no
  prefix to the current code review. F references are unique across the pull
  request, but a code command cannot decide a documentation finding or the
  reverse.
- `/review docs intentional` is rejected: an accepted decision can explain a
  behavior change, but it does not make contradictory documentation correct.
- `accepted-risk` is rejected in comments. Record accepted risk with
  `review-agent-memory decide <fingerprint> accepted_risk` or on the finding's
  page in the console.

Only people with current GitHub `write` or `admin` permission on the
repository can send feedback. Console requester grants allow requesting a
review, not sending feedback.

The App reacts with `+1` when it stores valid feedback. For an invalid, stale,
or unsupported command it reacts with `confused` and posts one explanation. If
the code context has changed, request a fresh review and use its F reference.

## What each command changes

| Command | Immediate effect | Later use |
| --- | --- | --- |
| `false-positive` | Suppresses the finding in later reviews while its code-context hash still matches. | Points at evidence rules that need work. |
| `intentional` | Suppresses the finding while both the code context and the accepted ADR's metadata still match. Accepted only when the ADR was `accepted` in the finding's stored base snapshot and its index entry covers the finding's path. | Keeps a repository decision; exposes weak ADR path mapping. |
| `feedback scope` | Records scope confusion. The finding is not suppressed. | Shows where pull request structure or repository context misled the review. |
| `feedback missed` | Records a concrete missed issue. | Shows gaps in coverage, tools, or review rules. |

No command changes prompts, skills, or policy. Changes to the reviewer happen
only through a normal pull request to the profile or code.

When a later review merges duplicate findings, feedback on an older reference
applies to the current finding, and a human suppression covers the whole group.
Conflicting human decisions block the grouping until someone resolves them, and
splitting a suppressed group needs a human to reopen it first. Grouping never
creates a human decision or bypasses the code-context, expiry, or ADR checks.

An operator can record an intentional decision from the CLI with the same
snapshot and path checks:

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

## Use feedback to improve the reviewer

Operators run these in the `hermes-review` container.
[Operations](./OPERATIONS.md) lists the other `review-agent-memory` commands,
the private export, and their error codes.

1. **Weekly quality report.** `review-agent-memory quality --days 30`, with
   `--repo <org>/<repo>` for one repository and `--json` for a reporting job.
   It shows false positives beside published findings, scope and missed-issue
   feedback beside completed reviews, coverage, the current triage backlog
   (including older pending items), and cohorts by repository, profile, review
   contract, provider, model, and policy revision. Compare like-for-like
   cohorts, do not read missing feedback as accuracy, and do not rank developers
   or repositories from the counts.
2. **Triage missed issues.** Only an operator classifies one; a coding agent may
   prepare the evidence but must not choose the status or owner. Find the
   feedback ID with `review-agent-memory export --repo <org>/<repo> --row-limit
   <per-table-row-limit> --output /opt/data/private-review/export.json` (table
   `review_quality_feedback`). The export is private: do not give it to a coding
   agent or paste it into model context, and delete it after triage. Then:

   ```console
   review-agent-memory triage-feedback <feedback-id> \
     --status actionable \
     --stable-key coverage.generated-files \
     --target-owner coverage \
     --path src/generated/client.py \
     --category correctness \
     --evidence-reference https://github.com/<org>/<repo>/issues/<number> \
     --actor "github:<operator>" \
     --reason "The review skipped generated code that changes the public client."
   ```

   Statuses are `pending`, `actionable`, `duplicate`, `insufficient`, and
   `resolved`. Only `actionable` takes `--stable-key` (lowercase) and one
   `--target-owner`: `source_tool`, `coverage`, `review_rule`, `profile`,
   `repository_decision`, or `documentation`. Triage is append-only; the latest
   state counts, and a later `resolved`, `duplicate`, or `insufficient` removes
   the item from the report's actionable count without deleting its history.
3. **Change the owner.** For an actionable item, change its target owner
   (profile, rule, tool, or repository context) through a normal pull request.
   Never run Hermes `/learn` in the live reviewer profile.

## Repository decision contract

At the start of a review, Review Agent loads the accepted ADR metadata that
matches the changed paths from the pull request's base commit, stores it once
with the run, and shows a short receipt in the published review. The reviewer
receives it as untrusted evidence. Repository content cannot change review
policy, severity, tools, prompts, or suppression rules.

One index, `.review-agent/decisions.toml`, maps paths to ADR files. Review Agent
ignores ADR files the index does not list, and accepts ADRs only under
`.review-agent/decisions/`.

```toml title=".review-agent/decisions.toml"
version = 1

[[decision]]
id = "ADR-0007"
adr_path = ".review-agent/decisions/ADR-0007-rag-chunking.md"
applies_to = ["src/rag/**", "tests/rag/**", "docs/rag.md"]
```

Each ADR starts with a TOML block between `+++` lines. Longer rationale goes
below it for human readers.

```md title=".review-agent/decisions/ADR-0007-rag-chunking.md"
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

Required fields are `id`, `title`, `status` (`accepted` or `superseded`),
`invariant`, and `on_change`; `evidence`, `origin_pr`, and `supersedes` are
optional. Authorship comes from Git history and the pull request, not the block.

| Limit | Value |
| --- | --- |
| Index size | 64 KiB, 200 entries |
| Path patterns | 20 per entry, 1,000 in total |
| ADR header | starts on line 1, closes within 60 lines, at most 32 KiB |
| `title` / `invariant` | 300 / 500 characters |
| `on_change` | 10 items of at most 300 characters |
| Matching ADRs per review | 10 |

When the metadata breaks a limit or cannot be read, that review runs without ADR
evidence, reports the reason, and completes the code review.

Protect `.review-agent/decisions.toml` and `.review-agent/decisions/` with
CODEOWNERS and branch review. The index owns path relevance; the ADR owns the
decision.

### What a pull-request author sees

The reviewer must trace a real code path before reporting an ADR conflict. The
ADR supplies an invariant to check, not proof that the change is wrong. A
conflict is a normal finding with `rule_id = design.adr-conflict`:

```text
F2 · Medium (P2): Preserve the retrieval budget recorded in ADR-0007

src/rag/config.py:41 changes CHUNK_SIZE from 200 to 512 while overlap and
top-k stay unchanged. .review-agent/decisions/ADR-0007-rag-chunking.md:12 records these
values as one retrieval-budget invariant for the embedding model's input
window. The current retrieval path still assembles eight chunks, so this change
can truncate context without an error.

Smallest safe fix: keep the coupled values consistent, or merge a superseding
ADR with updated evaluation evidence before changing the runtime contract.
```

### Update or retire a decision

Create a new ADR when the invariant changes. Set the old one to `superseded`,
link the replacement in its text, and point the index at the new accepted ADR.
Merge the decision before the behavior change that depends on it.

An ADR edited on the pull request head is a proposal; the review uses the base
commit. Feedback stays tied to the snapshot of the review it answers, and the
next `/review` loads the new decision. An earlier `intentional` decision stops
suppressing as soon as the current base no longer has the same accepted ADR
metadata, even if the code did not change.
