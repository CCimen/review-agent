---
sidebar_label: GitHub App setup
slug: /github-app-pilot
title: Set up the GitHub App
description: Register, install, enable, and verify Review Agent on selected repositories.
status: current
last_verified: 2026-08-27
---

# Set up the GitHub App

> **TL;DR:** Install one GitHub App with selected-repository access. The App
> receives `/review`, while a private gateway uses short-lived installation
> tokens for source reads and publication. Each repository remains disabled
> until an operator explicitly enables it.

Start with a test repository you own. Do not install the App on an organization
or repository that you are not authorized to change.

## 1. Register the App

From a source checkout with the Python requirements installed, generate a
prefilled registration URL:

```bash
python3 tools/review_agent_admin.py github-app registration-url \
  --owner <account> --owner-type <user-or-organization> \
  --public-url https://review.example.org \
  --homepage-url https://docs.example.org/review-agent/
```

Open the returned URL and verify these settings before creating the App:

| Setting | Value |
| --- | --- |
| Homepage URL | The Review Agent documentation or repository URL supplied with `--homepage-url`. |
| Callback URL | Leave blank. Review Agent does not use user OAuth. |
| Request user authorization during installation | Off |
| Device Flow | Off |
| Setup URL | Leave blank |
| Webhook | Active |
| Webhook URL | `https://review.example.org/webhooks/github-app` |
| Webhook secret | A new random value used only by this App |
| SSL verification | On |
| Installation | Use the account or organization that owns the selected repositories |

Grant these repository permissions:

| Permission | Access | Used for |
| --- | --- | --- |
| Metadata | Read | Stable repository identity |
| Contents | Read | Exact source snapshots |
| Issues | Write | PR comments and feedback-ready events |
| Pull requests | Write | PR state, reviews, and native suggestions |

Subscribe to **Issue comment**. GitHub sends installation and selected-repository
lifecycle events to installed Apps automatically. OAuth, user authorization,
organization permissions, Actions, Administration, Secrets, and Contents write
are not required.

The public hostname serves the webhook and readiness endpoints. It does not
serve an OAuth callback or an App homepage.

After creating the App, generate a private key. Record the numeric App ID and
store the downloaded PEM as a file in the deployment secret manager. Never put
the PEM contents in an environment variable or commit it.

## 2. Configure and start the deployment

Set:

```dotenv
REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET=<dedicated webhook secret>
REVIEW_AGENT_GITHUB_APP_ID=<numeric App ID>
REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=./secrets/github-app-private-key.pem
```

That path is the Compose and local-host default. For Dokploy, create a **File
Mount** with file path `github-app-private-key.pem`. Paste the complete file,
including its `BEGIN` and `END` lines. Leave the Dokploy mount path at `/`;
Compose reads the managed file from `../files` and mounts it into the gateway
at `/run/secrets`. Replace the default host path with:

```dotenv
REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=../files/github-app-private-key.pem
```

Dokploy stores this file outside the Git checkout so redeploys preserve it.
Compose still controls the container mount and exposes the key only to the
private GitHub gateway.

Anyone who can read or edit this Dokploy Compose service through the UI or API
can read file-mount content. Limit project access accordingly. Rotate the App
key if it appears in a terminal, log, screenshot, ticket, or model conversation.

From the checkout root, export your complete `.env`, then validate it on the
host without network or database access:

```bash
python3 tools/review_agent_admin.py capabilities
python3 tools/review_agent_admin.py preflight
```

Both commands return bounded JSON. Preflight reads the host-side key path and
prints no credential values.

Compose mounts the file read-only into the gateway. Validate and start the stack:

```bash
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
```

Use `--build` for a reviewed local-source deployment. The App key stays in the
private gateway. Workers receive no GitHub credential.

## 3. Install and onboard

Open the App's **Install App** page, choose **Only select repositories**, and
select the test repository. The current runtime rejects **All repositories** so
new repositories cannot become available implicitly.

Run one command in the private gateway. Replace the operator placeholder with
your GitHub login:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app onboard <owner/repository> \
  --actor "github:<operator>"
```

The command finds the repository's App installation, reconciles GitHub's
selected-repository inventory, and enables only the named repository. Review
Agent reads `REVIEW_AGENT_PROFILE` from the running stack, so you do not need to
copy the installation ID or profile into the command.

You do not need the two lower-level `installations sync` and `repositories
enable` commands for normal onboarding. They remain available for recovery and
auditing.

GitHub App access and review enablement remain separate audited decisions. The
onboarding command performs both steps in order for one named repository. It
never enables the rest of the installation.

Changing the deployment profile intentionally revokes repositories enabled for
a different profile until an operator enables them for the new one.

Run the onboarding command again after adding the repository to the App or when
the service missed an installation webhook. The operation is safe to repeat.

Before sending a review request, verify the live deployment and one open pull
request without calling the model or writing to GitHub:

```bash
docker compose exec hermes-review review-agent-admin doctor
docker compose exec hermes-review \
  review-agent-admin smoke-test --dry-run \
  --repository <owner/repository> --pr <number>
```

## 4. Verify one review

On a same-repository pull request, post a new top-level `/review` comment as a
user with current `write` or `admin` permission. Fork pull requests are rejected.

Check:

1. The command comment receives the configured acknowledgement reaction.
2. GitHub shows a successful `issue_comment` webhook delivery.
3. `docker compose logs --since 10m review-github-app-worker review-worker review-publisher` shows one accepted job and no unbounded retries.
4. `review-agent-admin jobs list --limit 10` and `review-agent-memory runs` show one job and run.
5. The private gateway logs source and publication traffic without exposing a token.
6. The review comment appears on the same head SHA without duplicate parts.

GitHub does not automatically redeliver every failed webhook. Use the App's
delivery page to inspect or redeliver an event; the delivery ID is idempotent.

## Disable a repository

```bash
docker compose exec review-github-gateway \
  review-agent-admin repositories disable <repository-id> \
  --actor "github:<operator>" \
  --reason "pause automated reviews"
```

Disabling blocks new admission and invalidates current source and publication
authorization. Use the `repository_id` returned by `github-app onboard`.
Preserve audit rows when diagnosing an incident.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Webhook returns `401` | GitHub and the deployment use different webhook secrets. |
| Sync rejects the installation | Confirm **Only select repositories** and all four permissions above. |
| `repository_onboarding_failed` | Run `doctor`, confirm the App is installed on the named repository with **Only select repositories**, and verify the App key and permissions. |
| `repository_not_authorized` | Confirm the App selection, then rerun `github-app onboard` for the named repository. |
| `sender_not_authorized` | The commenter needs current `write` or `admin` permission. |
| `fork_source_not_supported` | Test with a branch in the selected base repository. |
| `provider_authorization_denied` | Confirm the App is installed, active, and still includes the repository. |
| Source or publication loses authority | Check the worker lease and whether the repository was disabled or removed. |

Use [Operations](./OPERATIONS.md#runbook) for queue diagnostics and
[Security](./SECURITY.md) for the credential boundary.
