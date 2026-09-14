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

Published review text is the primary content on the reader page: on wide screens
it takes three fifths of the width and the request history is the rail beside
it, wide enough for the retained requests table to be read rather than
scrolled, and stacking below on narrow screens. A region that holds a table is
told it may shrink, so the table scrolls inside its own region instead of
pushing the page past the screen. The Result block leads with the
outcome (status dot, label, qualifier), groups commit, duration and comparison in a
MetadataList, and states a failure or coverage gap as a Banner whose title is the
next step. Request history
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

Administration's Usage page is restricted to owners and admins. It opens with
three "Most active" cards (teams, repositories, GitHub users), each the same
report cut to five rows and ranked against its leader; the tab below pages
through the full grouping, with a share bar under each request count. Ranking
bars carry the console's accent through `theme.css`, because the library's
accent variant is the blue this deployment spends on work that is running and
its neutral grey reads as a disabled control. Each card names whether it is the
grouping listed in full and otherwise links to it, so the cards and the tabs
below are not two competing ways to choose the same thing.

Every grouping can be narrowed to one GitHub login, so a team or repository
breakdown reads as one person's follow-up; a requester row links into it. An
active repository or GitHub user filter is a removable token under its own
label, one control clears them all, and because each drill is an address the
browser's Back button and a shared link both work. The console adds no back or
forward control of its own. Refresh sits beside the line that says how old the
figures are. There is no per-user daily series in the API, so trends stay on
Statistics, and the request list cannot yet be filtered by requester. Reuse the
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

Motion is Astryx's for controls, dialogs and disclosures. The console adds only
entrances in `src/theme.css`, on the theme's duration and easing tokens: a page
replacing another, a figure or list replacing its skeleton, the Copy label
swapping to a check, and one shake on a rejected sign-in. A copyable identifier
is shown as code beside an icon-only copy button, never inside a button, so a
chip, a pill and an underline do not stack on one value. On Health, each queue
and each service connection is a Card so uneven contents read as siblings. Hover, focus, filters and the ⌘K
palette get no added motion. Reduced motion keeps the fade and drops movement
and blur. Lists that will hold a table show the shared `Loading` skeleton while
their first answer is pending so the page does not grow when it lands.

## Documentation review controls

Implementation and rollout evidence are tracked by Beads epic
`ra-docs-review-ms1`. The [documentation review design](../docs/assessments/2026-09-14/documentation-review-design.md#team-and-repository-administration)
owns policy and authority. Reuse this console's components and navigation.

Documentation reviews are a Workspace destination, not a setting to be hunted
for. The page answers which repositories run them and where each mode comes
from, states the deployment switch first because it overrides everything, and
hands each repository to the editor that already owns it rather than becoming a
second one. A phone gets the same fields stacked instead of a table, because a
table would put the mode in a column the screen cannot show. The repository list reports the resolved mode, so the list and a
single repository share one precedence rule.

Team detail has a Documentation review section for the team's default mode. The
mode is a segmented control, not a menu: three or four mutually exclusive modes
are what the section is for, so they stay visible, with what "Inherit" resolves
to written beside them and the reason a viewer cannot change them carried by the
control's own disabled message. While the deployment switch is off, an owner can
reach it from the banner that says so.
Repository views expose a shared docs settings/detail section, reachable from team
repositories and repository activity as well as platform administration. Team
maintainers must not need the platform-only Access management page to change an
authorized repo override. Lead with effective mode, its inherited/explicit source
and readiness; keep rules, exclusions, ADR links and retained review evidence
progressively disclosed. The global control stays in owner-only Settings.

Show the affected inherited repositories and explicit exceptions before saving a
team default. Preserve settings drafts on refresh, keep input after recoverable
errors, and make read-only permissions clear. Mode and readiness have separate
labels: Automatic can be configured while waiting for rules or App permissions.
Repository rule/ADR changes go through GitHub; the panel provides source links and
a starter, not a second live policy editor.

Existing Activity, reader, Quality and authorized Usage views have a purpose
filter and links that retain team/repository/period context. Show exact selected
coverage and failure/skip reasons instead of a repository accuracy percentage.
Usage retains its platform-admin restriction and unknown token coverage. Reuse
current loading, empty, retry, freshness, focus, narrow-screen, light/dark and
reduced-motion behavior. Keep these controls in the existing navigation and visual system.

A save that worked is confirmed by a check in the theme's success colour beside
one sentence, announced once and entering on the fast band; every settings form
uses the same one. A link that leaves for GitHub opens in a new tab and says so.
Activity's reserved column widths add up to just under the console body at 1440
CSS pixels, so a row can be read without pushing the table sideways.

A render that fails, most often a lazily loaded page a deployment has replaced,
shows what to do about it rather than the framework's stack trace.
