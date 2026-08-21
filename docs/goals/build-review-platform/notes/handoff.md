# Goal Maker Handoff

This is a bounded conversational snapshot. `state.yaml` is authoritative.

## Current direction

- Work in `/Users/ccimen/Documents/ChatGPT/Review Agent` on `main`; direct commits and pushes are authorized.
- T014 is complete at `094e78ad67fd2fff3807a0d36af9a8313bcad9dc`: the run and finding application modules use their concrete persistence owners directly.
- T015 audited T014 and the external review. The application ownership is green; full Python CI is missing, and public sequencing is stale.
- T016 is active: add one read-only GitHub Actions adapter for `scripts/check_bundle.sh` and refresh current PostgreSQL migration sequencing without changing runtime behavior.

## Execution boundary

- The primary agent implements; subagents are read-only.
- Keep the process lean: proportional behavior-first and full validation, one skeptical peer gate at a stable candidate, one implementation commit, and one compact receipt/audit update where practical.
- PostgreSQL is the approved clean replacement: one database per environment.
  The owner confirmed no production deployment or persisted production review
  state. `goal.md` owns the no-legacy and recovery constraints; `docs/ROADMAP.md`
  owns the public sequence.
- Keep jobs, leases, and the outbox as later separate slices.
- Publication, trusted project context and policy, scanners/Codex Security,
  GitHub App, policy overlays, and feedback remain deferred.

## Continuity

- Successor task: `01a023e2-c60a-77b3-9857-23bb2fc3d6f4`.
- Previous task: `01a023ae-4213-7071-8cc7-50048392fe97`.
- Read `goal.md`, `state.yaml`, repository instructions, both approved plans,
  and the external review recorded by T015. Verify clean `main == origin/main`,
  then execute only the active task.
