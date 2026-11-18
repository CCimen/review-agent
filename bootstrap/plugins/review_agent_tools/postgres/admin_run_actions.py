"""PostgreSQL store for the job fences and audit trail of administrative run actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import TupleRow, class_row

from ..domain.review import ReviewRunId


@dataclass(frozen=True, slots=True)
class RecordedAction:
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


def controlled_job_id(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId, *, now: datetime
) -> int | None:
    """The job an operator acts on: a delayed retry if one waits, else the main job."""
    row = connection.execute(
        "SELECT id FROM review_agent.review_jobs WHERE review_run_id = %s "
        "ORDER BY (status = 'queued' AND available_at > %s) DESC, part LIMIT 1",
        (run_id, now),
    ).fetchone()
    return int(row[0]) if row is not None else None


def has_live_lease(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId, *, now: datetime
) -> bool:
    """Whether any job of the run, including a review part, holds a live lease."""
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM review_agent.review_jobs "
        "WHERE review_run_id = %s AND status = 'leased' AND lease_expires_at > %s)",
        (run_id, now),
    ).fetchone()
    return bool(row and row[0])


def lock_job(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId, job_id: int
) -> bool:
    """Lock one job of the run for an action; false when the run has no such job."""
    row = connection.execute(
        """
        SELECT id FROM review_agent.review_jobs
        WHERE review_run_id = %s AND id = %s
        FOR UPDATE
        """,
        (run_id, job_id),
    ).fetchone()
    return row is not None


def recent_actions(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> list[RecordedAction]:
    """The run's last 50 recorded actions, newest first."""
    with connection.cursor(row_factory=class_row(RecordedAction)) as cursor:
        return cursor.execute(
            """
            SELECT id, action, actor, reason, previous_run_status,
                   previous_job_status, expected_lease_generation,
                   expected_available_at, stale_after_minutes, recorded_at
            FROM review_agent.admin_run_actions
            WHERE review_run_id = %s
            ORDER BY id DESC
            LIMIT 50
            """,
            (run_id,),
        ).fetchall()


def record_action(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    job_id: int,
    action: str,
    actor: str,
    reason: str,
    previous_run_status: str,
    previous_job_status: str,
    expected_lease_generation: int,
    expected_available_at: datetime,
    stale_after_minutes: int | None,
) -> None:
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
            run_id,
            job_id,
            action,
            actor,
            reason,
            previous_run_status,
            previous_job_status,
            expected_lease_generation,
            expected_available_at,
            stale_after_minutes,
        ),
    )
