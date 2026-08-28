# Prerelease readiness audit

## Verdict

The implementation and documentation are ready to prepare a first prerelease.
The current commit is not the release commit because the owner has not selected
the prerelease tag and generated metadata must contain that exact tag before it
is published.

## Current evidence

- Python CI passed on `c21d131f62e8c177215a44a6bd8b3dc18f01893b`.
- Documentation CI and GitHub Pages passed on `927d5f08e064cab3a4342e2be5c7113a98db0e7d`;
  the later commit changed only doctor wording and its focused test.
- The live Dokploy deployment at `c21d131` is ready with three enabled
  repositories and drained review and publication queues.
- The multi-repository receipt proves selected-repository authorization,
  three-way leasing, exact-head review, deployment-owned SOUL behavior,
  deterministic supersession/publication, and feedback reporting.
- The release workflow validates the tag, binds it to generated metadata,
  smoke-tests the native image, publishes exact prerelease tags to GHCR, builds
  `linux/amd64` and `linux/arm64`, and emits SBOM, provenance, and digest
  attestation. It has not run because no release exists.

## Owner-authorized release sequence

1. Select the first prerelease tag, for example `v0.1.0-rc.1`.
2. Set generated `REVISION` metadata to that exact tag, regenerate the LLM
   files, validate the complete candidate, commit, and push.
3. Confirm Python and documentation workflows pass on the exact release commit.
4. Publish a GitHub release marked **Pre-release** at that commit.
5. Verify the GHCR digest, both platform manifests, SBOM, provenance, and package
   visibility.
6. Deploy the immutable digest and repeat doctor, inventory, queue, dry-run, and
   one owner-approved live review.

## Required prerelease disclosure

The first release must not claim sustained 100-repository throughput, live
arm64 execution, injected crash/backup recovery, or alternate-provider runtime
validation. These are explicit evidence gaps for later stable-release
promotion, not reasons to add speculative code before a release candidate.
