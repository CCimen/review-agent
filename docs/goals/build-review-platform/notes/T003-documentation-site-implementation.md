# Documentation site implementation

## Result

The Sundsvall Review Agent documentation site is live at
<https://ccimen.github.io/review-agent/>.

The implementation keeps canonical Markdown in the repository, publishes only
the nine paths in `website/public-documents.json`, and uses a small Docusaurus
presentation layer. GitHub Pages is configured to deploy from GitHub Actions.

## Published revisions

- `904b14a`: documentation, site, public-content contract, and Pages workflow.
- `388242f`: explicit Mermaid layout dependency required by clean Linux builds.

The first hosted run exposed that Docusaurus imports the optional Mermaid layout
peer on Linux. The follow-up made that real runtime dependency explicit without
changing content, routing, or design.

## Verification

- Node 24 clean install, TypeScript check, and Docusaurus production build.
- `python3 scripts/check_docs.py` validated nine public documents.
- Built-route validation matched the homepage, 404, and the nine manifest slugs.
- Deliberate unexpected and duplicate routes failed before publication.
- `./scripts/check_bundle.sh` passed strict Pyright, 461 tests, replay policy,
  and YAML validation.
- Dark primary-action contrast is 7.18:1 normally and 6.00:1 on hover.
- One skeptical Codex commit gate returned green with score 8.
- Pages run `32463657915` built, validated, uploaded, and deployed successfully.
- The live homepage and `/docs/getting-started` return HTTP 200.
- Live 500 by 812 browser evidence showed one H1, mobile navigation, no
  horizontal overflow, a 44px primary action, and valid light/dark colors.

## Deliberate boundaries

Search, analytics, a custom backend, scanner integrations, Codex Security,
PostgreSQL, durable jobs, and a GitHub App remain outside this tranche. The npm
audit reports transitive Docusaurus/build-tool advisories; no unsafe forced
override was added to the static-site slice.
