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
state when a capability has no source. Keep credentials and deployment integration
logic on the server. Build the generated API contract with the backend when
changing a public boundary.
