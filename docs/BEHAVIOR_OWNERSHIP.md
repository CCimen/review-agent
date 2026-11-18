---
sidebar_label: Behavior ownership
slug: /behavior-ownership
title: Behavior ownership
description: What you can customize about the reviewer, where each change belongs, and what no configuration can change.
status: current
last_verified: 2026-11-17
---

# Behavior ownership

Most organizations need no profile of their own. One deployment uses the
neutral `default-standard` profile for every approved installation and
repository, and each repository can add its own `.review-agent/` package.

## What you can change, and where

| To change | Edit | Scope |
| --- | --- | --- |
| The reviewer's identity, tone, and stance when explaining risk | `SOUL.md` in the deployment profile | Every repository served by the deployment |
| Review rules: scope, severity, coverage, finding and suggestion format, feedback rules | `workspace/AGENTS.md` in the deployment profile | Every repository served by the deployment |
| The review procedure and tool sequence | `skills/review-agent-pr/SKILL.md` (code) and `skills/review-agent-docs/SKILL.md` (documentation) in the deployment profile | Every repository served by the deployment |
| Repository instructions, ordered technical context, accepted design decisions, documentation mappings | `.review-agent/` in the repository ([Repository context](./REPOSITORY_CONTEXT.md)) | One repository, from the pull request's base commit |
| Provider, model, and reasoning effort | `REVIEW_AGENT_MODEL_PROVIDER`, `REVIEW_AGENT_MODEL`, `REVIEW_AGENT_REASONING_EFFORT`, or the console **Settings**; teams can use separate model connections ([Admin panel](./ADMIN_PANEL.md)) | The deployment, or one team |

The console records the model route of each admitted request with that
request. A saved route change applies to new requests only.

## What no configuration can change

Authorization, the model's tool set, the reviewed base and head commits and
their re-checks, persistence, the finding gates, and GitHub publication are
fixed in code and in the managed `hermes/config.yaml`. A profile manifest
cannot name a model, provider, tool, or permission, and repository content
cannot change any of them. Fixed GitHub headings and hidden markers are part of
the protocol, not profile templates. See [How reviews work](./HOW_REVIEWS_WORK.md).

## Create a deployment profile

Create another profile only when an environment needs a different shared
identity or fixed review policy. Profiles are part of the image, so a custom
profile means building your own image from this repository.

1. Copy `profiles/default-standard/` to
   `profiles/<key>/`. The key uses lower-case words separated by
   hyphens.
2. Edit the files. A profile contains:
   - `SOUL.md`;
   - `workspace/AGENTS.md`;
   - `profile.json` with exactly `schema_version` (`1`) and `skills`, a list
     of skill keys without duplicates;
   - `skills/<key>/SKILL.md` for each listed skill.
3. Keep `review-agent-pr` and `review-agent-docs` in `skills`. The installer
   rejects a profile missing either one.
4. Build the image and set `REVIEW_AGENT_PROFILE=<key>`. Compose passes it to
   the services, and the one-shot `review-profile-install` service installs
   the profile into `HERMES_HOME` before the reviewer starts.

The installer checks the profile's structure and that every listed skill
exists. It does not interpret their text. It writes a receipt of the installed
profile and skills, and on the next install it removes skills that the previous
profile installed and the new one does not list. Outside Compose, the installer
reuses the receipt's profile when `REVIEW_AGENT_PROFILE` is not set.

## Changing `SOUL.md` and `AGENTS.md`

A change to `SOUL.md` or `AGENTS.md` affects every repository the deployment
serves, so review it as a policy change. Put stable review rules and the
visible comment contract in `AGENTS.md`, and procedure in the skill. A team
that wants different wording or focus for its own repository uses
`.review-agent/instructions.md` instead.

`SOUL.md` follows Hermes'
[personality and `SOUL.md`](https://hermes-agent.nousresearch.com/docs/user-guide/features/personality)
conventions; see also its
[guide to a custom soul](https://hermes-agent.nousresearch.com/docs/guides/use-soul-with-hermes).

Feedback commands and design decisions are described in
[Feedback and design decisions](./FEEDBACK_AND_DECISIONS.md).
