"""Expiring application credentials and immutable grants for report reads."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from secrets import token_urlsafe
from typing import Literal

import psycopg
from psycopg.rows import TupleRow

from . import audit
from .team_access import AccessScope, ResourceNotFound, require_admin


@dataclass(frozen=True, slots=True)
class IntegrationTeam:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Integration:
    id: int
    name: str
    deployment_wide: bool
    read_review_content: bool
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    teams: tuple[IntegrationTeam, ...]
    state: Literal["active", "expired", "revoked"]


@dataclass(frozen=True, slots=True)
class IntegrationPage:
    items: tuple[Integration, ...]
    next_after_id: int | None


@dataclass(frozen=True, slots=True)
class IssuedIntegration:
    integration: Integration
    token: str = field(repr=False)


_SELECT = """SELECT id, name, deployment_wide, read_review_content,
    created_at, expires_at, revoked_at,
    CASE WHEN revoked_at IS NOT NULL THEN 'revoked'
         WHEN expires_at <= statement_timestamp() THEN 'expired' ELSE 'active' END
    FROM review_agent.integrations"""


def _views(
    connection: psycopg.Connection[TupleRow], rows: list[TupleRow]
) -> tuple[Integration, ...]:
    grants: dict[int, list[IntegrationTeam]] = {}
    for integration_id, team_id, name in connection.execute(
        """SELECT permission.integration_id, team.id, team.name
           FROM review_agent.integration_teams permission
           JOIN review_agent.teams team ON team.id = permission.team_id
           WHERE permission.integration_id = ANY(%s) ORDER BY team.id""",
        ([row[0] for row in rows],),
    ).fetchall():
        grants.setdefault(integration_id, []).append(IntegrationTeam(team_id, name))
    return tuple(
        Integration(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            tuple(grants.get(row[0], ())),
            row[7],
        )
        for row in rows
    )


def _get(connection: psycopg.Connection[TupleRow], integration_id: int) -> Integration:
    rows = connection.execute(_SELECT + " WHERE id = %s", (integration_id,)).fetchall()
    if not rows:
        raise ResourceNotFound()
    return _views(connection, rows)[0]


def list_integrations(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    after_id: int,
    limit: int,
) -> IntegrationPage:
    require_admin(scope)
    if not 1 <= limit <= 100 or after_id < 0:
        raise ValueError("Integration page exceeds its bounds")
    rows = connection.execute(
        _SELECT + " WHERE id > %s ORDER BY id LIMIT %s", (after_id, limit + 1)
    ).fetchall()
    items = _views(connection, rows[:limit])
    return IntegrationPage(items, items[-1].id if len(rows) > limit else None)


def create(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    name: str,
    team_ids: tuple[int, ...],
    deployment_wide: bool,
    read_review_content: bool,
    expires_at: datetime,
    reason: str,
) -> IssuedIntegration:
    require_admin(scope)
    name, reason = name.strip(), reason.strip()
    if not 1 <= len(name) <= 80 or not 1 <= len(reason) <= 500:
        raise ValueError("Provide an application name and reason within their bounds")
    if (
        type(deployment_wide) is not bool
        or type(read_review_content) is not bool
        or len(team_ids) > 100
        or len(set(team_ids)) != len(team_ids)
        or any(
            type(team_id) is not int or not 1 <= team_id <= 9223372036854775807
            for team_id in team_ids
        )
        or deployment_wide == bool(team_ids)
    ):
        raise ValueError("Choose deployment-wide access or between one and 100 teams")
    if expires_at.utcoffset() is None or expires_at <= datetime.now(timezone.utc):
        raise ValueError("Provide a future expiry with a timezone offset")
    # Validate all grants as one set; no per-team queries or partial creation.
    found = connection.execute(
        "SELECT count(*) FROM review_agent.teams WHERE id = ANY(%s)", (list(team_ids),)
    ).fetchone()
    assert found is not None
    if found[0] != len(team_ids):
        raise ResourceNotFound()
    token = "ra1_" + token_urlsafe(32)
    row = connection.execute(
        """INSERT INTO review_agent.integrations
           (name, credential_sha256, deployment_wide, read_review_content, created_by, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            name,
            sha256(token.encode()).hexdigest(),
            deployment_wide,
            read_review_content,
            scope.user_id,
            expires_at,
        ),
    ).fetchone()
    assert row is not None
    integration_id = int(row[0])
    connection.execute(
        "INSERT INTO review_agent.integration_teams SELECT %s, unnest(%s::bigint[])",
        (integration_id, list(team_ids)),
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.INTEGRATION_CREATED,
        subject=f"integration:{integration_id}",
        reason=reason,
        details={
            "name": name,
            "deployment_wide": deployment_wide,
            "team_ids": ",".join(str(team_id) for team_id in team_ids),
            "read_review_content": read_review_content,
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        },
    )
    return IssuedIntegration(_get(connection, integration_id), token)


def revoke(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    integration_id: int,
    reason: str,
) -> Integration:
    require_admin(scope)
    reason = reason.strip()
    if not 1 <= len(reason) <= 500:
        raise ValueError("Provide a reason of up to 500 characters")
    row = connection.execute(
        """UPDATE review_agent.integrations SET revoked_at = statement_timestamp()
           WHERE id = %s AND revoked_at IS NULL RETURNING id""",
        (integration_id,),
    ).fetchone()
    result = _get(connection, integration_id)
    if row is not None:
        audit.record(
            connection,
            scope,
            action=audit.AuditAction.INTEGRATION_REVOKED,
            subject=f"integration:{integration_id}",
            reason=reason,
            details={"name": result.name},
        )
    return result
