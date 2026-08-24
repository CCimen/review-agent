# T029 — PostgreSQL publication lifecycle

## Outcome

PostgreSQL now persists one immutable exact publication plan with relational
finding outcomes and typed delivery parts. Preparation is atomic. Delivery uses
short claim and acknowledgement transactions and releases the pool before every
GitHub call. Stored external IDs are authoritative; publication-scoped markers
recover a write when the process dies after GitHub succeeds.

The integration has no live tool or runtime caller. The active SQLite path is
unchanged and no backend interface, dual write, importer, or migration bridge
was introduced.

Implementation revision:
`204c2626c305201414b42fc6a9658400f6236151`.

## Reliability contract

- Only an acquired claim may perform ordinary delivery. Explicit crash recovery
  requires the caller to establish that the prior poster has stopped.
- A transient provider failure can reclaim the same plan and posts only the
  unfinished parts. A stale pull request terminalizes both publication and run.
- Completion supersedes the previous current publication atomically before the
  new publication enters the partial unique index.
- Suggestion recovery requires the exact publication key, author, head SHA,
  path, body, and line coordinates; recent issue comments are read newest first.
- Stored plans verify their Markdown and canonical payload hashes on read while
  decoding only the stable versioned delivery schema.

## Capacity decision

The SHA-256 and local-reference regular expressions remain protocol validators,
not model or source-reading limits. The duplicate 128 KiB domain cap and the
arbitrary 4,000-character publication-evidence cap were deleted. The initial SQL
schema retains one 128 KiB structured-provider-request storage guard with an
explicit comment; it is not a code-reading or model-context ceiling.

A separate queued task audits actual source, enumeration, suggestion, provider,
and resource-safety ceilings. It must remove model-era assumptions without
turning denial-of-service protection or external API contracts into unbounded
behavior.

## Verification

- Real PostgreSQL 17 schema and integration contract: 83 tests passed.
- Strict Pyright: no diagnostics.
- Full bundle: 584 tests passed with 71 expected no-database skips; replay and
  YAML checks passed.
- Active SQLite publication, failure-status, and module-boundary suites: 59
  tests passed after the PostgreSQL integration changes.
- Public documentation contract and website typecheck passed.
- Claude Opus/high session `review-agent-t029-postgresql-publications`, UUID
  `92fe6e53-5c85-4375-a8b3-c01c5981f2c4`, found material recovery and
  supersession defects in its first pass and gave the corrected candidate green
  at score 8 in the single resumed verification pass.

## Carry forward

The runtime/deployment owner must retry `publish_failed`, reclaim `posting` only
after the prior poster has stopped, and later reap abandoned publishing runs.
Before cutover, PostgreSQL delivery must render the historical superseded form;
the active SQLite owner retains that visible behavior until deletion.

Product-neutral naming and the bounded Hermes-native deployment profile are the
next task. The operational-capacity audit follows it before the final goal
audit.
