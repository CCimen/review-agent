"""Append-only PostgreSQL state for human missed-issue triage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.feedback import (
    FeedbackTargetOwner,
    FeedbackTriageDefinition,
    FeedbackTriageStatus,
)


class QualityTriageStoreError(ValueError):
    """A quality-triage operation violates its durable feedback scope."""


@dataclass(frozen=True, slots=True)
class QualityFeedbackTriage:
    id: int
    feedback_id: int
    status: FeedbackTriageStatus
    stable_key: str | None
    target_owner: FeedbackTargetOwner | None
    evidence_reference: str | None
    path: str | None
    category: str | None
    actor: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _TriageRow:
    id: int
    feedback_id: int
    status: str
    stable_key: str | None
    target_owner: str | None
    evidence_reference: str | None
    path: str | None
    category: str | None
    actor: str
    reason: str
    created_at: datetime


_TRIAGE_COLUMNS = """
    id, feedback_id, status, stable_key, target_owner, evidence_reference,
    path, category, actor, reason, created_at
"""


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise QualityTriageStoreError(
            "quality triage operations require an active transaction"
        )


def _triage(row: _TriageRow) -> QualityFeedbackTriage:
    try:
        status = FeedbackTriageStatus(row.status)
        owner = (
            FeedbackTargetOwner(row.target_owner)
            if row.target_owner is not None
            else None
        )
    except ValueError as exc:
        raise QualityTriageStoreError("stored quality triage is invalid") from exc
    return QualityFeedbackTriage(
        id=row.id,
        feedback_id=row.feedback_id,
        status=status,
        stable_key=row.stable_key,
        target_owner=owner,
        evidence_reference=row.evidence_reference,
        path=row.path,
        category=row.category,
        actor=row.actor,
        reason=row.reason,
        created_at=row.created_at,
    )


def append_triage(
    connection: psycopg.Connection[TupleRow],
    *,
    feedback_id: int,
    definition: FeedbackTriageDefinition,
    created_at: datetime,
) -> QualityFeedbackTriage:
    """Append one operator decision for an existing missed-issue signal."""
    _require_transaction(connection)
    feedback = connection.execute(
        """
        SELECT category
        FROM review_agent.review_quality_feedback
        WHERE id = %s
        FOR KEY SHARE
        """,
        (feedback_id,),
    ).fetchone()
    if feedback is None:
        raise QualityTriageStoreError("review quality feedback does not exist")
    if feedback != ("missed_issue",):
        raise QualityTriageStoreError("only missed-issue feedback can be triaged")
    with connection.cursor(row_factory=class_row(_TriageRow)) as cursor:
        row = cursor.execute(
            f"""
            INSERT INTO review_agent.review_quality_feedback_triage (
                feedback_id, feedback_category, status, stable_key,
                target_owner, evidence_reference, path, category, actor,
                reason, created_at
            ) VALUES (%s, 'missed_issue', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_TRIAGE_COLUMNS}
            """,
            (
                feedback_id,
                definition.status.value,
                definition.stable_key,
                (
                    definition.target_owner.value
                    if definition.target_owner is not None
                    else None
                ),
                definition.evidence_reference,
                definition.path,
                definition.category,
                definition.actor,
                definition.reason,
                created_at,
            ),
        ).fetchone()
    if row is None:
        raise QualityTriageStoreError("quality triage insert returned no identity")
    return _triage(row)
