---
sidebar_label: Documentation review
slug: /documentation-review
title: Keep documentation aligned with pull requests
description: Configure scoped documentation reviews, understand advisory GitHub checks, and monitor review usage.
status: current
last_verified: 2026-11-17
---

# Keep documentation aligned with pull requests

Documentation review checks whether a pull request changes behavior that its
maintained documents need to explain. It uses the same deployment, GitHub App,
model connections, and queue as code review, with its own request history,
feedback, and **Documentation review** check.

Start with manual reviews in one repository, map source areas to documents,
try representative pull requests, and correct scope mistakes before turning on
automatic reviews for a team.

## Enable a repository

1. Deploy all services and the managed profile from the same release
   ([upgrade procedure](DEPLOYMENT.md#upgrade-and-roll-back-production)). A
   custom profile must list the `review-agent-docs` skill in `profile.json`;
   see [Behavior ownership](BEHAVIOR_OWNERSHIP.md#create-a-deployment-profile).
2. Give the GitHub App **Checks: write** and subscribe it to the **Pull
   request** and **Check run** events, in addition to **Issue comment**
   ([App permissions](GITHUB_APP_PILOT.md#1-register-the-app)). An existing
   installation's owner may need to accept the new permission. No console
   setting can grant GitHub access.
3. An owner switches documentation review on in the admin console under
   **Settings**. It is off by default, and the console is the only place to turn
   it on. A team maintainer then sets the team default in **Teams →
   Documentation**, or a repository override in **Repositories →
   Documentation**.
4. Add `.review-agent/documentation.toml` through a normal pull request. Adapt
   the [starter configuration](https://github.com/CCimen/review-agent/blob/main/examples/repository-context/.review-agent/documentation.toml)
   to real source areas and exact document paths, and check it with
   [repository-context validate](REPOSITORY_CONTEXT.md#start-with-the-copyable-package).
5. Merge it, then post `/review docs` as a new top-level comment on an open pull
   request. The same people who may request a code review may request this one
   ([who can request a review](GITHUB_APP_PILOT.md#who-can-request-a-review)).
   Pull requests from forks are not reviewed.

Review Agent uses the configuration at the pull request's target-base commit.
A pull request that adds or edits it can preview the proposed scope, but the
change applies only to later reviews whose base contains it.

## Choose when reviews run

| Mode | Behavior |
| --- | --- |
| Off | Documentation review is disabled for the repository. |
| Manual | A developer requests a review with `/review docs`. |
| Automatic | Eligible pull-request updates schedule reviews; `/review docs` still works. |

A repository inherits its team's default unless it has its own override. With
neither, it uses Manual. The deployment switch in
**Settings** overrides every team and repository mode; turning it off keeps the
saved modes, and the console shows configured and effective modes separately.
A team default of Off does not override a repository set explicitly to
Automatic. Viewers can see these settings; only maintainers and administrators
can change them. A stale settings or transfer preview must be refreshed before
it can be saved.

In Automatic mode, opening, reopening, marking ready, a new head commit, or a
changed target base schedules a review about two minutes later, so a series of
pushes settles first. Draft pull requests are skipped. The worker re-checks the
pull request before it runs. An explicit `/review docs` skips the wait.
Switching to Automatic does not review existing open pull requests.

A deterministic scope check runs before any model call. Missing or invalid
configuration, unavailable inputs, and changes covered entirely by explicit
exclusions get their result without a model call. Unmapped changes are
assessed: an unknown path is not evidence that documentation is unaffected.

## Understand the result

The check is advisory. Review Agent does not edit files, commit fixes, or open
pull requests. Do not make it a required status check while adopting it.

| Result | GitHub conclusion | Meaning |
| --- | --- | --- |
| No documentation review needed | Skipped | Every change is explicitly excluded; no model call was needed. |
| No documentation mismatch found | Success | The selected scope was assessed with complete evidence and no mismatch remains. |
| Documentation changes recommended | Neutral | The report lists evidence-backed corrections to consider. |
| Incomplete, unavailable, or configuration needs attention | Neutral | The report explains the gap and does not claim complete verification. |

A success covers only the selected scope. The reviewer can report an unchanged
guide that changed behavior affects, or a missing or deleted guide, using a real
source location; it never invents a line in a missing file. The report belongs
to the reviewed head commit and is not published as current if the head moved.

The result is in the pull request's **Checks** tab under **Documentation
review**. A neutral conclusion still shows as passing, so the report is also
posted as a pull request comment when the open findings change: the first time
changes are recommended, and when a later review resolves a finding, finds a new
one, changes a severity, or can no longer check one. A review that finds the
same open findings updates only the check. A report too large for the check is
always posted as linked comments.

When a pull request edits `.review-agent/documentation.toml`, the report says
it used the target branch's rules. When unmapped paths leave a review
incomplete, it suggests mapping or excluding them. A later review marks a
finding resolved only when the current documents support the correction;
findings outside the new scope stay not checked, and removing a mapping does
not resolve them.

## Feedback

Documentation findings take the `docs` prefix. F references are unique across
the pull request, and each command applies only to the current publication of
its own kind, so a code command cannot decide a documentation finding:

```text
/review docs false-positive F1 because <what disproves it>
/review docs feedback scope F1 because <why it is outside this PR>
/review docs feedback missed because <what was missed and where>
```

`/review docs intentional` is rejected: an accepted ADR can explain a behavior
change, but it does not make a guide that describes different behavior correct.
See [Feedback and design decisions](FEEDBACK_AND_DECISIONS.md) for what each
command changes and who may send it.

## Rules, guidance, and decisions

`.review-agent/documentation.toml` owns source-to-document relationships, their
intent, and explained exclusions. [`config.toml`, `instructions.md`, and
`context/`](REPOSITORY_CONTEXT.md) own repository guidance, and
[accepted ADRs](FEEDBACK_AND_DECISIONS.md#repository-decision-contract) supply
architectural intent from the same base commit. A superseded ADR, or an ADR
edited only on the pull request head, is not accepted authority, and missing
ADR context is reported where it affects the assessment.

Protect the package with branch review and CODEOWNERS. Repository text is
untrusted evidence: it cannot change tools, publication, scope completeness, or
authorization. When a mapped document is renamed, update its path in the same
pull request; the base rules still govern that pull request's review. The
[Review Agent starter](https://github.com/CCimen/review-agent/blob/main/examples/documentation-review/review-agent.toml)
shows the mappings this project uses for itself.

## Inspect configuration and usage in the console

**Repositories → Documentation → Refresh** reads the configuration at the
default branch's current commit and shows the commit, parsed areas and
exclusions, and any syntax problem. It does not run a review or check that every
mapped document exists.

Activity, history, and usage filter by Code or Documentation, and quality views
keep documentation results separate. A documentation request shows its outcome,
selected scope, commits, evidence limits, whether a model assessment ran, its
duration, and its recorded input, output, and total tokens (shown as unavailable
when the provider reported none). Use a manual trial to estimate cost before
turning on Automatic more widely.

## Use deterministic checks in CI

The [example workflow](REPOSITORY_CONTEXT.md#check-the-package-in-ci) validates
`.review-agent/` offline on every pull request and adds the
[documentation scope preview](REPOSITORY_CONTEXT.md#preview-documentation-scope-from-committed-revisions)
to the job summary, without a model or GitHub credentials.

Keep documentation builds, link checks, generated API checks, tests, and type
checks in their existing CI jobs. Documentation review adds semantic feedback
and does not replace them.
