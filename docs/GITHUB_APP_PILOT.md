---
sidebar_label: GitHub App setup
slug: /github-app-pilot
title: Set up and share the GitHub App
description: Register the App, install and approve it, and let other organizations use it.
status: current
last_verified: 2026-11-17
---

# Set up and share the GitHub App

Review Agent reaches GitHub only through a GitHub App. The App delivers
`/review` comments to your deployment and issues short-lived, repository-scoped
tokens for source reads and publication.

There are two ways to use it:

| You want to | Do this |
| --- | --- |
| Run your own Review Agent | Deploy it, register your own App (steps 1–5). The App can stay private. |
| Let another organization use your deployment | Make your App public and approve their installation ([Share the App](#share-the-app-with-other-organizations)). |

Examples use Compose.

## 1. Register the App

Generate a prefilled registration URL with the release image you will deploy.
The command needs no network, database or secrets:

```bash
docker run --rm --network none \
  --entrypoint review-agent-admin \
  ghcr.io/ccimen/review-agent:<release> \
  github-app registration-url \
  --owner <account> --owner-type <user-or-organization> \
  --public-url https://review.example.org \
  --homepage-url https://docs.example.org/review-agent/
```

The URL opens GitHub's App registration form with the settings below filled in.
It registers a **private** App: only the owning account can install it.

| Setting | Value |
| --- | --- |
| Homepage URL | The URL supplied with `--homepage-url` |
| Callback URL | Leave blank; Review Agent does not use user OAuth |
| Request user authorization during installation | Off |
| Device Flow | Off |
| Setup URL | Leave blank |
| Webhook | Active |
| Webhook URL | `https://review.example.org/webhooks/github-app` |
| Webhook secret | A new random value used only by this App |
| SSL verification | On |

Repository permissions:

| Permission | Access | Used for |
| --- | --- | --- |
| Metadata | Read | Stable repository identity |
| Contents | Read | Source at the reviewed commits |
| Issues | Write | PR comments, reactions, and feedback commands |
| Pull requests | Write | PR state, reviews, and native suggestions |
| Checks | Write | Documentation review checks (optional) |

Subscribe to **Issue comment**, **Pull request**, and **Check run**. Code review
needs only Issue comment; the other two events and the Checks permission serve
[documentation review](./DOCUMENTATION_REVIEW.md). GitHub sends installation
events to every App automatically. No other permission is required.

After creating the App, generate a private key. Record the numeric App ID and
store the downloaded PEM as a protected file. Never commit it or paste it into
a chat.

## 2. Give the deployment the credentials

```dotenv
REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET=<dedicated webhook secret>
REVIEW_AGENT_GITHUB_APP_ID=<numeric App ID>
REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=./secrets/github-app-private-key.pem
```

For Dokploy, create a **File Mount** named `github-app-private-key.pem`, paste
the complete PEM including its `BEGIN` and `END` lines, and set:

```dotenv
REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=../files/github-app-private-key.pem
```

Compose mounts the key read-only into the private GitHub gateway and, if you
deploy the optional admin panel, into the admin service. The workers, Hermes,
and the public admission service never receive it. Anyone who can read
the Dokploy Compose service may be able to read file-mount content, so limit
project access and rotate the key if it is exposed.

Then start the release as described in [Deployment](./DEPLOYMENT.md).

## 3. Install the App

Open the App's **Install App** page and choose a scope:

- **All repositories**: one approval covers current and future repositories.
- **Only select repositories**: GitHub itself limits which repositories the App
  can reach.

Installing the App does not start reviews. Every new installation is locked
until an operator of the deployment approves it.

Find the installation ID in the installation URL, or list installations:

```bash
docker compose exec review-github-gateway \
  review-agent-admin installations list
```

## 4. Approve the installation

Approval is a Review Agent decision, separate from GitHub's installation scope.
Choose one of two modes per installation.

### Automatic: approve once

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app approve <installation-id> \
  --actor "github:<operator>" \
  --reason "approved organization-managed reviews"
```

The command re-reads the live installation, requires Contents read, Issues
write, and Pull requests write, and records who approved it and why. It does
not list or enable repositories. A repository is enabled when its first signed
`/review` command is processed: the gateway requests a token restricted to that
one repository, verifies its identity, and records it as enabled. Only then
does the gateway check the sender
([who can request a review](#who-can-request-a-review)). A command from someone
who may not request reviews is rejected and starts no review, but the
repository stays enabled. Anyone who can comment on a pull request in a
repository the installation reaches can therefore cause it to be enabled. In
explicit mode a comment never enables a repository.

Platform administrators can do the same in the console under
**Repositories → Access management**.

### Explicit: enable named repositories

Leave the installation in its default explicit mode and enable repositories one
by one:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app onboard <owner/repository> \
  --actor "github:<operator>"
```

The console offers the same action as **Add repository**.

To return an automatic installation to explicit mode:

```bash
docker compose exec review-github-gateway \
  review-agent-admin github-app approve <installation-id> \
  --mode explicit \
  --actor "github:<operator>" \
  --reason "require explicit repository approval"
```

This disables the repositories that were enabled automatically. Repositories
enabled with `onboard` stay enabled.

## 5. Verify

```bash
docker compose exec hermes-review review-agent-admin doctor
docker compose exec review-github-gateway \
  review-agent-admin repositories list
```

`doctor` checks the full App contract: permissions, subscribed events, and the
documentation-review prerequisites. Right after an automatic approval, zero
enabled repositories is normal.

In an open pull request whose branch is in the same repository, post a new
top-level comment:

```text
/review
```

Check that the comment receives a reaction, GitHub shows one successful
`issue_comment` delivery, `repositories list` shows the repository, and one
review is published for the current head commit.

To prove read and publication access without a model call or a GitHub write:

```bash
docker compose exec hermes-review \
  review-agent-admin smoke-test --dry-run \
  --repository <owner/repository> --pr <number>
```

## Who can request a review

- A user with current GitHub `write` or `admin` permission on the repository.
- A user added under **Repositories → Who can request reviews** in the console.
  Grants follow the stable GitHub account ID and do not change GitHub
  permissions.
- Feedback commands always require `write` or `admin`.
- Pull requests from forks are rejected (`fork_source_not_supported`).
- Comments from bots, edited comments, and comments on plain issues are ignored.

## Share the App with other organizations

One deployment can serve installations from multiple GitHub accounts.
Nothing in Review Agent restricts which account installs the App; the approval
in step 4 is the gate.

**What you do as the operator**

1. Make the App public: App settings → **Advanced** → **Make public**. See
   GitHub's [guide to App visibility](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private).
   A public App can be installed by anyone who has the link, which is why new
   installations start locked.
2. Send the other organization the install link:
   `https://github.com/apps/<app-slug>/installations/new`. The console shows
   this link under **Repositories → Access management**.
3. When their installation appears in `installations list`, approve it in
   automatic or explicit mode as in step 4.
4. Optionally create a console team for them and assign their repositories, so
   their members see only their own reviews and can use a separate model
   connection. See [Admin console](./ADMIN_PANEL.md).

**What the other organization does**

1. An organization owner installs the App from the link and chooses all or
   selected repositories.
2. Developers with `write` or `admin` permission post `/review` on a pull
   request.

They deploy nothing and hold no credentials.

**What both sides should know before agreeing**

- Their source code and diffs are read by your deployment and sent to the model
  provider configured in your deployment.
- All installations share one PostgreSQL database and one job queue.
- Reviews use the deployment's model route and account. A console team can be
  given its own model connection, and its repositories then use that account
  instead.
- Console teams limit what each member can see. They are an access control
  inside one deployment, not isolation between mutually hostile tenants. An
  organization that needs that isolation should run its own deployment with its
  own App.
- Uninstalling the App stops all access immediately. Review history already
  stored stays in your database until you remove it.

## Change or revoke access

Disable one repository without touching the GitHub installation:

```bash
docker compose exec review-github-gateway \
  review-agent-admin repositories disable <repository-id> \
  --actor "github:<operator>" \
  --reason "pause reviews for this repository"
```

A repository disabled this way stays disabled until an operator enables it
again; automatic activation does not override it.

Suspending or uninstalling the App on GitHub blocks every repository of that
installation. Removing one repository from a selected-repositories installation
blocks that repository. When GitHub access returns, a repository of an
automatic installation is enabled again by the next `/review` or feedback
command processed for it, before the sender is checked, as described under
[automatic activation](#automatic-approve-once); a repository an operator
disabled stays disabled. In explicit mode, run `github-app onboard` again.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Webhook returns `401` | GitHub and the deployment use different webhook secrets. |
| `installation_approval_failed` | Verify the installation ID, the App key, that the installation is active, and the three required permissions. |
| A new installation cannot review | Expected until an operator approves it. |
| `repository_onboarding_failed` | The installation must be active and include the named repository. |
| `repository_not_authorized` | Approve the installation for automatic activation, or onboard the repository. |
| `sender_not_authorized` | The commenter needs `write` or `admin`, or a requester grant in the console. Feedback needs `write` or `admin`. |
| `fork_source_not_supported` | Use a branch in the base repository. |
| `provider_authorization_denied` | Confirm the App is active and still includes the repository. |

See [Operations](./OPERATIONS.md#runbook) for queue diagnostics and
[Security](./SECURITY.md) for credential and token boundaries.
