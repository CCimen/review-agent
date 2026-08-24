# T107 durable admission and deployment receipt

## Outcome

Signed GitHub review requests now enter through a bounded public admission
service. The service validates the signed caller assertion and repository
allowlist, reads the exact open pull-request snapshot from GitHub, and commits
the review run and PostgreSQL job in one transaction. Queue capacity is exact
under concurrent admission.

Workers claim at most one live job per repository. Repository locks with
`SKIP LOCKED`, priority aging, generation-fenced leases, and existing snapshot
checks preserve fairness and prevent superseded work from publishing. Operators
can list, retry, and cancel jobs through the existing database CLI owner.

Hermes is authenticated and private. Compose uses a dedicated outbound network
rather than the shared proxy network. The OpenShift example uses arbitrary-UID
startup, a `Recreate` strategy for its `ReadWriteOnce` PVC, and a worker-only
Hermes ingress policy. The public deployment guide covers GitHub permissions,
secret creation, Dokploy/Compose, Coolify/Portainer, OpenShift, scaling, and
queue controls with platform tabs and one editable Mermaid flow.

## Validation

- PostgreSQL schema, migration, backup, recovery, queue, and concurrency suite:
  129 tests passed.
- Canonical bundle: strict Pyright passed; 347 tests passed and 115 environment-
  gated tests skipped.
- Focused admission and documentation contracts: 35 tests passed.
- Compose rendered successfully and confirmed Hermes is absent from the shared
  ingress network.
- OpenShift template parsed as 14 objects, including the NetworkPolicy.
- Fresh image build and admission, worker, pinned Hermes, and arbitrary-UID
  entrypoint checks passed.
- Docusaurus typecheck and clean production build passed.
- Claude session `review-agent-t107-durable-admission` moved from changes
  required at score 6 to green at score 8 after the verified fixes.
- Exact-commit GitHub validation passed: Python/image run `32769601380` and
  documentation/Pages run `32769601428`.

## Deliberate follow-ups

Workload-specific OpenShift resource defaults need measured usage rather than
invented limits. Making the feedback sidecar an optional Compose profile and
replacing older string-sliced deployment tests with full YAML structure checks
remain adjacent maintenance work; neither blocks durable review admission.

Source revision: `f9dc0f6eed0097814335d4e02753b018d3359460`.
