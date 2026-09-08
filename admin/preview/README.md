# Astryx console preview

This screen preserves the accepted Astryx Neutral design in light and dark
modes. It uses illustrative pull requests and has no API connection.
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
The production console uses the same upstream Neutral theme.

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

Both entry points import `@astryxdesign/theme-neutral/built` and its
shared `src/theme.css` import entry (reset, Core, then Neutral). The pinned upstream package owns all theme tokens, component
overrides, icons, and licensing; no copied palette or theme is maintained.

Astryx Core, Neutral, StyleX's runtime, Lucide, and Figtree are production
dependencies. The CLI remains a development dependency. The preview has no
production route and is excluded from the admin image context. The full
lockfile remains the build inventory.

Astryx relies on modern CSS anchor positioning for selectors and other popovers.
See the installed CLI's `npm --prefix admin run astryx -- docs browser-support`
for its current browser tiers. Use the documented modern-browser baseline for the console.

`scripts/check_admin.sh` validates both builds with strict TypeScript and runs
the existing console tests. Visual and interaction checks at desktop and narrow
widths remain required before adopting the design.
