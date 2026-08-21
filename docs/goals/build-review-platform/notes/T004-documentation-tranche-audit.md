# Documentation tranche audit

## Decision

Complete at revision `388242f`. The original site commit alone was not complete
because its first clean Linux build failed; the explicit Mermaid dependency and
successful hosted deployment close that defect.

## Evidence

- Pages run `32463657915` passed install, contract validation, type-check,
  production build, exact-route verification, artifact upload, and deployment.
- The live site and a direct documentation deep link return HTTP 200.
- One JSON manifest owns publication membership; build-output equality excludes
  goal notes, runtime profiles, private learning material, and other Markdown.
- Current PAT, Actions, webhook, Docker, and SQLite behavior is separated from
  planned PostgreSQL, jobs, GitHub App, and trusted repository context.
- `SOUL.md` remains deployment-wide identity, not repository customization.
- The live mobile audit and source-level accessibility rules expose no material
  responsive or accessibility blocker.

## Next slice

Centralize interpretation of core runtime environment settings in one typed,
standard-library-only module. Preserve observable behavior and avoid changing
the feedback sidecar, deployment contracts, public schemas, persistence, or
GitHub publication behavior.

Existing SQLite and HTTP-error resource warnings are a separate required
cleanup after settings ownership. They must not expand the settings slice.
