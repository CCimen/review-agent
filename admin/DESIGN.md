# Review Agent console

The console uses the accepted Astryx Neutral design in light and dark modes.
The [interactive preview](preview/README.md) is the visual reference. The upstream
`@astryxdesign/theme-neutral` package owns colors, typography, spacing, and
component appearance. Figtree is served locally; code uses the theme's system monospace
stack. The separate API reference retains its bundled IBM Plex assets.

`src/console.tsx` uses Astryx AppShell and SideNav for the collapsible rail and
mobile drawer. Page content starts beside the rail and stops growing at 1440 CSS
pixels. The page scrolls normally so route scroll restoration and review anchors
retain their existing behavior. Search, team context, account access, and build
status stay available from the shell.

Use `xds` MCP to inspect components, templates, and best practices before adding
new UI. The Login Card and Settings Form templates supply the login and settings
layouts. The Side Nav template uses the same shell structure. The incident console
and table templates provide useful scanning patterns for operational lists.
Adapt those patterns to actual data and actions. The login card includes optional
organization sign-in and registration. Email registration verifies an allowed
address before asking the user to choose a password.

Use Astryx controls, tables, typography, feedback, and public layout props across
the console. `src/theme.css` is the shared import entry: reset first, then Core,
Neutral, and locally served fonts. Keep that order so the reset cannot override
component styles. Do not add a separate visual stylesheet or copied theme.
The page Section has zero container padding; its VStack owns page spacing so
nested tables and sections cannot inherit an unrelated inset and overlap nearby
content. Compare pull requests and retained review requests in native tables.
Group related status and capacity details with MetadataList and explicit gaps.
Ordinary actions record their operation automatically. Only audit-log access asks
for a written justification. Owners manage SMTP in Settings; its password is
write-only and the test action uses the owner’s account email.

Keep form submission and browser validation native. Astryx text inputs forward
standard DOM attributes through a spread checked against React's input types,
which preserves required fields, autocomplete, datalists, and length limits.
Text areas use Astryx's length limit with UTF-16 clamping to match the API contract.
Numeric controls use integer validation where required; the shared Form blocks
submission of invalid drafts so a previously valid value cannot be submitted.
Published Markdown is sanitized before Astryx renders its typography, code blocks,
tables, and disclosures. Rejected URLs render as noninteractive text.

Published review text is the primary content on the reader page. Request history
belongs in the secondary rail; review actions open from the selected request status.
Recovery actions remain under an advanced disclosure, with confirmation before a
change. Settings and user management share persistent navigation. Finding decisions link
to an exact published occurrence. Historical results, live worker state, container
state, and provider authentication must retain their distinct meanings.

All displayed metrics come from typed API responses. Show an unavailable or empty
state when a capability has no source. Keep provider credentials and deployment integration
logic on the server. Newly issued application credentials are shown once and
kept out of shared caches and browser storage. Build the generated API contract with the backend when
changing a public boundary.

Administration's Usage page is restricted to owners and admins. Reuse the
workspace team scope and reporting period, with tabs for teams, repositories,
and GitHub requesters. Rank and paginate on the server; summary figures cover
all matching rows. Keep token reporting coverage next to the totals and show
missing tokens as unknown. Requests select the period; current outcomes and all
reported attempts belong to those requests. Team and repository links retain
the viewing context. Usage refreshes on demand rather than polling.

Keep bounded page content aligned with the start of the console body on wide
screens. Operational settings pair section descriptions with one column of controls,
stacking on narrow screens. Advanced controls remain progressively disclosed. Label GitHub's repository scope separately from Review
Agent activation. Live checks, stored state, and startup observations each show
their source and timing; background refresh must not replace an editor's draft.

Team context stays visible near the top of the rail, and appears in the mobile navigation drawer. A member with one team enters that workspace automatically and sees
its name without a selector. Members of multiple teams use a searchable Astryx
popover; owners and admins can view all teams or select one. The picker shows team
names, repository counts, and a link to team management. Team and reporting-period
context follow console navigation. Platform administration pages identify their
scope in the page introduction.

Application integrations share Settings navigation with users and roles. State
the platform scope, distinguish aggregate and published-content grants, and show
expiry and revocation without an editable permission matrix.
