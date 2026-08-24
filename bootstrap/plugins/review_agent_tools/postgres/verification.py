"""Concrete PostgreSQL verification and reconciliation operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.finding import FindingOccurrenceId
from ..domain.review import ReviewRunId
from ..domain.verification import (
    CandidateReconciliationId,
    CandidateVerificationDefinition,
    CandidateVerificationId,
    CandidateVerdict,
    ReconciliationDecision,
    ReconciliationDefinition,
    VerificationMode,
    VerificationRunDefinition,
    VerificationRunId,
    VerificationStatus,
)


class VerificationStoreError(ValueError):
    """A verification operation violates its durable review scope."""


class VerificationScopeError(VerificationStoreError):
    """Verification evidence does not belong to the selected review run."""


class CandidateVerificationConflict(VerificationStoreError):
    """The verifier attempt already recorded this candidate occurrence."""


class ReconciliationFrozen(VerificationStoreError):
    """Publication preparation made the reconciliation immutable."""


@dataclass(frozen=True, slots=True)
class VerificationRun:
    id: VerificationRunId
    review_run_id: ReviewRunId
    provider: str | None
    model: str | None
    mode: VerificationMode
    status: VerificationStatus
    bundle_hash: str | None
    failure_code: str | None
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CandidateVerification:
    id: CandidateVerificationId
    verification_run_id: VerificationRunId
    review_run_id: ReviewRunId
    occurrence_id: FindingOccurrenceId
    verdict: CandidateVerdict
    confidence: float
    counter_evidence: str | None
    notes: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateReconciliation:
    id: CandidateReconciliationId
    review_run_id: ReviewRunId
    occurrence_id: FindingOccurrenceId
    verification_run_id: VerificationRunId | None
    final_decision: ReconciliationDecision
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _VerificationRunRow:
    id: VerificationRunId
    review_run_id: ReviewRunId
    provider: str | None
    model: str | None
    mode: str
    status: str
    bundle_hash: str | None
    failure_code: str | None
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _CandidateVerificationRow:
    id: CandidateVerificationId
    verification_run_id: VerificationRunId
    review_run_id: ReviewRunId
    finding_occurrence_id: FindingOccurrenceId
    verdict: str
    confidence: Decimal
    counter_evidence: str | None
    notes: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ReconciliationRow:
    id: CandidateReconciliationId
    review_run_id: ReviewRunId
    finding_occurrence_id: FindingOccurrenceId
    verification_run_id: VerificationRunId | None
    final_decision: str
    reason: str | None
    created_at: datetime


_RUN_COLUMNS = """
    id, review_run_id, provider, model, mode, status, bundle_hash,
    failure_code, started_at, completed_at
"""
_CANDIDATE_COLUMNS = """
    id, verification_run_id, review_run_id, finding_occurrence_id,
    verdict, confidence, counter_evidence, notes, created_at
"""
_RECONCILIATION_COLUMNS = """
    id, review_run_id, finding_occurrence_id, verification_run_id,
    final_decision, reason, created_at
"""


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise VerificationStoreError(
            "verification operations require an active transaction"
        )


def _run(row: _VerificationRunRow) -> VerificationRun:
    try:
        mode = VerificationMode(row.mode)
        status = VerificationStatus(row.status)
    except ValueError as exc:
        raise VerificationStoreError(
            "stored verification lifecycle is invalid"
        ) from exc
    return VerificationRun(
        id=row.id,
        review_run_id=row.review_run_id,
        provider=row.provider,
        model=row.model,
        mode=mode,
        status=status,
        bundle_hash=row.bundle_hash,
        failure_code=row.failure_code,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _candidate(row: _CandidateVerificationRow) -> CandidateVerification:
    try:
        verdict = CandidateVerdict(row.verdict)
    except ValueError as exc:
        raise VerificationStoreError("stored candidate verdict is invalid") from exc
    return CandidateVerification(
        id=row.id,
        verification_run_id=row.verification_run_id,
        review_run_id=row.review_run_id,
        occurrence_id=row.finding_occurrence_id,
        verdict=verdict,
        confidence=float(row.confidence),
        counter_evidence=row.counter_evidence,
        notes=row.notes,
        created_at=row.created_at,
    )


def _reconciliation(row: _ReconciliationRow) -> CandidateReconciliation:
    try:
        decision = ReconciliationDecision(row.final_decision)
    except ValueError as exc:
        raise VerificationStoreError(
            "stored reconciliation decision is invalid"
        ) from exc
    return CandidateReconciliation(
        id=row.id,
        review_run_id=row.review_run_id,
        occurrence_id=row.finding_occurrence_id,
        verification_run_id=row.verification_run_id,
        final_decision=decision,
        reason=row.reason,
        created_at=row.created_at,
    )


def record_run(
    connection: psycopg.Connection[TupleRow],
    *,
    review_run_id: ReviewRunId,
    definition: VerificationRunDefinition,
) -> VerificationRun:
    """Record one external verifier attempt for an existing review run."""
    _require_transaction(connection)
    exists = connection.execute(
        "SELECT id FROM review_agent.review_runs WHERE id = %s FOR KEY SHARE",
        (review_run_id,),
    ).fetchone()
    if exists is None:
        raise VerificationScopeError("verification review run does not exist")
    with connection.cursor(row_factory=class_row(_VerificationRunRow)) as cursor:
        row = cursor.execute(
            f"""
            INSERT INTO review_agent.verification_runs (
                review_run_id, provider, model, mode, status, bundle_hash,
                failure_code, started_at, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_RUN_COLUMNS}
            """,
            (
                review_run_id,
                definition.provider,
                definition.model,
                definition.mode,
                definition.status,
                definition.bundle_hash,
                definition.failure_code,
                definition.started_at,
                definition.completed_at,
            ),
        ).fetchone()
    if row is None:
        raise VerificationStoreError("verification run insert returned no row")
    return _run(row)


def record_candidate(
    connection: psycopg.Connection[TupleRow],
    *,
    verification_run_id: VerificationRunId,
    occurrence_id: FindingOccurrenceId,
    definition: CandidateVerificationDefinition,
) -> CandidateVerification:
    """Record one verdict for one exact occurrence in a verifier attempt."""
    _require_transaction(connection)
    scope = connection.execute(
        """
        SELECT verification.review_run_id, occurrence.review_run_id
        FROM review_agent.verification_runs AS verification
        JOIN review_agent.finding_occurrences AS occurrence
          ON occurrence.id = %s
        WHERE verification.id = %s
        FOR KEY SHARE OF verification, occurrence
        """,
        (occurrence_id, verification_run_id),
    ).fetchone()
    if scope is None or scope[0] != scope[1]:
        raise VerificationScopeError(
            "verification attempt and occurrence could not be resolved "
            "in the same review run"
        )
    try:
        with connection.cursor(
            row_factory=class_row(_CandidateVerificationRow)
        ) as cursor:
            row = cursor.execute(
                f"""
                INSERT INTO review_agent.candidate_verifications (
                    verification_run_id, review_run_id, finding_occurrence_id,
                    verdict, confidence, counter_evidence, notes, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_CANDIDATE_COLUMNS}
                """,
                (
                    verification_run_id,
                    scope[0],
                    occurrence_id,
                    definition.verdict,
                    definition.confidence,
                    definition.counter_evidence,
                    definition.notes,
                    definition.created_at,
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise CandidateVerificationConflict(
            "verification attempt already recorded this occurrence"
        ) from exc
    if row is None:
        raise VerificationStoreError("candidate verification insert returned no row")
    return _candidate(row)


def reconcile_candidate(
    connection: psycopg.Connection[TupleRow],
    *,
    review_run_id: ReviewRunId,
    occurrence_id: FindingOccurrenceId,
    verification_run_id: VerificationRunId | None,
    definition: ReconciliationDefinition,
) -> CandidateReconciliation:
    """Create or revise a candidate decision before publication preparation."""
    _require_transaction(connection)
    # This must remain FOR UPDATE: publication's review-run FK takes KEY SHARE,
    # so concurrent publication preparation serializes before the freeze check.
    run = connection.execute(
        "SELECT status FROM review_agent.review_runs WHERE id = %s FOR UPDATE",
        (review_run_id,),
    ).fetchone()
    if run is None or run[0] != "running":
        raise VerificationScopeError(
            "reconciliation requires an active review run"
        )
    publication = connection.execute(
        "SELECT id FROM review_agent.publications WHERE review_run_id = %s",
        (review_run_id,),
    ).fetchone()
    if publication is not None:
        raise ReconciliationFrozen(
            "publication preparation made reconciliation immutable"
        )
    occurrence = connection.execute(
        "SELECT id FROM review_agent.finding_occurrences "
        "WHERE id = %s AND review_run_id = %s FOR KEY SHARE",
        (occurrence_id, review_run_id),
    ).fetchone()
    if occurrence is None:
        raise VerificationScopeError(
            "reconciliation occurrence belongs to a different review run"
        )
    if verification_run_id is not None:
        verification = connection.execute(
            "SELECT id FROM review_agent.verification_runs "
            "WHERE id = %s AND review_run_id = %s FOR KEY SHARE",
            (verification_run_id, review_run_id),
        ).fetchone()
        if verification is None:
            raise VerificationScopeError(
                "reconciliation verification belongs to a different review run"
            )
    with connection.cursor(row_factory=class_row(_ReconciliationRow)) as cursor:
        row = cursor.execute(
            f"""
            INSERT INTO review_agent.candidate_reconciliations (
                review_run_id, finding_occurrence_id, verification_run_id,
                final_decision, reason, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT
                candidate_reconciliations_run_occurrence_uk
            DO UPDATE SET
                verification_run_id = EXCLUDED.verification_run_id,
                final_decision = EXCLUDED.final_decision,
                reason = EXCLUDED.reason,
                -- Reconciliation is mutable until publication; this timestamp
                -- records the latest revision rather than the first insert.
                created_at = EXCLUDED.created_at
            RETURNING {_RECONCILIATION_COLUMNS}
            """,
            (
                review_run_id,
                occurrence_id,
                verification_run_id,
                definition.final_decision,
                definition.reason,
                definition.created_at,
            ),
        ).fetchone()
    if row is None:
        raise VerificationStoreError("reconciliation upsert returned no row")
    return _reconciliation(row)


def reconciliations_for_run(
    connection: psycopg.Connection[TupleRow],
    *,
    review_run_id: ReviewRunId,
) -> tuple[CandidateReconciliation, ...]:
    """Return the exact final candidate decisions for one review run."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_ReconciliationRow)) as cursor:
        rows = cursor.execute(
            f"SELECT {_RECONCILIATION_COLUMNS} "
            "FROM review_agent.candidate_reconciliations "
            "WHERE review_run_id = %s ORDER BY id",
            (review_run_id,),
        ).fetchall()
    return tuple(_reconciliation(row) for row in rows)
