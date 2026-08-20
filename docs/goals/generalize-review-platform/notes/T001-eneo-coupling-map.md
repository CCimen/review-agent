# Eneo Coupling Map

> **Superseded migration assumption:** This discovery receipt originally assumed
> the source deployment needed one-release compatibility aliases. The repository
> owner later clarified that this is a new Sundsvall platform with no Eneo
> compatibility requirement. Do not follow compatibility recommendations below;
> the active clean-break contract is recorded in `state.yaml`.

## Result

The repository contains broad Eneo naming, but the review domain and SQLite
schema are already repository-neutral. Most coupling belongs to deployment,
public command, profile, and persisted compatibility contracts. A blind global
rename would mix behavior and migration changes.

The smallest coherent first implementation slice is the repository allowlist:
make `REVIEW_AGENT_ALLOWED_REPOSITORIES` the canonical setting used by both live
review reads and feedback ingestion, retain `ENEO_ALLOWED_REPOSITORIES` as a
one-release compatibility alias, and fail closed when both are configured with
different values.

## Canonical Owners

- Visible review copy: `bootstrap/plugins/eneo_review_tools/review_identity.py`
- Tone and identity: `bootstrap/SOUL.md`
- Repository review policy: `bootstrap/workspace/AGENTS.md`
- Review procedure: `bootstrap/skills/eneo-pr-review/SKILL.md`
- Tool registration and contracts:
  `bootstrap/plugins/eneo_review_tools/__init__.py`, `schemas.py`, `plugin.yaml`,
  and `bootstrap/config.yaml`
- Repository allowlist:
  `tools.py::_allowlisted_repository`,
  `feedback_bridge.py::parse_repository_allowlist`, and
  `feedback_bridge.py::load_config`
- Persistence: `memory_schema.py` and `memory_validation.py`
- Persisted publication compatibility: `memory_publications.py`,
  `memory_suggestions.py`, `review_renderer.py`, and `review_publisher.py`
- Deployment: `compose.yaml`, `Dockerfile`, `bootstrap/install.py`,
  `.env.example`, and `docs/OPERATIONS.md`
- GitHub trigger: `examples/github/ai-review-request.yml`

## Coupling Classification

### Visible identity

The PR review title and feedback copy are already generic and centralized in
`review_identity.py`. Eneo remains visible in profile prose, workflow names and
logs, operator headings, learning reports, documentation, and examples.

### Compatibility identifiers

The `eneo_review_tools` package, `eneo_review` toolset, seven tool names,
`eneo-pr-review` skill, plugin name, operator commands, install paths, webhook
routes, and `ENEO_*` variables are public or deployment contracts. Hidden
`eneo-review:*` comment markers are persisted protocols rather than branding.

### Policy and profile

`bootstrap/workspace/AGENTS.md` contains Eneo-specific FastAPI, SQLAlchemy,
PostgreSQL/pgvector, Redis/ARQ, SvelteKit, tenant, OIDC, retrieval, and MCP
invariants. Generalizing it changes review behavior and therefore belongs to a
later versioned policy/profile slice.

### Persistence

SQLite table and column names are generic. No Eneo-branded schema migration is
needed now. `ENEO_REVIEW_DB` and `ENEO_REVIEW_POLICY_REVISION` are configuration
contracts; publication markers and stored policy revisions need explicit
compatibility handling.

### Deployment and workflow

Environment reads span reviewer, persistence, publisher, feedback, and CLI
entrypoint modules. Docker/install paths and executable names are coupled to
Compose, Pyright, bundle checks, documentation, and tests. Workflow concurrency,
route identifiers, and delivery strings are operational contracts, not merely
cosmetic copy.

### Tests and baseline gaps

Tests intentionally pin legacy environment names, imports, tool names, install
paths, comment markers, commands, workflow strings, and Eneo replay fixtures.
Replay tests validate fixture structure, not exact rendered output. There is no
committed full rendered-review golden or captured database fixture.

## Proposed First Slice

Acceptance:

- The generic setting alone authorizes the same exact repository list.
- The legacy setting alone behaves exactly as before.
- Empty configuration remains deny-by-default.
- Conflicting generic and legacy values fail closed without exposing values.
- Review Markdown, SQLite schema, tool names, routes, and GitHub markers do not
  change.

Balanced verification:

- One shared configuration contract covering generic, legacy, conflict, and
  empty behavior.
- Existing reviewer and feedback tests prove both consumers use that contract.
- Run the three affected test modules, `git diff --check`, then the existing
  bundle once.
- Do not add repository-count or environment-variable permutation matrices.

## Explicit Deferrals

- PostgreSQL, jobs, outbox, GitHub App, scanners, Slack, and service topology.
- Repository/project/component policy resolution.
- Removing Eneo policy from `AGENTS.md`.
- Package, tool, skill, plugin, command, route, install-path, or marker renames.
- SQLite schema changes and replay fixture rewriting.
- Renaming Eneo replay repositories, which are valid legacy evidence.
- Product title changes until Sundsvalls kommun supplies the display name.

## Safe Rename Order After This Slice

1. Generic repository-allowlist owner and compatibility alias.
2. Remaining `REVIEW_AGENT_*` aliases in credential-boundary-sized slices.
3. Generic CLI aliases while retaining old executable names.
4. Generic tool aliases after Hermes alias registration is proven.
5. Package, skill, plugin, install path, and import rename together.
6. Profile/policy extraction as a behavior-versioned change.
7. Hidden GitHub markers last, with dual-read compatibility.

## Separate Follow-Ups

- Correct stale publisher-token permissions in `.env.example` and the stale
  Hermes version in `docs/OPERATIONS.md`.
- Capture a rendered-review golden and baseline SQLite fixture before marker or
  package renames.
- Map and migrate remaining `ENEO_*` settings after the allowlist pattern is
  proven.
