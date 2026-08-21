# Frontend convention evidence

## Scope

This read-only review used two maintained Sundsvalls kommun repositories as
examples, not as organization-wide truth:

- `Sundsvallskommun/web-app-starter` at
  `eb2e817d5ac350df2a7204642c059a87fb64b77a` on `develop`.
- `Sundsvallskommun/web-shared-components` at
  `e5214095a3ae66803f9df134d908124786d73710` on `main`.

The evidence supports a later, repository-specific frontend policy overlay. It
does not justify adding frontend assumptions to the baseline reviewer profile.

## Reusable conventions

1. **Prefer the maintained design system over local replacements.**
   `web-app-starter/AGENTS.md` asks applications to build UI from
   `@sk-web-gui/react`, and imports throughout `frontend/src` and `admin/src`
   demonstrate that convention. `frontend/src/layouts/app/app-layout.component.tsx`
   also establishes `GuiProvider` as the application boundary. A review policy
   can flag hand-built substitutes when an existing SK Web GUI component already
   owns the behavior.

2. **Keep API types generated and end-to-end.**
   `web-app-starter/AGENTS.md`, each package's `generate:contracts` script, and
   imports from `@data-contracts/backend` establish generated OpenAPI contracts
   as the owner of API shapes. A policy can reject hand edits to detected
   generated-contract directories and duplicated handwritten transport types.

3. **Preserve strict typing instead of suppressing it.**
   The starter's TypeScript configurations enable `strict`,
   `noUncheckedIndexedAccess`, and `noImplicitReturns`; its agent guidance bans
   `any` in favor of `unknown` plus narrowing. Reviews should follow the actual
   repository configuration and challenge new suppressions or weakened checks.

4. **Reuse repository-owned quality gates.**
   `web-app-starter/package.json` composes lint, formatting, type checking, dead
   code detection, tests, and builds under `yarn verify`. The reusable rule is to
   discover and run the repository's existing focused and aggregate gates—not to
   hard-code this command or toolchain for every frontend.

5. **Keep design tokens and component styling in their canonical owner.**
   In `web-shared-components`, `packages/core/src/components/**` owns Tailwind
   component styling while component packages own React structure, behavior,
   types, and class composition. For changes inside that repository, reviews
   should reject new token or component-style truth scattered into package-local
   React files.

6. **Treat the aggregate React package as a curated public API.**
   `web-shared-components/packages/react/src/index.ts` explicitly re-exports the
   selected component packages, and `AGENTS.md` requires its exports and
   dependencies to move together. Reviews should verify both sides when an
   approved public component is added or removed.

7. **Verify component states and accessibility in representative usage.**
   Component stories are colocated under `packages/*/stories`, use autodocs, and
   the Storybook configuration enables the accessibility addon. The button
   implementation and starter usage also preserve native button behavior,
   loading/disabled state, focus treatment, and accessible labels. A later
   overlay can require representative states and accessible names without
   prescribing a specific test count.

## Details not suitable for organization-wide policy

- Next.js router choice, Express/routing-controllers, the admin application, and
  the starter's three-package monorepo shape.
- Current Node, Tailwind, ESLint, TypeScript, React, Storybook, or dependency
  pins. These are repository state, not durable organization rules.
- Particular authentication providers, upstream API subscriptions, locale
  libraries, build-time environment values, or deployment topology.
- The shared-components release/version workflow and its approval rule for the
  aggregate React package outside that repository.
- A requirement that every frontend use Storybook. Storybook ownership is
  verified for the component library, not for all applications.
- Package names or directory conventions unless the target repository actually
  contains the corresponding owner.

## Candidate overlay inputs

Apply these rules only when repository evidence activates them:

- When `@sk-web-gui/react` or the starter lineage is present, reuse an existing
  design-system component before creating local UI primitives.
- When a generated-contract owner is present, do not hand-edit its output;
  regenerate it and keep transport shapes derived from that owner.
- Follow the repository's strict TypeScript and lint configuration; do not add
  `any`, suppression comments, or weakened rules merely to make a change pass.
- Run the smallest relevant repository-owned checks, then the existing aggregate
  gate when the change warrants it.
- In `web-shared-components`, keep reusable styling/tokens in `packages/core`,
  keep React behavior in the component package, and verify public export and
  dependency changes together.
- For user-visible component changes, verify accessible names, keyboard/focus
  behavior, disabled/loading/error states, and representative responsive usage
  using the repository's existing Storybook or test setup.

These inputs should become a versioned, opt-in frontend overlay only after the
platform has a concrete policy-bundle owner. They must not be copied into the
generic baseline profile as unconditional framework rules.
