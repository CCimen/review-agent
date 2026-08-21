---
sidebar_label: Behavior ownership
slug: /behavior-ownership
title: Behavior ownership
description: Where reviewer identity, policy, procedure, wiring, and repository onboarding belong.
status: current
last_verified: 2026-08-21
---

# Behavior ownership

> **Current with a planned extension** — The deployment-wide owners below are
> current. Trusted base-branch repository configuration is planned and is
> labeled separately.

The current deployment has one municipal reviewer profile per environment.
Change the canonical owner of a concern instead of adding repository-name
conditionals or copying policy into runtime code.

| Concern | Current owner | Change it when |
| --- | --- | --- |
| Identity, tone, and evidence posture | `bootstrap/profiles/sundsvall-standard/SOUL.md` | The reviewer's deployment-wide voice or stance changes. |
| Review rules and visible contract | `bootstrap/profiles/sundsvall-standard/workspace/AGENTS.md` | Scope, severity, coverage, finding, suggestion, or feedback rules change. |
| Review procedure and tool sequence | `bootstrap/profiles/sundsvall-standard/skills/review-agent-pr/SKILL.md` | The two-pass workflow or tool-call procedure changes. |
| Model, toolset, route, and prompt wiring | `bootstrap/config.yaml` | Hermes runtime wiring changes. |
| Deterministic reads, state, rendering, and publication | `bootstrap/plugins/review_agent_tools/` | A runtime invariant or external boundary changes. |
| Deployment and environment wiring | `compose.yaml` and `.env.example` | Container topology or supported configuration changes. |
| Repository trigger contract | `examples/github/ai-review-request.yml` | Trusted GitHub request behavior changes. |

## Changing `SOUL.md`

`SOUL.md` owns the global reviewer identity: evidence-first tone, constructive
language, and the stance taken when explaining risk and remediation. It is not
a repository customization file. A change affects every repository using that
deployed profile, so review it as a product-policy change.

## Changing review rules

Use `AGENTS.md` for stable review invariants and the visible comment contract.
Use the review skill for procedural sequencing. Keep authorization, snapshot
validation, persistence, and publication rules in deterministic code rather
than relying on prose alone.

## Repository-specific context

Today, repositories share the same deployment-wide profile. The current
onboarding mechanism is the explicit allowlist plus the protected GitHub
workflow described in [Getting started](./GETTING_STARTED.md).

Trusted base-branch repository configuration and `AGENTS.md` discovery are
planned. When implemented, that context will be read from the trusted base
snapshot, never from a pull-request head `SOUL.md`. Until then, do not simulate
overlays with repository-name branches in the engine.
