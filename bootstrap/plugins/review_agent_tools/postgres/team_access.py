"""Console authorization resolved against current accounts and team ownership."""

from dataclasses import dataclass, field
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Literal, overload
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow


from ..domain.access import Role, TeamRole
from .runtime import PostgreSQLRuntime


class AccessDenied(PermissionError):
    def __init__(
        self,
        message: str = "This action requires additional access",
        *,
        status: int = 403,
    ) -> None:
        super().__init__(message)
        self.status = status


class ResourceNotFound(LookupError):
    def __init__(self) -> None:
        super().__init__("This resource is unavailable in your current scope")


@dataclass(frozen=True, slots=True)
class AccessRequest:
    """Authenticated identity and requested scope, without caller-granted rights."""

    user_id: UUID
    team_id: int | None = None


IntegrationOperation = Literal[
    "overview", "repositories", "reviews", "review_content", "quality", "openapi"
]


@dataclass(frozen=True, slots=True)
class IntegrationAccessRequest:
    credential_sha256: str = field(repr=False)
    team_id: int | None = None


@dataclass(frozen=True, slots=True)
class IntegrationScope:
    id: int
    global_read: bool
    read_review_content: bool
    team_id: int | None

    @property
    def is_admin(self) -> bool:
        return False

    @property
    def is_owner(self) -> bool:
        return False

    @property
    def actor(self) -> str:
        return f"integration:{self.id}"


@dataclass(frozen=True, slots=True)
class AccessScope:
    user_id: UUID
    role: Role
    team_id: int | None

    @property
    def is_admin(self) -> bool:
        return self.role in (Role.ADMIN, Role.OWNER)

    @property
    def is_owner(self) -> bool:
        return self.role is Role.OWNER

    @property
    def global_read(self) -> bool:
        return self.role is not Role.MEMBER

    @property
    def actor(self) -> str:
        return (
            f"{self.role.value if self.is_admin else 'team-maintainer'}:{self.user_id}"
        )


ReadAccessRequest = AccessRequest | IntegrationAccessRequest
ReadScope = AccessScope | IntegrationScope


@overload
def authorized_transaction(
    runtime: PostgreSQLRuntime,
    access: AccessRequest,
    *,
    write: bool = False,
    access_change: bool = False,
) -> AbstractContextManager[tuple[psycopg.Connection[TupleRow], AccessScope]]: ...


@overload
def authorized_transaction(
    runtime: PostgreSQLRuntime,
    access: ReadAccessRequest,
    *,
    write: bool = False,
    access_change: bool = False,
) -> AbstractContextManager[tuple[psycopg.Connection[TupleRow], ReadScope]]: ...


@contextmanager
def authorized_transaction(
    runtime: PostgreSQLRuntime,
    access: ReadAccessRequest,
    *,
    write: bool = False,
    access_change: bool = False,
) -> Iterator[tuple[psycopg.Connection[TupleRow], ReadScope]]:
    """Resolve current authorization in the transaction containing the operation."""
    with runtime.transaction() as connection:
        if isinstance(access, IntegrationAccessRequest):
            if write or access_change:
                raise AccessDenied("Integration credentials allow report reads only")
            # Authorization and data use one snapshot. The only write is the
            # access receipt; in-flight reads may finish after revocation.
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            integration = resolve_integration_scope(connection, access)
            yield connection, integration
            return
        if access_change:
            lock_access_change(connection)
        elif not write:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        scope = resolve_scope(connection, access, write=write and not access_change)
        yield connection, scope


def lock_access_change(connection: psycopg.Connection[TupleRow]) -> None:
    # Match admin_auth's account mutation lock. Membership and ownership changes
    # must also serialize with console writes authorized under the previous state.
    connection.execute(
        "LOCK TABLE review_agent.admin_users IN SHARE ROW EXCLUSIVE MODE"
    )


def resolve_scope(
    connection: psycopg.Connection[TupleRow],
    request: AccessRequest,
    *,
    write: bool = False,
) -> AccessScope:
    if write:
        # Concurrent authorized writes can share this lock. Revocation waits for
        # their bounded, network-free transactions before taking effect.
        connection.execute("LOCK TABLE review_agent.admin_users IN SHARE MODE")
    row = connection.execute(
        "SELECT is_superuser, is_global_viewer, is_platform_owner FROM review_agent.admin_users WHERE id = %s AND is_active",
        (request.user_id,),
    ).fetchone()
    if row is None:
        raise AccessDenied("Sign in to continue", status=401)
    role = (
        Role.OWNER
        if row[2]
        else Role.ADMIN
        if row[0]
        else Role.VIEWER
        if row[1]
        else Role.MEMBER
    )
    scope = AccessScope(request.user_id, role, request.team_id)
    if request.team_id is not None:
        require_team(connection, scope, request.team_id)
    return scope


def resolve_integration_scope(
    connection: psycopg.Connection[TupleRow], request: IntegrationAccessRequest
) -> IntegrationScope:
    row = connection.execute(
        """SELECT id, deployment_wide, read_review_content
           FROM review_agent.integrations
           WHERE credential_sha256 = %s AND revoked_at IS NULL
             AND expires_at > statement_timestamp()""",
        (request.credential_sha256,),
    ).fetchone()
    if row is None:
        raise AccessDenied("Integration credential is invalid or expired", status=401)
    scope = IntegrationScope(row[0], row[1], row[2], request.team_id)
    if request.team_id is not None:
        require_team(connection, scope, request.team_id)
    return scope


def require_admin(scope: ReadScope) -> None:
    if not scope.is_admin:
        raise AccessDenied("Platform administrator access required")


def require_owner(scope: ReadScope) -> None:
    if not scope.is_owner:
        raise AccessDenied("Platform owner access required")


def require_team(
    connection: psycopg.Connection[TupleRow],
    scope: ReadScope,
    team_id: int,
    *,
    maintain: bool = False,
) -> TeamRole | None:
    if isinstance(scope, IntegrationScope):
        if maintain:
            raise AccessDenied("Integration credentials allow report reads only")
        row = connection.execute(
            """SELECT 1 FROM review_agent.teams team WHERE team.id = %s
               AND (%s OR EXISTS (SELECT 1 FROM review_agent.integration_teams permission
                   WHERE permission.integration_id = %s AND permission.team_id = team.id))""",
            (team_id, scope.global_read, scope.id),
        ).fetchone()
        if row is None:
            raise ResourceNotFound()
        return None
    row = connection.execute(
        """SELECT member.role FROM review_agent.teams team
           LEFT JOIN review_agent.team_members member ON member.team_id = team.id AND member.user_id = %s
           WHERE team.id = %s""",
        (scope.user_id, team_id),
    ).fetchone()
    if row is None or (not scope.global_read and row[0] is None):
        raise ResourceNotFound()
    role = TeamRole(row[0]) if row[0] is not None else None
    if maintain and not scope.is_admin and role is not TeamRole.MAINTAINER:
        raise AccessDenied("Team maintainer access required")
    return role


def repository_source(scope: ReadScope | None) -> sql.Composable:
    """A set-oriented repository relation; None is reserved for local operator callers."""
    if scope is None or (scope.global_read and scope.team_id is None):
        return sql.Identifier("review_agent", "repositories")
    if scope.team_id is not None:
        condition = sql.SQL("ownership.team_id = {}").format(sql.Literal(scope.team_id))
    elif isinstance(scope, IntegrationScope):
        condition = sql.SQL(
            "EXISTS (SELECT 1 FROM review_agent.integration_teams permission WHERE permission.team_id = ownership.team_id AND permission.integration_id = {})"
        ).format(sql.Literal(scope.id))
    else:
        condition = sql.SQL(
            "EXISTS (SELECT 1 FROM review_agent.team_members member WHERE member.team_id = ownership.team_id AND member.user_id = {})"
        ).format(sql.Literal(scope.user_id))
    return sql.SQL("""(SELECT repository.* FROM review_agent.repositories repository
        JOIN review_agent.team_repositories ownership ON ownership.repository_id = repository.id
        WHERE {})""").format(condition)


def run_source(scope: ReadScope | None) -> sql.Composable:
    if scope is None or (scope.global_read and scope.team_id is None):
        return sql.Identifier("review_agent", "review_runs")
    return sql.SQL("""(SELECT scoped_run.* FROM review_agent.review_runs scoped_run
        JOIN review_agent.pull_requests scoped_pr ON scoped_pr.id = scoped_run.pull_request_id
        JOIN {} scoped_repo ON scoped_repo.id = scoped_pr.repository_id)""").format(
        repository_source(scope)
    )


def maintainer_predicate(
    scope: ReadScope | None, repository_id: sql.Composable
) -> sql.Composable:
    if scope is None or scope.is_admin:
        return sql.SQL("true")
    if isinstance(scope, IntegrationScope):
        return sql.SQL("false")
    return sql.SQL("""EXISTS (SELECT 1 FROM review_agent.team_repositories owned
        JOIN review_agent.team_members maintainer ON maintainer.team_id = owned.team_id
        WHERE owned.repository_id = {repository_id} AND maintainer.user_id = {user_id}
          AND maintainer.role = 'maintainer')""").format(
        repository_id=repository_id, user_id=sql.Literal(scope.user_id)
    )


def can_maintain(
    connection: psycopg.Connection[TupleRow], scope: ReadScope, repository: str
) -> bool:
    row = connection.execute(
        sql.SQL(
            "SELECT {predicate} FROM {repositories} repository WHERE lower(repository.full_name) = lower(%s)"
        ).format(
            predicate=maintainer_predicate(scope, sql.SQL("repository.id")),
            repositories=repository_source(scope),
        ),
        (repository,),
    ).fetchone()
    return bool(row and row[0])


def _require_repository_row(
    connection: psycopg.Connection[TupleRow],
    scope: ReadScope,
    row: TupleRow | None,
    *,
    maintain: bool,
) -> int:
    if row is None:
        raise ResourceNotFound()
    repository_id = int(row[0])
    if maintain and not scope.is_admin:
        owner = connection.execute(
            "SELECT team_id FROM review_agent.team_repositories WHERE repository_id = %s",
            (repository_id,),
        ).fetchone()
        if owner is None:
            raise AccessDenied("Team maintainer access required")
        require_team(connection, scope, int(owner[0]), maintain=True)
    return repository_id


def require_repository(
    connection: psycopg.Connection[TupleRow],
    scope: ReadScope,
    repository: str,
    *,
    maintain: bool = False,
) -> int:
    row = connection.execute(
        sql.SQL(
            "SELECT id FROM {} repository WHERE lower(full_name) = lower(%s)"
        ).format(repository_source(scope)),
        (repository,),
    ).fetchone()
    return _require_repository_row(connection, scope, row, maintain=maintain)


def require_run(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    run_id: int,
    *,
    maintain: bool = False,
) -> int:
    row = connection.execute(
        sql.SQL("""SELECT repository.id FROM review_agent.review_runs run
            JOIN review_agent.pull_requests pr ON pr.id = run.pull_request_id
            JOIN {} repository ON repository.id = pr.repository_id WHERE run.id = %s""").format(
            repository_source(scope)
        ),
        (run_id,),
    ).fetchone()
    return _require_repository_row(connection, scope, row, maintain=maintain)


def require_feedback(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    feedback_id: int,
    *,
    maintain: bool = False,
) -> int:
    row = connection.execute(
        sql.SQL("""SELECT repository.id FROM review_agent.review_quality_feedback feedback
            JOIN review_agent.pull_requests pr ON pr.id = feedback.pull_request_id
            JOIN {} repository ON repository.id = pr.repository_id WHERE feedback.id = %s""").format(
            repository_source(scope)
        ),
        (feedback_id,),
    ).fetchone()
    return _require_repository_row(connection, scope, row, maintain=maintain)
