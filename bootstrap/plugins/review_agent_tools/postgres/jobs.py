"""PostgreSQL durable review-job enqueue and claim operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TypeAlias

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.review import PullRequestId, ReviewRunId, ReviewStatus


class ReviewJobError(ValueError):
    """A durable review-job operation violates its contract."""


class ReviewJobBusy(ReviewJobError):
    """The pull-request enqueue lock could not be acquired within its bound."""


class ReviewJobNotFound(ReviewJobError):
    """The requested review job does not exist."""


class ReviewJobStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class ReviewJob:
    id: int
    review_run_id: ReviewRunId
    status: ReviewJobStatus
    priority: int
    available_at: datetime
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_generation: int
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _ReviewJobRow:
    id: int
    review_run_id: ReviewRunId
    status: str
    priority: int
    available_at: datetime
    attempt_count: int
    max_attempts: int
    lease_owner: str | None
    lease_generation: int
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _ReviewRunScopeRow:
    id: ReviewRunId
    pull_request_id: PullRequestId
    status: str


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    job: ReviewJob


@dataclass(frozen=True, slots=True)
class DuplicateJob:
    job: ReviewJob


JobEnqueue: TypeAlias = EnqueuedJob | DuplicateJob


_JOB_COLUMNS = """
    id, review_run_id, status, priority, available_at, attempt_count,
    max_attempts, lease_owner, lease_generation, lease_expires_at,
    last_heartbeat_at, created_at, started_at, completed_at
"""


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise ReviewJobError("review-job operations require an active transaction")


def _job(row: _ReviewJobRow) -> ReviewJob:
    try:
        status = ReviewJobStatus(row.status)
    except ValueError as exc:
        raise ReviewJobError("stored review job has an unknown status") from exc
    return ReviewJob(
        id=row.id,
        review_run_id=row.review_run_id,
        status=status,
        priority=row.priority,
        available_at=row.available_at,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_generation=row.lease_generation,
        lease_expires_at=row.lease_expires_at,
        last_heartbeat_at=row.last_heartbeat_at,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _integer(value: object, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewJobError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ReviewJobError(f"{field} must be at least {minimum}")
    return value


def _by_run(
    connection: psycopg.Connection[TupleRow], review_run_id: ReviewRunId
) -> ReviewJob | None:
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"SELECT {_JOB_COLUMNS} FROM review_agent.review_jobs "
            "WHERE review_run_id = %s",
            (review_run_id,),
        ).fetchone()
    return _job(row) if row is not None else None


def get_job(
    connection: psycopg.Connection[TupleRow], job_id: int
) -> ReviewJob:
    """Return one durable job at any lifecycle state."""
    _require_transaction(connection)
    resolved_job_id = _integer(job_id, field="job_id", minimum=1)
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"SELECT {_JOB_COLUMNS} FROM review_agent.review_jobs WHERE id = %s",
            (resolved_job_id,),
        ).fetchone()
    if row is None:
        raise ReviewJobNotFound("review job does not exist")
    return _job(row)


def enqueue_run(
    connection: psycopg.Connection[TupleRow],
    *,
    review_run_id: ReviewRunId,
    priority: int,
    max_attempts: int,
) -> JobEnqueue:
    """Create one queue record for an active run and supersede older queued work."""
    _require_transaction(connection)
    resolved_run_id = ReviewRunId(
        _integer(review_run_id, field="review_run_id", minimum=1)
    )
    resolved_priority = _integer(priority, field="priority")
    resolved_max_attempts = _integer(
        max_attempts, field="max_attempts", minimum=1
    )

    with connection.cursor(row_factory=class_row(_ReviewRunScopeRow)) as cursor:
        run = cursor.execute(
            "SELECT id, pull_request_id, status FROM review_agent.review_runs "
            "WHERE id = %s",
            (resolved_run_id,),
        ).fetchone()
    if run is None:
        raise ReviewJobError("review run does not exist")
    try:
        pull_request = connection.execute(
            "SELECT id FROM review_agent.pull_requests WHERE id = %s "
            "FOR NO KEY UPDATE",
            (run.pull_request_id,),
        ).fetchone()
    except errors.LockNotAvailable as exc:
        raise ReviewJobBusy("pull request is busy enqueuing review work") from exc
    if pull_request is None:
        raise ReviewJobError("review run pull request does not exist")

    with connection.cursor(row_factory=class_row(_ReviewRunScopeRow)) as cursor:
        locked_run = cursor.execute(
            "SELECT id, pull_request_id, status FROM review_agent.review_runs "
            "WHERE id = %s",
            (resolved_run_id,),
        ).fetchone()
    if locked_run is None:
        raise ReviewJobError("review run disappeared while enqueuing")
    if locked_run.status != ReviewStatus.RUNNING:
        raise ReviewJobError("only an active review run can be enqueued")

    existing = _by_run(connection, resolved_run_id)
    if existing is not None:
        return DuplicateJob(existing)

    try:
        connection.execute(
            """
            UPDATE review_agent.review_jobs AS job
            SET status = 'superseded', completed_at = statement_timestamp()
            FROM review_agent.review_runs AS run
            WHERE run.id = job.review_run_id
              AND run.pull_request_id = %s
              AND job.status = 'queued'
            """,
            (run.pull_request_id,),
        )
    except errors.LockNotAvailable as exc:
        raise ReviewJobBusy("queued review work is busy") from exc

    try:
        with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
            row = cursor.execute(
                f"""
                INSERT INTO review_agent.review_jobs (
                    review_run_id, status, priority, available_at,
                    attempt_count, max_attempts, lease_generation, created_at
                ) VALUES (
                    %s, 'queued', %s, statement_timestamp(), 0, %s, 0,
                    statement_timestamp()
                )
                ON CONFLICT (review_run_id) DO NOTHING
                RETURNING {_JOB_COLUMNS}
                """,
                (resolved_run_id, resolved_priority, resolved_max_attempts),
            ).fetchone()
    except errors.LockNotAvailable as exc:
        raise ReviewJobBusy("review run is busy while enqueuing") from exc
    if row is not None:
        return EnqueuedJob(_job(row))

    existing = _by_run(connection, resolved_run_id)
    if existing is None:
        raise ReviewJobError("review job could not be resolved after enqueue")
    return DuplicateJob(existing)


def claim_next_job(
    connection: psycopg.Connection[TupleRow],
    *,
    lease_owner: str,
    lease_duration: timedelta,
) -> ReviewJob | None:
    """Claim one ready job with a short, fenced ``SKIP LOCKED`` update."""
    _require_transaction(connection)
    owner = lease_owner.strip()
    if not owner:
        raise ReviewJobError("lease_owner is required")
    if lease_duration <= timedelta(0):
        raise ReviewJobError("lease_duration must be positive")

    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            """
            WITH candidate AS (
                SELECT job.id
                FROM review_agent.review_jobs AS job
                WHERE job.status = 'queued'
                  AND job.available_at <= statement_timestamp()
                  AND job.attempt_count < job.max_attempts
                  AND EXISTS (
                      SELECT 1
                      FROM review_agent.review_runs AS run
                      WHERE run.id = job.review_run_id
                        AND run.status = 'running'
                  )
                ORDER BY job.priority DESC, job.available_at, job.id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE review_agent.review_jobs AS job
            SET status = 'leased',
                attempt_count = job.attempt_count + 1,
                lease_owner = %s,
                lease_generation = job.lease_generation + 1,
                lease_expires_at = statement_timestamp() + %s,
                last_heartbeat_at = statement_timestamp(),
                started_at = COALESCE(job.started_at, statement_timestamp())
            FROM candidate
            WHERE job.id = candidate.id AND job.status = 'queued'
            RETURNING job.*
            """,
            (owner, lease_duration),
        ).fetchone()
    return _job(row) if row is not None else None
