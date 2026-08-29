# Contributing to Review Agent

Issues and pull requests are welcome. Keep changes focused and do not include
credentials or personal data. Install the pinned development tools alongside
the runtime dependencies:

```bash
python3 -m pip install --requirement requirements.txt --requirement requirements-dev.txt
npm install --global pyright@1.1.408
```

Run the fast bundle for every change. Run the PostgreSQL contract when Docker
is available, and always when database or durable workflow behavior changes:

```bash
./scripts/check_bundle.sh
./scripts/check_postgres_schema.sh
```

Repository rulesets should require the stable `CI / required` check. That check
passes only after the Python, PostgreSQL, and container-image jobs succeed.

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
