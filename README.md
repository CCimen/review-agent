# Review Agent

Self-hosted, advisory AI code review for GitHub pull requests, built on Hermes
with deterministic controls.

[![Python bundle](https://github.com/CCimen/review-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/CCimen/review-agent/actions/workflows/ci.yml)
[![Publish documentation](https://github.com/CCimen/review-agent/actions/workflows/docs-pages.yml/badge.svg)](https://github.com/CCimen/review-agent/actions/workflows/docs-pages.yml)
[![Documentation](https://img.shields.io/badge/docs-ccimen.github.io-blue)](https://ccimen.github.io/review-agent/)

An authorized maintainer comments `/review` on a pull request. The service pins the
exact base/head snapshot, reviews it in two passes through bounded read tools,
and posts evidence-backed findings through a deterministic publisher. Reviews
stay advisory: the reviewer has no shell, no repository write access, and no
merge authority.

![Four phases of a review: request and authorize, read and review, verify and publish, then re-review with explicit feedback.](website/static/img/review-lifecycle.webp)

## Quick start

1. **Deploy the service** with Compose, Dokploy, Coolify, Portainer, or
   OpenShift: [deployment guide](docs/DEPLOYMENT.md).
2. **Approve the GitHub App installation once**. The recommended
   organization-managed mode covers current and future repositories without a
   per-repository operator command:

   ```bash
   docker compose exec review-github-gateway \
     review-agent-admin github-app approve <installation-id> \
     --actor "github:<operator>" \
     --reason "approved organization-managed reviews"
   ```

   New installations remain locked until this approval. An explicit
   per-repository mode is also available; [Getting
   started](docs/GETTING_STARTED.md) explains both choices.
3. **Request a review** with a new top-level PR comment:

   ```text
   /review
   ```

Repositories work with the neutral baseline by default. Teams that need local
review principles, platform context, or ADR evidence can copy the explicit
`.review-agent/` starter and validate it offline; see [Repository
context](docs/REPOSITORY_CONTEXT.md).

The GitHub App receives review commands and gives the private gateway short-lived,
repository-scoped credentials for source reads and deterministic publication.
The model and publisher never receive the App private key.

After fixing findings, push a commit and comment `/review` again; the new
snapshot starts a fresh round. Collaborators with current write or admin
permission post structured feedback in a new top-level comment:

```text
/review false-positive F2 because <what code, guard, or invariant disproves it>
/review intentional F2 ADR-0007 because <why the accepted ADR requires this design>
/review feedback scope F2 because <why this finding is outside the intended PR scope>
/review feedback missed because <what concrete issue was missed and where>
```

A [sanitized example review](examples/comments/example-review.md) shows the
comment shape a pull request receives.

## Documentation

The [documentation site](https://ccimen.github.io/review-agent/) has local
search and task-based navigation. Key pages:

| You want to | Read |
| --- | --- |
| Run your first review | [Getting started](docs/GETTING_STARTED.md) |
| Deploy the service | [Deployment](docs/DEPLOYMENT.md) |
| Install and approve the GitHub App | [GitHub App setup](docs/GITHUB_APP_PILOT.md) |
| Set up with a coding agent | [AI-assisted setup](docs/AI_ASSISTED_SETUP.md) |
| Understand the lifecycle | [How reviews work](docs/HOW_REVIEWS_WORK.md) |
| Change voice or review rules | [Behavior ownership](docs/BEHAVIOR_OWNERSHIP.md) |
| Add repository instructions and platform context | [Repository context](docs/REPOSITORY_CONTEXT.md) |
| Operate or recover it | [Operations](docs/OPERATIONS.md) |
| Assess trust boundaries | [Security](docs/SECURITY.md) |
| See capabilities and boundaries | [Capabilities](docs/ROADMAP.md) |

Successful container release workflows publish attested `linux/amd64` and
`linux/arm64` images, SBOMs, and per-platform vulnerability reports. Compose can
either build locally or use an immutable release tag through
`REVIEW_AGENT_IMAGE`; see
[Deployment](docs/DEPLOYMENT.md#choose-an-image).

## What it does

- Reviews the exact base/head snapshot of an open PR through bounded plugin
  tools: metadata, diffs, changed-file lists, and selected files.
- Publishes every evidence-backed finding that survives a skeptical second
  pass, and reports incomplete coverage instead of implying a clean review.
- Re-checks unresolved findings from earlier rounds on each new review.
- Offers small, independently safe patches through GitHub's native suggestion
  UI; coordinated changes go into a copyable coding-agent brief.
- Stores runs, findings, coverage, publication state, and human feedback in
  PostgreSQL, then freezes exact comment parts for a separate publisher with a
  recoverable lease. Interrupted delivery resumes from durable state.

## What it does not do

- Gate merges. Reviews stay advisory unless your organization makes a separate
  merge-policy decision.
- Replace CodeQL, Dependabot, dependency review, or CVE scanning. Those stay
  independent CI controls; [docs/SECURITY.md](docs/SECURITY.md) owns the
  dependency-scanning boundary.
- Execute contributor code, or give the model a shell, browser, delegation, or
  arbitrary GitHub write access.

## Configuration

> **Behavior TL;DR:** One deployment normally uses the neutral
> `default-standard` profile for every approved organization and repository.
> Teams customize a repository with its optional `.review-agent/` package.
> Create another deployment profile only when an environment needs a different
> shared identity or fixed review policy; GitHub App installation controls
> access, not reviewer behavior.

The reusable part is the review engine: webhook admission, bounded GitHub
reads, PostgreSQL review state, deterministic publication, and operator
tooling. Voice and review policy live in a swappable profile; select one with
`REVIEW_AGENT_PROFILE=<profile-key>`. The shipped `default-standard` bundle
is the neutral default deployment profile, not an organization identity.
Repository-specific principles and technical context live in the optional,
explicitly indexed `.review-agent/` package.

The deployment selects the Hermes provider, model, and reasoning effort with
`REVIEW_AGENT_MODEL_PROVIDER`, `REVIEW_AGENT_MODEL`, and
`REVIEW_AGENT_REASONING_EFFORT`. The defaults use `openai-codex`,
`gpt-5.6-sol`, and `xhigh`. Hermes can authenticate this route with a ChatGPT
device-code login, so the recommended setup needs no model API key. Anthropic
Claude, API-key services, and self-hosted OpenAI-compatible endpoints use the
same deployment settings and Hermes-owned credentials. Check Hermes'
[provider guide](https://hermes-agent.nousresearch.com/docs/integrations/providers),
[model catalog](https://hermes-agent.nousresearch.com/docs/reference/model-catalog),
and [environment reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)
before selecting a route.

[Behavior ownership](docs/BEHAVIOR_OWNERSHIP.md) maps every concern to its
owner and explains how to create a profile. Authorization, snapshot checks,
persistence, and publication rules are engine configuration; a profile cannot
change them.

## Operations and security

[docs/OPERATIONS.md](docs/OPERATIONS.md) owns runtime limits, recovery,
backups, updates, and operator commands. [docs/SECURITY.md](docs/SECURITY.md)
owns the trust model, prompt-injection posture, token boundaries, and
dependency-scanning scope.

The short version: the reviewer is useful because it has narrow tools and
durable audit state. Keep deterministic scanners, tests, type checks, and
human ownership as separate controls.

## Development

Run the full validation bundle before shipping changes:

```bash
./scripts/check_bundle.sh
```

GitHub Actions runs the same bundle for pull requests and pushes to `main`,
builds the container, and verifies the pinned Hermes contracts used by durable
execution. It cannot live-test your deployed routes, GitHub token approval,
ChatGPT OAuth state, or repository rules.

## License

Review Agent is licensed under `EUPL-1.2` (Version 1.2 only). Copyright © 2026
Çağrı Çimen and contributors. Third-party components retain their original
licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Maintainers publish versioned images from GitHub releases. Follow
[RELEASING.md](RELEASING.md) for the release gate, GHCR verification, and
rollback checks.
