# Review-run application ownership

## Result

Review-run lifecycle and objective coverage coordination now have one concrete,
typed application owner. `review_run_application.py` owns run requests and
subjects, exact-snapshot validation, phase transitions, one-time terminalization,
changed-file registration, diff exposure, changed-file paging, file-context
loading, and source-range coverage.

`tools.py` remains the transport adapter. It still validates model input, loads
GitHub pull data through the single narrow callback, serializes public JSON and
errors, records findings, finalizes and delivers publications, and publishes
terminal failure status. SQLite persistence still belongs to the existing
`memory_db` facade and its run and coverage modules.

## Complexity removed

- Deleted direct run-lifecycle and coverage facade calls from the tool adapter.
- Replaced repeated repository, pull-request, run, phase, and terminal checks
  with one typed run-subject interface.
- Grouped snapshot validation with changed-file paging and file-context loading
  instead of adding pass-through persistence wrappers.
- Kept snapshot database access to the prior two bounded connection lifetimes.
- Added no provider port, dependency-injection framework, cache, schema change,
  or deferred platform capability.

## Behavior evidence

- The new interface tests first failed because the application owner did not
  exist, then passed after the extraction.
- Inactive or wrong-head runs are rejected before the GitHub pull loader runs.
- Snapshot changes terminalize once; later calls reuse the persisted terminal
  state without another GitHub read or duplicate failure publication.
- An injected failure in the first phase transition marks the committed run
  failed and permits an immediate retry with a new run ID.
- Diff and source-read coverage remain complete, incomplete, or unknown through
  the existing persistence contract.

## Validation

- 111 focused tests passed with ResourceWarnings promoted to errors.
- Strict Pyright passed with zero errors and warnings.
- The full bundle passed 477 tests plus YAML and bundle validation. Historical
  replay remained skipped because no fixtures are tracked.
- `git diff --check` passed.
- The resumed `review-agent-t008-run-coverage` Codex gate challenged connection
  locality, shallow wrappers, import guards, expected-head ownership, and initial
  phase failure recovery. Iteration 3 was green at score 8 with no findings.

## Delivery

Commit `43afb8de991ea87995580cb6c18290d404800568` was pushed directly to
`CCimen/review-agent` `main`.
