# Product Context

<!--
impeccable_context_version: 1
surface: web documentation
audience: Sundsvall municipality platform operators and repository maintainers
-->

## Product

Sundsvall Review Agent is an organization-wide, advisory pull-request reviewer.
It combines bounded LLM reasoning with deterministic authorization, snapshot,
memory, and GitHub publication controls.

## Documentation Outcome

The documentation site must let a new operator or repository maintainer answer
five questions quickly:

1. What does the reviewer do, and what does it deliberately not do?
2. How does a review move from `/review` to a published GitHub comment?
3. How is a repository added to the current deployment?
4. Which files own identity, review rules, procedure, and runtime wiring?
5. Which capabilities are current, planned, or explicitly deferred?

## Audience And Use Scene

- Platform operators deploy and maintain the shared service.
- Repository maintainers connect repositories and request reviews.
- Developers read findings, apply suggestions, give feedback, and request a new
  review round.

Readers are usually looking for a concrete answer while configuring or
debugging the service. The site should feel calm, trustworthy, and quick to
scan, with long-form reading treated as the primary task.

## Current Product Truth

- One deployment-wide reviewer profile serves an explicit repository allowlist.
- GitHub Actions authorizes trusted commenters and sends HMAC-signed webhooks.
- The live model receives bounded GitHub read tools, no shell, and no arbitrary
  GitHub writer.
- SQLite owns durable review, publication, and feedback state.
- Deterministic code rechecks the PR snapshot and publishes comments and safe
  native suggestions.
- Reviews are advisory and are not a default merge gate.

## Roadmap Boundary

Trusted base-branch repository context, PostgreSQL, durable jobs, a GitHub App,
and broader integrations are planned work. Scanner and Codex Security
integrations are explicitly deferred. The site must never present these as
shipped behavior.

## Content And Language

Existing canonical documentation is English, so the first release remains
coherently English. Docusaurus keeps the locale owner explicit so Swedish can be
added later as a complete translation rather than mixed into individual pages.
