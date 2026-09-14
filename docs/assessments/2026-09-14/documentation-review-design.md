# Scoped documentation review design

Design prepared 14 September 2026 against `feature/admin-panel` at
`d2554c4d75083227a3d8672ba5bb8f21b3d04b52`. The version 1 `documentation.toml` validator and
revision-bound local `repository-context docs-scope` preview are implemented. Live documentation
reviews, settings and GitHub check publication remain proposed and are not deployed. Beads epic
`ra-docs-review-ms1` owns implementation status, dependencies and acceptance; this document owns
the product and technical decisions.

A ready pull request should receive a focused, advisory assessment of documentation that its
changes could make incorrect. Authors get evidence and a small correction they can apply in the
same PR. Existing documentation can already be correct; a matched source path never creates an
obligation to edit prose by itself.

Use the existing GitHub App, PostgreSQL, durable delivery/job system, bounded source gateway,
trusted profile and publisher. Add one review purpose, one repository mapping file, one focused
procedure and one documentation check. Automatic opt-in is part of the intended feature; the
manual path provides a controlled way to validate it before automatic admission is enabled.

The feature checks selected claims against a specific change. It cannot certify the whole
repository. A missing mapping, unread document, exhausted budget or unavailable provider must
remain visible.

## Developer experience and ownership

| Surface | Contract |
| --- | --- |
| `/review` and existing aliases | Preserve the current code review and feedback behavior. Do not implicitly launch docs review. |
| `/review docs` | Authorized immediate documentation assessment, including draft PRs; reuse equivalent active work. A new explicit request may rerun a terminal attempt. |
| `/review docs false-positive F2 because ...` | Resolve the finding through the current documentation publication, with existing authorization, audit and expiry protections. |
| `/review docs feedback scope F2 because ...` and `/review docs feedback missed because ...` | Record docs-specific quality feedback. Never mix code and docs denominators. |
| Unsupported docs commands | Return concise help. Never fall through to code feedback. Docs-specific `intentional` suppression is deferred. |
| `Review Agent / Documentation` | One current result per PR and purpose, attached to the stored head SHA, with precise coverage and suggested corrections. Keep successful and skipped results quiet. |
| Deterministic CI | Validate configuration, generated references, documentation builds and reproducible examples. These checks may gate merging independently. The AI check is advisory and is not required in V1. |

A repository enables scope by adding `.review-agent/documentation.toml` in an ordinary PR. It
does not need to add instructions, context files or ADRs merely to adopt this capability.
Authorized settings resolve a team default and optional repository override to `off`, `manual`
or `automatic`; start with Manual and require an explicit repo or team-default choice for
automatic work. The console exposes the effective mode and its source. A
typed boolean in the existing deployment-settings owner initially keeps docs disabled until the
compatible worker, permission and publication path is qualified. Repository mode then controls
manual or automatic admission. Disabling docs preserves code review and historical results.

Extend the current repository access/operator owner. Do not repurpose the existing
`trigger_mode` (how repository access was granted) as a docs review setting, infer spending
permission from repository prose, or introduce a new settings service or top-level dashboard.
Expose the shared policy owner through supported operator controls and the existing console.
Missing Checks permission is a capability-specific setup problem; it must not make ordinary
code review unavailable.

## Team and repository administration

The console is part of V1 adoption, not a later optional dashboard. Extend existing Team detail,
repository views, Settings, Activity, Quality and Usage. Preserve the Astryx Neutral design and
current scope/navigation. The product UI contract belongs in [the console
design](../../../admin/DESIGN.md#documentation-review-controls).

| Level | Proposed control | Meaning |
| --- | --- | --- |
| Platform Settings | Documentation review enabled/disabled | A global switch in existing owner-only Settings is a hard stop. It does not grant GitHub access or change code review. |
| Team detail → Documentation review | Default: Off, Manual or Automatic | Applies to repositories using the team default. Start with Manual. Existing team model/account and capacity policy also serves docs reviews. |
| Repository → Documentation review | Use team default, Off, Manual or Automatic | An explicit repo override wins over the team default. Show its source and offer a return to inheritance. |
| Repository rules and decisions | Read-only current rules, intent, exclusions, guidance and indexed ADRs, with revision/source links | Repository files retain authority. Edits use a normal GitHub PR; the panel does not maintain another live copy of the rules. |

A team default is a default, not a ceiling or team-wide pause: changing it to Off does not
disable an explicitly Automatic repository. Label this clearly, show the number of inherited
repositories affected and the exceptions before Save, and link to their settings. The platform
disable always wins. Do not add another override-permission matrix, per-repo model selection,
docs-specific budgets or scheduling controls in V1.

For example, Platform team can default to Manual, while `payments-api` explicitly uses Automatic
and `legacy-service` uses Off. Another repo can show “Manual · inherited from Platform team.” A
single server-owned resolver returns configured override, team default, effective mode, source,
applicable revisions and readiness reasons to both admission and the console. The browser must
not recreate precedence. An unassigned repo uses the Manual fallback and only platform
administrators can manage it; this does not override its existing App activation/access
requirements.

New repositories normally inherit. Changing ownership recomputes inherited mode; explicit
repository overrides remain repository settings. The existing assignment/transfer action must
show the resulting effective docs mode, including any move to Automatic and account/team context
change. Audit that result. Recheck current ownership and maintainer authorization in the write
transaction, so a stale page cannot edit a repository after transfer.

Team maintainers can change their team's docs default and overrides for its assigned
repositories; platform admins can manage those controls across teams. Team viewers can inspect
permitted configuration and results but cannot save. Preserve the owner-only global Settings
boundary, platform-admin App activation/installation control and current
model-connection/capacity restrictions. Saving Automatic never grants installation access,
enables a disabled repository, promotes a viewer or bypasses the global stop. GitHub permissions
still govern editing the repository's actual rules and ADRs.

Separate intended mode from readiness. Show “Automatic · waiting for merged documentation
rules,” “Manual · ready,” “Automatic · Checks permission approval needed” or “Paused by platform
settings,” with the precise next action. Optional guidance or missing ADRs alone must not appear
as failed setup. Display the owning team, active/default-branch configuration revision, last
refresh time and last review with coverage. A bounded authorized Refresh reads and validates
repository configuration without a model; it does not run a repository-wide audit. A PR review
still displays the exact base policy and ADR snapshots it used, which may differ from the latest
default-branch view.

Provide View rules in GitHub, Copy starter configuration, View pull request and Copy `/review
docs` actions. These make adoption and manual requests usable without adding a new
admin-initiated review authorization path or repository-write workflow in V1. A future editor
may prepare a PR; it must not silently save different rules only in the database. Team-wide
standards can be adopted through reviewed repository files; live inheritance of arbitrary team
prose or team ADRs is outside V1.

Use the existing settings save/revision and audit patterns. Preserve drafts on refresh and
recoverable errors; reject stale revisions and unauthorized writes on the server. Show changed
mode, actor, time and effective source in the relevant history. Save does not mass-review all
currently open PRs: Automatic takes effect on subsequent eligible events or explicit requests.
Moving effective mode to Manual cancels pending automatic work and prevents new automatic
admission; already running authorized work can finish, and manual work remains eligible.
Off/global disable stops docs work through the same
admission/execution/publication checks. Retained reviews keep their original trigger and policy
provenance.

Monitoring follows the current team/repository/period navigation. Add Code/Documentation purpose
selection to relevant Activity, reader, Quality and administrative Usage surfaces. Team/repo
lists show mode, readiness, last result and items needing attention; a row links to the exact
review. Distinguish waiting for the quiet period, queued, running, complete, skipped,
incomplete, failed and superseded. “Not configured” and “not checked” are not successful
reviews. Show selection/exclusion reasons and whether a model ran in the result details; never
label a whole repo's documentation verified or turn these counts into an accuracy score.

Keep costs/token use and model-reporting coverage on the existing owner/admin Usage page,
filterable by team, repo and purpose. Team maintainers/viewers retain scoped Activity and
Quality access; do not widen financial/operational permissions just to add a docs filter.
Preserve current ownership semantics in Usage and separately retain admission-team/policy
provenance on historical runs. Reuse existing refresh and failure displays; no separate
monitoring service, alert stack or new top-level dashboard is required.

## Repository policy and accepted intent

The existing files retain their responsibilities:

- `config.toml`: optional guidance activation and its ordered context index. Its `enabled` flag continues to mean guidance activation.
- `instructions.md` and indexed `context/` files: engineering guidance, terminology and relevant project facts.
- `decisions.toml` and typed ADRs: accepted decisions, invariants and change consequences.
- New `documentation.toml`: maintained document relationships, reader intent and explained exclusions.

The proposed closed TOML contract is:

```toml
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

These illustrative paths are not Review Agent's starter. The tested starter must use actual
repository files and cover review commands/feedback, repository context and deployment.

Use the existing constrained ADR glob semantics for `sources` and exclusion paths; documents are
exact normalized repository paths. Extract the shared path/glob primitive only where two
consumers need it. Do not make docs parsing depend on successfully parsing the ADR index. Bound
bytes, entries, patterns, path lengths, intent and reasons with named protocol constants; reuse
existing bounds where their meaning matches. Reject unknown keys, duplicate IDs, escaping paths,
unsupported globs, remote imports, executable settings, invalid text and symlinks/submodules
masquerading as files. Do not add per-repository model budgets.

An area is selected when either a source path or a listed document changes. Include
authoritative previous paths for renames and deleted paths. Deduplicate reads of a document
selected by several areas, preserving each intent. Explicit mappings win over `ignore_changes`;
that exclusion only removes otherwise-unmapped changes. `ignore_documents` excludes
current-version freshness assessment, not source obligations, structural validation, builds,
link checks or code review. An explicitly listed document also excluded by `ignore_documents` is
invalid configuration.

Policy files must still receive deterministic structural/proposal assessment when exclusions
match them. Exclusions require a reason and appear in the scope receipt. Blanket built-in skips
for tests, dependencies, frontend or generated files are inappropriate because those files can
change supported behavior.

Accepted policy and active guidance/ADRs come from the exact target-base snapshot. A head edit
is a proposal. Newly added head configuration produces a deterministic proposed-scope preview,
with no semantic verification claim or model call. Head exclusions cannot suppress review under
existing base rules. A malformed base configuration yields `invalid_configuration`; a malformed
proposal is reported separately and does not replace valid base policy.

A document missing at the accepted policy revision is a configuration error. A document deleted
or renamed by the PR is a change to assess. Follow verified rename metadata, inspect old and new
content and show a missing/moved mapping repair. Permit that mapping update in the same PR; no
preliminary policy PR is required for an ordinary move. Do not silently trust a head path
rewrite without evidence linking the replacement.

ADRs protect intent. Compare accepted intent, implementation and the reader-facing claim
separately. If code and docs agree but contradict an accepted invariant, report a
decision/behavior conflict and the needed human decision. Do not draft prose that normalizes a
likely bug.

For docs-only PRs, map relevant documentation paths using existing ADR `applies_to`. Preserve
real changed-path matching and its count/provenance requirements. Do not add `adr_ids` or
fabricate a changed source path to force an ADR match. Assess changed ADR files as proposals
without activating them. Preserve the existing merge-first lifecycle for superseding decisions.
When indexed context is also a maintained document, its base is active guidance and its head is
the assessed artifact; label these roles explicitly.

## Scope, source evidence and outcomes

Extend `review-agent-admin repository-context validate ROOT` to report optional docs
configuration without changing its local-tree semantics. Add `repository-context docs-scope ROOT
--base COMMIT --head COMMIT` for deterministic, revision-bound selection. It uses local Git
objects, no GitHub, database or model, and explains selected areas, documents, exclusions,
unmapped paths, proposal status and incomplete inputs. Dirty working-tree files do not change
this preview.

Preserve `base_sha` as the PR target-base identity. Record the comparison/merge-base SHA
separately to attribute introduced or worsened behavior, and record head SHA. An accepted
target-base policy is not interchangeable with the comparison baseline. Resolve exact comparison
provenance through the bounded source owner; if it cannot be established, expose the limitation
rather than blaming the PR for unrelated target-branch changes. The local preview uses the same
semantics, not a two-dot shortcut.

Persist a small immutable documentation scope receipt referencing the run, exact refs, complete
changed-file inventory, configuration digest, guidance/ADR snapshots, selected areas/documents
with reasons, exclusions and contract version. Use one docs result record per run, with a
write-once scope receipt and bounded server-recorded evidence/coverage, frozen at publication.
Subsequent evidence reads retain exact path, explicit revision, role (policy, comparison or
head), blob identity and ranges. Reuse existing head/base read receipts where their meaning
matches; comparison evidence must not be stored as a target-base read. Do not copy the
repository documentation tree into PostgreSQL or overload the optional guidance context budget
with all documents.

Assess the relevant scope of the whole current PR on every run. A docs-only follow-up must
recheck the earlier implementation change it addresses. Duplicate active requests share work; no
cross-revision semantic cache is needed in V1.

| Scope | Required behavior |
| --- | --- |
| Complete inventory; everything explicitly excluded | Persist a scoped `not_needed` receipt and publish a terminal skip with zero model calls. |
| Mapped change | Review the selected relationship; unchanged but now incorrect docs remain valid targets. |
| Unmapped source or changed document | Use bounded impact assessment and repository reads. It may find a document or conclude no impact; unresolved relevance becomes a coverage gap, never a deterministic skip. |
| Discovered relevant document | Record that it was discovered and retain its evidence. Do not write a new mapping automatically. |
| Invalid/unavailable/truncated inventory or required content | Report invalid, unavailable or incomplete input, preserving useful findings. Do not mark clean. |
| No base configuration | Report not configured, or a proposed-scope preview when the PR adds configuration. Do not spend model capacity. |

Read headings and relevant sections, expanding only to evidence needed to verify the claim. Code
examples are data, not commands to execute. Reuse bounded exact-revision readers; add a narrowly
scoped docs evidence path where the current unchanged-base-file guard cannot serve valid
baseline reads. Preserve the existing code-review guard and full-PR coverage contract. No
repository execution, arbitrary remote browsing, graph/index infrastructure or additional agent
is required.

Keep lifecycle, scope coverage and semantic outcome separate. A completed assessment can be
`not_needed`, `no_mismatch_found`, `findings`, `incomplete`, `not_configured`,
`invalid_configuration` or `unavailable`; these are not new global run statuses. Record whether
semantic inference was used. Findings can coexist with incomplete coverage. A successful clean
result requires completed selected coverage. Old unrelated debt is not an actionable PR finding;
an incidental non-blocking note is sufficient.

## Run, finding and feedback contracts

Add typed `ReviewPurpose.code` and `.documentation` at the canonical request/run boundary. Keep
file-level `ReviewMode` unchanged. Existing persisted rows and external code-only callers retain
`code`. Derive purpose through run/subject relations where possible; duplicate it only when a
required database constraint needs a relationally enforced discriminator.

A forward migration must isolate active runs, current publications and stable finding identity
by purpose. Put purpose in subject identity and on the run/publication rows needed for partial
uniqueness, with composite foreign keys preventing disagreement. Include purpose in
repository-scoped finding uniqueness while preserving existing code fingerprints. Shared
reference allocation and review numbering need not restart for docs. Audit request
deduplication, exact subject reuse, supersession, continuation, retry/cancel, publication
numbering, feedback, fingerprints, group/suppression lookup and reporting. Updating one unique
index is insufficient. In particular, update the failure-status newer-run predicates and
suppression functions in `postgres/review_runs.py`, current publication lookup in
`postgres/feedback.py`, current findings in `review_finding_application.py`, and the
latest-run/publication projections in `postgres/admin_reporting.py`, as well as publication and
quality owners. Preserve existing code fingerprints and public reference numbers while
namespacing new docs identity. A docs run cannot supersede, publish, suppress or resolve
feedback on a code finding. Closure can intentionally cancel both purposes.

At most one active logical run and one current posted result exist per `(PR, purpose)`. Physical
execution remains sequential per repository under the current repository, connection and team
limits. Queue priority cannot preempt an in-flight review; an explicit request can wait behind
one running docs assessment. Duplicate delivery identity is separate from logical
`(installation/repository, PR, purpose, target base, head, policy/review-contract)` identity and
terminal attempt identity. New base with the same head requires reassessment.

Documentation findings require a typed evidence contract: stable rule/identity, priority, reader
consequence, changed behavior or changed claim, exact documentation evidence or a verified
missing-document expectation, base/head comparison, disproof attempt, optional accepted decision
and a small proposed correction or decision request. Validate citations against registered
reads. A removed document uses its base evidence; unchanged documentation uses head evidence.
Never invent a changed-code anchor to fit the current recorder.

Reuse the existing finding identity, occurrence, decision/audit and publication tables with a
typed documentation input and small documentation finding validator/hash owner. Use the real
document path, line and heading for unchanged docs, base evidence for deletion, and a real
changed-source line plus area identity for a missing-document expectation. Store structured
evidence/provenance with the docs result and link occurrences to it. Preserve the code
recorder's changed-file eligibility. For docs, require a cited change that introduced or
worsened the discrepancy; do not equate that with a changed documentation line or manufacture a
true introduction flag. The engine computes the evidence hash from verified records, not a
model-supplied digest. Keep the existing stable PR-wide `F<n>` allocation across purposes; never
renumber old references or create a second F namespace. The docs prefix selects purpose before
calling the shared feedback parser and resolver. Existing code commands remain code-only and
reject a docs reference; docs commands likewise reject code references. Resolve feedback against
the current publication and its current occurrence; a reference alone grants no authority.
Suppression must match source and documentation evidence identities plus relevant policy/ADR
metadata. Include missing-file expectations and counterpart evidence so a later source or docs
edit invalidates stale feedback. No learning from silence, merge status or an accepted patch.

Use two managed procedures (`review-agent-pr` and proposed `review-agent-docs`) with explicit
worker routing from the persisted purpose. Include both in profile manifests, installation
receipts and contract hashes. Keep shared trusted invariants in the existing profile owner;
repository context never selects a skill, model or toolset.

## Check publication and automatic admission

Use the existing recoverable publisher with an explicit documentation-check output contract.
Current comment partitioning and recovery do not automatically implement the Checks API. Add a
`check_run` publication part and its validated payload/decoder, part-type constraint, gateway
create/update/list operations and publication allowlist. Require a check part for docs rather
than satisfying code's mandatory summary-comment contract with a dummy comment. Keep immutable
payload planning, fenced leases, durable provider IDs and exact-run provenance; add check
create/update/reconciliation in the GitHub writer owner. Do not route writes through the model
or build a general output-plugin framework.

Suggested check conclusions are `skipped` for deterministic exclusion, `success` for completed
reviewed scope without mismatches, `neutral` for advisory findings/setup problems/incomplete
evidence (including execution timeouts) and `cancelled` for superseded/cancelled work. Always
explain coverage. Invalid configuration can separately fail deterministic CI; neutral AI output
is not a merge barrier.

Create against the stored head SHA and keep prior attempts identifiable. Persist the provider
check ID; use an exact app-owned external marker for recovery, not as an assumed GitHub
uniqueness constraint. Revalidate authorization, generation and current refs before writes. A
ref change after that check is still possible: old output must remain attached to its old SHA,
lose current status when detected and never replace a newer current result. Recovery must
reconcile ambiguous creation before retrying. List by exact head with `check_name`, `app_id`,
`filter=all` and bounded pagination, then match the exact returned `external_id` and App
identity locally. The API has no external-id query filter or uniqueness guarantee. [GitHub's
Checks API](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference)
documents the available filters; reconciliation remains our responsibility. An incomplete scan
or ambiguous multiple matches is unresolved delivery, not permission to create again.

Keep V1 check output textual. Put the full report in the check when it fits the current provider
limit. If it does not, publish deterministic overflow report comments using the existing block
partitioner and marker recovery, and have the single check link to those acknowledged parts.
Model output cannot choose the destination; the frozen publication records the complete report,
partition plan and logical part references, and the publisher resolves returned IDs into links.
This preserves access for PR readers without a new report portal or append-only annotation
recovery system. Ordinary results need no PR comment. Do not silently discard findings; an
oversized unpublishable block is an explicit delivery limitation, retained internally, never a
successful complete report. Persist part acknowledgements so retry does not duplicate overflow
comments. Inline suggestions are optional; copyable corrections with exact expected revisions
are sufficient for V1.

Request `checks:write` and supported webhook events through the existing setup/doctor/install
reconciliation owner. Update registration metadata, doctor validation, normalized installation
permissions and persisted installation state together. Preserve the required code permission
baseline, allow only the documented extra capabilities, and diagnose manual/automatic docs
readiness separately; installations with partial approval remain usable for code. Recognize
supported event subsets during capability rollout instead of accepting arbitrary events.
Existing installations may require their owner to approve the added permission. The [Checks API
permission contract](https://docs.github.com/en/rest/checks/runs#create-a-check-run) requires
Checks write access. No permission change or live enablement is authorized by this design
document.

The same App admits semantic review; Actions must not independently launch it. Automatic
admission requires an open, ready, same-repository PR, explicit repository opt-in, valid access
and capability. V1 preserves the source gateway's existing rejection of forks, including manual
requests; adding fork source identity/access support is separate work.

Handle `opened`, `reopened`, `ready_for_review`, `synchronize`, base-changing `edited`,
`converted_to_draft`, `closed` and app-owned `check_run.rerequested`. Extend the live
`source_control.PullSnapshot` and its gateway codec with validated `draft` and target base-ref
fields; they are absent today. Unknown readiness is ineligible for automatic work. Refresh
current state instead of trusting event refs or a check payload's PR association. Ignore
title/body-only edits. Returning to draft stops automatic work; authorized manual draft
assessment remains allowed. Closure prevents publication. A check rerun must resolve the stored
PR/purpose and reauthorize the requester.

Coalesce automatic updates durably for approximately 120 seconds after the latest relevant
received update. Reuse webhook-delivery availability and leases for this delay, with an indexed
validated PR identity/purpose projection. Under the repository/PR serialization owner, keep the
newest pending automatic delivery as the wakeup and mark older pending deliveries coalesced. A
leased older delivery rechecks for a newer pending update before admission and defers to its
later deadline. Merely delaying each delivery by 120 seconds is insufficient. Draft/close
control events are handled promptly. Preserve delivery payloads as immutable audit evidence;
scheduling/terminal state may change. The current admission expiry is 24 hours, longer than the
debounce; validate that relationship when settings change. Reuse current jobs, priority and
retries after admission; no in-memory timer or new queue service. Carry trigger provenance so
automatic cancellation does not cancel authorized manual work merely because a PR is a draft.
Manual requests bypass debounce and can promote an equivalent queued automatic request.
Coalescing changes scheduling/request state, never an admitted immutable subject. A new snapshot
supersedes only its own purpose. Keep request keys tied to the actual
delivery/comment/check-rerun request. Separately reuse equivalent active work, and permit
automatic reuse only of a valid current completed result for the identical subject/contract. A
permanent subject-hash request key must not make a reopened PR reuse a cancelled attempt or make
an explicit rerun impossible. Reordered events always use freshly resolved PR state.

Give explicit requests preference through the current scheduler while preserving team fairness,
aging and model limits. The execution owner is a deterministic documentation preflight in
`ReviewWorker`, after the exact contract/lease checks and before Hermes or any model-execution
record. Factor reusable source initialization out of `review_source_tools.review_begin` through
the existing source/run owners, rather than invoke the tool through the model. It freezes
inventory/policy/scope, then either prepares a terminal docs check or continues with the docs
procedure. Terminal zero-model outcomes still use the normal publishing/completion lifecycle. V1
uses the existing worker lease and may briefly occupy queue capacity for these reads; zero model
calls does not imply bypassing queue/account eligibility. This avoids a second privileged
execution path. Measure preflight queue delay before introducing separate capacity for it. The
kill switch must stop pending docs admission/execution and publication as defined by the same
authorization owner, while preserving history and code work.

V1 does not fan out reviews whenever a target branch advances. Results identify exact refs and
refresh on supported events/manual requests, with a final freshness check. A later required
check or merge-queue integration needs an explicit merge-candidate freshness contract.

## Verification, release and boundaries

Implementation tasks own focused tests at each changed contract and the repository's applicable
Ruff, strict Pyright, bundle and generated-API checks. Schema changes need both a fresh database
and upgrade of existing code-only records. Include concurrency, crash-after-check-create, late
publication, duplicate events, revocation, partial reads and code/docs feedback collisions where
those owners change. Do not turn each helper or hypothetical branch into a separate test
project.

After the deterministic implementation checks pass, evaluate the manual path in the pilot
qualification task on a small maintainer-labelled set of roughly 20–30 varied cases using the
existing evaluation/fixture infrastructure where available. Include true mismatches,
already-correct docs, docs-only false claims, accepted-intent conflicts, exclusions, unmapped
scope, historical debt, renames and incomplete evidence. Audit skipped/clean outcomes as well as
findings. Retain label rationale and exact inputs; report observed useful findings, false
alarms, misses, coverage and model use without claiming statistical accuracy from the sample.
Agree observed usefulness before enabling automatic admission.

Add a tested starter and update canonical setup, command, behavior-ownership and feedback
documentation when the capability exists. A planning document must not make proposed commands
look available. Deterministic required workflows should always reach a terminal result; put
path-conditioned work inside them. Add merge-group support only when required checks are
actually used with a merge queue. See [GitHub's required-check
guidance](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).

Release/deployment work is a separate authorized boundary after implementation. Drain old
workers before migrating and restarting purpose-aware workers where mixed-version processing is
unsupported. Test upgrade and disable/recovery behavior; do not assume an old binary can safely
process new docs rows. Keep additive history instead of reversing migrations containing real
review data. Existing execution-contract comparison rejects mismatched bundles before Hermes;
preserve and test that guard. It does not make a mixed fleet safe: old workers can still claim
and fail new work. The global docs switch remains off until all worker/profile instances are
compatible. A pilot requires explicit repository/operator enablement; planning does not alter
branch protection or production.

Release-candidate consistency, autonomous patch application, fork support, external
documentation systems, repository-wide recurring audits, cross-revision caching and a separate
dashboard product are outside this epic. Settings and monitoring within the existing console
are included as described above. A later release assessment should compare a trusted exact
candidate with the previous supported release before publication, using the candidate's accepted
policies. It must not fabricate a PR subject.

## Existing source to read

Pinned links below describe existing code at the planning baseline, not implemented
documentation-review APIs. Read the affected implementation, caller and existing tests before
editing; rebase the plan's owner references if another branch changes them.

| Concern | Existing sources |
| --- | --- |
| Ownership and accepted intent | [Behavior ownership](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/docs/BEHAVIOR_OWNERSHIP.md#L25), [Repository context](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/docs/REPOSITORY_CONTEXT.md), [Feedback and ADR lifecycle](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/docs/FEEDBACK_AND_DECISIONS.md) |
| Rules and offline setup | [Guidance schema](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/domain/repository_guidance.py#L55), [ADR glob contract](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/domain/repository_decisions.py#L221), [Offline validator](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/repository_context_validation.py#L165), [Admin CLI](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/tools/review_agent_admin.py#L897) |
| Purpose and database identity | [Review subject](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/domain/review.py#L100), [Run admission and failure delivery](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres/review_runs.py#L911), [Initial constraints](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres_migrations/001_initial.sql#L166), [Migration tests](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/tests/test_postgres_migrations.py) |
| Delivery and scheduling | [Webhook normalization](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/github_webhook.py#L250), [App processor](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/github/app_processor.py#L396), [Durable deliveries](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres/webhook_deliveries.py#L83), [Job claims and fairness](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres/jobs.py#L484) |
| Exact evidence and snapshots | [Live PR snapshot](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/source_control.py#L368), [Source initialization](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/review_source_tools.py#L258), [Exact base reader](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/repository_base_files.py#L81), [ADR snapshots](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/repository_decision_context.py#L374) |
| Finding and feedback contracts | [Finding recorder](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/review_finding_application.py#L126), [Domain finding eligibility](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/domain/finding.py#L379), [Current publication feedback](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres/feedback.py#L117), [Feedback syntax](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/feedback_commands.py#L91) |
| Check delivery extension | [Publication payload contract](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/domain/publication.py#L268), [Publication and recovery](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/review_publication_application.py#L95), [GitHub writer protocol](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/github/publication.py#L82), [Comment partitioner](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/publication_partition.py#L136) |
| Worker and trusted procedures | [Worker dispatch](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/worker.py#L130), [Execution-contract rejection](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/worker.py#L429), [Immutable reviewer contract](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/review_contract.py#L697), [Current managed procedure](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/profiles/default-standard/skills/review-agent-pr/SKILL.md) |
| Operator capabilities | [Registration and doctor](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/operator_setup.py#L193), [Installation authorization](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres/github_app.py#L785), [Installation permission schema](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres_migrations/006_github_app_installations.sql), [Doctor tests](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/tests/test_operator_admin.py) |
| Reporting and verification | [Latest-run reporting](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres/admin_reporting.py), [Quality reporting](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/bootstrap/plugins/review_agent_tools/postgres/quality_reporting.py), [Replay tests](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/tests/test_review_replay.py), [Repository checks](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/scripts/check_bundle.sh) |
| Documentation and release boundaries | [Documentation CI](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/.github/workflows/docs-check.yml), [Documentation checks](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/scripts/check_docs.py), [Release image workflow](https://github.com/CCimen/review-agent/blob/d2554c4d75083227a3d8672ba5bb8f21b3d04b52/.github/workflows/release-image.yml) |

Admin planning was checked against `c2b3720d2abe712da00f5072d010b0b55a4036e9`. Relevant existing owners: [Team UI and scope](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/admin/src/teams.tsx), [Repository access UI](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/admin/src/access.tsx), [Console design](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/admin/DESIGN.md), [Team/repository authorization](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/bootstrap/plugins/review_agent_tools/postgres/team_access.py), [Team model-policy precedent](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/bootstrap/plugins/review_agent_tools/postgres/model_connections.py), [Ownership and transfer](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/bootstrap/plugins/review_agent_tools/postgres/repository_requests.py), [Global Settings permissions](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/bootstrap/plugins/review_agent_tools/admin_settings_api.py), [Usage scope and permissions](https://github.com/CCimen/review-agent/blob/c2b3720d2abe712da00f5072d010b0b55a4036e9/bootstrap/plugins/review_agent_tools/admin_api.py).
