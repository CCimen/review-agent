# Bounded GitHub read client scope

## Decision

Extract the live reviewer's bounded GitHub read transport from the 1,806-line
tool handler into one concrete `GitHubReadClient`. Two independent read-only
audits selected this as the highest-leverage next step in the approved A2
maintainability sequence.

This is not a generic source-control port. GitHub is the only demonstrated
provider, so a concrete deep module is the smallest complete owner.

## Canonical ownership

`source_control.py` will own authenticated bounded GET requests, endpoint
validation, retries and backoff, response byte limits and headers, JSON
decoding, transport error classification, and response cleanup.

`tools.py` will continue to own tool payload validation, repository/run
lifecycle, PR and file shape validation, exact-snapshot coordination,
persistence calls, coverage recording, and public JSON serialization.

`changed_files.py` already owns offset-safe pagination through its narrow
`RequestFn` callback and remains unchanged. Publication and feedback keep their
separate credential, mutation, idempotency, and trust boundaries.

## Required preservation

- Exact public tool JSON and error messages.
- Retry count, status set, and backoff.
- Blank-token behavior and authorization header formation.
- Response byte limits, truncation, and header handling.
- Whole-diff 406 and transport-truncation fallback behavior.
- Fork-head and exact base/head file reads.
- Terminal missing-file behavior and stale-snapshot no-network behavior.

## Explicit non-goals

No generic provider interface, dependency-injection framework, global client
cache, third-party HTTP dependency, publisher or feedback change, database
change, application-service extraction, project-context policy, PostgreSQL,
GitHub App, Slack, scanner, or Codex Security work.
