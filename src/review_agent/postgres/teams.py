"""Team membership, repository ownership, and their durable access history."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import TupleRow

from . import audit
from .audit import AuditAction
from .team_access import (
    AccessScope,
    ResourceNotFound,
    TeamRole,
    require_admin,
    require_team,
)


class TeamConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Team:
    id: int
    name: str
    description: str
    revision: int
    created_at: datetime
    role: TeamRole | None
    member_count: int
    repository_count: int
    pending_requests: int


@dataclass(frozen=True, slots=True)
class TeamPage:
    items: tuple[Team, ...]
    total: int
    next_after_id: int | None


@dataclass(frozen=True, slots=True)
class TeamMember:
    user_id: UUID
    email: str
    role: TeamRole
    active: bool


@dataclass(frozen=True, slots=True)
class TeamMemberPage:
    items: tuple[TeamMember, ...]
    next_offset: int | None


_TEAM_SELECT = """
    SELECT team.id, team.name, team.description, team.revision, team.created_at, member.role,
        (SELECT count(*) FROM review_agent.team_members m WHERE m.team_id = team.id) AS member_count,
        (SELECT count(*) FROM review_agent.team_repositories r WHERE r.team_id = team.id) AS repository_count,
        (SELECT count(*) FROM review_agent.repository_requests q WHERE q.team_id = team.id AND q.status = 'pending') AS pending_requests
    FROM review_agent.teams team
    LEFT JOIN review_agent.team_members member ON member.team_id = team.id AND member.user_id = %(user_id)s
"""


def _team(row: TupleRow) -> Team:
    return Team(
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        TeamRole(row[5]) if row[5] is not None else None,
        row[6],
        row[7],
        row[8],
    )


def list_teams(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    limit: int,
    after_id: int,
    search: str,
) -> TeamPage:
    where = """ WHERE (%(global_read)s OR member.user_id IS NOT NULL)
        AND (%(team_id)s::bigint IS NULL OR team.id = %(team_id)s)
        AND position(lower(%(search)s) IN lower(team.name)) > 0"""
    parameters = {
        "user_id": scope.user_id,
        "global_read": scope.global_read,
        "team_id": scope.team_id,
        "search": search,
        "after_id": after_id,
        "limit": limit + 1,
    }
    count = connection.execute(
        "SELECT count(*) FROM (" + _TEAM_SELECT + where + ") teams", parameters
    ).fetchone()
    assert count is not None
    rows = connection.execute(
        _TEAM_SELECT
        + where
        + " AND team.id > %(after_id)s ORDER BY team.id LIMIT %(limit)s",
        parameters,
    ).fetchall()
    items = tuple(_team(row) for row in rows[:limit])
    return TeamPage(items, count[0], items[-1].id if len(rows) > limit else None)


def get_team(
    connection: psycopg.Connection[TupleRow], scope: AccessScope, team_id: int
) -> Team:
    require_team(connection, scope, team_id)
    row = connection.execute(
        _TEAM_SELECT + " WHERE team.id = %(team_id)s",
        {"user_id": scope.user_id, "team_id": team_id},
    ).fetchone()
    if row is None:
        raise ResourceNotFound()
    return _team(row)


def create_team(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    name: str,
    description: str,
    reason: str,
) -> Team:
    require_admin(scope)
    try:
        row = connection.execute(
            "INSERT INTO review_agent.teams (name, description) VALUES (%s, %s) RETURNING id",
            (name, description),
        ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise TeamConflict("A team with this name already exists") from exc
    assert row is not None
    audit.record(
        connection,
        scope,
        team_id=row[0],
        action=AuditAction.TEAM_CREATED,
        subject=str(row[0]),
        reason=reason,
        details={"name": name},
    )
    return get_team(connection, scope, row[0])


def update_team(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    name: str,
    description: str,
    expected_revision: int,
    reason: str,
) -> Team:
    require_admin(scope)
    current = get_team(connection, scope, team_id)
    if current.revision != expected_revision:
        raise TeamConflict("This team changed. Refresh it before saving")
    if (current.name, current.description) == (name, description):
        return current
    try:
        connection.execute(
            "UPDATE review_agent.teams SET name = %s, description = %s, revision = revision + 1 WHERE id = %s",
            (name, description, team_id),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise TeamConflict("A team with this name already exists") from exc
    audit.record(
        connection,
        scope,
        team_id=team_id,
        action=AuditAction.TEAM_UPDATED,
        subject=str(team_id),
        reason=reason,
        details={
            "previous_name": current.name,
            "name": name,
            "previous_description": current.description,
            "description": description,
        },
    )
    return get_team(connection, scope, team_id)


def members(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    limit: int,
    offset: int,
) -> TeamMemberPage:
    require_team(connection, scope, team_id)
    rows = connection.execute(
        """SELECT account.id, account.email, member.role, account.is_active
           FROM review_agent.team_members member JOIN review_agent.admin_users account ON account.id = member.user_id
           WHERE member.team_id = %s ORDER BY lower(account.email), account.id LIMIT %s OFFSET %s""",
        (team_id, limit + 1, offset),
    ).fetchall()
    return TeamMemberPage(
        tuple(
            TeamMember(row[0], row[1], TeamRole(row[2]), row[3]) for row in rows[:limit]
        ),
        offset + limit if len(rows) > limit else None,
    )


def put_member(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    email: str,
    role: TeamRole,
    reason: str,
) -> TeamMember:
    require_team(connection, scope, team_id, maintain=True)
    account = connection.execute(
        "SELECT id, email FROM review_agent.admin_users WHERE lower(email) = lower(%s) AND is_active",
        (email,),
    ).fetchone()
    if account is None:
        raise ResourceNotFound()
    current = connection.execute(
        "SELECT role FROM review_agent.team_members WHERE team_id = %s AND user_id = %s",
        (team_id, account[0]),
    ).fetchone()
    if current is None or current[0] != role.value:
        connection.execute(
            """INSERT INTO review_agent.team_members (team_id, user_id, role) VALUES (%s, %s, %s)
               ON CONFLICT (team_id, user_id) DO UPDATE SET role = EXCLUDED.role""",
            (team_id, account[0], role.value),
        )
        connection.execute(
            "UPDATE review_agent.teams SET revision = revision + 1 WHERE id = %s",
            (team_id,),
        )
        connection.execute(
            "UPDATE review_agent.admin_users SET access_revision = access_revision + 1 WHERE id = %s",
            (account[0],),
        )
        audit.record(
            connection,
            scope,
            team_id=team_id,
            action=AuditAction.MEMBER_ADDED
            if current is None
            else AuditAction.MEMBER_UPDATED,
            subject=str(account[0]),
            reason=reason,
            details={
                "previous_role": current[0] if current else None,
                "role": role.value,
            },
        )
    return TeamMember(account[0], account[1], role, True)


def remove_member(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    user_id: UUID,
    reason: str,
) -> None:
    require_team(connection, scope, team_id, maintain=True)
    row = connection.execute(
        "DELETE FROM review_agent.team_members WHERE team_id = %s AND user_id = %s RETURNING role",
        (team_id, user_id),
    ).fetchone()
    if row is not None:
        connection.execute(
            "UPDATE review_agent.teams SET revision = revision + 1 WHERE id = %s",
            (team_id,),
        )
        connection.execute(
            "UPDATE review_agent.admin_users SET access_revision = access_revision + 1 WHERE id = %s",
            (user_id,),
        )
        audit.record(
            connection,
            scope,
            team_id=team_id,
            action=AuditAction.MEMBER_REMOVED,
            subject=str(user_id),
            reason=reason,
            details={"previous_role": row[0]},
        )
