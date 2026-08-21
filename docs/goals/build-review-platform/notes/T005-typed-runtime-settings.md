# Typed core runtime settings

## Result

Core runtime environment interpretation now has one typed, standard-library-only
owner in `bootstrap/plugins/review_agent_tools/settings.py`.

The owner is stateless and lazy. Invalid settings fail only when a consumer uses
that value, so an unrelated configuration error cannot break a previously
independent path. Existing module functions remain in place as domain wrappers,
preserving public errors and caller behavior.

## Preserved contracts

- Empty repository allowlists deny by default; matching remains exact and
  case-insensitive after trimming.
- Read and publish tokens retain separate roles and existing read fallback.
- Publication byte limits retain their default, clamp, and exact error.
- Database path, policy revision, and feedback enablement retain their current
  fallback, normalization, and validation behavior.
- The five migrated consumers contain no direct environment reads.
- Feedback-sidecar configuration remains a separate trust boundary.

## Verification

- Red-first settings import failed before the owner existed.
- 170 focused settings, tools, runs, publisher, and database tests passed.
- Strict Pyright and the full 467-test bundle passed with replay and YAML checks.
- Direct top-level fallback imports passed.
- One skeptical Codex peer gate returned green with score 9.
- Published revision: `c34eb827179eff1faf8b8c728070cc75a6b8e90e`.

Resource-lifetime warnings observed during the suite are unchanged pre-existing
defects and are isolated into T006.
