# App-only cutover owner map

## Decision input

Current main already has strong canonical owners for App credentials, exact
review subjects, job leases, publication leases, and durable feedback. The
smallest coherent first slice is an internal deterministic GitHub gateway that
takes over the existing direct-App admission reads. This removes the private
key from the App event worker without prematurely mixing in Hermes source,
publication, feedback, or deployment deletion work.

## Canonical owners

| Concern | Canonical owner | Current duplication |
| --- | --- | --- |
| App key, JWT, installation token and cache | `github/app_auth.py` | App worker and offline sync load the key directly |
| Installation and repository authorization | `postgres/github_app.py` | Environment repository allowlists |
| Durable delivery lease | `postgres/webhook_deliveries.py` | Lease predicates are repeated across transitions |
| Provider read transport | `source_control.py` | Tools, admission and App processor construct clients with different credentials |
| Exact review subject | `postgres/review_runs.py` | Scope lacks stable provider repository ID |
| Review worker lease | `postgres/jobs.py` and `WorkerLeaseSession` | Model tools already fence through the canonical owner |
| Publication plan, parts and lease | `postgres/publications.py` and `review_publication_application.py` | Publisher owns claim/heartbeat and PAT transport owns GitHub calls |
| Feedback decision and persistence | `review_feedback_application.py`, `postgres/feedback.py`, `postgres/decisions.py` | App feedback is not cut over; HMAC/PAT sidecar duplicates provider work |
| Deployment topology | `compose.yaml` and the OpenShift template | PAT/HMAC is default; App worker owns the key; feedback is public |

Publication currently has one publication-level lease. Immutable publication
parts are not separately claimed. The App-only publication cutover must preserve
that owner rather than invent a publication-part lease.

## Closed gateway progression

The gateway never accepts an arbitrary method, URL, token, installation ID,
repository, requester, or pull-request number and never returns a token.

1. First slice: authorize a durable review delivery by delivery ID, lease owner,
   and lease generation.
2. Source cutover: exact review-run snapshot, changed-file, diff, and file-page
   operations bound to the review run and live job lease.
3. Publication cutover: execute the current publication under its publication
   ID, lease owner, and generation.
4. Feedback cutover: fixed provider verification and acknowledgement operations;
   the feedback application remains the decision owner.

For the first operation, the gateway loads the signed command and current App
authorization from PostgreSQL, performs provider reads outside the transaction,
then rechecks the delivery lease and App authorization before returning a
bounded typed snapshot. The event worker loses its private-key mount.

## Proposed first Worker slice

Add a Review-Agent-specific gateway for the current direct-App admission reads.
Reuse `app_auth.py`, `source_control.py`, `app_processor.py`, and the final
admission transaction. Add one fixed gateway operation and a narrow client. Keep
the default legacy deployment unchanged until the later atomic deletion slices.

Candidate paths:

- `bootstrap/plugins/review_agent_tools/github/app_auth.py`
- `bootstrap/plugins/review_agent_tools/github/app_processor.py`
- `bootstrap/plugins/review_agent_tools/github/gateway.py`
- `bootstrap/plugins/review_agent_tools/github/gateway_client.py`
- `bootstrap/plugins/review_agent_tools/postgres/webhook_deliveries.py`
- `tools/review_agent_github_app_worker.py`
- `tools/review_agent_github_gateway.py`
- `Dockerfile`
- `compose.yaml`
- focused gateway, App processor, App auth, webhook lease, image and docs tests

Required behavior:

- request contains only delivery ID, lease owner, and lease generation;
- stale or wrong delivery lease fails before GitHub I/O;
- current selected-repository authorization is proved before and after I/O;
- collaborator identity matches the signed delivery and has write/admin access;
- pull snapshot matches the stable repository ID, open PR, and same-repository
  head policy;
- 401 refreshes once; rate limit, transient, malformed-provider and permission
  failures remain typed and secret-safe;
- client has no generic request interface;
- only the gateway mounts the runtime key and it has no ingress exposure;
- final admission transaction and request-key idempotency remain unchanged.

## Deletion gates

- Source cutover must deepen `ReviewRunScope` with stable repository identity,
  switch every model-facing read, then delete the read PAT and environment
  allowlist.
- Publication must preserve the current publication-level lease, heartbeat,
  ambiguity recovery, and failure delivery before deleting its PAT.
- Feedback decisions remain in `review_feedback_application`; delete the
  sidecar only after App acknowledgement and replay behavior pass.
- Delete legacy admission, copied workflow, pilot profile, and feedback service
  only after source, publication, and feedback are all green.
- Operator reconciliation must stop loading the key directly or be explicitly
  classified as an offline-only exception before claiming one key owner.
- Public documentation must not claim App-only behavior before deployment
  deletion is complete.

No live pilot or prerelease may precede residual-path, topology, secret-leak,
and recovery gates.
