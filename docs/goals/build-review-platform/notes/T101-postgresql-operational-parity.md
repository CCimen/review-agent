# T101 — PostgreSQL operational parity

## Outcome

PostgreSQL now owns every durable operation needed before the atomic runtime
cutover: historical publication supersession, failure-status comment state,
finding inspection and decisions, run recovery and statistics, publication and
coverage inspection, verifier input, coach receipts, and repository export.

Revision `e9042e5a33f0ac57dfab82c286188aef6452c09d` completed publication
and failure-status parity. Revision
`a7de8fa03742d00babda8265ed65f881aac1ea01` completed the bounded operator
and reporting surface. The packaged CLI still uses SQLite; T102 switches all
live callers and Compose together.

## Ownership and reliability

- Concrete PostgreSQL publication and review-run modules own historical
  supersession, missing-comment failure recording, and status-comment state.
- One operator application boundary owns short transactions and input
  validation; concrete reporting, decision, coverage, verification, and
  coaching modules own their SQL and read models.
- Decision targets are explicit by occurrence id, PR-local finding reference,
  or an explicitly requested latest occurrence. No branch guesses between
  these modes.
- Repository exports require an explicit positive per-table row budget, run in
  one repeatable-read read-only snapshot, and expose version 16 completeness
  through `complete` and `truncated_tables`. There is no hidden review-depth
  ceiling.
- Stale recovery updates runs and unfinished publication state atomically from
  the database clock. GitHub reads and writes remain outside transactions.
- Verifier metadata reads the PostgreSQL migration ledger rather than coupling
  database schema identity to the repository-export format.

## Deliberate deletion boundary

No SQLite importer, dual write, backend selector, compatibility schema, or
migration layer was added. The product has not shipped and has no production
SQLite state to preserve. T102 performs the one PostgreSQL-only live cutover;
T104 then deletes the retired SQLite application store and tests.

## Verification

- Real PostgreSQL 17 schema contract: 101 tests passed.
- Strict Pyright: zero errors, warnings, or information messages.
- Canonical bundle: 616 tests passed with 87 expected no-database skips.
- Documentation contract: 24 tests passed.
- Claude Opus/high sessions `review-agent-t101-publication-parity` and
  `review-agent-t101-operator-parity` both reached green at score 8. The latter
  closed export-completeness, canonical-decision-owner, cross-PR target, and
  repeatable-read proof gaps before commit.
- Exact-commit GitHub workflows passed: Python bundle run `32733756788` and
  documentation build/deploy run `32733756671`. GitHub Pages deployed the
  updated roadmap from `a7de8fa03742d00babda8265ed65f881aac1ea01`.

## Carry forward

T102 must teach any version-16 export consumer to reject `complete: false` in
the same change that enables the version; merely adding 16 to an accepted
version set would be unsafe. It must preserve short transaction ownership and
switch review, publication, feedback, operator, and Compose callers atomically,
without a SQLite fallback or rollback window.
