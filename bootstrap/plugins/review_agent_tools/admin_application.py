"""Read-only application boundary for the operator web panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .domain.feedback import resolve_repository
from .postgres import admin_reporting
from .postgres.runtime import PostgreSQLRuntime


def repositories(
    runtime: PostgreSQLRuntime,
    *,
    days: int = 30,
    search: str = "",
    offset: int = 0,
    limit: int = 50,
) -> admin_reporting.RepositoryPage:
    _bounds(days=days, limit=limit)
    if not 0 <= offset <= 10000 or len(search) > 200:
        raise ValueError("repository filter exceeds its bounds")
    now = datetime.now(timezone.utc)
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return admin_reporting.repositories(
            connection,
            since=now - timedelta(days=days),
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
    now = datetime.now(timezone.utc)
    with runtime.transaction() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return admin_reporting.history(
            connection,
            since=now - timedelta(days=days),
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
