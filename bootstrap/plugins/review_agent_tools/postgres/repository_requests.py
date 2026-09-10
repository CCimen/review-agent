"""Repository ownership and the team's explicit onboarding decision lifecycle."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow, class_row

from ..domain.review import RepositoryId
from . import audit, github_app
from .team_access import AccessScope, ResourceNotFound, require_admin, require_team
from .teams import TeamConflict


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class RepositoryRequest:
    id: int
    team_id: int
    team_name: str
    requester_id: UUID
    repository_name: str
    repository_id: int | None
    reason: str
    status: RequestStatus
    submitted_at: datetime
    decided_by: UUID | None
    decided_at: datetime | None
    decision_reason: str | None


@dataclass(frozen=True, slots=True)
class RepositoryRequestPage:
    items: tuple[RepositoryRequest, ...]
    pending: int
    next_before_id: int | None


@dataclass(frozen=True, slots=True)
class TeamRepository:
    repository_id: int
    provider_repository_id: int
    repository: str
    enabled: bool
    access: github_app.RepositoryAccess | None
    profile: str | None
    assigned_at: datetime


@dataclass(frozen=True, slots=True)
class TeamRepositoryPage:
    items: tuple[TeamRepository, ...]
    next_after_id: int | None


_SELECT = """SELECT request.id, request.team_id, team.name AS team_name, requester_id, repository_name,
    repository_id, reason, status, submitted_at, decided_by, decided_at, decision_reason
    FROM review_agent.repository_requests request JOIN review_agent.teams team ON team.id = request.team_id"""


def get_request(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    request_id: int,
    *,
    for_update: bool = False,
) -> RepositoryRequest:
    with connection.cursor(row_factory=class_row(RepositoryRequest)) as cursor:
        row = cursor.execute(
            sql.SQL(_SELECT)
            + sql.SQL(" WHERE request.id = %s")
            + (sql.SQL(" FOR UPDATE OF request") if for_update else sql.SQL("")),
            (request_id,),
        ).fetchone()
    if row is None:
        raise ResourceNotFound()
    require_team(connection, scope, row.team_id)
    return replace(row, status=RequestStatus(row.status))


def list_requests(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    status: RequestStatus | None,
    limit: int,
    before_id: int | None,
) -> RepositoryRequestPage:
    where = """ WHERE (%(all)s OR EXISTS (SELECT 1 FROM review_agent.team_members member WHERE member.team_id = request.team_id AND member.user_id = %(user)s))
        AND (%(team)s::bigint IS NULL OR request.team_id = %(team)s)"""
    parameters = {
        "all": scope.global_read,
        "user": scope.user_id,
        "team": scope.team_id,
        "status": status.value if status else None,
        "before": before_id,
        "limit": limit + 1,
    }
    pending = connection.execute(
        sql.SQL(
            "SELECT count(*) FROM review_agent.repository_requests request"
            + where
            + " AND status = 'pending'"
        ),
        parameters,
    ).fetchone()
    assert pending is not None
    with connection.cursor(row_factory=class_row(RepositoryRequest)) as cursor:
        rows = cursor.execute(
            sql.SQL(
                _SELECT
                + where
                + " AND (%(status)s::text IS NULL OR status = %(status)s) AND (%(before)s::bigint IS NULL OR request.id < %(before)s) ORDER BY request.id DESC LIMIT %(limit)s"
            ),
            parameters,
        ).fetchall()
    return RepositoryRequestPage(
        tuple(replace(row, status=RequestStatus(row.status)) for row in rows[:limit]),
        pending[0],
        rows[limit - 1].id if len(rows) > limit else None,
    )


def submit(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    repository: str,
    reason: str,
) -> RepositoryRequest:
    require_team(connection, scope, team_id, maintain=True)
    existing = connection.execute(
        "SELECT id FROM review_agent.repository_requests WHERE team_id = %s AND lower(repository_name) = lower(%s) AND status = 'pending'",
        (team_id, repository),
    ).fetchone()
    if existing is not None:
        return get_request(connection, scope, existing[0])
    row = connection.execute(
        "INSERT INTO review_agent.repository_requests (team_id, requester_id, repository_name, reason) VALUES (%s, %s, %s, %s) RETURNING id",
        (team_id, scope.user_id, repository, reason),
    ).fetchone()
    assert row is not None
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.REPOSITORY_REQUESTED,
        team_id=team_id,
        subject=str(row[0]),
        reason=reason,
        details={"repository": repository},
    )
    return get_request(connection, scope, row[0])


def finish(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    request_id: int,
    status: RequestStatus,
    reason: str,
    repository_id: int | None = None,
) -> RepositoryRequest:
    current = get_request(connection, scope, request_id, for_update=True)
    if status is RequestStatus.WITHDRAWN:
        require_team(connection, scope, current.team_id, maintain=True)
    else:
        require_admin(scope)
    if status is RequestStatus.PENDING:
        raise TeamConflict("A pending request needs a decision")
    if current.status is status:
        return current
    if current.status is not RequestStatus.PENDING:
        raise TeamConflict("This request already has a different decision")
    connection.execute(
        "UPDATE review_agent.repository_requests SET status = %s, repository_id = %s, decided_by = %s, decided_at = statement_timestamp(), decision_reason = %s WHERE id = %s",
        (status.value, repository_id, scope.user_id, reason, request_id),
    )
    action = {
        RequestStatus.APPROVED: audit.AuditAction.REQUEST_APPROVED,
        RequestStatus.REJECTED: audit.AuditAction.REQUEST_REJECTED,
        RequestStatus.WITHDRAWN: audit.AuditAction.REQUEST_WITHDRAWN,
    }[status]
    audit.record(
        connection,
        scope,
        action=action,
        team_id=current.team_id,
        subject=str(request_id),
        reason=reason,
        details={"repository": current.repository_name, "repository_id": repository_id},
    )
    return get_request(connection, scope, request_id)


def assign(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    repository_id: int,
    team_id: int,
    expected_team_id: int | None,
    reason: str,
) -> None:
    require_admin(scope)
    require_team(connection, scope, team_id)
    repository = connection.execute(
        "SELECT id FROM review_agent.repositories WHERE id = %s", (repository_id,)
    ).fetchone()
    if repository is None:
        raise ResourceNotFound()
    current = connection.execute(
        "SELECT team_id FROM review_agent.team_repositories WHERE repository_id = %s",
        (repository_id,),
    ).fetchone()
    previous_team = current[0] if current else None
    if previous_team == team_id:
        return
    if previous_team != expected_team_id:
        raise TeamConflict("Repository ownership changed. Refresh before assigning it")
    connection.execute(
        "INSERT INTO review_agent.team_repositories (repository_id, team_id, assigned_by) VALUES (%s, %s, %s) ON CONFLICT (repository_id) DO UPDATE SET team_id = EXCLUDED.team_id, assigned_by = EXCLUDED.assigned_by, assigned_at = statement_timestamp()",
        (repository_id, team_id, scope.user_id),
    )
    connection.execute(
        "UPDATE review_agent.admin_users SET access_revision = access_revision + 1 WHERE id IN (SELECT user_id FROM review_agent.team_members WHERE team_id = %s OR team_id = %s)",
        (team_id, previous_team),
    )
    details = {
        "repository_id": repository_id,
        "previous_team_id": previous_team,
        "team_id": team_id,
    }
    action = (
        audit.AuditAction.REPOSITORY_ASSIGNED
        if previous_team is None
        else audit.AuditAction.REPOSITORY_TRANSFERRED
    )
    audit.record(
        connection,
        scope,
        team_id=team_id,
        action=action,
        subject=str(repository_id),
        reason=reason,
        details=details,
    )
    if previous_team is not None:
        audit.record(
            connection,
            scope,
            team_id=previous_team,
            action=action,
            subject=str(repository_id),
            reason=reason,
            details=details,
        )


def repositories(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    limit: int,
    after_id: int,
) -> TeamRepositoryPage:
    require_team(connection, scope, team_id)
    with connection.cursor(row_factory=class_row(TeamRepository)) as cursor:
        rows = cursor.execute(
            """SELECT repository.id AS repository_id, repository.provider_repository_id,
            repository.full_name AS repository, coalesce(access.enabled, false) AS enabled,
            access.access_state AS access, access.profile_key AS profile, ownership.assigned_at
            FROM review_agent.team_repositories ownership
            JOIN review_agent.repositories repository ON repository.id = ownership.repository_id
            LEFT JOIN review_agent.github_app_repository_access access ON access.repository_id = repository.id
            WHERE ownership.team_id = %s AND repository.id > %s ORDER BY repository.id LIMIT %s""",
            (team_id, after_id, limit + 1),
        ).fetchall()
    return TeamRepositoryPage(
        tuple(
            replace(
                row,
                access=github_app.RepositoryAccess(row.access) if row.access else None,
            )
            for row in rows[:limit]
        ),
        rows[limit - 1].repository_id if len(rows) > limit else None,
    )


def remove(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    repository_id: int,
    reason: str,
) -> None:
    require_admin(scope)
    require_team(connection, scope, team_id)
    owner = connection.execute(
        "SELECT team_id FROM review_agent.team_repositories WHERE repository_id = %s",
        (repository_id,),
    ).fetchone()
    if owner is None:
        return
    if owner[0] != team_id:
        raise TeamConflict("Repository ownership changed. Refresh before removing it")
    try:
        current = github_app.get_repository_access(
            connection, RepositoryId(repository_id)
        )
    except github_app.GitHubAppRepositoryNotFound:
        current = None
    if (
        current is not None
        and current.access_state is github_app.RepositoryAccess.AVAILABLE
    ):
        github_app.disable_repository(
            connection,
            repository_id=current.repository_id,
            actor=scope.actor,
            reason=reason,
        )
    connection.execute(
        "DELETE FROM review_agent.team_repositories WHERE repository_id = %s AND team_id = %s",
        (repository_id, team_id),
    )
    connection.execute(
        "UPDATE review_agent.admin_users SET access_revision = access_revision + 1 WHERE id IN (SELECT user_id FROM review_agent.team_members WHERE team_id = %s)",
        (team_id,),
    )
    audit.record(
        connection,
        scope,
        team_id=team_id,
        action=audit.AuditAction.REPOSITORY_REMOVED,
        subject=str(repository_id),
        reason=reason,
        details={"repository_id": repository_id},
    )
