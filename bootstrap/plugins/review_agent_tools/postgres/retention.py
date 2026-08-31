"""Conservative, bounded retention operations for approved transient records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow


class RetentionError(ValueError):
    """A retention request is ambiguous or unsafe to execute."""


@dataclass(frozen=True, slots=True)
class RetentionResult:
    before: datetime
    limit: int
    matched: int
    deleted: int
    more: bool
    oldest_processed_at: datetime | None


def prune_terminal_webhook_deliveries(
    connection: psycopg.Connection[TupleRow],
    *,
    before: datetime,
    limit: int,
    apply: bool,
) -> RetentionResult:
    """Preview or delete one oldest-first batch of terminal webhook receipts."""
    if before.tzinfo is None or before.utcoffset() is None:
        raise RetentionError("before must include a timezone")
    if isinstance(limit, bool) or limit < 1:
        raise RetentionError("limit must be positive")
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise RetentionError("retention operations require an active transaction")

    lock_clause = "FOR UPDATE" if apply else ""
    rows = connection.execute(
        f"""
        SELECT id, processed_at
        FROM review_agent.github_webhook_deliveries
        WHERE status IN ('accepted', 'ignored', 'rejected', 'failed')
          AND processed_at < %s
        ORDER BY processed_at, id
        {lock_clause}
        LIMIT %s
        """,
        (before, limit + 1),
    ).fetchall()
    candidates = rows[:limit]
    deleted = 0
    if apply and candidates:
        candidate_ids = [cast(int, row[0]) for row in candidates]
        deleted = len(
            connection.execute(
                """
                DELETE FROM review_agent.github_webhook_deliveries
                WHERE id = ANY(%s)
                RETURNING id
                """,
                (candidate_ids,),
            ).fetchall()
        )
        if deleted != len(candidate_ids):
            raise RetentionError("retention batch changed while it was locked")

    oldest = cast(datetime, candidates[0][1]) if candidates else None
    return RetentionResult(
        before=before,
        limit=limit,
        matched=len(candidates),
        deleted=deleted,
        more=len(rows) > limit,
        oldest_processed_at=oldest,
    )
