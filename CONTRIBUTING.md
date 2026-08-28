# Contributing to Review Agent

Issues and pull requests are welcome. Keep changes focused, do not include
credentials or personal data, and run the checks relevant to your change. Use
the full bundle before proposing a release:

```bash
./scripts/check_bundle.sh
```

## Sign off commits

Review Agent uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/).
Sign off each contribution:

```bash
git commit --signoff
```

By submitting a contribution, you certify the DCO and license that contribution
under `EUPL-1.2` (Version 1.2 only). The DCO does not transfer copyright or give
the project permission to relicense your contribution. A later license change
may require the affected contributors' consent.

Please describe the user or operator outcome, include focused tests for changed
behavior, and update public documentation when the contract changes.
