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
- T108 is the sole active task: persist publication readiness and outbox intent
  atomically, then deliver exact stored parts through one recoverable publisher.
- Public identity is “Review Agent.” `sundsvall-standard` remains a selectable
  municipal profile. PostgreSQL is the only application persistence contract.

## T108 execution boundary

- Reuse the existing PostgreSQL publication plan, part, direct-ID, marker
  recovery, and deterministic GitHub publisher owners. Do not create another
  serializer or parallel publication path.
- Commit publication readiness and outbox intent in one transaction. Provider
  calls must happen after commit through a recoverable claim.
- Claim exact stored publication parts with a durable fence. Acknowledgement
  must be independently recoverable per part.
- Prove the ambiguous boundary: if GitHub succeeds and the process dies before
  database acknowledgement, replay must resolve the exact external object and
  must not create a duplicate.
- Add queue age, retry, failure, and recovery visibility in the existing
  operator/runtime owners. Keep GitHub as the sole delivery sink; no generic
  notification bus, Celery, ARQ, Redis, or broker.
- Update public operations/security/how-it-works docs only where externally
  visible behavior changes. Keep prose concise and diagrams code-native.

## Remaining order

T108 transactional publication outbox → T109 final read-only audit.
GitHub App, repository policy overlays, Slack, scanners, Codex Security,
feedback-sidecar packaging, and evidence-based OpenShift resource defaults are
deferred unless T108 directly requires them.

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
