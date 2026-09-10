"""PostgreSQL durable review-job enqueue and claim operations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import re
from typing import TypeAlias, cast
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from .. import failure_codes
from ..model_quota import MANUAL_REFRESH_SECONDS, QuotaCheck
from ..review_contract import SHARED_CONNECTION_ID
from ..domain.review import PullRequestId, ReviewRunId, ReviewStatus


class ReviewJobError(ValueError):
    """A durable review-job operation violates its contract."""


class ReviewJobBusy(ReviewJobError):
    """The pull-request enqueue lock could not be acquired within its bound."""


class ReviewQueueFull(ReviewJobError):
    """The configured active-job capacity is already in use."""


class ReviewJobNotFound(ReviewJobError):
    """The requested review job does not exist."""


class ReviewJobLeaseLost(ReviewJobError):
    """The exact lease no longer owns a mutable job."""

    current_job: "ReviewJob"

    def __init__(self, current_job: "ReviewJob") -> None:
        super().__init__("review job lease is no longer current")
        self.current_job = current_job


_WORKER_SESSION_PREFIX = "review-agent-job-"
_WORKER_SESSION_RE = re.compile(r"^review-agent-job-([1-9][0-9]*)-lease-([1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class WorkerLeaseSession:
    """Trusted Hermes session identity for one durable job generation."""

    job_id: int
    lease_generation: int

    def encode(self) -> str:
        return f"{_WORKER_SESSION_PREFIX}{self.job_id}-lease-{self.lease_generation}"

    @classmethod
    def parse(cls, value: object) -> "WorkerLeaseSession | None":
        session_id = str(value or "").strip()
        if not session_id:
            return None
        matched = _WORKER_SESSION_RE.fullmatch(session_id)
        if matched is not None:
            return cls(job_id=int(matched[1]), lease_generation=int(matched[2]))
        if session_id.startswith(_WORKER_SESSION_PREFIX):
            raise ReviewJobError("worker session identity is malformed")
        return None


class ReviewJobStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    AWAITING_PUBLICATION = "awaiting_publication"
    SUPERSEDED = "superseded"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


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
    failure_code: str | None
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
    failure_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _ReviewJobReportRow(_ReviewJobRow):
    repository: str
    pr_number: int
    run_status: str


@dataclass(frozen=True, slots=True)
class _ReviewRunScopeRow:
    id: ReviewRunId
    pull_request_id: PullRequestId
    status: str


@dataclass(frozen=True, slots=True)
class _RecoveredJobRow:
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
    failure_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    run_status: str


@dataclass(frozen=True, slots=True)
class EnqueuedJob:
    job: ReviewJob


@dataclass(frozen=True, slots=True)
class DuplicateJob:
    job: ReviewJob


JobEnqueue: TypeAlias = EnqueuedJob | DuplicateJob


@dataclass(frozen=True, slots=True)
class JobFailureResult:
    job: ReviewJob
    run_failure_code: str | None


@dataclass(frozen=True, slots=True)
class RecoveryBatch:
    jobs: tuple[ReviewJob, ...]
    run_ids_to_fail: tuple[ReviewRunId, ...]


@dataclass(frozen=True, slots=True)
class ReviewJobReport:
    job: ReviewJob
    repository: str
    pr_number: int
    run_status: ReviewStatus


@dataclass(frozen=True, slots=True)
class ReviewQueueHealth:
    active: int
    queued: int
    leased: int
    expired_leases: int
    dead_letters: int


_JOB_COLUMNS = """
    id, review_run_id, status, priority, available_at, attempt_count,
    max_attempts, lease_owner, lease_generation, lease_expires_at,
    last_heartbeat_at, failure_code, created_at, started_at, completed_at
"""
_QUALIFIED_JOB_COLUMNS = """
    job.id, job.review_run_id, job.status, job.priority, job.available_at,
    job.attempt_count, job.max_attempts, job.lease_owner,
    job.lease_generation, job.lease_expires_at, job.last_heartbeat_at,
    job.failure_code, job.created_at, job.started_at, job.completed_at
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
        failure_code=row.failure_code,
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


def _failure_code(value: str) -> str:
    code = value.strip()
    if code not in failure_codes.JOB_ALL:
        raise ReviewJobError("failure_code is not a canonical job failure code")
    return code


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


def get_job(connection: psycopg.Connection[TupleRow], job_id: int) -> ReviewJob:
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


def list_jobs(
    connection: psycopg.Connection[TupleRow],
    *,
    statuses: Sequence[ReviewJobStatus],
    limit: int,
) -> tuple[ReviewJobReport, ...]:
    """List a bounded queue snapshot with its repository scope."""
    _require_transaction(connection)
    row_limit = _integer(limit, field="limit", minimum=1)
    resolved_statuses = tuple(dict.fromkeys(status.value for status in statuses))
    if not resolved_statuses:
        raise ReviewJobError("at least one job status is required")
    with connection.cursor(row_factory=class_row(_ReviewJobReportRow)) as cursor:
        rows = cursor.execute(
            f"""
            SELECT {_QUALIFIED_JOB_COLUMNS},
                   repository.full_name AS repository,
                   pull_request.number AS pr_number,
                   run.status AS run_status
            FROM review_agent.review_jobs AS job
            JOIN review_agent.review_runs AS run ON run.id = job.review_run_id
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = run.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE job.status = ANY(%s)
            ORDER BY job.created_at, job.id
            LIMIT %s
            """,
            (list(resolved_statuses), row_limit),
        ).fetchall()
    reports: list[ReviewJobReport] = []
    for row in rows:
        reports.append(
            ReviewJobReport(
                job=_job(row),
                repository=row.repository,
                pr_number=row.pr_number,
                run_status=ReviewStatus(row.run_status),
            )
        )
    return tuple(reports)


def retry_queued_job(
    connection: psycopg.Connection[TupleRow], *, job_id: int
) -> ReviewJob:
    """Make one delayed queued retry available now without reviving terminal work."""
    _require_transaction(connection)
    resolved_job_id = _integer(job_id, field="job_id", minimum=1)
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_jobs AS job
            SET available_at = statement_timestamp(), failure_code = NULL
            FROM review_agent.review_runs AS run
            WHERE job.id = %s
              AND run.id = job.review_run_id
              AND job.status = 'queued'
              AND run.status = 'running'
            RETURNING {_QUALIFIED_JOB_COLUMNS}
            """,
            (resolved_job_id,),
        ).fetchone()
    if row is None:
        current = get_job(connection, resolved_job_id)
        raise ReviewJobError(
            f"review job {current.id} is not a queued retry for an active run"
        )
    return _job(row)


def require_live_lease(
    connection: psycopg.Connection[TupleRow],
    *,
    job_id: int,
    review_run_id: ReviewRunId,
    lease_generation: int,
) -> ReviewJob:
    """Validate the worker generation carried by one Hermes tool session."""
    _require_transaction(connection)
    resolved_job_id = _integer(job_id, field="job_id", minimum=1)
    resolved_run_id = ReviewRunId(
        _integer(review_run_id, field="review_run_id", minimum=1)
    )
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"""
            SELECT {_JOB_COLUMNS}
            FROM review_agent.review_jobs
            WHERE id = %s
              AND review_run_id = %s
              AND status = 'leased'
              AND lease_generation = %s
              AND lease_expires_at > statement_timestamp()
            """,
            (resolved_job_id, resolved_run_id, generation),
        ).fetchone()
    if row is None:
        raise ReviewJobLeaseLost(get_job(connection, resolved_job_id))
    return _job(row)


def enqueue_run(
    connection: psycopg.Connection[TupleRow],
    *,
    review_run_id: ReviewRunId,
    priority: int,
    max_attempts: int,
    active_job_limit: int,
) -> JobEnqueue:
    """Create one queue record for an active run."""
    _require_transaction(connection)
    resolved_run_id = ReviewRunId(
        _integer(review_run_id, field="review_run_id", minimum=1)
    )
    resolved_priority = _integer(priority, field="priority")
    resolved_max_attempts = _integer(max_attempts, field="max_attempts", minimum=1)
    resolved_active_limit = _integer(
        active_job_limit, field="active_job_limit", minimum=1
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
            "SELECT id FROM review_agent.pull_requests WHERE id = %s FOR NO KEY UPDATE",
            (run.pull_request_id,),
        ).fetchone()
    except errors.LockNotAvailable as exc:
        raise ReviewJobBusy("pull request is busy enqueuing review work") from exc
    if pull_request is None:
        raise ReviewJobError("review run pull request does not exist")

    try:
        with connection.cursor(row_factory=class_row(_ReviewRunScopeRow)) as cursor:
            locked_run = cursor.execute(
                "SELECT id, pull_request_id, status FROM review_agent.review_runs "
                "WHERE id = %s FOR UPDATE",
                (resolved_run_id,),
            ).fetchone()
    except errors.LockNotAvailable as exc:
        raise ReviewJobBusy("review run is busy while enqueuing") from exc
    if locked_run is None:
        raise ReviewJobError("review run disappeared while enqueuing")
    existing = _by_run(connection, resolved_run_id)
    if existing is not None:
        return DuplicateJob(existing)
    if locked_run.status != ReviewStatus.RUNNING:
        raise ReviewJobError("only an active review run can be enqueued")

    # Serialize only admission count/insert operations. Worker updates do not
    # contend on this transaction-scoped namespace lock.
    connection.execute(
        "SELECT pg_advisory_xact_lock("
        "hashtextextended('review-agent:queue-capacity', 0))"
    )
    active_count = connection.execute(
        "SELECT count(*) FROM review_agent.review_jobs "
        "WHERE status IN ('queued', 'leased', 'awaiting_publication')"
    ).fetchone()
    if active_count is None or not isinstance(active_count[0], int):
        raise ReviewJobError("active review-job count could not be read")
    if active_count[0] >= resolved_active_limit:
        raise ReviewQueueFull("review queue is at its configured capacity")

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
    priority_aging_interval: timedelta,
    runtime_key: str = "shared",
) -> ReviewJob | None:
    """Claim one ready job with a short, fenced ``SKIP LOCKED`` update."""
    _require_transaction(connection)
    owner = lease_owner.strip()
    if not owner:
        raise ReviewJobError("lease_owner is required")
    if lease_duration <= timedelta(0):
        raise ReviewJobError("lease_duration must be positive")
    if priority_aging_interval <= timedelta(0):
        raise ReviewJobError("priority_aging_interval must be positive")

    # Claims across runtimes share team limits. Hold this only through the short
    # claim transaction; provider calls and lease heartbeats never acquire it.
    # The runtime's lock timeout bounds contention. Read occupancy in the next
    # statement so a waiting claimer sees the previous transaction's reservation.
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('review-agent:dispatch-capacity', 0))"
    )
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            """
            WITH occupied_jobs AS MATERIALIZED (
                SELECT id FROM review_agent.review_jobs WHERE status = 'leased'
                UNION
                SELECT job_id FROM review_agent.model_executions WHERE finished_at IS NULL
            ),
            occupancy AS MATERIALIZED (
                SELECT pull_request.repository_id, subject.model_connection_id, subject.admission_team_id
                FROM occupied_jobs occupied
                JOIN review_agent.review_jobs job ON job.id = occupied.id
                JOIN review_agent.review_runs run ON run.id = job.review_run_id
                JOIN review_agent.pull_requests pull_request ON pull_request.id = run.pull_request_id
                JOIN review_agent.review_subjects subject ON subject.id = run.review_subject_id
            ),
            connection_usage AS MATERIALIZED (
                SELECT model_connection_id, count(*) AS active FROM occupancy GROUP BY model_connection_id
            ),
            team_usage AS MATERIALIZED (
                SELECT admission_team_id, count(*) AS active FROM occupancy
                WHERE admission_team_id IS NOT NULL GROUP BY admission_team_id
            ),
            candidate AS MATERIALIZED (
                SELECT job.id, subject.admission_team_id
                FROM review_agent.review_jobs AS job
                JOIN review_agent.review_runs AS run
                  ON run.id = job.review_run_id
                JOIN review_agent.pull_requests AS pull_request
                  ON pull_request.id = run.pull_request_id
                JOIN review_agent.repositories AS repository
                  ON repository.id = pull_request.repository_id
                JOIN review_agent.review_subjects AS subject
                  ON subject.id = run.review_subject_id
                JOIN review_agent.model_connections AS model_connection
                  ON model_connection.id = subject.model_connection_id
                LEFT JOIN review_agent.model_accounts AS model_account
                  ON model_account.connection_id = model_connection.id
                 AND model_account.provider = subject.model_provider
                LEFT JOIN connection_usage ON connection_usage.model_connection_id = model_connection.id
                LEFT JOIN team_usage ON team_usage.admission_team_id = subject.admission_team_id
                LEFT JOIN review_agent.team_model_policies policy ON policy.team_id = subject.admission_team_id
                LEFT JOIN review_agent.review_dispatch_turns turn ON turn.team_id = subject.admission_team_id
                WHERE job.status = 'queued'
                  AND job.available_at <= statement_timestamp()
                  AND job.attempt_count < job.max_attempts
                  AND run.status = 'running'
                  AND NOT EXISTS (SELECT 1 FROM occupancy WHERE occupancy.repository_id = repository.id)
                  AND model_connection.runtime_key = %s
                  AND model_connection.state = 'enabled'
                  AND (model_account.quota_wait_until IS NULL
                       OR model_account.quota_wait_until <= statement_timestamp())
                  AND COALESCE(connection_usage.active, 0) < model_connection.max_concurrency
                  AND (subject.admission_team_id IS NULL
                       OR COALESCE(team_usage.active, 0) < COALESCE(policy.max_concurrency, 4))
                  -- Retained requests without a model contract must still reach
                  -- the worker's terminal validation instead of being stranded.
                  AND (subject.model_provider IS NULL
                       OR model_account.revision = subject.model_account_revision)
                ORDER BY
                    -- A single scalar lookup keeps normal team joins hashable.
                    CASE WHEN subject.admission_team_id IS NULL
                        THEN (SELECT last_claimed_at FROM review_agent.review_dispatch_turns WHERE team_id IS NULL)
                        ELSE turn.last_claimed_at END NULLS FIRST,
                    subject.admission_team_id NULLS FIRST,
                    job.available_at - (%s * job.priority),
                    job.available_at,
                    job.id
                FOR UPDATE OF repository SKIP LOCKED
                FOR SHARE OF model_connection SKIP LOCKED
                LIMIT 1
            ),
            claimed AS (
            UPDATE review_agent.review_jobs AS job
            SET status = 'leased',
                attempt_count = job.attempt_count + 1,
                lease_owner = %s,
                lease_generation = job.lease_generation + 1,
                lease_expires_at = statement_timestamp() + %s,
                last_heartbeat_at = statement_timestamp(),
                failure_code = NULL,
                started_at = COALESCE(job.started_at, statement_timestamp())
            FROM candidate
            WHERE job.id = candidate.id AND job.status = 'queued'
            RETURNING job.*
            ),
            advance_turn AS (
                INSERT INTO review_agent.review_dispatch_turns (team_id, last_claimed_at)
                SELECT candidate.admission_team_id, statement_timestamp()
                FROM candidate JOIN claimed ON claimed.id = candidate.id
                ON CONFLICT (team_id) DO UPDATE SET last_claimed_at = EXCLUDED.last_claimed_at
            )
            SELECT * FROM claimed
            """,
            (runtime_key, priority_aging_interval, owner, lease_duration),
        ).fetchone()
    return _job(row) if row is not None else None


def begin_model_execution(
    connection: psycopg.Connection[TupleRow],
    *,
    session: WorkerLeaseSession,
    runtime_key: str,
    runtime_instance: UUID,
    provider: str,
    model: str,
    reasoning_effort: str,
    identity_sha256: str,
    quota: QuotaCheck | None,
) -> dict[str, object] | None:
    """Fence native Hermes activation against reconnect and stale worker leases.

    Called by the Hermes plugin before entering the upstream chat handler. The
    connection row serializes activation with account lifecycle changes; a
    worker lease expiring never removes the resulting execution reservation.
    """
    _require_transaction(connection)
    if re.fullmatch(r"[0-9a-f]{64}", identity_sha256) is None:
        raise ReviewJobError("Provider account identity is unavailable")
    managed = connection.execute(
        "SELECT id, state FROM review_agent.model_connections WHERE runtime_key = %s FOR UPDATE",
        (runtime_key,),
    ).fetchone()
    if managed is None or managed[1] not in {"enabled", "disabled"}:
        raise ReviewJobError("Model connection is not enabled")
    row = connection.execute(
        """SELECT job.review_run_id, subject.model_account_revision,
                  subject.resolved_config, account.revision, account.identity_sha256, job.lease_owner,
                  account.quota_observed_at, account.quota_wait_until
           FROM review_agent.review_jobs job
           JOIN review_agent.review_runs run ON run.id = job.review_run_id
           JOIN review_agent.review_subjects subject ON subject.id = run.review_subject_id
           JOIN review_agent.model_accounts account
             ON account.connection_id = subject.model_connection_id AND account.provider = subject.model_provider
           WHERE job.id = %s AND job.lease_generation = %s AND job.status = 'leased'
             AND job.lease_expires_at > statement_timestamp() AND run.status = 'running'
             AND subject.model_connection_id = %s AND subject.model_provider = %s
             AND subject.resolved_config #>> '{review_contract,model}' = %s
             AND subject.resolved_config #>> '{review_contract,reasoning_effort}' = %s""",
        (
            session.job_id,
            session.lease_generation,
            managed[0],
            provider,
            model,
            reasoning_effort,
        ),
    ).fetchone()
    if row is None:
        raise ReviewJobError(
            "Worker lease or admitted model assignment is no longer current"
        )
    if row[1] != row[3] or (row[4] is not None and row[4] != identity_sha256):
        raise ReviewJobError("Provider account changed after this review was admitted")
    if (
        connection.execute(
            "SELECT 1 FROM review_agent.model_executions WHERE job_id = %s AND (finished_at IS NULL OR lease_generation = %s) LIMIT 1",
            (session.job_id, session.lease_generation),
        ).fetchone()
        is not None
    ):
        raise ReviewJobError("The previous Hermes execution has not finished")
    if managed[1] == "disabled":
        # A pause can win after claim but before native activation. Preserve the
        # exact assignment without spending an inference attempt in that race.
        requeue_unstarted_job(
            connection,
            job_id=session.job_id,
            lease_owner=row[5],
            lease_generation=session.lease_generation,
        )
        return None
    if row[4] is None:
        if managed[0] != SHARED_CONNECTION_ID or row[3] != 1:
            raise ReviewJobError(
                "Provider account must be verified before enabling the connection"
            )
        # The migration's first shared account represents the already configured
        # deployment. Bind it once; subsequent changes require explicit reconnect.
        connection.execute(
            "UPDATE review_agent.model_accounts SET identity_sha256 = %s, observed_at = statement_timestamp() WHERE connection_id = %s AND provider = %s",
            (identity_sha256, managed[0], provider),
        )
    waiting = row[7]
    if quota is not None and (row[6] is None or quota.observed_at >= row[6]):
        waiting = None if quota.allowed else quota.retry_at
        connection.execute(
            """UPDATE review_agent.model_accounts SET quota_observed_at = %s, quota_wait_until = %s
               WHERE connection_id = %s AND provider = %s""",
            (quota.observed_at, waiting, managed[0], provider),
        )
    if waiting is not None:
        # A passed estimate permits another quota check, never presumed recovery.
        # Keep one account deadline instead of rewriting all of its queued jobs.
        connection.execute(
            """UPDATE review_agent.model_accounts
               SET quota_wait_until = GREATEST(quota_wait_until, statement_timestamp() + %s)
               WHERE connection_id = %s AND provider = %s""",
            (timedelta(seconds=MANUAL_REFRESH_SECONDS), managed[0], provider),
        )
        requeue_unstarted_job(
            connection,
            job_id=session.job_id,
            lease_owner=row[5],
            lease_generation=session.lease_generation,
        )
        return None
    inserted = connection.execute(
        """INSERT INTO review_agent.model_executions
               (job_id, lease_generation, connection_id, provider, account_revision, runtime_instance)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (job_id, lease_generation) DO NOTHING RETURNING job_id""",
        (
            session.job_id,
            session.lease_generation,
            managed[0],
            provider,
            row[3],
            runtime_instance,
        ),
    ).fetchone()
    if inserted is None:
        raise ReviewJobError("This worker execution was already dispatched")
    return cast(dict[str, object], row[2])


def finish_model_execution(
    connection: psycopg.Connection[TupleRow],
    *,
    session: WorkerLeaseSession,
    runtime_instance: UUID,
) -> None:
    """Release only after the native Hermes handler has finished its response."""
    connection.execute(
        """UPDATE review_agent.model_executions SET finished_at = statement_timestamp()
           WHERE job_id = %s AND lease_generation = %s AND runtime_instance = %s
             AND finished_at IS NULL""",
        (session.job_id, session.lease_generation, runtime_instance),
    )


def active_model_executions(
    connection: psycopg.Connection[TupleRow], *, connection_id: int
) -> int:
    row = connection.execute(
        "SELECT count(*) FROM review_agent.model_executions WHERE connection_id = %s AND finished_at IS NULL",
        (connection_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def heartbeat_job(
    connection: psycopg.Connection[TupleRow],
    *,
    job_id: int,
    lease_owner: str,
    lease_generation: int,
    lease_duration: timedelta,
) -> ReviewJob:
    """Extend one live exact lease or report the current terminal state."""
    _require_transaction(connection)
    resolved_job_id = _integer(job_id, field="job_id", minimum=1)
    owner = lease_owner.strip()
    if not owner:
        raise ReviewJobError("lease_owner is required")
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    if lease_duration <= timedelta(0):
        raise ReviewJobError("lease_duration must be positive")
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_jobs
            SET last_heartbeat_at = statement_timestamp(),
                lease_expires_at = statement_timestamp() + %s
            WHERE id = %s
              AND status = 'leased'
              AND lease_owner = %s
              AND lease_generation = %s
              AND lease_expires_at > statement_timestamp()
            RETURNING {_JOB_COLUMNS}
            """,
            (lease_duration, resolved_job_id, owner, generation),
        ).fetchone()
    if row is None:
        raise ReviewJobLeaseLost(get_job(connection, resolved_job_id))
    return _job(row)


def requeue_unstarted_job(
    connection: psycopg.Connection[TupleRow],
    *,
    job_id: int,
    lease_owner: str,
    lease_generation: int,
) -> ReviewJob:
    """Return one exact pre-activation lease without consuming an attempt."""
    _require_transaction(connection)
    resolved_job_id = _integer(job_id, field="job_id", minimum=1)
    owner = lease_owner.strip()
    if not owner:
        raise ReviewJobError("lease_owner is required")
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_jobs AS job
            SET status = 'queued',
                available_at = statement_timestamp(),
                attempt_count = job.attempt_count - 1,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = NULL,
                started_at = CASE
                    WHEN job.attempt_count = 1 THEN NULL
                    ELSE job.started_at
                END,
                completed_at = NULL
            WHERE job.id = %s
              AND job.status = 'leased'
              AND job.lease_owner = %s
              AND job.lease_generation = %s
              AND job.attempt_count > 0
              AND job.lease_expires_at > statement_timestamp()
            RETURNING {_JOB_COLUMNS}
            """,
            (resolved_job_id, owner, generation),
        ).fetchone()
    if row is None:
        raise ReviewJobLeaseLost(get_job(connection, resolved_job_id))
    return _job(row)


def fail_claimed_job(
    connection: psycopg.Connection[TupleRow],
    *,
    job_id: int,
    lease_owner: str,
    lease_generation: int,
    failure_code: str,
    retryable: bool,
    retry_delay: timedelta | None,
) -> JobFailureResult:
    """Apply one exact fenced failure without deciding the owning run outcome."""
    _require_transaction(connection)
    resolved_job_id = _integer(job_id, field="job_id", minimum=1)
    owner = lease_owner.strip()
    if not owner:
        raise ReviewJobError("lease_owner is required")
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    code = _failure_code(failure_code)
    if retryable:
        if retry_delay is None or retry_delay <= timedelta(0):
            raise ReviewJobError("retry_delay must be positive for retryable failure")
        delay = retry_delay
    else:
        if retry_delay is not None:
            raise ReviewJobError("retry_delay belongs only to retryable failure")
        delay = timedelta(0)

    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_jobs AS job
            SET status = CASE
                    WHEN %s AND job.attempt_count < job.max_attempts THEN 'queued'
                    WHEN %s THEN 'dead_letter'
                    ELSE 'failed'
                END,
                available_at = CASE
                    WHEN %s AND job.attempt_count < job.max_attempts
                    THEN statement_timestamp() + %s
                    ELSE job.available_at
                END,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = %s,
                completed_at = CASE
                    WHEN %s AND job.attempt_count < job.max_attempts THEN NULL
                    ELSE statement_timestamp()
                END
            WHERE job.id = %s
              AND job.status = 'leased'
              AND job.lease_owner = %s
              AND job.lease_generation = %s
              AND job.lease_expires_at > statement_timestamp()
            RETURNING {_JOB_COLUMNS}
            """,
            (
                retryable,
                retryable,
                retryable,
                delay,
                code,
                retryable,
                resolved_job_id,
                owner,
                generation,
            ),
        ).fetchone()
    if row is None:
        raise ReviewJobLeaseLost(get_job(connection, resolved_job_id))
    job = _job(row)
    run_failure_code = (
        failure_codes.JOB_RETRY_EXHAUSTED
        if job.status is ReviewJobStatus.DEAD_LETTER
        else (
            failure_codes.JOB_EXECUTION_FAILED
            if job.status is ReviewJobStatus.FAILED
            else None
        )
    )
    return JobFailureResult(job=job, run_failure_code=run_failure_code)


def handoff_to_publication(
    connection: psycopg.Connection[TupleRow],
    *,
    review_run_id: ReviewRunId,
    job_id: int | None,
    lease_generation: int | None,
) -> ReviewJob | None:
    """Release the exact review lease after its publication intent is durable."""
    _require_transaction(connection)
    if job_id is None and lease_generation is None:
        return None
    if job_id is None or lease_generation is None:
        raise ReviewJobError("job_id and lease_generation must be supplied together")
    resolved_job_id = _integer(job_id, field="job_id", minimum=1)
    generation = _integer(lease_generation, field="lease_generation", minimum=1)
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        row = cursor.execute(
            f"""
            UPDATE review_agent.review_jobs
            SET status = 'awaiting_publication',
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = NULL
            WHERE id = %s
              AND review_run_id = %s
              AND status = 'leased'
              AND lease_generation = %s
              AND lease_expires_at > statement_timestamp()
            RETURNING {_JOB_COLUMNS}
            """,
            (resolved_job_id, review_run_id, generation),
        ).fetchone()
    if row is None:
        raise ReviewJobLeaseLost(get_job(connection, resolved_job_id))
    publication = connection.execute(
        "SELECT id FROM review_agent.publications WHERE review_run_id = %s",
        (review_run_id,),
    ).fetchone()
    if publication is None:
        raise ReviewJobError("publication intent must exist before job handoff")
    return _job(row)


def reconcile_run_jobs(
    connection: psycopg.Connection[TupleRow],
    *,
    run_ids: Sequence[ReviewRunId],
    status: ReviewStatus,
) -> tuple[ReviewJob, ...]:
    """Terminalize non-terminal jobs after their owning runs become terminal."""
    _require_transaction(connection)
    if not run_ids:
        return ()
    if status is ReviewStatus.RUNNING:
        raise ReviewJobError("an active run cannot terminalize its job")
    job_status = {
        ReviewStatus.COMPLETED: ReviewJobStatus.SUCCEEDED,
        ReviewStatus.FAILED: ReviewJobStatus.FAILED,
        ReviewStatus.SUPERSEDED: ReviewJobStatus.SUPERSEDED,
    }[status]
    job_failure_code = (
        failure_codes.JOB_RUN_FAILED if status is ReviewStatus.FAILED else None
    )
    with connection.cursor(row_factory=class_row(_ReviewJobRow)) as cursor:
        rows = cursor.execute(
            f"""
            UPDATE review_agent.review_jobs
            SET status = %s,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = %s,
                completed_at = statement_timestamp()
            WHERE review_run_id = ANY(%s::bigint[])
              AND status IN ('queued', 'leased', 'awaiting_publication')
            RETURNING {_JOB_COLUMNS}
            """,
            (job_status.value, job_failure_code, [int(item) for item in run_ids]),
        ).fetchall()
    return tuple(sorted((_job(row) for row in rows), key=lambda item: item.id))


def recover_expired_leases(
    connection: psycopg.Connection[TupleRow],
    *,
    limit: int,
) -> RecoveryBatch:
    """Recover a bounded expiry batch after locking runs before job rows."""
    _require_transaction(connection)
    row_limit = _integer(limit, field="limit", minimum=1)
    recovered_columns = """
                job.id, job.review_run_id, job.status, job.priority,
                job.available_at, job.attempt_count, job.max_attempts,
                job.lease_owner, job.lease_generation, job.lease_expires_at,
                job.last_heartbeat_at, job.failure_code, job.created_at,
                job.started_at, job.completed_at, run.status AS run_status
    """
    with connection.cursor(row_factory=class_row(_RecoveredJobRow)) as cursor:
        rows = cursor.execute(
            f"""
            WITH candidate_runs AS MATERIALIZED (
                SELECT run.id
                FROM review_agent.review_runs AS run
                JOIN review_agent.review_jobs AS job
                  ON job.review_run_id = run.id
                WHERE job.status = 'leased'
                  AND job.lease_expires_at <= statement_timestamp()
                ORDER BY job.lease_expires_at, job.id
                FOR UPDATE OF run SKIP LOCKED
                LIMIT %s
            )
            UPDATE review_agent.review_jobs AS job
            SET status = CASE
                    WHEN run.status = 'completed' THEN 'succeeded'
                    WHEN run.status = 'failed' THEN 'failed'
                    WHEN run.status = 'superseded' THEN 'superseded'
                    WHEN job.attempt_count < job.max_attempts THEN 'queued'
                    ELSE 'dead_letter'
                END,
                available_at = CASE
                    WHEN run.status = 'running'
                     AND job.attempt_count < job.max_attempts
                    THEN statement_timestamp()
                    ELSE job.available_at
                END,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_heartbeat_at = NULL,
                failure_code = CASE
                    WHEN run.status = 'completed' THEN NULL
                    WHEN run.status = 'superseded' THEN NULL
                    WHEN run.status = 'failed' THEN %s
                    ELSE %s
                END,
                completed_at = CASE
                    WHEN run.status = 'running'
                     AND job.attempt_count < job.max_attempts
                    THEN NULL
                    ELSE statement_timestamp()
                END
            FROM candidate_runs AS candidate
            JOIN review_agent.review_runs AS run ON run.id = candidate.id
            WHERE job.review_run_id = candidate.id
              AND job.status = 'leased'
              AND job.lease_expires_at <= statement_timestamp()
            RETURNING {recovered_columns}
            """,
            (
                row_limit,
                failure_codes.JOB_RUN_FAILED,
                failure_codes.JOB_LEASE_EXPIRED,
            ),
        ).fetchall()
    jobs = tuple(
        sorted(
            (
                _job(
                    _ReviewJobRow(
                        id=row.id,
                        review_run_id=row.review_run_id,
                        status=row.status,
                        priority=row.priority,
                        available_at=row.available_at,
                        attempt_count=row.attempt_count,
                        max_attempts=row.max_attempts,
                        lease_owner=row.lease_owner,
                        lease_generation=row.lease_generation,
                        lease_expires_at=row.lease_expires_at,
                        last_heartbeat_at=row.last_heartbeat_at,
                        failure_code=row.failure_code,
                        created_at=row.created_at,
                        started_at=row.started_at,
                        completed_at=row.completed_at,
                    )
                )
                for row in rows
            ),
            key=lambda item: item.id,
        )
    )
    failures = tuple(
        sorted(
            row.review_run_id
            for row in rows
            if row.run_status == ReviewStatus.RUNNING
            and row.status == ReviewJobStatus.DEAD_LETTER
        )
    )
    return RecoveryBatch(jobs=jobs, run_ids_to_fail=failures)
