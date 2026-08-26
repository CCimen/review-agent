"""PostgreSQL review-run start and lifecycle operations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, LiteralString, TypeAlias, cast

import psycopg
from psycopg import sql
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from .. import failure_codes
from ..domain.review import (
    FailureStatusDelivery,
    PullRequestId,
    ReviewPhase,
    ReviewRunId,
    ReviewStatus,
    ReviewSubjectId,
    ResolvedConfig,
    decode_resolved_config,
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


class FailureStatusLeaseLost(ReviewRunError):
    """A failure-status delivery no longer owns its exact lease."""


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
class ReviewRunScope:
    run: ReviewRun
    provider_repository_id: int
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    resolved_config: ResolvedConfig


@dataclass(frozen=True, slots=True)
class _ReviewRunScopeRow:
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
    provider_repository_id: int
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    resolved_config_schema_version: int
    resolved_config_json: str


@dataclass(frozen=True, slots=True)
class StartedRun:
    run: ReviewRun
    superseded_run_id: ReviewRunId | None = None


@dataclass(frozen=True, slots=True)
class DuplicateRun:
    run: ReviewRun
    reason: DuplicateReason


RunStart: TypeAlias = StartedRun | DuplicateRun


@dataclass(frozen=True, slots=True)
class FailureStatusTarget:
    run_id: ReviewRunId
    repository: str
    pr_number: int
    head_sha: str
    status: ReviewStatus
    failure_code: str
    comment_id: int | None
    posted_at: datetime | None
    delivery_status: FailureStatusDelivery
    delivery_attempt_count: int
    delivery_max_attempts: int
    delivery_lease_owner: str | None
    delivery_lease_generation: int


@dataclass(frozen=True, slots=True)
class StoredFailureStatusComment:
    run_id: ReviewRunId
    comment_id: int


@dataclass(frozen=True, slots=True)
class _FailureStatusTargetRow:
    run_id: ReviewRunId
    repository: str
    pr_number: int
    head_sha: str
    status: str
    failure_code: str | None
    comment_id: int | None
    posted_at: datetime | None
    delivery_status: str
    delivery_attempt_count: int
    delivery_max_attempts: int
    delivery_lease_owner: str | None
    delivery_lease_generation: int


@dataclass(frozen=True, slots=True)
class FailureStatusClaim:
    target: FailureStatusTarget


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


def lock_run(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> ReviewRun:
    """Return and lock one run before a related job mutation."""
    _require_transaction(connection)
    run = _by_id(connection, run_id, for_update=True)
    if run is None:
        raise ReviewRunNotFound("review run does not exist")
    return run


def get_run_scope(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    for_update: bool = False,
) -> ReviewRunScope:
    """Return one run with the exact repository, PR, and immutable subject."""
    _require_transaction(connection)
    lock = " FOR UPDATE OF run" if for_update else ""
    with connection.cursor(row_factory=class_row(_ReviewRunScopeRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT run.id, run.pull_request_id, run.review_subject_id,
                   run.request_key, run.trigger_comment_id, run.trigger_user,
                   run.status, run.phase, run.findings_count, run.failure_code,
                   run.started_at, run.last_heartbeat_at, run.completed_at,
                   repository.provider_repository_id,
                   repository.full_name AS repository,
                   pull_request.number AS pr_number,
                   subject.base_sha, subject.head_sha,
                   subject.resolved_config_schema_version,
                   subject.resolved_config::text AS resolved_config_json
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            WHERE run.id = %s{lock}
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise ReviewRunNotFound("review run does not exist")
    return ReviewRunScope(
        run=_run(
            _ReviewRunRow(
                id=row.id,
                pull_request_id=row.pull_request_id,
                review_subject_id=row.review_subject_id,
                request_key=row.request_key,
                trigger_comment_id=row.trigger_comment_id,
                trigger_user=row.trigger_user,
                status=row.status,
                phase=row.phase,
                findings_count=row.findings_count,
                failure_code=row.failure_code,
                started_at=row.started_at,
                last_heartbeat_at=row.last_heartbeat_at,
                completed_at=row.completed_at,
            )
        ),
        provider_repository_id=row.provider_repository_id,
        repository=row.repository,
        pr_number=row.pr_number,
        base_sha=row.base_sha,
        head_sha=row.head_sha,
        resolved_config=decode_resolved_config(
            row.resolved_config_json,
            schema_version=row.resolved_config_schema_version,
        ),
    )


def validate_snapshot(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    expected_head_sha: str | None,
    phase: ReviewPhase,
) -> tuple[ReviewRunScope, bool, bool]:
    """Validate one provider snapshot and atomically terminalize drift."""
    scope = get_run_scope(connection, run_id, for_update=True)
    if scope.repository != repository or scope.pr_number != pr_number:
        raise ReviewRunError("run_id does not match this pull request")
    if expected_head_sha is not None and scope.head_sha != expected_head_sha:
        raise ReviewRunError("head_sha does not match the active review run")
    if scope.run.status is ReviewStatus.SUPERSEDED:
        return scope, False, False
    if scope.run.status is not ReviewStatus.RUNNING:
        raise InvalidReviewTransition("review run is not active")
    if scope.base_sha != base_sha or scope.head_sha != head_sha:
        return ReviewRunScope(
            run=mark_superseded(connection, run_id),
            provider_repository_id=scope.provider_repository_id,
            repository=scope.repository,
            pr_number=scope.pr_number,
            base_sha=scope.base_sha,
            head_sha=scope.head_sha,
            resolved_config=scope.resolved_config,
        ), False, True
    updated = advance_phase(connection, run_id, phase)
    return ReviewRunScope(
        run=updated,
        provider_repository_id=scope.provider_repository_id,
        repository=scope.repository,
        pr_number=scope.pr_number,
        base_sha=scope.base_sha,
        head_sha=scope.head_sha,
        resolved_config=scope.resolved_config,
    ), True, False


def _failure_status_row(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
    *,
    for_update: bool = False,
) -> _FailureStatusTargetRow | None:
    lock = " FOR UPDATE OF run" if for_update else ""
    with connection.cursor(row_factory=class_row(_FailureStatusTargetRow)) as cursor:
        return cursor.execute(
            f"""
            SELECT run.id AS run_id, repository.full_name AS repository,
                   pull_request.number AS pr_number, subject.head_sha,
                   run.status, run.failure_code,
                   run.failure_status_comment_id AS comment_id,
                   run.failure_status_posted_at AS posted_at,
                   run.failure_status_delivery_status AS delivery_status,
                   run.failure_status_delivery_attempt_count AS delivery_attempt_count,
                   run.failure_status_delivery_max_attempts AS delivery_max_attempts,
                   run.failure_status_delivery_lease_owner AS delivery_lease_owner,
                   run.failure_status_delivery_lease_generation AS delivery_lease_generation
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            JOIN review_agent.review_subjects AS subject
              ON subject.id = run.review_subject_id
            WHERE run.id = %s{lock}
            """,
            (run_id,),
        ).fetchone()


def _failure_status_target(row: _FailureStatusTargetRow) -> FailureStatusTarget:
    try:
        status = ReviewStatus(row.status)
        delivery_status = FailureStatusDelivery(row.delivery_status)
    except ValueError as exc:
        raise ReviewRunError("stored review run has an unknown lifecycle state") from exc
    if status not in {ReviewStatus.FAILED, ReviewStatus.SUPERSEDED}:
        raise InvalidReviewTransition("failure status requires a terminal failed run")
    if row.failure_code is None:
        raise ReviewRunError("terminal failed run has no failure code")
    return FailureStatusTarget(
        run_id=row.run_id,
        repository=row.repository,
        pr_number=row.pr_number,
        head_sha=row.head_sha,
        status=status,
        failure_code=row.failure_code,
        comment_id=row.comment_id,
        posted_at=row.posted_at,
        delivery_status=delivery_status,
        delivery_attempt_count=row.delivery_attempt_count,
        delivery_max_attempts=row.delivery_max_attempts,
        delivery_lease_owner=row.delivery_lease_owner,
        delivery_lease_generation=row.delivery_lease_generation,
    )


def failure_status_target(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> FailureStatusTarget:
    """Load the exact terminal run targeted by a deterministic status comment."""
    _require_transaction(connection)
    row = _failure_status_row(connection, run_id)
    if row is None:
        raise ReviewRunNotFound("review run does not exist")
    return _failure_status_target(row)


def claim_next_failure_status(
    connection: psycopg.Connection[TupleRow],
    *,
    lease_owner: str,
    lease_duration: timedelta,
) -> FailureStatusClaim | None:
    """Claim the oldest ready terminal status, recovering expired leases."""
    _require_transaction(connection)
    owner = lease_owner.strip()
    if not owner or lease_duration <= timedelta(0):
        raise ReviewRunError("a lease owner and positive duration are required")
    connection.execute(
        """
        UPDATE review_agent.review_runs
        SET failure_status_delivery_status = 'failed',
            failure_status_delivery_failure_code = 'delivery_attempts_exhausted',
            failure_status_delivery_available_at = NULL,
            failure_status_delivery_lease_owner = NULL,
            failure_status_delivery_lease_expires_at = NULL,
            failure_status_delivery_last_heartbeat_at = NULL,
            failure_status_delivery_completed_at = statement_timestamp()
        WHERE failure_status_delivery_status = 'posting'
          AND failure_status_delivery_lease_expires_at <= statement_timestamp()
          AND failure_status_delivery_attempt_count >= failure_status_delivery_max_attempts
        """
    )
    row = connection.execute(
        """
        WITH candidate AS (
            SELECT id
            FROM review_agent.review_runs
            WHERE (
                (
                    failure_status_delivery_status IN ('pending', 'publish_failed')
                    AND failure_status_delivery_available_at <= statement_timestamp()
                ) OR (
                    failure_status_delivery_status = 'posting'
                    AND failure_status_delivery_lease_expires_at <= statement_timestamp()
                    AND failure_status_delivery_attempt_count < failure_status_delivery_max_attempts
                )
            )
              AND NOT EXISTS (
                  SELECT 1 FROM review_agent.review_runs AS newer
                  WHERE newer.pull_request_id = review_runs.pull_request_id
                    AND newer.id > review_runs.id
              )
            ORDER BY failure_status_delivery_available_at, id
            FOR UPDATE SKIP LOCKED LIMIT 1
        )
        UPDATE review_agent.review_runs AS run
        SET failure_status_delivery_status = 'posting',
            failure_status_delivery_attempt_count = run.failure_status_delivery_attempt_count + 1,
            failure_status_delivery_lease_owner = %s,
            failure_status_delivery_lease_generation = run.failure_status_delivery_lease_generation + 1,
            failure_status_delivery_lease_expires_at = statement_timestamp() + %s,
            failure_status_delivery_last_heartbeat_at = statement_timestamp(),
            failure_status_delivery_failure_code = NULL
        FROM candidate WHERE run.id = candidate.id RETURNING run.id
        """,
        (owner, lease_duration),
    ).fetchone()
    if row is None:
        return None
    target = failure_status_target(connection, ReviewRunId(int(row[0])))
    return FailureStatusClaim(target=target)


def claim_failure_status(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId,
    lease_owner: str, lease_duration: timedelta,
) -> FailureStatusClaim:
    """Claim one explicit ready status for bounded operator recovery."""
    _require_transaction(connection)
    owner = lease_owner.strip()
    if not owner or lease_duration <= timedelta(0):
        raise ReviewRunError("a lease owner and positive duration are required")
    changed = connection.execute(
        """UPDATE review_agent.review_runs AS run SET
        failure_status_delivery_status = 'posting',
        failure_status_delivery_attempt_count = CASE
            WHEN failure_status_delivery_status = 'failed' THEN 1
            ELSE failure_status_delivery_attempt_count + 1
        END,
        failure_status_delivery_lease_owner = %s,
        failure_status_delivery_lease_generation = failure_status_delivery_lease_generation + 1,
        failure_status_delivery_lease_expires_at = statement_timestamp() + %s,
        failure_status_delivery_last_heartbeat_at = statement_timestamp(),
        failure_status_delivery_failure_code = NULL,
        failure_status_delivery_completed_at = NULL
        WHERE id = %s
          AND (
              (failure_status_delivery_status IN ('pending', 'publish_failed')
               AND failure_status_delivery_available_at <= statement_timestamp()
               AND failure_status_delivery_attempt_count < failure_status_delivery_max_attempts)
              OR failure_status_delivery_status = 'failed'
          )
          AND NOT EXISTS (
              SELECT 1 FROM review_agent.review_runs AS newer
              WHERE newer.pull_request_id = run.pull_request_id
                AND newer.id > run.id
          )""",
        (owner, lease_duration, run_id),
    ).rowcount
    if changed != 1:
        raise FailureStatusLeaseLost("failure status is not ready to claim")
    return FailureStatusClaim(target=failure_status_target(connection, run_id))


def require_live_failure_status_lease(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    lease_owner: str,
    lease_generation: int,
) -> FailureStatusTarget:
    _require_transaction(connection)
    target = failure_status_target(connection, run_id)
    eligible = connection.execute(
        """
        SELECT failure_status_delivery_status = 'posting'
           AND failure_status_delivery_lease_owner = %s
           AND failure_status_delivery_lease_generation = %s
           AND failure_status_delivery_lease_expires_at > statement_timestamp()
           AND NOT EXISTS (
                SELECT 1 FROM review_agent.review_runs AS newer
                WHERE newer.pull_request_id = run.pull_request_id
                  AND newer.id > run.id
           )
        FROM review_agent.review_runs AS run WHERE run.id = %s
        """,
        (lease_owner, lease_generation, run_id),
    ).fetchone()
    if eligible != (True,):
        raise FailureStatusLeaseLost("failure-status delivery lease was lost")
    return target


def heartbeat_failure_status(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId,
    lease_owner: str, lease_generation: int, lease_duration: timedelta,
) -> None:
    _require_transaction(connection)
    changed = connection.execute(
        """UPDATE review_agent.review_runs AS run
        SET failure_status_delivery_lease_expires_at = statement_timestamp() + %s,
            failure_status_delivery_last_heartbeat_at = statement_timestamp()
        WHERE id = %s AND failure_status_delivery_status = 'posting'
          AND failure_status_delivery_lease_owner = %s
          AND failure_status_delivery_lease_generation = %s
          AND failure_status_delivery_lease_expires_at > statement_timestamp()""",
        (lease_duration, run_id, lease_owner, lease_generation),
    ).rowcount
    if changed != 1:
        raise FailureStatusLeaseLost("failure-status delivery lease was lost")


def complete_failure_status(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId,
    lease_owner: str, lease_generation: int, comment_id: int,
) -> FailureStatusTarget:
    _require_transaction(connection)
    changed = connection.execute(
        """UPDATE review_agent.review_runs AS run
        SET failure_status_comment_id = %s,
            failure_status_posted_at = statement_timestamp(),
            failure_status_delivery_status = 'posted',
            failure_status_delivery_available_at = NULL,
            failure_status_delivery_lease_owner = NULL,
            failure_status_delivery_lease_expires_at = NULL,
            failure_status_delivery_last_heartbeat_at = NULL,
            failure_status_delivery_failure_code = NULL,
            failure_status_delivery_completed_at = statement_timestamp()
        WHERE id = %s AND failure_status_delivery_status = 'posting'
          AND failure_status_delivery_lease_owner = %s
          AND failure_status_delivery_lease_generation = %s
          AND failure_status_delivery_lease_expires_at > statement_timestamp()
          AND NOT EXISTS (SELECT 1 FROM review_agent.review_runs AS newer
              WHERE newer.pull_request_id = run.pull_request_id
                AND newer.id > run.id)""",
        (comment_id, run_id, lease_owner, lease_generation),
    ).rowcount
    if changed != 1:
        raise FailureStatusLeaseLost("failure-status delivery lease was lost")
    return failure_status_target(connection, run_id)


def retry_failure_status(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId,
    lease_owner: str, lease_generation: int, failure_code: str,
    retry_delay: timedelta,
) -> None:
    _require_transaction(connection)
    normalized_failure_code = failure_code.strip()
    if not normalized_failure_code:
        raise ReviewRunError("failure_code must not be blank")
    target = failure_status_target(connection, run_id)
    exhausted = target.delivery_attempt_count >= target.delivery_max_attempts
    changed = connection.execute(
        """UPDATE review_agent.review_runs SET
        failure_status_delivery_status = %s,
        failure_status_delivery_available_at = CASE WHEN %s THEN NULL ELSE statement_timestamp() + %s END,
        failure_status_delivery_lease_owner = NULL,
        failure_status_delivery_lease_expires_at = NULL,
        failure_status_delivery_last_heartbeat_at = NULL,
        failure_status_delivery_failure_code = %s,
        failure_status_delivery_completed_at = CASE WHEN %s THEN statement_timestamp() ELSE NULL END
        WHERE id = %s AND failure_status_delivery_status = 'posting'
          AND failure_status_delivery_lease_owner = %s
          AND failure_status_delivery_lease_generation = %s
          AND failure_status_delivery_lease_expires_at > statement_timestamp()""",
        ('failed' if exhausted else 'publish_failed', exhausted, retry_delay,
         normalized_failure_code, exhausted, run_id, lease_owner, lease_generation),
    ).rowcount
    if changed != 1:
        raise FailureStatusLeaseLost("failure-status delivery lease was lost")


def failed_runs_needing_status(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str | None,
    pr_number: int | None,
    limit: int = 100,
) -> tuple[FailureStatusTarget, ...]:
    """Return a bounded recovery queue without claiming external delivery."""
    _require_transaction(connection)
    if isinstance(limit, bool) or limit < 1 or limit > 500:
        raise ReviewRunError("failure status limit must be between 1 and 500")
    conditions = [
        "run.status IN ('failed', 'superseded')",
        "run.failure_code IS NOT NULL",
        "run.failure_status_delivery_status IN ('pending', 'publish_failed', 'failed')",
        "(run.failure_status_delivery_status = 'failed' OR "
        "run.failure_status_delivery_available_at <= statement_timestamp())",
        "NOT EXISTS (SELECT 1 FROM review_agent.review_runs AS newer "
        "WHERE newer.pull_request_id = run.pull_request_id AND newer.id > run.id)",
    ]
    parameters: list[object] = []
    if repository is not None:
        conditions.append("lower(repository.full_name) = lower(%s)")
        parameters.append(repository)
    if pr_number is not None:
        conditions.append("pull_request.number = %s")
        parameters.append(pr_number)
    parameters.append(limit)
    query = sql.SQL(
        "SELECT run.id AS run_id, repository.full_name AS repository, "
        "pull_request.number AS pr_number, subject.head_sha, run.status, "
        "run.failure_code, run.failure_status_comment_id AS comment_id, "
        "run.failure_status_posted_at AS posted_at, "
        "run.failure_status_delivery_status AS delivery_status, "
        "run.failure_status_delivery_attempt_count AS delivery_attempt_count, "
        "run.failure_status_delivery_max_attempts AS delivery_max_attempts, "
        "run.failure_status_delivery_lease_owner AS delivery_lease_owner, "
        "run.failure_status_delivery_lease_generation AS delivery_lease_generation "
        "FROM review_agent.review_runs AS run "
        "JOIN review_agent.pull_requests AS pull_request "
        "ON pull_request.id = run.pull_request_id "
        "JOIN review_agent.repositories AS repository "
        "ON repository.id = pull_request.repository_id "
        "JOIN review_agent.review_subjects AS subject "
        "ON subject.id = run.review_subject_id WHERE "
    ) + sql.SQL(" AND ").join(
        sql.SQL(cast(LiteralString, item)) for item in conditions
    ) + sql.SQL(
        " ORDER BY run.completed_at, run.id LIMIT %s"
    )
    with connection.cursor(row_factory=class_row(_FailureStatusTargetRow)) as cursor:
        rows = cursor.execute(query, tuple(parameters)).fetchall()
    return tuple(_failure_status_target(row) for row in rows)


def suppress_unposted_failure_statuses_for_pull_request(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str,
    pr_number: int,
    exclude_run_id: ReviewRunId | None = None,
) -> None:
    """Suppress unposted terminal statuses superseded by another PR outcome."""
    _require_transaction(connection)
    if isinstance(pr_number, bool) or pr_number < 1:
        raise ReviewRunError("pr_number must be positive")
    connection.execute(
        """
        UPDATE review_agent.review_runs AS run
        SET failure_status_delivery_status = 'suppressed',
            failure_status_delivery_available_at = NULL,
            failure_status_delivery_lease_owner = NULL,
            failure_status_delivery_lease_expires_at = NULL,
            failure_status_delivery_last_heartbeat_at = NULL,
            failure_status_delivery_failure_code = NULL,
            failure_status_delivery_completed_at = statement_timestamp()
        FROM review_agent.pull_requests AS pull_request,
             review_agent.repositories AS repository
        WHERE pull_request.id = run.pull_request_id
          AND repository.id = pull_request.repository_id
          AND repository.provider = 'github'
          AND lower(repository.full_name) = lower(%s)
          AND pull_request.number = %s
          AND run.status IN ('failed', 'superseded')
          AND run.failure_status_comment_id IS NULL
          AND run.failure_status_delivery_status NOT IN ('suppressed', 'posted')
          AND (%s::bigint IS NULL OR run.id <> %s::bigint)
        """,
        (repository.strip(), pr_number, exclude_run_id, exclude_run_id),
    )


def clear_failure_status_comment(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
) -> FailureStatusTarget:
    """Forget one prior failure-status comment after its external cleanup."""
    _require_transaction(connection)
    row = _failure_status_row(connection, run_id, for_update=True)
    if row is None:
        raise ReviewRunNotFound("review run does not exist")
    _failure_status_target(row)
    connection.execute(
        """
        UPDATE review_agent.review_runs
        SET failure_status_comment_id = NULL,
            failure_status_posted_at = NULL,
            failure_status_delivery_status = 'suppressed',
            failure_status_delivery_available_at = NULL,
            failure_status_delivery_lease_owner = NULL,
            failure_status_delivery_lease_expires_at = NULL,
            failure_status_delivery_last_heartbeat_at = NULL,
            failure_status_delivery_failure_code = NULL,
            failure_status_delivery_completed_at = statement_timestamp()
        WHERE id = %s
        """,
        (run_id,),
    )
    cleared = _failure_status_row(connection, run_id)
    if cleared is None:
        raise ReviewRunNotFound("review run disappeared after status cleanup")
    return _failure_status_target(cleared)


def failure_status_comments_for_pull_request(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str,
    pr_number: int,
    exclude_run_id: ReviewRunId | None = None,
) -> tuple[StoredFailureStatusComment, ...]:
    """List stored failure-status comments for one GitHub pull request."""
    _require_transaction(connection)
    if isinstance(pr_number, bool) or pr_number < 1:
        raise ReviewRunError("pr_number must be positive")
    with connection.cursor(row_factory=class_row(StoredFailureStatusComment)) as cursor:
        rows = cursor.execute(
            """
            SELECT run.id AS run_id,
                   run.failure_status_comment_id AS comment_id
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE repository.provider = 'github'
              AND lower(repository.full_name) = lower(%s)
              AND pull_request.number = %s
              AND run.failure_status_comment_id IS NOT NULL
              AND (%s::bigint IS NULL OR run.id <> %s::bigint)
            ORDER BY run.id
            """,
            (repository.strip(), pr_number, exclude_run_id, exclude_run_id),
        ).fetchall()
    return tuple(rows)


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

    superseded_run_id: ReviewRunId | None = None
    active = _active_run(connection, pull_request_id)
    if active is not None:
        if active.review_subject_id == review_subject_id:
            return DuplicateRun(run=active, reason="active_run")
        superseded_run_id = mark_superseded(connection, active.id).id

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
        return StartedRun(
            run=_run(row), superseded_run_id=superseded_run_id
        )

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


def reopen_finding_collection(
    connection: psycopg.Connection[TupleRow],
    run_id: ReviewRunId,
) -> ReviewRun:
    """Reopen finding collection after deterministic render validation fails.

    This is the lifecycle's only backward edge. It is deliberately narrower
    than ``advance_phase``: an active reviewing run is heartbeated, an active
    rendering run returns to reviewing, and every other state is rejected.
    """
    _require_transaction(connection)
    run = _by_id(connection, run_id, for_update=True)
    if run is None:
        raise ReviewRunNotFound("review run does not exist")
    if run.status is not ReviewStatus.RUNNING or run.phase not in {
        ReviewPhase.REVIEWING,
        ReviewPhase.RENDERING,
    }:
        raise InvalidReviewTransition(
            "finding collection can reopen only from reviewing or rendering"
        )
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_runs
            SET phase = 'reviewing', last_heartbeat_at = statement_timestamp()
            WHERE id = %s AND status = 'running'
              AND phase IN ('reviewing', 'rendering')
            RETURNING {_RUN_COLUMNS}
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise InvalidReviewTransition(
            "review run stopped before finding collection reopened"
        )
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
                last_heartbeat_at = statement_timestamp(),
                failure_status_delivery_status = 'pending',
                failure_status_delivery_available_at = statement_timestamp()
            WHERE id = %s AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            (code, findings_count, run_id),
        ).fetchone()
    if row is None:
        raise InvalidReviewTransition("review run stopped before failure update")
    return _run(row)


def fail_active_runs(
    connection: psycopg.Connection[TupleRow],
    *,
    run_ids: Sequence[ReviewRunId],
    failure_code: str,
) -> tuple[ReviewRun, ...]:
    """Fail a bounded run set, ignoring rows already terminalized."""
    _require_transaction(connection)
    if not run_ids:
        return ()
    code = failure_code.strip()
    if not code or len(code) > 80:
        raise ReviewRunError("failure_code must contain at most 80 characters")
    with connection.cursor(row_factory=class_row(_ReviewRunRow)) as cursor:
        rows = cursor.execute(
            f"""
            UPDATE review_agent.review_runs
            SET status = 'failed', phase = 'failed', failure_code = %s,
                completed_at = statement_timestamp(),
                last_heartbeat_at = statement_timestamp(),
                failure_status_delivery_status = 'pending',
                failure_status_delivery_available_at = statement_timestamp()
            WHERE id = ANY(%s::bigint[]) AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            (code, [int(run_id) for run_id in run_ids]),
        ).fetchall()
    return tuple(sorted((_run(row) for row in rows), key=lambda run: run.id))


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
                last_heartbeat_at = statement_timestamp(),
                failure_status_delivery_status = 'pending',
                failure_status_delivery_available_at = statement_timestamp()
            WHERE id = %s AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise InvalidReviewTransition("only an active review run can be superseded")
    return _run(row)


def mark_stale_runs_failed(
    connection: psycopg.Connection[TupleRow],
    *,
    cutoff: datetime,
    repository: str | None,
    pr_number: int | None,
) -> tuple[ReviewRunId, ...]:
    """Fail old running runs that have no durable work owner."""
    _require_transaction(connection)
    conditions = [
        "run.status = 'running'",
        "run.last_heartbeat_at < %s",
        # Unknown future states remain owned work until classified explicitly.
        "NOT EXISTS ("
        "SELECT 1 FROM review_agent.review_jobs AS live_job "
        "WHERE live_job.review_run_id = run.id "
        "AND live_job.status NOT IN "
        "('superseded', 'succeeded', 'failed', 'dead_letter')"
        ")",
        "NOT EXISTS ("
        "SELECT 1 FROM review_agent.publications AS live_publication "
        "WHERE live_publication.review_run_id = run.id "
        "AND live_publication.status NOT IN ('posted', 'failed', 'stale')"
        ")",
    ]
    parameters: list[object] = [cutoff]
    if repository is not None:
        conditions.append("lower(repository.full_name) = lower(%s)")
        parameters.append(repository)
    if pr_number is not None:
        conditions.append("pull_request.number = %s")
        parameters.append(pr_number)
    parameters.append(failure_codes.STALE_TIMEOUT)
    rows = connection.execute(
        sql.SQL(cast(LiteralString,
            "WITH target AS ("
            "SELECT run.id FROM review_agent.review_runs AS run "
            "JOIN review_agent.pull_requests AS pull_request "
            "ON pull_request.id = run.pull_request_id "
            "JOIN review_agent.repositories AS repository "
            "ON repository.id = pull_request.repository_id "
            f"WHERE {' AND '.join(conditions)} FOR UPDATE OF run SKIP LOCKED"
            ") UPDATE review_agent.review_runs AS run "
            "SET status = 'failed', phase = 'failed', "
            "failure_code = %s, "
            "completed_at = statement_timestamp(), "
            "last_heartbeat_at = statement_timestamp(), "
            "failure_status_delivery_status = 'pending', "
            "failure_status_delivery_available_at = statement_timestamp() "
            "FROM target WHERE run.id = target.id RETURNING run.id"
        )),
        tuple(parameters),
    ).fetchall()
    return tuple(sorted(ReviewRunId(int(row[0])) for row in rows))
