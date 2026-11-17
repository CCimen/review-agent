# Contributing to Review Agent

Issues and pull requests are welcome. Keep changes focused and do not include
credentials or personal data. Install the pinned development tools alongside
the runtime dependencies:

```bash
python3 -m pip install --requirement requirements-admin.txt --requirement requirements-dev.txt
npm install --global pyright@1.1.408
```

Run the fast bundle for every change. Run the PostgreSQL contract when Docker
is available, and always when database or durable workflow behavior changes:

```bash
./scripts/check_bundle.sh
./scripts/check_postgres_schema.sh
```

Admin frontend work also requires Node.js 24 and `sh ./scripts/check_admin.sh`.
The script checks the committed OpenAPI schema and generated TypeScript types,
then builds the frontend. After an intentional API change, regenerate them with
`python3 tools/generate_admin_openapi.py > admin/openapi.json` and
`npm --prefix admin run generate`. Authentication tests use the disposable
PostgreSQL environment in `check_postgres_schema.sh`.

Repository rulesets should require the stable `CI / required` check. That check
passes only after the Python, PostgreSQL, and container-image jobs succeed.

## Where the code lives

| Path | What it is |
| --- | --- |
| `bootstrap/plugins/review_agent_tools/` | The application: one Python package, loaded by Hermes as a plugin and by every service |
| `tools/` | Entrypoints for the services and operator commands; each starts code from the package |
| `bootstrap/` | What the image installs into Hermes: installer, managed configuration, reviewer profiles |
| `admin/` | The admin console frontend (React); its API lives in the package's `admin/` |
| `tests/` | Unit tests and the PostgreSQL contract tests |
| `scripts/` | Checks that CI runs, and release tooling |
| `examples/`, `install/`, `skills/` | The OpenShift template and Helm chart, example configuration, the install plan schema, and the install skill |
| `docs/`, `website/` | Documentation and the site that publishes it |

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
| `admin/`, `admin_auth.py` | The admin console backend: HTTP API, accounts and sign-in |
| `domain/` | Pure rules and value types with no I/O |
| `postgres/`, `postgres_migrations/` | All SQL, one module per concern, and the schema migrations |
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
