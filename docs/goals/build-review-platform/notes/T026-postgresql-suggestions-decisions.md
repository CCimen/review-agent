# PostgreSQL suggestions and decisions receipt

## Outcome

PostgreSQL now preserves validated, exact-head optional suggestions and
context-matched human decisions behind the existing finding application owner.
Suggestion selection has one shared policy owner across SQLite and PostgreSQL.
The work remains integration-only: the active reviewer still uses SQLite.

## Published implementation

- Implementation revision: `4e1c5abc620d3f031512104d5dccc82936f56cde`
- Executable-mode correction: `062d53970a9a5bd260ec66ca4712729453887911`
- Parent: `13b72fb4aa70cd35a70e335bc47981a4fc5cc46d`
- Exact source paths:
  - `bootstrap/plugins/review_agent_tools/domain/finding.py`
  - `bootstrap/plugins/review_agent_tools/suggestion_validation.py`
  - `bootstrap/plugins/review_agent_tools/memory_suggestions.py`
  - `bootstrap/plugins/review_agent_tools/memory_decisions.py`
  - `bootstrap/plugins/review_agent_tools/postgres/findings.py`
  - `bootstrap/plugins/review_agent_tools/postgres/suggestions.py`
  - `bootstrap/plugins/review_agent_tools/postgres/decisions.py`
  - `bootstrap/plugins/review_agent_tools/review_finding_application.py`
  - `scripts/check_postgres_schema.sh`
  - `tests/test_postgres_suggestions_decisions.py`
  - `tests/test_review_suggestions.py`
  - `docs/ROADMAP.md`

The shared selector owns ranking, suppression, canonical reuse, bounded head
reads, overlap, high-risk omission, validation, and typed status outcomes.
PostgreSQL modules own short suggestion replacement and atomic decision-plus-audit
transactions. Findings commit first; optional storage failures never roll them
back. A failed context transaction preserves existing suggestions and cannot
silently bypass an active matching suppression.

No migration changed because the approved initial schema already had the required
tables, constraints, and indexes. No backend interface, dual write, fallback,
importer, publication caller, or runtime cutover was added.

## Reliability evidence

- Exact head bytes, expected text, changed-hunk range, deletion replacements,
  overlap, high-risk omission, and the 12-read cap are behavior-tested.
- Same-head reruns reuse canonical patch bytes; authoritative omission clears the
  stored row, while context-read, decision-read, or transaction-exit failures
  preserve it and report a typed storage failure.
- Latest decisions use durable append order. Suppression requires the exact
  occurrence context hash and an unexpired suppressive kind.
- Decision and audit insertion commits or rolls back together; duplicate audit
  evidence returns a typed conflict.

## Review and validation

- Claude Opus/high session `review-agent-t026-suggestions-decisions`, canonical
  UUID `a36ca0a2-a42c-4f9f-a388-e28bebd82a80`, converged to green at score 8 in
  iteration 5. The metadata-only executable-mode correction was green at score 8
  in the same session.
- 65 PostgreSQL 17 contract tests passed.
- Strict Pyright passed with zero diagnostics; the canonical bundle passed 562
  tests with 56 expected no-DSN skips.
- Documentation checks, TypeScript type-check, isolated production build, and all
  nine built routes passed.
- Live Python bundle run `32698350796` and Publish documentation run
  `32698129766` succeeded. The hosted roadmap publishes the PostgreSQL suggestion
  milestone and still identifies SQLite as the active runtime.

## Deliberate non-goals and follow-up

Verification, reconciliation, coaching, publication, feedback, tools, deployment,
settings, and the active SQLite path did not change. The next slice makes existing
provider-neutral verification, reconciliation, and coaching persistence available
on PostgreSQL without adding an analyzer framework or production caller.
