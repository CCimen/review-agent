---
name: install-review-agent
description: Plan, deploy, verify, upgrade, or recover the self-hosted Review Agent, connect its GitHub App to explicitly selected repositories, and establish the initial quality baseline. Use for Review Agent installation, organization onboarding, Dokploy or Compose deployment, OpenShift setup, repository activation, readiness checks, smoke tests, initial quality reporting, upgrades, and rollback. Do not use to classify reviewer feedback or bypass owner, secret, OAuth, DNS, deployment, or live-review approval gates.
---

# Install Review Agent

Work from an exact release or commit. Treat the checked-out repository as the
source of truth; do not infer shipped behavior from a roadmap, proposal, or old
conversation.

Follow five phases:

1. Inspect the exact revision and shipped capabilities.
2. Prepare and validate one non-secret installation plan.
3. Present one mutation, verification, rollback, and human-gate plan.
4. Apply only the approved deployment and repository scope.
5. Prove readiness with doctor, inventory, dry-run, and one approved live test.

## Minimize operator effort

- Resolve repository, release, platform, and current deployment facts with the
  available read-only tools before asking the operator.
- Collect missing non-secret owner decisions into one concise request. Do not
  ask one question at a time when the decisions can be made together.
- Never ask the operator to paste a secret into chat. Give the exact protected
  field, file mount, or interactive login command and pause for confirmation.
- Pause once at each unavoidable manual gate, then resume from the recorded
  plan and evidence. Do not make the operator repeat confirmed information.
- If access is missing, state the exact blocked action and the smallest access
  or manual step needed. Never substitute a guess.

## Establish the contract

Read only the material needed for the requested platform:

- `website/static/llms.txt` for the public entry points and release state;
- `docs/AI_ASSISTED_SETUP.md` for approval boundaries;
- `docs/DEPLOYMENT.md` for Compose/Dokploy or OpenShift;
- `docs/GITHUB_APP_PILOT.md` for App registration and repository activation;
- `docs/OPERATIONS.md` for updates, recovery, and backup checks;
- `docs/SECURITY.md` for credential and network boundaries.

Prepare the source checkout once:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python tools/review_agent_admin.py capabilities
```

The last command returns the shipped behavior as bounded JSON.

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
   .venv/bin/python tools/review_agent_admin.py github-app registration-url \
     --owner <account> --owner-type <user-or-organization> \
     --public-url https://<review-agent-domain> \
     --homepage-url https://<documentation-or-repository-url>
   ```

   Opening and submitting the GitHub form is an owner action.
4. Populate the deployment's protected secret store. Refer to secret names,
   never values, in the plan.
   Keep the default `REVIEW_AGENT_MODEL_PROVIDER`, `REVIEW_AGENT_MODEL`, and
   `REVIEW_AGENT_REASONING_EFFORT`, or select a provider/model pair supported by
   Hermes and the owner's account. Prefer the default Codex device-code OAuth
   route for the first deployment because it needs no model API key. Use
   `hermes model` for OAuth, API keys, and custom endpoints. Keep provider
   secrets in Hermes' persisted credential store or Hermes-owned `.env`. The
   deployment values, not the interactive wizard, are the source of truth.
   Do not claim live validation for a non-default provider until its deployed
   route passes the same dry-run and live acceptance gates.
5. Run `.venv/bin/python tools/review_agent_admin.py preflight` in the prepared host
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

On Dokploy, use **Deploy** when the source revision changes. A plain
**Redeploy** may reuse the existing checkout. Confirm the expected commit in
the completed deployment before running acceptance checks. Preserve
the Hermes data volume so normal deployments retain provider credentials.
Request setup again only when that volume changed or authentication actually
fails. Use `hermes model` for provider setup, then rerun the managed profile
installer so the deployment-selected provider/model remains canonical.

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
2. Reconcile and enable each approved repository from the private gateway:

   ```bash
   review-agent-admin github-app onboard <owner/name> \
     --actor <audited-identity>
   ```
3. List durable repositories and confirm that the command enabled only the
   approved names.
4. Run this in `hermes-review`:

   ```bash
   review-agent-admin smoke-test --dry-run \
     --repository <owner/name> --pr <number>
   ```

   It must complete without a model call or GitHub write.
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

## Establish the quality baseline

After the live acceptance check, run this in `hermes-review`:

```bash
review-agent-memory quality --days 30 --repo <owner/name>
```

Explain each signal beside its reported denominator and keep the current triage
backlog separate from the activity window. Do not calculate an accuracy score
from missing feedback. Do not classify feedback for the operator. If the
operator requests triage, the operator must handle the private repository
export and supply the selected feedback ID and evidence. Never read, quote, or
summarize the raw export. Present the allowed states, wait for the operator to
choose the status and target owner, and ask the operator to remove the export
after triage.

## Report and recover

Report stable IDs, exact versions and digests, readiness, dry-run and live-run
results, backup state, rollback readiness, and unresolved manual gates. Never
include secret values, full webhook payloads, model prompts, or source excerpts.

Use this compact completion shape:

```text
Status: ready | incomplete | failed
Deployed: <exact release, commit, or image digest> in <environment>
GitHub App: <App and installation IDs>; <enabled repositories>
Runtime: <profile, provider/model policy, doctor and queue result>
Acceptance: <dry-run result>; <live run and publication IDs or not approved>
Recovery: <backup owner and rollback state>
Owner actions: <none or exact remaining gates>
```

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
