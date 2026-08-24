# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T026 is live at implementation revision `4e1c5abc` plus executable-mode
  correction `062d539`. PostgreSQL suggestions and decisions are integration-only;
  the active reviewer still uses SQLite.
- T027 is active for provider-neutral PostgreSQL verification, reconciliation,
  and immutable coach-run candidate evidence. Follow Slice 2C of the approved
  local plan and the task's exact constraints; no analyzer framework or runtime
  caller belongs in this slice.

## Execution boundary

- The primary agent implements. Use the existing behavior owners and corrected
  initial schema before adding code; no generic repository, backend interface,
  dual write, fallback, or importer.
- Keep provider/model calls and artifact I/O outside short database transactions.
  Preserve typed failures, exact-run relationships, immutable evidence, and the
  active SQLite behavior.
- Keep publication delivery, feedback, tools, settings, Compose, deployment,
  runtime cutover, jobs/outbox, trusted project policy, scanners, and GitHub App
  work in later slices.
- Ponytail lite is active: build the approved slice, but call out a materially
  lazier existing-owner alternative before adding a new abstraction.

## Continuity

- Read `goal.md`, `state.yaml`, this handoff, `refactor-plan1.md` Slice 2C, and
  the active task's exact source paths before editing. Preserve the user-owned
  `refactor-plan1.md`.
- T026 evidence: 65 PostgreSQL tests, strict Pyright, 562 bundle tests, docs
  checks/build, live Python run `32698350796`, and docs run `32698129766` passed.
  Claude session `review-agent-t026-suggestions-decisions` (UUID
  `a36ca0a2-a42c-4f9f-a388-e28bebd82a80`) was green at score 8.
- Start one new resumable Claude Opus/high session for T027's stable commit gate;
  use the same session only for verified blocker follow-ups.
- All Codex and Claude work must stop by 23:50 Europe/Stockholm and may resume at
  06:00. Do not start a unit that risks crossing the stop boundary.
