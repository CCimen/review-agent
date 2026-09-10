"""PostgreSQL owner for fenced administrative review-run actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from .. import failure_codes, review_run_application
from ..domain.review import ReviewPhase, ReviewRunId, ReviewStatus
from . import jobs, review_runs


class AdminRunActionError(ValueError):
    """An administrative run action is invalid or no longer current."""


class AdminRunActionStale(AdminRunActionError):
    """The submitted run and job snapshot is no longer current."""


class RunAction(StrEnum):
    RELEASE_RETRY = "release_retry"
    CANCEL = "cancel"
    MARK_STALLED = "mark_stalled"


@dataclass(frozen=True, slots=True)
class ActionAvailability:
    available: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    id: ReviewRunId
    status: ReviewStatus
    phase: ReviewPhase
    last_heartbeat_at: datetime


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    id: int
    status: jobs.ReviewJobStatus
    lease_generation: int
    available_at: datetime
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminRunActionEvent:
    id: int
    action: RunAction
    actor: str
    reason: str
    previous_run_status: ReviewStatus
    previous_job_status: jobs.ReviewJobStatus
    expected_lease_generation: int
    expected_available_at: datetime
    stale_after_minutes: int | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RunControls:
    run: RunSnapshot
    job: JobSnapshot | None
    release_retry: ActionAvailability
    cancel: ActionAvailability
    mark_stalled: ActionAvailability
    audit: tuple[AdminRunActionEvent, ...]


@dataclass(frozen=True, slots=True)
class ActionRequest:
    action: RunAction
    expected_job_id: int
    expected_lease_generation: int
    expected_status: jobs.ReviewJobStatus
    expected_available_at: datetime
    actor: str
    reason: str
    stale_after_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class _AuditRow:
    id: int
    action: str
    actor: str
    reason: str
    previous_run_status: str
    previous_job_status: str
    expected_lease_generation: int
    expected_available_at: datetime
    stale_after_minutes: int | None
    recorded_at: datetime


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise AdminRunActionError("admin run actions require an active transaction")


def _moment(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise AdminRunActionError("now must include a timezone")
    return moment


def _audit_text(value: str, *, field: str, maximum: int) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > maximum:
        raise AdminRunActionError(f"{field} must be 1 to {maximum} characters")
    return normalized


def _job_snapshot(job: jobs.ReviewJob) -> JobSnapshot:
    return JobSnapshot(
        id=job.id,
        status=job.status,
        lease_generation=job.lease_generation,
        available_at=job.available_at,
        lease_expires_at=job.lease_expires_at,
        last_heartbeat_at=job.last_heartbeat_at,
    )


def _audit(row: _AuditRow) -> AdminRunActionEvent:
    try:
        return AdminRunActionEvent(
            id=row.id,
            action=RunAction(row.action),
            actor=row.actor,
            reason=row.reason,
            previous_run_status=ReviewStatus(row.previous_run_status),
            previous_job_status=jobs.ReviewJobStatus(row.previous_job_status),
            expected_lease_generation=row.expected_lease_generation,
            expected_available_at=row.expected_available_at,
            stale_after_minutes=row.stale_after_minutes,
            recorded_at=row.recorded_at,
        )
    except ValueError as exc:
        raise AdminRunActionError("stored admin run action is invalid") from exc


def _availability(
    run: review_runs.ReviewRun,
    job: jobs.ReviewJob | None,
    *,
    now: datetime,
    stale_after_minutes: int,
) -> tuple[ActionAvailability, ActionAvailability, ActionAvailability]:
    if run.status is not ReviewStatus.RUNNING:
        unavailable = ActionAvailability(False, "The review run is terminal.")
        return unavailable, unavailable, unavailable
    if job is None:
        unavailable = ActionAvailability(False, "This run has no review job.")
        return unavailable, unavailable, unavailable

    release = ActionAvailability(
        job.status is jobs.ReviewJobStatus.QUEUED and job.available_at > now,
        (
            "Release this delayed queued retry now."
            if job.status is jobs.ReviewJobStatus.QUEUED and job.available_at > now
            else "Only a delayed queued retry can be released."
        ),
    )
    cancellable = job.status in {
        jobs.ReviewJobStatus.QUEUED,
        jobs.ReviewJobStatus.LEASED,
    }
    cancel_reason = (
        "Cancel this running review and terminalize its job."
        if cancellable
        else (
            "Publication has started and cannot be cancelled safely."
            if job.status is jobs.ReviewJobStatus.AWAITING_PUBLICATION
            else "The review job is terminal."
        )
    )
    cancel = ActionAvailability(cancellable, cancel_reason)
    cutoff = now - timedelta(minutes=stale_after_minutes)
    lease_live = (
        job.status is jobs.ReviewJobStatus.LEASED
        and job.lease_expires_at is not None
        and job.lease_expires_at > now
    )
    stalled = (
        run.last_heartbeat_at < cutoff
        and job.status in {jobs.ReviewJobStatus.QUEUED, jobs.ReviewJobStatus.LEASED}
        and not lease_live
    )
    mark_stalled = ActionAvailability(
        stalled,
        (
            f"Mark this review failed after {stale_after_minutes} minutes without a heartbeat."
            if stalled
            else "The review heartbeat or worker lease is still current."
        ),
    )
    return release, cancel, mark_stalled


def controls(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    now: datetime | None = None,
    stale_after_minutes: int = 15,
) -> RunControls:
    """Return one run's current job controls and last 50 audit events."""
    _require_transaction(connection)
    moment = _moment(now)
    if not 1 <= stale_after_minutes <= 1440:
        raise AdminRunActionError("stale_after_minutes must be 1 to 1440")
    run = review_runs.get_run(connection, run_id)
    job = connection.execute(
        "SELECT id FROM review_agent.review_jobs WHERE review_run_id = %s",
        (run.id,),
    ).fetchone()
    current_job = jobs.get_job(connection, int(job[0])) if job is not None else None
    release, cancel, mark_stalled = _availability(
        run,
        current_job,
        now=moment,
        stale_after_minutes=stale_after_minutes,
    )
    with connection.cursor(row_factory=class_row(_AuditRow)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, action, actor, reason, previous_run_status,
                   previous_job_status, expected_lease_generation,
                   expected_available_at, stale_after_minutes, recorded_at
            FROM review_agent.admin_run_actions
            WHERE review_run_id = %s
            ORDER BY id DESC
            LIMIT 50
            """,
            (run.id,),
        ).fetchall()
    return RunControls(
        run=RunSnapshot(
            id=run.id,
            status=run.status,
            phase=run.phase,
            last_heartbeat_at=run.last_heartbeat_at,
        ),
        job=_job_snapshot(current_job) if current_job is not None else None,
        release_retry=release,
        cancel=cancel,
        mark_stalled=mark_stalled,
        audit=tuple(_audit(row) for row in rows),
    )


def apply_action(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    request: ActionRequest,
    now: datetime | None = None,
) -> None:
    """Apply one exact fenced action and record its audit in the same transaction."""
    _require_transaction(connection)
    moment = _moment(now)
    actor = _audit_text(request.actor, field="actor", maximum=200)
    reason = _audit_text(request.reason, field="reason", maximum=500)
    if request.expected_job_id < 1 or request.expected_lease_generation < 0:
        raise AdminRunActionError("expected job identity is invalid")
    if (
        request.expected_available_at.tzinfo is None
        or request.expected_available_at.utcoffset() is None
    ):
        raise AdminRunActionError("expected_available_at must include a timezone")
    stale_minutes = request.stale_after_minutes
    if request.action is RunAction.MARK_STALLED:
        stale_minutes = 15 if stale_minutes is None else stale_minutes
        if not 1 <= stale_minutes <= 1440:
            raise AdminRunActionError("stale_after_minutes must be 1 to 1440")
    elif stale_minutes is not None:
        raise AdminRunActionError("stale_after_minutes is only valid for mark_stalled")

    run = review_runs.lock_run(connection, run_id)
    locked_job = connection.execute(
        """
        SELECT id FROM review_agent.review_jobs
        WHERE review_run_id = %s AND id = %s
        FOR UPDATE
        """,
        (run.id, request.expected_job_id),
    ).fetchone()
    if locked_job is None:
        raise AdminRunActionStale("The review job changed. Refresh and try again.")
    job = jobs.get_job(connection, request.expected_job_id)
    if (
        job.review_run_id != run.id
        or job.lease_generation != request.expected_lease_generation
        or job.status is not request.expected_status
        or job.available_at != request.expected_available_at
    ):
        raise AdminRunActionStale("The review job changed. Refresh and try again.")
    if run.status is not ReviewStatus.RUNNING:
        raise AdminRunActionStale("The review run is already terminal.")

    if request.action is RunAction.RELEASE_RETRY:
        if job.status is not jobs.ReviewJobStatus.QUEUED or job.available_at <= moment:
            raise AdminRunActionError("Only a delayed queued retry can be released.")
        jobs.retry_queued_job(connection, job_id=job.id)
    elif request.action is RunAction.CANCEL:
        if job.status not in {
            jobs.ReviewJobStatus.QUEUED,
            jobs.ReviewJobStatus.LEASED,
        }:
            raise AdminRunActionError("This review job cannot be cancelled safely.")
        review_run_application.fail_run_in_transaction(
            connection,
            run.id,
            failure_code=failure_codes.OPERATOR_CANCELLED,
        )
    else:
        assert stale_minutes is not None
        cutoff = moment - timedelta(minutes=stale_minutes)
        lease_live = (
            job.status is jobs.ReviewJobStatus.LEASED
            and job.lease_expires_at is not None
            and job.lease_expires_at > moment
        )
        if (
            run.last_heartbeat_at >= cutoff
            or job.status
            not in {jobs.ReviewJobStatus.QUEUED, jobs.ReviewJobStatus.LEASED}
            or lease_live
        ):
            raise AdminRunActionError(
                "The review heartbeat or worker lease is still current."
            )
        review_run_application.fail_run_in_transaction(
            connection,
            run.id,
            failure_code=failure_codes.STALE_TIMEOUT,
        )

    connection.execute(
        """
        INSERT INTO review_agent.admin_run_actions (
            review_run_id, job_id, action, actor, reason,
            previous_run_status, previous_job_status,
            expected_lease_generation, expected_available_at,
            stale_after_minutes
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            run.id,
            job.id,
            request.action.value,
            actor,
            reason,
            run.status.value,
            job.status.value,
            job.lease_generation,
            job.available_at,
            stale_minutes,
        ),
    )
