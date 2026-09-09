"""Administration audit journal, appended in the transaction it describes."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from .team_access import (
    AccessDenied,
    AccessScope,
    IntegrationScope,
    ReadScope,
    require_admin,
    require_team,
)


class AuditAction(StrEnum):
    INTEGRATION_CREATED = "integration_created"
    INTEGRATION_REVOKED = "integration_revoked"
    INTEGRATION_READ = "integration_read"
    AUDIT_ACCESS_STARTED = "audit_access_started"
    AUDIT_ACCESS_ENDED = "audit_access_ended"
    AUDIT_VIEWED = "audit_viewed"
    AUDIT_EXPORTED = "audit_exported"
    TEAM_CREATED = "team_created"
    TEAM_UPDATED = "team_updated"
    MEMBER_ADDED = "member_added"
    MEMBER_UPDATED = "member_updated"
    MEMBER_REMOVED = "member_removed"
    REPOSITORY_ASSIGNED = "repository_assigned"
    REPOSITORY_TRANSFERRED = "repository_transferred"
    REPOSITORY_REMOVED = "repository_removed"
    REPOSITORY_REQUESTED = "repository_requested"
    REQUEST_APPROVED = "request_approved"
    REQUEST_REJECTED = "request_rejected"
    REQUEST_WITHDRAWN = "request_withdrawn"
    ACCOUNT_CREATED = "account_created"
    ACCOUNT_UPDATED = "account_updated"
    PASSWORD_CHANGED = "password_changed"
    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    FINDING_DECIDED = "finding_decided"
    FEEDBACK_TRIAGED = "feedback_triaged"
    RUN_ACTION = "run_action"
    SETTINGS_UPDATED = "settings_updated"
    EMAIL_UPDATED = "email_updated"
    REGISTRATION_UPDATED = "registration_updated"
    ACCESS_UPDATED = "access_updated"
    PROVIDER_LOGIN = "provider_login"
    PROVIDER_LOGOUT = "provider_logout"
    PROVIDER_LOGIN_CANCELLED = "provider_login_cancelled"
    CONNECTION_CREATED = "connection_created"
    CONNECTION_UPDATED = "connection_updated"
    CONNECTION_REMOVED = "connection_removed"
    IDENTITY_LINKED = "identity_linked"
    SCIM_PROVISIONED = "scim_provisioned"
    SCIM_UPDATED = "scim_updated"
    SCIM_DEACTIVATED = "scim_deactivated"


class AuditOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: int
    team_id: int | None
    actor_id: UUID | None
    actor_email: str | None
    actor_role: str
    action: AuditAction
    subject: str
    reason: str
    details: dict[str, str | int | bool | None]
    owner_only: bool
    operation_id: UUID | None
    outcome: AuditOutcome
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class AuditPage:
    items: tuple[AuditEvent, ...]
    next_before_id: int | None


@dataclass(frozen=True, slots=True)
class AuditFilters:
    before_id: int | None = None
    actor_id: UUID | None = None
    action: AuditAction | None = None
    outcome: AuditOutcome | None = None
    search: str = ""
    since: datetime | None = None
    until: datetime | None = None


class AuditPurpose(StrEnum):
    INCIDENT_INVESTIGATION = "incident_investigation"
    ACCESS_REVIEW = "access_review"
    SUPPORT = "support"
    ROUTINE_REVIEW = "routine_review"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class AuditAccess:
    id: UUID
    purpose: AuditPurpose
    reason: str
    team_id: int | None
    expires_at: datetime


def _access(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    access_id: UUID | None,
    team_id: int | None,
) -> AuditAccess:
    require_admin(scope)
    # The immutable start event owns the grant; end events revoke it. Both use
    # the operation index, so access validation never scans the journal.
    with connection.cursor(row_factory=class_row(AuditAccess)) as cursor:
        row = cursor.execute(
            """SELECT event.operation_id AS id, event.details->>'purpose' AS purpose,
                      event.reason, event.team_id,
                      event.recorded_at + interval '30 minutes' AS expires_at
               FROM review_agent.admin_audit_events event
               JOIN review_agent.admin_users account ON account.id = event.actor_id
               WHERE event.operation_id = %s AND event.action = 'audit_access_started'
                 AND event.actor_id = %s AND event.team_id IS NOT DISTINCT FROM %s::bigint
                 AND event.actor_role = %s
                 AND event.details->>'access_revision' = account.access_revision::text
                 AND event.recorded_at > clock_timestamp() - interval '30 minutes'
                 AND NOT EXISTS (
                     SELECT 1 FROM review_agent.admin_audit_events ended
                     WHERE ended.operation_id = event.operation_id AND ended.action = 'audit_access_ended')""",
            (access_id, scope.user_id, team_id, scope.role.value),
        ).fetchone()
    if row is None:
        raise AccessDenied(
            "State a purpose and justification to access this audit log."
        )
    return replace(row, purpose=AuditPurpose(row.purpose))


def start_access(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    purpose: AuditPurpose,
    reason: str,
) -> AuditAccess:
    require_admin(scope)
    reason = reason.strip()
    if not 10 <= len(reason) <= 500:
        raise ValueError("Audit justification must contain 10 to 500 characters.")
    row = connection.execute(
        "SELECT access_revision FROM review_agent.admin_users WHERE id = %s",
        (scope.user_id,),
    ).fetchone()
    if row is None:
        raise AccessDenied()
    access_id = uuid4()
    record(
        connection,
        scope,
        action=AuditAction.AUDIT_ACCESS_STARTED,
        subject="audit",
        reason=reason,
        details={"purpose": purpose.value, "access_revision": row[0]},
        team_id=scope.team_id,
        operation_id=access_id,
        owner_only=scope.is_owner,
    )
    return _access(connection, scope, access_id, scope.team_id)


def end_access(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    access_id: UUID,
) -> None:
    grant = _access(connection, scope, access_id, scope.team_id)
    record(
        connection,
        scope,
        action=AuditAction.AUDIT_ACCESS_ENDED,
        subject="audit",
        reason=grant.reason,
        details={"purpose": grant.purpose.value},
        team_id=scope.team_id,
        operation_id=access_id,
        owner_only=scope.is_owner,
    )


def record(
    connection: psycopg.Connection[TupleRow],
    scope: ReadScope,
    *,
    action: AuditAction,
    subject: str,
    reason: str,
    details: dict[str, str | int | bool | None],
    team_id: int | None = None,
    repository_id: int | None = None,
    owner_only: bool = False,
    operation_id: UUID | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCEEDED,
) -> None:
    actor_id = scope.user_id if isinstance(scope, AccessScope) else None
    if isinstance(scope, IntegrationScope):
        details = {**details, "integration_id": scope.id}
    if repository_id is not None:
        owner = connection.execute(
            "SELECT team_id FROM review_agent.team_repositories WHERE repository_id = %s",
            (repository_id,),
        ).fetchone()
        team_id = owner[0] if owner else None
        details = {**details, "repository_id": repository_id}
    connection.execute(
        """INSERT INTO review_agent.admin_audit_events
            (team_id, actor_id, actor_email, actor_role, action, subject, reason, details, owner_only, operation_id, outcome)
            VALUES (%s, %s, (SELECT email FROM review_agent.admin_users WHERE id = %s), %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            team_id,
            actor_id,
            actor_id,
            scope.actor.split(":", 1)[0],
            action.value,
            subject,
            reason,
            Jsonb(details),
            owner_only,
            operation_id,
            outcome.value,
        ),
    )


def events(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int | None,
    limit: int,
    filters: AuditFilters,
    access_id: UUID | None,
    export_format: str | None = None,
) -> AuditPage:
    require_admin(scope)
    if team_id is not None:
        require_team(connection, scope, team_id)
    grant = _access(connection, scope, access_id, team_id)
    with connection.cursor(row_factory=class_row(AuditEvent)) as cursor:
        rows = cursor.execute(
            """SELECT event.id, team_id, actor_id, actor_email, actor_role, action, subject, reason, details,
                      owner_only, operation_id, outcome, recorded_at
               FROM review_agent.admin_audit_events event
               WHERE (%(team)s::bigint IS NULL OR team_id = %(team)s)
                 AND (%(owner)s OR NOT owner_only)
                 AND (%(before)s::bigint IS NULL OR event.id < %(before)s)
                 AND (%(actor)s::uuid IS NULL OR actor_id = %(actor)s)
                 AND (%(action)s::text IS NULL OR action = %(action)s)
                 AND (%(outcome)s::text IS NULL OR outcome = %(outcome)s)
                 AND (%(search)s = '' OR search_text @@ plainto_tsquery('simple', %(search)s))
                 AND (%(since)s::timestamptz IS NULL OR recorded_at >= %(since)s)
                 AND (%(until)s::timestamptz IS NULL OR recorded_at < %(until)s)
               ORDER BY event.id DESC LIMIT %(limit)s""",
            {
                "team": team_id,
                "owner": scope.is_owner,
                "before": filters.before_id,
                "actor": filters.actor_id,
                "action": filters.action.value if filters.action is not None else None,
                "outcome": filters.outcome.value
                if filters.outcome is not None
                else None,
                "search": filters.search,
                "since": filters.since,
                "until": filters.until,
                "limit": limit + 1,
            },
        ).fetchall()
    page = AuditPage(
        tuple(
            replace(
                row, action=AuditAction(row.action), outcome=AuditOutcome(row.outcome)
            )
            for row in rows[:limit]
        ),
        rows[limit - 1].id if len(rows) > limit else None,
    )
    record(
        connection,
        scope,
        action=AuditAction.AUDIT_EXPORTED
        if export_format
        else AuditAction.AUDIT_VIEWED,
        subject="audit",
        reason=grant.reason,
        team_id=team_id,
        operation_id=grant.id,
        owner_only=scope.is_owner,
        details={
            "purpose": grant.purpose.value,
            "format": export_format,
            "before_id": filters.before_id,
            "actor_id": str(filters.actor_id) if filters.actor_id else None,
            "action": filters.action.value if filters.action else None,
            "outcome": filters.outcome.value if filters.outcome else None,
            "search": filters.search,
            "since": filters.since.isoformat() if filters.since else None,
            "until": filters.until.isoformat() if filters.until else None,
            "limit": limit,
            "returned_count": len(page.items),
            "first_event_id": page.items[0].id if page.items else None,
            "last_event_id": page.items[-1].id if page.items else None,
        },
    )
    return page
