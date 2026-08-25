# Goal Maker Handoff

`state.yaml` is authoritative.

## Current state

- Work in `/Users/ccimen/Documents/ChatGPT/Security Review Infra`; direct commits
  and pushes to `CCimen/review-agent` `main` are authorized.
- The approved maintainability-first platform and documentation plan completed
  through T113. The follow-up review-remediation tranche has completed H2-H5 in
  bounded owner-level slices.
- T122 completed at `5a319a8634a429a8d73443bc098c922d4052a9a3`, delivering
  terminal failures through the existing leased PostgreSQL publisher with exact
  ambiguous-write recovery and operator retry recovery.
- T199 is the sole active task: audit the verified review findings against the
  completed H2-H5 work and select only a remaining direct blocker. H1, the
  immutable effective-review contract, remains the expected candidate unless
  current source disproves it.
- Public identity is “Review Agent.” `sundsvall-standard` remains a selectable
  municipal profile. PostgreSQL is the only application persistence contract.

## Current boundary

Treat the external review as evidence, not authority. Fix confirmed correctness
and reliability defects at their existing canonical owners. Keep GitHub App,
repository policy overlays, chat, scanners, external brokers, broad module
splits, and unmeasured scaling machinery outside the tranche unless a verified
failure mode directly requires them.

Preserve user-owned `refactor-plan1.md`.

After the correctness audit, continue the separately authorized product-readiness
work: an evidence-backed Python and performance audit, only justified module
splits, safe update and rollback ownership, separate migration credentials,
operator experience, release-triggered GHCR packaging, and final documentation
polish. Do not create a tag or release until every approved task is complete;
then create one deliberate prerelease.
