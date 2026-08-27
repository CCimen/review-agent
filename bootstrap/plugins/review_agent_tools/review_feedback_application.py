"""Application boundary for durable review feedback."""

from __future__ import annotations

from datetime import datetime, timezone
import re

from .domain.feedback import (
    FeedbackResult,
    FeedbackStatus,
    resolve_event_id,
    resolve_positive_int,
    resolve_repository,
    resolve_text,
)
from .domain.finding import resolve_decision
from .feedback_commands import (
    FeedbackCommand,
    FindingFeedbackCommand,
    ReviewQualityFeedbackCommand,
)
from .postgres import decisions as postgres_decisions
from .postgres import feedback as postgres_feedback
from .postgres import repository_decisions as postgres_repository_decisions
from . import repository_decision_context
from .postgres.runtime import PostgreSQLRuntime


class ReviewFeedbackError(ValueError):
    """Feedback could not be admitted or recorded."""


def record_postgres_feedback(
    runtime: PostgreSQLRuntime,
    *,
    event_id: str,
    repository: str,
    pr_number: int,
    command: FeedbackCommand,
    actor_user_id: object,
    actor_login: str = "",
    author_association: str = "",
    authorization_version: str,
    source_comment_id: object,
    source_comment_url: str = "",
    expires_days: int | None = None,
    now: datetime | None = None,
) -> FeedbackResult:
    """Validate feedback before opening its one PostgreSQL transaction."""
    resolved_event_id = resolve_event_id(event_id)
    resolved_actor_user_id = str(
        resolve_positive_int(actor_user_id, field="actor_user_id")
    )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", authorization_version) is None:
        raise ReviewFeedbackError(
            "authorization_version must be a sha256:<64 hex> identifier"
        )
    resolved_repository = resolve_repository(repository)
    resolved_pr_number = resolve_positive_int(pr_number, field="pr_number")
    resolved_source_comment_id = resolve_positive_int(
        source_comment_id,
        field="source_comment_id",
    )
    resolved_actor_login = resolve_text(
        actor_login,
        field="actor_login",
        maximum=200,
    )
    resolved_association = resolve_text(
        author_association,
        field="author_association",
        maximum=80,
    )
    resolved_source_url = resolve_text(
        source_comment_url,
        field="source_comment_url",
        maximum=500,
    )
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ReviewFeedbackError("feedback time must be timezone-aware")

    decision_definition = None
    if isinstance(command, FindingFeedbackCommand):
        decision_definition = resolve_decision(
            decision=command.decision,
            reason=command.reason,
            actor=f"github-id:{resolved_actor_user_id}",
            adr_id=command.adr_id,
            expires_days=expires_days,
            now=moment,
        )

    with runtime.transaction() as connection:
        existing = postgres_feedback.claim_event(
            connection,
            event_id=resolved_event_id,
            processed_at=moment,
        )
        if existing is not None:
            return FeedbackResult(
                status=existing,
                event_id=resolved_event_id,
                replayed=True,
            )

        publication = postgres_feedback.current_publication(
            connection,
            repository=resolved_repository,
            pr_number=resolved_pr_number,
        )
        if publication is None:
            postgres_feedback.complete_event(
                connection,
                event_id=resolved_event_id,
                outcome=FeedbackStatus.NO_MAPPING,
            )
            return FeedbackResult(
                status=FeedbackStatus.NO_MAPPING,
                event_id=resolved_event_id,
            )

        if isinstance(command, ReviewQualityFeedbackCommand):
            if command.local_reference and postgres_feedback.current_finding(
                connection,
                publication_id=publication.publication_id,
                local_reference=command.local_reference,
            ) is None:
                postgres_feedback.complete_event(
                    connection,
                    event_id=resolved_event_id,
                    outcome=FeedbackStatus.NOT_CURRENT,
                )
                return FeedbackResult(
                    status=FeedbackStatus.NOT_CURRENT,
                    event_id=resolved_event_id,
                    local_reference=command.local_reference,
                )
            feedback_id = postgres_feedback.record_quality_feedback(
                connection,
                publication=publication,
                command=command,
                actor_user_id=resolved_actor_user_id,
                actor_login=resolved_actor_login or None,
                author_association=resolved_association or None,
                source_comment_id=resolved_source_comment_id,
                source_comment_url=resolved_source_url or None,
                created_at=moment,
            )
            postgres_feedback.complete_event(
                connection,
                event_id=resolved_event_id,
                outcome=FeedbackStatus.RECORDED,
            )
            return FeedbackResult(
                status=FeedbackStatus.RECORDED,
                event_id=resolved_event_id,
                feedback_id=feedback_id,
            )

        target = postgres_feedback.current_finding(
            connection,
            publication_id=publication.publication_id,
            local_reference=command.local_reference,
        )
        if target is None:
            postgres_feedback.complete_event(
                connection,
                event_id=resolved_event_id,
                outcome=FeedbackStatus.NOT_CURRENT,
            )
            return FeedbackResult(
                status=FeedbackStatus.NOT_CURRENT,
                event_id=resolved_event_id,
                local_reference=command.local_reference,
            )
        if decision_definition is None:
            raise ReviewFeedbackError("finding feedback has no decision definition")
        intentional_evidence = None
        if command.decision == "intentional_by_design":
            decision_context = postgres_repository_decisions.load_context(
                connection,
                run_id=target.review_run_id,
            )
            intentional_evidence = repository_decision_context.intentional_evidence(
                decision_context,
                review_run_id=target.review_run_id,
                adr_id=command.adr_id,
                finding_path=target.path,
            )
            if intentional_evidence is None:
                postgres_feedback.complete_event(
                    connection,
                    event_id=resolved_event_id,
                    outcome=FeedbackStatus.STALE,
                )
                return FeedbackResult(
                    status=FeedbackStatus.STALE,
                    event_id=resolved_event_id,
                    local_reference=target.local_reference,
                    adr_id=command.adr_id,
                )
        decision = postgres_decisions.append_decision_with_audit(
            connection,
            finding_id=target.finding_id,
            occurrence_id=target.occurrence_id,
            definition=decision_definition,
            audit=postgres_decisions.DecisionAudit(
                actor_user_id=resolved_actor_user_id,
                actor_login=resolved_actor_login or None,
                author_association=resolved_association or None,
                authorization_version=authorization_version,
                source_comment_id=resolved_source_comment_id,
                source_comment_url=resolved_source_url or None,
            ),
            intentional_evidence=intentional_evidence,
        )
        postgres_feedback.complete_event(
            connection,
            event_id=resolved_event_id,
            outcome=FeedbackStatus.RECORDED,
        )
        return FeedbackResult(
            status=FeedbackStatus.RECORDED,
            event_id=resolved_event_id,
            decision_id=int(decision.id),
            fingerprint=target.fingerprint,
            local_reference=target.local_reference,
            title=target.title,
            context_hash=decision.context_hash,
            adr_id=decision.adr_id or "",
            expires_at=(
                decision.expires_at.isoformat().replace("+00:00", "Z")
                if decision.expires_at is not None
                else None
            ),
        )
