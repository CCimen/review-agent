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
    IntentionalDesignEvidence,
)
from ..domain.review import ReviewRunId
from .. import repository_decision_context
from . import repository_decisions as decision_snapshots


class DecisionStoreError(ValueError):
    """A decision operation violates its persisted finding scope."""


class DecisionAuditConflict(DecisionStoreError):
    """The authorization audit already belongs to another decision."""


@dataclass(frozen=True, slots=True)
class DecisionAudit:
    actor_user_id: str
    actor_login: str | None
    author_association: str | None
    authorization_version: str
    source_comment_id: int
    source_comment_url: str | None


@dataclass(frozen=True, slots=True)
class SuppressionDecision:
    """Latest finding decision plus current-run ADR validity when required."""

    latest: FindingDecision
    intentional_evidence_current: bool


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


@dataclass(frozen=True, slots=True)
class _IntentionalEvidenceRow:
    finding_decision_id: FindingDecisionId
    finding_id: FindingId
    finding_path: str
    review_run_id: ReviewRunId
    review_decision_snapshot_id: int
    repository_decision_id: str
    repository_decision_metadata_hash: str
    repository_decision_path: str
    repository_decision_base_sha: str


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


def _validated_intentional_evidence(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_id: FindingId,
    occurrence_id: FindingOccurrenceId,
    definition: FindingDecisionDefinition,
    evidence: IntentionalDesignEvidence | None,
) -> IntentionalDesignEvidence | None:
    intentional = definition.decision is DecisionKind.INTENTIONAL_BY_DESIGN
    if intentional != (evidence is not None):
        raise DecisionStoreError(
            "intentional-by-design decisions require exact repository evidence"
        )
    if evidence is None:
        return None
    target = connection.execute(
        """
        SELECT occurrence.review_run_id, identity.path
        FROM review_agent.finding_occurrences AS occurrence
        JOIN review_agent.finding_identities AS identity
          ON identity.id = occurrence.finding_id
        WHERE occurrence.id = %s AND occurrence.finding_id = %s
        FOR KEY SHARE OF occurrence
        """,
        (occurrence_id, finding_id),
    ).fetchone()
    if target is None:
        raise DecisionStoreError(
            "decision occurrence does not belong to the selected finding"
        )
    review_run_id = ReviewRunId(int(target[0]))
    context = decision_snapshots.load_context(
        connection,
        run_id=review_run_id,
    )
    expected = repository_decision_context.intentional_evidence(
        context,
        review_run_id=review_run_id,
        adr_id=definition.adr_id or "",
        finding_path=str(target[1]),
    )
    if expected is None or expected != evidence:
        raise DecisionStoreError(
            "intentional repository evidence does not match the finding snapshot"
        )
    return expected


def _insert_intentional_evidence(
    connection: psycopg.Connection[TupleRow],
    *,
    decision: FindingDecision,
    evidence: IntentionalDesignEvidence | None,
) -> None:
    if evidence is None:
        return
    if decision.occurrence_id is None:
        raise DecisionStoreError("intentional decision is missing its occurrence")
    connection.execute(
        """
        INSERT INTO review_agent.intentional_design_evidence (
            finding_decision_id, finding_occurrence_id, review_run_id,
            decision_kind, review_decision_snapshot_id,
            repository_decision_id, repository_decision_metadata_hash,
            repository_decision_path, repository_decision_base_sha, recorded_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            decision.id,
            decision.occurrence_id,
            evidence.review_run_id,
            decision.decision.value,
            evidence.review_decision_snapshot_id,
            evidence.repository_decision_id,
            evidence.repository_decision_metadata_hash,
            evidence.repository_decision_path,
            evidence.repository_decision_base_sha,
            decision.created_at,
        ),
    )


def append_decision_with_audit(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_id: FindingId,
    occurrence_id: FindingOccurrenceId,
    definition: FindingDecisionDefinition,
    audit: DecisionAudit,
    intentional_evidence: IntentionalDesignEvidence | None = None,
) -> FindingDecision:
    """Derive occurrence context and append the decision plus audit atomically."""
    _require_transaction(connection)
    resolved_evidence = _validated_intentional_evidence(
        connection,
        finding_id=finding_id,
        occurrence_id=occurrence_id,
        definition=definition,
        evidence=intentional_evidence,
    )
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
                author_association, authorization_version, source_comment_id,
                source_comment_url, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                decision.id,
                audit.actor_user_id,
                audit.actor_login,
                audit.author_association,
                audit.authorization_version,
                audit.source_comment_id,
                audit.source_comment_url,
                definition.created_at,
            ),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise DecisionAuditConflict(
            "decision audit source comment was already applied"
        ) from exc
    _insert_intentional_evidence(
        connection,
        decision=decision,
        evidence=resolved_evidence,
    )
    return decision


def append_operator_decision(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_id: FindingId,
    occurrence_id: FindingOccurrenceId,
    definition: FindingDecisionDefinition,
    intentional_evidence: IntentionalDesignEvidence | None = None,
) -> FindingDecision:
    """Append a directly authenticated operator decision without GitHub audit data."""
    _require_transaction(connection)
    resolved_evidence = _validated_intentional_evidence(
        connection,
        finding_id=finding_id,
        occurrence_id=occurrence_id,
        definition=definition,
        evidence=intentional_evidence,
    )
    decision = _append_decision(
        connection,
        finding_id=finding_id,
        occurrence_id=occurrence_id,
        definition=definition,
    )
    _insert_intentional_evidence(
        connection,
        decision=decision,
        evidence=resolved_evidence,
    )
    return decision


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


def latest_suppression_decisions(
    connection: psycopg.Connection[TupleRow],
    *,
    finding_ids: Sequence[FindingId],
    current_run_id: ReviewRunId,
) -> dict[FindingId, SuppressionDecision]:
    """Load latest decisions and validate intentional evidence once per run."""
    decisions = latest_decisions(connection, finding_ids=finding_ids)
    intentional_ids = [
        int(decision.id)
        for decision in decisions.values()
        if decision.decision is DecisionKind.INTENTIONAL_BY_DESIGN
    ]
    current_evidence: set[FindingId] = set()
    if intentional_ids:
        context = decision_snapshots.load_context(
            connection,
            run_id=current_run_id,
        )
        with connection.cursor(row_factory=class_row(_IntentionalEvidenceRow)) as cursor:
            rows = cursor.execute(
                """
                SELECT evidence.finding_decision_id,
                       decision.finding_id,
                       identity.path AS finding_path,
                       evidence.review_run_id,
                       evidence.review_decision_snapshot_id,
                       evidence.repository_decision_id,
                       evidence.repository_decision_metadata_hash,
                       evidence.repository_decision_path,
                       evidence.repository_decision_base_sha
                FROM review_agent.intentional_design_evidence AS evidence
                JOIN review_agent.finding_decisions AS decision
                  ON decision.id = evidence.finding_decision_id
                JOIN review_agent.finding_identities AS identity
                  ON identity.id = decision.finding_id
                WHERE evidence.finding_decision_id = ANY(%s::bigint[])
                """,
                (intentional_ids,),
            ).fetchall()
        for row in rows:
            evidence = IntentionalDesignEvidence(
                review_run_id=row.review_run_id,
                review_decision_snapshot_id=row.review_decision_snapshot_id,
                repository_decision_id=row.repository_decision_id,
                repository_decision_metadata_hash=(
                    row.repository_decision_metadata_hash
                ),
                repository_decision_path=row.repository_decision_path,
                repository_decision_base_sha=row.repository_decision_base_sha,
            )
            if repository_decision_context.intentional_evidence_is_current(
                context,
                evidence=evidence,
                finding_path=row.finding_path,
            ):
                current_evidence.add(row.finding_id)
    return {
        finding_id: SuppressionDecision(
            latest=decision,
            intentional_evidence_current=finding_id in current_evidence,
        )
        for finding_id, decision in decisions.items()
    }


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
