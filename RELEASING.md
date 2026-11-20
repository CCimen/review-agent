# Release Review Agent

Use a prerelease when the App-only path, multi-repository scale, backup
recovery, capacity, or the arm64 runtime lacks evidence for the intended
release claim. An alternate model provider is required only when its support is
claimed in that release. GitHub publishes the matching GHCR images only after
you publish the GitHub release.

## Before a release

- Confirm `LICENSE`, `NOTICE.md`, `CONTRIBUTING.md`, `CITATION.cff`, and
  `THIRD_PARTY_NOTICES.md` are current.
- Keep API changes, the generated OpenAPI and TypeScript contracts, migrations,
  and their consumers in the same reviewed change.
- Merge completed changes into `main` after `CI / required` passes. Select a
  clean, reviewed commit from `main`, or a maintained release branch for a
  backport; a backport must pass the same release checks. The release workflow
  reruns the canonical gate against that exact tagged candidate before
  publishing an image.
- Confirm the public docs, generated LLM files, and installation-skill mirrors
  are current.
- Update `REVISION` in `scripts/generate_llms_docs.py`, `version` in
  `pyproject.toml` (a test keeps the two equal), the image example in
  `docs/REPOSITORY_CONTEXT.md`, and `REVIEW_AGENT_IMAGE` in
  `examples/workflows/review-agent-context.yml` to the same release. Regenerate
  both LLM files and commit them, with any changed operator guidance, before
  tagging.
- The Pages gate requires an attached `IMAGE-DIGESTS.txt` and that the newest
  **Publish container image** run for the declared tag succeeded, including its
  **Publish qualified version tags** job, at the commit the tag names. A
  version-bump merge fails this gate until that run has succeeded; the
  currently published site remains available.
  Successful image qualification automatically retries publication from `main`.
- Record the test deployment, one dry run, one published review, and backup
  owner without copying secrets.
- Record one exact prior Review Agent digest as the rollback target, the
  post-migration schema version from `review-agent-admin database ready`, and a
  receipt showing that prior digest passes `database ready`, `doctor`, and the
  smoke test against a restored copy of the post-migration database. If this was
  not tested, state that rollback is limited to a forward fix or backup restore.
- Keep known validation gaps in the release notes. A prerelease must not claim
  production scale that the test deployment did not exercise.
- Review `.github/release-image-critical-exceptions.json` against the
  [vulnerability policy](#vulnerability-policy).

Run the local candidate checks once. These supplement, but do not replace, the
canonical quality gate that the release workflow reruns against the exact
tagged commit:

```bash
./scripts/check_bundle.sh
sh ./scripts/check_admin.sh
npm --prefix website run build
python3 scripts/check_docs.py --build-dir website/build
python3 scripts/generate_llms_docs.py --check
python3 scripts/sync_install_skill.py --check
git diff --check
```

`check_bundle.sh` covers Python imports, strict type checks, unit tests, and
YAML. It does not prove deployment routing, GitHub organization
token approval, provider OAuth state, or repository rules.

Python inventory generation requires Python with pip on the build host. A
hash-pinned installer is mounted read-only into a temporary tool environment;
the runtime image does not need pip or `ensurepip`. Installed runtime packages
are recorded before tool installation and checked against the generated inventory.

## Publish a release

1. Push the validated, reviewed candidate and create an unused exact SemVer
   tag such as `v0.1.0-rc.1` or `v0.1.0` at that commit. Push the tag. Do not
   move an existing release tag; a source correction needs a new tag.
2. Publish the GitHub release from that existing tag. For an RC, mark it
   **Pre-release**; leave a stable release unmarked. Publish concise notes:
   shipped behavior, setup path, validation evidence, known gaps, and rollback.
3. Wait for **Publish container image**. It verifies the tag and generated
   release documentation, runs `CI / required` against that exact source, then
   builds and pushes `review-agent` and `review-agent-admin` by digest for
   `linux/amd64` and `linux/arm64`. A failed Python, PostgreSQL, image
   smoke, or dependency check blocks publication. The workflow
   creates registry SBOM and provenance attestations for both images, then scans
   all four exact published platform digests. A failed platform scan fails the release workflow;
   retain its reports for triage and do not deploy the affected digest. Evidence
   generation and scanning have read-only repository and package access. Only
   after those checks pass does a separate job verify the closed file set,
   checksums, source SHA, and published image digest, attest and attach the
   files, then update the release notes with the generated vulnerability
   summary. The exact published amd64 digest also passes the normal runtime
   image smoke contract, and the admin digest passes its separate runtime smoke
   contract before the release is qualified for deployment. The admin image does
   not inherit the Hermes image's temporary critical-vulnerability exceptions.
   Both builds receive the same release tag and verified source SHA. Evidence
   generation checks registry labels and the admin's baked metadata, then
   extracts each admin platform's frontend inventory, verifying its build
   lockfile against the source checkout.
   Only after all evidence checks and attachments pass does the final job
   promote both version tags to the verified digests.
   The workflow does not update `latest`; deployments select the recorded
   immutable digest pair.
4. Confirm the release contains per-platform CycloneDX JSON, SPDX JSON, and
   readable tables for both images; their focused Python-runtime CycloneDX files;
   `review-agent-admin-frontend-<tag>-linux-amd64.cyclonedx.json` and its
   `linux-arm64` counterpart;
   both `vulnerability-linux-*.json` and both `vulnerability-admin-linux-*.json`
   reports; `VULNERABILITY-POLICY.json`;
   `VULNERABILITY-SUMMARY.md`; `IMAGE-DIGESTS.txt`; `SOURCE-SHA.txt`; and
   `SBOM-SHA256SUMS.txt`. Confirm
   each of `ghcr.io/<owner>/<repository>:<tag>` and
   `ghcr.io/<owner>/<repository>-admin:<tag>` resolves to its recorded workflow
   digest. The workflow publishes under the lowercase name of the repository it
   runs in: `ghcr.io/ccimen/review-agent` upstream, the fork's own name in a
   fork. `IMAGE-DIGESTS.txt` has six entries: a manifest and two platform
   digests per image. Make each package public in GitHub Package settings if anonymous pulls are
   part of the release. Keep `IMAGE-DIGESTS.txt` and `SOURCE-SHA.txt` with the
   deployment record: registry attestation proves an image's origin, and
   workflow success establishes that the release passed qualification.
5. Verify downloaded inventory files before using them, naming the repository
   whose workflow produced them (`CCimen/review-agent` upstream):

   ```bash
   gh attestation verify ./SBOM-SHA256SUMS.txt --repo <owner>/<repository>
   sha256sum --check SBOM-SHA256SUMS.txt
   ```

6. Wait for **Publish documentation** and confirm the public `llms.txt` names
   the qualified release declared on `main`. The successful image workflow
   retriggers publication automatically; a maintained-branch backport leaves
   `main`'s documentation version unchanged. If needed, run **Publish
   documentation** manually from `main` after qualification.
7. Deploy the immutable digest only after the full release workflow succeeds
   and the generated vulnerability summary is visible, through the
   [upgrade procedure](docs/DEPLOYMENT.md#upgrade-and-roll-back-production).
   Preserve secrets, environment settings, persistent storage, and network
   configuration; updating an image reference does not require a fresh example
   environment file. Run `doctor`, queue inspection, repository inventory, a
   dry-run smoke test, and one owner-approved `/review`.
   With the admin panel enabled, deploy both images from the same evidence
   record and compare its authenticated `/api/version` with the release tag
   and `SOURCE-SHA.txt`.

### If the workflow fails

If a scan or image smoke fails, version-tag promotion does not run. Inspect the
failed job and its retained reports. Retry a transient registry or
evidence-upload failure with **Re-run failed jobs**, which keeps the digests
already built. If promotion stops after one tag, retry the failed promotion
job; consumers must wait for both tags. A source fix needs a new reviewed commit
and release tag. Never treat a fresh build of the same source as the same
deployment artifact.

## Roll back

Redeploy only the exact verified rollback digest recorded in the release
evidence above. If no verified target exists, use a forward fix or verified
backup restore. Disable repository admission if the failure affects
authorization or publication. Preserve PostgreSQL and Hermes volumes; follow
the [backup and recovery](docs/OPERATIONS.md#backup-and-recovery) runbook
instead of reversing migrations by hand.

Publish a stable release only after the owner accepts the live correctness,
capacity, recovery, documentation, and support evidence. Do not turn an
untested optional provider or deployment platform into a release claim.

## Vulnerability policy

Repository CI runs one pinned Trivy policy against all shipped Python
requirements and the admin, installer, and documentation npm lockfiles. Release
CI runs the same policy against the exact published `linux/amd64` and
`linux/arm64` digests. Every JSON report the scanner produces is retained.

- Every critical vulnerability blocks by default.
- A high vulnerability blocks when the scanner reports an available fix; an
  unfixed high vulnerability stays visible in the report.
- The release-image gate may accept only the exact critical package identities
  in `.github/release-image-critical-exceptions.json`. That reviewed file has one
  release-only scope and an expiry date that bounds its entries. A new ID,
  package version, unused entry, available fix, or expired entry fails closed.
- Scanner suppressions are disabled. The exception file hides nothing: the
  exact reports, the machine-readable policy receipt, and the generated summary
  remain release evidence.

Source reports stay available as workflow artifacts for 30 days. Successful
release reports are checksummed, attested, and attached to the GitHub release;
failed platform scans keep any reports they produced as workflow artifacts. If
setup or transport fails before a report exists, the gate still fails and the
diagnostic stays in the workflow log. A report records the vulnerability data
available at scan time, so reruns can differ as the database is updated.

## Dependency overrides

Temporary npm overrides belong to the manifest that consumes them:

| Manifest | Override | Reason and removal condition |
| --- | --- | --- |
| `console/package.json` | `js-yaml` 4.3.2 | Fix [CVE-2026-84375](https://github.com/advisories/GHSA-2883-xcg3-v3hh) in the Swagger UI parser and OpenAPI generator. Remove when the parent packages resolve a patched parser without the override; rebuild and check the API reference. |
| `website/package.json` | `js-yaml` 4.3.2 and `svgo` 3.3.5 on their affected major lines | Fix [CVE-2026-84375](https://github.com/advisories/GHSA-2883-xcg3-v3hh) and [CVE-2026-84370](https://github.com/advisories/GHSA-w27v-7q3p-w38r). Remove each override when the documentation toolchain resolves its patched dependency normally. |
| `website/package.json` | `serialize-javascript` 7.1.1 | Keep the build toolchain above the fixes for [code injection](https://github.com/advisories/GHSA-5c6j-r48x-rmvq) and [CPU exhaustion](https://github.com/advisories/GHSA-qj8w-gfj5-8c6v). Remove when upstream dependencies select a compatible patched version. |
| `website/package.json` | `tinypool` 2.1.2 | Fix the worker-pool vulnerabilities addressed in [2.1.2](https://github.com/tinylibs/tinypool/releases/tag/v2.1.2). Docusaurus still selects the affected 1.x line. Remove when Docusaurus resolves a patched version normally; verify its threaded static-page build when changing this override. |

Recheck overrides when updating a parent dependency; a forced older major can
break a newer parent. Regenerate the lockfile, run the affected build, and scan
the resolved dependencies before removing or changing an override.

## Runtime dependency pins

`HERMES_IMAGE` is pinned to the Hermes v2026.8.31 release tag and its immutable
multi-platform digest in `.env.example`, `compose.yaml`, and `Dockerfile`.
Update the tag and digest together through a reviewed dependency bump; never
use the moving `latest` or `main` tag. Hermes owns the supported provider
integrations; the Review Agent renders the selected provider, model, and effort
into managed config.

The pinned Hermes image supplies Python 3.13.5, and its installed package
requires Python below 3.14, so the main and graph environments keep 3.13.5. The
admin image and PostgreSQL CI checks use Python 3.14.7. The bundle CI check runs
on 3.13.5 to cover the Hermes runtime. Recheck the upstream constraint when
changing Hermes; upgrading the admin runtime does not change Hermes's
interpreter.

The main runtime's `requirements.txt` pins, over versions inherited from Hermes:

- `httpx2` 2.12.0, which selects the matching `httpcore2` release, for
  [WebSocket TLS through SOCKS proxies](https://github.com/pydantic/httpx2/security/advisories/GHSA-7mj9-2mp8-4m2p)
  and [unbounded response decompression](https://github.com/pydantic/httpx2/security/advisories/GHSA-8xx6-hgc6-gc2m);
- `anyio` 4.15.1 over the inherited 4.12.1 for the critical CVE-2026-63374,
  matching the admin and code-graph environments;
- `urllib3` 2.8.0 for its
  [HTTPS proxy TLS and streaming fixes](https://github.com/urllib3/urllib3/releases/tag/2.8.0);
- `pillow` 13.0.0, `pyasn1` 0.6.5, `oauthlib` 4.0.1, `certifi` 2026.10.18,
  `brotlicffi` 1.2.1.0 and `mcp` 2.2.0, whose inherited versions fail the
  release image scan. Hermes declares exact Pillow and certifi versions and
  imports Pillow only to read images.

The runtime image removes the inherited SSH client, C headers, and system
libxml2 with their dependent build, browser, and media packages; the managed
review uses HTTPS and text, and graph parsers use prebuilt wheels. The image
applies security updates from the pinned base's Debian release at build time and
replaces its `uv` binary from a second digest-pinned upstream image. Reassess
these pins when updating the Hermes base, using the built image's dependency
inventory, compatibility checks, and vulnerability report.

## Release evidence

Step 4 of [Publish a release](#publish-a-release) lists the attached files. The
image inventories (CycloneDX, SPDX and Syft table) are generated from the exact
published main and admin digests for both platforms. The focused Python
inventories are CycloneDX 1.7 and cover the `linux/amd64` Hermes and admin
environments. Each frontend inventory is CycloneDX 1.5, generated by the pinned
build image's npm from the exact `console/package-lock.json` used by `npm ci`,
then extracted from the published admin image.

The image inventories describe the whole shipped container; the focused Python
inventories describe installed application dependencies. The frontend inventory
includes locked production and development packages used to build the web
assets; it does not claim each remains a separate runtime module. Release checks
compare its recorded lockfile hash and direct production package versions with
the verified source. Each frontend inventory records the release tag, source
SHA, exact platform image reference, and lockfile SHA-256 in its metadata; its
npm package version is distinct from the release tag. The locked package
inventory should match across architectures; the separate files bind it to each
platform image. BuildKit's registry SBOM and provenance, the image attestation,
and the file attestation are separate evidence for the same release.

An SBOM is evidence about what shipped, not a vulnerability verdict; the Trivy
JSON files record that verdict. Verify downloaded files with the checksums and
GitHub attestation before using them in audit workflows.
