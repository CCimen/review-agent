---
sidebar_label: Behavior ownership
slug: /behavior-ownership
title: Behavior ownership
description: Where reviewer identity, policy, procedure, wiring, and repository onboarding belong.
status: current
last_verified: 2026-08-24
---

# Behavior ownership

> **Current**: Deployment profiles own reviewer behavior. Typed repository
> decisions add evidence from the exact base commit without changing that
> behavior.

The current deployment has one selected reviewer profile per environment.
Change the canonical owner of a concern instead of adding repository-name
conditionals or copying policy into runtime code.

| Concern | Current owner | Change it when |
| --- | --- | --- |
| Identity, tone, and evidence posture | `bootstrap/profiles/sundsvall-standard/SOUL.md` | The reviewer's deployment-wide voice or stance changes. |
| Review rules and visible contract | `bootstrap/profiles/sundsvall-standard/workspace/AGENTS.md` | Scope, severity, coverage, finding, suggestion, or feedback rules change. |
| Review procedure and tool sequence | `bootstrap/profiles/sundsvall-standard/skills/review-agent-pr/SKILL.md` | The two-pass workflow or tool-call procedure changes. |
| Provider, model, and reasoning effort | Deployment environment rendered into `bootstrap/config.yaml` | The inference route, model, or review depth changes. |
| Toolset, route, and prompt wiring | `bootstrap/config.yaml` | Hermes runtime wiring changes. |
| Deterministic reads, state, rendering, and publication | `bootstrap/plugins/review_agent_tools/` | A runtime invariant or external boundary changes. |
| Durable job execution | `review_agent_tools.worker` and `review_agent_tools.postgres.jobs` | Claim, heartbeat, retry, or exact-run continuation behavior changes. |
| Deployment and environment wiring | `compose.yaml` and `.env.example` | Container topology or supported configuration changes. |
| Repository trigger contract | `review_agent_tools.github_webhook` and `review_agent_tools.admission` | Signed GitHub App event or command behavior changes. |
| Repository decision format and matching | `.review-agent/decisions.toml`, typed ADR headers, and `review_agent_tools.domain.repository_decisions` | A repository maps an accepted invariant to different paths or the shared typed contract changes. |
| Immutable decision evidence | `review_agent_tools.repository_decision_context` and `review_decision_snapshots` | Loading, degradation, hashing, or run provenance changes. |

## Selecting a deployment profile

Set `REVIEW_AGENT_PROFILE` to a trusted directory key under
`bootstrap/profiles`. The default is `sundsvall-standard`. The installer records
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

`SOUL.md` owns the global reviewer identity: evidence-first tone, constructive
language, and the stance taken when explaining risk and remediation. It is not
a repository customization file. A change affects every repository using that
deployed profile, so review it as a product-policy change.

This follows Hermes' native [personality and `SOUL.md` ownership](https://hermes-agent.nousresearch.com/docs/user-guide/features/personality)
and its [deployment guide for a custom soul](https://hermes-agent.nousresearch.com/docs/guides/use-soul-with-hermes).

## Changing review rules

Use `AGENTS.md` for stable review invariants and the visible comment contract.
Use the review skill for procedural sequencing. Keep authorization, snapshot
validation, persistence, and publication rules in deterministic code rather
than relying on prose alone.

## Repository-specific context

Today, repositories share the same deployment-wide profile. The current
onboarding mechanism is selected-repository GitHub App installation followed by
explicit operator enablement, as described in [Getting
started](./GETTING_STARTED.md).

Typed repository decisions provide additive evidence from the exact base
snapshot. Repository files do not replace the deployment profile or change
review policy. [Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md)
defines the authoring and trust contract. Keep repository-specific policy out of
engine branches.
