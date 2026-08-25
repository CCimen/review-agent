# Goal Maker Handoff

`state.yaml` is authoritative.

## Current state

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- T102-T104 are complete at `4474d71fce7c970c4f9577931e8f926fe2d51219`.
  PostgreSQL application persistence and controlled backup/restore are proven.
- T105 is complete at `4042ddf0d03fec8d538241775caeb0e3aead12bc`.
  Durable jobs are one-to-one extensions of run-owned acceptance identity, with
  atomic fenced claim, queued supersession, and recoverable lease history.
- T106 is complete at `a73b6a2ee265231712bb912784f5392c2a9aff3a`.
  Exact heartbeat, retry/dead-letter recovery, and two-way run/job reconciliation
  are proven.
- T110 is complete at `aabbb7bda0851b688620dcec3044502f58802a1f`.
  The serial worker has exact-run continuation, isolated heartbeats, bounded
  retry, graceful stop, and pinned Hermes generation-fence proof.
- T107 is complete at `f9dc0f6eed0097814335d4e02753b018d3359460`.
  Signed admission atomically creates runs/jobs; fair workers, operator controls,
  private runtime networking, Compose-based deployment, and arbitrary-UID
  OpenShift are active and documented.
- T108 is complete at `007e1d7ddb8695650a7df3212395b32f7455a93f`.
  Immutable publication intent and exact stored parts are delivered by a
  recoverable, generation-fenced publisher.
- T109 audited the complete platform and found one direct completion blocker:
  the model-facing delivery schema and Security guide still claimed that
  `review_agent_deliver` performs synchronous GitHub publication.
- T111 is complete at `d37c02c2c1ea790548ffcd0a4484e9fdf1dfd1a7`.
  The model-facing schema and Security guide now describe the recoverable
  publisher handoff; Claude iteration 4 was green at score 8.
- T112 is the sole active task: finish the public documentation experience with
  task-oriented navigation, local static search, readable wide content, and
  completed-platform copy in the homepage, README, and capabilities page.
- Public identity is “Review Agent.” `sundsvall-standard` remains a selectable
  municipal profile. PostgreSQL is the only application persistence contract.

## T112 execution boundary

- Reuse Docusaurus and the current civic Read-mode tokens. Improve information
  hierarchy without replacing the site shell or visual identity.
- Add one build-time local search owner limited to the ten public documents. Do
  not require a hosted crawler, credentials, analytics, or AI search.
- Keep prose, tables, diagrams, keyboard focus, narrow layouts, and dark mode
  readable. Update only stale or needlessly long public copy.
- Keep future integrations as optional boundaries. Do not present completed
  durable jobs or publication delivery as roadmap work.

## Remaining order

T112 documentation finish → fresh final read-only audit.
GitHub App, repository policy overlays, Slack, scanners, Codex Security,
feedback-sidecar packaging, and evidence-based OpenShift resource defaults are
deferred.

## Verification continuity

- T105: PostgreSQL 116 tests, strict Pyright, 316-test bundle, final green 8.
- T106: PostgreSQL 123 tests, strict Pyright, 323-test bundle, final green 8.
- T110: PostgreSQL 125 tests, strict Pyright, 334-test bundle, fresh image and
  pinned Hermes adapter checks, final green 8; GitHub CI/Pages passed.
- T107: PostgreSQL 129 tests; strict Pyright and 347-test bundle; fresh image,
  admission/worker/Hermes/arbitrary-UID checks; Compose isolation; 14-object
  OpenShift parse; ten-document contract and Docusaurus build. Claude session
  `review-agent-t107-durable-admission` converged from 6 to green 8.
- T107 exact-commit Python/image run `32769601380` and Pages run `32769601428`
  passed.
- Preserve user-owned `refactor-plan1.md`.
- Stop all Codex and Claude work by 23:50 Europe/Stockholm; resume at 06:00.
