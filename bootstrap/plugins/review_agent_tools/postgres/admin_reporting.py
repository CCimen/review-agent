"""Repository activity and paginated review history for the operator panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import TupleRow, class_row

from ..domain.review import ReviewRunId
from . import coverage as postgres_coverage


HistoryStatus = Literal[
    "all", "active", "published", "failed", "latest_failed", "superseded"
]
RunState = Literal[
    "queued", "running", "publishing", "published", "failed", "superseded"
]


@dataclass(frozen=True, slots=True)
class RepositoryActivity:
    repository: str
    prs_reviewed: int
    published_requests: int
    failed_requests: int
    active_requests: int
    latest_failed_prs: int
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class RepositoryTotals:
    prs_reviewed: int
    published_requests: int
    failed_requests: int
    active_requests: int
    latest_failed_prs: int


@dataclass(frozen=True, slots=True)
class RepositoryPage:
    items: tuple[RepositoryActivity, ...]
    has_more: bool
    generated_at: datetime
    window_days: int
    window_start: datetime
    window_end: datetime
    total: int
    totals: RepositoryTotals


@dataclass(frozen=True, slots=True)
class HistoryRow:
    id: ReviewRunId
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    state: RunState
    phase: str
    findings_count: int | None
    failure_code: str | None
    job_failure_code: str | None
    attempt_count: int
    max_attempts: int | None
    started_at: datetime
    last_heartbeat_at: datetime
    completed_at: datetime | None
    posted_at: datetime | None
    publication_superseded: bool
    is_latest: bool
    recovered: bool


@dataclass(frozen=True, slots=True)
class ReviewUsage:
    started_attempts: int
    reported_attempts: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class HistoryItem(HistoryRow):
    usage: ReviewUsage
    coverage: postgres_coverage.CoverageSummary


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: tuple[HistoryItem, ...]
    next_cursor: int | None
    generated_at: datetime
    window_days: int
    window_start: datetime
    window_end: datetime
    total: int


def repositories(
    connection: psycopg.Connection[TupleRow],
    *,
    since: datetime,
    until: datetime,
    now: datetime,
    days: int,
    search: str,
    offset: int,
    limit: int,
) -> RepositoryPage:
    query = """
            WITH activity AS (
                SELECT pr.repository_id,
                    count(DISTINCT pr.id) FILTER (
                        WHERE pub.posted_at IS NOT NULL AND (run.started_at >= %(since)s AND run.started_at < %(until)s)
                    )::integer AS prs_reviewed,
                    count(*) FILTER (
                        WHERE pub.posted_at IS NOT NULL AND (run.started_at >= %(since)s AND run.started_at < %(until)s)
                    )::integer AS published_requests,
                    count(*) FILTER (
                        WHERE run.status = 'failed' AND (run.started_at >= %(since)s AND run.started_at < %(until)s)
                    )::integer AS failed_requests,
                    count(*) FILTER (WHERE run.status = 'running')::integer AS active_requests,
                    count(*) FILTER (
                        WHERE run.status = 'failed' AND NOT EXISTS (
                            SELECT 1 FROM review_agent.review_runs AS newer
                            WHERE newer.pull_request_id = run.pull_request_id AND newer.id > run.id
                        )
                    )::integer AS latest_failed_prs,
                    max(coalesce(run.completed_at, run.last_heartbeat_at)) AS last_activity_at
                FROM review_agent.review_runs AS run
                JOIN review_agent.pull_requests AS pr ON pr.id = run.pull_request_id
                LEFT JOIN review_agent.publications AS pub ON pub.review_run_id = run.id
                WHERE (run.started_at >= %(since)s AND run.started_at < %(until)s) OR run.status = 'running'
                GROUP BY pr.repository_id
            )
            SELECT repo.full_name AS repository,
                coalesce(a.prs_reviewed, 0) AS prs_reviewed,
                coalesce(a.published_requests, 0) AS published_requests,
                coalesce(a.failed_requests, 0) AS failed_requests,
                coalesce(a.active_requests, 0) AS active_requests,
                coalesce(a.latest_failed_prs, 0) AS latest_failed_prs,
                a.last_activity_at
            FROM review_agent.repositories AS repo
            LEFT JOIN activity AS a ON a.repository_id = repo.id
            WHERE position(lower(%(search)s) IN lower(repo.full_name)) > 0
                """
    parameters = {
        "since": since,
        "until": until,
        "search": search,
        "limit": limit + 1,
        "offset": offset,
    }
    totals = connection.execute(
        "SELECT count(*), coalesce(sum(prs_reviewed), 0)::bigint, "
        "coalesce(sum(published_requests), 0)::bigint, coalesce(sum(failed_requests), 0)::bigint, "
        "coalesce(sum(active_requests), 0)::bigint, coalesce(sum(latest_failed_prs), 0)::bigint "
        "FROM (" + query + ") AS matching",
        parameters,
    ).fetchone()
    assert totals is not None
    with connection.cursor(row_factory=class_row(RepositoryActivity)) as cursor:
        rows = cursor.execute(
            query
            + " ORDER BY lower(repo.full_name), repo.id LIMIT %(limit)s OFFSET %(offset)s",
            parameters,
        ).fetchall()
    return RepositoryPage(
        tuple(rows[:limit]),
        len(rows) > limit,
        now,
        days,
        since,
        until,
        totals[0],
        RepositoryTotals(totals[1], totals[2], totals[3], totals[4], totals[5]),
    )


def history(
    connection: psycopg.Connection[TupleRow],
    *,
    since: datetime,
    until: datetime,
    now: datetime,
    days: int,
    repository: str | None,
    status: HistoryStatus,
    pr_number: int | None,
    limit: int,
    before_id: int | None,
) -> HistoryPage:
    query = """
            SELECT run.id, repo.full_name AS repository, pr.number AS pr_number,
                subject.base_sha, subject.head_sha,
                CASE WHEN run.status = 'failed' THEN 'failed'
                     WHEN run.status = 'superseded' THEN 'superseded'
                     WHEN pub.posted_at IS NOT NULL THEN 'published'
                     WHEN pub.status IN ('generated', 'posting') THEN 'publishing'
                     WHEN job.status = 'queued' THEN 'queued' ELSE 'running' END AS state,
                run.phase, run.findings_count, run.failure_code,
                job.failure_code AS job_failure_code,
                coalesce(job.attempt_count, 0) AS attempt_count, job.max_attempts,
                run.started_at, run.last_heartbeat_at, run.completed_at, pub.posted_at,
                (pub.superseded_at IS NOT NULL) AS publication_superseded,
                NOT EXISTS (
                    SELECT 1 FROM review_agent.review_runs AS newer
                    WHERE newer.pull_request_id = pr.id AND newer.id > run.id
                ) AS is_latest,
                (run.status = 'failed' AND EXISTS (
                    SELECT 1 FROM review_agent.publications AS newer
                    WHERE newer.pull_request_id = pr.id AND newer.review_run_id > run.id
                      AND newer.posted_at IS NOT NULL
                )) AS recovered
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pr ON pr.id = run.pull_request_id
            JOIN review_agent.repositories AS repo ON repo.id = pr.repository_id
            JOIN review_agent.review_subjects AS subject ON subject.id = run.review_subject_id
            LEFT JOIN review_agent.review_jobs AS job ON job.review_run_id = run.id
            LEFT JOIN review_agent.publications AS pub ON pub.review_run_id = run.id
            WHERE (%(repository)s::text IS NULL OR lower(repo.full_name) = lower(%(repository)s))
              AND (%(pr)s::integer IS NULL OR pr.number = %(pr)s)
              AND (CASE WHEN %(status)s = 'active' THEN run.status = 'running'
                        ELSE run.started_at >= %(since)s AND run.started_at < %(until)s END)
              AND (%(status)s IN ('all', 'active')
                   OR (%(status)s = 'published' AND pub.posted_at IS NOT NULL)
                   OR (%(status)s = 'failed' AND run.status = 'failed')
                   OR (%(status)s = 'latest_failed' AND run.status = 'failed' AND NOT EXISTS (
                       SELECT 1 FROM review_agent.review_runs AS newer
                       WHERE newer.pull_request_id = pr.id AND newer.id > run.id
                   ))
                   OR (%(status)s = 'superseded' AND run.status = 'superseded'))
                """
    parameters = {
        "repository": repository,
        "pr": pr_number,
        "before": before_id,
        "status": status,
        "since": since,
        "until": until,
        "limit": limit + 1,
    }
    total = connection.execute(
        "SELECT count(*) FROM (" + query + ") AS matching", parameters
    ).fetchone()
    assert total is not None
    with connection.cursor(row_factory=class_row(HistoryRow)) as cursor:
        rows = cursor.execute(
            query
            + " AND (%(before)s::bigint IS NULL OR run.id < %(before)s) ORDER BY run.id DESC LIMIT %(limit)s",
            parameters,
        ).fetchall()
    selected = rows[:limit]
    usage_rows = connection.execute(
        """
        SELECT job.review_run_id, job.lease_generation, count(u.job_id),
            sum(u.prompt_tokens)::bigint, sum(u.completion_tokens)::bigint, sum(u.total_tokens)::bigint
        FROM review_agent.review_jobs job
        LEFT JOIN review_agent.review_attempt_usage u ON u.job_id = job.id
        WHERE job.review_run_id = ANY(%s)
        GROUP BY job.id
    """,
        ([row.id for row in selected],),
    ).fetchall()
    usage = {
        row[0]: ReviewUsage(row[1], row[2], row[3], row[4], row[5])
        for row in usage_rows
    }
    summaries = postgres_coverage.summarize_many(
        connection, tuple(row.id for row in selected)
    )
    items = tuple(
        HistoryItem(
            id=row.id,
            repository=row.repository,
            pr_number=row.pr_number,
            base_sha=row.base_sha,
            head_sha=row.head_sha,
            state=row.state,
            phase=row.phase,
            findings_count=row.findings_count,
            failure_code=row.failure_code,
            job_failure_code=row.job_failure_code,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            started_at=row.started_at,
            last_heartbeat_at=row.last_heartbeat_at,
            completed_at=row.completed_at,
            posted_at=row.posted_at,
            publication_superseded=row.publication_superseded,
            is_latest=row.is_latest,
            recovered=row.recovered,
            coverage=summaries[row.id],
            usage=usage.get(row.id, ReviewUsage(0, 0, None, None, None)),
        )
        for row in selected
    )
    return HistoryPage(
        items,
        int(items[-1].id) if len(rows) > limit else None,
        now,
        days,
        since,
        until,
        total[0],
    )
