# Astryx console preview

This screen evaluates Astryx's Neutral theme in light and dark modes before a
console migration. It uses illustrative pull requests and has no API connection.
Search, team and status filtering, theme switching, and navigation collapse are
interactive. Activity is the only preview destination.
Clearing filters keeps the selected team workspace.

From the repository root:

```sh
npm --prefix admin ci
npm --prefix admin run build:preview
npm --prefix admin run serve:preview
```

Open <http://127.0.0.1:8092>. For development, use `npm --prefix admin run dev:preview`.
The separate Vite configuration has no backend proxy. The production build does
not include this entry, and the admin image excludes preview sources and outputs.
The existing console continues to use `src/console.tsx` and `src/styles.css`.

The direction follows the supplied Notara and Dokploy references: quiet neutral
surfaces, a compact sidebar, visible team context, and a table that keeps review
requests easy to scan. Astryx owns controls, spacing, typography, focus behavior,
and the responsive navigation drawer. Figtree fonts are served locally.

The content stays aligned to the rail and stops growing at 1440 CSS pixels so
statuses remain near their pull requests on large displays. The columns require
960 CSS pixels together; the table scrolls horizontally by keyboard or touch
when its available space is narrower. Empty results
use Astryx's announced empty state outside that region so the recovery action
does not inherit the table's minimum width.

`theme/` was copied with Astryx 0.5.4's `theme add neutral` command. The preview
uses that editable source through the Theme provider's runtime injection. The
only source adjustment removes an unused React import for strict TypeScript.
The upstream MIT license is retained in `theme/LICENSE`. Before production
adoption, compile the accepted theme with `astryx theme build` and validate the
console's forms, review reader, permissions, and browser support.

The live theme imports are `neutralTheme.ts`, `icons.tsx`, and
`neutralPaletteRefs.generated.ts`. The remaining palette files and receipt are
authoring and regeneration inputs copied by the CLI. The installed
`@astryxdesign/theme-neutral` package remains the pinned upstream reference for
comparing this editable copy during upgrades.

All preview packages are development dependencies. The admin image builder still
installs them because it uses `npm ci`, which includes development tools. They
therefore add build-time downloads and disk use, and appear in the full lockfile
inventory as development packages. They are excluded from the direct production
dependency check and do not add code to the production UI entry points. Remove
the preview directory, its package scripts and dependencies, and its
`check_admin.sh` build step if this direction is declined.

Astryx relies on modern CSS anchor positioning for selectors and other popovers.
See the installed CLI's `npm --prefix admin run astryx -- docs browser-support`
for its current browser tiers. Older-browser positioning needs a deliberate
product decision before migration.

`scripts/check_admin.sh` validates both builds with strict TypeScript and runs
the existing console tests. Visual and interaction checks at desktop and narrow
widths remain required before adopting the design.
