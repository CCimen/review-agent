---
sidebar_label: Behavior ownership
slug: /behavior-ownership
title: Behavior ownership
description: Where reviewer identity, policy, procedure, wiring, and repository onboarding belong.
status: current
last_verified: 2026-09-02
---

# Behavior ownership

> **Current**: The deployment owns the neutral identity and fixed review
> contract. Repositories may add bounded instructions, ordered technical
> context, and typed decisions from the exact base commit.

The current deployment has one selected reviewer profile per environment.
Change the canonical owner of a concern instead of adding repository-name
conditionals or copying policy into runtime code.

| Concern | Current owner | Change it when |
| --- | --- | --- |
| Identity, tone, and evidence posture | `bootstrap/profiles/default-standard/SOUL.md` | The reviewer's deployment-wide voice or stance changes. |
| Review rules and visible contract | `bootstrap/profiles/default-standard/workspace/AGENTS.md` | Scope, severity, coverage, finding, suggestion, or feedback rules change. |
| Review procedure and tool sequence | `bootstrap/profiles/default-standard/skills/review-agent-pr/SKILL.md` | The two-pass workflow or tool-call procedure changes. |
| Provider, model, and reasoning effort | Deployment environment rendered into `bootstrap/config.yaml` | The inference route, model, or review depth changes. |
| Toolset, route, and prompt wiring | `bootstrap/config.yaml` | Hermes runtime wiring changes. |
| Deterministic reads, state, rendering, and publication | `bootstrap/plugins/review_agent_tools/` | A runtime invariant or external boundary changes. |
| Durable job execution | `review_agent_tools.worker` and `review_agent_tools.postgres.jobs` | Claim, heartbeat, retry, or exact-run continuation behavior changes. |
| Deployment and environment wiring | `compose.yaml` and `.env.example` | Container topology or supported configuration changes. |
| Repository trigger contract | `review_agent_tools.github_webhook` and `review_agent_tools.admission` | Signed GitHub App event or command behavior changes. |
| Repository instructions and ordered context | `.review-agent/config.toml`, `.review-agent/instructions.md`, and indexed `.review-agent/context/**/*.md` | A team changes repository-specific review focus, technical facts, or communication preferences. |
| Immutable repository guidance | `review_agent_tools.repository_guidance_context` and `review_guidance_snapshots` | Exact-base loading, bounds, degradation, hashing, or run provenance changes. |
| Repository decision format and matching | `.review-agent/decisions.toml`, typed ADR headers, and `review_agent_tools.domain.repository_decisions` | A repository maps an accepted invariant to different paths or the shared typed contract changes. |
| Immutable decision evidence | `review_agent_tools.repository_decision_context` and `review_decision_snapshots` | Loading, degradation, hashing, or run provenance changes. |

## Selecting a deployment profile

Set `REVIEW_AGENT_PROFILE` to a trusted directory key under
`bootstrap/profiles`. The default is `default-standard`. The installer records
the selected profile and its reviewed skill list in `HERMES_HOME`. Outside the
packaged Compose deployment, it reuses that receipt when no explicit value is
supplied. Compose always injects `REVIEW_AGENT_PROFILE`, so deployed selection
remains explicit. Skills recorded as managed by the previous selector are
removed when the new bundle does not list them.

Each profile contains:

- `SOUL.md` for identity, tone, and explanatory language;
- `workspace/AGENTS.md` for stable review rules and presentation;
- `profile.json` for the explicit reviewed-skill keys; and
- one directory under `skills/` for each listed key.

Every profile must include the skills named by the managed webhook review route,
currently `review-agent-pr`. Additional skill files are trusted, code-reviewed
profile content. The installer validates their keys and presence; it does not
interpret or authorize their prose or front matter.

The machine-validated manifest deliberately has no model, provider, route,
tool, authorization, snapshot, persistence, marker, or lifecycle settings.
The deployment selects the provider, model, and reasoning effort; other runtime
invariants remain fixed in managed configuration and deterministic code. Fixed
GitHub headings and hidden markers are not free-form profile templates.

## Changing `SOUL.md`

`SOUL.md` owns the neutral reviewer identity: evidence-first tone, constructive
language, and the stance taken when explaining risk and remediation. A change
affects every repository using that deployed profile, so review it as a
product-policy change. Teams use repository `instructions.md` for local
communication preferences rather than replacing Hermes' deployment-wide soul.

This follows Hermes' native [personality and `SOUL.md` ownership](https://hermes-agent.nousresearch.com/docs/user-guide/features/personality)
and its [deployment guide for a custom soul](https://hermes-agent.nousresearch.com/docs/guides/use-soul-with-hermes).

## Changing review rules

Use `AGENTS.md` for stable review invariants and the visible comment contract.
Use the review skill for procedural sequencing. Keep authorization, snapshot
validation, persistence, and publication rules in deterministic code rather
than relying on prose alone.

## Repository-specific context

Repositories share the deployment's neutral profile and deterministic safety
contract. Each repository may opt into one explicit `.review-agent/` package
for local engineering principles, ordered platform context, and accepted
decisions. The runtime reads it from the exact base commit and stores immutable
run snapshots; current pull-request content cannot rewrite its own review.

[Repository context](./REPOSITORY_CONTEXT.md) owns the copyable package,
selection order, validation command, capacity boundary, and trust rules.
[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md) owns typed ADR and
feedback semantics. Keep repository-specific behavior out of engine branches
and repository-name conditionals.
