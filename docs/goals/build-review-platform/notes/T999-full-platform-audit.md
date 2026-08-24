# T999 — Full platform goal audit

## Decision

`not_complete`

The completed work is coherent and follows the corrected architecture through
PostgreSQL publication planning and delivery persistence, but the packaged runtime still writes
Review Agent application state to SQLite. The remaining core path is finite and
ordered: PostgreSQL feedback, one clean runtime/Compose cutover, recovery proof,
SQLite deletion, durable jobs, and a transactional publication outbox.

## Requirement evidence

| Requirement | Evidence | Decision |
| --- | --- | --- |
| Public documentation and GitHub Pages | T001–T004; live Pages deployment and exact public-document allowlist | Complete |
| Maintainable owners before persistence change | T005–T015; typed settings, concrete GitHub reader, application owners, profile bundle, and publication split | Complete |
| Product-neutral identity and operator customization | T090; `Review Agent` is the product, `sundsvall-standard` is a profile, and one validated bundle owns SOUL, rules, presentation, and reviewed skills | Complete |
| Model-era capacity limits | T091; continuation replaces total-depth caps and retained provider/resource bounds are explicit | Complete |
| Correct PostgreSQL first-write contract | T018–T022; schema, checksum migrations, bounded pool, readiness, and migration health | Complete |
| PostgreSQL review lifecycle and coverage | T023–T024; registry, immutable subjects, idempotent runs, changed files, and normalized reads | Complete |
| PostgreSQL findings and governance | T025–T027; rename-stable identity, occurrences, suggestions, decisions, verification, reconciliation, and coaching | Complete |
| PostgreSQL publication planning and delivery persistence | T028–T029; deterministic planning, exact payloads, short claim/ack transactions, and ambiguous-delivery recovery exist, while historical supersession rendering and failure-status operations still need PostgreSQL owners before cutover | Partial |
| PostgreSQL feedback | Schema tables exist, but `feedback_bridge.py` and `memory_feedback.py` still transact through SQLite; no PostgreSQL feedback application owner exists | Missing |
| PostgreSQL operational parity | Historical supersession rendering, failure-status comment record/list/clear, and the shipped operator inspection/decision/stale-run CLI remain SQLite-only | Missing |
| PostgreSQL runtime and Compose cutover | PostgreSQL operations are integration-only. `tools.py`, the active application paths, `compose.yaml`, and the feedback bridge still use `REVIEW_AGENT_DB` and the SQLite volume | Missing |
| Controlled review, backup, restore, and rollback proof | Component PostgreSQL tests exist, but no complete fresh-database review/feedback/backup/restore proof exists | Missing |
| SQLite deletion | SQLite modules, CLI, environment contract, volume, documentation, and implementation-detail tests remain | Missing |
| Durable jobs | No job table, atomic lease claim, heartbeat, reaper, retry/dead-letter lifecycle, supersession, or queue worker exists | Missing |
| Transactional publication outbox | Publication intent and external delivery are not joined by an outbox; no publisher worker owns replay and queue-age metrics | Missing |

## Scope decisions

- The repository owner confirmed there is no production deployment or valuable
  SQLite state. Therefore no importer, dual write, backend selector,
  compatibility layer, or SQLite rollback path is allowed.
- At the first PostgreSQL cutover, recovery uses a PostgreSQL backup/restore and
  redeployment of the same compatible revision. From the second PostgreSQL
  revision onward, rollback may also use the prior PostgreSQL-compatible image
  against the same database.
- The shipped operator CLI remains valuable for inspection, decisions, and
  stale-run recovery. Port its observable commands to existing PostgreSQL
  owners before cutover; do not keep a split-brain SQLite CLI or rebuild it as a
  new administration framework.
- The profile workflow delivered in T090 satisfies the current request for
  configurable SOUL, stable rules, presentation, language, and reviewed skills.
  Trusted per-repository policy overlays remain a separate deferred product
  capability; they are not a reason to introduce a policy engine now.
- GitHub App migration, Slack, Codex Security, scanners, artifact storage,
  repository policy overlays, and an administration UI remain explicitly
  deferred. They do not influence the current schema or completion path.
- Provider, protocol, storage, publication, and denial-of-service bounds remain
  fixed or narrowly operator-configurable as recorded by T091. They are not
  model reading-depth limits.

## Ordered completion path

1. Port feedback as one PostgreSQL transaction behind a concrete application
   owner, without a live backend switch.
2. Complete PostgreSQL operational parity for historical supersession,
   failure-status comments, and the existing operator CLI.
3. Switch the review and feedback runtimes plus Compose atomically to
   PostgreSQL-only application state.
4. Prove a fresh review, publication recovery, feedback, backup, restore, and
   first-cutover recovery against PostgreSQL.
5. Delete the unreachable SQLite application implementation and its operational
   contract.
6. Add the durable queue schema and atomic lease claim.
7. Add worker lifecycle and crash recovery.
8. Add supersession, fairness, fast request acknowledgement, and operator queue
   inspection.
9. Add the transactional publication outbox and publisher worker, then perform
   one final whole-goal audit.

The sequence deepens existing PostgreSQL and application owners, closes named
behavior gaps before the switch, and removes the temporary backend. It does not
create generic repository interfaces, a plugin platform, or infrastructure for
deferred features.
