# T002: Documentation Slice Decision

Task: `T002`
Kind: `judge`
Status: `done`

## Decision

Proceed with one static Docusaurus 3.10.2 documentation site under `website/`.
The site will render the canonical Operations, Security, and example-review
Markdown directly through an explicit publication allowlist. It will add only
the missing orientation, onboarding, ownership, FAQ, and roadmap pages plus a
small, accessible Sundsvall-inspired Read-mode visual layer.

`PRODUCT.md` and `DESIGN.md` are included as internal product/design owners
required by the selected UI workflow. They are not part of the published
Docusaurus allowlist.

The existing README-title contract test may change only to pin the new public
product title. No new test matrix belongs in this slice.

## Information Architecture

- Home: current product outcome, trust summary, and direct paths to first review,
  repository onboarding, security, and operations.
- Start here: Getting started; How reviews work.
- Understand: Behavior ownership; Example review.
- Operate: Operations; Security model; FAQ.
- Future: Current and planned capabilities.

## Product Truth

- Current: repository-scoped PATs, GitHub Actions and HMAC webhooks,
  Docker/Dokploy, SQLite, bounded reads, deterministic publication, and advisory
  review.
- Deployment-wide owners: `SOUL.md`, `AGENTS.md`, `SKILL.md`, and
  `bootstrap/config.yaml`.
- Planned: trusted base-branch repository context, PostgreSQL, durable jobs, and
  a GitHub App.
- Deferred: scanner and Codex Security integrations.
- Private learning and verification remain operator-run shadow workflows outside
  live publication and merge gating.
- GitHub Pages hosts documentation only and receives no reviewer credentials.

## Visual And Accessibility Contract

Use a restrained Read mode: near-white canvas, charcoal text, pale rails, one
vattjom-blue accent, a 65-75 character article measure, modest radii, and
minimal shadow. Use system fonts and no municipal logo or invented brand asset.
Retain Docusaurus light/dark behavior, semantic landmarks, skip navigation,
visible current-page and focus states, keyboard navigation, scrollable code and
tables, reduced-motion handling, and WCAG AA contrast. Verify 375 x 812 and
1440 x 900 layouts without clipping or page-level horizontal overflow.

## Explicit Non-Goals

- Reviewer runtime, Compose/Dokploy behavior, persistence, plugins, token
  semantics, or GitHub review-trigger behavior.
- Scanner, Codex Security, SARIF, or dependency-security aggregation.
- Third-party themes, search, analytics, a blog, versioned docs, API reference,
  a custom backend, or a separate web application.
- PostgreSQL, GitHub App, durable jobs, trusted per-repository context, or
  private-learning changes.
- Any retired branding, alias, compatibility path, or private inspiration
  reference.

## Validation

- `npm --prefix website ci`
- `npm --prefix website run typecheck`
- `npm --prefix website run build`
- confirm exact Docusaurus 3.10.2 pins and immutable workflow action SHAs
- confirm the build exposes only the homepage and nine allowlisted documents
- inspect 375 x 812 and 1440 x 900 in light and dark modes, including keyboard
  focus, navigation, code, and tables
- run the Impeccable detector once
- `./scripts/check_bundle.sh`
- `git diff --check`
- Goal Maker state validation
- one skeptical Codex peer gate before commit

## Stop Conditions

Stop if canonical content must be copied, the content plugin requires a broad
repository glob, current product behavior remains ambiguous, a third-party
theme/search dependency becomes necessary, private content becomes public, or a
focused build/link/accessibility check fails twice for a non-mechanical reason.
If GitHub Pages needs an owner-only repository setting, finish and publish the
repository work, then report: Settings -> Pages -> Source -> GitHub Actions.
