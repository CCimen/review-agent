---
sidebar_label: AI-assisted setup
slug: /ai-assisted-setup
title: Set up with a coding agent
description: Give Codex, Claude Code, or another coding agent a safe, verifiable Review Agent installation contract.
status: current
last_verified: 2026-08-28
---

# Set up with a coding agent

> **TL;DR:** A coding agent can prepare, deploy, and verify Review Agent when it
> has the source checkout, the non-secret installation plan, and access to your
> deployment tools. You still approve the GitHub App, selected repositories,
> secrets, model login, DNS, deployment, and first real `/review`.

Send the agent this page or the repository's
[`llms.txt`](https://ccimen.github.io/review-agent/llms.txt). For the most
predictable setup, also ask it to use
[`install-review-agent`](https://github.com/CCimen/review-agent/tree/main/skills/install-review-agent)
from the source checkout.

## Give this assignment to your agent

```text
Set up Review Agent from this exact checkout or release. Use
skills/install-review-agent/SKILL.md and website/static/llms.txt as your entry
points. Read only the documentation for my deployment platform. Prepare and
validate the non-secret installation plan, then show one concise mutation and
rollback plan before external changes. Resolve facts you can inspect and group
missing owner decisions into one request. Never ask me to paste secrets into
chat. Pause only when I must approve the GitHub App, repository access, secret
placement, DNS, model login, deployment, or the first live /review. Finish with
doctor, inventory, dry-run, and one approved live-review result. Report exact
versions and stable IDs. On Dokploy, use Deploy when the source revision changes
and verify the completed deployment commit. Mark unknowns as incomplete instead
of guessing.
```

Add the target platform, public hostname, GitHub owner and repositories, and
the deployment tools the agent may use. The validated installation plan records
the remaining non-secret choices. This keeps the conversation focused on real
owner decisions instead of asking you to translate the deployment guide.

## What the agent may do

| The agent can | A human owner must approve or complete |
| --- | --- |
| Validate non-secret decisions | GitHub App creation and installation |
| Generate a prefilled App registration URL | Selected-repository access |
| Prepare Compose, Dokploy, or OpenShift changes | Secret placement and model login |
| Run local preflight and live doctor checks | DNS or production deployment approval |
| Reconcile and enable approved repositories | The first real `/review` and feedback test |

> [!IMPORTANT]
> Do not paste a private key, webhook secret, database credential, internal API
> key, model credential, or backup credential into chat. Put secrets directly
> in the deployment platform's protected configuration.

## 1. Record the non-secret decisions

Copy
[`install/review-agent.example.yaml`](https://github.com/CCimen/review-agent/blob/main/install/review-agent.example.yaml)
outside version control. Replace the example URL, immutable image digest,
GitHub owner and repositories, capacity settings, and backup owner.

Validate the result against
[`install/review-agent.schema.json`](https://github.com/CCimen/review-agent/blob/main/install/review-agent.schema.json):

```bash
npm --prefix install ci
node install/validate.mjs <installation-plan.yaml>
```

The plan contains no secrets. Capacity values are deployment choices rather
than product ceilings; start with the example and increase workers only when
queue wait time or measured throughput requires it.

The plan maps to shipped deployment controls:

| Plan field | Deployment control |
| --- | --- |
| `deployment.image` | `REVIEW_AGENT_IMAGE=<repository>@<digest>` |
| `deployment.profile` | `REVIEW_AGENT_PROFILE` |
| `deployment.model_provider` | `REVIEW_AGENT_MODEL_PROVIDER` |
| `deployment.model` | `REVIEW_AGENT_MODEL` |
| `deployment.reasoning_effort` | `REVIEW_AGENT_REASONING_EFFORT` |
| `deployment.timezone` | `TZ` |
| `deployment.feedback_enabled` | `REVIEW_AGENT_FEEDBACK_ENABLED` |
| `runtime.worker_concurrency` | `REVIEW_AGENT_WORKER_CONCURRENCY` |
| `runtime.active_job_limit` | `REVIEW_AGENT_ACTIVE_JOB_LIMIT` |
| `runtime.worker_replicas` | Compose `--scale review-worker=N` or OpenShift replicas |
| `runtime.publication_replicas` | Compose `--scale review-publisher=N` or OpenShift replicas |

The public URL, platform, selected repositories, and backup owner drive the
deployment plan and its approval gates rather than a second runtime config file.

## 2. Confirm the shipped behavior

From the exact release or commit you intend to deploy, run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python tools/review_agent_admin.py capabilities
.venv/bin/python tools/review_agent_admin.py preflight
```

The last two commands return bounded JSON. Capabilities reports the current App-only
contract. Preflight checks local configuration and the App key file without a
network call, database write, or credential output.

Do not use roadmap text as installation authority. Until the first prerelease
exists, build and deploy an exact reviewed commit. After releases begin, prefer
the signed image digest from the release.

## 3. Review the mutation plan

Before the agent changes GitHub or your deployment, require one short plan that
names:

- the exact commit or image digest;
- the deployment project, environment, services, and public route;
- the GitHub App owner, permissions, event, and selected repositories;
- repositories that will become review-enabled;
- secret names, never values;
- migration, backup, verification, and rollback steps;
- every action that still needs a human.

Approve only the named scope. `github-app onboard` reconciles App access and
records review authorization for one named repository. It does not enable other
repositories selected in the installation.

## 4. Deploy and finish the owner gates

Follow [Deployment](./DEPLOYMENT.md) for Compose platforms, including Dokploy,
Coolify, and Portainer, or for OpenShift. Follow [GitHub App
setup](./GITHUB_APP_PILOT.md) for the prefilled registration URL, exact
permissions, installation reconciliation, and repository enablement.

Keep PostgreSQL, Hermes, workers, publishers, and the GitHub gateway private.
Only the admission route is public. The App private key belongs only in the
private gateway.

If model authentication uses a browser or device flow, the agent should give
you the exact container command and pause. Resume after you confirm login.

## 5. Verify before a real review

Run the live checks from the deployed containers:

```bash
docker compose exec hermes-review review-agent-admin doctor
docker compose exec hermes-review review-agent-admin queues inspect
docker compose exec review-github-gateway review-agent-admin installations list
docker compose exec review-github-gateway review-agent-admin repositories list
docker compose exec hermes-review review-agent-admin smoke-test --dry-run \
  --repository <owner/repository> --pr <number>
```

The dry run proves that one enabled, open, same-repository pull request can be
read and has publication scope. It does not call the model or write to GitHub.

The deployment is not ready merely because containers are healthy. Doctor,
repository enablement, private-service isolation, backup ownership, and the dry
run must all be known and successful.

## 6. Run the owner-controlled acceptance check

After the dry run passes, approve one new top-level comment on the test pull
request:

```text
/review
```

Verify one accepted App delivery, one review run and durable job, and one
terminal publication or deterministic failure status. Confirm that no duplicate
publication appeared and that the result belongs to the expected head SHA.

Record a baseline after the live test:

```console
docker compose exec hermes-review \
  review-agent-memory quality --days 30 --repo <owner/repository>
```

The agent may explain the denominators and current backlog. It must not classify
feedback, promote a coach proposal, or change the reviewer profile without an
operator decision.

Test feedback only when you want to validate the feedback path. The exact
commands and recovery checks are in [Operations](./OPERATIONS.md).

## Completion evidence

A setup report is complete only when it records, without secrets:

- exact deployed commit or image digest;
- deployment environment and public URL;
- database readiness and migration version;
- App ID, verified identity, permissions, event, and installation ID;
- selected and explicitly enabled repository IDs;
- profile, doctor, queue, and dry-run results;
- real review run and publication identifiers;
- backup and rollback status;
- any unfinished owner action.

If one of these is unknown, report the setup as incomplete instead of guessing.
