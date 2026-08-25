# Transactional publication delivery receipt

## Outcome

Publication preparation now creates a durable delivery intent in PostgreSQL in
the same transaction that hands the exact leased review job to publication.
The stored publication and its immutable parts are the outbox; no parallel
event table, broker, or second serializer was added.

A separately scalable publisher claims one generation-fenced lease, sends only
stored parts, heartbeats while GitHub is in flight, and persists retry,
recovery, terminal exhaustion, and completion state. Expired final attempts
fail the owning run instead of remaining stuck. Terminal or superseded runs
cannot begin new delivery, while an external write already in flight can still
be acknowledged without reviving the terminal run. All related writers use the
same run-then-publication lock order.

GitHub remains the only delivery sink. Existing direct-ID and hidden-marker
recovery resolves ambiguous writes, including process death after GitHub
success but before PostgreSQL acknowledgement, without producing duplicates.

## Deployment and operator surface

- Compose and OpenShift include one private publisher process that can scale
  independently from admission and review workers.
- The GitHub write token belongs to the publisher rather than Hermes.
- Queue age, attempt budget, recovery count, availability, and lease expiry are
  visible through the existing PostgreSQL reporting owner.
- The runtime and review lifecycle documentation use two readable high-level
  images; runtime settings remain live, searchable text.

## Verification

- PostgreSQL 17 schema, migration, concurrency, recovery, backup, and restore
  contract: 134 tests passed.
- Full unit and contract discovery: 352 tests passed; the PostgreSQL cases
  skipped there were executed by the dedicated contract above.
- Strict Pyright: zero errors or warnings.
- Documentation contract, Docusaurus typecheck and production build, Compose
  rendering, OpenShift YAML parsing, fresh container build, and publisher entry
  point passed.
- Claude session `review-agent-t108-publication-outbox` verified the final
  lock-order and recovery candidate green at minimum score 8 after two bounded
  correction passes.

Source revision: `007e1d7ddb8695650a7df3212395b32f7455a93f`.
