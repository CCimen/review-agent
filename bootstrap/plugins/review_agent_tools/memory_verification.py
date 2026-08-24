"""Verifier output and Codex reconciliation state for review runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

try:
    from .domain.verification import (
        CandidateVerdict as CandidateVerdict,
        ReconciliationDecision as ReconciliationDecision,
        VerificationDomainError,
        VerificationMode as VerificationMode,
        VerificationStatus as VerificationStatus,
        resolve_candidate_verification,
        resolve_reconciliation,
        resolve_verification_run,
    )
    from .memory_validation import (
        ReviewMemoryError,
        isoformat,
        utc_now,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from domain.verification import (
        CandidateVerdict as CandidateVerdict,
        ReconciliationDecision as ReconciliationDecision,
        VerificationDomainError,
        VerificationMode as VerificationMode,
        VerificationStatus as VerificationStatus,
        resolve_candidate_verification,
        resolve_reconciliation,
        resolve_verification_run,
    )
    from memory_validation import (
        ReviewMemoryError,
        isoformat,
        utc_now,
    )


def _positive_id(value: int, *, field: str) -> int:
    if isinstance(value, bool) or int(value) < 1:
        raise ReviewMemoryError(f"{field} must be a positive integer")
    return int(value)


def _run_row(connection: sqlite3.Connection, review_run_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM review_runs WHERE id = ?",
        (review_run_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("review_run_id does not match a recorded review run")
    return dict(row)


def _observation_row(
    connection: sqlite3.Connection, *, review_run_id: int, observation_id: int
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT id, review_run_id, fingerprint
        FROM finding_observations
        WHERE id = ?
        """,
        (observation_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("observation_id does not match a recorded finding")
    item = dict(row)
    if int(item["review_run_id"] or 0) != review_run_id:
        raise ReviewMemoryError("observation_id belongs to a different review run")
    return item


def _verification_row(
    connection: sqlite3.Connection, verification_run_id: int
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM review_verification_runs WHERE id = ?",
        (verification_run_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("verification_run_id does not match a verifier run")
    return dict(row)


def _ensure_no_publication(connection: sqlite3.Connection, review_run_id: int) -> None:
    row = connection.execute(
        "SELECT id FROM review_publications WHERE review_run_id = ? LIMIT 1",
        (review_run_id,),
    ).fetchone()
    if row is not None:
        raise ReviewMemoryError(
            "review run already has a publication; reconciliation is immutable"
        )


def record_verification_run(
    connection: sqlite3.Connection,
    *,
    review_run_id: int,
    provider: str = "",
    model: str = "",
    mode: str = "advise",
    status: str = "completed",
    bundle_hash: str = "",
    failure_code: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one external verifier attempt for a review run.

    This table is audit input only. Publication ignores these rows until Codex
    records an explicit candidate reconciliation.
    """
    review_run_id = _positive_id(review_run_id, field="review_run_id")
    _run_row(connection, review_run_id)
    try:
        definition = resolve_verification_run(
            provider=provider,
            model=model,
            mode=mode,
            status=status,
            bundle_hash=bundle_hash,
            failure_code=failure_code,
            now=now or utc_now(),
        )
    except VerificationDomainError as exc:
        raise ReviewMemoryError(str(exc)) from exc
    moment = isoformat(definition.started_at)
    completed_at = (
        isoformat(definition.completed_at) if definition.completed_at else None
    )
    cursor = connection.execute(
        """
        INSERT INTO review_verification_runs (
            review_run_id, provider, model, mode, status, bundle_hash,
            failure_code, started_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_run_id,
            definition.provider or "",
            definition.model or "",
            definition.mode,
            definition.status,
            definition.bundle_hash or "",
            definition.failure_code or "",
            moment,
            completed_at,
        ),
    )
    connection.commit()
    return _verification_row(connection, int(cursor.lastrowid or 0))


def record_candidate_verification(
    connection: sqlite3.Connection,
    *,
    verification_run_id: int,
    observation_id: int,
    verdict: str,
    confidence: float,
    counter_evidence: str = "",
    notes: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a verifier verdict for an existing Codex candidate observation."""
    verification_run_id = _positive_id(
        verification_run_id, field="verification_run_id"
    )
    observation_id = _positive_id(observation_id, field="observation_id")
    verification = _verification_row(connection, verification_run_id)
    review_run_id = int(verification["review_run_id"])
    observation = _observation_row(
        connection,
        review_run_id=review_run_id,
        observation_id=observation_id,
    )
    try:
        definition = resolve_candidate_verification(
            verdict=verdict,
            confidence=confidence,
            counter_evidence=counter_evidence,
            notes=notes,
            now=now or utc_now(),
        )
    except VerificationDomainError as exc:
        raise ReviewMemoryError(str(exc)) from exc
    cursor = connection.execute(
        """
        INSERT INTO candidate_verifications (
            verification_run_id, review_run_id, observation_id, fingerprint,
            verdict, confidence, counter_evidence, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verification_run_id,
            review_run_id,
            observation_id,
            observation["fingerprint"],
            definition.verdict,
            definition.confidence,
            definition.counter_evidence or "",
            definition.notes or "",
            isoformat(definition.created_at),
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM candidate_verifications WHERE id = ?",
        (int(cursor.lastrowid or 0),),
    ).fetchone()
    return dict(row) if row else {}


def record_candidate_reconciliation(
    connection: sqlite3.Connection,
    *,
    review_run_id: int,
    observation_id: int,
    final_decision: str,
    reason: str = "",
    verification_run_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record Codex's final decision for whether a candidate publishes."""
    review_run_id = _positive_id(review_run_id, field="review_run_id")
    observation_id = _positive_id(observation_id, field="observation_id")
    run = _run_row(connection, review_run_id)
    if str(run["status"]) != "running":
        raise ReviewMemoryError("review_run_id is not an active review run")
    _ensure_no_publication(connection, review_run_id)
    observation = _observation_row(
        connection,
        review_run_id=review_run_id,
        observation_id=observation_id,
    )
    try:
        definition = resolve_reconciliation(
            final_decision=final_decision,
            reason=reason,
            now=now or utc_now(),
        )
    except VerificationDomainError as exc:
        raise ReviewMemoryError(str(exc)) from exc
    verification_id: int | None = None
    if verification_run_id is not None:
        verification_id = _positive_id(
            verification_run_id, field="verification_run_id"
        )
        verification = _verification_row(connection, verification_id)
        if int(verification["review_run_id"]) != review_run_id:
            raise ReviewMemoryError(
                "verification_run_id belongs to a different review run"
            )
    connection.execute(
        """
        INSERT INTO candidate_reconciliations (
            review_run_id, observation_id, fingerprint, final_decision,
            reason, verification_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_run_id, fingerprint) DO UPDATE SET
            observation_id = excluded.observation_id,
            final_decision = excluded.final_decision,
            reason = excluded.reason,
            verification_run_id = excluded.verification_run_id,
            created_at = excluded.created_at
        """,
        (
            review_run_id,
            observation_id,
            observation["fingerprint"],
            definition.final_decision,
            definition.reason or "",
            verification_id,
            isoformat(definition.created_at),
        ),
    )
    connection.commit()
    row = connection.execute(
        """
        SELECT *
        FROM candidate_reconciliations
        WHERE review_run_id = ? AND fingerprint = ?
        """,
        (review_run_id, observation["fingerprint"]),
    ).fetchone()
    return dict(row) if row else {}


def candidate_reconciliations_for_run(
    connection: sqlite3.Connection, review_run_id: int
) -> dict[str, dict[str, Any]]:
    review_run_id = _positive_id(review_run_id, field="review_run_id")
    rows = connection.execute(
        """
        SELECT *
        FROM candidate_reconciliations
        WHERE review_run_id = ?
        """,
        (review_run_id,),
    ).fetchall()
    return {str(row["fingerprint"]): dict(row) for row in rows}


def latest_verification_status_by_run(
    connection: sqlite3.Connection, review_run_ids: list[int]
) -> dict[int, dict[str, Any]]:
    ids = sorted({_positive_id(run_id, field="review_run_id") for run_id in review_run_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT *
        FROM review_verification_runs
        WHERE id IN (
            SELECT MAX(id)
            FROM review_verification_runs
            WHERE review_run_id IN ({placeholders})
            GROUP BY review_run_id
        )
        """,
        ids,
    ).fetchall()
    return {int(row["review_run_id"]): dict(row) for row in rows}
