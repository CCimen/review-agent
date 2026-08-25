# GitHub App persistence receipt

Commit `0ad65321e59461425582445142fc7531723fc8c5` adds the
PostgreSQL-owned installation and selected-repository lifecycle.

The slice keeps GitHub installation access separate from Review Agent
enablement. Repository identity remains owned by the existing registry and is
keyed by GitHub's stable repository ID. Installation suspension, deletion, and
repository removal fail closed under concurrent transitions and retain durable,
idempotent audit history.

Validation:

- PostgreSQL contract: 161 tests passed, including two real two-connection lock
  races, migration/readiness, and backup/restore.
- Pyright: zero errors.
- Claude Opus/high commit gate: green, score 8.

No GitHub network calls, App credentials, direct webhook route, or runtime
cutover are part of this commit.
