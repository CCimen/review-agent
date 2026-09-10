# Release Review Agent

Use a prerelease when the App-only path, multi-repository scale, backup
recovery, capacity, or the arm64 runtime lacks evidence for the intended
release claim. An alternate model provider is required only when its support is
claimed in that release. GitHub publishes the matching GHCR images only after
you publish the GitHub release.

## Before a release

- Confirm `LICENSE`, `NOTICE.md`, `CONTRIBUTING.md`, `CITATION.cff`, and
  `THIRD_PARTY_NOTICES.md` are current.
- Merge completed changes into `main` after `CI / required` passes. Select a
  clean, reviewed commit from `main`, or a maintained release branch for a
  backport. The release workflow reruns the canonical gate against that exact
  tagged candidate before publishing an image.
- Confirm the public docs, generated LLM files, and installation-skill mirrors
  are current.
- Update `REVISION` in `scripts/generate_llms_docs.py` and the image example in
  `docs/REPOSITORY_CONTEXT.md` to the same release. Regenerate both LLM files
  and commit them before tagging.
- The Pages gate requires an attached `IMAGE-DIGESTS.txt`. A version-bump
  merge can fail this gate while image qualification is pending; the currently
  published site remains available.
  Successful image qualification automatically retries publication from `main`.
- Record the pilot deployment, one dry run, one published review, and backup
  owner without copying secrets.
- Record one exact prior Review Agent digest as the rollback target, the
  post-migration schema version from `review-agent-admin database ready`, and a
  receipt showing that prior digest passes `database ready`, `doctor`, and the
  smoke test against a restored copy of the post-migration database. If this was
  not tested, state that rollback is limited to a forward fix or backup restore.
- Keep known validation gaps in the release notes. A prerelease must not claim
  production scale that the pilot did not exercise.
- Review `release-image-critical-exceptions.json`. The shared gate blocks every
  critical finding by default and every high finding with an available fix.
  The release-only file may accept exact unfixed critical package versions for
  a bounded period; unknown, changed, stale, or expired findings still fail.

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
   each of `ghcr.io/ccimen/review-agent:<tag>` and
   `ghcr.io/ccimen/review-agent-admin:<tag>` resolves to its recorded workflow
   digest. `IMAGE-DIGESTS.txt` has six entries: a manifest and two platform
   digests per image. Make each package public in GitHub Package settings if anonymous pulls are
   part of the release.
5. Verify downloaded inventory files before using them:

   ```bash
   gh attestation verify ./SBOM-SHA256SUMS.txt --repo CCimen/review-agent
   sha256sum --check SBOM-SHA256SUMS.txt
   ```

6. Wait for **Publish documentation** and confirm the public `llms.txt` names
   the qualified release declared on `main`. The successful image workflow
   retriggers publication automatically; a maintained-branch backport leaves
   `main`'s documentation version unchanged. If needed, run **Publish
   documentation** manually from `main` after qualification.
7. Deploy the immutable digest only after the full release workflow succeeds
   and the generated vulnerability summary is visible. Run `doctor`, queue inspection,
   repository inventory, a dry-run smoke test, and one owner-approved `/review`.
   With the admin panel enabled, deploy both images from the same evidence
   record and compare its authenticated `/api/version` with the release tag
   and `SOURCE-SHA.txt`.

## Roll back

Redeploy only the exact verified rollback digest recorded in the release
evidence above. If no verified target exists, use a forward fix or verified
backup restore. Disable repository admission if the failure affects
authorization or publication. Preserve PostgreSQL and Hermes volumes; follow
the database recovery runbook instead of reversing migrations by hand.

Publish a stable release only after the owner accepts the live correctness,
capacity, recovery, documentation, and support evidence. Do not turn an
untested optional provider or deployment platform into a release claim.
