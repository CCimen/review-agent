# Operational capacity-limit audit

TL;DR: The reviewer now pages large per-file diffs, reads every regular source
file supported by GitHub's Contents API, and lets Hermes derive context-file and
turn capacity from the active runtime. Provider, persistence, publication, and
denial-of-service guards remain explicit, and every unavailable path keeps
coverage incomplete.

## Decision ledger

| Surface | Decision | Canonical owner and rationale |
| --- | --- | --- |
| Hermes context files | Remove the explicit 24,000-character override. | Hermes derives the allowance from the selected model context window when the key is absent. `SOUL.md` and `AGENTS.md` are no longer tied to today's model. |
| Hermes turns | Remove the explicit 90-turn override. | Hermes' native default is unlimited and its loop guardrails still stop repeated exact failures and idempotent no-progress calls. A review is no longer cut off because a future model needs a different number of calls. |
| Per-file diff | Derive a JSON-safe response page from one operator-configurable 160,000-character plugin-result budget, then add `next_start_char`, total length, and source identity. | `capacity.py` owns one startup-validated page-result budget shared by schema and handlers, and `_page_output` enforces it without rejecting unrelated memory or delivery payloads. It is not a total readable-diff ceiling. Coverage stays conservatively truncated because the persistence layer does not claim that independent response pages prove a complete set. |
| Source file | Delete the arbitrary 5,000,000-byte ceiling; use GitHub's documented 100 MB Contents API boundary and cache one immutable revision-keyed response. | `tools.py` owns the exact-revision source read, one-file process-memory bound, avoidance of sequential-page refetch amplification, and terminal unavailable state. Output is bounded by both 400 lines and the JSON-safe page derived from the configured complete-result budget; low-newline content reports honest truncation instead of overflowing the result or recording a partial line as complete. |
| Changed files | Retain and rename the 3,000-file value as a GitHub provider contract. | `changed_files.py` owns offset-safe pagination and honest `api_limit` or `budget_exceeded` state. GitHub documents a maximum of 3,000 files from this endpoint. |
| Enumeration transport | Retain 3,000,000 bytes per response with descending fixed page sizes. | The changed-file owner retries complete passes without offset drift, then returns an incomplete terminal pass when one patch cannot fit. This is bounded network memory, not a repository quota. |
| Changed-path and source pages | Retain 200 changed paths and 400 source lines per response. | `schemas.py` owns both public per-call limits. Both interfaces have continuation inputs and bound one result, not total review depth. |
| Historical context | Reuse the 200-path changed-file page size and require page batches in the review procedure. | `schemas.py` owns the shared public per-call limit. It bounds one database/result call while allowing every changed-file page to be checked. |
| Finding record | Consolidate the duplicate 200-item constant under the finding domain owner and retain it. | It bounds one atomic record/publication lifecycle. Over-limit input fails explicitly; it is not silently truncated or used as an editorial quota. |
| Native suggestions | Retain 12 selected suggestions, 8 changed lines, 16 replacement lines, and bounded text. | The shared suggestion validator owns an optional, independently applicable GitHub patch contract. Findings that do not receive a suggestion remain fully published. |
| Publication parts | Retain the typed 60,000-byte default and 65,000-byte maximum. | The publication partition owner splits arbitrarily large reviews into deterministic GitHub-safe parts. This is a per-part provider guard. |
| PostgreSQL payloads and pools | Retain JSON aggregate, text, transaction, pool, timeout, and retry bounds. | These are storage-integrity and resource-safety contracts. They do not restrict how much source the model can inspect. |
| Webhook bodies and feedback text | Retain request and field bounds. | These are authenticated-input denial-of-service and deterministic parsing guards. |
| Identifiers | Retain SHA-256, commit SHA, and local finding-reference grammar. | These values are protocol identifiers, not model or capacity limits. |

Primary provider evidence:

- [GitHub pull-request files](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files)
  documents the 3,000-file maximum.
- [GitHub repository contents](https://docs.github.com/en/rest/repos/contents#get-repository-content)
  documents raw support from 1 MB through 100 MB and no Contents API support
  above 100 MB.
- [Hermes configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
  documents unlimited turns and the purpose of per-response tool-output bounds.
- [Hermes context files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)
  documents model-capability-derived context sizing when no explicit override is
  present.

## Deliberate non-goals

No limits framework, deployment-profile capacity keys, provider abstraction,
administration UI, dynamic policy engine, or persisted diff-page ledger was
added. Persisting exact page coverage would require a storage-contract change;
until that work has demonstrated value, an oversized paged diff remains
honestly incomplete.
