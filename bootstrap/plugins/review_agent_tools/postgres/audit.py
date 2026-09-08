"""Administration audit journal, appended in the transaction it describes."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import psycopg
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from .team_access import AccessScope, require_admin, require_team


class AuditAction(StrEnum):
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


def record(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
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
    if repository_id is not None:
        owner = connection.execute(
            "SELECT team_id FROM review_agent.team_repositories WHERE repository_id = %s",
            (repository_id,),
        ).fetchone()
        team_id = owner[0] if owner else None
        details = {**details, "repository_id": repository_id}
    connection.execute(
        """INSERT INTO review_agent.admin_audit_events
            (team_id, actor_id, actor_role, action, subject, reason, details, owner_only, operation_id, outcome)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            team_id,
            scope.user_id,
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
    before_id: int | None,
    actor_id: UUID | None = None,
    action: AuditAction | None = None,
) -> AuditPage:
    require_admin(scope)
    if team_id is not None:
        require_team(connection, scope, team_id)
    with connection.cursor(row_factory=class_row(AuditEvent)) as cursor:
        rows = cursor.execute(
            """SELECT event.id, team_id, actor_id, account.email AS actor_email, actor_role, action, subject, reason, details,
                      owner_only, operation_id, outcome, recorded_at
               FROM review_agent.admin_audit_events event
               LEFT JOIN review_agent.admin_users account ON account.id = event.actor_id
               WHERE (%(team)s::bigint IS NULL OR team_id = %(team)s)
                 AND (%(owner)s OR NOT owner_only)
                 AND (%(before)s::bigint IS NULL OR event.id < %(before)s)
                 AND (%(actor)s::uuid IS NULL OR actor_id = %(actor)s)
                 AND (%(action)s::text IS NULL OR action = %(action)s)
               ORDER BY event.id DESC LIMIT %(limit)s""",
            {
                "team": team_id,
                "owner": scope.is_owner,
                "before": before_id,
                "actor": actor_id,
                "action": action.value if action is not None else None,
                "limit": limit + 1,
            },
        ).fetchall()
    return AuditPage(
        tuple(
            replace(
                row, action=AuditAction(row.action), outcome=AuditOutcome(row.outcome)
            )
            for row in rows[:limit]
        ),
        rows[limit - 1].id if len(rows) > limit else None,
    )
