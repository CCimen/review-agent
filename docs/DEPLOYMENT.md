---
sidebar_label: Deploy
slug: /deployment
title: Deploy Review Agent
description: Choose an image, set secrets, and deploy with Compose, Dokploy, Coolify, Portainer, or Kubernetes; upgrade and roll back.
status: current
last_verified: 2026-11-18
---

# Deploy Review Agent

## Runtime shape

![Compose topology: only admission and the optional console sit behind the reverse proxy. The private GitHub gateway and the optional console hold the App key, Hermes alone calls the model provider, and every service shown uses PostgreSQL on an internal network.](../website/static/img/compose-topology.svg)

One box represents one worker type, not one replica. Scale review workers and
publishers independently. Expose admission on port `8644`. Keep the GitHub
gateway, Hermes `8642`, and PostgreSQL off the shared proxy network.

The optional [admin panel](ADMIN_PANEL.md) adds a separate image and an
authenticated service on port `8090` in the same deployment.

The private gateway holds the GitHub App key and uses it for bounded source and
publication operations. Hermes and the publisher do not receive the key or
installation tokens. The optional admin panel also mounts the key, to read App
and installation status for access administration.

Two one-shot jobs prepare the deployment: `review-profile-install` installs
the review profile, and `review-db-migrate` migrates the database once
PostgreSQL is healthy. The gateway starts after the migration. Admission and
Hermes also wait for the profile; the App worker, publisher, and Hermes wait for
a healthy gateway; and the review worker waits for a healthy Hermes.

The optional provider-control service runs in Hermes' network namespace. The
optional code graph reaches GitHub only through the gateway, which also sends
its embedding requests to OpenAI when embeddings are enabled.

## Create the credentials

### GitHub App

Register the App as described in
[Set up and share the GitHub App](./GITHUB_APP_PILOT.md). Store the App ID,
webhook secret, and private-key file in the deployment secret manager.

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
Compose with `--build`. A qualified release supplies two images for
`linux/amd64` and `linux/arm64`:

| Image | Contents | Used by |
| --- | --- | --- |
| `review-agent` | Hermes and the Review Agent runtime | Initialization jobs, admission, gateway, workers, and publisher |
| `review-agent-admin` | Admin API and compiled browser application | Optional operator console |

Both images come from one source commit and release version. Their image
digests differ. The admin image serves the frontend and API together; it has
no Node.js runtime. Keep the source checkout, Compose files, and migration image
on the same release as the services.

Wait for **Publish container image** to succeed, then use the manifest
references from that release's `IMAGE-DIGESTS.txt`:

```bash
export REVIEW_AGENT_IMAGE='ghcr.io/ccimen/review-agent@sha256:<release-manifest-digest>'
docker compose pull
```

Set `REVIEW_AGENT_ADMIN_IMAGE` to the admin manifest reference from that same
file when installing the console. Every release publishes only its exact
version tag, after both images pass qualification. Pin digests in production:
a rebuild can change image contents without changing source. The registry
cannot update two tags atomically, so a single available tag does not show that
the release workflow has completed.

The examples use the upstream `ghcr.io/ccimen/review-agent` package.
Organizations using upstream releases keep these image references with their own
GitHub App, database, hostnames, and secrets. A fork publishes under
`ghcr.io/<owner>/<repository>` and `ghcr.io/<owner>/<repository>-admin`, in
lowercase.

Pin PostgreSQL as well. For a Compose deployment, resolve the selected
PostgreSQL tag once and store the resulting repository digest as `POSTGRES_IMAGE`:

```bash
docker pull postgres:17-alpine
export POSTGRES_IMAGE="$(
  docker image inspect postgres:17-alpine \
    --format '{{index .RepoDigests 0}}'
)"
printf 'Review Agent: %s\nPostgreSQL: %s\n' \
  "$REVIEW_AGENT_IMAGE" "$POSTGRES_IMAGE"
```

Record all selected image references with the deployment. Do not combine a
PostgreSQL major-version change with a Review Agent upgrade.

## Deploy

Follow the section for your platform:
[Compose or Dokploy](#deploy-with-compose-or-dokploy),
[Coolify or Portainer](#deploy-with-coolify-or-portainer), or
[Kubernetes (Helm)](#deploy-with-kubernetes-helm).

### Deploy with Compose or Dokploy

1. Copy `.env.example` to the platform secret store. Replace each
   `replace-with...` placeholder: the webhook secret, `API_SERVER_KEY`, the
   PostgreSQL password and both database URLs. Set `REVIEW_AGENT_GITHUB_APP_ID`
   in place of the example `123456`, and `REVIEW_AGENT_MODEL`, which the file
   leaves empty. The file holds the required values and
   the optional integrations. Tuning variables keep their Compose defaults and
   are listed in the
   [configuration reference](./OPERATIONS.md#configuration-reference).

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
   `REVIEW_AGENT_MODEL` is required and has no default. Without it, Compose
   stops with "Set REVIEW_AGENT_MODEL to the review model", the installer and
   operator preflight report "REVIEW_AGENT_MODEL must name the review model",
   and admission answers `/ready` with `503`. Set it to a model the provider
   account can use; after the account is connected, the console suggests the
   models Hermes lists for it. Once the console saves settings, their model
   governs new reviews instead of this variable. The provider defaults to
   `openai-codex` and the effort to `xhigh`. The Codex route is the simplest
   first deployment: Hermes supports a ChatGPT device-code login, so no model API
   key is needed. Hermes does not document how this traffic counts against each
   ChatGPT plan's Codex allowance.

   Review Agent supports two providers: `openai-codex` and `anthropic`. Any
   other value is rejected. For Claude, set provider `anthropic` and choose a
   Claude model from Hermes. Hermes documents that Claude OAuth requires Claude Max plus
   extra-usage credits; use an Anthropic API key when that route does not fit.
   Hermes maps reasoning effort to the selected provider.

   Use Hermes' current [provider guide](https://hermes-agent.nousresearch.com/docs/integrations/providers)
   and [model catalog](https://hermes-agent.nousresearch.com/docs/reference/model-catalog)
   to choose the three values. The [environment reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables)
   lists the API-key settings. Keep those provider secrets in
   Hermes' persisted credential store or Hermes-owned `.env`; do not add them
   to the shared Review Agent `.env`.

   On Dokploy, create a **File Mount** named `github-app-private-key.pem`, paste
   the complete PEM including its `BEGIN` and `END` lines, then set:

   ```dotenv
   REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH=../files/github-app-private-key.pem
   ```

   Do not paste the PEM into the environment. Compose reads that host-side file
   and mounts it read-only into the private GitHub gateway. If the file is
   absent or unreadable, Compose fails before it creates the gateway container.

   Treat Dokploy project access as secret access because authorized UI and API
   clients can read file-mount content. Rotate the key if it has appeared in a
   terminal, log, screenshot, ticket, or model conversation.
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

> [!WARNING]
> **Keep private services off the proxy.** Do not route `hermes-review`,
> `review-worker`, `review-publisher`, `review-github-gateway`, or
> `review-postgres`. Admission and the optional authenticated admin panel are
> the only services on the ingress network.

4. Route the review hostname to `review-admission:8644`.

   In Dokploy **Advanced > Networks**, detach every service except
   `review-admission` and an explicitly enabled `review-admin` from
   `dokploy-network`. Leave **Enable Isolated
   Deployment** off; Dokploy deprecated that option, and `compose.yaml` already
   declares the networks. `review-database`, `review-runtime` and
   `review-github-control` are internal and have no outbound access.
   `review-egress` and `review-github-egress` are ordinary bridges: Hermes
   reaches the model provider through the first, and the gateway reaches
   GitHub through the second. Keep their outbound access.
5. Connect the selected provider and restart Hermes:

   ```bash
   docker compose exec hermes-review hermes model
   docker compose exec hermes-review /opt/review-agent-bootstrap/install.sh
   docker compose restart hermes-review
   curl -fsS https://review.example.org/ready
   ```

   `hermes model` is Hermes' provider setup wizard for OAuth and API-key
   logins. The installer command restores the deployment-selected
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

### Deploy with Coolify or Portainer

Import `compose.yaml` as a Compose stack and enter the required values from
`.env.example` in the platform secret UI. Set `REVIEW_AGENT_INGRESS_NETWORK` to
the external proxy network used by the platform. Point the public proxy at
`review-admission:8644`.

Both platforms run the same containers and health checks. Platform-specific
work stays at the proxy boundary; do not publish Hermes or PostgreSQL ports to
the host.

### Deploy with Kubernetes (Helm)

The chart in `examples/helm/review-agent` deploys the main runtime on a
Kubernetes cluster that provides the rest. It expects an existing PostgreSQL
database, three secrets, an ingress controller, a TLS certificate for the
webhook host, and a storage class that can provision the 5 Gi volume Hermes
and the review workers share. With the default `ReadWriteOnce` access mode,
one node must have room for Hermes and every review worker. It uses
fixed resource names, so install one release per namespace.

Create the secrets in the target namespace. Set the webhook variable to the
exact value registered in the GitHub App:

```bash
export REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET='paste-the-registered-value'
kubectl create secret generic review-agent-database \
  --from-literal=REVIEW_AGENT_DATABASE_URL='postgresql://migration-owner:...@host:5432/review_agent' \
  --from-literal=REVIEW_AGENT_RUNTIME_DATABASE_URL='postgresql://review_agent_runtime:...@host:5432/review_agent'
kubectl create secret generic review-agent-hermes \
  --from-literal=API_SERVER_KEY="$(openssl rand -hex 32)"
kubectl create secret generic review-agent-github-app \
  --from-literal=REVIEW_AGENT_GITHUB_APP_ID='123456' \
  --from-literal=REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET="$REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET" \
  --from-file=github-app-private-key.pem=./github-app-private-key.pem
```

Write the values to `review-agent-values.yaml`. `tlsSecretName` names a
`kubernetes.io/tls` secret for the host, for example one issued by
cert-manager; leave it out only when the ingress controller already serves a
trusted certificate for that host.

```yaml
image: ghcr.io/ccimen/review-agent@sha256:<release-manifest-digest>
model: <review-model>
ingress:
  enabled: true
  className: <ingress-class>
  host: review.example.org
  tlsSecretName: review-agent-tls
```

Deploy in three phases. The script stops at the first failure; fix the cause
and run it again from the top:

```bash
bash -euo pipefail <<'HELM_DEPLOY'
CHART=examples/helm/review-agent
VALUES=review-agent-values.yaml
DEPLOYMENTS="hermes-review review-agent-admission review-agent-github-gateway
  review-agent-github-app-worker review-agent-worker review-agent-publisher"

# 1. Drain: scale every workload to zero, delete the Jobs of an earlier
#    attempt, and wait until no application or initialization pod is left.
helm upgrade --install review-agent "$CHART" -f "$VALUES" \
  --set drained=true --set initialize=false
for deployment in $DEPLOYMENTS; do
  generation="$(kubectl get "deployment/$deployment" -o jsonpath='{.metadata.generation}')"
  kubectl wait "deployment/$deployment" --timeout=5m \
    --for=jsonpath='{.status.observedGeneration}'="$generation"
done
kubectl delete job -l app=review-agent-init --ignore-not-found
for app in $DEPLOYMENTS review-agent-init; do
  pods="$(kubectl get pods -l "app=$app" -o name)"
  if [ -n "$pods" ]; then
    kubectl wait --for=delete $pods --timeout=5m
  fi
done

# 2. Initialize: install the profile and migrate the database.
helm upgrade review-agent "$CHART" -f "$VALUES" \
  --set drained=true --set initialize=true --wait --wait-for-jobs --timeout 10m

# 3. Start the workloads.
helm upgrade review-agent "$CHART" -f "$VALUES" --wait --timeout 10m
HELM_DEPLOY
```

The profile and migration Jobs exist only in phase 2, so they never run beside
the workloads or beside an earlier attempt.

| Fails in | State left behind | Next step |
| --- | --- | --- |
| Phase 1 | Workloads may still be running or stopping | Fix the cause and rerun |
| Phase 2 | Workloads at zero; the failed Jobs remain for inspection with `kubectl logs job/<name>` | Fix the cause and rerun; phase 1 deletes those Jobs first |
| Phase 3 | Initialization is complete; some workloads may be running but not ready | Inspect the pods, fix the cause and rerun |

Phase 1 does not wait for the volume: a storage class that binds on first use
binds it in phase 2, when the profile Job mounts it.

Use `https://review.example.org/webhooks/github-app` as the App webhook URL.
Connect the configured model provider inside Hermes once, then restart that
deployment:

```bash
kubectl exec -it deployment/hermes-review -- hermes model
kubectl exec deployment/hermes-review -- /opt/review-agent-bootstrap/install.sh
kubectl rollout restart deployment/hermes-review
```

Only admission has an Ingress. Network policies allow traffic to Hermes only
from the review worker, and to the gateway only from Hermes, the review worker,
the App delivery worker, and the publisher. Pods run as user `10000`, like the
Compose services; change `podSecurityContext` on clusters that assign users
themselves.

Hermes and the review workers share one volume. With the default
`ReadWriteOnce` access mode the chart keeps them on one node, also when only
Hermes or only a worker is replaced; set `persistence.accessMode: ReadWriteMany`
when the storage class supports it.
The volume holds the model login and is kept when the release is uninstalled.
`examples/helm/review-agent/values.yaml` lists every value.

### Finish the setup

After the services are running, install and approve the App as described in
[Set up and share the GitHub App](./GITHUB_APP_PILOT.md), then run
`review-agent-admin doctor` in `hermes-review`.

## Upgrade and roll back production

Before an upgrade, make a tested PostgreSQL backup as described in
[Backup and recovery](./OPERATIONS.md#backup-and-recovery). Record the current
and candidate Review Agent digests and the exact PostgreSQL digest. Keep the
current PostgreSQL image during an application upgrade unless the database has
its own reviewed maintenance plan.

If the admin panel is installed, record both its current and candidate
`REVIEW_AGENT_ADMIN_IMAGE` digests alongside `REVIEW_AGENT_IMAGE`. Select both
from one qualified release's `IMAGE-DIGESTS.txt`; their release tag and source
revision must match. Preserve `compose.admin.yaml` in Compose commands, stop
`review-admin` before migration, and start it with the matching worker services.
Follow the [admin service procedure](ADMIN_PANEL.md#add-the-service) for the
overlay, hostname, and post-upgrade version check. The Compose and Dokploy
procedures below cover every installed service, including `review-admin` and
the other overlay services; the Helm chart deploys the main runtime only.

The review worker requires the private GitHub gateway at startup, even when
documentation review is disabled. In Compose, its `REVIEW_AGENT_GITHUB_GATEWAY_URL`
is `http://review-github-gateway:8646` and it is attached to
`review-github-control`. It uses the existing `API_SERVER_KEY`. Keep this
wiring if you maintain a modified Compose file.

Drain every long-running Review Agent component before running a migration.
This prevents an old process from writing against a schema that changed after
its image was built. When a release changes the installed reviewer, a review
still queued under the previous one fails with `review_contract_changed`; the
release's upgrade notes say when that applies. In that case let the
[queue](./OPERATIONS.md#queue) finish before you stop the services, or request
those reviews again afterwards, as
[Failure codes](./OPERATIONS.md#failure-codes) describes.

### Upgrade with Compose

Set `REVIEW_AGENT_IMAGE` and `POSTGRES_IMAGE` to the recorded digest references,
and `REVIEW_AGENT_ADMIN_IMAGE` when the admin panel is installed. When the
deployment uses overlay files, export `COMPOSE_FILE` with every installed file
in order, for example
`COMPOSE_FILE=compose.yaml:compose.admin.yaml:compose.providers.yaml`, so that
every command covers the same services.

Record the capacity the deployment runs with, because the script starts every
service from the Compose configuration and the deployment environment alone:
the replica count of each service (`docker compose ps`), and
`REVIEW_AGENT_WORKER_CONCURRENCY` and `REVIEW_AGENT_ACTIVE_JOB_LIMIT`. A value
passed only on an earlier command line, and a replica count given only with
`--scale`, are not kept. Put the two variables in the deployment environment,
or in saved **Settings** once a revision exists, and the replica counts in an
overlay file named in `COMPOSE_FILE` (`deploy.replicas`). Then run:

```bash
bash -euo pipefail <<'COMPOSE_UPGRADE'
APP_SERVICES="$(docker compose config --services |
  grep -vxE 'review-postgres|review-profile-install|review-db-migrate')"

docker compose pull
docker compose stop $APP_SERVICES

remaining="$(docker compose ps --all --format '{{.Service}} {{.State}}' |
  awk '$1 != "review-postgres" && $2 != "exited" && $2 != "created"')"
if [ -n "$remaining" ]; then
  echo "Not stopped; do not migrate:" >&2
  echo "$remaining" >&2
  exit 1
fi

docker compose run --rm --no-deps review-profile-install </dev/null
docker compose run --rm --no-deps review-db-migrate </dev/null

docker compose up -d --no-deps --no-build --force-recreate \
  --wait --wait-timeout 300 $APP_SERVICES
docker compose exec -T hermes-review review-agent-admin doctor </dev/null
docker compose ps
COMPOSE_UPGRADE
```

The script stops and recreates every service of those files except
`review-postgres` and the two one-shot jobs. It does not migrate unless every
other container of the Compose project has exited; a container that is
running, restarting or paused stops it, as does a failure to list the
containers. This also catches an overlay left out of `COMPOSE_FILE`. PostgreSQL remains available while the application is
stopped. The two one-shot commands must exit with status `0` before new
application containers start. They and the `doctor` call read from
`/dev/null`: the script reaches `bash` on standard input, and a container
command that inherited it would consume the remaining lines, ending the script
early without an error.

The script sees only this Compose project. Stop anything else that uses the
same database first, such as a model runtime
[provisioned separately](./ADMIN_PANEL.md#provision-another-connection), and
upgrade it to the same release.

Afterwards compare the replica counts, the worker concurrency and the active
job limit with the values recorded before the upgrade.

### Upgrade with Dokploy

1. Pause **AutoDeploy** for the maintenance window and take the database backup.
2. Set `REVIEW_AGENT_IMAGE` and `POSTGRES_IMAGE` to exact digest references in
   **Environment**, and `REVIEW_AGENT_ADMIN_IMAGE` to the same release's admin
   digest when the admin panel is installed.
3. In **Advanced > Run Command**, keep the project name, Compose path, and
   `--remove-orphans`, but replace `--build` with
   `--no-build --pull always`. Save the pull-only command.
4. Select **Stop** and wait until every long-running application container is
   stopped, including `review-admin` and the other overlay services when they
   are installed. The two initialization containers may already show
   `Exited (0)`.
5. Select **Deploy**. Confirm the intended source commit and image digest. Do not
   continue if the previous application containers are still running.
6. Verify that `review-profile-install` and `review-db-migrate` exited with
   status `0`, the long-running containers are healthy, `/ready` returns `200`,
   and `review-agent-admin doctor` reports `ready: true` from `hermes-review`.
7. Re-enable **AutoDeploy** only after the deployment receipt is recorded.

Dokploy's stop step may also stop the bundled PostgreSQL container. Its named
volume remains intact, and Compose starts it before the migration job. Do not
delete the Compose service or either named volume during an upgrade.

### Upgrade with Kubernetes (Helm)

Check out the release so the chart matches the image, and set `image` in
`review-agent-values.yaml` to the release manifest digest. Then run the
three-phase script from
[Deploy with Kubernetes (Helm)](#deploy-with-kubernetes-helm) again; the
installation and the upgrade use the same script.

Its wait loop is the migration fence: phase 2 does not start until every old
application pod and every earlier initialization pod is gone. If a phase fails,
the table below the script shows the state it leaves; fix the cause and run the
script again from the top. Afterwards, verify:

```bash
kubectl exec deployment/hermes-review -- review-agent-admin doctor
```

### Upgrade notes

**After 0.4.14: the Python package is `review_agent`, and the images install it.**
Its source moved from `bootstrap/plugins/review_agent_tools/` to
`src/review_agent/`, its import name changed with it, and its modules were
regrouped into packages by what they do. The command scripts in `tools/` became
modules in that package, and both images now install the package into their
Python environment instead of copying its files. The main image no longer holds
`/opt/review-agent-bootstrap/plugins/` or `/usr/local/bin/review_agent_*.py`.
Installed command names (`review-agent-*` in `/usr/local/bin`, and
`/opt/review-agent-bootstrap/install.sh`), HTTP routes, table names,
environment variables and reviewer tool names are unchanged. So are the Hermes
plugin name `review-agent-tools` and its directory `plugins/review_agent_tools`
in the Hermes home.

The 0.4.14 `compose.yaml` and `compose.admin.yaml` work unchanged with these
images. The 0.4.14 `compose.providers.yaml` and `compose.code-graph.yaml` do
not: each starts a service by a path that no longer exists, so
`review-provider-control` and `review-code-graph` fail to start, and the
console, which waits for `review-provider-control`, does not start either. Use
this release's file for those two overlays. Every command that names a path or
a module:

| Where | 0.4.14 | Now |
| --- | --- | --- |
| `review-provider-control` entrypoint in `compose.providers.yaml` | `python -m review_agent_tools.provider_gateway` | `python -m review_agent.hermes.provider_gateway` |
| `review-code-graph` entrypoint in `compose.code-graph.yaml` | `/opt/hermes/.venv/bin/python /usr/local/bin/review_agent_code_graph.py` | `/usr/local/bin/review-agent-code-graph` |
| Console image command | `uvicorn review_agent_tools.admin_api:app_from_environment` | `uvicorn review_agent.console.app:app_from_environment` |
| First platform owner, run in the console container | `python -m review_agent_tools.admin_auth` | `python -m review_agent.console.auth` |
| Operator commands in a source checkout | `python tools/review_agent_admin.py` | `.venv/bin/review-agent-admin`, after `pip install --constraint requirements/requirements-admin.txt -e .` into that environment |

The console image starts the new command itself, so only a deployment that
overrides the image command must change it. This release's
`compose.providers.yaml` no longer sets
`PYTHONPATH=/opt/review-agent-bootstrap/plugins`: the package is installed, so
nothing needs it. A deployment that still sets it is unaffected, because that
directory no longer exists and Python skips it. The console image's build
metadata moved from `/app/bootstrap/plugins/review_agent_tools/_build.json` to
`_build.json` in the installed package
(`/opt/admin-venv/lib/python3.14/site-packages/review_agent/`).

Three repository directories moved as well: `admin/` is now `console/`,
`bootstrap/profiles/` is `profiles/`, and the rest of `bootstrap/` (the
installer, the managed `config.yaml` and the Hermes patches) is `hermes/`.
Nothing a deployment names changed with them. Every path inside the images
(`/opt/review-agent-bootstrap/` with `install.sh`, `config.yaml` and
`profiles/`, and `/app/admin/dist` in the console image), the image name
`review-agent-admin`, the service `review-admin` and the file
`compose.admin.yaml` are as before. Building from source is what changed: a
custom profile now goes in `profiles/<key>/`, and the console image builds from
`console/Dockerfile`. This release's `compose.admin.yaml` names that file; a
direct build, from the repository root, is
`docker build -f console/Dockerfile -t review-agent-admin:local .`. The
package's optional dependency group for the console API is `console`. No
release used another name; only a source checkout of `main` from before this
change called it `admin`.

The next step applies only to a source checkout that built the console in the
old directory; a deployment of the released images has nothing to remove. After
pulling, `admin/node_modules/`, `admin/dist/`, `admin/dist-preview/` and
`admin/tsconfig.tsbuildinfo` are no longer ignored by git; delete them.

Logger names follow the module path. In every service except `hermes-review`
the prefix `review_agent_tools.` is now `review_agent.`: for example,
`review_agent_tools.worker` became `review_agent.cli.worker`. What a command
logged as `__main__` now carries its module, for example
`review_agent.cli.github_gateway`. Inside `hermes-review`
the plugin's loggers keep the prefix `hermes_plugins.review_agent_tools.`. The
rest of a name changed where its module moved, so a log filter on a module
needs that module's new path; [CONTRIBUTING.md](https://github.com/CCimen/review-agent/blob/main/CONTRIBUTING.md#where-the-code-lives)
lists the packages. Both loggers named under
[Review limits](OPERATIONS.md#review-limits) changed; the 0.4.14 documentation
wrote them without the `hermes_plugins.` prefix:

| Logger | 0.4.14 | Now |
| --- | --- | --- |
| Diff cache | `hermes_plugins.review_agent_tools.review_diff_cache` | `hermes_plugins.review_agent_tools.review.diff_cache` |
| Java guidance | `hermes_plugins.review_agent_tools.review_source_tools` | `hermes_plugins.review_agent_tools.tools.source` |

In the console's OpenAPI document, the component schema
`review_agent_tools__admin_access_api__RepositoryAccess` is now
`RepositoryAccessItem`, with that `title`, and
`review_agent_tools__postgres__github_app__RepositoryAccess` is now
`RepositoryAccess`. Paths and operation IDs are unchanged. One payload
changed: `FindingDecision.occurrence_id` could be `null` and is now always an
integer.

In a run's `failure_detail`, the reviewer's input validation error now reads
`ReviewValidationError` where it read `ReviewMemoryError`. Failure codes are
unchanged, and details stored before the upgrade keep the old name.

Removed since 0.4.14:

- Nine `review-agent-memory` subcommands of the offline learning tools:
  `learning-report`, `coach-export`, `coach-propose`, `coach-verify-proposal`,
  `coach-run`, `coach-record-outcome`, `coach-history`, `validate-replay` and
  `verification-export`. The other subcommands are unchanged.
- From the repository export (`review-agent-memory export`, now schema version
  18): the keys `coach_runs`, `coach_candidates`, `review_verification_runs`,
  `candidate_verifications` and `candidate_reconciliations`. From the
  publications listing: the four `verification_*` fields, which were always
  null.
- The OpenShift template. The Helm chart under `examples/helm/` remains. An
  installation plan with `deployment.platform: openshift` no longer validates;
  `compose` is the only accepted platform.

This release changes the installed reviewer, so reviews queued under 0.4.14
fail with `review_contract_changed` after the switch: let the queue finish
first or request them again, as the upgrade sequence above says.

Migration 039 adds two constraints: a finding decision must name the
occurrence it judged, and a changed file's part must be a part of its review.
If existing rows violate either, the migration fails and changes nothing. The
tables of the removed tools, and any rows in them, are retained; this release
no longer reads or writes them. Whether the previous release can run against
the migrated database is decided as [Rollback boundary](#rollback-boundary)
describes, not by these notes.

### Rollback boundary

The admin API requires every migration shipped with its source revision. Older
console images do not enforce team access and the separate owner and admin
roles, and treat new team members as global viewers. Do not roll back the
console image alone; use a forward fix or coordinated image and database
recovery. Database-ahead readiness alone does not prove that an older image
preserves access policy.

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

Incompatible schema changes use an expand-first sequence: add
backward-compatible schema, deploy code that accepts both forms, backfill, and
remove the old form only in a later release after every older application
digest has been retired as a rollback target.

## Scale and operate the queue

Each worker process runs up to `REVIEW_AGENT_WORKER_CONCURRENCY` review jobs at
once (`4` by default) and claims work only when a slot is free. PostgreSQL
lets one review run per repository at a time (the part jobs of one split review
may overlap), while priority aging lets older ready jobs advance. Increase the
per-process value while CPU, memory, and provider capacity remain healthy; add
replicas when you need more capacity or failure isolation:

```bash
# In the deployment environment (.env or the platform's secret store):
#   REVIEW_AGENT_ACTIVE_JOB_LIMIT=120
#   REVIEW_AGENT_WORKER_CONCURRENCY=10
# 100 worker slots, and up to 120 reviews queued or running.
docker compose up -d --scale review-worker=10
```

Keep the two variables in the deployment environment, not on one command line,
so that a later `up` or upgrade starts the services with the same values. They
take effect only while no settings revision has been saved in the admin
console. After the first save, change both values in **Settings** and restart
the services that read them
([environment defaults and admin controls](./ADMIN_PANEL.md#environment-defaults-and-admin-controls)).
A replica count given with `--scale` lasts until the service is next recreated
without it; the [upgrade procedure](#upgrade-and-roll-back-production) says how
to keep it.

Worker slots are an upper bound on running jobs, not a count of running
reviews. Each model connection and each team also runs at most four jobs at
once by default, and these workers all serve the shared connection. This
example therefore runs at most four jobs at a time until an owner raises that
connection's **Maximum concurrent reviews** in the console; see
[Limits](./TEAM_ACCESS_AND_MODEL_ACCOUNTS.md#limits).

Set `REVIEW_AGENT_ACTIVE_JOB_LIMIT` at or above the number of active and queued
reviews you intend to accept. The bundled Compose database allows 200
connections; use the pool formula in Operations to size an external database.
Confirm that the model provider accepts the chosen concurrency. Compose limits
each worker container to 64 PIDs. Keep per-replica concurrency at or below 25
for thread headroom, or raise the PID limit after a capacity test.

Scale `review-publisher` the same way when publication wait time grows. The
database prevents two publishers from owning the same delivery generation.

`REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS` controls how long one priority point
can move a ready job forward. Each idle worker checks the bounded ready queue at
its configured poll interval, so keep the active-job limit near measured
capacity. The queue and job commands are in the
[Operations runbook](./OPERATIONS.md#runbook).
