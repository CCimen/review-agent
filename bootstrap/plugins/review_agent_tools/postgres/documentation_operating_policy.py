"""Shared documentation mode resolution and scoped console policy changes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow, class_row

from ..deployment_settings import DeploymentSettings
from ..domain.documentation_operating_policy import (
    DocumentationMode,
    ResolvedDocumentationMode,
    resolve_mode,
)
from . import audit, deployment_settings, team_access
from . import documentation_configuration
from ..documentation_configuration import DocumentationConfiguration
from .team_access import AccessScope, ResourceNotFound
from .teams import TeamConflict


@dataclass(frozen=True, slots=True)
class RepositoryDocumentationPolicy:
    repository_id: int
    repository: str
    team_id: int | None
    team_name: str | None
    repository_override: DocumentationMode | None
    team_default: DocumentationMode | None
    resolved: ResolvedDocumentationMode
    revision: str
    repository_revision: int
    team_revision: int | None
    deployment_revision: int
    capability: Literal["not_checked", "ready", "missing_checks", "access_unavailable"] = "not_checked"
    configuration: Literal["not_checked", "valid", "not_configured", "invalid", "unavailable"] = "not_checked"
    configuration_detail: DocumentationConfiguration | None = None
    can_manage: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryModeImpact:
    repository_id: int
    repository: str
    repository_override: DocumentationMode | None
    before: ResolvedDocumentationMode
    after: ResolvedDocumentationMode


@dataclass(frozen=True, slots=True)
class TeamDocumentationPolicy:
    team_id: int
    default_mode: DocumentationMode
    proposed_mode: DocumentationMode
    revision: str
    deployment_enabled: bool
    inherited_count: int
    exception_count: int
    repositories: tuple[RepositoryModeImpact, ...]
    next_after_id: int | None
    can_manage: bool


@dataclass(frozen=True, slots=True)
class OwnershipDocumentationPreview:
    repository_id: int
    previous_team_id: int | None
    destination_team_id: int | None
    destination_team_name: str | None
    before: ResolvedDocumentationMode
    after: ResolvedDocumentationMode
    previous_connection_id: int
    destination_connection_id: int
    account_policy_changed: bool
    revision: str


@dataclass(frozen=True, slots=True)
class _Repository:
    repository_id: int
    repository: str
    repository_override: str | None
    repository_revision: int
    team_id: int | None
    team_name: str | None
    team_default: str | None
    team_revision: int | None


def deployment(connection: psycopg.Connection[TupleRow]) -> tuple[bool, int]:
    """The deployment switch and its revision, for callers resolving many
    repositories at once."""
    return _deployment(connection)


def _deployment(connection: psycopg.Connection[TupleRow]) -> tuple[bool, int]:
    current = deployment_settings.latest(connection)
    settings = (
        current.settings
        if current is not None
        else DeploymentSettings.from_environment()
    )
    return (
        settings.documentation_review_enabled,
        current.id if current is not None else 0,
    )


def _revision(*values: object) -> str:
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":")).encode()
    ).hexdigest()


def _repository(
    connection: psycopg.Connection[TupleRow], repository_id: int
) -> _Repository:
    with connection.cursor(row_factory=class_row(_Repository)) as cursor:
        row = cursor.execute(
            """SELECT repository.id AS repository_id, repository.full_name AS repository,
                      repository.documentation_mode AS repository_override,
                      repository.documentation_revision AS repository_revision,
                      ownership.team_id, team.name AS team_name,
                      team.documentation_mode AS team_default,
                      team.documentation_revision AS team_revision
               FROM review_agent.repositories repository
               LEFT JOIN review_agent.team_repositories ownership ON ownership.repository_id = repository.id
               LEFT JOIN review_agent.teams team ON team.id = ownership.team_id
               WHERE repository.id = %s""",
            (repository_id,),
        ).fetchone()
    if row is None:
        raise ResourceNotFound()
    return row


def resolve(
    connection: psycopg.Connection[TupleRow], *, repository_id: int
) -> RepositoryDocumentationPolicy:
    """Runtime and console consumers share current mode and revision precedence."""
    deployment_settings.lock(connection, shared=True)
    row = _repository(connection, repository_id)
    enabled, deployment_revision = _deployment(connection)
    override = (
        DocumentationMode(row.repository_override)
        if row.repository_override is not None
        else None
    )
    team_default = (
        DocumentationMode(row.team_default) if row.team_default is not None else None
    )
    return RepositoryDocumentationPolicy(
        repository_id=row.repository_id,
        repository=row.repository,
        team_id=row.team_id,
        team_name=row.team_name,
        repository_override=override,
        team_default=team_default,
        resolved=resolve_mode(
            deployment_enabled=enabled,
            team_default=team_default,
            repository_override=override,
        ),
        revision=_revision(
            row.repository_id,
            row.repository_revision,
            row.team_id,
            row.team_revision,
            deployment_revision,
            enabled,
        ),
        repository_revision=row.repository_revision,
        team_revision=row.team_revision,
        deployment_revision=deployment_revision,
    )


def repository_policy(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    repository_id: int,
    maintain: bool = False,
) -> RepositoryDocumentationPolicy:
    from dataclasses import replace

    row = connection.execute(
        sql.SQL("SELECT full_name FROM {} repository WHERE id = %s").format(
            team_access.repository_source(scope)
        ),
        (repository_id,),
    ).fetchone()
    if row is None:
        raise ResourceNotFound()
    team_access.require_repository(connection, scope, str(row[0]), maintain=maintain)
    snapshot = documentation_configuration.latest(connection, repository_id)
    return replace(
        resolve(connection, repository_id=repository_id),
        can_manage=team_access.can_maintain(connection, scope, str(row[0])),
        configuration=snapshot.state if snapshot is not None else "not_checked",
        configuration_detail=snapshot,
    )


def team_policy(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    proposed_mode: DocumentationMode | None = None,
    after_id: int = 0,
    limit: int = 50,
) -> TeamDocumentationPolicy:
    role = team_access.require_team(connection, scope, team_id)
    if not 1 <= limit <= 100 or after_id < 0:
        raise ValueError("Documentation impact pagination is out of range")
    row = connection.execute(
        "SELECT documentation_mode, documentation_revision FROM review_agent.teams WHERE id = %s",
        (team_id,),
    ).fetchone()
    assert row is not None
    default = DocumentationMode(row[0])
    proposed = proposed_mode if proposed_mode is not None else default
    enabled, deployment_revision = _deployment(connection)
    counts = connection.execute(
        """SELECT count(*) FILTER (WHERE repository.documentation_mode IS NULL),
                  count(*) FILTER (WHERE repository.documentation_mode IS NOT NULL)
           FROM review_agent.team_repositories ownership
           JOIN review_agent.repositories repository ON repository.id = ownership.repository_id
           WHERE ownership.team_id = %s""",
        (team_id,),
    ).fetchone()
    assert counts is not None
    rows = connection.execute(
        """SELECT repository.id, repository.full_name, repository.documentation_mode
           FROM review_agent.team_repositories ownership
           JOIN review_agent.repositories repository ON repository.id = ownership.repository_id
           WHERE ownership.team_id = %s AND repository.id > %s
           ORDER BY repository.id LIMIT %s""",
        (team_id, after_id, limit + 1),
    ).fetchall()
    impacts: list[RepositoryModeImpact] = []
    for item in rows[:limit]:
        override = DocumentationMode(item[2]) if item[2] is not None else None
        impacts.append(
            RepositoryModeImpact(
                item[0],
                item[1],
                override,
                resolve_mode(
                    deployment_enabled=enabled,
                    team_default=default,
                    repository_override=override,
                ),
                resolve_mode(
                    deployment_enabled=enabled,
                    team_default=proposed,
                    repository_override=override,
                ),
            )
        )
    return TeamDocumentationPolicy(
        team_id=team_id,
        default_mode=default,
        proposed_mode=proposed,
        revision=_revision(
            team_id, row[1], proposed.value, deployment_revision, enabled
        ),
        deployment_enabled=enabled,
        inherited_count=counts[0],
        exception_count=counts[1],
        repositories=tuple(impacts),
        next_after_id=rows[limit - 1][0] if len(rows) > limit else None,
        can_manage=scope.is_admin or role is team_access.TeamRole.MAINTAINER,
    )


def save_team_policy(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    mode: DocumentationMode,
    expected_revision: str,
) -> TeamDocumentationPolicy:
    team_access.require_team(connection, scope, team_id, maintain=True)
    deployment_settings.lock(connection)
    connection.execute(
        "SELECT id FROM review_agent.teams WHERE id = %s FOR UPDATE", (team_id,)
    )
    before = team_policy(connection, scope, team_id=team_id, proposed_mode=mode)
    if before.revision != expected_revision:
        raise TeamConflict(
            "Documentation policy or affected repositories changed. Refresh the preview before saving."
        )
    if before.default_mode is mode:
        return before
    connection.execute(
        "UPDATE review_agent.teams SET documentation_mode = %s, documentation_revision = documentation_revision + 1 WHERE id = %s",
        (mode.value, team_id),
    )
    audit.record(
        connection,
        scope,
        team_id=team_id,
        action=audit.AuditAction.SETTINGS_UPDATED,
        subject=f"team-documentation:{team_id}",
        reason="Changed the documentation review default",
        details={
            "previous_mode": before.default_mode.value,
            "mode": mode.value,
            "inherited_repositories": before.inherited_count,
            "repository_exceptions": before.exception_count,
            "source": "team",
        },
    )
    from . import documentation_admissions
    affected = connection.execute("""SELECT repository_id FROM review_agent.team_repositories WHERE team_id = %s""", (team_id,)).fetchall()
    for (repository_id,) in affected:
        current = resolve(connection, repository_id=repository_id)
        documentation_admissions.cancel_ineligible(connection, repository_id=repository_id, effective_mode=current.resolved.effective_mode)
    return team_policy(connection, scope, team_id=team_id)


def save_repository_policy(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    repository_id: int,
    mode: DocumentationMode | None,
    expected_revision: str,
) -> RepositoryDocumentationPolicy:
    deployment_settings.lock(connection)
    repository_policy(connection, scope, repository_id=repository_id, maintain=True)
    owner = _repository(connection, repository_id)
    if owner.team_id is not None:
        connection.execute(
            "SELECT id FROM review_agent.teams WHERE id = %s FOR UPDATE",
            (owner.team_id,),
        )
    connection.execute(
        "SELECT id FROM review_agent.repositories WHERE id = %s FOR UPDATE",
        (repository_id,),
    )
    before = repository_policy(
        connection, scope, repository_id=repository_id, maintain=True
    )
    if before.revision != expected_revision:
        raise TeamConflict(
            "Documentation policy or repository ownership changed. Refresh before saving."
        )
    if before.repository_override is mode:
        return before
    connection.execute(
        "UPDATE review_agent.repositories SET documentation_mode = %s, documentation_revision = documentation_revision + 1 WHERE id = %s",
        (mode.value if mode is not None else None, repository_id),
    )
    if before.team_id is not None:
        connection.execute(
            "UPDATE review_agent.teams SET documentation_revision = documentation_revision + 1 WHERE id = %s",
            (before.team_id,),
        )
    after = repository_policy(connection, scope, repository_id=repository_id)
    audit.record(
        connection,
        scope,
        team_id=after.team_id,
        action=audit.AuditAction.SETTINGS_UPDATED,
        subject=f"repository-documentation:{repository_id}",
        reason="Changed documentation review mode",
        details={
            "repository_id": repository_id,
            "previous_override": before.repository_override.value
            if before.repository_override is not None
            else None,
            "override": mode.value if mode is not None else None,
            "previous_mode": before.resolved.configured_mode.value,
            "mode": after.resolved.configured_mode.value,
            "previous_source": before.resolved.source,
            "source": after.resolved.source,
        },
    )
    from . import documentation_admissions
    documentation_admissions.cancel_ineligible(connection, repository_id=repository_id, effective_mode=after.resolved.effective_mode)
    return after


def ownership_preview(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    repository_id: int,
    destination_team_id: int | None,
) -> OwnershipDocumentationPreview:
    team_access.require_admin(scope)
    before = repository_policy(connection, scope, repository_id=repository_id)
    target_default: DocumentationMode | None = None
    target_name: str | None = None
    target_revision: int | None = None
    if destination_team_id is not None:
        team_access.require_team(connection, scope, destination_team_id)
        target = connection.execute(
            "SELECT name, documentation_mode, documentation_revision FROM review_agent.teams WHERE id = %s",
            (destination_team_id,),
        ).fetchone()
        assert target is not None
        target_name, target_default, target_revision = (
            target[0],
            DocumentationMode(target[1]),
            target[2],
        )
    accounts = connection.execute(
        "SELECT team_id, connection_id, provider, model, reasoning_effort, max_concurrency, revision FROM review_agent.team_model_policies WHERE team_id = %s OR team_id = %s ORDER BY team_id",
        (before.team_id, destination_team_id),
    ).fetchall()
    connections = {
        int(row[0]): int(row[1]) if row[1] is not None else 1 for row in accounts
    }
    routes = {int(row[0]): (connections[int(row[0])], *row[2:6]) for row in accounts}
    default_route = (1, None, None, None, None)
    return OwnershipDocumentationPreview(
        repository_id=repository_id,
        previous_team_id=before.team_id,
        destination_team_id=destination_team_id,
        destination_team_name=target_name,
        before=before.resolved,
        after=resolve_mode(
            deployment_enabled=before.resolved.deployment_enabled,
            team_default=target_default,
            repository_override=before.repository_override,
        ),
        previous_connection_id=connections.get(before.team_id, 1)
        if before.team_id is not None
        else 1,
        destination_connection_id=connections.get(destination_team_id, 1)
        if destination_team_id is not None
        else 1,
        account_policy_changed=(
            routes.get(before.team_id, default_route)
            if before.team_id is not None
            else default_route
        )
        != (
            routes.get(destination_team_id, default_route)
            if destination_team_id is not None
            else default_route
        ),
        revision=_revision(
            before.revision, destination_team_id, target_revision, accounts
        ),
    )


def ownership_changed(
    connection: psycopg.Connection[TupleRow],
    *,
    repository_id: int,
    previous_team_id: int | None,
    destination_team_id: int | None,
) -> None:
    connection.execute(
        "UPDATE review_agent.repositories SET documentation_revision = documentation_revision + 1 WHERE id = %s",
        (repository_id,),
    )
    connection.execute(
        "UPDATE review_agent.teams SET documentation_revision = documentation_revision + 1 WHERE id = %s OR id = %s",
        (previous_team_id, destination_team_id),
    )
