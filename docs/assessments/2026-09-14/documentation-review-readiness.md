# Documentation review rollout evidence

The source candidate implements documentation review as an advisory capability.
Automatic production spending remains disabled. Deterministic tests establish
source, state, permission, and publication behavior; they do not establish model
accuracy or usefulness.

This record is updated at the release boundary. Beads `ra-docs-review-ms1.9`
remains open until the labelled model pilot is run and reviewed by maintainers.

## Candidate and rollout

The intended release is `v0.4.0-rc.7`. Its exact commit and qualified runtime/admin
image digests must be recorded after the commit gate and release workflow finish.
The migration boundary is 035; the managed profile includes `review-agent-docs`.
Deploy all services from one qualified source/image pair, drain existing work,
back up PostgreSQL, stop old applications, apply the bundled migrations, install
the matching profile, and verify service readiness before resuming admission.

The authorized deployment target is the existing Review Agent production Compose
application in Dokploy. Preserve its secrets, networks, volumes, repository grants,
and saved operating choices. Update the docs site from `main` only after the
qualified release evidence exists. Use the existing deployment and release
procedures in [Operations](../../OPERATIONS.md#updating-and-validation).

The deployment-level documentation switch defaults off. Do not broaden App grants,
change repository modes, or trigger reviews of unrelated pull requests during an
upgrade. The GitHub owner must accept Checks write where missing. Choose one
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
  and TypeScript checks pass locally. Browser visual verification is unavailable
  because the browser administrator denied access to the local fixture; no
  alternate browser surface was used.
- The public documentation manifest includes the documentation-review guide.
  Local site generation is distinct from verified public Pages publication.

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
