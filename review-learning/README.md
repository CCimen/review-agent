# Review-agent learning pipeline

This directory is for private, human-governed reviewer improvement work. It is
not installed into the public webhook reviewer and it is not a policy source.

The public review-agent profile stays locked down: `bootstrap/config.yaml`
disables local file access, shell/code execution, session search, web, memory
writes, skill writes, and delegation. The reviewer writes bounded SQLite
observations through the review plugin. A private review-coach workflow may
read exported observations, propose improvements, and produce normal Git
changes for humans to review.

## First slice

The preferred operator path reads the live SQLite database directly and writes
only bounded private coach artifacts:

```bash
review-agent-memory --db /opt/data/review-memory/review_memory.sqlite3 \
  coach-run \
  --repo Sundsvallskommun/example-repository \
  --output-dir /opt/data/review-memory/coach-run
```

`coach-run` records a receipt in SQLite and emits `coach-export.json`,
`proposal.json`, and `SUMMARY.md` with mode `0600`. The evidence pairs the
reviewer's original claim and disproof checks with the maintainer's
counter-evidence. If fewer than two independent episodes support the same stable
finding identity, the correct result is `no_change`.
Coach/proposal schema v1 artifacts are not migrated; regenerate them from SQLite.

Use the lower-level export commands below only for historical snapshots or
diagnosis; the normal coach path does not need a raw database export on disk.

Generate an export from the operator CLI:

```bash
review-agent-memory export \
  --output /opt/data/review-memory/export.json
```

Then generate a private candidate report from that export:

```bash
review-agent-memory learning-report \
  --export /opt/data/review-memory/export.json \
  --repo Sundsvallskommun/example-repository \
  --output /opt/data/review-memory/learning-candidates.md
```

The report groups decision chains by finding fingerprint. For example, a
`false_positive -> reopen -> false_positive` sequence is one current decision
episode with the chain preserved as context, not three independent policy
candidates.

For private coach input, generate the typed allowlist JSON bundle instead of
feeding raw exports or Markdown to an LLM:

```bash
review-agent-memory coach-export \
  --export /opt/data/review-memory/export.json \
  --repo Sundsvallskommun/example-repository \
  --after-decision-id 0 \
  --after-feedback-id 0 \
  --output /opt/data/review-memory/coach-export.json
```

Coach exports contain stable event ids, exact observation provenance, bounded
`*_untrusted` text fields, an exact snapshot hash, and an event-set hash for
deduping equivalent evidence across exports. They omit actors, source URLs, and
raw database rows. Output files are written atomically with mode `0600`.

For independent finding falsification, export one completed review run instead
of asking another model to review the whole PR:

```bash
review-agent-memory verification-export \
  --run-id <id> \
  --output /opt/data/review-memory/verification/run-<id>.json
```

Verification exports are shadow-mode evidence bundles. They do not call Claude,
publish comments, suppress findings, change reviewer policy, or open pull
requests. Use them to ask a private reviewer to challenge the current published
findings; promote only human-accepted lessons through replay fixtures and normal
code review.

Then select deterministic improvement proposals from the coach events:

```bash
review-agent-memory coach-propose \
  --events /opt/data/review-memory/coach-export.json \
  --output-dir /opt/data/review-memory/coach-proposal
```

This writes:

- `proposal.json`: machine-readable candidate, governance, and rejection data;
- `SUMMARY.md`: a human-readable summary and copyable next-step prompt.

The proposal step is the first automated-improvement gate. It does not call an
LLM, edit reviewer policy, or open a GitHub PR. It admits normal improvement
candidates only after repeated independent episodes for the same stable finding
identity. A single accepted-risk decision is kept as a governance observation,
not treated as evidence that reviewer behavior should change.

Review-quality feedback that lacks exact publication or finding provenance is
shown as not promoted until the feedback writer records that provenance.

Validate replay fixtures before relying on them:

```bash
review-agent-memory validate-replay review-learning/replay
```

Replay fixtures are strict JSON files. This keeps validation on the standard
library path and fails loudly instead of silently accepting a partial YAML parse.
The generic baseline intentionally ships without historical fixtures, so the
bundle check reports a skip until one is added. Direct validation of an empty
fixture directory still fails loudly. Fixture validation checks structure only.
Before marking a lesson
`replay-tested`, run the current reviewer policy blind against the exact
historical base/head and retain a scrubbed, bounded execution receipt under
`review-learning/reports/`. A receipt must distinguish the expected replay claim
from unrelated findings returned from the same historical snapshot.

The report reads explicit human decisions and any populated
`review_quality_feedback` rows. Empty review-quality sections mean no allowlisted
feedback command has been ingested yet. That is correct; do not infer learning
from silence, merges, thumbs-up, or a later code change.

New decisions are anchored to the exact `finding_observations.id` that the human
judged. The report derives repository, PR number, head SHA, path, title, and
local `F` reference from that immutable observation, not from the mutable latest
`findings` identity row. Older decisions without observation provenance remain
visible as historical context, but they are marked incomplete and are not
eligible for promotion into policy.

Generated reports may contain maintainer-entered reasons, private URLs, or
customer-specific details. Review and scrub them before committing or sharing.
Move only scrubbed reports that are useful to review as versioned artifacts into
`review-learning/reports/`.

## Signal strength

Strong signals:

- explicit `false_positive` or `intentional_by_design` decision with a reason;
- missed issue linked to a bug, incident, security issue, or concrete example;
- severity correction from an authorized maintainer;
- invalidated or reopened finding with counter-evidence;
- fixed finding backed by a regression test.

Medium signals:

- duplicate finding decisions that show repeated root-cause splitting;
- remediation feedback where the proposed fix was unsafe or impractical;
- repeated unclear or too-verbose feedback across reviews.

Weak signals are ignored:

- silence;
- a PR merge without addressing a finding;
- thumbs-up or generic praise;
- a later code change without a linked decision or test.

## Promotion ladder

1. `captured`: a decision or feedback row exists in SQLite.
2. `candidate`: the export report surfaces it as an improvement candidate.
3. `replay-tested`: a historical replay case proves the current reviewer would
   make the same mistake or should preserve the same useful behavior.
4. `human-approved`: a maintainer accepts the policy, ADR, skill, or plugin
   change.
5. `shadow`: the change is measured on real reviews without making it a gate.
6. `active`: the change is deployed through version control and
   `/opt/review-agent-bootstrap/install.sh --force-agents`.
7. `retired/replaced`: a better canonical owner absorbs it or the lesson stops
   matching current architecture.

## Where approved lessons go

Do not create a second always-on policy file for production. Fold approved
lessons into the narrowest canonical owner:

- exact finding decisions stay in SQLite;
- architectural context becomes an accepted ADR;
- visible review shape belongs in `bootstrap/profiles/sundsvall-standard/workspace/AGENTS.md`;
- review procedure belongs in `bootstrap/profiles/sundsvall-standard/skills/review-agent-pr/SKILL.md`;
- mechanical enforcement belongs in plugin code and tests;
- replay behavior belongs in `review-learning/replay/`.

Hermes `/learn` can turn a scrubbed `SUMMARY.md` into a staged draft, but run it
only in a separate operator profile or workstation that does not share the live
reviewer's `HERMES_HOME`, skills, or gateway. Inspect the draft with
`/skills pending` and `/skills diff <id>`, port only the validated lesson to the
canonical owner in this repository, then reject the draft. Do not run `/learn`
on arbitrary PR comments, contributor branches, raw session transcripts, or
unsanitized exports. Proposed learnings stay advisory until a human approves
them and a replay or focused test protects the behavior.
