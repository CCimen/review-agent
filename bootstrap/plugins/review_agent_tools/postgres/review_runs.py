"""PostgreSQL review-run start and lifecycle operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.review import (
    PullRequestId,
    ReviewPhase,
    ReviewRunId,
    ReviewStatus,
    ReviewSubjectId,
)


DuplicateReason: TypeAlias = Literal["request_key", "active_run"]


class ReviewRunError(ValueError):
    """A review-run operation violates its lifecycle contract."""


class DuplicateReviewRequest(ReviewRunError):
    """A durable request key was reused for another exact review."""


class ReviewRunBusy(ReviewRunError):
    """The pull-request start lock could not be acquired within its bound."""


class ReviewRunNotFound(ReviewRunError):
    """The requested review run does not exist."""


class InvalidReviewTransition(ReviewRunError):
    """The requested transition is invalid for the stored run state."""


@dataclass(frozen=True, slots=True)
class ReviewRun:
    id: ReviewRunId
    pull_request_id: PullRequestId
    review_subject_id: ReviewSubjectId
    request_key: str
    trigger_comment_id: int | None
    trigger_user: str | None
    status: ReviewStatus
    phase: ReviewPhase
    findings_count: int | None
    failure_code: str | None
    started_at: datetime
    last_heartbeat_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class StartedRun:
    run: ReviewRun


@dataclass(frozen=True, slots=True)
class DuplicateRun:
    run: ReviewRun
    reason: DuplicateReason


RunStart: TypeAlias = StartedRun | DuplicateRun


@dataclass(frozen=True, slots=True)
class _ReviewRunRow:
    id: ReviewRunId
    pull_request_id: PullRequestId
    review_subject_id: ReviewSubjectId
    request_key: str
    trigger_comment_id: int | None
    trigger_user: str | None
    status: str
    phase: str
    findings_count: int | None
    failure_code: str | None
    started_at: datetime
    last_heartbeat_at: datetime
    completed_at: datetime | None


_RUN_COLUMNS = """
    id, pull_request_id, review_subject_id, request_key, trigger_comment_id,
    trigger_user, status, phase, findings_count, failure_code, started_at,
    last_heartbeat_at, completed_at
"""

_NEXT_PHASE = {
    ReviewPhase.ACCEPTED: ReviewPhase.FETCHING_PR,
    ReviewPhase.FETCHING_PR: ReviewPhase.COLLECTING_DIFF,
    ReviewPhase.COLLECTING_DIFF: ReviewPhase.REVIEWING,
    ReviewPhase.REVIEWING: ReviewPhase.RENDERING,
    ReviewPhase.RENDERING: ReviewPhase.PUBLISHING,
}
_ACTIVE_PHASES = frozenset(_NEXT_PHASE) | {ReviewPhase.PUBLISHING}


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise ReviewRunError("review-run operations require an active transaction")


def _run(row: _ReviewRunRow) -> ReviewRun:
    try:
        status = ReviewStatus(row.status)
        phase = ReviewPhase(row.phase)
    except ValueError as exc:
        raise ReviewRunError("stored review run has an unknown lifecycle value") from exc
    return ReviewRun(
        id=row.id,
        pull_request_id=row.pull_request_id,
        review_subject_id=row.review_subject_id,
        request_key=row.request_key,
        trigger_comment_id=row.trigger_comment_id,
        trigger_user=row.trigger_user,
        status=status,
        phase=phase,
        findings_count=row.findings_count,
        failure_code=row.failure_code,
        started_at=row.started_at,
        last_heartbeat_at=row.last_heartbeat_at,
        completed_at=row.completed_at,
    )


def _request_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ReviewRunError("request_key is required")
    if len(normalized) > 500:
        raise ReviewRunError("request_key exceeds 500 characters")
    return normalized


def _trigger_user(value: str) -> str | None:
    normalized = " ".join(value.strip().split())
    if len(normalized) > 200:
        raise ReviewRunError("trigger_user exceeds 200 characters")
    return normalized or None


def _by_request_key(
    connection: psycopg.Connection[TupleRow], request_key: str
) -> ReviewRun | None:
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"SELECT {_RUN_COLUMNS} FROM review_agent.review_runs "
            "WHERE request_key = %s",
            (request_key,),
        ).fetchone()
    return _run(row) if row is not None else None


def _active_run(
    connection: psycopg.Connection[TupleRow], pull_request_id: PullRequestId
) -> ReviewRun | None:
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"SELECT {_RUN_COLUMNS} FROM review_agent.review_runs "
            "WHERE pull_request_id = %s AND status = 'running'",
            (pull_request_id,),
        ).fetchone()
    return _run(row) if row is not None else None


def _by_id(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    for_update: bool = False,
) -> ReviewRun | None:
    lock = " FOR UPDATE" if for_update else ""
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"SELECT {_RUN_COLUMNS} FROM review_agent.review_runs "
            f"WHERE id = %s{lock}",
            (run_id,),
        ).fetchone()
    return _run(row) if row is not None else None


def get_run(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> ReviewRun:
    """Return one typed review run at any lifecycle state."""
    _require_transaction(connection)
    run = _by_id(connection, run_id)
    if run is None:
        raise ReviewRunNotFound("review run does not exist")
    return run


def _same_request(
    existing: ReviewRun,
    *,
    pull_request_id: PullRequestId,
    review_subject_id: ReviewSubjectId,
) -> DuplicateRun:
    if (
        existing.pull_request_id != pull_request_id
        or existing.review_subject_id != review_subject_id
    ):
        raise DuplicateReviewRequest(
            "request_key already belongs to another pull request or exact subject"
        )
    return DuplicateRun(run=existing, reason="request_key")


def start_run(
    connection: psycopg.Connection[TupleRow],
    *,
    pull_request_id: PullRequestId,
    review_subject_id: ReviewSubjectId,
    request_key: str,
    trigger_comment_id: int | None = None,
    trigger_user: str = "",
) -> RunStart:
    """Start one exact review under a repository-local serialization lock."""
    _require_transaction(connection)
    request_key = _request_key(request_key)
    if trigger_comment_id is not None and (
        isinstance(trigger_comment_id, bool) or trigger_comment_id < 1
    ):
        raise ReviewRunError("trigger_comment_id must be positive")
    user = _trigger_user(trigger_user)

    try:
        pull_request = connection.execute(
            "SELECT id FROM review_agent.pull_requests WHERE id = %s "
            "FOR NO KEY UPDATE",
            (pull_request_id,),
        ).fetchone()
    except errors.LockNotAvailable as exc:
        raise ReviewRunBusy("pull request is busy starting another review") from exc
    if pull_request is None:
        raise ReviewRunError("pull request does not exist")

    existing_request = _by_request_key(connection, request_key)
    if existing_request is not None:
        return _same_request(
            existing_request,
            pull_request_id=pull_request_id,
            review_subject_id=review_subject_id,
        )

    active = _active_run(connection, pull_request_id)
    if active is not None:
        if active.review_subject_id == review_subject_id:
            return DuplicateRun(run=active, reason="active_run")
        mark_superseded(connection, active.id)

    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"""
            INSERT INTO review_agent.review_runs (
                pull_request_id, review_subject_id, request_key,
                trigger_comment_id, trigger_user, status, phase,
                started_at, last_heartbeat_at
            ) VALUES (
                %s, %s, %s, %s, %s, 'running', 'accepted',
                statement_timestamp(), statement_timestamp()
            )
            ON CONFLICT DO NOTHING
            RETURNING {_RUN_COLUMNS}
            """,
            (
                pull_request_id,
                review_subject_id,
                request_key,
                trigger_comment_id,
                user,
            ),
        ).fetchone()
    if row is not None:
        return StartedRun(run=_run(row))

    existing_request = _by_request_key(connection, request_key)
    if existing_request is not None:
        return _same_request(
            existing_request,
            pull_request_id=pull_request_id,
            review_subject_id=review_subject_id,
        )
    active = _active_run(connection, pull_request_id)
    if active is not None:
        return DuplicateRun(run=active, reason="active_run")
    raise ReviewRunError("review run could not be resolved after insert")


def advance_phase(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    phase: ReviewPhase,
) -> ReviewRun:
    """Advance one active run by exactly one phase, or heartbeat its phase."""
    _require_transaction(connection)
    if phase not in _ACTIVE_PHASES:
        raise InvalidReviewTransition("target phase is not an active review phase")
    run = _by_id(connection, run_id, for_update=True)
    if run is None:
        raise ReviewRunNotFound("review run does not exist")
    if run.status is not ReviewStatus.RUNNING or run.phase not in _ACTIVE_PHASES:
        raise InvalidReviewTransition("review run is not active")
    if phase != run.phase and _NEXT_PHASE.get(run.phase) is not phase:
        raise InvalidReviewTransition(
            f"review phase cannot move from {run.phase.value} to {phase.value}"
        )
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_runs
            SET phase = %s, last_heartbeat_at = statement_timestamp()
            WHERE id = %s AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            (phase.value, run_id),
        ).fetchone()
    if row is None:
        raise InvalidReviewTransition("review run stopped before phase update")
    return _run(row)


def complete_run(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    findings_count: int,
) -> ReviewRun:
    """Complete one published run from its final active phase."""
    _require_transaction(connection)
    if isinstance(findings_count, bool) or findings_count < 0:
        raise ReviewRunError("findings_count must be zero or greater")
    run = _by_id(connection, run_id, for_update=True)
    if run is None:
        raise ReviewRunNotFound("review run does not exist")
    if run.status is not ReviewStatus.RUNNING or run.phase is not ReviewPhase.PUBLISHING:
        raise InvalidReviewTransition(
            "review run can complete only from the publishing phase"
        )
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_runs
            SET status = 'completed', phase = 'posted', findings_count = %s,
                completed_at = statement_timestamp(),
                last_heartbeat_at = statement_timestamp()
            WHERE id = %s AND status = 'running' AND phase = 'publishing'
            RETURNING {_RUN_COLUMNS}
            """,
            (findings_count, run_id),
        ).fetchone()
    if row is None:
        raise InvalidReviewTransition("review run stopped before completion")
    return _run(row)


def fail_run(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    failure_code: str,
    findings_count: int | None = None,
) -> ReviewRun:
    """Fail one active run with a bounded canonical failure code."""
    _require_transaction(connection)
    code = failure_code.strip()
    if not code or len(code) > 80:
        raise ReviewRunError("failure_code must contain at most 80 characters")
    if findings_count is not None and (
        isinstance(findings_count, bool) or findings_count < 0
    ):
        raise ReviewRunError("findings_count must be zero or greater")
    run = _by_id(connection, run_id, for_update=True)
    if run is None:
        raise ReviewRunNotFound("review run does not exist")
    if run.status is not ReviewStatus.RUNNING:
        raise InvalidReviewTransition("only an active review run can fail")
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_runs
            SET status = 'failed', phase = 'failed', failure_code = %s,
                findings_count = %s, completed_at = statement_timestamp(),
                last_heartbeat_at = statement_timestamp()
            WHERE id = %s AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            (code, findings_count, run_id),
        ).fetchone()
    if row is None:
        raise InvalidReviewTransition("review run stopped before failure update")
    return _run(row)


def mark_superseded(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> ReviewRun:
    """Terminalize one active run whose exact subject is no longer current."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_runs
            SET status = 'superseded', phase = 'superseded',
                failure_code = 'snapshot_superseded',
                completed_at = statement_timestamp(),
                last_heartbeat_at = statement_timestamp()
            WHERE id = %s AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise InvalidReviewTransition("only an active review run can be superseded")
    return _run(row)
