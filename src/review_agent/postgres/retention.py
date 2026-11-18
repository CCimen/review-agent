"""Conservative, bounded retention operations for approved transient records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow


class RetentionError(ValueError):
    """A retention request is ambiguous or unsafe to execute."""


RetentionTarget = Literal["terminal_webhook_deliveries", "integration_reads"]


@dataclass(frozen=True, slots=True)
class RetentionResult:
    before: datetime
    limit: int
    matched: int
    deleted: int
    more: bool
    oldest_timestamp: datetime | None


def prune_receipts(
    connection: psycopg.Connection[TupleRow],
    *,
    target: RetentionTarget,
    before: datetime,
    limit: int,
    apply: bool,
) -> RetentionResult:
    """Preview or delete one oldest-first batch of an approved receipt class."""
    if before.tzinfo is None or before.utcoffset() is None:
        raise RetentionError("before must include a timezone")
    if isinstance(limit, bool) or limit < 1:
        raise RetentionError("limit must be positive")
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise RetentionError("retention operations require an active transaction")

    if target == "terminal_webhook_deliveries":
        table = "github_webhook_deliveries"
        timestamp = "processed_at"
        predicate = "status IN ('accepted', 'ignored', 'rejected', 'failed')"
    elif target == "integration_reads":
        table = "admin_audit_events"
        timestamp = "recorded_at"
        predicate = "action = 'integration_read' AND actor_role = 'integration' AND outcome = 'succeeded'"
    else:
        raise RetentionError("unknown retention target")

    lock_clause = "FOR UPDATE" if apply else ""
    rows = connection.execute(
        sql.SQL("""
        SELECT id, {timestamp}
        FROM {table}
        WHERE {predicate} AND {timestamp} < %s
        ORDER BY {timestamp}, id
        {lock}
        LIMIT %s
        """).format(
            timestamp=sql.Identifier(timestamp),
            table=sql.Identifier("review_agent", table),
            predicate=sql.SQL(predicate),
            lock=sql.SQL(lock_clause),
        ),
        (before, limit + 1),
    ).fetchall()
    candidates = rows[:limit]
    deleted = 0
    if apply and candidates:
        candidate_ids = [cast(int, row[0]) for row in candidates]
        deleted = len(
            connection.execute(
                sql.SQL("""
                DELETE FROM {table}
                WHERE id = ANY(%s)
                RETURNING id
                """).format(table=sql.Identifier("review_agent", table)),
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
        oldest_timestamp=oldest,
    )
