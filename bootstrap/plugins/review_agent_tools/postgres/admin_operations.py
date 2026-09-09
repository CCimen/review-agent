"""Typed operational reports over durable queues and optional worker telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow, class_row

from .team_access import ReadScope, repository_source, run_source

WorkerKind = Literal["review", "publisher", "webhook"]
WORKER_STALE_SECONDS = 90


@dataclass(frozen=True, slots=True)
class ActivityCounts:
    requests: int
    published_reviews: int
    reviewed_prs: int
    failed_requests: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reported_attempts: int
    median_publication_seconds: float | None
    p95_publication_seconds: float | None


@dataclass(frozen=True, slots=True)
class ActivityDay:
    date: datetime
    published_reviews: int


@dataclass(frozen=True, slots=True)
class FailureCount:
    failure_code: str
    requests: int


@dataclass(frozen=True, slots=True)
class Overview:
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    retained_since: datetime | None
    repository_count: int
    active_requests: int
    started_attempts: int
    reported_attempts: int
    live_review_workers: int | None
    review_capacity: int | None
    lifetime: ActivityCounts
    window: ActivityCounts
    daily_publications: tuple[ActivityDay, ...]
    recent_failure_reasons: tuple[FailureCount, ...]


def _counts(
    connection: psycopg.Connection[TupleRow],
    start: datetime | None,
    end: datetime,
    scope: ReadScope | None,
) -> ActivityCounts:
    with connection.cursor(row_factory=class_row(ActivityCounts)) as cursor:
        result = cursor.execute(
            sql.SQL("""
            WITH review_counts AS (
            SELECT
                count(*) FILTER (WHERE %(start)s::timestamptz IS NULL OR r.started_at >= %(start)s) AS requests,
                count(*) FILTER (WHERE p.posted_at < %(end)s AND (%(start)s::timestamptz IS NULL OR p.posted_at >= %(start)s)) AS published_reviews,
                count(DISTINCT r.pull_request_id) FILTER (WHERE p.posted_at < %(end)s AND (%(start)s::timestamptz IS NULL OR p.posted_at >= %(start)s)) AS reviewed_prs,
                count(*) FILTER (WHERE r.status = 'failed' AND r.completed_at < %(end)s AND (%(start)s::timestamptz IS NULL OR r.completed_at >= %(start)s)) AS failed_requests,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM p.posted_at - r.started_at)) FILTER (WHERE p.posted_at < %(end)s AND (%(start)s::timestamptz IS NULL OR p.posted_at >= %(start)s)) AS median_publication_seconds,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM p.posted_at - r.started_at)) FILTER (WHERE p.posted_at < %(end)s AND (%(start)s::timestamptz IS NULL OR p.posted_at >= %(start)s)) AS p95_publication_seconds
            FROM {runs} r
            LEFT JOIN review_agent.publications p ON p.review_run_id = r.id
            WHERE r.started_at < %(end)s
            ), usage AS (
                SELECT sum(u.prompt_tokens)::bigint AS prompt_tokens,
                    sum(u.completion_tokens)::bigint AS completion_tokens,
                    sum(u.total_tokens)::bigint AS total_tokens,
                    count(*) AS reported_attempts
                FROM review_agent.review_attempt_usage u
                JOIN review_agent.review_jobs j ON j.id = u.job_id
                JOIN {runs} r ON r.id = j.review_run_id
                WHERE u.recorded_at < %(end)s
                  AND (%(start)s::timestamptz IS NULL OR u.recorded_at >= %(start)s)
            )
            SELECT review_counts.*, usage.* FROM review_counts CROSS JOIN usage
        """).format(runs=run_source(scope), repositories=repository_source(scope)),
            {"start": start, "end": end},
        ).fetchone()
    assert result is not None
    return result


def overview(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: ReadScope | None = None,
    start: datetime,
    end: datetime,
    now: datetime,
) -> Overview:
    totals = connection.execute(
        sql.SQL("""
        SELECT (SELECT min(started_at) FROM {runs} r),
            (SELECT count(*) FROM {repositories} AS repositories),
            (SELECT count(*) FROM {runs} r WHERE status = 'running'),
            (SELECT coalesce(sum(j.lease_generation), 0)::bigint FROM review_agent.review_jobs j JOIN {runs} r ON r.id = j.review_run_id),
            (SELECT count(*) FROM review_agent.review_attempt_usage u JOIN review_agent.review_jobs j ON j.id = u.job_id JOIN {runs} r ON r.id = j.review_run_id),
            count(*) FILTER (WHERE kind = 'review' AND state = 'running' AND last_seen_at > %(now)s - %(stale_seconds)s * interval '1 second'),
            coalesce(sum(capacity) FILTER (WHERE kind = 'review' AND state = 'running' AND last_seen_at > %(now)s - %(stale_seconds)s * interval '1 second'), 0)::bigint
        FROM review_agent.worker_instances
    """).format(runs=run_source(scope), repositories=repository_source(scope)),
        {"now": now, "stale_seconds": WORKER_STALE_SECONDS},
    ).fetchone()
    assert totals is not None
    with connection.cursor(row_factory=class_row(ActivityDay)) as cursor:
        days = cursor.execute(
            sql.SQL("""
            SELECT date_trunc('day', posted_at, 'UTC') AS date, count(*) AS published_reviews
            FROM review_agent.publications p JOIN {runs} r ON r.id = p.review_run_id WHERE posted_at >= %s AND posted_at < %s
            GROUP BY 1 ORDER BY 1
        """).format(runs=run_source(scope), repositories=repository_source(scope)),
            (start, end),
        ).fetchall()
    with connection.cursor(row_factory=class_row(FailureCount)) as cursor:
        failures = cursor.execute(
            sql.SQL("""
            SELECT coalesce(failure_code, 'unknown') AS failure_code, count(*) AS requests
            FROM {runs} r
            WHERE status = 'failed' AND completed_at >= %s AND completed_at < %s
            GROUP BY 1 ORDER BY requests DESC, failure_code LIMIT 10
        """).format(runs=run_source(scope), repositories=repository_source(scope)),
            (start, end),
        ).fetchall()
    return Overview(
        now,
        start,
        end,
        totals[0],
        totals[1],
        totals[2],
        totals[3],
        totals[4],
        totals[5]
        if scope is None or (scope.global_read and scope.team_id is None)
        else None,
        totals[6]
        if scope is None or (scope.global_read and scope.team_id is None)
        else None,
        _counts(connection, None, now, scope),
        _counts(connection, start, end, scope),
        tuple(days),
        tuple(failures),
    )


@dataclass(frozen=True, slots=True)
class WorkerInstance:
    id: UUID
    kind: WorkerKind
    lease_owner: str
    capacity: int
    started_at: datetime
    last_seen_at: datetime
    state: Literal["running", "draining", "stopped", "unresponsive"]
    active_leases: int | None


@dataclass(frozen=True, slots=True)
class QueueStatus:
    kind: WorkerKind
    waiting: int
    due: int
    delayed: int
    leased: int
    expired_leases: int
    failed: int
    awaiting_publication: int
    dead_letters: int
    expired_exhausted: int
    live_workers: int
    worker_capacity: int
    cooldown_waiting: int
    capacity_waiting: int
    oldest_waiting_at: datetime | None
    oldest_due_at: datetime | None
    next_available_at: datetime | None
    cooldown_until: datetime | None
    last_progress_at: datetime | None
    last_heartbeat_at: datetime | None
    last_terminal_reason: str | None
    last_terminal_at: datetime | None


@dataclass(frozen=True, slots=True)
class Operations:
    generated_at: datetime
    stale_after_seconds: int
    workers: tuple[WorkerInstance, ...]
    workers_truncated: bool
    queues: tuple[QueueStatus, ...]


def operations(
    connection: psycopg.Connection[TupleRow], *, now: datetime
) -> Operations:
    with connection.cursor(row_factory=class_row(WorkerInstance)) as cursor:
        workers = cursor.execute(
            """
            WITH leases AS (
                SELECT 'review' AS kind, lease_owner FROM review_agent.review_jobs WHERE status = 'leased' AND lease_expires_at > %(now)s
                UNION ALL
                SELECT 'publisher', delivery_lease_owner FROM review_agent.publications WHERE status = 'posting' AND delivery_lease_expires_at > %(now)s
                UNION ALL
                SELECT 'publisher', failure_status_delivery_lease_owner FROM review_agent.review_runs WHERE failure_status_delivery_status = 'posting' AND failure_status_delivery_lease_expires_at > %(now)s
                UNION ALL
                SELECT 'webhook', lease_owner FROM review_agent.github_webhook_deliveries WHERE status = 'processing' AND lease_expires_at > %(now)s
            ), busy AS (SELECT kind, lease_owner, count(*) AS count FROM leases GROUP BY kind, lease_owner)
            SELECT w.id, w.kind, w.lease_owner, w.capacity, w.started_at, w.last_seen_at,
                CASE WHEN state <> 'stopped' AND last_seen_at <= %(now)s - %(stale_seconds)s * interval '1 second' THEN 'unresponsive' ELSE state END AS state,
                CASE WHEN w.state = 'stopped' THEN 0
                     WHEN count(*) FILTER (WHERE w.state <> 'stopped') OVER (PARTITION BY w.kind, w.lease_owner) > 1 THEN NULL
                     ELSE coalesce(b.count, 0) END AS active_leases
            FROM review_agent.worker_instances w
            LEFT JOIN busy b ON b.kind = w.kind AND b.lease_owner = w.lease_owner
            ORDER BY (w.state <> 'stopped') DESC, w.last_seen_at DESC, w.id LIMIT 101
        """,
            {"now": now, "stale_seconds": WORKER_STALE_SECONDS},
        ).fetchall()
    return Operations(
        now, WORKER_STALE_SECONDS, tuple(workers[:100]), len(workers) > 100,
        queue_status(connection, now=now),
    )


def queue_status(
    connection: psycopg.Connection[TupleRow], *, now: datetime
) -> tuple[QueueStatus, ...]:
    """Share queue timing and worker evidence across console and operator reports."""
    with connection.cursor(row_factory=class_row(QueueStatus)) as cursor:
        rows = cursor.execute(
            """
            WITH work AS (
                SELECT 'review' AS kind, 'job' AS source, job.id,
                    job.status = 'queued' AS waiting, job.status = 'leased' AS leased,
                    job.status IN ('failed', 'dead_letter') AS failed,
                    job.status = 'dead_letter' AS dead_letter,
                    job.status = 'awaiting_publication' AS awaiting_publication,
                    job.attempt_count >= job.max_attempts AS exhausted,
                    job.created_at, GREATEST(job.available_at, account.quota_wait_until) AS available_at,
                    job.lease_expires_at, job.started_at, job.last_heartbeat_at,
                    job.completed_at, job.failure_code, account.quota_wait_until AS cooldown_until
                FROM review_agent.review_jobs job
                JOIN review_agent.review_runs run ON run.id = job.review_run_id
                JOIN review_agent.review_subjects subject ON subject.id = run.review_subject_id
                LEFT JOIN review_agent.model_accounts account
                  ON account.connection_id = subject.model_connection_id
                 AND account.provider = subject.model_provider AND account.revision = subject.model_account_revision
                UNION ALL
                SELECT 'publisher', 'publication', id,
                    status IN ('generated', 'publish_failed'), status = 'posting',
                    status = 'failed', false, false,
                    delivery_attempt_count >= delivery_max_attempts,
                    generated_at, delivery_available_at, delivery_lease_expires_at,
                    posting_started_at, delivery_last_heartbeat_at,
                    delivery_completed_at, failure_code, NULL::timestamptz
                FROM review_agent.publications
                UNION ALL
                SELECT 'publisher', 'failure_status', id,
                    failure_status_delivery_status IN ('pending', 'publish_failed'),
                    failure_status_delivery_status = 'posting', failure_status_delivery_status = 'failed',
                    false, false,
                    failure_status_delivery_attempt_count >= failure_status_delivery_max_attempts,
                    completed_at, failure_status_delivery_available_at,
                    failure_status_delivery_lease_expires_at, NULL::timestamptz,
                    failure_status_delivery_last_heartbeat_at,
                    failure_status_delivery_completed_at, failure_status_delivery_failure_code,
                    NULL::timestamptz
                FROM review_agent.review_runs WHERE failure_status_delivery_status <> 'not_required'
                UNION ALL
                SELECT 'webhook', 'delivery', id, status = 'received', status = 'processing',
                    status = 'failed', false, false, attempt_count >= max_attempts,
                    received_at, available_at, lease_expires_at, started_at,
                    last_heartbeat_at, processed_at, failure_code, NULL::timestamptz
                FROM review_agent.github_webhook_deliveries
            ), workers AS (
                SELECT kind, count(*) AS live_workers, sum(capacity)::bigint AS capacity
                FROM review_agent.worker_instances
                WHERE state = 'running'
                  AND last_seen_at > %(now)s - %(stale_seconds)s * interval '1 second'
                GROUP BY kind
            ), terminal AS (
                SELECT DISTINCT ON (kind) kind, failure_code, completed_at
                FROM work WHERE completed_at IS NOT NULL AND failure_code IS NOT NULL
                ORDER BY kind, completed_at DESC, source, id DESC
            )
            SELECT kinds.kind,
                count(*) FILTER (WHERE w.waiting AND NOT w.failed) AS waiting,
                count(*) FILTER (WHERE w.waiting AND NOT w.failed AND available_at <= %(now)s) AS due,
                count(*) FILTER (WHERE w.waiting AND NOT w.failed AND available_at > %(now)s) AS delayed,
                count(*) FILTER (WHERE w.leased) AS leased,
                count(*) FILTER (WHERE w.leased AND lease_expires_at <= %(now)s) AS expired_leases,
                count(*) FILTER (WHERE w.failed) AS failed,
                count(*) FILTER (WHERE w.awaiting_publication) AS awaiting_publication,
                count(*) FILTER (WHERE w.dead_letter) AS dead_letters,
                count(*) FILTER (WHERE w.leased AND lease_expires_at <= %(now)s AND w.exhausted) AS expired_exhausted,
                coalesce(workers.live_workers, 0) AS live_workers,
                coalesce(workers.capacity, 0) AS worker_capacity,
                count(*) FILTER (WHERE w.waiting AND w.cooldown_until > %(now)s) AS cooldown_waiting,
                count(*) FILTER (WHERE w.waiting AND w.failure_code = 'review_waiting_for_capacity') AS capacity_waiting,
                min(created_at) FILTER (WHERE w.waiting AND NOT w.failed) AS oldest_waiting_at,
                min(available_at) FILTER (WHERE w.waiting AND NOT w.failed AND available_at <= %(now)s) AS oldest_due_at,
                min(available_at) FILTER (WHERE w.waiting AND NOT w.failed AND available_at > %(now)s) AS next_available_at,
                min(w.cooldown_until) FILTER (WHERE w.waiting AND w.cooldown_until > %(now)s) AS cooldown_until,
                max(GREATEST(w.started_at, w.completed_at)) AS last_progress_at,
                max(w.last_heartbeat_at) FILTER (WHERE w.leased) AS last_heartbeat_at,
                terminal.failure_code AS last_terminal_reason,
                terminal.completed_at AS last_terminal_at
            FROM (VALUES ('review'), ('publisher'), ('webhook')) kinds(kind)
            LEFT JOIN work w ON w.kind = kinds.kind
            LEFT JOIN workers ON workers.kind = kinds.kind
            LEFT JOIN terminal ON terminal.kind = kinds.kind
            GROUP BY kinds.kind, workers.live_workers, workers.capacity,
                terminal.failure_code, terminal.completed_at
            ORDER BY kinds.kind
        """,
            {"now": now, "stale_seconds": WORKER_STALE_SECONDS},
        ).fetchall()
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    id: int
    worker_id: UUID
    occurred_at: datetime
    event: Literal[
        "started",
        "draining",
        "stopped",
        "review_started",
        "review_returned",
        "usage_unavailable",
    ]
    review_run_id: int | None
    job_id: int | None


@dataclass(frozen=True, slots=True)
class WorkerEventPage:
    items: tuple[WorkerEvent, ...]
    next_cursor: int | None


def events(
    connection: psycopg.Connection[TupleRow],
    *,
    worker_id: UUID | None,
    before_id: int | None,
    limit: int,
) -> WorkerEventPage:
    with connection.cursor(row_factory=class_row(WorkerEvent)) as cursor:
        rows = cursor.execute(
            """
            SELECT id, worker_id, occurred_at, event, review_run_id, job_id FROM review_agent.worker_events
            WHERE (%(worker)s::uuid IS NULL OR worker_id = %(worker)s)
              AND (%(before)s::bigint IS NULL OR id < %(before)s)
            ORDER BY id DESC LIMIT %(limit)s
        """,
            {"worker": worker_id, "before": before_id, "limit": limit + 1},
        ).fetchall()
    return WorkerEventPage(
        tuple(rows[:limit]), rows[limit - 1].id if len(rows) > limit else None
    )
