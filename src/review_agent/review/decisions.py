"""Human decisions on findings and feedback triage, from the console or the operator CLI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import TupleRow

from ..domain import input_checks
from ..domain import repository_decision_context
from ..domain.feedback import resolve_feedback_triage, resolve_repository
from ..domain.finding import (
    DecisionKind,
    FindingDecisionId,
    FindingDomainError,
    FindingOccurrenceId,
    require_explicit_decision_target,
    resolve_decision,
    resolve_fingerprint_query,
)
from ..domain.review import ReviewPurpose
from ..postgres import decisions as postgres_decisions
from ..postgres import findings as postgres_findings
from ..postgres import quality_triage as postgres_quality_triage
from ..postgres import repository_decisions as postgres_repository_decisions
from ..postgres import reporting as postgres_reporting
from ..postgres.runtime import PostgreSQLRuntime


@dataclass(frozen=True, slots=True)
class OperatorDecisionRequest:
    repository: str
    fingerprint: str
    decision: str
    reason: str
    actor: str
    occurrence_id: int | None = None
    pr_number: int | None = None
    local_reference: str = ""
    latest: bool = False
    expires_days: int | None = None
    adr_id: str = ""


@dataclass(frozen=True, slots=True)
class OperatorDecisionResult:
    id: FindingDecisionId
    fingerprint: str
    occurrence_id: FindingOccurrenceId
    decision: DecisionKind
    reason: str
    actor: str
    context_hash: str
    adr_id: str | None
    created_at: datetime
    expires_at: datetime | None


def show_finding(
    runtime: PostgreSQLRuntime,
    *,
    repository: str,
    fingerprint: str,
    occurrence_id: int | None = None,
    decision_limit: int | None = None,
    decision_before_id: int | None = None,
    now: datetime | None = None,
) -> postgres_reporting.FindingDetail:
    with runtime.transaction() as connection:
        return show_finding_in_transaction(
            connection,
            repository=repository,
            fingerprint=fingerprint,
            occurrence_id=occurrence_id,
            decision_limit=decision_limit,
            decision_before_id=decision_before_id,
            now=now,
        )


def show_finding_in_transaction(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str,
    fingerprint: str,
    occurrence_id: int | None = None,
    decision_limit: int | None = None,
    decision_before_id: int | None = None,
    now: datetime | None = None,
    purpose: ReviewPurpose = ReviewPurpose.CODE,
) -> postgres_reporting.FindingDetail:
    normalized_repository = resolve_repository(repository)
    query = resolve_fingerprint_query(fingerprint)
    moment = input_checks.now(now)
    scope = postgres_reporting.repository_scope(
        connection, repository=normalized_repository
    )
    resolved = postgres_findings.resolve_fingerprint(
        connection,
        repository_id=scope.id,
        purpose=purpose,
        query=query,
    )
    return postgres_reporting.finding_detail(
        connection,
        repository_id=scope.id,
        purpose=purpose,
        fingerprint=resolved,
        now=moment,
        occurrence_id=(
            FindingOccurrenceId(input_checks.positive(occurrence_id, field="occurrence_id"))
            if occurrence_id is not None
            else None
        ),
        decision_limit=(
            input_checks.positive(decision_limit, field="decision_limit")
            if decision_limit is not None
            else None
        ),
        decision_before_id=(
            FindingDecisionId(input_checks.positive(decision_before_id, field="decision_before_id"))
            if decision_before_id is not None
            else None
        ),
    )


def decide_finding(
    runtime: PostgreSQLRuntime,
    request: OperatorDecisionRequest,
    *,
    now: datetime | None = None,
) -> OperatorDecisionResult:
    with runtime.transaction() as connection:
        return decide_finding_in_transaction(connection, request, now=now)


def decide_finding_in_transaction(
    connection: psycopg.Connection[TupleRow],
    request: OperatorDecisionRequest,
    *,
    now: datetime | None = None,
) -> OperatorDecisionResult:
    repository = resolve_repository(request.repository)
    query = resolve_fingerprint_query(request.fingerprint)
    moment = input_checks.now(now)
    definition = resolve_decision(
        decision=request.decision,
        reason=request.reason,
        actor=request.actor,
        adr_id=request.adr_id,
        expires_days=request.expires_days,
        now=moment,
    )
    occurrence_id = (
        FindingOccurrenceId(input_checks.positive(request.occurrence_id, field="occurrence_id"))
        if request.occurrence_id is not None
        else None
    )
    pr_number = (
        input_checks.positive(request.pr_number, field="pr_number")
        if request.pr_number is not None
        else None
    )
    local_reference = request.local_reference.strip() or None
    try:
        require_explicit_decision_target(
            occurrence_id=occurrence_id,
            pr_number=pr_number,
            local_reference=local_reference,
            latest=request.latest,
        )
    except FindingDomainError as exc:
        raise input_checks.OperatorInputError(str(exc)) from exc
    scope = postgres_reporting.repository_scope(connection, repository=repository)
    purpose = (
        postgres_reporting.occurrence_purpose(connection, repository_id=scope.id, occurrence_id=occurrence_id)
        if occurrence_id is not None else ReviewPurpose.CODE
    )
    if purpose is ReviewPurpose.DOCUMENTATION and definition.decision is DecisionKind.INTENTIONAL_BY_DESIGN:
        raise input_checks.OperatorInputError("Documentation findings do not support intentional-by-design decisions")
    fingerprint = postgres_findings.resolve_fingerprint(
        connection,
        repository_id=scope.id,
        query=query,
        purpose=purpose,
    )
    target = postgres_reporting.decision_target(
        connection,
        repository_id=scope.id,
        fingerprint=fingerprint,
        occurrence_id=occurrence_id,
        pr_number=pr_number,
        local_reference=local_reference,
        latest=request.latest,
        purpose=purpose,
    )
    intentional_evidence = None
    if definition.decision is DecisionKind.INTENTIONAL_BY_DESIGN:
        decision_context = postgres_repository_decisions.load_context(
            connection,
            run_id=target.review_run_id,
        )
        intentional_evidence = repository_decision_context.intentional_evidence(
            decision_context,
            review_run_id=target.review_run_id,
            adr_id=definition.adr_id or "",
            finding_path=target.path,
        )
        if intentional_evidence is None:
            raise input_checks.OperatorInputError(
                "intentional decision does not match the occurrence's exact "
                "accepted ADR snapshot and path"
            )
    stored = postgres_decisions.append_operator_decision(
        connection,
        finding_id=target.finding_id,
        occurrence_id=target.occurrence_id,
        definition=definition,
        intentional_evidence=intentional_evidence,
    )
    return OperatorDecisionResult(
        id=stored.id,
        fingerprint=fingerprint,
        occurrence_id=target.occurrence_id,
        decision=stored.decision,
        reason=stored.reason,
        actor=stored.actor,
        context_hash=stored.context_hash,
        adr_id=stored.adr_id,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
    )


def triage_review_feedback(
    runtime: PostgreSQLRuntime,
    *,
    feedback_id: int,
    status: str,
    stable_key: str,
    target_owner: str,
    evidence_reference: str,
    path: str,
    category: str,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> postgres_quality_triage.QualityFeedbackTriage:
    with runtime.transaction() as connection:
        return triage_review_feedback_in_transaction(
            connection,
            feedback_id=feedback_id,
            status=status,
            stable_key=stable_key,
            target_owner=target_owner,
            evidence_reference=evidence_reference,
            path=path,
            category=category,
            actor=actor,
            reason=reason,
            now=now,
        )


def triage_review_feedback_in_transaction(
    connection: psycopg.Connection[TupleRow],
    *,
    feedback_id: int,
    status: str,
    stable_key: str,
    target_owner: str,
    evidence_reference: str,
    path: str,
    category: str,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> postgres_quality_triage.QualityFeedbackTriage:
    """Append one governed classification for missed-issue feedback."""
    resolved_feedback_id = input_checks.positive(feedback_id, field="feedback_id")
    definition = resolve_feedback_triage(
        status=status,
        stable_key=stable_key,
        target_owner=target_owner,
        evidence_reference=evidence_reference,
        path=path,
        category=category,
        actor=actor,
        reason=reason,
    )
    moment = input_checks.now(now)
    return postgres_quality_triage.append_triage(
        connection,
        feedback_id=resolved_feedback_id,
        definition=definition,
        created_at=moment,
    )
