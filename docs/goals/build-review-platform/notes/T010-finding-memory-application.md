# T010 finding-memory application owner

## Outcome

Historical finding context, trusted finding persistence, and optional atomic-suggestion selection now have one typed application owner in `review_finding_application.py`. The tool adapter retains public input validation, exact snapshot coordination, GitHub reads, and JSON/error serialization. Existing memory modules remain the persistence and validation owners.

## Ownership change

- Moved bounded historical context loading out of `tools.py`.
- Moved trusted blob-hash selection with exact-head fallback into the application owner.
- Moved finding recording, suppression-aware suggestion ordering, canonical same-head reuse, overlap rejection, the twelve-suggestion cap, and atomic suggestion replacement into the application owner.
- Kept one narrow decoded head-file callback so GitHub transport stays in the tool adapter.
- Removed `sqlite3` and `memory_suggestions` imports plus direct context and finding-record calls from `tools.py`.
- Added AST boundary checks against ownership regressions.

## Reliability correction

The first peer pass found that rejected candidates could bypass the successful-selection cap and load up to 200 distinct five-megabyte head files. The verified fix reuses the existing twelve-item safety limit for distinct head-file reads and cached contents. Same-path reuse remains available without another read, and lower-priority new paths use the existing `suggestion_review_limit` omission reason.

## Validation

- 155 focused tests passed with `ResourceWarning` promoted to errors.
- Strict Pyright passed with zero errors and warnings.
- The full bundle passed 486 tests; replay validation was skipped because no historical fixtures are tracked, and YAML checks passed.
- `git diff --check` passed.
- Skeptical Codex peer session `review-agent-t010-finding-memory` requested changes at score 8 in iteration 1, then returned green at score 8 with no findings in iteration 2.

## Delivery

Commit `19f73db15f2b5f8bb786e16c9cfcfcbcf5bc44bb` was pushed to `main`.
