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
its F reference, explanation, and entry in the copyable fix brief. The copyable
feedback commands appear once, at the end of the review, and only when the
operator has enabled them (`REVIEW_AGENT_FEEDBACK_ENABLED`, off by default).
See the [example review](../examples/comments/example-review.md).

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
- `accepted-risk` is rejected in comments. Record accepted risk on the finding's
  page in the console, or from the operator CLI:

  ```bash
  review-agent-memory decide FINGERPRINT accepted_risk \
    --repo OWNER/REPOSITORY \
    --pr PR_NUMBER \
    --local-reference F2 \
    --actor "github:alice" \
    --reason "The endpoint is reachable only from the internal network; accepted until the next audit."
  ```

Only people with current GitHub `write` or `admin` permission on the
repository can send feedback. Console requester grants allow requesting a
review, not sending feedback.

The App reacts with `+1` when it stores valid feedback. For an invalid, stale,
or unsupported command it reacts with `confused` and posts one explanation. If
the code context has changed, request a fresh review and use its F reference.

## What each command changes

| Command | Immediate effect | Later use |
| --- | --- | --- |
| `false-positive` | Suppresses the finding in later reviews while its code-context hash still matches and the decision has not expired. | Points at evidence rules that need work. |
| `intentional` | Suppresses the finding while the code context and the accepted ADR's metadata both still match and the decision has not expired. Accepted only when the ADR was `accepted` in the finding's stored base snapshot and its index entry covers the finding's path. | Keeps a repository decision; exposes weak ADR path mapping. |
| `feedback scope` | Records scope confusion. The finding is not suppressed. | Shows where pull request structure or repository context misled the review. |
| `feedback missed` | Records a concrete missed issue. | Shows gaps in coverage, tools, or review rules. |

Every suppressing decision expires, whether it is a false positive, an
intentional decision, an accepted risk, or a duplicate. A decision made with a
comment or on the console's finding page expires after 180 days. The CLI
(`--expires-days`) and the console API (`expires_days`) accept 1 to 3,650 days
and use 180 when none is given. A later review suppresses the finding only
while the decision has not expired, the stored code-context hash still
matches, and, for an intentional decision, the current base still has the same
accepted ADR metadata. After expiry the finding is evaluated again even if the
code did not change, and suppressing it again needs a new decision.

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
review-agent-memory decide FINGERPRINT intentional_by_design \
  --repo OWNER/REPOSITORY \
  --pr PR_NUMBER \
  --local-reference F2 \
  --adr-id ADR-0007 \
  --actor "github:alice" \
  --reason "The accepted retrieval-budget decision requires these coupled values." \
  --expires-days 180
```

## Use feedback to improve the reviewer

Feedback never changes the reviewer by itself. Operators read it in a weekly
quality report, classify missed issues, and change the owning profile, rule,
tool, or repository context through a normal pull request. The procedure is in
[Operations](./OPERATIONS.md#quality-report-and-triage).

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
