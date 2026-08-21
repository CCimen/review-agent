# Documentation Design

## Direction

Use a restrained civic Read mode inspired by maintained Sundsvall frontend
examples without copying an application shell. The visual character comes from
clear hierarchy, narrow reading measure, flat structure, deliberate focus
states, and one blue accent—not decoration.

## Foundations

- Canvas: white and near-white (`#ffffff`, `#fafafa`, `#f0f0f0`).
- Text: charcoal (`#1f1f25`) with muted text that remains WCAG AA compliant.
- Accent: vattjom blue (`#005595`), with `#004c85` for text on pale surfaces and
  `#0c8ced` for focus indication.
- Typography: durable system sans stack; 16/24 body; compact but decisive
  headings; 65-75 character article measure.
- Structure: 1px dividers, modest 12px radii only for functional surfaces, and
  minimal shadow.

## Homepage Composition

The homepage leads with the outcome: evidence-backed pull-request review. A
short lifecycle strip explains Request, Review, Publish, Improve. Direct paths
lead to the first review, repository onboarding, the security model, and
operations. A plain current-versus-next section prevents roadmap ambiguity.

## Documentation Chrome

Docusaurus remains the navigation and accessibility owner. Customize its
tokens, link and focus treatment, content measure, rails, tables, and code
surfaces; do not replace its semantic header, skip link, mobile menu, sidebar,
or color-mode control with a second component system.

## Accessibility And Responsive Rules

- Preserve semantic landmarks, one page `h1`, native navigation, and the skip
  link.
- Keep visible `:focus-visible` rings and non-color current-page indicators.
- Maintain at least 4.5:1 normal-text and 3:1 large-text/non-text contrast.
- Keep primary controls near 40-44px and code/tables horizontally scrollable.
- At 375px, use one readable column with no page-level horizontal overflow.
- At 1440px, keep article prose narrow even though navigation chrome is wider.
- Respect reduced motion; transitions must not be required to understand state.
- Support light and dark modes using the same semantic hierarchy.

## Deliberate Omissions

No municipal logo, invented seal, external font, gradient, glass effect,
decorative illustration, icon-card grid, oversized hero, search dependency, or
marketing metrics. The site documents a working engineering system; it does not
pretend to be a separate product application.
