---
sidebar_label: Deploy
slug: /deployment
title: Deploy Review Agent
description: Create GitHub credentials and deploy with Compose, Dokploy, Coolify, Portainer, or OpenShift.
status: current
last_verified: 2026-09-02
---

import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

# Deploy Review Agent

> **TL;DR**: Choose a released image or build locally, provide PostgreSQL and a
> least-privilege GitHub App, then expose only the admission endpoint. Tune
> bounded worker concurrency before adding replicas. Queue limits protect shared
> compute and do not limit PR size.

## Runtime shape

![Review Agent runtime: GitHub App webhooks enter through admission, review workers use Hermes, a separate publisher writes through the private App gateway, and PostgreSQL owns durable state and queues.](../website/static/img/runtime-shape.webp)

One box represents one worker type, not one replica. Scale review workers and
publishers independently. Expose admission on port `8644`. Keep the GitHub
gateway, Hermes `8642`, and PostgreSQL off the shared proxy network.

The GitHub App receives review commands directly. The private gateway uses its
key for bounded source and publication operations; Hermes and the publisher do
not receive the key or installation tokens.

## Create the credentials

### GitHub App

Create a GitHub App with webhook events and permissions from [GitHub App
setup](./GITHUB_APP_PILOT.md). Use **All repositories** with one audited
installation approval for organization-managed operation, or **Only select
repositories** with an explicit allowlist.
Store the App ID, webhook secret, and private-key file in the deployment secret
manager. The App needs Contents read, Issues write, Pull requests write, and
Metadata read; it does not need Actions, Administration, Secrets, or Contents
write.

### Service secrets

Generate different random values for the App webhook and the private Hermes API:

```bash
openssl rand -hex 32  # REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET
openssl rand -hex 32  # API_SERVER_KEY
```

Generate two more values for PostgreSQL: one migration-owner password and one
runtime password. The live services receive only the runtime login. Do not
reuse a webhook secret as a GitHub token or database password.

## Choose an image

For local development, keep `REVIEW_AGENT_IMAGE=review-agent:local` and start
Compose with `--build`. Each published release also creates one attested
`linux/amd64` and `linux/arm64` image in GitHub Container Registry. Production
deployments must use the manifest digest from the release's
`IMAGE-DIGESTS.txt`, not a tag:

```bash
export REVIEW_AGENT_IMAGE='ghcr.io/ccimen/review-agent@sha256:<release-manifest-digest>'
docker compose pull
```

Prereleases receive only their exact version tag. Stable releases also update
`latest`; production deployments should still pin the digest. Pin PostgreSQL as
well. For a Compose deployment, resolve the selected PostgreSQL tag once and
store the resulting repository digest as `POSTGRES_IMAGE`:

```bash
docker pull postgres:17-alpine
export POSTGRES_IMAGE="$(
  docker image inspect postgres:17-alpine \
    --format '{{index .RepoDigests 0}}'
)"
printf 'Review Agent: %s\nPostgreSQL: %s\n' \
  "$REVIEW_AGENT_IMAGE" "$POSTGRES_IMAGE"
```

Record both exact references with the deployment. The OpenShift template uses
an external database, so its operator must pin and record that database image
separately. Do not combine a PostgreSQL major-version change with a Review Agent
upgrade.

GitHub creates the first package as private even for a public repository. After
the first release, open **Packages > review-agent > Package settings > Change
visibility** to make anonymous pulls available. Public visibility cannot be
reversed. See GitHub's guides to
[publishing container images](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)
and [package visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

## Deploy

<Tabs groupId="deployment-platform">
<TabItem value="compose" label="Compose / Dokploy" default>

1. Copy `.env.example` to the platform secret store and replace each
   `replace-with...` placeholder: the GitHub App values, service secrets, and
   both PostgreSQL credentials and URLs.
   Every other value is a documented tuning default you can keep.

   `REVIEW_AGENT_DATABASE_URL` is the migration-owner URL used only by the
   one-shot `review-db-migrate` service. `REVIEW_AGENT_RUNTIME_DATABASE_URL`
   uses a different login and password for every live service. Both URLs must
   target the same host, port, and database. The initialization job rejects a
   shared login or mismatched effective target before applying a migration.
   The migration owner must own the application schema, have `CREATEROLE`, and
   be able to grant `CONNECT` on this database; PostgreSQL superuser is not
   required. The job refuses to reuse an unrelated role with the requested
   runtime name and verifies a fresh runtime login before it succeeds.

   `REVIEW_AGENT_MODEL_PROVIDER`, `REVIEW_AGENT_MODEL`, and
   `REVIEW_AGENT_REASONING_EFFORT` select inference behavior for new reviews.
   The defaults are `openai-codex`, `gpt-5.6-sol`, and `xhigh`. We recommend
   this route for the first deployment because Hermes supports a ChatGPT
   device-code login and you do not need to provision a model API key. Hermes
   does not document how this traffic counts against each ChatGPT plan's Codex
   allowance, so check your provider usage after the pilot.

   You can use any provider/model pair supported by the installed Hermes
   release. For Claude, set provider `anthropic` and choose a Claude model from
   Hermes. Hermes documents that Claude OAuth requires Claude Max plus
   extra-usage credits; use an Anthropic API key when that route does not fit.
   Hermes maps reasoning effort to the selected provider.

   Use Hermes' current [provider guide](https://hermes-agent.nousresearch.com/docs/integrations/providers)
   and [model catalog](https://hermes-agent.nousresearch.com/docs/reference/model-catalog)
   to choose the three values. The [environment reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)
   lists API-key and custom-endpoint settings. Keep those provider secrets in
   Hermes' persisted credential store or Hermes-owned `.env`; do not add them
   to the shared Review Agent `.env`.

   On Dokploy, add the App PEM as the file mount described in
   [GitHub App setup](./GITHUB_APP_PILOT.md#2-configure-and-start-the-deployment),
   then set:

   ```dotenv
   REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=../files/github-app-private-key.pem
   ```

   Do not paste the PEM into the environment. Compose reads that host-side file
   and mounts it read-only into the private GitHub gateway. If the file is
   absent or unreadable, Compose fails before it creates the gateway container.

   Treat Dokploy project access as secret access because authorized UI and API
   clients can read file-mount content. Rotate the key before broader use if
   it has appeared in a terminal, log, screenshot, ticket, or model conversation.
2. Create the external ingress network. Dokploy already provides
   `dokploy-network`; a plain Docker host can create its configured name:

   ```bash
   docker network create "${REVIEW_AGENT_INGRESS_NETWORK:-dokploy-network}"
   ```

3. Validate the stack, then choose one start path:

   ```bash
   docker compose config --quiet
   ```

   Local source:

   ```bash
   docker compose up -d --build
   ```

   Released image after `REVIEW_AGENT_IMAGE` is set:

   ```bash
   docker compose pull
   docker compose up -d --no-build
   ```

   `--no-build` prevents a missing pull from being replaced by a locally tagged
   working-tree build.

   Confirm the services started:

   ```bash
   docker compose ps
   ```

   In Dokploy, use **Deploy** after a source commit changes so the platform
   fetches the new revision. **Redeploy** can reuse the existing source
   checkout. Confirm the deployment shows the intended commit before testing.

   Dokploy's default Compose command includes `--build`. For a released digest,
   open **Advanced > Run Command**, copy the displayed default command, and
   replace `--build` with `--no-build --pull always`. Keep Dokploy's existing
   project name, Compose path, and `--remove-orphans` flag. Dokploy prefixes the
   field with `docker`, so the saved command begins with `compose`. This keeps a
   checkout from being built and tagged over the recorded release image.

4. Route the review hostname to `review-admission:8644`.

   :::warning[Keep private services off the proxy]
   Do not route `hermes-review`, `review-worker`, `review-publisher`,
   `review-github-gateway`, or `review-postgres`. Only admission belongs on the
   ingress network.
   :::
   In Dokploy **Advanced > Networks**, detach every service except
   `review-admission` from `dokploy-network`. Leave **Enable Isolated
   Deployment** off; Dokploy deprecated that option, and `compose.yaml` already
   declares the private database, runtime, egress, and GitHub-control networks.
5. Connect the selected provider and restart Hermes:

   ```bash
   docker compose exec hermes-review hermes model
   docker compose exec hermes-review /opt/review-agent-bootstrap/install.sh
   docker compose restart hermes-review
   curl -fsS https://review.example.org/ready
   ```

   `hermes model` is Hermes' provider setup wizard for OAuth, API-key, and
   custom routes. The installer command restores the deployment-selected
   provider, model, and effort after the wizard stores credentials. Credentials
   live in `hermes_review_data`; normal redeploys retain them. Repeat setup only
   after replacing that volume or when Hermes reports an authorization failure.

Dokploy reads `compose.yaml` as a Compose application. Add one HTTPS domain to
admission and keep the generated Traefik settings. The checked-in health checks
cover admission, the private gateway, Hermes, and PostgreSQL.

Use the matching container for operator commands:

| Task | Container |
| --- | --- |
| Connect the model provider, run `doctor`, or run `smoke-test` | `hermes-review` |
| Onboard, disable, or reconcile repositories | `review-github-gateway` |
| Inspect review execution | `review-github-app-worker`, `review-worker`, and `review-publisher` logs |

The profile installer and database migrator exit with status `0` after a
successful deployment. Do not open a shell in those completed containers.

</TabItem>
<TabItem value="coolify-portainer" label="Coolify / Portainer">

Import `compose.yaml` as a Compose stack and enter the values from `.env.example`
in the platform secret UI. Set `REVIEW_AGENT_INGRESS_NETWORK` to the external
proxy network used by the platform. Point the public proxy at
`review-admission:8644`.

Both platforms run the same containers and health checks. Platform-specific
work stays at the proxy boundary; do not publish Hermes or PostgreSQL ports to
the host.

</TabItem>
<TabItem value="openshift" label="OpenShift">

The template expects an existing PostgreSQL database and three secrets. Set the
webhook variable to the exact value registered in the GitHub App, then create
the secrets in the target project:

```bash
export REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET='paste-the-registered-value'
oc create secret generic review-agent-database \
  --from-literal=REVIEW_AGENT_DATABASE_URL='postgresql://migration-owner:...@host:5432/review_agent' \
  --from-literal=REVIEW_AGENT_RUNTIME_DATABASE_URL='postgresql://review_agent_runtime:...@host:5432/review_agent'
oc create secret generic review-agent-hermes \
  --from-literal=API_SERVER_KEY="$(openssl rand -hex 32)"
oc create secret generic review-agent-github-app \
  --from-literal=REVIEW_AGENT_GITHUB_APP_ID='123456' \
  --from-literal=REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET="$REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET" \
  --from-file=github-app-private-key.pem=./github-app-private-key.pem
```

Render the template with one immutable image, wait for its initialization jobs,
then start the six long-running components:

```bash
oc delete job review-agent-profile-install review-agent-db-migrate \
  --ignore-not-found
oc process -f examples/openshift/review-agent-template.yaml \
  -p IMAGE='ghcr.io/ccimen/review-agent@sha256:<release-manifest-digest>' \
  -p WORKER_CONCURRENCY=4 | oc apply -f -
oc wait --for=condition=complete job/review-agent-profile-install \
  job/review-agent-db-migrate --timeout=10m
oc scale deployment/hermes-review deployment/review-agent-admission \
  deployment/review-agent-github-gateway \
  deployment/review-agent-github-app-worker deployment/review-agent-worker \
  deployment/review-agent-publisher --replicas=1
oc get route review-agent
```

Delete only these completed initialization Jobs. Their pod templates are
immutable, and recreating them makes the profile and migration checks run
against the exact image being deployed. Follow
[Upgrade and roll back production](#upgrade-and-roll-back-production) after the
initial installation.

Use `https://<route-host>/webhooks/github-app` as the App webhook URL. Connect
the configured model provider inside Hermes once, then restart that deployment:

```bash
oc rsh deployment/hermes-review hermes model
oc rsh deployment/hermes-review /opt/review-agent-bootstrap/install.sh
oc rollout restart deployment/hermes-review
```

Only admission has a Route. The template mounts the App key into the private
gateway pod and allows gateway ingress only from Hermes, the App delivery
worker, and the publisher. It omits `runAsUser`, drops Linux capabilities, and
uses PVC or `emptyDir` mounts for writable paths so OpenShift can assign an
arbitrary UID under `restricted-v2`.

</TabItem>
</Tabs>

## Upgrade and roll back production

Before an upgrade, make a tested PostgreSQL backup as described in
[Backup and recovery](./OPERATIONS.md#backup-and-recovery). Record the current
and candidate Review Agent digests and the exact PostgreSQL digest. Keep the
current PostgreSQL image during an application upgrade unless the database has
its own reviewed maintenance plan.

Drain every long-running Review Agent component before running a migration.
This prevents an old process from writing against a schema that changed after
its image was built.

<Tabs groupId="upgrade-platform">
<TabItem value="compose-upgrade" label="Compose">

Set `REVIEW_AGENT_IMAGE` and `POSTGRES_IMAGE` to the recorded digest references,
then run:

```bash
bash -euo pipefail <<'COMPOSE_UPGRADE'
APP_SERVICES='hermes-review review-admission review-github-gateway review-github-app-worker review-worker review-publisher'

docker compose pull
docker compose stop $APP_SERVICES

running="$(docker compose ps --status running --services)"
for service in $APP_SERVICES; do
  if printf '%s\n' "$running" | grep -qx "$service"; then
    echo "$service is still running; do not migrate." >&2
    exit 1
  fi
done

docker compose run --rm --no-deps --no-build review-profile-install
docker compose run --rm --no-deps --no-build review-db-migrate

docker compose up -d --no-deps --no-build --force-recreate \
  --wait --wait-timeout 300 \
  hermes-review review-admission review-github-gateway \
  review-github-app-worker review-worker review-publisher
docker compose exec hermes-review review-agent-admin doctor
docker compose ps
COMPOSE_UPGRADE
```

PostgreSQL remains available while the application is stopped. The two
one-shot commands must exit with status `0` before new application containers
start.

</TabItem>
<TabItem value="dokploy-upgrade" label="Dokploy">

1. Pause **AutoDeploy** for the maintenance window and take the database backup.
2. Set `REVIEW_AGENT_IMAGE` and `POSTGRES_IMAGE` to exact digest references in
   **Environment**.
3. In **Advanced > Run Command**, keep the project name, Compose path, and
   `--remove-orphans`, but replace `--build` with
   `--no-build --pull always`. Save the pull-only command.
4. Select **Stop** and wait until the six long-running application containers
   are stopped. The two initialization containers may already show `Exited (0)`.
5. Select **Deploy**. Confirm the intended source commit and image digest. Do not
   continue if the previous application containers are still running.
6. Verify that `review-profile-install` and `review-db-migrate` exited with
   status `0`, the long-running containers are healthy, `/ready` returns `200`,
   and `review-agent-admin doctor` reports `ready: true` from `hermes-review`.
7. Re-enable **AutoDeploy** only after the deployment receipt is recorded.

Dokploy's stop step may also stop the bundled PostgreSQL container. Its named
volume remains intact, and Compose starts it before the migration job. Do not
delete the Compose service or either named volume during an upgrade.

</TabItem>
<TabItem value="openshift-upgrade" label="OpenShift">

Set `IMAGE` to the release manifest digest. Confirm that the separately managed
PostgreSQL workload also uses a recorded digest, then drain the old application
pods before applying the template:

```bash
bash -euo pipefail <<'OPENSHIFT_UPGRADE'
export IMAGE='ghcr.io/ccimen/review-agent@sha256:<release-manifest-digest>'
DEPLOYMENTS=(
  hermes-review
  review-agent-admission
  review-agent-github-gateway
  review-agent-github-app-worker
  review-agent-worker
  review-agent-publisher
)

oc scale deployment "${DEPLOYMENTS[@]}" --replicas=0
for deployment in "${DEPLOYMENTS[@]}"; do
  pods="$(oc get pods -l "app=$deployment" -o name)"
  if [ -n "$pods" ]; then
    oc wait --for=delete $pods --timeout=5m
  fi
done

oc delete job review-agent-profile-install review-agent-db-migrate \
  --ignore-not-found
oc process -f examples/openshift/review-agent-template.yaml \
  -p IMAGE="$IMAGE" -p WORKER_CONCURRENCY=4 | oc apply -f -
oc wait --for=condition=complete job/review-agent-profile-install \
  job/review-agent-db-migrate --timeout=10m

oc scale deployment/hermes-review \
  deployment/review-agent-github-gateway --replicas=1
for deployment in hermes-review review-agent-github-gateway; do
  oc rollout status "deployment/$deployment" --timeout=5m
done

oc scale deployment/review-agent-admission \
  deployment/review-agent-github-app-worker deployment/review-agent-worker \
  deployment/review-agent-publisher --replicas=1
for deployment in review-agent-admission review-agent-github-app-worker \
  review-agent-worker review-agent-publisher; do
  oc rollout status "deployment/$deployment" --timeout=5m
done

oc get deployment "${DEPLOYMENTS[@]}" \
  -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
oc rsh deployment/hermes-review review-agent-admin doctor
OPENSHIFT_UPGRADE
```

The first wait loop is the migration fence. Do not delete the old Jobs or apply
the new template until every old application pod is gone.

</TabItem>
</Tabs>

### Rollback boundary

Roll back only to the exact prior Review Agent digest named in the release
record. That record must include the post-migration schema version and a receipt
showing that the prior digest passed `database ready`, `doctor`, and the smoke
test against a restored copy of that schema. If the receipt is absent, use a
forward fix or the verified backup restore instead of an application rollback.

For a verified target, repeat the same drain sequence, select that digest,
rerun the profile installer, and verify database readiness before starting
application replicas. Keep the current PostgreSQL data and exact database image
unless the separate database recovery plan requires a restore.

Never reverse or edit an applied migration. If the previous application cannot
run against the current schema, either deploy a forward fix or restore the
pre-upgrade backup into a separate database and verify it before switching
traffic.

Future incompatible schema changes must use an expand-first sequence: add
backward-compatible schema, deploy code that accepts both forms, backfill, and
remove the old form only in a later release after every older application
digest has been retired as a rollback target.

## Configure GitHub

Register the App and choose one activation model. The recommended
organization-managed model installs it with **All repositories** and records one
audited installation approval; repositories then activate on their first
signed `/review` delivery. Requester authorization still gates the review. The
explicit model uses **Only select repositories** and
an operator allowlist. The [GitHub App setup guide](./GITHUB_APP_PILOT.md) has
the exact permissions, commands, and rollback behavior. Neither mode requires a
repository workflow or Actions secrets.

Run `review-agent-admin doctor` in `hermes-review` after approving the
installation. In automatic mode it is valid for zero repositories to be enabled
before the first signed `/review` delivery.

## Scale and operate the queue

Each worker process runs up to `REVIEW_AGENT_WORKER_CONCURRENCY` model reviews at
once (`4` by default) and claims work only when a slot is free. PostgreSQL
prevents two live leases for the same repository, while priority aging lets
older ready jobs advance. Increase the per-process value while CPU, memory, and
provider capacity remain healthy; add replicas when you need more capacity or
failure isolation:

```bash
# 100 cross-repository slots, plus room for 20 queued reviews.
REVIEW_AGENT_ACTIVE_JOB_LIMIT=120 REVIEW_AGENT_WORKER_CONCURRENCY=10 \
  docker compose up -d --scale review-worker=10

oc set env deployment/review-agent-github-app-worker REVIEW_AGENT_ACTIVE_JOB_LIMIT=120
oc set env deployment/review-agent-worker REVIEW_AGENT_WORKER_CONCURRENCY=10
```

Set `REVIEW_AGENT_ACTIVE_JOB_LIMIT` at or above the number of active and queued
reviews you intend to accept. The bundled Compose database allows 200
connections; use the pool formula in Operations to size an external database.
Confirm that the model provider accepts the chosen concurrency. Compose limits
each worker container to 64 PIDs. Keep per-replica concurrency at or below 25
for thread headroom, or raise the PID limit after a capacity test. The shown
value of 10 stays within the shipped limit.

The OpenShift worker reads the managed profile from a `ReadWriteOnce` PVC.
Keep one worker replica unless the storage class provides `ReadWriteMany` or
the scheduler constrains all consumers to the volume's node. Increase
per-process concurrency first; add replicas only after satisfying that storage
contract.

Scale `review-publisher` the same way when publication wait time grows. The
database prevents two publishers from owning the same delivery generation.

Inspect active jobs, release a delayed retry, or cancel the owning run:

```bash
review-agent-admin queues inspect
review-agent-admin jobs list --limit 100
review-agent-admin jobs retry 42
review-agent-admin jobs cancel 42
```

Tune `REVIEW_AGENT_ACTIVE_JOB_LIMIT` from observed wait time and model capacity.
`REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS` controls how long one priority point
can move a ready job forward. Neither value limits files, lines, tokens, or total
review depth. Keep the active-job limit near measured capacity: each idle worker
checks the bounded ready queue at its configured poll interval.
