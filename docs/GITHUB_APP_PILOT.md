---
sidebar_label: GitHub App pilot
slug: /github-app-pilot
title: GitHub App admission pilot
description: Test direct GitHub App admission for one selected repository without replacing the current review credentials.
status: current
last_verified: 2026-08-26
---

# GitHub App admission pilot

> **Pilot boundary:** The GitHub App can receive `/review` comments directly,
> track selected repository access, authorize the requester, and admit one
> durable review job. Source reads, publication, and feedback still use the
> current repository-scoped tokens. Keep the protected GitHub Actions path
> available during the pilot.

Use this guide for one owner-controlled installation on one selected repository.
The default production setup remains the
[GitHub Actions path](./GETTING_STARTED.md).

## Before you begin

You need:

- owner access to the GitHub account that will register and install the App;
- access to the deployment secret manager and Compose host;
- the current Review Agent deployment with PostgreSQL migrations applied.

Use a test or pre-production deployment first. Do not install the pilot on an
organization or repository that you have not been authorized to change.

## 1. Register the App

Open **GitHub Settings > Developer settings > GitHub Apps > New GitHub App**.
Use these settings:

| Setting | Value |
| --- | --- |
| Homepage URL | The Review Agent repository or documentation URL |
| Webhook | Active |
| Webhook URL | `https://review.example.org/webhooks/github-app` |
| Webhook secret | A new high-entropy value used only by this App |
| Where can this GitHub App be installed? | Only on this account |

Grant these repository permissions:

| Permission | Access |
| --- | --- |
| Metadata | Read |
| Contents | Read |
| Issues | Read |
| Pull requests | Read |

Subscribe to **Issue comment**. GitHub sends installation lifecycle events to
installed Apps without a separate event subscription. The pilot does not need
OAuth, user authorization, write permissions, repository administration, or
organization permissions.

Generate a private key after GitHub creates the App. Record the numeric **App
ID**, then store the downloaded PEM file in the deployment secret manager. Do
not paste the PEM into an environment variable or commit it.

## 2. Configure the deployment

Set these values in the deployment environment:

```dotenv
REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET=<dedicated App webhook secret>
REVIEW_AGENT_GITHUB_APP_ID=<numeric App ID>
REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=./secrets/github-app-private-key.pem
```

`REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH` is a host path. Compose mounts the
file as a read-only secret at `/run/secrets/github-app-private-key.pem`. Keep the
existing `GITHUB_READ_TOKEN`, `REVIEW_AGENT_PUBLISH_GH_TOKEN`, and
`REVIEW_AGENT_FEEDBACK_GH_TOKEN` values.

Validate the profile, recreate admission so it loads the App webhook secret, and
start the opt-in worker:

```bash
docker compose --profile github-app-pilot config --quiet
docker compose --profile github-app-pilot up -d --no-build
docker compose --profile github-app-pilot ps
```

Use `--build` instead of `--no-build` for a reviewed local-source deployment.
The webhook route remains unavailable when
`REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET` is empty.

## 3. Install on one repository

Open the App's **Install App** page. Choose **Only select repositories** and
select the pilot repository only. Do not choose **All repositories**; the pilot
rejects that installation mode.

Record the numeric installation ID from the installation settings URL. For a URL
ending in `/settings/installations/12345678`, the installation ID is `12345678`.

Get the stable repository ID from a trusted workstation:

```bash
gh api repos/CCimen/review-agent --jq .id
```

Replace `CCimen/review-agent` with the selected repository.

## 4. Reconcile and enable

Fetch the complete selected-repository inventory and reconcile it into
PostgreSQL:

```bash
docker compose --profile github-app-pilot exec review-github-gateway \
  review-agent-database sync-github-app-installation \
  --provider-installation-id <installation-id> \
  --actor "github:<operator>" \
  --reason "initial GitHub App pilot inventory"
```

The JSON result reports the installation status and repository counts. A newly
seen or restored repository remains disabled. Enable only the stable repository
ID you intend to test:

```bash
docker compose --profile github-app-pilot exec review-github-gateway \
  review-agent-database enable-github-app-repository \
  --provider-repository-id <repository-id> \
  --profile sundsvall-standard \
  --actor "github:<operator>" \
  --reason "approved direct-admission pilot"
```

Run reconciliation again after changing selected repositories or repairing a
missed lifecycle delivery. Reconciliation reads the full provider inventory
before changing database state and does not enable new repositories.

## 5. Prove the pilot

On a same-repository pull request, post a new top-level `/review` comment as a
user with current `write` or `admin` permission. Fork pull requests are rejected
during this pilot. Verify:

1. GitHub records a successful `issue_comment` webhook delivery.
2. `docker compose --profile github-app-pilot logs --since 10m review-github-app-worker`
   reports one delivery with `status=accepted`.
3. `review-agent-database jobs --limit 10` and `review-agent-memory runs`
   show one job and one run for the request.
4. The review reaches GitHub through the current publisher token.
5. A simultaneous Actions delivery does not create a second run.

The last check protects the transition period. A successful pilot proves direct
admission, not App-only operation: Hermes still reads the pull request through
`GITHUB_READ_TOKEN`, the publisher still uses
`REVIEW_AGENT_PUBLISH_GH_TOKEN`, and feedback still uses its dedicated token.

GitHub does not redeliver failed webhooks automatically. Use the GitHub App
delivery page to inspect or redeliver a failed event, then confirm that the same
delivery ID does not create duplicate work.

## Disable or roll back

Disable the repository first:

```bash
docker compose --profile github-app-pilot exec review-github-app-worker \
  review-agent-database disable-github-app-repository \
  --provider-repository-id <repository-id> \
  --actor "github:<operator>" \
  --reason "end direct-admission pilot"
```

Then stop the opt-in worker:

```bash
docker compose --profile github-app-pilot stop review-github-app-worker
```

Remove the App webhook secret, App ID, and private-key mount from the deployment.
Keep the Actions workflow and current service tokens active. Retain webhook
delivery and access-audit rows for diagnosis; uninstall the App only after you
have preserved the evidence you need.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Webhook returns `404` | The App webhook secret is empty or the deployment was not restarted. |
| Webhook returns `401` | GitHub and the deployment use different App webhook secrets. |
| Sync rejects the installation | Confirm **Only select repositories** and the four read permissions above. |
| Sync exits temporarily unavailable | Retry after GitHub or PostgreSQL recovers; the command does not apply a partial inventory. |
| `/review` is ignored or rejected | Read `reason=` on the App worker delivery log line. The common terminal reasons are listed below. |
| Review reads or publication fail | Repair the current read or publisher token; the pilot does not replace them. |

- `sender_not_authorized`: the sender does not have current `write` or `admin`
  permission, or the returned GitHub identity does not match the signed sender.
- `repository_not_authorized`: reconcile the installation, then explicitly
  enable the stable repository ID.
- `fork_source_not_supported`: retry on a pull request whose head branch belongs
  to the selected base repository.
- `provider_authorization_denied`: confirm the installation accepted all four
  read permissions and still includes the repository.
- `feedback_not_cut_over`: direct App feedback is not active; use the current
  feedback endpoint.

Use [Operations](./OPERATIONS.md#runbook) for queue and run diagnostics and
[Security](./SECURITY.md) for the full trust boundary.
