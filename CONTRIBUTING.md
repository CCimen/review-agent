# Contributing to Review Agent

Issues and pull requests are welcome. Keep changes focused and do not include
credentials or personal data. Install the pinned development tools alongside
the runtime dependencies:

```bash
python3 -m pip install --requirement requirements/requirements-admin.txt --requirement requirements/requirements-dev.txt
npm install --global pyright@1.1.408
```

The tests and checks put `src` on the import path themselves. To run a command
from the checkout, install the package in editable mode on top of those pins
and call the command by name. `pyproject.toml` maps each command to its module
in `src/review_agent/cli/`, and `PYTHONPATH=src python3 -m review_agent.cli.admin`
runs the same code without installing:

```bash
python3 -m pip install --no-deps --editable .
review-agent-admin capabilities
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
`python3 scripts/generate_admin_openapi.py > admin/openapi.json` and
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

export PYTHONPATH=src
export REVIEW_AGENT_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres
export REVIEW_AGENT_ADMIN_PUBLIC_URL=http://127.0.0.1:5173

REVIEW_AGENT_RUNTIME_DATABASE_URL=postgresql://review_agent_runtime:local-runtime-password@127.0.0.1:55432/postgres \
  python3 -m review_agent.cli.admin database prepare
python3 -m review_agent.console.auth --email admin@example.com
uvicorn review_agent.console.app:app_from_environment --factory --port 8090
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
| `src/review_agent/` | The application: one Python package, `review_agent`, loaded by Hermes as a plugin and by every service |
| `bootstrap/` | What the image installs into Hermes: installer, managed configuration, reviewer profiles |
| `admin/` | The admin console frontend (React); its API lives in the package's `console/` |
| `tests/` | Unit tests and the PostgreSQL contract tests; `tests/support.py` seeds review runs for them |
| `scripts/` | Checks that CI runs, and release tooling |
| `examples/`, `install/`, `skills/` | The Helm chart, example configuration, the install plan schema, and the install skill |
| `docs/`, `website/` | Documentation and the site that publishes it |
| `requirements.txt`, `requirements/` | Pinned dependencies: the runtime's at the root; the console's, the code graph's, development and release tooling in the directory |
| `pyproject.toml` | Build metadata for the package, the installed commands (`[project.scripts]`), and Ruff and Pyright settings. The images build the wheel and install it without dependencies on top of the pins; `./scripts/check_package.sh` checks the installed copy and its commands |

Inside the package:

| Path | Responsibility |
| --- | --- |
| `__init__.py` | The Hermes plugin entry: `register()` wires the model tools |
| `settings.py`, `deployment_settings.py`, `capacity.py`, `https.py`, `build_info.py` | Shared by several packages: environment settings, operator policy defaults, per-response budgets and page limits, same-origin HTTPS redirect handling, image build metadata |
| `domain/` | Pure rules and value types with no I/O: review subject, finding, publication, feedback commands, diff, suggestions, failure codes |
| `postgres/` | PostgreSQL stores, one module per table family. `runtime.py` owns the pool, `telemetry.py` records process presence, and `migrations/` holds the runner and the SQL |
| `github/` | Everything that talks to GitHub: App credentials, the read client, webhook verification, the private gateway that holds the App key and its client, delivery processing, publication delivery |
| `hermes/` | Everything specific to the Hermes runtime: chat and control clients, runtime API, model accounts, quota, context window, the provider gateway |
| `repository_context/` | A repository's `.review-agent/` package: guidance, decisions, validation |
| `code_graph/` | The optional code graph service, cache, indexer and client |
| `review/` | Code-review use cases: run lifecycle, findings, parts, decisions, feedback, the review contract |
| `documentation/` | Documentation-review use cases: scope, preflight, findings, configuration |
| `publication/` | Turning a finished review into GitHub output: plan, render, partition, publish |
| `tools/` | The tools the review model calls, with their JSON schemas |
| `operations/` | Operator use cases: inspection and recovery in `inspection.py`; `doctor`, the smoke test and App registration in `readiness.py` |
| `console/` | The admin console backend. `app.py` builds the FastAPI app, `auth.py` wires sign-in and the account routes, `accounts.py` holds accounts and sessions, `registration.py` and `account_admin.py` their use cases, `application.py` the console's other use cases, and `api/` one HTTP module per area |
| `cli/` | One module per installed command, named after the command: `review-agent-worker` is `cli/worker.py`. The exception is `review-agent-memory`, which inspects stored records and is `cli/records.py`. A long-running command holds its loop or server: the review worker, the publisher, webhook intake, the GitHub App delivery worker, the GitHub gateway and the code graph service |

One noun names a concept in every layer: `domain/finding.py` holds the values,
`postgres/findings.py` the store, `review/findings.py` the use cases and
`tools/findings.py` the model tools.

Three modules are started by module path, so their paths are part of what a
deployment names: `review_agent.console.app` (the console image's `uvicorn`
command), `review_agent.console.auth` (`python -m review_agent.console.auth`)
and `review_agent.hermes.provider_gateway` (`compose.providers.yaml`).

Imports inside the package are relative. Hermes loads the same files as
`hermes_plugins.review_agent_tools`, so an absolute `review_agent` import
there would load a second copy.

Dependencies point mostly one way: `domain/`, then `postgres/`, then the
adapters (`github/`, `hermes/`, `repository_context/`, `code_graph/`), then the
use cases (`review/`, `documentation/`, `publication/`, `operations/`), then
what calls them (`tools/`, `console/`, `cli/`). The top-level modules are
shared configuration. This is a
direction, not a strict layering: some imports still point upward, most of
them to `review/contract.py`.

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
