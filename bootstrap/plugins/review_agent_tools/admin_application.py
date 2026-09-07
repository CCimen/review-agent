"""Read-only application boundary for the operator web panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from .domain.feedback import resolve_repository
from .postgres import admin_operations, admin_reporting
from .postgres.runtime import PostgreSQLRuntime


def repositories(
    runtime: PostgreSQLRuntime,
    *,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
    search: str = "",
    offset: int = 0,
    limit: int = 50,
) -> admin_reporting.RepositoryPage:
    _bounds(days=days, limit=limit)
    if not 0 <= offset <= 10000 or len(search) > 200:
        raise ValueError("repository filter exceeds its bounds")
    since, until, now = report_window(days=days, start=start, end=end)
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return admin_reporting.repositories(
            connection,
            since=since,
            until=until,
            now=now,
            days=days,
            search=search.strip(),
            offset=offset,
            limit=limit,
        )


def history(
    runtime: PostgreSQLRuntime,
    *,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
    repository: str | None = None,
    status: admin_reporting.HistoryStatus = "all",
    pr_number: int | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> admin_reporting.HistoryPage:
    _bounds(days=days, limit=limit)
    if status not in (
        "all",
        "active",
        "published",
        "failed",
        "latest_failed",
        "superseded",
    ):
        raise ValueError("unsupported history status")
    if any(value is not None and value < 1 for value in (pr_number, before_id)):
        raise ValueError("PR number and cursor must be positive")
    normalized = resolve_repository(repository) if repository is not None else None
    since, until, now = report_window(days=days, start=start, end=end)
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return admin_reporting.history(
            connection,
            since=since,
            until=until,
            now=now,
            days=days,
            repository=normalized,
            status=status,
            pr_number=pr_number,
            limit=limit,
            before_id=before_id,
        )


def _bounds(*, days: int, limit: int) -> None:
    if not 1 <= days <= 90 or not 1 <= limit <= 100:
        raise ValueError("days must be 1–90 and limit must be 1–100")


def report_window(
    *, days: int, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime, datetime]:
    now = datetime.now(timezone.utc)
    if (start is None) != (end is None):
        raise ValueError("Provide both start and end")
    if start is None or end is None:
        return now - timedelta(days=days), now, now
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Use timestamps with a timezone offset")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if not start < end or end - start > timedelta(days=366):
        raise ValueError("The time range must be positive and at most 366 days")
    return start, end, now


def overview(
    runtime: PostgreSQLRuntime,
    *,
    days: int = 30,
    start: datetime | None = None,
    end: datetime | None = None,
) -> admin_operations.Overview:
    _bounds(days=days, limit=1)
    window_start, window_end, now = report_window(days=days, start=start, end=end)
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return admin_operations.overview(
            connection, start=window_start, end=window_end, now=now
        )


def operations(runtime: PostgreSQLRuntime) -> admin_operations.Operations:
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return admin_operations.operations(connection, now=datetime.now(timezone.utc))


def events(
    runtime: PostgreSQLRuntime,
    *,
    limit: int = 50,
    worker_id: UUID | None = None,
    before_id: int | None = None,
) -> admin_operations.WorkerEventPage:
    _bounds(days=1, limit=limit)
    if before_id is not None and before_id < 1:
        raise ValueError("The cursor must be positive")
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        return admin_operations.events(
            connection, worker_id=worker_id, before_id=before_id, limit=limit
        )
