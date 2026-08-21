# Resource lifetime cleanup implementation

## Result

The demonstrated SQLite connection and HTTP error response leaks now close at
their existing ownership boundaries. Review behavior, retries, token fallback,
database readiness, error translation, and public contracts are unchanged.

## Ownership clarified

- `verify_database_ready` closes the connection it opens.
- Each HTTP transport closes the `HTTPError` response it catches before the
  existing retry, fallback, or terminal branch continues.
- The schema-mismatch test fixture closes its own raw SQLite connection.

No shared helper or transport abstraction was added. The next architecture
slice remains responsible for any broader adapter boundary.

## Evidence

- The new ownership assertions failed and reproduced the resource warnings
  before the production correction.
- Eight focused warning paths passed with tracemalloc enabled and no
  `ResourceWarning` output.
- The four affected test modules passed 170 tests with resource warnings
  promoted to errors.
- Strict Pyright and the full bundle passed 468 tests plus replay and YAML
  validation with resource warnings promoted to errors.
- `git diff --check` and the Goal Maker checker passed.
- The `review-agent-resource-lifetime` Codex peer gate was green at score 8
  with no blockers and recommended committing the frozen candidate unchanged.

## Delivery

Commit `c59b6ac55af527de3b3a283747cb05ce9dbde755` was pushed directly to
`CCimen/review-agent` `main`.
