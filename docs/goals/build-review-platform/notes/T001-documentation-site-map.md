# T001: Documentation Site Map

Task: `T001`
Kind: `scout`
Status: `current`

## Summary

The first complete slice should add one isolated Docusaurus site under
`website/`, reuse the repository's public Markdown owners, add only the missing
high-level and onboarding pages, and deploy static output through GitHub Pages.
The site must describe the current SQLite/PAT/workflow deployment honestly and
label PostgreSQL, the GitHub App, trusted project context, and other platform
work as roadmap. Security scanners and Codex Security are explicitly deferred.

## Canonical Content Owners

- `README.md`: concise product/repository overview and entry links.
- `docs/OPERATIONS.md`: setup, environment, deployment, repository onboarding,
  runbooks, backups, and updates.
- `docs/SECURITY.md`: public trust model, tool/token boundaries, prompt
  injection, data handling, and deterministic-scanner boundary.
- `examples/github/ai-review-request.yml`: exact current GitHub trigger contract.
- `examples/comments/example-review.md`: public rendered-review example.
- `review-learning/README.md`: private learning governance; link to it on GitHub
  but do not publish the private learning tree as site content.
- `bootstrap/SOUL.md`: global reviewer identity, tone, and evidence posture.
- `bootstrap/workspace/AGENTS.md`: global review contract and fixed invariants.
- `bootstrap/skills/review-agent-pr/SKILL.md`: runtime review procedure and tool
  sequence.
- `bootstrap/config.yaml`: Hermes runtime/model/tool wiring, not policy.

The site must not publish `docs/goals/`, runtime profile files, private learning
artifacts, or broad glob matches from the repository root.

## Required Site Content

1. A custom home page that explains the product and links to the first review,
   repository onboarding, behavior ownership, security, and operations.
2. How reviews work: trusted trigger, exact snapshot, bounded reads, two-pass
   reasoning, durable memory, deterministic publication, feedback, and re-review.
3. Getting started and adding a repository today:
   - extend `REVIEW_AGENT_ALLOWED_REPOSITORIES`;
   - scope the three current GitHub tokens to the repository;
   - copy the example workflow to the default branch;
   - configure four Actions secrets and `AI_REVIEW_ALLOWED_USERS`;
   - protect the workflow and run `/review`.
4. Behavior ownership:
   - `SOUL.md` changes global identity/tone;
   - `AGENTS.md` changes the global review contract;
   - `SKILL.md` changes the global procedure;
   - `bootstrap/config.yaml` changes runtime wiring;
   - repository-specific trusted base-branch context is planned, not available.
5. FAQ covering access, execution, merge-gate status, findings/suggestions,
   feedback, SQLite, learning, dependency scanning, and failures.
6. A current-versus-target roadmap page that never presents planned capability
   as shipped.
7. Existing Operations, Security, and example review pages in the same sidebar.

## Technical Shape

- Docusaurus `3.10.2`, React 19, TypeScript, npm, Node 24.
- Site root: `website/`; committed `package-lock.json`; root remains Python-owned.
- Docusaurus content path may point at the repository root only with an explicit
  `include` allowlist for public Markdown and no broad recursive glob.
- GitHub Pages production URL: `https://ccimen.github.io/review-agent/` with
  `url`, `baseUrl`, and `trailingSlash: false` configured explicitly.
- Official Mermaid theme for the high-level lifecycle diagram.
- No search dependency in this small first release. Docusaurus has no official
  local-search implementation; navigation and browser find are sufficient until
  usage justifies Algolia or a reviewed local plugin.
- Two workflows: a pull-request docs build and an artifact-based Pages deploy
  from `main`, using minimum permissions and immutable action revisions.
- Owner action after merge may be required: Settings -> Pages -> Source ->
  GitHub Actions.

## Visual And Accessibility Direction

Use a restrained Read-mode interpretation of maintained Sundsvall examples,
not a copied service-application shell:

- white/near-white canvas, charcoal text, pale neutral rails and separators;
- vattjom blue as the single navigation/link/focus accent;
- Raleway headings and Arial body only if fonts are self-hosted through a
  maintained dependency; otherwise prefer the durable system stack;
- article measure around 65-75 characters, moderate 8/12/16/24 spacing rhythm,
  modest radii, and minimal shadows/cards;
- native navigation semantics, clear current-page state, strong `:focus-visible`
  ring, semantic headings, skip link, 40-44px targets, scrollable code/tables,
  reduced-motion respect, and mobile navigation below 768px;
- no official municipal logo unless an approved asset is available; use a
  text-first product identity rather than redrawing a mark;
- keep the existing English documentation coherent and leave the Docusaurus
  i18n owner ready for a later Swedish translation instead of mixing languages.

Verified contrast evidence from the example tokens includes charcoal on white
at approximately 16.39:1, vattjom text on its pale surface at approximately
8.06:1, and the blue focus ring on white at approximately 3.51:1.

## Implementation Boundary

Candidate allowed files:

- `PRODUCT.md`
- `DESIGN.md`
- `.impeccable/**`
- `.gitignore`
- `README.md`
- `docs/OPERATIONS.md`
- `docs/SECURITY.md`
- new public `docs/*.md` pages and category metadata
- `website/**`
- `.github/workflows/docs-check.yml`
- `.github/workflows/docs-pages.yml`
- `docs/goals/build-review-platform/**`

Do not change reviewer runtime, persistence, deployment, profile behavior,
workflow trigger semantics, or private learning artifacts in this slice.

## Validation

- `npm ci`, TypeScript type check, and strict Docusaurus production build.
- Verify built output contains only the public allowlisted content and correct
  `/review-agent/` assets/links.
- Inspect desktop and mobile renders, keyboard focus, navigation, tables, code
  overflow, light/dark contrast, and reduced-motion behavior.
- Run the Impeccable detector once after the UI is complete.
- Run the existing documentation-contract tests and full bundle once.
- Run `git diff --check` and the Goal Maker state checker.
- One skeptical Codex peer-review gate before commit.
- Commit and push the verified slice directly to `main`.

## Board Receipt Snippet

```yaml
receipt:
  result: done
  note: notes/T001-documentation-site-map.md
  summary: "Mapped one truthful Docusaurus/Pages slice with canonical Markdown ownership, current onboarding, roadmap separation, and evidence-backed Sundsvall Read-mode accessibility."
```
