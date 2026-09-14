---
sidebar_label: Documentation review
slug: /documentation-review
title: Keep documentation aligned with pull requests
description: Configure scoped documentation reviews, understand advisory GitHub checks, and monitor review usage.
status: current
last_verified: 2026-09-14
---

# Keep documentation aligned with pull requests

Documentation review checks whether a pull request changes behavior that its
maintained documents need to explain. It uses the same Review Agent deployment,
GitHub App, model connections, and review queue as code review. It has its own
request history, feedback, and **Documentation review** check.

Start with manual reviews in one repository. Add explicit relationships between
source areas and maintained documents, try representative pull requests, and
correct scope mistakes before enabling automatic reviews for a team.

## Enable a repository

1. Update all Review Agent services and the managed profile together, using the
   [upgrade procedure](OPERATIONS.md#backup-and-recovery). Documentation review
   starts disabled at the deployment level. Custom profiles must include the
   reviewed `review-agent-docs` skill and list it in `profile.json`; installation
   verifies both managed procedures.
2. Give the GitHub App **Checks: write**, in addition to its existing repository
   permissions. Subscribe to **Pull request** and **Check run** as well as
   **Issue comment**. An existing installation may need its GitHub owner to
   accept the new permission. Changing a console mode cannot grant GitHub access.
3. An owner enables documentation review in **Settings**. A team maintainer then
   sets the team's default in **Teams → Documentation**, or chooses a repository
   override in **Repositories → Documentation**.
4. Add `.review-agent/documentation.toml` to the repository through a normal pull
   request. Adapt the [starter configuration](https://github.com/CCimen/review-agent/blob/main/examples/repository-context/.review-agent/documentation.toml)
   to actual source areas and exact document paths. Validate the package with
   [repository-context validate](REPOSITORY_CONTEXT.md#start-with-the-copyable-package).
5. Merge the configuration. Post `/review docs` as a new top-level comment on an
   open pull request. The requester needs current repository write or admin
   permission. Fork pull requests remain unsupported.

A pull request that first adds the configuration can preview the proposed scope,
but cannot activate its own rules. Review Agent uses the accepted configuration
at that pull request's exact target-base commit. Policy changes take effect in
later reviews whose base contains the merged change.

## Choose when reviews run

| Mode | Behavior |
| --- | --- |
| Off | Documentation review is disabled for the repository. |
| Manual | A developer requests a review with `/review docs`. |
| Automatic | Eligible pull-request updates schedule reviews; `/review docs` remains available for an explicit request. |

Repositories inherit their team's default unless they have an explicit override.
An unassigned repository defaults to Manual. The deployment switch overrides all
team and repository modes. The console displays configured and effective modes
separately, so a deployment pause does not erase saved choices.

Changing a team default shows the inherited repositories it affects and the
repositories with exceptions. A team default of Off affects inherited repositories; explicit Automatic
overrides remain enabled. Use the deployment switch for a deployment-wide stop.
Repository transfers show the resulting review
mode and model-account change before saving. A stale settings or transfer preview
must be refreshed before it can be applied. Viewers can inspect these settings;
only authorized maintainers or administrators can change them.

Automatic review waits about two minutes after an eligible update so a series of
pushes can settle. Draft pull requests do not start automatic reviews. Opening,
reopening, marking ready, changing the head, or changing the target base can
schedule work. The worker verifies current pull-request state before executing.
An explicit request bypasses the waiting period and can promote equivalent
pending work. Turning on Automatic does not scan and review every existing open
pull request.

These controls limit unnecessary model calls. A deterministic scope check runs
first. Missing or invalid configuration, unavailable inputs, and complete changes
covered entirely by explicit exclusions receive their appropriate result without
a semantic model call. Unmapped changes require assessment; an unknown source
path is not evidence that documentation is unaffected.

## Understand the result

The GitHub check is advisory. Review Agent does not edit files, commit fixes, or
open a documentation pull request. Developers apply the suggested corrections
and request another review when ready. Do not make this check a required branch
protection rule as part of initial adoption.

| Result | GitHub conclusion | Meaning |
| --- | --- | --- |
| No documentation review needed | Skipped | Complete scope analysis established that the changes were explicitly excluded; no semantic call was needed. |
| No documentation mismatch found | Success | The selected scope was assessed with complete evidence and no retained mismatch. |
| Documentation changes recommended | Neutral | The report contains evidence-backed corrections for a developer to consider. |
| Incomplete, unavailable, or configuration needs attention | Neutral | The report explains the gap; it does not claim complete verification. |

A successful check is limited to the selected scope and retained evidence. It
cannot certify all documentation in a repository. Review Agent can inspect an
unchanged guide affected by changed behavior, and can report a missing or deleted
guide using a real source location. It never invents a line in a missing file.

The report belongs to the exact reviewed head commit. If that subject becomes
stale during delivery, it cannot publish as the current result. A retained check
on an older commit remains historical evidence. Most reports fit in the check;
large reports use linked comment parts, with the complete retained report also
available in console history.

On a later review, a prior finding is marked resolved only when current document
evidence supports the correction. Findings outside the new scope or without enough
evidence remain not checked. Removing a mapping does not resolve its findings.

Code review remains available through `/review`. F references remain unique across the pull request; code and documentation
reviews accept feedback only for their own current publication. Use the documentation prefix when sending feedback:

```text
/review docs false-positive F1 <reason>
/review docs feedback scope <reason>
/review docs feedback missed <reason>
```

## Keep rules, guidance, and ADRs in their existing places

`.review-agent/documentation.toml` owns source-to-document relationships, their
intent, and explained exclusions. [`config.toml`, `instructions.md`, and `context/`](REPOSITORY_CONTEXT.md)
continue to own repository guidance. [Accepted ADRs](FEEDBACK_AND_DECISIONS.md)
provide evidence of architectural intent from the same exact base snapshot.
They are not copied into the documentation configuration.

An accepted ADR can explain a deliberate behavior change. It does not excuse a
user guide that describes different behavior. Documentation findings therefore
do not support intentional-by-design suppression. Proposed or superseded ADRs
cannot supply accepted authority, and an unavailable ADR context is reported as
a limitation where it affects the assessment.

Protect changes to the repository package through normal review and CODEOWNERS.
Repository text remains untrusted evidence: it cannot change tool permissions,
publication behavior, scope completeness requirements, or authorization.

## Inspect configuration and usage in the console

**Repositories → Documentation → Refresh** reads the default branch and then
inspects the configuration at that exact commit. The page shows the read time,
commit, parsed areas and exclusions, and any syntax problem. This is a saved
inspection for operators. It does not execute a review, crawl the repository,
verify every mapped document, or replace a review's base-commit policy.

Activity, history, and usage can be filtered by Code or Documentation. Quality
views start with Code and keep documentation results in a separate cohort.
Opening a documentation request shows its outcome, selected scope, exact refs,
evidence limitations, and whether a semantic assessment ran.

Each retained request also shows recorded token usage and duration. Token figures
come from provider usage records; missing usage is shown as unavailable rather
than zero. The request summary shows recorded input, output, and total tokens.
History keeps these figures attached to the request that consumed them, including
retries where usage was recorded. This makes a manual pilot useful for estimating
cost before automatic review is enabled more widely.

A [Review Agent-specific starter](https://github.com/CCimen/review-agent/blob/main/examples/documentation-review/review-agent.toml)
shows relationships for this project's real review commands, context owners,
and deployment files. Renaming a mapped document requires updating its exact
path in the configuration through the same reviewed change; accepted base rules
still govern that PR's review.

## Use deterministic checks in CI

Run the existing local validator in CI when `.review-agent/` or its referenced
files change. Use `repository-context docs-scope --base <commit> --head <commit>`
to inspect scope from committed revisions without a model or GitHub credentials.
The [configuration reference](REPOSITORY_CONTEXT.md#preview-documentation-scope-from-committed-revisions)
explains this receipt and the merge-base comparison.

Keep documentation builds, link checks, generated API checks, tests, and type
checks in their existing CI owners. Documentation review adds semantic feedback;
it does not regenerate public contracts or replace those deterministic checks.
