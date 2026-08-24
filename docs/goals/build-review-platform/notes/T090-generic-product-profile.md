# T090 — Generic product and trusted deployment profiles

## Outcome

The public product and documentation site are now named **Review Agent**.
`sundsvall-standard` remains the packaged default and first municipal deployment
profile; legitimate `sundsvallskommun` examples remain examples rather than
engine identity.

Implementation revision:
`e16163a0d5817a97fd9dd3927089492540294c14`.

## Profile contract

Operators select a trusted in-repository bundle with `REVIEW_AGENT_PROFILE` or
the installer `--profile` flag. The installer validates a lower-case bundle key,
required `SOUL.md` and `workspace/AGENTS.md` files, the closed `profile.json`
schema, each reviewed skill directory, and every skill required by the managed
webhook route. Unknown, traversing, structurally invalid, or route-incomplete
profiles fail before `HERMES_HOME` is changed.

- `SOUL.md` owns identity, voice, and explanatory language.
- `workspace/AGENTS.md` owns stable review rules and presentation.
- `profile.json` lists reviewed skills; skill files remain trusted,
  code-reviewed content rather than machine-safe configuration.
- `bootstrap/config.yaml` and deterministic plugin code retain exclusive
  ownership of model/provider, routes, toolsets, authorization, prompt trust,
  snapshots, persistence, publication markers, delivery, and lifecycle.

The installer records its selected profile and managed skills atomically, can
reuse that receipt outside Compose, removes previously recorded managed skills
that the next profile does not list, and treats a damaged receipt as absent so
an explicit profile can recover the deployment.

No inheritance graph, template language, plugin marketplace, dynamic loader,
provider abstraction, live upload, or administration UI was added. Fixed
publication headings and markers are not free-form profile templates.

## Naming boundary

Organization-specific product copy was removed from README, product context,
onboarding, roadmap, Docusaurus metadata/homepage, and package metadata. The
Sundsvall name remains only where it has domain meaning: the shipped profile's
identity and municipal configuration/examples.

## Verification

- Strict Pyright and the 588-test bundle passed with 71 expected no-database
  skips; replay and YAML checks passed.
- Forty-two focused profile, documentation, Dockerfile, and workflow contract
  tests passed. The final route-skill pin also passed in the 31-test focused
  profile/docs run.
- The public nine-document contract, website typecheck, and Docusaurus
  production build passed.
- Claude Opus/high session `review-agent-t090-generic-profile`, UUID
  `caa28055-7ac5-4923-808a-de58403cd5e8`, found the missing route-skill
  cross-check at score 7 and gave the corrected candidate green at score 8 in
  one resumed verification pass.

## Carry forward

T091 audits real capacity ceilings. It must distinguish model-era source/context
assumptions from provider payload, database storage, request-body, and
denial-of-service guards. Profile files must not gain authority over those fixed
security and resource boundaries.
