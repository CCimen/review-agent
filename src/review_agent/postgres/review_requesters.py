"""Repository-scoped grants for requesting reviews with a signed GitHub identity."""

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow, class_row

from . import audit, team_access
from .team_access import AccessScope, ResourceNotFound


@dataclass(frozen=True, slots=True)
class ReviewRequester:
    github_user_id: int
    github_login: str
    granted_at: datetime


@dataclass(frozen=True, slots=True)
class RepositoryReviewRequesters:
    repository_id: int
    repository: str
    can_manage: bool
    items: tuple[ReviewRequester, ...]
    next_after_user_id: int | None


def repository(
    connection: psycopg.Connection[TupleRow], scope: AccessScope,
    repository_id: int, *, maintain: bool = False,
) -> tuple[str, int, int | None]:
    row = connection.execute(
        sql.SQL("""SELECT repository.full_name, repository.provider_repository_id,
                          ownership.team_id
                   FROM {} repository
                   LEFT JOIN review_agent.team_repositories ownership
                       ON ownership.repository_id = repository.id
                   WHERE repository.id = %s AND repository.provider = 'github'""").format(
            team_access.repository_source(scope)
        ), (repository_id,),
    ).fetchone()
    if row is None:
        raise ResourceNotFound()
    name, provider_id, team_id = str(row[0]), int(row[1]), row[2]
    team_access.require_repository(connection, scope, name, maintain=maintain)
    return name, provider_id, team_id


def list_requesters(
    connection: psycopg.Connection[TupleRow], scope: AccessScope, *,
    repository_id: int, limit: int, after_user_id: int,
) -> RepositoryReviewRequesters:
    name, _, _ = repository(connection, scope, repository_id)
    with connection.cursor(row_factory=class_row(ReviewRequester)) as cursor:
        rows = cursor.execute(
            """SELECT github_user_id, github_login, granted_at
               FROM review_agent.repository_review_requesters
               WHERE repository_id = %s AND github_user_id > %s
               ORDER BY github_user_id LIMIT %s""",
            (repository_id, after_user_id, limit + 1),
        ).fetchall()
    items = tuple(rows[:limit])
    return RepositoryReviewRequesters(
        repository_id, name, team_access.can_maintain(connection, scope, name),
        items, items[-1].github_user_id if len(rows) > limit else None,
    )


def grant(
    connection: psycopg.Connection[TupleRow], scope: AccessScope, *,
    repository_id: int, github_user_id: int, github_login: str,
) -> ReviewRequester:
    name, _, team_id = repository(connection, scope, repository_id, maintain=True)
    connection.execute("SELECT id FROM review_agent.repositories WHERE id = %s FOR UPDATE", (repository_id,))
    row = connection.execute(
        """INSERT INTO review_agent.repository_review_requesters
               (repository_id, github_user_id, github_login)
           VALUES (%s, %s, %s)
           ON CONFLICT (repository_id, github_user_id) DO NOTHING
           RETURNING github_user_id, github_login, granted_at""",
        (repository_id, github_user_id, github_login),
    ).fetchone()
    if row is not None:
        audit.record(connection, scope, team_id=team_id,
            action=audit.AuditAction.REVIEW_REQUESTER_GRANTED, subject=name,
            reason="Allow a GitHub user to request reviews",
            details={"github_user_id": github_user_id, "github_login": github_login})
    else:
        row = connection.execute(
            """SELECT github_user_id, github_login, granted_at
               FROM review_agent.repository_review_requesters
               WHERE repository_id = %s AND github_user_id = %s""",
            (repository_id, github_user_id),
        ).fetchone()
    assert row is not None
    return ReviewRequester(*row)


def revoke(
    connection: psycopg.Connection[TupleRow], scope: AccessScope, *,
    repository_id: int, github_user_id: int,
) -> None:
    name, _, team_id = repository(connection, scope, repository_id, maintain=True)
    connection.execute("SELECT id FROM review_agent.repositories WHERE id = %s FOR UPDATE", (repository_id,))
    row = connection.execute(
        """DELETE FROM review_agent.repository_review_requesters
           WHERE repository_id = %s AND github_user_id = %s RETURNING github_login""",
        (repository_id, github_user_id),
    ).fetchone()
    if row is not None:
        audit.record(connection, scope, team_id=team_id,
            action=audit.AuditAction.REVIEW_REQUESTER_REVOKED, subject=name,
            reason="Revoke a GitHub user's additional review access",
            details={"github_user_id": github_user_id, "github_login": str(row[0])})


def is_allowed(
    connection: psycopg.Connection[TupleRow], *,
    provider_repository_id: int, github_user_id: int,
) -> bool:
    return connection.execute(
        """SELECT 1 FROM review_agent.repository_review_requesters requester
           JOIN review_agent.repositories repository ON repository.id = requester.repository_id
           WHERE repository.provider = 'github' AND repository.provider_repository_id = %s
             AND requester.github_user_id = %s""",
        (provider_repository_id, github_user_id),
    ).fetchone() is not None
