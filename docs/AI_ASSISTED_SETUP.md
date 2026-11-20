---
sidebar_label: AI-assisted setup
slug: /ai-assisted-setup
title: Set up with a coding agent
description: What to give Codex, Claude Code, or another coding agent to install Review Agent, what it may do, and which steps stay with you.
status: current
last_verified: 2026-11-17
---

# Set up with a coding agent

A coding agent with access to your source and deployment tools can prepare,
deploy, and verify Review Agent. It follows the versioned installation skill,
`skills/install-review-agent/SKILL.md`, from the exact release it checks out.
The Codex and Claude Code copies are included in the repository, so you do not
install the skill yourself. You keep the approvals and secrets listed below.

## Give this assignment to your agent

```text
Set up Review Agent. If an exact source checkout was not provided, read
https://ccimen.github.io/review-agent/llms.txt, clone the source repository,
and check out the exact release declared there before doing any setup work.
Follow the repository-local skills/install-review-agent/SKILL.md; its Codex and
Claude Code mirrors are already included in the checkout, so do not install a
floating global copy. Read only the documentation needed for my deployment
platform. Prepare and validate the non-secret installation plan, then show one
concise mutation and rollback plan before external changes. Resolve facts you
can inspect and group missing owner decisions into one request. Never ask me to
paste secrets into chat. Pause only when I must approve the GitHub App,
repository access, secret placement, DNS, model login, deployment, or the first
live /review. Finish with doctor, inventory, dry-run, and one approved
live-review result. Report exact versions and stable IDs. On Dokploy, use Deploy
when the source revision changes and verify the completed deployment commit. If
a team wants repository-specific review guidance, use the versioned
examples/repository-context/.review-agent starter and validate it with
review-agent-admin repository-context validate; never invent or auto-discover
policy files. Mark unknowns as incomplete instead of guessing.
```

Add the target platform, public hostname, GitHub owner, installation scope,
any explicit repositories, and the deployment tools the agent may use. The agent
records the remaining non-secret choices in a validated installation plan.

The assisted path covers Compose-based installations, including Dokploy: the
installation plan accepts only the `compose` platform. For Kubernetes, follow
the manual Helm procedure in [Deploy Review Agent](./DEPLOYMENT.md#deploy-with-kubernetes-helm).

For the optional [admin panel](ADMIN_PANEL.md), ask for it in the assignment.
The agent can prepare the Compose service and hostname from the same release as
the runtime image. You enter the first platform-owner password in the container
terminal; it never goes into the plan.

## Configure only one repository

If Review Agent is already deployed, give a coding agent this shorter assignment:

```text
Configure repository-owned Review Agent guidance for this repository. Do not
change the deployment, GitHub App, installation policy, model, or secrets. Read
https://ccimen.github.io/review-agent/llms.txt, check out the exact release it
declares, and use that checkout's skills/install-review-agent/SKILL.md section
"Configure one repository". This mode assumes the target repository is already
covered by an approved Review Agent installation; if it is not, report that
access prerequisite instead of changing the installation. Copy
examples/repository-context/.review-agent only when the target has no
.review-agent/ directory. Otherwise preserve the existing package and edit it
in place. Keep instructions in instructions.md, list technical context
explicitly in config.toml, and store indexed ADRs under
.review-agent/decisions/. Do not invent or auto-discover policy files. Prepare
the environment and validate the target repository with the commands that
section gives for the release you checked out. Show the repository diff and
bounded validation receipt for normal review. The new guidance becomes active
only after it is merged and a later pull request reads it from the base commit.
```

This path needs no Dokploy access, model login, database connection, or GitHub
App administration. The repository owner reviews and merges the package like
other project policy. See [Repository context](./REPOSITORY_CONTEXT.md).

## What the agent may do

| The agent can | A human owner must approve or complete |
| --- | --- |
| Validate non-secret decisions | GitHub App creation and installation |
| Generate a prefilled App registration URL | GitHub installation scope and one Review Agent installation approval |
| Prepare Compose or Dokploy changes | Secret placement and model login |
| Run local preflight and live doctor checks | DNS or production deployment approval |
| Approve one installation or onboard an explicit repository | The first real `/review` and feedback test |
| Validate a proposed `.review-agent/` package offline | Approve and merge repository-owned guidance or decisions |

The agent must not classify feedback or change the reviewer profile without
your decision.

> [!IMPORTANT]
> Do not paste a private key, webhook secret, database credential, internal API
> key, model credential, or backup credential into chat. Put secrets directly
> in the deployment platform's protected configuration.

## Where you act

1. **Approve the mutation plan.** Before it changes GitHub or your deployment,
   the agent shows one short plan naming the exact image digest or commit, the
   deployment project, services and public route, the App owner, permissions,
   events, installation scope and activation policy, secret names (never
   values), migration, backup, verification and rollback steps, and every
   remaining human action. Approve only the named scope.
2. **Register and install the App.** Open the prefilled registration URL the
   agent generates and submit it, then install the App. GitHub installation
   scope and Review Agent activation are separate decisions:

   | Owner action | Result |
   | --- | --- |
   | Install the App with **All repositories** or **Only select repositories** | GitHub defines where the App may request a token. Reviews remain locked for a new installation. |
   | Run `github-app approve <installation-id>` | The trusted installation activates an exact repository on its first signed `/review` or feedback command. [Automatic activation](./GITHUB_APP_PILOT.md#automatic-approve-once) says whose command can do that in each release. |
   | Keep explicit mode and run `github-app onboard <owner/repository>` | Only that named repository is verified and enabled, with either GitHub installation scope. |

   [Set up and share the GitHub App](./GITHUB_APP_PILOT.md) owns the details.
3. **Place the secrets** in the platform's protected configuration and mount
   the App private key as [Deploy Review Agent](./DEPLOYMENT.md) describes.
4. **Log in to the model provider.** For a browser or device login, the agent
   gives you the exact container command and waits until you confirm.
5. **Approve DNS and the production deployment.**
6. **Post the first `/review`** as a new top-level comment on a test pull
   request. The agent's dry run needs a repository that is already enabled. If
   it is, for example after `github-app onboard`, post once the dry run has
   passed. On a fresh automatic installation no repository is enabled yet:
   post the first `/review` when the agent asks, and the agent runs the dry run
   after that review has enabled the repository. Test feedback commands only if
   you want to validate that path.

## What you receive

The agent finishes with a report in the skill's completion format: deployed
release and digest, App and installation IDs, activation policy, doctor, queue,
dry-run and live-run results, backup and rollback state, and any remaining owner
action. It contains no secrets. If any of these is unknown, the setup is
incomplete.
