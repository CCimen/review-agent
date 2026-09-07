---
sidebar_label: Admin panel
slug: /admin-panel
title: Review activity and user accounts
description: Add an optional panel for repository statistics, review history, and individual operator accounts.
status: transitional
last_verified: 2026-09-07
---

# Review activity and user accounts

The optional admin panel shows repository activity and review history. Sign in
with an email address and password. Viewers can read all repositories in the
deployment; administrators can also add accounts, change roles, disable access,
and reset passwords. Review commands and findings stay on GitHub.

This feature is available in the source candidate and has not yet been released.
The current v0.4.0-rc.4 image does not include it. Build both images from the same
candidate checkout, or use a future qualified release that supplies both
`review-agent` and `review-agent-admin` digests. Do not combine this panel with
an older migration image: accounts require PostgreSQL schema 17.

## What the numbers mean

Choose a reporting period of 7, 30, or 90 days. Counts refer to review requests
started in that period:

| Metric | Meaning |
| --- | --- |
| PRs reviewed | Distinct pull requests with at least one published review. |
| Published | Published review requests; reviewing the same PR twice counts twice. |
| Failed requests | Historical failed requests, including those followed by a successful review. |
| Active now | Queued, running, or publishing requests, regardless of their age. |
| Latest failures | PRs whose newest request failed within the selected period. |

History shows the exact base and head commits, worker attempts, timestamps,
failure codes, findings count, and diff coverage. A later success marks an
earlier failure as recovered without removing it from history. Published reviews
may have incomplete coverage; publication does not mean that a PR is approved.
The page refreshes about every ten seconds while open and shows read errors
with a retry control.

## Add the service

Follow the normal [deployment and upgrade procedure](DEPLOYMENT.md), including
backup and migrations. The panel is one additional service in the existing
Compose application, with its own image and hostname. It serves its compiled
frontend and API together on port `8090`; Node.js is used only during the build.

1. Add these non-secret values to the deployment configuration:

   ```dotenv
   REVIEW_AGENT_ADMIN_IMAGE=review-agent-admin:local
   REVIEW_AGENT_ADMIN_PUBLIC_URL=https://admin.example.org
   ```

   The panel uses the existing `REVIEW_AGENT_RUNTIME_DATABASE_URL`. It needs no
   GitHub App key, model credential, or shared password in the environment.
   `REVIEW_AGENT_ADMIN_PUBLIC_URL` must exactly match its HTTPS origin, including
   a non-default port if used. Serve it at the hostname root, not a URL subpath.

2. Include `compose.admin.yaml` with every Compose operation. Retain any other
   overlays already used by the deployment. For a source build:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml config --quiet
   docker compose -f compose.yaml -f compose.admin.yaml up -d --build
   ```

   For a qualified release that includes the panel, set both image variables to
   their respective manifest references from the same `IMAGE-DIGESTS.txt`, then
   use `pull` and `up -d --no-build`. The two images have different digests.

   In Dokploy, keep the panel in the same Compose application so it can reach
   the existing private database network. For a raw Compose deployment, merge
   the overlay into a copy of the deployment's current definition:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml config \
     --no-interpolate --no-path-resolution --no-normalize > compose.dokploy.yaml
   ```

   Preserve the existing project name, volumes, mounts, networks, and overlays
   when saving the result. Set values in Dokploy Environment. For a Git-based
   deployment, include both Compose files in its command. Apply the normal
   Deploy procedure after changing the source revision.

3. In Dokploy Domains, route a separate HTTPS hostname to service `review-admin`,
   container port `8090`. Keep its path as `/` and enable the HTTPS redirect.
   Admission retains its own hostname and port `8644`. Only admission and the
   panel join the shared ingress network; database, gateway, and Hermes remain
   private. A separate Dokploy application is unnecessary.

4. After migrations complete, create the first administrator from the panel
   container's terminal:

   ```bash
   python -m review_agent_tools.admin_auth --email admin@example.org
   ```

   With a local Compose terminal, the equivalent is:

   ```bash
   docker compose -f compose.yaml -f compose.admin.yaml exec review-admin \
     python -m review_agent_tools.admin_auth --email admin@example.org
   ```

   Enter the password at the hidden prompts. Do not put it in command arguments,
   environment files, or chat. Use 15–128 characters. The command creates an
   account only when no accounts exist; service restarts never reset passwords.

5. Open the admin hostname and sign in. Check repository counts and a known
   review, then use **Users** to add an account. A viewer should see statistics
   and history without access to user administration.

## Accounts and recovery

Authentication uses FastAPI Users, Argon2 password hashes, and revocable
PostgreSQL sessions. HTTPS cookies are HttpOnly and SameSite Strict and expire
after eight hours. There is no public registration or email service. Share an
initial password privately; users can change it under **Your account**.

An administrator can disable an account or set a replacement password. Account
changes revoke its sessions, and logout revokes the current session. The panel
prevents disabling or demoting the last active administrator. Keep access to a
second administrator for password recovery; the bootstrap command deliberately
does not replace existing accounts.

Users and sessions are included in the existing PostgreSQL backup. Back up the
database before upgrading both images. To remove the panel, remove its route
and optional service while preserving PostgreSQL. Removing the panel does not
require deleting its tables. Follow the existing database recovery procedure
for a full version rollback; do not reverse migrations manually.

The panel currently provides deployment-wide visibility and account management.
It does not provide per-repository roles, review retries, queue cancellation, or
a Kanban board.
