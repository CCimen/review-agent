# T100 — Atomic PostgreSQL feedback

## Outcome

Revision `56fc865bcd25a8a4f826b4d28735ff421ed230fb` adds the missing
PostgreSQL feedback application boundary without switching the packaged SQLite
runtime. Feedback authorization and parsing finish before pool checkout, then
one short transaction owns the event claim, current publication/reference,
decision or quality signal, audit, and durable terminal outcome.

Duplicate events now return their committed outcome. The conflict-then-read
protocol pins and checks Read Committed isolation, and current publications are
locked against concurrent supersession. GitHub calls remain outside the
transaction and the later atomic cutover remains the only live switch.

## Ownership and simplification

- `domain/feedback.py` owns typed statuses, results, and webhook-boundary values.
- `postgres/feedback.py` owns concrete event, target, and quality-signal SQL.
- `review_feedback_application.py` owns the transaction and reuses the existing
  authorization, parser, decision, publication, and audit owners.
- No backend interface, selector, dual write, importer, callback port, or SQLite
  compatibility path was added.
- The final review deleted duplicate row dataclasses, a Python-side current-row
  filter, an unreachable persisted `stale` outcome, and a vacuous test assertion.

## Verification

- Real PostgreSQL 17 schema contract: 91 tests passed.
- Strict Pyright: zero errors, warnings, or information messages.
- Canonical bundle: 606 tests passed with 78 expected database-environment skips.
- Public docs contract: nine documents passed.
- Website typecheck and clean temporary Docusaurus production build passed.
- Claude session `review-agent-t100-postgresql-feedback`, UUID
  `d521ac61-b774-43cb-b4b7-fb7e1528b433`, moved from score 7 to green at score
  8 after the concurrency and isolation requirements were proven.

## Deliberate boundary

The deployed feedback bridge still uses SQLite. T101 completes the remaining
PostgreSQL operational equivalents, and T102 switches review, feedback,
operator, and Compose callers atomically. Repository-name validation
consolidation is real but crosses existing registry, coaching, and retiring
SQLite owners; it is not a correctness requirement for this transaction and
was not expanded into this slice.
