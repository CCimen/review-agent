# T009 review-run application audit

## Decision

T008 is complete and behavior-preserving. Continue with the bounded finding-memory application slice recorded as T010.

## Evidence

- Main was clean at `2ab8f52` and matched `origin/main`; runtime code was unchanged from the delivered T008 revision `43afb8d`.
- `review_run_application.py` is the typed owner for exact-snapshot run lifecycle, terminalization, phases, changed-file inventory, and coverage.
- Database connections remain locally bounded. Snapshot loading closes its first connection before the GitHub callback and uses exactly two connections on success.
- `tools.py` no longer calls the moved run or coverage facade operations directly.
- The application module imports no source-control, publisher, settings, schema, URL, or JSON infrastructure.
- The focused 111-test suite passed with resource warnings promoted to errors; strict Pyright, `git diff --check`, clean status, and board validation also passed.
- Tests cover pre-network rejection, one-time stale-snapshot terminalization, retry after initial phase failure, coverage behavior, and module boundaries.
- No deferred scanner, security, PostgreSQL, jobs, GitHub App, policy-overlay, feedback, or publication capability entered the slice.

## Next owner slice

Move historical finding-context lookup, finding persistence, and optional atomic-suggestion selection from `tools.py` into one typed `review_finding_application.py` owner. Keep GitHub reads and public JSON/error serialization in the tool adapter, and preserve persistence and suggestion behavior exactly.

The exact allowed paths, validation commands, stop conditions, and non-goals are frozen in T010 on the board.
