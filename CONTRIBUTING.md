# Contributing to Review Agent

Issues and pull requests are welcome. Keep changes focused and do not include
credentials or personal data. Install the pinned development tools alongside
the runtime dependencies:

```bash
python3 -m pip install --requirement requirements/requirements-admin.txt --requirement requirements/requirements-dev.txt
npm install --global pyright@1.1.408
```

Run the fast bundle for every change. Run the PostgreSQL contract when Docker
is available, and always when database or durable workflow behavior changes:

```bash
./scripts/check_bundle.sh
./scripts/check_postgres_schema.sh
```

While working on one database-backed area, name its test modules to run only
those against a disposable database. It takes seconds instead of minutes and
skips the backup and restore check:

```bash
./scripts/check_postgres_schema.sh tests.test_admin_api tests.test_postgres_jobs
```

Admin frontend work also requires Node.js 24 and `sh ./scripts/check_admin.sh`.
The script checks the committed OpenAPI schema and generated TypeScript types,
then builds the frontend. After an intentional API change, regenerate them with
`python3 tools/generate_admin_openapi.py > admin/openapi.json` and
`npm --prefix admin run generate`. Authentication tests use the disposable
PostgreSQL environment in `check_postgres_schema.sh`.

Repository rulesets should require the stable `CI / required` check. That check
passes only after the Python, PostgreSQL, and container-image jobs succeed.

## Run the console locally

The console needs PostgreSQL, the admin API and the Vite dev server. Nothing
else has to run; pages that read review data are empty until a review has run.

```bash
docker run --detach --name review-agent-dev-db --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_PASSWORD=postgres postgres:17

export PYTHONPATH=bootstrap/plugins
export REVIEW_AGENT_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
export REVIEW_AGENT_ADMIN_PUBLIC_URL=http://127.0.0.1:5173

REVIEW_AGENT_RUNTIME_DATABASE_URL=postgresql://review_agent_runtime:local-runtime-password@127.0.0.1:55432/postgres \
  python3 tools/review_agent_admin.py database prepare
python3 -m review_agent_tools.admin_auth --email admin@example.com
uvicorn review_agent_tools.admin.api:app_from_environment --factory --port 8090
```

In a second terminal:

```bash
npm --prefix admin ci
npm --prefix admin run dev
```

Open `http://127.0.0.1:5173` and sign in with the account you created. Vite
proxies `/api` to port 8090. `REVIEW_AGENT_ADMIN_PUBLIC_URL` must be the address
in the browser, because the API rejects writes from any other origin. The
runtime role's password must be at least 16 characters. Remove the database
with `docker rm --force review-agent-dev-db`.

## Where the code lives

| Path | What it is |
| --- | --- |
| `bootstrap/plugins/review_agent_tools/` | The application: one Python package, loaded by Hermes as a plugin and by every service |
| `tools/` | Entrypoints for the services and operator commands; each starts code from the package |
| `bootstrap/` | What the image installs into Hermes: installer, managed configuration, reviewer profiles |
| `admin/` | The admin console frontend (React); its API lives in the package's `admin/` |
| `tests/` | Unit tests and the PostgreSQL contract tests; `tests/support.py` seeds review runs for them |
| `scripts/` | Checks that CI runs, and release tooling |
| `examples/`, `install/`, `skills/` | The Helm chart, example configuration, the install plan schema, and the install skill |
| `docs/`, `website/` | Documentation and the site that publishes it |
| `requirements.txt`, `requirements/` | Pinned dependencies: the runtime's at the root; the console's, the code graph's, development and release tooling in the directory |
| `pyproject.toml` | Ruff and Pyright settings; the application is not an installable distribution |

Inside the package:

| Path | Responsibility |
| --- | --- |
| `admission.py`, `github_webhook.py` | Receive and verify GitHub webhooks |
| `github/` | The private gateway that holds the App key, the delivery worker, source reads and publication |
| `worker.py`, `publisher.py` | The review worker and the publisher processes |
| `review/` | The code review: model tools, run lifecycle, findings, rendering, publication planning, the review contract |
| `documentation/` | Documentation review |
| `repository_context/` | A repository's `.review-agent/` package: guidance, decisions, validation |
| `code_graph/` | The optional code graph service |
| `model_runtime/` | Hermes integration: runtime API, model accounts, quota, context window |
| `admin/`, `admin_auth.py` | The admin console backend. `admin_auth.py` wires sign-in and the account routes; `admin/accounts.py` holds accounts and sessions, `admin/registration.py` and `admin/account_admin.py` their use cases, and the `*_api.py` modules the rest of the HTTP API |
| `domain/` | Pure rules and value types with no I/O |
| `postgres/`, `postgres_migrations/` | PostgreSQL persistence, one module per concern, and the schema migrations |
| `operator_application.py`, `operator_setup.py` | Operator command use cases, `doctor` and App registration |
| `settings.py`, `deployment_settings.py`, `schemas.py` and the other top-level modules | Configuration, tool schemas and helpers shared by several packages |

Two modules stay at the top of the package because they are started by module
path: `admin_auth` (`python -m review_agent_tools.admin_auth`) and
`provider_gateway` (`compose.providers.yaml`).

Dependencies point mostly one way: entrypoints call the application modules,
which call `postgres/` and the gateway, and everything may use `domain/`. The
split is by topic, not a strict layering: `postgres/` modules still import
review and documentation code in places.

## Sign off commits

Review Agent uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/).
Sign off each contribution:

```bash
git commit --signoff
```

By submitting a contribution, you certify the DCO and license that contribution
under `EUPL-1.2` (Version 1.2 only). The DCO does not transfer copyright or give
the project permission to relicense your contribution. A later license change
may require the affected contributors' consent.

Please describe the user or operator outcome, include focused tests for changed
behavior, and update public documentation when the contract changes.
