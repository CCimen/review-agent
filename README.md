# Review Agent

Self-hosted, advisory AI code review for GitHub pull requests, built on Hermes
with deterministic controls.

[![Python bundle](https://github.com/CCimen/review-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/CCimen/review-agent/actions/workflows/ci.yml)
[![Publish documentation](https://github.com/CCimen/review-agent/actions/workflows/docs-pages.yml/badge.svg)](https://github.com/CCimen/review-agent/actions/workflows/docs-pages.yml)
[![Documentation](https://img.shields.io/badge/docs-ccimen.github.io-blue)](https://ccimen.github.io/review-agent/)

Deploy Review Agent once, approve a GitHub App installation, and maintainers
request reviews by commenting `/review` on a pull request. Repositories need no
workflow, secret, or local setup. Reviews are advisory: the model's tools read
the pull request and record findings in the service's database, and it has no
shell and no tool that writes to GitHub or merges.

For each request the service records the pull request's base and head commits,
lets the model read the diff and the files it asks for, checks every recorded
finding against fixed rules, and publishes the result through a separate
publisher. [How reviews work](docs/HOW_REVIEWS_WORK.md) describes each step and
which parts are code and which are the model.

![Review lifecycle: admission stores the signed delivery; the App worker authorizes it through the private GitHub gateway and queues a job; the review worker starts a Hermes session in which the model reads source and the review tools record findings; the publisher rechecks both commits and posts the review. PostgreSQL holds each handoff. The figure shows a review that fits one job.](website/static/img/review-lifecycle.svg)

## Start here

**Request a review.** If Review Agent already serves your repository, add a new
top-level comment to an open pull request whose branch is in the same
repository:

```text
/review
```

Users with GitHub `write` or `admin` permission on the repository can request
reviews. Others can be allowed per repository in the console. The
[example review](examples/comments/example-review.md) shows what is published.

**Run it for your organization.** Register the GitHub App, deploy the service
with the App's credentials, then install the App and approve the installation.
[Getting started](docs/GETTING_STARTED.md) walks through that order and the two
ways repositories are enabled.

**Assess it.** The [security model](docs/SECURITY.md) covers trust boundaries
and data handling, and [Capabilities](docs/ROADMAP.md) lists what is and is not
supported.

## Share it with other organizations

One deployment can serve several GitHub organizations. Make the App public,
send the install link, and approve each new installation; nothing is reviewed
before that approval. The other organization deploys nothing. Their code is
then processed by your deployment and model account, so read
[what is shared](docs/GITHUB_APP_PILOT.md#share-the-app-with-other-organizations)
first. An organization that wants its own data boundary runs its own deployment
with its own App.

## Use the review result

After fixing findings, push a commit and comment `/review` again. Maintainers
can answer a finding in a new top-level comment:

```text
/review false-positive F2 because <what code, guard, or invariant disproves it>
/review intentional F2 ADR-0007 because <why the accepted ADR requires this design>
/review feedback scope F2 because <why this finding is outside the intended PR scope>
/review feedback missed because <what concrete issue was missed and where>
```

See [Feedback and design decisions](docs/FEEDBACK_AND_DECISIONS.md) and a
[sanitized example review](examples/comments/example-review.md).

## What it does not do

- Gate merges. Reviews stay advisory unless your organization decides otherwise.
- Replace CodeQL, Dependabot, dependency review, tests, or type checks.
- Execute contributor code, or give the model a shell, browser, or GitHub write
  access.
- Review pull requests from forks, or start code reviews without a `/review`
  comment.

## Documentation

The [documentation site](https://ccimen.github.io/review-agent/) has search,
and the [Overview](docs/OVERVIEW.md) lists every guide by task.

## Configuration

A deployment selects one reviewer profile with `REVIEW_AGENT_PROFILE`; the
shipped `default-standard` profile is neutral and not tied to an organization.
Repository-specific rules belong in the repository's optional `.review-agent/`
package. [Behavior ownership](docs/BEHAVIOR_OWNERSHIP.md) explains what a
profile can and cannot change.

The model is set with `REVIEW_AGENT_MODEL_PROVIDER` (`openai-codex`, the
default, or `anthropic`), `REVIEW_AGENT_MODEL`, which is required, and
`REVIEW_AGENT_REASONING_EFFORT`; the
[deployment guide](docs/DEPLOYMENT.md#deploy-with-compose-or-dokploy) explains
each. `.env.example` lists the required values, and
[Operations](docs/OPERATIONS.md#configuration-reference) lists every variable
the Compose files read.

## Development

The runtime, admin API, and console live in this repository so API and schema
changes ship together:

```text
src/review_agent/             the application package; start reading here
hermes/                       what the image installs into Hermes: installer, managed configuration, patches
profiles/                     reviewer profiles
console/                      admin console frontend
tests/  scripts/              tests, CI checks and release tooling
docs/  website/  examples/    documentation, its site, deployment templates
```

[CONTRIBUTING.md](CONTRIBUTING.md#where-the-code-lives) describes each package
inside the application and which way its imports point.

```bash
./scripts/check_bundle.sh
```

is the fast local bundle, which includes Ruff, Pyright and the unit tests. CI
runs it and also checks the installed package, the admin frontend, the
PostgreSQL contract, the images and dependency vulnerabilities. See
[CONTRIBUTING.md](CONTRIBUTING.md), and [RELEASING.md](RELEASING.md) for how
images are built, scanned, and published.

## License

Review Agent is licensed under `EUPL-1.2` (Version 1.2 only). Copyright © 2026
Çağrı Çimen and contributors. Third-party components retain their original
licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
