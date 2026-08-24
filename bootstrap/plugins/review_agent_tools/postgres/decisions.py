"""Concrete PostgreSQL operations for human finding decisions and audit."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.finding import (
    DecisionKind,
    FindingDecision,
    FindingDecisionDefinition,
    FindingDecisionId,
    FindingId,
    FindingOccurrenceId,
)


class DecisionStoreError(ValueError):
    """A decision operation violates its persisted finding scope."""


class DecisionAuditConflict(DecisionStoreError):
    """The authorization audit already belongs to another decision."""


@dataclass(frozen=True, slots=True)
class DecisionAudit:
    actor_user_id: str
    actor_login: str | None
    author_association: str | None
    allowlist_version: str
    source_comment_id: int
    source_comment_url: str | None


@dataclass(frozen=True, slots=True)
class _DecisionRow:
    id: FindingDecisionId
    finding_id: FindingId
    finding_occurrence_id: FindingOccurrenceId | None
    decision: str
    reason: str
    actor: str
    context_hash: str | None
    adr_id: str | None
    created_at: datetime
    expires_at: datetime | None


_DECISION_COLUMNS = """
    id, finding_id, finding_occurrence_id, decision, reason, actor,
    context_hash, adr_id, created_at, expires_at
"""


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise DecisionStoreError("decision operations require an active transaction")


def _decision(row: _DecisionRow) -> FindingDecision:
    try:
        kind = DecisionKind(row.decision)
    except ValueError as exc:
        raise DecisionStoreError("stored decision kind is invalid") from exc
    if row.context_hash is None:
        raise DecisionStoreError(
            "stored decision is missing its exact occurrence context"
        )
    return FindingDecision(
        id=row.id,
        finding_id=row.finding_id,
        occurrence_id=row.finding_occurrence_id,
        decision=kind,
        reason=row.reason,
        actor=row.actor,
        context_hash=row.context_hash,
        adr_id=row.adr_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _append_decision(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_id: FindingId,
    occurrence_id: FindingOccurrenceId,
    definition: FindingDecisionDefinition,
) -> FindingDecision:
    occurrence = connection.execute(
        """
        SELECT context_hash
        FROM review_agent.finding_occurrences
        WHERE id = %s AND finding_id = %s
        FOR KEY SHARE
        """,
        (occurrence_id, finding_id),
    ).fetchone()
    if occurrence is None:
        raise DecisionStoreError(
            "decision occurrence does not belong to the selected finding"
        )
    context_hash = str(occurrence[0])
    with connection.cursor(row_factory=class_row(_DecisionRow)) as cursor:
        row = cursor.execute(
            f"""
            INSERT INTO review_agent.finding_decisions (
                finding_id, finding_occurrence_id, decision, reason, actor,
                context_hash, adr_id, created_at, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_DECISION_COLUMNS}
            """,
            (
                finding_id,
                occurrence_id,
                definition.decision.value,
                definition.reason,
                definition.actor,
                context_hash,
                definition.adr_id,
                definition.created_at,
                definition.expires_at,
            ),
        ).fetchone()
    if row is None:
        raise DecisionStoreError("decision insert did not return its durable row")
    return _decision(row)


def append_decision_with_audit(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_id: FindingId,
    occurrence_id: FindingOccurrenceId,
    definition: FindingDecisionDefinition,
    audit: DecisionAudit,
) -> FindingDecision:
    """Derive occurrence context and append the decision plus audit atomically."""
    _require_transaction(connection)
    decision = _append_decision(
        connection,
        finding_id=finding_id,
        occurrence_id=occurrence_id,
        definition=definition,
    )
    try:
        connection.execute(
            """
            INSERT INTO review_agent.decision_audit (
                finding_decision_id, actor_user_id, actor_login,
                author_association, allowlist_version, source_comment_id,
                source_comment_url, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                decision.id,
                audit.actor_user_id,
                audit.actor_login,
                audit.author_association,
                audit.allowlist_version,
                audit.source_comment_id,
                audit.source_comment_url,
                definition.created_at,
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise DecisionAuditConflict(
            "decision audit source comment was already applied"
        ) from exc
    return decision


def append_operator_decision(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_id: FindingId,
    occurrence_id: FindingOccurrenceId,
    definition: FindingDecisionDefinition,
) -> FindingDecision:
    """Append a directly authenticated operator decision without GitHub audit data."""
    _require_transaction(connection)
    return _append_decision(
        connection,
        finding_id=finding_id,
        occurrence_id=occurrence_id,
        definition=definition,
    )


def latest_decision(
    connection: psycopg.Connection[TupleRow], *, finding_id: FindingId
) -> FindingDecision | None:
    """Return the latest human decision for one stable finding identity."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_DecisionRow)) as cursor:
        row = cursor.execute(
            f"SELECT {_DECISION_COLUMNS} "
            "FROM review_agent.finding_decisions "
            "WHERE finding_id = %s ORDER BY id DESC LIMIT 1",
            (finding_id,),
        ).fetchone()
    return _decision(row) if row is not None else None


def latest_decisions(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_ids: Sequence[FindingId],
) -> dict[FindingId, FindingDecision]:
    """Return the latest appended decision for each requested finding."""
    _require_transaction(connection)
    unique_ids = sorted({int(finding_id) for finding_id in finding_ids})
    if not unique_ids:
        return {}
    with connection.cursor(row_factory=class_row(_DecisionRow)) as cursor:
        rows = cursor.execute(
            f"""
            SELECT DISTINCT ON (finding_id) {_DECISION_COLUMNS}
            FROM review_agent.finding_decisions
            WHERE finding_id = ANY(%s::bigint[])
            ORDER BY finding_id, id DESC
            """,
            (unique_ids,),
        ).fetchall()
    return {row.finding_id: _decision(row) for row in rows}


def decision_history(
    connection: psycopg.Connection[TupleRow], *, finding_id: FindingId
) -> tuple[FindingDecision, ...]:
    """Return the complete append-only decision chain for one finding."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_DecisionRow)) as cursor:
        rows = cursor.execute(
            f"SELECT {_DECISION_COLUMNS} FROM review_agent.finding_decisions "
            "WHERE finding_id = %s ORDER BY id",
            (finding_id,),
        ).fetchall()
    return tuple(_decision(row) for row in rows)
