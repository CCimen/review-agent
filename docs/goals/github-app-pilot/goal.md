# GitHub App pilot

## Objective

Replace repository-specific workflow and personal-token onboarding with one
GitHub App that can be installed on selected repositories, while Review Agent
keeps an explicit PostgreSQL enablement gate. Prove the path first on
`CCimen/review-agent`; do not change any `Sundsvallskommun` resource.

## Goal Kind

`specific`

## Current Tranche

Map the current authentication and lifecycle owners, implement and verify the
durable GitHub App foundation, then audit whether the foundation is safe enough
for the direct-webhook cutover tranche.

## Non-Negotiable Constraints

- PostgreSQL owns installation, repository access, activation, and webhook-delivery state.
- GitHub provider repository IDs are stable identity; repository names are mutable labels.
- App access and Review Agent enablement remain separate gates.
- The App private key never enters Hermes or PostgreSQL.
- Installation tokens are repository-scoped, permission-reduced, cached only in memory, and never logged.
- Preserve the current working trigger until the App foundation passes local validation.
- Delete the PAT/Actions path when the direct App route passes the pilot; do not retain a long-lived compatibility layer.
- Use existing PostgreSQL transactions, durable jobs, publisher leases, and GitHub transports before adding machinery.
- Do not add Redis, Celery, Kafka, a policy engine, or an administration UI.
- Keep public documentation accurate about shipped versus target behavior.
- Do not create, install, or modify a GitHub App outside `CCimen/review-agent` without explicit owner direction.

## Stop Rule

Stop when the tranche audit passes, all safe local work is blocked, or the next
step requires the owner to approve an App installation or place credentials.
Do not claim a live pilot from mocked or local tests.

## Canonical Board

Machine truth lives at:

`docs/goals/github-app-pilot/state.yaml`

If this charter and `state.yaml` disagree, `state.yaml` wins for task status,
active task, receipts, verification freshness, and completion truth.

## Run Command

```text
/goal Follow docs/goals/github-app-pilot/goal.md through the first safe verified implementation slice. Do not stop after planning unless blocked.
```

## PM Loop

On every continuation:

1. Read this charter and `state.yaml`.
2. Work only on the active board task.
3. Write a compact receipt and update the board.
4. Activate the next safe task while work remains.
5. Finish a tranche only with a Judge or PM audit receipt.
