# Review Agent production assessment
Assessed 5 September 2026 against commit `a220980dd17b0ba47755a7578e5e264d9c7c8263` (runtime matches v0.2.0), the supplied Eneo review history, and the visible Dokploy deployment. The assessment was read-only. Its evidence and recommendations are a dated snapshot; implementation status lives exclusively in Beads. The later preservation task changes documentation and the Review Agent board only. Eneo is review-output evidence, not an implementation target.

## Continuing the work

The canonical improvement epic is `ra-quality-2026-09-614` in this repository's `.beads/` workspace (prefix `ra`). It owns phase membership, ROI order, dependencies, acceptance criteria and the next implementation slice. Existing closed issues and other repositories' boards are unchanged. Run these commands from the Review Agent checkout:

```bash
br show ra-quality-2026-09-614 --json
br ready --epic ra-quality-2026-09-614 --sort priority --json
```

The `roi-NN` labels order implementation recommendations within this assessment; numeric priority also accounts for material failure and time-sensitive operating risks. Dependencies express prerequisites, not arbitrary sequencing. Deferred product integrations require an explicit decision before implementation. No live deployment, model switch, release, repository activation or scan is authorized merely by creating an issue. The source links below point to the assessed commit so later implementation does not rewrite the evidence.

## Subsequent product direction: automatic improvements

Later on 5 September, the owner requested an improvement loop that automatically evaluates and applies routine improvements, because repeated manual application would limit adoption. The planned direction is one-time scope enablement, automatic bounded evidence/candidate collection, replay and LLM-assisted evaluation, then versioned canary promotion with monitoring and rollback. LLM approval contributes evidence; deterministic policy and measured checks control application. Uncertain results and changes outside the enabled scope are escalated or left unapplied. No method guarantees absence of every future regression.

This extends the quality-baseline work in Beads under `ra-quality-2026-09-614.2`. Beads owns the implementation slices and dependencies. The current runtime remains proposal-only; the recommendations below preserve the original assessment and do not describe automatic promotion as already implemented or enabled. No production automation or model invocation was started by recording this direction.

## Decision
The architecture is suitable for a centrally operated service serving 200+ repositories. The evidence supports preparing a five-repository advisory pilot; it does not yet support organization-wide production acceptance or a promise that reviews finish within an hour. Repository count is not the capacity driver: arrival rate, PR size, concurrent model sessions, provider limits, and queue recovery are.

Keep one Dokploy deployment initially. Keep PostgreSQL, the existing workers and gateway, Hermes, GitHub App authorization, and Git-managed repository guidance. Fix the specific defects below, establish operating evidence, and expand in stages. A new queue service, Kubernetes migration, broad RBAC product, or replacement agent framework is not justified by the evidence.

The intended operating model is one central operator, teams maintaining their own ADRs and .review-agent packages, about 30 minutes as the preferred review turnaround, and under an hour as the practical target. Daily demand remains unknown.

## What is already strong
- GitHub App installation tokens are the sole production GitHub credential path. The private gateway separates those credentials from Hermes and derives authority from durable jobs and leases.
- Signed webhook admission, current collaborator authorization, exact base/head subjects, immutable behavior snapshots, PostgreSQL transactions, fenced leases, recoverable publication, and stale-head checks have explicit owners.
- The live model receives bounded review tools. It cannot execute repository code, use a shell, grant itself authority, or directly publish arbitrary GitHub mutations.
- Review comments expose evidence, impact, and a proposed fix, use stable finding references, show changes between reviews, and disclose incomplete coverage.
- Repository instructions and ADRs are bounded and loaded from the exact base commit. PR authors cannot rewrite the rules used to assess that same PR.
- Feedback requires an authorized human and is tied to code/decision context. There is no uncontrolled automatic prompt learning.
- The repository has substantial contract and PostgreSQL verification, deployment guidance, image pinning, and release evidence machinery.

Primary source: [security boundaries](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/docs/SECURITY.md#L16), [behavior ownership](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/docs/BEHAVIOR_OWNERSHIP.md#L20).

## Confirmed weaknesses and proposed corrections

### 1. Queue saturation can silently terminate an accepted review request — first pilot blocker
Webhook admission records a delivery and returns acceptance. If the downstream review queue is full, the App processor treats that as a retryable delivery failure. Defaults allow three delivery attempts with a 30-second delay. If capacity is still unavailable on the third attempt, the delivery becomes failed and its normalized payload is removed. No review run exists, and this path sends no acknowledgement or terminal explanation.

I reproduced this against disposable PostgreSQL using the existing processor fixture: received → received → failed; failure review_queue_unavailable; zero review runs/jobs, zero public comments/reactions, payload absent. The reproduction accelerates the retry delay; production defaults imply roughly a minute plus processing time, far shorter than a normal review. GitHub will not automatically replay an accepted webhook for this application-level failure.

Correction: keep temporary capacity waiting separate from the delivery's processing-error attempt budget. The same catch currently includes ReviewJobBusy; distinguish that lock-contention error from ReviewQueueFull rather than exempting all retryable errors. Retain bounded, replayable intent until an explicit age/retention policy expires, and provide visible failure/retry status. Preserve authorization, head validation, deduplication, and bounded admission. Fix this in the existing App processor and webhook delivery lifecycle, without another queue. Expiry must still be explicit; an indefinitely growing waiting area would move the failure.

Acceptance: simulate capacity remaining full longer than normal service time, then release it; each accepted eligible request either becomes one review or reaches a visible terminal outcome. Restarting workers must not lose or duplicate it. Test sustained overload and actual database/backpressure failure separately.

Sources: [queue failure mapping](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/github/app_processor.py#L423), [terminal delivery handling](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/postgres/webhook_deliveries.py#L662), [defaults](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/admission.py#L43).

### 2. Duplicate root causes undermine review credibility — first product-quality fix
The supplied [Review 7](https://github.com/eneo-ai/eneo/pull/721#issuecomment-5543756971) counts F18, F21, and F22 as three high-severity findings, but their evidence, impact, and proposed correction describe the same tenant-admission problem. The distinct medium-severity finding is a different issue. This assessment concerns duplicate presentation; it does not independently adjudicate the Eneo code findings.

Current fingerprints hash model-supplied rule ID, path, symbol, and anchor. Exact duplicate hashes are rejected, but changed wording can create a new identity. A small probe confirmed identical evidence with an anchor wording variation is accepted as two identities.

Correction: start with a regression corpus containing the actual duplicates and distinct nearby defects, then strengthen reconciliation within a review and against existing findings at the current finding/history owners. The profile already says one root cause is one finding (workspace/AGENTS.md:161), so simply adding that sentence again is insufficient. Require one canonical published finding/count and preserve history continuity; choose the smallest implementation that passes the corpus before proposing schema changes, aliases, or semantic matching infrastructure. Define the equivalence evidence before selecting a matching technique. A wording-only fuzzy filter risks hiding independent defects. Renderer-only hiding would leave incorrect persisted counts and history.

Acceptance: the observed duplicate examples publish one root cause with one count, while two independent defects at the same location remain distinct. Corrections, reappearances, and human suppressions retain trustworthy history.

Sources: [identity](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/domain/finding.py#L321), [recording boundary](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/review_finding_application.py#L126).

### 3. Coverage accounting and repeated GitHub reads need correction
Review 7 reports complete textual diff exposure for 95 of 205 changed paths, with 19 truncated and one unavailable; it also read source context from 38 changed paths. All seven sampled review rounds report incomplete coverage. These counts describe tool exposure, not the percentage of behavior understood.

A confirmed accounting defect makes large-file pagination permanently conservative: every partial page marks the path truncated, including its final page. A six-page reproduction returned the entire diff but reported no completely exposed path. Therefore some incompleteness can be bookkeeping, while unread or provider-unavailable content remains a separate limitation.

The PR diff tool also fetches the full PR diff again on repeated calls; its fallback can enumerate all changed-file pages for each path/page. Source pagination fetches the whole file again before slicing. These are measured-code-path optimization candidates, not yet measured latency savings.

Correction: let existing coverage storage aggregate exact snapshot/path intervals and mark complete only when the union covers all bytes. Reuse bounded immutable content within an exact base/head/file-SHA scope, retaining fresh authorization and stale-head checks. Set byte/lifetime bounds and clear ownership; avoid a broad unbounded cache.

Acceptance: all pages read means complete exposure, overlapping or missing pages never imply completeness, and a changed head cannot reuse stale content. Measure provider request count, bytes, memory, and wall time on the same representative PRs.

Sources: [diff pagination](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/diff_render.py#L336), [PR reads](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/review_source_tools.py#L673), [file reads](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/github/source.py#L322).

### 4. Rate-limit recovery and operator visibility are incomplete
GitHub rate-limit responses are recognized, but response timing information is not preserved through all exception/retry boundaries. Several paths use fixed 30-second retries. This can exhaust attempts during a legitimate provider cooldown and becomes more significant with many repositories.

The current queue-health surface covers review and publication jobs, not the webhook backlog. Doctor checks capacity and expired leases, but cannot establish useful worker progress from oldest waiting age or phase throughput. Worker and publisher CLIs omit logging initialization for their INFO completion messages; their visible production logs were empty during inspection. Empty logs alone do not prove failed processing.

Correction: carry structured retry timing into the existing provider and scheduling boundaries, honor Retry-After/reset where available, and use bounded delayed scheduling. Expose all three queues, oldest eligible age, active leases, last progress, terminal failures, provider cooldown, publication delay, and correlation IDs. Configure ordinary structured lifecycle logs with redaction. Prefer existing health/CLI owners and an exporter before building a dashboard.

Acceptance: simulated provider cooldown survives until its permitted retry; permanent errors remain terminal. Queue age alerts detect stopped consumers even when HTTP health and PostgreSQL remain up. No credentials or raw PR/prompt content enter logs by default.

Sources: [queue-health contract](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/operator_application.py#L64), [doctor checks](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/bootstrap/plugins/review_agent_tools/operator_setup.py#L440), [worker entry point](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/tools/review_agent_worker.py#L1).

### 5. The deployed release has time-limited critical package exceptions
The inspected deployment manifest digest matches v0.2.0's IMAGE-DIGESTS.txt. Its 2 September release scan summary reports 18 critical package findings without vendor fixes, accepted under reviewed exceptions through 30 September 2026. These are package findings, not 18 distinct vulnerabilities. The documented reasons concern packages outside the managed execution path or architecture-specific applicability; this assessment did not independently prove every reachability assertion.

Before broader use, review those exceptions for this deployment, scan the exact candidate images with current vulnerability data, and assign an owner to refresh the image or reassess each exception before expiry. Consider removing unused inherited runtime packages when a measured, supported image reduction is practical. The exception policy is enforced by the release check; expiry does not automatically stop or update an already-running deployment. Preserve the existing no-shell boundary, especially when adding a future security scanner.

Sources: [release evidence](https://github.com/CCimen/review-agent/releases/tag/v0.2.0), [release scan summary](https://github.com/CCimen/review-agent/releases/download/v0.2.0/VULNERABILITY-SUMMARY.md), [exception policy](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/release-image-critical-exceptions.json#L4).

## Capacity and deployment
The supplied PR has seven request-to-comment observations, 45.32–67.03 minutes, median 50.70 minutes. Six finished within an hour. This is one large PR repeatedly reviewed, not a representative sample or an established percentile, and the timings combine waiting and execution.

With four execution slots, 30-minute reviews permit a theoretical eight completions/hour; 50.7-minute reviews permit about 4.7/hour. Both exclude retries, overhead and spare capacity. Plan well below saturation. Under an empty-queue, equal-duration illustration, ten simultaneous 50-minute reviews at four slots complete in roughly three waves, with the final wave around 150 minutes. An under-hour objective therefore depends on burst demand, not just repository count.

The queue permits one leased review per repository, protecting fairness but serializing busy repositories. Default active-review capacity is 100 and job timeout is two hours. I have not verified the live concurrency/model environment because values are redacted. Existing scale measurements exercise PostgreSQL claims, publication, intake, and a synthetic 3,000-file inventory; they do not demonstrate end-to-end model throughput across 200 repositories. [Existing scale evidence](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/benchmarks/production-scale.md#L26).

Visible Dokploy snapshot: seven containers running, no recorded restarts/OOM kills; four configured health checks passing; only admission publicly routed. Hermes has an 8 GiB limit and two CPUs, gateway 256 MiB; observed idle usage is not a sizing benchmark. PostgreSQL has no explicit container memory limit. One host remains a shared failure domain.

Retain one deployment. Instrument demand and measure peak memory/provider concurrency before increasing worker slots. Budget DB pools, gateway resources, and provider allowances together. Scale worker capacity within the deployment first; add isolation or separate installations only if measured quotas, load interference, or a real trust boundary require it. A larger repository inventory alone does not justify sharding.

I found no Dokploy backup or volume-backup schedule for this service. An external host-level backup may exist and was not verified. Before the five-repository pilot, establish a named backup owner, encrypted off-host PostgreSQL backups, agreed RPO/RTO, and a restore drill including App/provider credential recovery. A local test restore passed; this does not prove production recoverability.

## Developer experience and a premium product
The most valuable premium qualities here are trustworthy findings, a visible lifecycle, easy repository setup, and predictable recovery.

Keep the comment concise: a current root-cause count, new/resolved/remaining changes, clear scope/coverage, evidence and consequence per finding, and the next action. Preserve the collapsed coding-agent handoff, but remove duplicate findings and avoid repeating the same fix prose unnecessarily. Keep model confidence/scores internal; they are model assertions, not calibrated probabilities. Add queued/running/completed/failed status tied to the exact head, ideally through a consistent status/check surface, while preserving the final comment/history contract. Do not imply a clean security result after partial execution.

Build an evaluation set from human-adjudicated findings, known missed bugs, accepted ADR tradeoffs, and clean changes across several languages and PR sizes. Use the existing feedback/export machinery. Track root-cause precision, actionable findings, duplicate rate, false-positive recurrence, known-miss detection, turnaround, and incomplete reviews. Do not infer precision from silence or fix rate alone. Additional automatic model reviewers should require measured benefit; they add latency/cost and can share errors.

Team setup should remain Git-native:
- Copy one minimal .review-agent example, add concise project constraints and ordered technical context, and map relevant accepted ADRs.
- Validate in CI using the existing repository-context validation command.
- Use CODEOWNERS/normal PR review for guidance and ADR changes.
- Explain that new guidance applies after merge because reviews read the base commit.
- Explain that enabled=false disables this guidance package, not the repository's review activation.
- Show a concise loaded/missing/invalid guidance receipt. Optional-context failure currently allows ordinary review to continue; do not conceal that degraded state.

Avoid a remote rules registry, bespoke per-team deployments, or UI editing of duplicated rules. Current bounds (10 context files, 400 lines each, bounded matching ADRs) encourage focused guidance rather than a giant prompt. Sources: [repository context](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/docs/REPOSITORY_CONTEXT.md#L122), [feedback and decisions](https://github.com/CCimen/review-agent/blob/a220980dd17b0ba47755a7578e5e264d9c7c8263/docs/FEEDBACK_AND_DECISIONS.md#L22).

## Documentation and DevOps
The documentation is broad and the machine-readable install plan/doctor are valuable. The setup still crosses App registration, two database connection roles, a PEM mount, Dokploy deployment-command details, network configuration, provider authentication, and installer refresh/restart. Consolidate those steps into one tested Dokploy journey for the supported path: prerequisites → App → deployment → provider authentication → doctor → installation approval → one review → backup/restore → upgrade/rollback. Link specialist detail at the step that needs it. Reuse current scripts and validation rather than adding an installer framework.

Correct the statement that admission returns HTTP 429 when the active review queue reaches capacity; the current accepted-webhook behavior is different. Make the distinction between GitHub App access, application installation approval, repository activation, and repository guidance explicit. For the first five repositories, use selected-repository access and explicit inventory; later all-repositories access should be an operator choice with the supported automatic activation behavior understood. Removing guidance does not revoke access.

Publish an actionable preflight report: image/behavior contract, DB migration state, App identity and approved scope, provider reachability/authentication, queue progress, public/private route checks, backup age, and next action. Extend existing commands only where actual gaps exist. Keep secrets out of diagnostic bundles.

I validated the checked-in documentation contract and example package. The browser denied access to the public documentation site under an administrator URL policy, so I could not assess its rendered appearance or navigation. This is a verification limitation, not a discovered defect in the site.

## Admin panel and RBAC
A small operational view is useful after the underlying reporting gaps are fixed. For one operator, begin with existing CLI plus metrics/logs. When the five-repository pilot demonstrates repetitive operator questions, add one read-only page showing installation/repository state, three queues, running reviews, failures, provider cooldowns and links to relevant GitHub reviews and redacted logs.

Keep Dokploy responsible for deployments, logs and restarts; GitHub App settings for installation access; existing application commands for approval/activation; Git PRs for team rules. Do not copy those authorities into another settings database.

Put any panel behind organization SSO or a trusted authenticated proxy. Start with viewer access and, only when mutations are needed, a narrow operator role using existing application owners and audit records bound to authenticated identity. A user-supplied actor string is not an authorization system. Team self-service repository-scoped mutation and more granular RBAC can wait for an actual second operating role.

## GPT-6 Astra migration
Treat this as a measured behavior-contract rollout. The environment already owns model/provider/reasoning selection, and the worker checks the frozen contract. Drain queued/running work, change the selected model through the supported installer/config refresh, verify the effective model and tool behavior, then restart/admit new jobs. Preserve a rollback to the prior contract after draining.

The [official GPT-6 Astra API page](https://developers.openai.com/api/docs/models/gpt-6-astra) documents a large API context window; that does not establish support, entitlement, or the same limit through the pinned Hermes openai-codex authentication route. Bootstrap configuration still contains Sol-specific context/compression assumptions. Verify the actual Hermes integration before increasing tool output or context limits.

Compare Sol and Astra on the same frozen, adjudicated PR set at a deliberate reasoning setting. Measure precision, duplicates, missed bugs, tool failures, turnaround, and usage/cost where available. Do not assume the stronger model fixes queue lifecycle, duplicate identities, or accounting defects. The pilot promotion criterion is observed quality and acceptable operations.

## CI and future /security
Start CI in advisory mode. Trigger the existing durable PR workflow through a clearly authorized exact-head request, and report a Check/status that distinguishes queued, running, completed, incomplete, and failed. This requires an explicit trigger/publication contract: current support is manual /review on same-repository PRs, not a ready-made CI integration. Bound repeated pushes and cancel/obsolete stale work through the existing lifecycle. Fork support is a separate authorization/execution decision.

Use deterministic CI tools for formatting, typing, unused code, duplication, dependency vulnerabilities, and secrets. Let LLM review assess correctness, unnecessary complexity, misleading comments, missing behavior proof, and security implications. Avoid subjective AI-written-code detection as a gate; evaluate the resulting code. Only consider blocking high-confidence classes after human-labelled replay and pilot evidence establish acceptable false positives.

A first /security mode can focus the existing read-only PR review on changed authorization, tenant isolation, injection, secrets, unsafe I/O and similar issues, using the same deterministic authorization/persistence/publication owners. Avoid a duplicate reviewer lifecycle.

Repository-wide historical vulnerability scans need a distinct subject and evidence contract: their findings are not necessarily introduced by a PR. The [Codex Security CLI](https://learn.chatgpt.com/docs/security/cli) and [CI guidance](https://learn.chatgpt.com/docs/security/cli/ci) provide useful examples: report-only operation, bounded diff/path/full scopes, machine-readable findings/coverage/manifests and SARIF, cost controls, and root-cause history. If integrating an executable scanner, use a separate isolated job with narrowly scoped credentials, resource/time/network limits and retained partial/error artifacts. Do not grant shell access to the current Hermes reviewer. Code-scanning/SARIF availability for private repositories depends on organization entitlement. Treat installation/licensing/data handling as an integration decision, not assumed capability.

## Rollout decision retained from the assessment
The proposed operating direction is one deployment, beginning with five repositories and expanding toward roughly 20, 50 and eventually 200+ only after measured quality, recovery and capacity evidence. The desired turnaround is around 30 minutes, ideally below one hour. Demand remains unknown and the seven observations from one PR cannot establish a production percentile. Current gates and slice ordering are maintained in the Beads epic rather than duplicated here. Earlier code and architecture corrections have higher immediate ROI than expansion work.

## Validation and limits
- Bundle check passed: Ruff, strict Pyright, configuration checks; 715 tests discovered, 223 PostgreSQL-dependent tests skipped in this invocation.
- The separate pinned disposable PostgreSQL check passed 239 tests, migration through version 14 and backup/restore canaries. These test counts overlap in coverage and must not be added as unique tests.
- Documentation contract check passed for 15 documents; example repository guidance validated.
- Three targeted local probes confirmed terminal queue-pressure behavior, anchor-variant finding identities and fully read diff pages still marked truncated.
- Git diff/status were clean at assessment start. No production mutation, live review, code change or security scan was performed.
- Runtime inspection is a point-in-time deployment snapshot. Effective live model/provider quotas, sustained peak use, external backups, data-processing approval and production restore readiness remain unverified.


## Single peer review and disposition
Fable 5.1 at high effort reviewed the frozen assessment once. Model/session identity checks passed; it confirmed the main source mechanisms but requested a smaller first-pilot scope. No second iteration was run. This report incorporates locally verified corrections; the peer's original verdict was changes_required, not production approval.

- Accepted: exclude unmeasured caching, a new exporter, and speculative semantic infrastructure from the five-repository gate. Keep logging initialization, documentation correction and verified recovery in the initial operating work. Separate capacity waiting from ReviewJobBusy lock-contention retry semantics.
- Partly accepted: begin duplicate correction with the existing evidence and a small regression corpus. Rejected prompt-only assurance because the existing profile already contains the proposed one-root-cause rule; the live duplicate example shows that rule alone is insufficient. The three public findings share a path, but internal rule/symbol/anchor values were unavailable, so no specific normalization fix is asserted.
- Rejected: delaying queue-loss correction on the premise that five repositories/four execution slots cannot reach 100 active jobs. The limit counts queued, leased and publication-waiting jobs; repository count limits neither pending PR count nor backlog during an outage. Live capacity is unverified. Increasing the delivery attempt count alone only postpones terminal loss and is not the correction.
- Rejected source claim: the peer said later truncated exposure can overwrite complete coverage. postgres/coverage.py:611 explicitly rejects complete-to-incomplete transitions. The independently reproduced inability to accumulate paginated reads remains valid. No new table layout is prescribed before designing the smallest owner-aligned correction.
- Qualified: missing Compose backup automation is not proof that external backups do not exist. The report preserves that unknown. Release exception evidence was checked separately against the actual deployed manifest digest and added to the maintenance gates.

The single peer review used `claude-fable-5-1` at high effort, with read-only tools and no delegation. Model/session identity checks passed. Its advisory verdict was `changes_required`; the verified dispositions above preserve the material conclusions without presenting that review as production approval. Raw provider receipts remain private and are not required to resume from the Beads issues.
