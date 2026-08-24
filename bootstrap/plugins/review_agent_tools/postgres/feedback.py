"""PostgreSQL feedback event, target, and quality-signal operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, class_row

from ..domain.feedback import FeedbackStatus
from ..domain.finding import FindingId, FindingOccurrenceId
from ..domain.publication import PublicationId
from ..domain.review import PullRequestId
from ..feedback_commands import ReviewQualityFeedbackCommand


class FeedbackStoreError(ValueError):
    """A feedback operation violates its durable transaction contract."""


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    publication_id: PublicationId
    pull_request_id: PullRequestId


@dataclass(frozen=True, slots=True)
class FindingTarget:
    finding_id: FindingId
    occurrence_id: FindingOccurrenceId
    local_reference: str
    fingerprint: str
    title: str
    context_hash: str


def _require_transaction(connection: psycopg.Connection[TupleRow]) -> None:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise FeedbackStoreError("feedback operations require an active transaction")


def claim_event(
    connection: psycopg.Connection[TupleRow],
    *,
    event_id: str,
    processed_at: datetime,
) -> FeedbackStatus | None:
    """Claim once, or return the committed prior outcome after a replay.

    The conflict-then-read protocol requires the runtime's pinned Read Committed
    isolation so the second statement sees the conflicting transaction's result.
    """
    _require_transaction(connection)
    inserted = connection.execute(
        """
        INSERT INTO review_agent.processed_feedback_events (
            event_id, outcome, processed_at
        ) VALUES (%s, 'pending', %s)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
        """,
        (event_id, processed_at),
    ).fetchone()
    if inserted is not None:
        return None
    row = connection.execute(
        """
        SELECT outcome
        FROM review_agent.processed_feedback_events
        WHERE event_id = %s
        FOR KEY SHARE
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        raise FeedbackStoreError("feedback event disappeared after conflict")
    if row[0] == "pending":
        raise FeedbackStoreError("feedback event is still pending")
    try:
        return FeedbackStatus(str(row[0]))
    except ValueError as exc:
        raise FeedbackStoreError("stored feedback outcome is invalid") from exc


def complete_event(
    connection: psycopg.Connection[TupleRow],
    *,
    event_id: str,
    outcome: FeedbackStatus,
) -> None:
    _require_transaction(connection)
    if outcome not in {
        FeedbackStatus.RECORDED,
        FeedbackStatus.NO_MAPPING,
        FeedbackStatus.NOT_CURRENT,
    }:
        raise FeedbackStoreError("feedback outcome is not persistable")
    updated = connection.execute(
        """
        UPDATE review_agent.processed_feedback_events
        SET outcome = %s
        WHERE event_id = %s AND outcome = 'pending'
        RETURNING event_id
        """,
        (outcome.value, event_id),
    ).fetchone()
    if updated is None:
        raise FeedbackStoreError("feedback event is not pending")


def current_publication(
    connection: psycopg.Connection[TupleRow],
    *,
    repository: str,
    pr_number: int,
) -> PublicationTarget | None:
    """Lock the current posted GitHub publication for one repository pull request."""
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(PublicationTarget)) as cursor:
        row = cursor.execute(
            """
            SELECT publication.id AS publication_id,
                   publication.pull_request_id
            FROM review_agent.publications AS publication
            JOIN review_agent.pull_requests AS pull_request
              ON pull_request.id = publication.pull_request_id
            JOIN review_agent.repositories AS repository
              ON repository.id = pull_request.repository_id
            WHERE repository.provider = 'github'
              AND lower(repository.full_name) = lower(%s)
              AND pull_request.number = %s
              AND publication.status = 'posted'
              AND publication.superseded_by_publication_id IS NULL
            FOR SHARE OF publication
            """,
            (repository, pr_number),
        ).fetchone()
    if row is None:
        return None
    return row


def current_finding(
    connection: psycopg.Connection[TupleRow],
    *,
    publication_id: PublicationId,
    local_reference: str,
) -> FindingTarget | None:
    _require_transaction(connection)
    with connection.cursor(row_factory=class_row(FindingTarget)) as cursor:
        row = cursor.execute(
            """
            SELECT finding.id AS finding_id,
                   occurrence.id AS occurrence_id,
                   publication_finding.local_reference,
                   finding.fingerprint,
                   occurrence.title,
                   occurrence.context_hash
            FROM review_agent.publication_findings AS publication_finding
            JOIN review_agent.finding_identities AS finding
              ON finding.id = publication_finding.finding_id
            JOIN review_agent.finding_occurrences AS occurrence
              ON occurrence.id = publication_finding.source_finding_occurrence_id
             AND occurrence.finding_id = publication_finding.finding_id
            WHERE publication_finding.publication_id = %s
              AND publication_finding.local_reference = %s
              AND publication_finding.outcome = 'current'
            """,
            (publication_id, local_reference),
        ).fetchone()
    if row is None:
        return None
    return row


def record_quality_feedback(
    connection: psycopg.Connection[TupleRow],
    *,
    publication: PublicationTarget,
    command: ReviewQualityFeedbackCommand,
    actor_user_id: str,
    actor_login: str | None,
    author_association: str | None,
    source_comment_id: int,
    source_comment_url: str | None,
    created_at: datetime,
) -> int:
    _require_transaction(connection)
    row = connection.execute(
        """
        INSERT INTO review_agent.review_quality_feedback (
            pull_request_id, publication_id, local_reference, category, reason,
            actor_user_id, actor_login, author_association, source_comment_id,
            source_comment_url, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            publication.pull_request_id,
            publication.publication_id,
            command.local_reference or None,
            command.category,
            command.reason,
            actor_user_id,
            actor_login,
            author_association,
            source_comment_id,
            source_comment_url,
            created_at,
        ),
    ).fetchone()
    if row is None:
        raise FeedbackStoreError("quality feedback insert returned no identity")
    return int(row[0])
