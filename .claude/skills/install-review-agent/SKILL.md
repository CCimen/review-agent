---
name: install-review-agent
description: Plan, deploy, verify, upgrade, or recover the self-hosted Review Agent and connect its GitHub App to explicitly selected repositories. Use for Review Agent installation, organization onboarding, Dokploy or Compose deployment, OpenShift setup, repository activation, readiness checks, smoke tests, upgrades, and rollback. Do not use to change reviewer findings or bypass owner, secret, OAuth, DNS, deployment, or live-review approval gates.
disable-model-invocation: true
user-invocable: true
---

# Install Review Agent

Work from an exact release or commit. Treat the checked-out repository as the
source of truth; do not infer shipped behavior from a roadmap, proposal, or old
conversation.

## Establish the contract

Read only the material needed for the requested platform:

- `website/static/llms.txt` for the public entry points and release state;
- `docs/AI_ASSISTED_SETUP.md` for approval boundaries;
- `docs/DEPLOYMENT.md` for Compose/Dokploy or OpenShift;
- `docs/GITHUB_APP_PILOT.md` for App registration and repository activation;
- `docs/OPERATIONS.md` for updates, recovery, and backup checks;
- `docs/SECURITY.md` for credential and network boundaries.

Run `python3 tools/review_agent_admin.py capabilities` from the source checkout
before planning. It returns the shipped behavior as bounded JSON.

## Safety boundaries

- Never print, commit, or place a private key, webhook secret, database
  credential, internal API key, model credential, or backup credential in an
  installation plan or report.
- Never invent the account, repository, domain, profile, image digest, backup
  owner, or approval.
- Keep GitHub installation access and Review Agent enablement separate. App
  access alone never authorizes reviews.
- Use **Only select repositories**. Do not broaden App permissions to clear an
  error.
- Deploy an immutable image digest or build an exact reviewed commit. Never
  deploy a floating tag.
- Keep the reviewer advisory. Do not create a merge gate.
- Never claim completion from container health alone.

## Prepare without mutation

1. Copy `install/review-agent.example.yaml` outside version control and replace
   every example decision. Keep secrets out of the file.
2. Validate it against `install/review-agent.schema.json`. In a source checkout
   with the focused validator dependencies installed, run:

   ```bash
   npm --prefix install ci
   node install/validate.mjs <installation-plan.yaml>
   ```

3. Generate the prefilled App registration URL with:

   ```bash
   python3 tools/review_agent_admin.py github-app registration-url \
     --owner <account> --owner-type <user-or-organization> \
     --public-url https://<review-agent-domain>
   ```

   Opening and submitting the GitHub form is an owner action.
4. Populate the deployment's protected secret store. Refer to secret names,
   never values, in the plan.
5. Run `python3 tools/review_agent_admin.py preflight` in the prepared host
   environment. It is local and non-mutating.

Before any external write, show the exact image or commit, services, public
route, selected repositories, repository enablement, secret names, migration,
backup owner, manual gates, verification, and rollback. Ask for approval if the
user has not already authorized those exact mutations.

## Apply the approved deployment

Use the repository's Compose or OpenShift definition without copying its logic
into another template. Change only the named environment or project. Keep the
GitHub gateway, PostgreSQL, Hermes API, workers, and publishers private; expose
only the admission route.

Pause for the human when GitHub owner approval, selected-repository approval,
secret placement, DNS control, or Hermes model login cannot be completed with
the authorized tools. State one exact next action and resume after confirmation.

## Verify in layers

After deployment:

Use `docker compose exec hermes-review review-agent-admin ...` for health,
queue, and smoke-test commands. Use
`docker compose exec review-github-gateway review-agent-admin ...` for
installation inventory and repository activation. On OpenShift, run the same
commands with `oc exec` in the matching workload.

1. Run `review-agent-admin doctor` and `review-agent-admin queues inspect` in
   `hermes-review`.
2. Reconcile and enable each approved repository from the private gateway with
   `review-agent-admin github-app onboard <owner/name>
   --actor <audited-identity>` in `review-github-gateway`.
3. List durable repositories and confirm that the command enabled only the
   approved names.
4. Run `review-agent-admin smoke-test --dry-run --repository <owner/name> --pr
   <number>` in `hermes-review`. This must complete without a model call or
   GitHub write.
5. Confirm backups, the exact deployed digest or commit, schema readiness, App
   identity and permissions, selected repository access, and private-service
   isolation.

The operator commands already return JSON unless the command documents a
different default. Do not add decorative `--json` flags.

## Run the live acceptance gate

Ask the human to approve and post one new top-level `/review` on an open,
same-repository pull request. Then reconcile exact state:

```text
one accepted App delivery
one review run and durable job
one terminal publication or deterministic failure status
no duplicate publication
```

Read the published GitHub result and deployment state. Test feedback only when
the user approves a real feedback comment. Do not claim success from webhook
receipt, queue state, or container health alone.

## Report and recover

Report stable IDs, exact versions and digests, readiness, dry-run and live-run
results, backup state, rollback readiness, and unresolved manual gates. Never
include secret values, full webhook payloads, model prompts, or source excerpts.

Classify failures before acting:

- `401` or `403`: verify identity, installation, permission approval, and
  repository selection; do not broaden permissions automatically.
- `404`: resolve the stable repository ID; do not guess a rename.
- `429` or retryable provider/database failure: preserve durable work and honor
  retry guidance.
- migration failure or database-ahead state: stop and follow the documented
  rollback path.
- stale PR head: allow supersession; never publish findings for the old head.
- incomplete coverage: preserve the incomplete result.
