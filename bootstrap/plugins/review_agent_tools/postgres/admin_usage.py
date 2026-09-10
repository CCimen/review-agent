"""Administrative usage by request period and current repository ownership."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow, class_row

from .admin_reporting import ReviewUsage
from .team_access import AccessScope, repository_source


UsageDimension = Literal["team", "repository", "requester"]
UsageSort = Literal["requests", "total_tokens", "published_requests"]


@dataclass(frozen=True, slots=True)
class UsageMetrics(ReviewUsage):
    requests: int
    published_requests: int
    failed_requests: int
    repository_count: int
    requester_count: int
    unknown_requester_requests: int


@dataclass(frozen=True, slots=True)
class UsageTotals(UsageMetrics):
    groups: int


@dataclass(frozen=True, slots=True)
class UsageRow(UsageMetrics):
    key: str
    label: str
    team_id: int | None
    repository: str | None
    requester: str | None


@dataclass(frozen=True, slots=True)
class UsageReport:
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    dimension: UsageDimension
    sort: UsageSort
    totals: UsageTotals
    items: tuple[UsageRow, ...]
    has_more: bool


# Restrict the cohort before reading telemetry. Aggregate attempts once per job
# so retries cannot multiply requests, publications, or distinct repositories.
_REQUESTS = sql.SQL("""
    WITH cohort AS MATERIALIZED (
        SELECT run.id, run.status, repo.id AS repository_id,
            repo.full_name AS repository, ownership.team_id, team.name AS team_name,
            lower(nullif(btrim(run.trigger_user), '')) AS requester,
            (pub.posted_at IS NOT NULL) AS published,
            job.id AS job_id, coalesce(job.lease_generation, 0) AS started_attempts
        FROM review_agent.review_runs run
        JOIN review_agent.pull_requests pr ON pr.id = run.pull_request_id
        JOIN {repositories} repo ON repo.id = pr.repository_id
        LEFT JOIN review_agent.team_repositories ownership ON ownership.repository_id = repo.id
        LEFT JOIN review_agent.teams team ON team.id = ownership.team_id
        LEFT JOIN review_agent.publications pub ON pub.review_run_id = run.id
        LEFT JOIN review_agent.review_jobs job ON job.review_run_id = run.id
        WHERE run.started_at >= %(start)s AND run.started_at < %(end)s
          AND (%(repository)s::text IS NULL OR lower(repo.full_name) = lower(%(repository)s))
    ), attempts AS (
        SELECT usage.job_id, count(*) AS reported_attempts,
            sum(usage.prompt_tokens)::bigint AS prompt_tokens,
            sum(usage.completion_tokens)::bigint AS completion_tokens,
            sum(usage.total_tokens)::bigint AS total_tokens
        FROM review_agent.review_attempt_usage usage
        JOIN cohort ON cohort.job_id = usage.job_id
        GROUP BY usage.job_id
    ), requests AS (
        SELECT cohort.*, coalesce(attempts.reported_attempts, 0) AS reported_attempts,
            attempts.prompt_tokens, attempts.completion_tokens, attempts.total_tokens
        FROM cohort LEFT JOIN attempts ON attempts.job_id = cohort.job_id
    )
""")

_METRICS = sql.SQL("""
    count(*) AS requests,
    count(*) FILTER (WHERE published) AS published_requests,
    count(*) FILTER (WHERE status = 'failed') AS failed_requests,
    count(DISTINCT repository_id) AS repository_count,
    count(DISTINCT requester) AS requester_count,
    count(*) FILTER (WHERE requester IS NULL) AS unknown_requester_requests,
    coalesce(sum(started_attempts), 0)::bigint AS started_attempts,
    coalesce(sum(reported_attempts), 0)::bigint AS reported_attempts,
    sum(prompt_tokens)::bigint AS prompt_tokens,
    sum(completion_tokens)::bigint AS completion_tokens,
    sum(total_tokens)::bigint AS total_tokens
""")


def usage(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: AccessScope,
    start: datetime,
    end: datetime,
    now: datetime,
    dimension: UsageDimension,
    sort: UsageSort,
    repository: str | None,
    search: str,
    offset: int,
    limit: int,
) -> UsageReport:
    dimensions: dict[UsageDimension, tuple[LiteralString, ...]] = {
        "team": (
            "coalesce('team:' || team_id::text, 'unassigned')",
            "coalesce(team_name, 'Unassigned repositories')",
            "team_id",
            "NULL::text",
            "NULL::text",
        ),
        "repository": (
            "'repository:' || repository_id::text",
            "repository",
            "team_id",
            "repository",
            "NULL::text",
        ),
        "requester": (
            "coalesce('user:' || requester, 'unknown')",
            "coalesce(requester, 'Unknown requester')",
            "NULL::bigint",
            "NULL::text",
            "requester",
        ),
    }
    key, label, team_id, repository_column, requester = dimensions[dimension]
    requests = _REQUESTS.format(repositories=repository_source(scope))
    matching = sql.SQL(
        " FROM requests WHERE position(lower(%(search)s) IN lower({label})) > 0 "
    ).format(label=sql.SQL(label))
    parameters = {
        "start": start,
        "end": end,
        "repository": repository,
        "search": search,
        "offset": offset,
        "limit": limit + 1,
    }
    with connection.cursor(row_factory=class_row(UsageTotals)) as cursor:
        totals = cursor.execute(
            requests
            + sql.SQL("SELECT count(DISTINCT {key}) AS groups, {metrics}").format(
                key=sql.SQL(key), metrics=_METRICS
            )
            + matching,
            parameters,
        ).fetchone()
    assert totals is not None
    with connection.cursor(row_factory=class_row(UsageRow)) as cursor:
        rows = cursor.execute(
            requests
            + sql.SQL("""
                SELECT {key} AS key, {label} AS label, {team_id} AS team_id,
                    {repository} AS repository, {requester} AS requester, {metrics}
            """).format(
                key=sql.SQL(key),
                label=sql.SQL(label),
                team_id=sql.SQL(team_id),
                repository=sql.SQL(repository_column),
                requester=sql.SQL(requester),
                metrics=_METRICS,
            )
            + matching
            + sql.SQL("""
                GROUP BY 1, 2, 3, 4, 5
                ORDER BY {sort} DESC NULLS LAST, key
                LIMIT %(limit)s OFFSET %(offset)s
            """).format(sort=sql.Identifier(sort)),
            parameters,
        ).fetchall()
    return UsageReport(
        now, start, end, dimension, sort, totals, tuple(rows[:limit]), len(rows) > limit
    )
