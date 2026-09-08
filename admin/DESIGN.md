# Review Agent console

The console follows the supplied Review Agent Console v3 design: a 224px sidebar,
52px top bar, compact tables, and persistent deployment status. On small screens,
section navigation moves below the content and the review request selector replaces
the history rail. The main sections are Activity, Repositories, Review quality,
Health, and Settings.

Use the IBM Plex Sans and Mono assets in `src/assets`, with their bundled OFL
license. `src/styles.css` owns the shared colors, spacing, type scale, light and
dark themes, controls, and responsive behavior. Keep forms, dialogs, disclosures,
tables, and focus behavior native where possible.

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

Keep bounded page content aligned with the start of the console body on wide
screens. Operational settings use at most three columns, with advanced controls
progressively disclosed. Label GitHub's repository scope separately from Review
Agent activation. Live checks, stored state, and startup observations each show
their source and timing; background refresh must not replace an editor's draft.

Team context stays visible near the top of the rail, with a compact counterpart on
small screens. A member with one team enters that workspace automatically and sees
its name without a selector. Members of multiple teams use a searchable native
popover; owners and admins can view all teams or select one. The picker shows team
names, repository counts, and a link to team management. Team and reporting-period
context follow console navigation. Platform administration pages identify their
scope in the page introduction.

Application integrations share Settings navigation with users and roles. State
the platform scope, distinguish aggregate and published-content grants, and show
expiry and revocation without an editable permission matrix.
