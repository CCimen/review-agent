"""Canonical review-run and review-job failure-code vocabularies.

Run codes are written to the durable run row and surfaced by the operator CLI;
job codes describe execution failures on the one-to-one durable job. Keeping
both named sets here prevents lifecycle and delivery paths from inventing codes.
"""

from __future__ import annotations

from typing import Final

# A review run was abandoned/failed without a more specific code (best-effort failer).
REVIEW_FAILED: Final = "review_failed"
# The reaper failed a run whose heartbeat stopped past the stale cutoff.
STALE_TIMEOUT: Final = "stale_timeout"
# The pull request base/head snapshot changed, so a newer explicit review may proceed.
SNAPSHOT_SUPERSEDED: Final = "snapshot_superseded"
# A duplicate active run was retired during a schema/lifecycle migration.
SUPERSEDED_DUPLICATE_MIGRATION: Final = "superseded_duplicate_migration"
# review_deliver raised a known ToolInputError/ReviewMemoryError before publishing.
REVIEW_DELIVER_ERROR: Final = "review_deliver_error"
# review_deliver raised an unexpected error before publishing.
UNEXPECTED_REVIEW_DELIVER_FAILURE: Final = "unexpected_review_deliver_failure"
# GitHub cannot render the exact pull-request diff for this snapshot.
GITHUB_DIFF_UNAVAILABLE: Final = "github_diff_406"
# A durable job consumed its configured attempt budget.
JOB_RETRY_EXHAUSTED: Final = "job_retry_exhausted"
# A durable job reported a non-retryable execution failure.
JOB_EXECUTION_FAILED: Final = "job_execution_failed"

# Job-row causes are deliberately separate from final review-run outcomes.
JOB_LEASE_EXPIRED: Final = "job_lease_expired"
JOB_RETRYABLE_EXECUTION: Final = "job_retryable_execution"
JOB_TERMINAL_EXECUTION: Final = "job_terminal_execution"
JOB_RUN_FAILED: Final = "job_run_failed"

# The complete set, for validation/telemetry callers that want to enumerate codes.
ALL: Final = frozenset(
    {
        REVIEW_FAILED,
        STALE_TIMEOUT,
        SNAPSHOT_SUPERSEDED,
        SUPERSEDED_DUPLICATE_MIGRATION,
        REVIEW_DELIVER_ERROR,
        UNEXPECTED_REVIEW_DELIVER_FAILURE,
        GITHUB_DIFF_UNAVAILABLE,
        JOB_RETRY_EXHAUSTED,
        JOB_EXECUTION_FAILED,
    }
)

JOB_ALL: Final = frozenset(
    {
        JOB_LEASE_EXPIRED,
        JOB_RETRYABLE_EXECUTION,
        JOB_TERMINAL_EXECUTION,
        JOB_RUN_FAILED,
    }
)
