# GitHub App owner map

## Current owners

- `settings.py` owns the PAT and repository-name allowlist environment contract.
- `admission.py` authenticates the existing HMAC request, reads the PR from GitHub,
  checks the name allowlist, and delegates atomic run/job creation.
- `review_run_application.py` owns the transactional repository, PR, immutable
  subject, run, and job admission lifecycle.
- `postgres/registry.py` already owns repository identity and rename handling by
  `(provider, provider_repository_id)`.
- `source_control.py` owns bounded GitHub reads with a static token.
- `github/publication.py` owns GitHub comment and review writes with static read
  and publish tokens.
- `tools.py` performs GitHub reads inside Hermes, so the App private key cannot
  replace `GITHUB_READ_TOKEN` there without expanding the model-facing secret
  boundary.
- PostgreSQL jobs and publication leases already own expensive asynchronous work
  and crash recovery. The App work should reuse them after a fast durable webhook
  intake.
- `compose.yaml`, `.env.example`, Deployment, Operations, Getting Started, and
  the copied workflow own the current install path.

## Verified GitHub constraints

- An App installation can target selected repositories on a personal account or
  organization.
- Installation tokens expire after one hour and can be narrowed to repository
  IDs and a subset of the App's permissions.
- App JWTs use RS256, allow at most ten minutes of lifetime, and should tolerate
  clock drift.
- GitHub recommends caching installation tokens until shortly before expiry.
- `X-Hub-Signature-256` covers the untouched request bytes.
- GitHub expects a 2xx webhook response within ten seconds.
- A redelivery retains the original `X-GitHub-Delivery` GUID; failed webhooks are
  not redelivered automatically.

## Ownership decision candidates

The existing repository registry remains the identity owner. New PostgreSQL
tables should own App installations, repository access/activation, and durable
webhook deliveries. A narrow GitHub App token issuer should own JWT signing and
repository-scoped installation-token caching. A later internal token endpoint
can expose that issuer to Hermes without exposing the private key.

The first write slice should add only the durable installation and activation
model. Token signing, webhook processing, token delivery to Hermes, cutover, and
documentation can then land as separate behavior-tested commits.

## Deferred from the first slice

- No live App registration or credential placement.
- No direct issue-comment trigger.
- No PAT/Actions deletion.
- No Hermes token-broker endpoint.
- No reporting UI or organization mutation.
