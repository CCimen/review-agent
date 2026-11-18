"""Seeding helpers for PostgreSQL tests.

Each helper composes the production functions the live review path calls and
adds no validation of its own. Production code never imports this module.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent.domain.review import (  # noqa: E402
    DiffState,
    FileReadDefinition,
    ReviewRunId,
    resolve_changed_file,
    resolve_changed_file_count,
    resolve_diff_observation,
)
from review_agent.postgres import (  # noqa: E402
    coverage as postgres_coverage,
    review_runs as postgres_review_runs,
)
from review_agent.postgres.runtime import PostgreSQLRuntime  # noqa: E402
from review_agent.review import run_application  # noqa: E402


def start_postgres_review(
    runtime: PostgreSQLRuntime, request: run_application.PostgresRunRequest
) -> postgres_review_runs.RunStart:
    """Start one exact review run without a durable job."""
    with runtime.transaction() as connection:
        pull_request_id, subject_id = run_application.ensure_review_scope(
            connection, request
        )
        return run_application.start_run_in_transaction(
            connection,
            pull_request_id=pull_request_id,
            review_subject_id=subject_id,
            request_key=request.request_key,
            trigger_comment_id=request.trigger_comment_id,
            trigger_user=request.trigger_user,
        )


def register_postgres_changed_files(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    files: Sequence[run_application.PostgresChangedFile],
    changed_files_reported: int,
    registration_complete: bool,
) -> postgres_coverage.CoverageRegistration:
    """Register one changed-file batch for a run."""
    resolved = tuple(
        resolve_changed_file(
            path=item.path,
            change_status=item.change_status,
            previous_path=item.previous_path,
            domain=item.domain,
            review_mode=item.review_mode,
        )
        for item in files
    )
    with runtime.transaction() as connection:
        return postgres_coverage.insert_changed_files(
            connection,
            run_id=run_id,
            files=resolved,
            changed_files_reported=resolve_changed_file_count(changed_files_reported),
            registration_complete=registration_complete,
        )


def record_postgres_diff_observation(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    paths: Sequence[str],
    state: DiffState,
    unavailable_reason: str = "",
) -> int:
    """Record one diff outcome for registered paths."""
    observation = resolve_diff_observation(
        paths=paths, state=state, unavailable_reason=unavailable_reason
    )
    with runtime.transaction() as connection:
        return postgres_coverage.record_diff_observation(
            connection, run_id=run_id, observation=observation
        )


def record_postgres_file_reads(
    runtime: PostgreSQLRuntime,
    *,
    run_id: ReviewRunId,
    reads: Sequence[FileReadDefinition],
) -> postgres_coverage.FileReadBatch:
    """Record source ranges resolved with ``resolve_file_read``."""
    with runtime.transaction() as connection:
        return postgres_coverage.insert_file_reads(
            connection, run_id=run_id, reads=tuple(reads)
        )
