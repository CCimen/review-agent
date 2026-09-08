"""Typed operational reports over durable queues and optional worker telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow, class_row

from .team_access import AccessScope, repository_source, run_source

WorkerKind = Literal["review", "publisher", "webhook"]


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
    scope: AccessScope | None,
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
    scope: AccessScope | None = None,
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
            count(*) FILTER (WHERE kind = 'review' AND state = 'running' AND last_seen_at > %(now)s - interval '90 seconds'),
            coalesce(sum(capacity) FILTER (WHERE kind = 'review' AND state = 'running' AND last_seen_at > %(now)s - interval '90 seconds'), 0)::bigint
        FROM review_agent.worker_instances
    """).format(runs=run_source(scope), repositories=repository_source(scope)),
        {"now": now},
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
    oldest_waiting_at: datetime | None
    next_available_at: datetime | None


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
                SELECT 'webhook', lease_owner FROM review_agent.github_webhook_deliveries WHERE status = 'processing' AND lease_expires_at > %(now)s
            ), busy AS (SELECT kind, lease_owner, count(*) AS count FROM leases GROUP BY kind, lease_owner)
            SELECT w.id, w.kind, w.lease_owner, w.capacity, w.started_at, w.last_seen_at,
                CASE WHEN state <> 'stopped' AND last_seen_at <= %(now)s - interval '90 seconds' THEN 'unresponsive' ELSE state END AS state,
                CASE WHEN w.state = 'stopped' THEN 0
                     WHEN count(*) FILTER (WHERE w.state <> 'stopped') OVER (PARTITION BY w.kind, w.lease_owner) > 1 THEN NULL
                     ELSE coalesce(b.count, 0) END AS active_leases
            FROM review_agent.worker_instances w
            LEFT JOIN busy b ON b.kind = w.kind AND b.lease_owner = w.lease_owner
            ORDER BY (w.state <> 'stopped') DESC, w.last_seen_at DESC, w.id LIMIT 101
        """,
            {"now": now},
        ).fetchall()
    with connection.cursor(row_factory=class_row(QueueStatus)) as cursor:
        queues = cursor.execute(
            """
            WITH work AS (
                SELECT 'review' AS kind, job.status = 'queued' AS waiting, job.status = 'leased' AS leased,
                    job.status IN ('failed', 'dead_letter') AS failed, job.created_at,
                    GREATEST(job.available_at, account.quota_wait_until) AS available_at, job.lease_expires_at
                FROM review_agent.review_jobs job
                JOIN review_agent.review_runs run ON run.id = job.review_run_id
                JOIN review_agent.review_subjects subject ON subject.id = run.review_subject_id
                LEFT JOIN review_agent.model_accounts account
                  ON account.connection_id = subject.model_connection_id
                 AND account.provider = subject.model_provider AND account.revision = subject.model_account_revision
                UNION ALL
                SELECT 'publisher', status IN ('generated', 'publish_failed'), status = 'posting', status = 'failed', generated_at, delivery_available_at, delivery_lease_expires_at FROM review_agent.publications WHERE posted_at IS NULL AND superseded_at IS NULL
                UNION ALL
                SELECT 'webhook', status = 'received', status = 'processing', status = 'failed', received_at, available_at, lease_expires_at FROM review_agent.github_webhook_deliveries
            )
            SELECT kinds.kind,
                count(*) FILTER (WHERE w.waiting AND NOT w.failed) AS waiting,
                count(*) FILTER (WHERE w.waiting AND NOT w.failed AND available_at <= %(now)s) AS due,
                count(*) FILTER (WHERE w.waiting AND NOT w.failed AND available_at > %(now)s) AS delayed,
                count(*) FILTER (WHERE w.leased) AS leased,
                count(*) FILTER (WHERE w.leased AND lease_expires_at <= %(now)s) AS expired_leases,
                count(*) FILTER (WHERE w.failed) AS failed,
                min(created_at) FILTER (WHERE w.waiting AND NOT w.failed) AS oldest_waiting_at,
                min(available_at) FILTER (WHERE w.waiting AND NOT w.failed AND available_at > %(now)s) AS next_available_at
            FROM (VALUES ('review'), ('publisher'), ('webhook')) kinds(kind)
            LEFT JOIN work w ON w.kind = kinds.kind GROUP BY kinds.kind ORDER BY kinds.kind
        """,
            {"now": now},
        ).fetchall()
    return Operations(now, 90, tuple(workers[:100]), len(workers) > 100, tuple(queues))


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
