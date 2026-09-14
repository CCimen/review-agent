# Documentation review rollout evidence

Stable release `v0.4.0` implements documentation review as an advisory capability
and is deployed. Documentation review remains disabled in production pending
GitHub App setup and pilot acceptance. Deterministic tests establish
source, state, permission, and publication behavior; they do not establish model
accuracy or usefulness.

This record is updated at the release boundary. Beads `ra-docs-review-ms1.9`
remains open until the labelled model pilot is run and reviewed by maintainers.

## Candidate and rollout

The [qualified release](https://github.com/CCimen/review-agent/releases/tag/v0.4.0)
uses source `75ed0ff80609ca56defb7f5574d7a7cc9808ed35`.
Its [image workflow](https://github.com/CCimen/review-agent/actions/runs/34876470601)
passed, and all 24 entries in the release checksum manifest were verified.

| Image | Deployed manifest digest |
| --- | --- |
| `ghcr.io/ccimen/review-agent` | `sha256:11d35faf8b3b1762dc9fa6f4cb8ac060363fa606985bed7570abc4e47c95b0c9` |
| `ghcr.io/ccimen/review-agent-admin` | `sha256:7e9ca026ca27df94fe451558a9f41bd21f85ab39eed5d9b6b2b78ec71366b92c` |

The existing Dokploy deployment was upgraded after draining work and verifying
a PostgreSQL backup restore. All 42 retained review request IDs survived the
upgrade. Migration 035 and runtime-role readiness passed; the managed profile
matches the release and includes `review-agent-docs`. All 11 services were
verified, including successful migration/profile jobs, before review intake
resumed. Review, publication, and webhook queues were empty with one live worker
each and no failures or dead letters at the final check.

The review worker now has the private gateway URL and network access described
in [Deployment](../../DEPLOYMENT.md#upgrade-and-roll-back-production). This required
Compose wiring was applied to the running deployment; the protected environment
file was unchanged. The stable release includes this wiring in both templates.
An obsolete `pull_policy: never` on the saved Dokploy admin service initially
skipped its image pull. Startup with an explicit pull succeeded, and the saved
override was removed. The earlier rc.7 release failed image qualification and
must not be deployed.

The public console health endpoint returned ready. Its index and 25 assets
matched the deployed admin image by SHA-256. The
[documentation site](https://ccimen.github.io/review-agent/docs/documentation-review)
was published from `main`: all 19 public documentation routes and both generated
LLM documentation files returned HTTP 200. These checks verify delivery, not
browser rendering or a live model review.

A post-upgrade dry run against an enabled repository's open, same-repository PR
verified GitHub App read/publication authority and available capacity. It made
no model call or GitHub write. The backup restored successfully into a temporary
database with all 42 request IDs; that database was removed after verification.

The deployment-level documentation switch defaults off. Do not broaden App grants,
change repository modes, or trigger reviews of unrelated pull requests during an
upgrade. The deployed GitHub App still needs **Checks: write** and the **Pull
request** and **Check run** event subscriptions. The GitHub owner must update its
registration and accept the installation permission where required. Choose one
explicit pilot repository and representative PR before enabling Manual. Automatic
adoption follows useful pilot evidence; saving Automatic does not sweep open PRs.

Disable documentation review through its deployment switch or the relevant
repository mode. Keep retained results and migrations. Recover across an
incompatible image change by restoring the matching verified backup and image
pair; do not reverse populated purpose or evidence migrations in place.

## Existing deterministic evidence

- `tests/test_documentation_scope.py` uses temporary Git histories for accepted
  base versus head proposals, exact refs, dirty-worktree isolation, renames,
  deletion, literal paths, symlinks, and bounded provider input.
- `tests/test_documentation_preflight.py` distinguishes explicit deterministic
  exclusions from unknown or incomplete scope before model execution.
- `tests/test_documentation_findings.py` and
  `tests/test_postgres_documentation_reviews.py` verify exact citations, unchanged
  and missing documents, coverage, accepted ADR provenance, immutable receipts,
  lease fencing, and revoked authority.
- Existing publication and gateway tests cover Check Run recovery, acknowledged
  overflow parts, incomplete scans, and stale-head cancellation.
- `tests/test_documentation_operating_policy.py` and
  `tests/test_documentation_configuration.py` cover inheritance, overrides,
  global stop, scoped permissions, stale saves, ownership changes, exact default
  branch inspection, and refresh ordering.
- Existing reporting and console tests cover purpose filters, exact request
  evidence, recorded token totals, and unknown usage. Production frontend builds
  and TypeScript checks pass locally. The contributor reported no horizontal
  overflow at 390×844 on seven routes and checked navigation, documentation
  panels, and the reader in light and dark modes. This was not repeated by the
  release operator, whose browser access to the local fixture was denied.
  Touch-target sizes, landscape, and Settings/Users/Audit mobile layouts remain
  unaudited.
- The public documentation manifest includes the documentation-review guide;
  public Pages delivery was verified separately from local generation.

The documentation-review implementation and rc.8 packaging commit gates passed
with Claude Opus 5 at xhigh effort.
Validation included the bundle suite (1,017 tests, 406 skipped without the
PostgreSQL test environment), 389 PostgreSQL contract tests with populated
migration and restore checks, 36 console tests, strict Python and TypeScript
checks, generated API contracts, and the 19-route documentation site build.
The stable candidate also passed Astra source acceptance and 34 focused
PostgreSQL reporting/policy tests. The complete canonical CI and exact-image
vulnerability policy passed again in the stable release workflow.

Final bundle, migration, admin, site, Claude gate, release, and deployment receipts
are retained outside the repository. Do not treat earlier partial implementation
receipts as proof of the final candidate.

## Pilot cases awaiting maintainer labels and model execution

The following is a proposed 24-case set, not a labelled benchmark or an executed
model report. Each case needs exact base/comparison/head commits and policy hash,
maintainer-confirmed obligations, and a retained normal-procedure review receipt.
Use synthetic Git histories for destructive/error cases and approved historical
PRs where access and labels are available. Keep private source and raw provider
logs out of this public repository.

| Case | Expected obligation for maintainer confirmation |
| --- | --- |
| Changed default, unchanged guide | Identify the now-incorrect documented default. |
| Changed default, corrected guide | Audit a no-mismatch result. |
| Renamed configuration key | Identify stale operator examples and migration guidance. |
| Added optional parameter | Decide whether the mapped user guide needs an update. |
| Removed CLI command | Identify obsolete setup or recovery instructions. |
| Changed failure behavior | Check the documented error and recovery path. |
| Docs-only false claim | Find a newly introduced claim contradicted by exact code evidence. |
| Docs-only clarification | Audit a clean result without demanding unrelated edits. |
| Incorrect code versus accepted ADR | Preserve the architectural conflict for a human decision. |
| Accepted code change, stale guide | Do not suppress the guide mismatch because the code is intentional. |
| Superseded ADR | Do not treat it as accepted authority. |
| Unavailable ADR evidence | Report the applicable limitation. |
| Unrelated old documentation debt | Exclude unchanged debt from PR findings. |
| Worsened old mismatch | Explain the newly worsened reader impact. |
| Fully excluded fixture changes | Audit a deterministic skip with no semantic call. |
| Mapped source also matches exclusion | Preserve the explicit mapping. |
| Unmapped source with no docs impact | Require evidence for the no-impact decision. |
| Unmapped public behavior change | Surface the obligation or an honest scope limitation. |
| Renamed source or guide | Inspect both previous and current paths. |
| Deleted required guide | Use a real comparison/source anchor. |
| First configuration addition | Preview the proposal without activating its own policy. |
| Head weakens accepted policy | Continue with accepted base policy. |
| Partial source or diff evidence | Preserve an incomplete result. |
| Follow-up changes only docs | Reassess the full current PR, including retained source changes. |

For each executed case, report useful findings, false positives, misses, audited
clean/skip outcomes, evidence gaps, queue delay, review duration, and recorded
model usage separately. A skipped or unrun case supplies no model-quality evidence.
Maintainers decide whether the signal is useful enough to enable Automatic more
widely; no universal accuracy percentage is assumed.
