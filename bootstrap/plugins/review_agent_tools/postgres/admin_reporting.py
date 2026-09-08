"""Repository activity and paginated review history for the operator panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow, class_row

from ..domain.review import ReviewRunId
from . import coverage as postgres_coverage
from .team_access import ReadScope, repository_source


HistoryStatus = Literal[
    "all", "active", "published", "failed", "latest_failed", "superseded"
]
RunState = Literal[
    "queued", "running", "publishing", "published", "failed", "superseded"
]


@dataclass(frozen=True, slots=True)
class RepositoryActivity:
    repository_id: int
    repository: str
    team_id: int | None
    team_name: str | None
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
    next_after_id: int | None = None
    watermark_id: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryRow:
    id: ReviewRunId
    pull_request_id: int
    previous_head_sha: str | None
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
    quota_wait_until: datetime | None


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
    watermark_id: int | None = None


def repositories(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: ReadScope | None = None,
    since: datetime,
    until: datetime,
    now: datetime,
    days: int,
    search: str,
    offset: int,
    limit: int,
    after_id: int | None = None,
    watermark_id: int | None = None,
) -> RepositoryPage:
    query = sql.SQL("""
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
                JOIN {repositories} scoped_repo ON scoped_repo.id = pr.repository_id
                LEFT JOIN review_agent.publications AS pub ON pub.review_run_id = run.id
                WHERE (run.started_at >= %(since)s AND run.started_at < %(until)s) OR run.status = 'running'
                GROUP BY pr.repository_id
            )
            SELECT repo.id AS repository_id, repo.full_name AS repository,
                ownership.team_id, team.name AS team_name,
                coalesce(a.prs_reviewed, 0) AS prs_reviewed,
                coalesce(a.published_requests, 0) AS published_requests,
                coalesce(a.failed_requests, 0) AS failed_requests,
                coalesce(a.active_requests, 0) AS active_requests,
                coalesce(a.latest_failed_prs, 0) AS latest_failed_prs,
                a.last_activity_at
            FROM {repositories} AS repo
            LEFT JOIN activity AS a ON a.repository_id = repo.id
            LEFT JOIN review_agent.team_repositories ownership ON ownership.repository_id = repo.id
            LEFT JOIN review_agent.teams team ON team.id = ownership.team_id
            WHERE position(lower(%(search)s) IN lower(repo.full_name)) > 0
                AND (%(watermark)s::bigint IS NULL OR repo.id <= %(watermark)s)
                """).format(repositories=repository_source(scope))
    parameters = {
        "since": since,
        "until": until,
        "search": search,
        "limit": limit + 1,
        "offset": offset,
        "after": after_id,
        "watermark": watermark_id,
    }
    totals = connection.execute(
        sql.SQL(
            "SELECT count(*), coalesce(sum(prs_reviewed), 0)::bigint, "
            "coalesce(sum(published_requests), 0)::bigint, coalesce(sum(failed_requests), 0)::bigint, "
            "coalesce(sum(active_requests), 0)::bigint, coalesce(sum(latest_failed_prs), 0)::bigint, max(repository_id) "
            "FROM ({}) AS matching"
        ).format(query),
        parameters,
    ).fetchone()
    assert totals is not None
    watermark = watermark_id if watermark_id is not None else totals[6] or 0
    if after_id is not None:
        parameters["watermark"] = watermark
    with connection.cursor(row_factory=class_row(RepositoryActivity)) as cursor:
        rows = cursor.execute(
            query
            + sql.SQL(
                " AND repo.id > %(after)s ORDER BY repo.id LIMIT %(limit)s"
                if after_id is not None
                else " ORDER BY lower(repo.full_name), repo.id LIMIT %(limit)s OFFSET %(offset)s"
            ),
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
        rows[limit - 1].repository_id
        if after_id is not None and len(rows) > limit
        else None,
        watermark if after_id is not None else None,
    )


_HISTORY_SELECT = """
            SELECT run.id, pr.id AS pull_request_id,
                (SELECT prior_subject.head_sha FROM review_agent.review_runs prior
                 JOIN review_agent.review_subjects prior_subject ON prior_subject.id = prior.review_subject_id
                 WHERE prior.pull_request_id = pr.id AND prior.id < run.id
                 ORDER BY prior.id DESC LIMIT 1) AS previous_head_sha,
                repo.full_name AS repository, pr.number AS pr_number,
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
                )) AS recovered,
                CASE WHEN job.status = 'queued' AND run.status = 'running'
                     THEN account.quota_wait_until END AS quota_wait_until
            FROM review_agent.review_runs AS run
            JOIN review_agent.pull_requests AS pr ON pr.id = run.pull_request_id
            JOIN {repositories} AS repo ON repo.id = pr.repository_id
            JOIN review_agent.review_subjects AS subject ON subject.id = run.review_subject_id
            LEFT JOIN review_agent.review_jobs AS job ON job.review_run_id = run.id
            LEFT JOIN review_agent.model_accounts AS account
              ON account.connection_id = subject.model_connection_id
             AND account.provider = subject.model_provider AND account.revision = subject.model_account_revision
            LEFT JOIN review_agent.publications AS pub ON pub.review_run_id = run.id
"""

_HISTORY_QUERY = (
    _HISTORY_SELECT
    + """
            WHERE (%(repository)s::text IS NULL OR lower(repo.full_name) = lower(%(repository)s))
              AND (%(pr)s::integer IS NULL OR pr.number = %(pr)s)
              AND (%(watermark)s::bigint IS NULL OR run.id <= %(watermark)s)
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
)


def history(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: ReadScope | None = None,
    since: datetime,
    until: datetime,
    now: datetime,
    days: int,
    repository: str | None,
    status: HistoryStatus,
    pr_number: int | None,
    limit: int,
    before_id: int | None,
    watermark_id: int | None = None,
) -> HistoryPage:
    query = sql.SQL(_HISTORY_QUERY).format(repositories=repository_source(scope))
    parameters = {
        "repository": repository,
        "pr": pr_number,
        "before": before_id,
        "status": status,
        "since": since,
        "until": until,
        "limit": limit + 1,
        "watermark": watermark_id,
    }
    total = connection.execute(
        sql.SQL("SELECT count(*), max(id) FROM ({}) AS matching").format(query),
        parameters,
    ).fetchone()
    assert total is not None
    watermark = watermark_id if watermark_id is not None else total[1] or 0
    parameters["watermark"] = watermark
    with connection.cursor(row_factory=class_row(HistoryRow)) as cursor:
        rows = cursor.execute(
            query
            + sql.SQL(
                " AND (%(before)s::bigint IS NULL OR run.id < %(before)s) ORDER BY run.id DESC LIMIT %(limit)s"
            ),
            parameters,
        ).fetchall()
    items = _history_items(connection, rows[:limit])
    return HistoryPage(
        items,
        int(items[-1].id) if len(rows) > limit else None,
        now,
        days,
        since,
        until,
        total[0],
        watermark,
    )


def _history_items(
    connection: psycopg.Connection[TupleRow], selected: list[HistoryRow]
) -> tuple[HistoryItem, ...]:
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
    return tuple(
        HistoryItem(
            id=row.id,
            pull_request_id=row.pull_request_id,
            previous_head_sha=row.previous_head_sha,
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
            quota_wait_until=row.quota_wait_until,
            coverage=summaries[row.id],
            usage=usage.get(row.id, ReviewUsage(0, 0, None, None, None)),
        )
        for row in selected
    )


@dataclass(frozen=True, slots=True)
class PublicationLink:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class ReviewDetail:
    item: HistoryItem
    markdown: str | None
    content_truncated: bool
    publication_links: tuple[PublicationLink, ...]
    links_truncated: bool
    requests: tuple[HistoryItem, ...]
    next_cursor: int | None
    generated_at: datetime
    can_maintain: bool = False


def review_detail(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: ReadScope | None = None,
    run_id: ReviewRunId,
    before_id: int | None,
    now: datetime,
) -> ReviewDetail | None:
    with connection.cursor(row_factory=class_row(HistoryRow)) as cursor:
        selected = cursor.execute(
            sql.SQL(_HISTORY_SELECT + " WHERE run.id = %s").format(
                repositories=repository_source(scope)
            ),
            (run_id,),
        ).fetchone()
        if selected is None:
            return None
        rows = cursor.execute(
            sql.SQL(
                _HISTORY_SELECT
                + " WHERE run.pull_request_id = %s AND (%s::bigint IS NULL OR run.id < %s)"
                " ORDER BY run.id DESC LIMIT 21"
            ).format(repositories=repository_source(scope)),
            (selected.pull_request_id, before_id, before_id),
        ).fetchall()
    items = _history_items(connection, [selected, *rows[:20]])
    # Read only the frozen, fully published result, never a generated draft or
    # model response. Bound retained content independently of publisher policy.
    publication = connection.execute(
        """SELECT id, left(rendered_markdown, 200000), length(rendered_markdown) > 200000
           FROM review_agent.publications
           WHERE review_run_id = %s AND pull_request_id = %s
             AND status = 'posted' AND posted_at IS NOT NULL""",
        (run_id, selected.pull_request_id),
    ).fetchone()
    links: list[PublicationLink] = []
    link_rows: list[TupleRow] = []
    if publication is not None:
        link_rows = connection.execute(
            """SELECT part_type, part_number, external_id
               FROM review_agent.publication_parts
               WHERE publication_id = %s AND status = 'posted' AND external_id IS NOT NULL
               ORDER BY CASE WHEN part_type = 'summary' THEN 0
                             WHEN part_type = 'continuation' THEN 1 ELSE 2 END, part_number
               LIMIT 101""",
            (publication[0],),
        ).fetchall()
        pr_url = f"https://github.com/{selected.repository}/pull/{selected.pr_number}"
        for part_type, part_number, external_id in link_rows[:100]:
            suggestion = part_type == "suggestion_review"
            label = (
                "Open suggested changes on GitHub"
                if suggestion
                else "Open published review on GitHub"
                if part_type == "summary"
                else f"Open review part {part_number} on GitHub"
            )
            anchor = "pullrequestreview" if suggestion else "issuecomment"
            links.append(PublicationLink(label, f"{pr_url}#{anchor}-{external_id}"))
    return ReviewDetail(
        item=items[0],
        markdown=publication[1] if publication else None,
        content_truncated=bool(publication and publication[2]),
        publication_links=tuple(links),
        links_truncated=len(link_rows) > 100,
        requests=items[1:],
        next_cursor=int(rows[19].id) if len(rows) > 20 else None,
        generated_at=now,
    )


@dataclass(frozen=True, slots=True)
class PullRequestGroup:
    pull_request_id: int
    matching_requests: int
    total_requests: int
    latest: HistoryItem


@dataclass(frozen=True, slots=True)
class PullRequestPage:
    items: tuple[PullRequestGroup, ...]
    total: int
    next_cursor: int | None
    generated_at: datetime
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True, slots=True)
class _GroupRow:
    pull_request_id: int
    matching_requests: int
    latest_id: ReviewRunId
    total_requests: int


def pull_requests(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: ReadScope | None = None,
    since: datetime,
    until: datetime,
    now: datetime,
    repository: str | None,
    status: HistoryStatus,
    pr_number: int | None,
    limit: int,
    before_id: int | None,
) -> PullRequestPage:
    parameters = {
        "watermark": None,
        "repository": repository,
        "status": status,
        "pr": pr_number,
        "since": since,
        "until": until,
        "before": before_id,
        "limit": limit + 1,
    }
    grouped = sql.SQL(
        "WITH matching AS ("
        + _HISTORY_QUERY
        + """), grouped AS (
        SELECT pull_request_id, count(*) AS matching_requests, max(id) AS latest_id
        FROM matching GROUP BY pull_request_id
    ) """
    ).format(repositories=repository_source(scope))
    total = connection.execute(
        grouped + sql.SQL("SELECT count(*) FROM grouped"), parameters
    ).fetchone()
    assert total is not None
    with connection.cursor(row_factory=class_row(_GroupRow)) as cursor:
        groups = cursor.execute(
            grouped
            + sql.SQL("""
            SELECT g.*, (SELECT count(*) FROM review_agent.review_runs r
                WHERE r.pull_request_id = g.pull_request_id) AS total_requests
            FROM grouped g
            WHERE (%(before)s::bigint IS NULL OR latest_id < %(before)s)
            ORDER BY latest_id DESC LIMIT %(limit)s
        """),
            parameters,
        ).fetchall()
    selected = groups[:limit]
    with connection.cursor(row_factory=class_row(HistoryRow)) as cursor:
        rows = cursor.execute(
            sql.SQL(_HISTORY_QUERY + " AND run.id = ANY(%(ids)s)").format(
                repositories=repository_source(scope)
            ),
            {**parameters, "ids": [group.latest_id for group in selected]},
        ).fetchall()
    details = {item.id: item for item in _history_items(connection, rows)}
    return PullRequestPage(
        tuple(
            PullRequestGroup(
                g.pull_request_id,
                g.matching_requests,
                g.total_requests,
                details[g.latest_id],
            )
            for g in selected
        ),
        total[0],
        int(selected[-1].latest_id) if len(groups) > limit else None,
        now,
        since,
        until,
    )
