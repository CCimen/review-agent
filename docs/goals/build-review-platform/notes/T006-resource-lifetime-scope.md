# Resource lifetime cleanup scope

## Decision

Proceed with one small cleanup slice. Tracemalloc reproduces one shared defect:
owned SQLite connections and caught HTTP error responses are not closed on all
paths. Splitting the fixes would add process overhead without separating risk.

## Canonical owners

- `memory_schema.verify_database_ready` owns its `connect_existing` connection.
- `tools._request` owns caught read-side `HTTPError` responses.
- `GitHubIssueCommentGateway._request_json_with_token` owns caught publication
  `HTTPError` responses.
- One schema-mismatch fixture in `tests/test_review_runs.py` owns its raw SQLite
  connection.

## Guardrails

Use `contextlib.closing` or direct `close()` at the existing ownership boundary.
Do not add a helper, change retry/fallback/error behavior, consume response
bodies, touch feedback architecture, or broaden into adapters or persistence
redesign.

Focused verification must cover database readiness, schema mismatch, GitHub read
retry and terminal errors, publisher read-token fallback, non-idempotent failure,
and review-creation errors with ResourceWarnings enabled and tracemalloc active.
