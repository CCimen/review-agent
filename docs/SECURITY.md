---
sidebar_label: Security model
slug: /security
title: Security model
description: Trust boundaries, tool surface, prompt-injection posture, token scopes, and data handling.
status: current
last_verified: 2026-08-30
---

# Security model

> **Current** — This trust model describes the live general reviewer. Planned
> analyzers and deferred security integrations do not change these boundaries.

This reviewer is an advisory code-review agent with narrow tools. It is useful
because it combines LLM reasoning with deterministic boundaries, not because the
model is trusted.

## Trust boundaries

- The admission service verifies the GitHub App webhook signature and stores
  only a bounded normalized delivery.
- The private gateway holds the App key and installation tokens. They never
  enter Hermes, PostgreSQL, admission, worker payloads, or logs.
- Before admitting a review or feedback command, the gateway verifies the
  sender's current write or admin permission and the exact open,
  same-repository pull-request snapshot.
- A worker leases the job and calls the private, authenticated Hermes API.
- Hermes runs the review through the bundled plugin, not through a shell.
- The model can read bounded PR context and record candidate findings.
- Deterministic plugin code owns memory writes, publication, feedback parsing,
  and GitHub mutations.

## Tool surface

The live reviewer does not receive:

- a shell;
- repository write access;
- a general GitHub mutation tool;
- a browser;
- delegation;
- arbitrary code execution;
- access to private coach artifacts under `review-learning/`.

Review output reaches GitHub only through a two-stage deterministic path.
`review_agent_deliver` verifies the PR base/head snapshot, freezes the exact
comment parts and validated atomic suggestions in PostgreSQL, and queues that
immutable publication intent. A separate recoverable publisher writes only those
stored parts through the lease-bound App gateway, records each GitHub ID, and completes the
run. Suggestions are grouped in one non-blocking GitHub `COMMENT` review; the
model never receives a GitHub mutation tool or write token.

## Prompt-injection handling

PR code, comments, commit messages, docs, and feedback are untrusted data. The
review profile tells the model to treat prompt-injection-looking text as evidence
only. Repository content cannot change reviewer policy, prompts, skills,
suppressions, memory decisions, or feedback commands.

The admission service, App worker, and private gateway are outside the model
path. Admission stores only a normalized `/review ...` command. Before the
feedback application writes PostgreSQL, the gateway checks the exact open pull
request and the sender's current GitHub write or admin permission. The gateway
can then post only a code-owned reaction or explanation.

Human feedback and coach exports may inform future reviewer changes, but they do
not automatically rewrite prompts, skills, suppressions, or policy. In short:
review evidence can propose changes, but it cannot change policy by itself.
Run Hermes `/learn` only in a separate operator profile or workstation that does
not share the live reviewer's `HERMES_HOME`, skills directory, or gateway.

## Private Claude Verification

Claude verification is an operator-run shadow workflow, not part of the live
webhook reviewer. The public review path does not launch Claude, spawn
subprocesses, delegate to subagents, execute repository code, or hand another
model a GitHub write token.

`review-agent-memory verification-export` reads an already completed review run
and writes a bounded private JSON artifact with mode `0600`. The artifact is for
falsifying current published findings out of band. It contains stable ids,
base/head SHAs, coverage summary, and bounded `*_untrusted` finding evidence. It
does not contain raw database rows, rendered Markdown, feedback actor identities,
or source comment URLs.
If an operator gives this artifact to an external model, this bounded finding
evidence is the intended review-data egress; do not paste raw database exports
or webhook payloads instead.

Claude output is advisory. It must not suppress findings, rewrite prompts,
change feedback commands, publish comments, or gate pull requests without a
separate human-reviewed implementation and replay evidence.

## GitHub credential boundaries

The GitHub App key exists only in the private gateway. It mints short-lived
installation tokens reduced to one repository and either read or publication
permissions. Callers supply a durable lease identity, never a repository, URL,
HTTP method, or credential. The gateway derives authority from PostgreSQL and
checks the lease before and after provider I/O.

The App cannot merge, change workflows, administer repositories, read secrets,
delete branches, or write repository contents. Native suggestions remain
advisory; a developer chooses whether GitHub should apply one.

## Dependency vulnerability scanning

The reviewer does not currently perform full dependency vulnerability scanning.
The live model never queries a vulnerability database or decides whether a CVE
passes CI.

It reviews code and security risks introduced or worsened by the PR. If a PR
changes dependency manifests or lockfiles, it may reason about obvious risks such
as unpinned packages, suspicious dependency additions, removed lockfile
discipline, or a dangerous version change. That is still LLM review, not a CVE
database lookup.

Repository CI runs one pinned Trivy policy against all shipped Python
requirements and the admin, installer, and documentation npm lockfiles. Release CI runs the
same policy against the exact published `linux/amd64` and `linux/arm64` digests.
Every JSON report the scanner produces is retained.

The policy is intentionally small:

- every critical vulnerability blocks by default;
- a high vulnerability blocks when the scanner reports an available fix;
- an unfixed high vulnerability remains visible in the report;
- the release-image gate may accept only the exact critical package identities
  in `release-image-critical-exceptions.json`. That reviewed file has one
  release-only scope and expiry date. A new ID, package version, unused entry,
  available fix, or expired review window fails closed;
- scanner suppressions are disabled. The exception file does not hide findings:
  the exact reports, machine-readable policy receipt, and generated human
  summary remain release evidence.

Temporary npm overrides belong to the package manifest that consumes them:

| Manifest | Override | Reason and removal condition |
| --- | --- | --- |
| `admin/package.json` | `js-yaml` 4.3.2 | Fix [CVE-2026-84375](https://github.com/advisories/GHSA-2883-xcg3-v3hh) in the Swagger UI parser and OpenAPI generator. Remove when the parent packages resolve a patched parser without the override; rebuild and check the API reference. |
| `website/package.json` | `js-yaml` 4.3.2 and `svgo` 3.3.5 on their affected major lines | Fix [CVE-2026-84375](https://github.com/advisories/GHSA-2883-xcg3-v3hh) and [CVE-2026-84370](https://github.com/advisories/GHSA-w27v-7q3p-w38r). Remove each override when the documentation toolchain resolves its patched dependency normally. |
| `website/package.json` | `serialize-javascript` 7.1.1 | Keep the build toolchain above the fixes for [code injection](https://github.com/advisories/GHSA-5c6j-r48x-rmvq) and [CPU exhaustion](https://github.com/advisories/GHSA-qj8w-gfj5-8c6v). Remove when upstream dependencies select a compatible patched version. |

Recheck overrides when updating a parent dependency; a forced older major can
break a newer parent. Regenerate the lockfile, run the affected build, and scan
the resolved dependencies before removing or changing an override.

The main runtime's `requirements.txt` also pins `httpx2` 2.12.0, which selects
the matching `httpcore2` release over the versions inherited from Hermes.
This addresses [WebSocket TLS through SOCKS proxies](https://github.com/pydantic/httpx2/security/advisories/GHSA-7mj9-2mp8-4m2p)
and [unbounded response decompression](https://github.com/pydantic/httpx2/security/advisories/GHSA-8xx6-hgc6-gc2m).
Reassess this pin when updating the Hermes base, using the built image's
dependency inventory, compatibility checks and vulnerability report.

The write-authorized evidence job appends the generated summary to the release
notes only after the exact platform scans pass and the attested evidence files
are attached. A release image is not qualified for deployment until this job
succeeds. A separate job then publishes each exact version tag from its
qualified manifest digest and verifies that the tag preserves that digest.
There is no moving `latest` alias. Wait for the full release workflow before
installing either image.

Source reports remain available as workflow artifacts for 30 days. Successful
release reports are checksummed, attested, and attached to the GitHub release;
failed platform scans retain any reports they produced as workflow artifacts.
If setup or transport fails before a report exists, the gate still fails and the
diagnostic remains in the workflow log. A report records the vulnerability data
available at scan time, so reruns can change as the database is updated.

Evidence generation and scanning run with read-only repository and package
access. A separate write-authorized job receives the successful workflow
artifact, accepts only the expected release files, and verifies every listed
checksum, the exact release source SHA, and the published image digest. It then
attests and attaches the files. A failed scan or invalid handoff therefore cannot
reach the release write boundary.

Other deterministic controls can remain independent where the organization uses
them:

- GitHub Dependency Review;
- Dependabot alerts;
- CodeQL or SARIF code scanning;
- OSV, Snyk, `npm audit`, `pip-audit`, or equivalent scanners.

The best integration is to let deterministic scanners produce their own results
and, later, optionally let the reviewer summarize or prioritize those results.
Do not make the model the source of truth for CVE/GHSA status.

## Release inventories

Releases built from this revision attach the following inventories and evidence:

- CycloneDX JSON, SPDX JSON, and a readable Syft table generated from the exact
  published main and admin image digests for `linux/amd64` and `linux/arm64`;
- a focused CycloneDX 1.7 inventory of the Python packages installed in the
  shipped `linux/amd64` Hermes and admin environments;
- one frontend CycloneDX 1.5 inventory per admin platform, generated by the
  pinned build image's native npm from the exact `admin/package-lock.json` used
  by `npm ci`, then extracted from the published admin image;
- the image digests, per-platform Trivy reports, and SHA-256 checksums for every
  attached evidence file;
- `SOURCE-SHA.txt`, which binds the evidence handoff to the verified release
  source.

The image inventories describe the whole shipped container. The focused Python
inventories describe installed application dependencies. The frontend inventory
includes locked production and development packages used to build the web
assets; it does not claim every package remains as a separate runtime module.
Release checks compare the recorded lockfile hash and direct production package
versions with the verified source. Each frontend inventory records the release
tag, source SHA, exact platform image reference, and lockfile SHA-256 in its
metadata properties. Its npm package version is distinct from the release tag.
The locked package inventory is expected to match across architectures; the
separate files bind it to each platform image.

Both images' registry labels and the admin's baked build metadata must match the
verified release tag and source SHA before evidence is attached. BuildKit's
registry SBOM and provenance, the image attestation, and the downloadable file
attestation remain separate evidence tied to the same release.

An SBOM is evidence about what shipped, not a vulnerability verdict. The Trivy
JSON files record the separate vulnerability verdict. Verify downloaded files
with the checksums and GitHub attestation before using them in audit workflows.

## Human-governed suppressions

Only a GitHub collaborator with current write or admin permission, or an
operator command, can suppress a finding.
The model can record observations, but it cannot mark itself correct or dismiss
its own findings.

Suppressions bind to the exact reviewed file version and expire. If the file
changes, the finding is re-evaluated. ADRs are context, not immunity: an accepted
ADR can explain an architectural decision, but the reviewer should still check
the invariants the ADR requires.

## Data handling

The PostgreSQL database stores findings, review runs, publication and suggestion
metadata, human decisions, and review-quality feedback. It can contain sensitive
unpublished findings and maintainer-entered reasons. Back it up securely and
scrub exports before sharing.

Coach and verification exports are private analysis artifacts. They contain
bounded untrusted text, stable ids, exact observation provenance, hashes, and
event metadata for human-reviewed workflows. Coach evidence includes the
reviewer's original claim and disproof checks alongside the human reason so the
mistake can be evaluated in context. The public webhook reviewer does not read
those exports.

## Public documentation boundary

The GitHub Pages site is static public documentation. It receives no reviewer
credentials, webhook payloads, database data, unpublished findings, private source
excerpts, feedback reasons, model sessions, or production access details. Its
Docusaurus configuration publishes an explicit Markdown allowlist; goal boards,
runtime profile files, and private learning artifacts are excluded.

## Non-goals

This deployment is not a replacement for:

- deterministic CI;
- tests;
- type checks;
- migration checks;
- deterministic security scanners listed above;
- human ownership.

Do not make the reviewer a required merge check until the team has measured its
false-positive rate, acceptance rate, missed-issue feedback, and operational
failure modes.
