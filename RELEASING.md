# Release Review Agent

Use a prerelease while multi-repository scale, backup recovery, arm64 runtime,
and any claimed alternate model provider still need live proof. GitHub publishes
the matching GHCR image only after you publish the GitHub release.

## Before a release

- Confirm `LICENSE`, `NOTICE.md`, `CONTRIBUTING.md`, `CITATION.cff`, and
  `THIRD_PARTY_NOTICES.md` are current.
- Confirm `main` is clean and `CI / required` passed on the exact commit. This
  check covers the Python bundle, PostgreSQL contract, image smoke test, and
  dependency vulnerability policy.
- Confirm the public docs, generated LLM files, and installation-skill mirrors
  are current.
- Update `REVISION` in `scripts/generate_llms_docs.py`, regenerate both LLM
  files, and commit them before tagging.
- Confirm the documentation workflow passed on the release commit. Dispatch
  **Publish documentation** when path filters did not start it.
- Record the pilot deployment, one dry run, one published review, and backup
  owner without copying secrets.
- Record one exact prior Review Agent digest as the rollback target, the
  post-migration schema version from `review-agent-admin database ready`, and a
  receipt showing that prior digest passes `database ready`, `doctor`, and the
  smoke test against a restored copy of the post-migration database. If this was
  not tested, state that rollback is limited to a forward fix or backup restore.
- Keep known validation gaps in the release notes. A prerelease must not claim
  production scale that the pilot did not exercise.

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

## Publish a prerelease

1. Choose a SemVer prerelease such as `v0.1.0-rc.1` and target the exact verified
   `main` commit.
2. Create a GitHub release, mark it **Pre-release**, and publish concise notes:
   shipped behavior, setup path, validation evidence, known gaps, and rollback.
3. Wait for **Publish container image**. It verifies the tag and generated
   release documentation, runs `CI / required` against that exact source, then
   publishes `linux/amd64` and `linux/arm64`. A failed Python, PostgreSQL, image
   smoke, or dependency check blocks publication. The workflow
   creates registry SBOM and provenance attestations, then scans both exact
   published platform digests. A failed platform scan fails the release workflow;
   retain its reports for triage and do not deploy the affected digest. A
   prerelease does not update `latest`.
4. Confirm the release contains per-platform CycloneDX JSON, SPDX JSON, and
   readable tables; the focused Python-runtime CycloneDX file; both
   `vulnerability-linux-*.json` reports; `IMAGE-DIGESTS.txt`; and
   `SBOM-SHA256SUMS.txt`. Confirm
   `ghcr.io/ccimen/review-agent:<tag>` resolves to the recorded workflow digest.
   Make the package public in GitHub Package settings if anonymous pulls are
   part of the release.
5. Verify downloaded inventory files before using them:

   ```bash
   gh attestation verify ./SBOM-SHA256SUMS.txt --repo CCimen/review-agent
   sha256sum --check SBOM-SHA256SUMS.txt
   ```

6. Deploy the immutable digest to the pilot. Run `doctor`, queue inspection,
   repository inventory, a dry-run smoke test, and one owner-approved `/review`.

## Roll back

Redeploy only the exact verified rollback digest recorded in the release
evidence above. If no verified target exists, use a forward fix or verified
backup restore. Disable repository admission if the failure affects
authorization or publication. Preserve PostgreSQL and Hermes volumes; follow
the database recovery runbook instead of reversing migrations by hand.

Promote a later build to a stable release only after the owner accepts the live
scale, recovery, documentation, and support evidence.
