---
sidebar_label: GitHub App setup
slug: /github-app-pilot
title: Set up the GitHub App
description: Register, install, enable, and verify Review Agent on selected repositories.
status: current
last_verified: 2026-08-26
---

# Set up the GitHub App

> **TL;DR:** Install one GitHub App with selected-repository access. The App
> receives `/review`, while a private gateway uses short-lived installation
> tokens for source reads and publication. Each repository remains disabled
> until an operator explicitly enables it.

Start with a test repository you own. Do not install the App on an organization
or repository that you are not authorized to change.

## 1. Register the App

Open **GitHub Settings > Developer settings > GitHub Apps > New GitHub App**.

| Setting | Value |
| --- | --- |
| Homepage URL | The Review Agent repository or documentation URL |
| Webhook | Active |
| Webhook URL | `https://review.example.org/webhooks/github-app` |
| Webhook secret | A new random value used only by this App |
| Installation | Only on the account that owns the deployment |

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

The private-key value is a host path. Compose mounts the file read-only into the
gateway. Validate and start the stack:

```bash
docker compose config --quiet
docker compose up -d --no-build
docker compose ps
```

Use `--build` for a reviewed local-source deployment. If feedback is enabled,
its current sidecar still needs `REVIEW_AGENT_FEEDBACK_GH_TOKEN`; admission,
source reads, and publication do not.

## 3. Install, reconcile, and enable

Open the App's **Install App** page, choose **Only select repositories**, and
select the test repository. The current runtime rejects **All repositories** so
new repositories cannot become available implicitly.

Record the installation ID from the installation settings URL, then get the
repository's stable ID:

```bash
gh api repos/CCimen/review-agent --jq .id
```

Reconcile the complete selected-repository inventory:

```bash
docker compose exec review-github-gateway \
  review-agent-database sync-github-app-installation \
  --provider-installation-id <installation-id> \
  --actor "github:<operator>" \
  --reason "initial GitHub App inventory"
```

Newly discovered repositories remain disabled. Enable only the intended stable
repository ID:

```bash
docker compose exec review-github-gateway \
  review-agent-database enable-github-app-repository \
  --provider-repository-id <repository-id> \
  --profile sundsvall-standard \
  --actor "github:<operator>" \
  --reason "approved review repository"
```

The `--profile` value must match `REVIEW_AGENT_PROFILE` in the running stack.
Changing the deployment profile intentionally revokes repositories enabled for
a different profile until an operator enables them for the new one.

Reconcile again after changing the App's repository selection or repairing a
missed lifecycle event. Reconciliation is atomic and never enables a repository.

## 4. Verify one review

On a same-repository pull request, post a new top-level `/review` comment as a
user with current `write` or `admin` permission. Fork pull requests are rejected.

Check:

1. GitHub shows a successful `issue_comment` webhook delivery.
2. `docker compose logs --since 10m review-github-app-worker` reports an accepted delivery.
3. `review-agent-database jobs --limit 10` and `review-agent-memory runs` show one job and run.
4. The private gateway logs source and publication traffic without exposing a token.
5. The review comment appears on the same head SHA without duplicate parts.

GitHub does not automatically redeliver every failed webhook. Use the App's
delivery page to inspect or redeliver an event; the delivery ID is idempotent.

## Disable a repository

```bash
docker compose exec review-github-gateway \
  review-agent-database disable-github-app-repository \
  --provider-repository-id <repository-id> \
  --actor "github:<operator>" \
  --reason "pause automated reviews"
```

Disabling blocks new admission and invalidates current source and publication
authorization. Preserve audit rows when diagnosing an incident.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Webhook returns `401` | GitHub and the deployment use different webhook secrets. |
| Sync rejects the installation | Confirm **Only select repositories** and all four permissions above. |
| `repository_not_authorized` | Reconcile, then explicitly enable the stable repository ID and profile. |
| `sender_not_authorized` | The commenter needs current `write` or `admin` permission. |
| `fork_source_not_supported` | Test with a branch in the selected base repository. |
| `provider_authorization_denied` | Confirm the App is installed, active, and still includes the repository. |
| Source or publication loses authority | Check the worker lease and whether the repository was disabled or removed. |

Use [Operations](./OPERATIONS.md#runbook) for queue diagnostics and
[Security](./SECURITY.md) for the credential boundary.
