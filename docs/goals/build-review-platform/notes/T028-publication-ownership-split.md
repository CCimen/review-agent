# T028 — Publication ownership split

## Outcome

The active SQLite publication behavior now has four concrete owners without a
visible behavior change:

- `review_publisher.py` composes settings and the concrete GitHub adapter;
- `review_publication_application.py` coordinates publication lifecycle,
  suggestions, supersession, recovery, and failure status;
- `publication_partition.py` deterministically builds exact comment parts;
- `github/publication.py` owns GitHub value types, HTTP, retry, parsing, and
  delivery errors.

`review_renderer.py`, public tool JSON, markers, native suggestions, recovery,
and the active SQLite lifecycle are unchanged. PostgreSQL publication
persistence remains the next slice.

Implementation revision:
`682736e7c43ce222c3fccbb4da997e1966b6df8b`.

## Verification

- 141 affected publication, failure-status, tool, suggestion, ownership, and
  direct-application tests passed after the final correction.
- Strict Pyright passed with no diagnostics.
- The final full bundle before the mechanical import correction passed 569
  tests with 60 expected no-DSN skips; every affected suite was rerun after it.
- A subprocess test loads `review_publisher` through the installed top-level
  plugin path and posts a real persisted failure-status comment through a fake
  gateway.
- The exact parent `113705ca2a883da12ec7e668dcfe8547f74688f4`
  `review_publisher.py` blob was loaded beside the candidate. Independent
  identical SQLite fixtures produced a forced three-part publication followed
  by new-head supersession. Result JSON and every created, updated, deleted, and
  native-review payload were byte-identical. The canonical comparison snapshot
  digest was
  `2fc1c734131260060c4fbd8118c709c2885039002bd47b9dce09380c6a1d17a4`.
- Claude Opus/high session `review-agent-t028-publication-ownership`, UUID
  `3fe3d43d-80be-41a4-9064-8b264e561757`, moved from score 7 to green at score
  8 after the direct-import, canonical-import, and parent-comparison evidence.
- Live Python bundle run `32705578215` passed. Publish documentation run
  `32705578272` built and deployed GitHub Pages. The hosted roadmap states that
  publication ownership is separated while PostgreSQL remains undeployed.

## Carry forward

Slice 3B must decide whether deterministic partition failures use a dedicated
publication-domain error vocabulary instead of the GitHub transport error. It
must also reassess the duplicated stale guards and failure-status lifecycle
after PostgreSQL preparation/claim ownership makes the surviving shape clear.
Do not move the same lifecycle twice merely to reduce the current line count.

The historical truncation notice currently preserves a literal `\\n` from the
pre-split behavior. Correct it only in a separate visible-output change with an
exact assertion. GitHub read-client consolidation also remains separate.

The repository owner additionally directed that the public product become the
general “Review Agent,” with `sundsvall-standard` retained as the initial
Sundsvalls kommun profile. A later bounded product/profile task must make
identity, language, presentation, rules, and reviewed skills straightforward
for an operator to customize through Hermes-native profile files while keeping
security and deterministic lifecycle invariants non-configurable. Do not turn
that task into a plugin framework or broad administration system.
