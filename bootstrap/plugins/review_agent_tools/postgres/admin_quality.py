"""Bounded quality-feedback read models for the admin console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.feedback import FeedbackTargetOwner, FeedbackTriageStatus
from ..domain.finding import FindingDecision
from .reporting import FindingReport
from .team_access import AccessScope, maintainer_predicate, repository_source


class AdminQualityError(ValueError):
    """Admin quality reporting requires a valid transaction and stored state."""


@dataclass(frozen=True, slots=True)
class QualityFeedbackItem:
    id: int
    repository: str
    pr_number: int
    publication_id: int
    reason: str | None
    actor_login: str | None
    source_comment_url: str | None
    created_at: datetime
    triage_status: FeedbackTriageStatus
    stable_key: str | None
    target_owner: FeedbackTargetOwner | None
    evidence_reference: str | None
    path: str | None
    category: str | None
    triage_actor: str | None
    triage_reason: str | None
    triaged_at: datetime | None
    can_triage: bool = False


@dataclass(frozen=True, slots=True)
class QualityFeedbackPage:
    items: tuple[QualityFeedbackItem, ...]
    total: int
    pending: int
    offset: int
    next_offset: int | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class ReviewFindingItem:
    fingerprint: str
    occurrence_id: int
    local_reference: str
    title: str
    severity: str
    category: str
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class ReviewFindingPage:
    items: tuple[ReviewFindingItem, ...]
    total: int


@dataclass(frozen=True, slots=True)
class AdminFindingDetail:
    finding: FindingReport
    decisions: tuple[FindingDecision, ...]
    has_more_decisions: bool
    next_decision_before_id: int | None
    can_decide: bool = False


@dataclass(frozen=True, slots=True)
class _FeedbackRow:
    id: int
    repository: str
    pr_number: int
    publication_id: int
    reason: str | None
    actor_login: str | None
    source_comment_url: str | None
    created_at: datetime
    triage_status: str
    stable_key: str | None
    target_owner: str | None
    evidence_reference: str | None
    path: str | None
    category: str | None
    triage_actor: str | None
    triage_reason: str | None
    triaged_at: datetime | None
    total: int
    pending: int
    can_triage: bool


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise AdminQualityError(
            "admin quality reporting requires an active transaction"
        )


def feedback_backlog(
    connection: psycopg.Connection[TupleRow],
    *,
    scope: AccessScope | None = None,
    repository: str | None,
    limit: int,
    offset: int,
) -> QualityFeedbackPage:
    """Return latest triage state for a bounded missed-issue feedback page."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(_FeedbackRow)) as cursor:
        rows = cursor.execute(
            sql.SQL("""
            WITH feedback_with_triage AS (
                SELECT feedback.id, repository.full_name AS repository,
                       pull_request.number AS pr_number,
                       feedback.publication_id, feedback.reason,
                       feedback.actor_login, feedback.source_comment_url,
                       feedback.created_at,
                       COALESCE(latest.status, 'pending') AS triage_status,
                       latest.stable_key, latest.target_owner,
                       latest.evidence_reference, latest.path, latest.category,
                       latest.actor AS triage_actor,
                       latest.reason AS triage_reason,
                       latest.created_at AS triaged_at,
                       {can_triage} AS can_triage
                FROM review_agent.review_quality_feedback AS feedback
                JOIN review_agent.pull_requests AS pull_request
                  ON pull_request.id = feedback.pull_request_id
                JOIN {repositories} AS repository
                  ON repository.id = pull_request.repository_id
                LEFT JOIN LATERAL (
                    SELECT triage.status, triage.stable_key,
                           triage.target_owner, triage.evidence_reference,
                           triage.path, triage.category, triage.actor,
                           triage.reason, triage.created_at
                    FROM review_agent.review_quality_feedback_triage AS triage
                    WHERE triage.feedback_id = feedback.id
                    ORDER BY triage.id DESC
                    LIMIT 1
                ) AS latest ON true
                WHERE feedback.category = 'missed_issue'
                  AND (
                      %s::text IS NULL
                      OR lower(repository.full_name) = lower(%s::text)
                  )
            )
            SELECT *, count(*) OVER ()::integer AS total,
                   count(*) FILTER (WHERE triage_status = 'pending')
                       OVER ()::integer AS pending
            FROM feedback_with_triage
            ORDER BY (triage_status = 'pending') DESC, created_at, id
            LIMIT %s OFFSET %s
            """).format(
                repositories=repository_source(scope),
                can_triage=maintainer_predicate(scope, sql.SQL("repository.id")),
            ),
            (repository, repository, limit + 1, offset),
        ).fetchall()
    page_rows = rows[:limit]
    items: list[QualityFeedbackItem] = []
    for row in page_rows:
        try:
            status = FeedbackTriageStatus(row.triage_status)
            owner = (
                FeedbackTargetOwner(row.target_owner)
                if row.target_owner is not None
                else None
            )
        except ValueError as exc:
            raise AdminQualityError("stored feedback triage is invalid") from exc
        items.append(
            QualityFeedbackItem(
                id=row.id,
                repository=row.repository,
                pr_number=row.pr_number,
                publication_id=row.publication_id,
                reason=row.reason,
                actor_login=row.actor_login,
                source_comment_url=row.source_comment_url,
                created_at=row.created_at,
                triage_status=status,
                stable_key=row.stable_key,
                target_owner=owner,
                evidence_reference=row.evidence_reference,
                path=row.path,
                category=row.category,
                triage_actor=row.triage_actor,
                triage_reason=row.triage_reason,
                triaged_at=row.triaged_at,
                can_triage=row.can_triage,
            )
        )
    first = rows[0] if rows else None
    return QualityFeedbackPage(
        items=tuple(items),
        total=first.total if first is not None else 0,
        pending=first.pending if first is not None else 0,
        offset=offset,
        next_offset=offset + limit if len(rows) > limit else None,
        has_more=len(rows) > limit,
    )


def review_findings(
    connection: psycopg.Connection[TupleRow], *, run_id: int
) -> ReviewFindingPage:
    """Return the exact current finding occurrences published for one run."""
    _require_transaction(connection)
    rows = connection.execute(
        """
        SELECT identity.fingerprint,
               published.source_finding_occurrence_id AS occurrence_id,
               published.local_reference, occurrence.title,
               occurrence.severity, occurrence.category,
               identity.path, occurrence.line,
               count(*) OVER ()::integer AS total
        FROM review_agent.publication_findings AS published
        JOIN review_agent.publications AS publication
          ON publication.id = published.publication_id
        JOIN review_agent.finding_identities AS identity
          ON identity.id = published.finding_id
        JOIN review_agent.finding_occurrences AS occurrence
          ON occurrence.id = published.source_finding_occurrence_id
        WHERE publication.review_run_id = %s
          AND publication.status = 'posted'
          AND published.outcome = 'current'
        ORDER BY published.id
        LIMIT 200
        """,
        (run_id,),
    ).fetchall()
    return ReviewFindingPage(
        items=tuple(
            ReviewFindingItem(
                fingerprint=str(row[0]),
                occurrence_id=int(row[1]),
                local_reference=str(row[2]),
                title=str(row[3]),
                severity=str(row[4]),
                category=str(row[5]),
                path=str(row[6]),
                line=int(row[7]),
            )
            for row in rows
        ),
        total=int(rows[0][8]) if rows else 0,
    )
