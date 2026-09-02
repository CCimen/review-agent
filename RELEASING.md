# Release Review Agent

Use a prerelease when the App-only path, multi-repository scale, backup
recovery, capacity, or the arm64 runtime lacks evidence for the intended
release claim. An alternate model provider is required only when its support is
claimed in that release. GitHub publishes the matching GHCR image only after
you publish the GitHub release.

## Before a release

- Confirm `LICENSE`, `NOTICE.md`, `CONTRIBUTING.md`, `CITATION.cff`, and
  `THIRD_PARTY_NOTICES.md` are current.
- Confirm the current `main` is clean and `CI / required` passed. Keep `main`
  and GitHub Pages on the latest qualified release while preparing a candidate
  that changes the public release revision. The release workflow reruns the
  canonical gate against the exact tagged candidate before publishing an image.
- Confirm the public docs, generated LLM files, and installation-skill mirrors
  are current.
- Update `REVISION` in `scripts/generate_llms_docs.py`, regenerate both LLM
  files, and commit them before tagging.
- Do not push a new public release revision to `main` before its release
  workflow succeeds. The Pages workflow verifies that the declared release has
  an attached `IMAGE-DIGESTS.txt`; an early push fails without replacing the
  currently published site.
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
npm --prefix website run build
python3 scripts/check_docs.py --build-dir website/build
python3 scripts/generate_llms_docs.py --check
python3 scripts/sync_install_skill.py --check
git diff --check
```

## Publish a release

1. Commit the locally validated candidate without pushing its branch. Create
   and push an exact SemVer tag such as `v0.1.0-rc.1` or `v0.1.0`. Push the tag,
   not the changed `main` branch, so the public site continues to name the last
   qualified release.
2. Publish the GitHub release from that existing tag. For an RC, mark it
   **Pre-release**; leave a stable release unmarked. Publish concise notes:
   shipped behavior, setup path, validation evidence, known gaps, and rollback.
3. Wait for **Publish container image**. It verifies the tag and generated
   release documentation, runs `CI / required` against that exact source, then
   publishes `linux/amd64` and `linux/arm64`. A failed Python, PostgreSQL, image
   smoke, or dependency check blocks publication. The workflow
   creates registry SBOM and provenance attestations, then scans both exact
   published platform digests. A failed platform scan fails the release workflow;
   retain its reports for triage and do not deploy the affected digest. Evidence
   generation and scanning have read-only repository and package access. Only
   after those checks pass does a separate job verify the closed file set,
   checksums, source SHA, and published image digest, attest and attach the
   files, then update the release notes with the generated vulnerability
   summary. The exact published amd64 digest also passes the normal runtime
   image smoke contract before the release is qualified for deployment.
   A prerelease does not update `latest`; a stable release does.
4. Confirm the release contains per-platform CycloneDX JSON, SPDX JSON, and
   readable tables; the focused Python-runtime CycloneDX file; both
   `vulnerability-linux-*.json` reports; `VULNERABILITY-POLICY.json`;
   `VULNERABILITY-SUMMARY.md`; `IMAGE-DIGESTS.txt`; `SOURCE-SHA.txt`; and
   `SBOM-SHA256SUMS.txt`. Confirm
   `ghcr.io/ccimen/review-agent:<tag>` resolves to the recorded workflow digest.
   Make the package public in GitHub Package settings if anonymous pulls are
   part of the release.
5. Verify downloaded inventory files before using them:

   ```bash
   gh attestation verify ./SBOM-SHA256SUMS.txt --repo CCimen/review-agent
   sha256sum --check SBOM-SHA256SUMS.txt
   ```

6. Fast-forward `main` to the qualified tagged commit. Wait for **Publish
   documentation** and confirm the public `llms.txt` now names the release. The
   workflow refuses to publish a revision without qualified image evidence.
7. Deploy the immutable digest only after the full release workflow succeeds
   and the generated vulnerability summary is visible. Run `doctor`, queue inspection,
   repository inventory, a dry-run smoke test, and one owner-approved `/review`.

## Roll back

Redeploy only the exact verified rollback digest recorded in the release
evidence above. If no verified target exists, use a forward fix or verified
backup restore. Disable repository admission if the failure affects
authorization or publication. Preserve PostgreSQL and Hermes volumes; follow
the database recovery runbook instead of reversing migrations by hand.

Publish a stable release only after the owner accepts the live correctness,
capacity, recovery, documentation, and support evidence. Do not turn an
untested optional provider or deployment platform into a release claim.
