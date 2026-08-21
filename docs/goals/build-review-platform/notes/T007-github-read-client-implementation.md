# Bounded GitHub read client implementation

## Result

Live-review GitHub transport now has one concrete owner. The new
`GitHubReadClient` contains authenticated bounded GET requests, endpoint
validation, retry and backoff policy, HTTP status classification, response byte
limits and headers, JSON decoding, and response cleanup.

The tool module retains only token acquisition and translation into its existing
public error hierarchy. Changed-file pagination, exact-snapshot coordination,
run state, persistence, coverage, and JSON serialization remain with their
existing owners. Publication and feedback transports were not touched.

## Complexity removed

- Deleted HTTP request construction, retry loops, status mapping, byte handling,
  JSON decoding, and `urllib.error`/`urllib.request` ownership from `tools.py`.
- Moved the four transport-focused tests from the broad tool-handler suite into
  the new owner.
- Reused the existing changed-file request callback unchanged.
- Added no provider interface, dependency injection, global cache, or external
  HTTP dependency.

## Evidence

- The focused client contract was written first and failed before the owner
  existed.
- 87 source-control, pagination, diff-fallback, tool, and run tests passed with
  ResourceWarnings promoted to errors.
- Strict Pyright passed.
- The full bundle passed 469 tests plus replay and YAML validation with
  ResourceWarnings promoted to errors.
- The first Codex gate required stronger proof for bounded reads, successful
  cleanup, and 404/406 translation. Focused tests were strengthened without
  changing production code.
- The resumed `review-agent-github-read-client` gate was green at score 8 with
  no remaining findings.

## Delivery

Commit `3c1072bd09b0a1e09eb1bb95edac323429ba4a77` was pushed directly to
`CCimen/review-agent` `main`.
