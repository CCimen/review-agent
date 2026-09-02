---
sidebar_label: GitHub App setup
slug: /github-app-pilot
title: Set up the GitHub App
description: Register the App, approve an installation once, and choose automatic or explicit repository activation.
status: current
last_verified: 2026-09-02
---

# Set up the GitHub App

> **TL;DR:** Install the App on an organization, then approve that installation
> once with `github-app approve`. In the recommended organization-managed mode,
> a current or future repository activates on its first signed `/review` delivery;
> no per-repository Dokploy command is required. Explicit repository onboarding
> remains available for installations that need a narrower operator allowlist.

Start with an account and repository you are authorized to change. A public App
may be installed by other accounts, but a new installation starts in
**explicit** mode and cannot consume review capacity until your deployment
operator approves it.

Examples use Compose and Dokploy. On OpenShift, replace
`docker compose exec review-github-gateway` with
`oc rsh deployment/review-agent-github-gateway`, and replace
`docker compose exec hermes-review` with
`oc rsh deployment/hermes-review`.

## Understand the two access decisions

GitHub and Review Agent own separate gates:

1. The account owner chooses **All repositories** or **Only select
   repositories** when installing the App. This controls where GitHub may issue
   an installation token.
2. The Review Agent operator chooses **automatic** or **explicit** activation
   for that installation. This controls which signed commands may enter the
   review queue.

Automatic activation does not mint an organization-wide runtime token. On the
first signed `/review`, the private gateway asks GitHub for a short-lived
installation token restricted to that one stable repository ID, verifies the repository identity,
and records only that repository. Normal source and publication tokens remain
restricted to the same exact repository. The commenter must still have current
`write` or `admin` permission, and the PR snapshot is checked before admission.

## 1. Register the App

From a source checkout with the Python requirements installed, generate a
prefilled registration URL:

```bash
python3 tools/review_agent_admin.py github-app registration-url \
  --owner <account> --owner-type <user-or-organization> \
  --public-url https://review.example.org \
  --homepage-url https://docs.example.org/review-agent/
```

Open the returned URL and verify these settings:

| Setting | Value |
| --- | --- |
| Homepage URL | The documentation or repository URL supplied with `--homepage-url` |
| Callback URL | Leave blank; Review Agent does not use user OAuth |
| Request user authorization during installation | Off |
| Device Flow | Off |
| Setup URL | Leave blank |
| Webhook | Active |
| Webhook URL | `https://review.example.org/webhooks/github-app` |
| Webhook secret | A new random value used only by this App |
| SSL verification | On |

Grant these repository permissions:

| Permission | Access | Used for |
| --- | --- | --- |
| Metadata | Read | Stable repository identity |
| Contents | Read | Exact source snapshots |
| Issues | Write | PR comments, reactions, and feedback events |
| Pull requests | Write | PR state, reviews, and native suggestions |

Subscribe only to **Issue comment**. GitHub sends installation and repository
selection lifecycle events to installed Apps automatically. OAuth, user
authorization, organization permissions, Actions, Administration, Secrets,
and Contents write are not required.

After creating the App, generate a private key. Record the numeric App ID and
store the downloaded PEM as a protected file. Never commit or paste the PEM into
chat.

## 2. Configure and start the deployment

Set the App credentials in the deployment secret store:

```dotenv
REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET=<dedicated webhook secret>
REVIEW_AGENT_GITHUB_APP_ID=<numeric App ID>
REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=./secrets/github-app-private-key.pem
```

For Dokploy, create a **File Mount** named
`github-app-private-key.pem`. Paste the complete PEM, including its `BEGIN` and
`END` lines, and set:

```dotenv
REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=../files/github-app-private-key.pem
```

Compose mounts the key read-only into the private GitHub gateway. Workers,
Hermes, and the public admission service do not receive it. Anyone who can read
or edit the Dokploy Compose service may be able to read file-mount content, so
limit project access and rotate the key if it is exposed.

Validate and start the exact reviewed release:

```bash
python3 tools/review_agent_admin.py capabilities
python3 tools/review_agent_admin.py preflight
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
```

Use `--build` only for an explicitly reviewed local-source deployment.

## 3. Install the App

Open the App's **Install App** page and choose one scope:

- **All repositories** is the recommended organization-managed mode. One
  approval covers current and future repositories without listing or enabling
  them in advance.
- **Only select repositories** keeps GitHub's own installation scope narrow.
  It can use automatic activation inside that selection, or explicit Review
  Agent onboarding for each repository.

Installing the App records provider state but does not approve model use. This
is intentional: a public App installation cannot authorize itself.

Find the installation ID in the installation URL or with:

```bash
docker compose exec review-github-gateway \
  review-agent-admin installations list
```

## 4. Choose the activation policy

### Organization-managed activation

Approve the installation once:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app approve <installation-id> \
  --actor "github:<operator>" \
  --reason "approved organization-managed reviews"
```

The default mode is `automatic`. The command re-reads the live GitHub
installation, verifies the exact App permission contract, and stores the
operator identity and reason. It does not enumerate or pre-enable repositories.

After this one approval, a developer with `write` or `admin` permission may
post `/review` in any repository included by the GitHub installation. The first
valid command verifies and enables only that repository. Later repositories use
the same path automatically.

### Explicit repository activation

For a sensitive installation, keep the default explicit policy and onboard
only the named repositories:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app onboard <owner/repository> \
  --actor "github:<operator>"
```

This command is limited to **Only select repositories** installations. It
reconciles the selected inventory and enables only the named repository for the
deployed profile.

### Return to explicit mode

To stop automatic activation:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app approve <installation-id> \
  --mode explicit \
  --actor "github:<operator>" \
  --reason "require explicit repository approval"
```

This immediately disables repositories that were enabled automatically.
Repositories that an operator enabled explicitly retain their separate audited
decision. A repository disabled with `repositories disable` becomes a durable
manual override and is not silently re-enabled by a later command.

## 5. Verify the deployment

Run the bounded health checks:

```bash
docker compose exec hermes-review review-agent-admin doctor
docker compose exec hermes-review review-agent-admin queues inspect
docker compose exec review-github-gateway \
  review-agent-admin installations list
docker compose exec review-github-gateway \
  review-agent-admin repositories list
```

For a newly approved automatic installation, `doctor` reports that repositories
will activate after the first signed `/review` delivery. Zero enabled
repositories is therefore a valid initial state. Requester authorization still
gates whether the review itself runs.

In an open same-repository pull request, post a new top-level comment as a user
with current `write` or `admin` permission:

```text
/review
```

Verify:

1. The command receives the configured acknowledgement reaction.
2. GitHub shows one successful `issue_comment` webhook delivery.
3. `repositories list` shows only the exact repository with
   `trigger_mode: automatic`.
4. One durable job and run reach a terminal publication or a deterministic
   failure state.
5. The published review belongs to the expected head SHA and is not duplicated.

After automatic activation, the dry-run can prove current read and publication
authority without another model call or GitHub write:

```bash
docker compose exec hermes-review \
  review-agent-admin smoke-test --dry-run \
  --repository <owner/repository> --pr <number>
```

## Disable or recover access

Disable one repository without changing the GitHub installation:

```bash
docker compose exec review-github-gateway \
  review-agent-admin repositories disable <repository-id> \
  --actor "github:<operator>" \
  --reason "pause reviews for this repository"
```

Suspending or uninstalling the App fences every repository owned by that
installation. Removing a selected repository fences that exact repository.
GitHub access must be restored before Review Agent can use it again.

In automatic mode, restored provider access becomes eligible on the next
authorized `/review` unless an operator manually disabled the repository. In
explicit mode, rerun `github-app onboard` for the named repository.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Webhook returns `401` | GitHub and the deployment use different webhook secrets. |
| `installation_approval_failed` | Verify the installation ID, App key, active status, and four permissions above. |
| A public installation cannot review | Expected: an operator must approve that installation first. |
| `repository_onboarding_failed` | Explicit onboarding requires an **Only select repositories** installation that includes the named repository. |
| `repository_not_authorized` | Confirm the installation is approved for automatic activation or explicitly onboard the repository. |
| `sender_not_authorized` | The commenter needs current `write` or `admin` permission. |
| `fork_source_not_supported` | Test with a branch in the selected base repository. |
| `provider_authorization_denied` | Confirm the App is active and still includes the exact repository. |
| Source or publication loses authority | Check the worker lease and whether the repository, installation, or activation policy changed. |

Use [Operations](./OPERATIONS.md#runbook) for queue diagnostics and
[Security](./SECURITY.md) for the credential and token boundaries.
