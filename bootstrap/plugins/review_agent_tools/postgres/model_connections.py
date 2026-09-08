"""Managed model connections, account revisions, and admission-time routing."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import os
from typing import cast
from uuid import UUID

import psycopg
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from .. import review_contract
from ..review_contract import SHARED_CONNECTION_ID
from ..deployment_settings import DeploymentSettings
from ..model_accounts import AccountAvailability, ManagedRuntimeStatus, ModelProvider
from . import audit, deployment_settings, team_access
from .team_access import AccessScope


@dataclass(frozen=True, slots=True)
class ModelChoice:
    provider: ModelProvider
    model: str
    reasoning_efforts: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.model.strip()
            or len(self.model) > 200
            or not self.model.isprintable()
        ):
            raise ValueError("Model must be printable and at most 200 characters")
        if (
            not self.reasoning_efforts
            or len(self.reasoning_efforts) > 8
            or len(set(self.reasoning_efforts)) != len(self.reasoning_efforts)
            or any(
                effort not in review_contract.REASONING_EFFORTS
                for effort in self.reasoning_efforts
            )
        ):
            raise ValueError("Model reasoning choices are invalid")

    def to_json(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "model": self.model,
            "reasoning_efforts": list(self.reasoning_efforts),
        }


def _choices(raw: object) -> tuple[ModelChoice, ...]:
    if not isinstance(raw, list):
        raise ValueError("Model choices must be an array")
    values = cast(list[object], raw)
    if len(values) > 50:
        raise ValueError("At most 50 model choices are supported")
    result: list[ModelChoice] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Invalid model choice")
        row = cast(dict[str, object], value)
        efforts = row.get("reasoning_efforts")
        model = row.get("model")
        if (
            set(row) != {"provider", "model", "reasoning_efforts"}
            or not isinstance(efforts, list)
            or not isinstance(model, str)
        ):
            raise ValueError("Invalid model choice")
        if any(not isinstance(effort, str) for effort in cast(list[object], efforts)):
            raise ValueError("Invalid reasoning choice")
        result.append(
            ModelChoice(
                ModelProvider(row["provider"]), model, tuple(cast(list[str], efforts))
            )
        )
    if len({(choice.provider, choice.model) for choice in result}) != len(result):
        raise ValueError("Model choices must be unique")
    return tuple(result)


class ConnectionState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    AUTHENTICATING = "authenticating"
    NEEDS_ATTENTION = "needs_attention"
    RETIRED = "retired"


class ConnectionConflict(ValueError):
    """The connection changed or has work that prevents this operation."""


class ModelPolicyUnavailable(ValueError):
    """Admission cannot resolve a usable account assignment."""


@dataclass(frozen=True, slots=True)
class ConnectionAccount:
    provider: ModelProvider
    revision: int
    label: str
    verified: bool
    observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ModelConnection:
    id: int
    runtime_key: str
    name: str
    team_id: int | None
    team_name: str | None
    state: ConnectionState
    revision: int
    allowed_routes: tuple[ModelChoice, ...]
    accounts: tuple[ConnectionAccount, ...]
    queued_jobs: int | None
    leased_jobs: int | None
    active_executions: int | None
    can_manage: bool
    can_configure: bool
    active_login_id: UUID | None
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class ConnectionPage:
    items: tuple[ModelConnection, ...]
    next_after_id: int | None


@dataclass(frozen=True, slots=True)
class _ConnectionRow:
    id: int
    runtime_key: str
    name: str
    team_id: int | None
    team_name: str | None
    state: str
    revision: int
    allowed_routes: object
    member_role: str | None
    max_concurrency: int


_CONNECTION_SELECT = """
    SELECT managed.id, managed.runtime_key, managed.name, managed.team_id,
           team.name AS team_name, managed.state, managed.revision, managed.allowed_routes,
           member.role AS member_role, managed.max_concurrency
    FROM review_agent.model_connections managed
    LEFT JOIN review_agent.teams team ON team.id = managed.team_id
    LEFT JOIN review_agent.team_members member ON member.team_id = managed.team_id AND member.user_id = %s
"""


def _require(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    row: _ConnectionRow,
    *,
    manage: bool,
) -> None:
    if (
        scope.team_id is not None
        and row.team_id is not None
        and row.team_id != scope.team_id
    ):
        raise team_access.ResourceNotFound()
    if row.team_id is None:
        if manage:
            team_access.require_owner(scope)
        elif not scope.is_admin:
            visible = connection.execute(
                """SELECT 1 FROM review_agent.team_members member
                   LEFT JOIN review_agent.team_model_policies policy ON policy.team_id = member.team_id
                   WHERE member.user_id = %s AND COALESCE(policy.connection_id, 1) = %s
                     AND (%s::bigint IS NULL OR member.team_id = %s) LIMIT 1""",
                (scope.user_id, row.id, scope.team_id, scope.team_id),
            ).fetchone()
            if visible is None:
                raise team_access.ResourceNotFound()
    elif not scope.is_admin:
        if row.member_role is None:
            raise team_access.ResourceNotFound()
        if manage and row.member_role != "maintainer":
            raise team_access.AccessDenied("Team maintainer access required")


def _view_rows(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    rows: list[_ConnectionRow],
) -> tuple[ModelConnection, ...]:
    if not rows:
        return ()
    ids = [row.id for row in rows]
    account_rows = connection.execute(
        """SELECT connection_id, provider, revision, label, identity_sha256 IS NOT NULL, observed_at
           FROM review_agent.model_accounts WHERE connection_id = ANY(%s) ORDER BY connection_id, provider""",
        (ids,),
    ).fetchall()
    accounts: dict[int, list[ConnectionAccount]] = {}
    for connection_id, provider, revision, label, verified, observed_at in account_rows:
        accounts.setdefault(connection_id, []).append(
            ConnectionAccount(
                ModelProvider(provider), revision, label, verified, observed_at
            )
        )
    workload = connection.execute(
        """SELECT connection_id, sum(queued), sum(leased), sum(executions) FROM (
               SELECT subject.model_connection_id AS connection_id,
                      count(*) FILTER (WHERE job.status = 'queued') AS queued,
                      count(*) FILTER (WHERE job.status = 'leased') AS leased, 0 AS executions
               FROM review_agent.review_jobs job
               JOIN review_agent.review_runs run ON run.id = job.review_run_id
               JOIN review_agent.review_subjects subject ON subject.id = run.review_subject_id
               WHERE job.status IN ('queued', 'leased') AND subject.model_connection_id = ANY(%s)
               GROUP BY subject.model_connection_id
               UNION ALL
               SELECT connection_id, 0, 0, count(*) FROM review_agent.model_executions
               WHERE finished_at IS NULL AND connection_id = ANY(%s) GROUP BY connection_id
           ) work GROUP BY connection_id""",
        (ids, ids),
    ).fetchall()
    counts = {int(row[0]): (int(row[1]), int(row[2]), int(row[3])) for row in workload}
    active_logins = dict(
        connection.execute(
            "SELECT connection_id, id FROM review_agent.model_login_sessions WHERE connection_id = ANY(%s) AND actor_id = %s AND finished_at IS NULL",
            (ids, scope.user_id),
        ).fetchall()
    )
    results: list[ModelConnection] = []
    for row in rows:
        queued, leased, executions = counts.get(row.id, (0, 0, 0))
        can_manage = (
            scope.is_owner
            if row.team_id is None
            else scope.is_admin or row.member_role == "maintainer"
        )
        see_workload = scope.is_admin or row.team_id is not None
        results.append(
            ModelConnection(
                row.id,
                row.runtime_key,
                row.name,
                row.team_id,
                row.team_name,
                ConnectionState(row.state),
                row.revision,
                _choices(row.allowed_routes),
                tuple(accounts.get(row.id, ())),
                queued if see_workload else None,
                leased if see_workload else None,
                executions if see_workload else None,
                can_manage,
                scope.is_owner if row.team_id is None else scope.is_admin,
                active_logins.get(row.id) if can_manage else None,
                row.max_concurrency,
            )
        )
    return tuple(results)


def list_connections(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    limit: int = 50,
    after_id: int = 0,
) -> ConnectionPage:
    if not 1 <= limit <= 100 or after_id < 0:
        raise ValueError("Invalid connection page")
    with connection.cursor(row_factory=class_row(_ConnectionRow)) as cursor:
        rows = cursor.execute(
            _CONNECTION_SELECT
            + """
            WHERE managed.id > %s AND managed.state <> 'retired'
              AND (%s::bigint IS NULL OR managed.team_id = %s OR managed.team_id IS NULL)
              AND (%s OR member.role IS NOT NULL OR (managed.team_id IS NULL AND EXISTS (
                  SELECT 1 FROM review_agent.team_members allowed_member
                  LEFT JOIN review_agent.team_model_policies policy ON policy.team_id = allowed_member.team_id
                  WHERE allowed_member.user_id = %s AND COALESCE(policy.connection_id, 1) = managed.id
                    AND (%s::bigint IS NULL OR allowed_member.team_id = %s)
              )))
            ORDER BY managed.id LIMIT %s""",
            (
                scope.user_id,
                after_id,
                scope.team_id,
                scope.team_id,
                scope.is_admin,
                scope.user_id,
                scope.team_id,
                scope.team_id,
                limit + 1,
            ),
        ).fetchall()
    return ConnectionPage(
        _view_rows(connection, scope, rows[:limit]),
        rows[limit - 1].id if len(rows) > limit else None,
    )


def get_connection(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    connection_id: int,
    *,
    manage: bool = False,
    lock: bool = False,
) -> ModelConnection:
    with connection.cursor(row_factory=class_row(_ConnectionRow)) as cursor:
        row = cursor.execute(
            _CONNECTION_SELECT
            + " WHERE managed.id = %s"
            + (" FOR UPDATE OF managed" if lock else ""),
            (scope.user_id, connection_id),
        ).fetchone()
    if row is None:
        raise team_access.ResourceNotFound()
    _require(connection, scope, row, manage=manage)
    return _view_rows(connection, scope, [row])[0]


def _expected(current: ModelConnection, expected_revision: int) -> None:
    if current.revision != expected_revision:
        raise ConnectionConflict("Connection changed. Reload before continuing.")
    if current.state is ConnectionState.RETIRED:
        raise ConnectionConflict("This connection has been retired")


def require_quota_account(
    connection: psycopg.Connection[TupleRow],
    *,
    connection_id: int,
    provider: ModelProvider,
    expected_revision: int,
    identity_sha256: str | None,
) -> None:
    row = connection.execute(
        """SELECT revision, identity_sha256 FROM review_agent.model_accounts
           WHERE connection_id = %s AND provider = %s""",
        (connection_id, provider.value),
    ).fetchone()
    if (
        row is None
        or row[0] != expected_revision
        or (identity_sha256 is not None and row[1] != identity_sha256)
    ):
        raise ConnectionConflict(
            "The provider account changed or has not been verified. Reload the connection."
        )


def create_connection(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    runtime_key: str,
    name: str,
    team_id: int | None,
    allowed_routes: tuple[ModelChoice, ...],
    reason: str,
    max_concurrency: int | None = None,
) -> ModelConnection:
    team_access.require_owner(scope)
    if team_id is not None:
        team_access.require_team(connection, scope, team_id)
    choices = _choices([choice.to_json() for choice in allowed_routes])
    capacity = _concurrency(max_concurrency if max_concurrency is not None else 4)
    try:
        row = connection.execute(
            """INSERT INTO review_agent.model_connections (runtime_key, name, team_id, allowed_routes, max_concurrency)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (
                runtime_key,
                name,
                team_id,
                Jsonb([choice.to_json() for choice in choices]),
                capacity,
            ),
        ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise ConnectionConflict(
            "This managed runtime already has a connection"
        ) from exc
    assert row is not None
    connection_id = int(row[0])
    connection.execute(
        "INSERT INTO review_agent.model_accounts (connection_id, provider) VALUES (%s, 'openai-codex'), (%s, 'anthropic')",
        (connection_id, connection_id),
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.CONNECTION_CREATED,
        team_id=team_id,
        subject=f"model-connection:{connection_id}",
        reason=reason,
        details={"name": name, "runtime_key": runtime_key, "max_concurrency": capacity},
        owner_only=team_id is None,
    )
    return get_connection(connection, scope, connection_id)


def update_connection(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    connection_id: int,
    name: str,
    allowed_routes: tuple[ModelChoice, ...],
    expected_revision: int,
    reason: str,
    max_concurrency: int | None = None,
) -> ModelConnection:
    current = get_connection(connection, scope, connection_id, manage=True, lock=True)
    if not current.can_configure:
        raise team_access.AccessDenied("Platform administrator access required")
    _expected(current, expected_revision)
    if current.state in {
        ConnectionState.AUTHENTICATING,
        ConnectionState.NEEDS_ATTENTION,
    }:
        raise ConnectionConflict(
            "Finish or reconcile the provider operation before editing this connection"
        )
    choices = _choices([choice.to_json() for choice in allowed_routes])
    capacity = _concurrency(
        current.max_concurrency if max_concurrency is None else max_concurrency
    )
    if (
        current.name == name
        and current.allowed_routes == choices
        and current.max_concurrency == capacity
    ):
        return current
    conflicts = connection.execute(
        """SELECT 1 FROM review_agent.team_model_policies policy
           WHERE COALESCE(policy.connection_id, 1) = %s AND policy.provider IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(%s::jsonb) choice
                 WHERE choice->>'provider' = policy.provider AND choice->>'model' = policy.model
                   AND choice->'reasoning_efforts' ? policy.reasoning_effort)
           LIMIT 1""",
        (connection_id, Jsonb([choice.to_json() for choice in choices])),
    ).fetchone()
    if conflicts is not None:
        raise ConnectionConflict(
            "A team still uses a model choice being removed. Update its model policy first."
        )
    connection.execute(
        "UPDATE review_agent.model_connections SET name = %s, allowed_routes = %s, max_concurrency = %s, revision = revision + 1, updated_at = statement_timestamp() WHERE id = %s",
        (
            name,
            Jsonb([choice.to_json() for choice in choices]),
            capacity,
            connection_id,
        ),
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.CONNECTION_UPDATED,
        team_id=current.team_id,
        subject=f"model-connection:{connection_id}",
        reason=reason,
        details={
            "name": name,
            "allowed_models": len(choices),
            "max_concurrency": capacity,
        },
        owner_only=current.team_id is None,
    )
    return get_connection(connection, scope, connection_id)


def set_enabled(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    connection_id: int,
    enabled: bool,
    expected_revision: int,
    reason: str,
    observed: ManagedRuntimeStatus | None = None,
) -> ModelConnection:
    current = get_connection(connection, scope, connection_id, manage=True, lock=True)
    _expected(current, expected_revision)
    desired = ConnectionState.ENABLED if enabled else ConnectionState.DISABLED
    if current.state is desired:
        return current
    if current.state in {
        ConnectionState.AUTHENTICATING,
        ConnectionState.NEEDS_ATTENTION,
    }:
        raise ConnectionConflict(
            "Finish or reconcile the provider operation before changing this connection"
        )
    if enabled:
        if observed is None or observed.runtime_key != current.runtime_key:
            raise ConnectionConflict(
                "The managed runtime could not verify its connection identity"
            )
        available = [
            account
            for account in observed.accounts
            if account.availability is AccountAvailability.AVAILABLE
        ]
        if not available or any(
            account.availability
            in {
                AccountAvailability.MULTIPLE_ACCOUNTS,
                AccountAvailability.ISOLATION_REQUIRED,
            }
            for account in observed.accounts
        ):
            raise ConnectionConflict(
                "Connect one account per provider in the runtime's own credential home"
            )
        for account in available:
            existing = connection.execute(
                "SELECT identity_sha256 FROM review_agent.model_accounts WHERE connection_id = %s AND provider = %s",
                (connection_id, account.provider.value),
            ).fetchone()
            if existing is None or (
                existing[0] is not None and existing[0] != account.identity_sha256
            ):
                raise ConnectionConflict(
                    "The provider account changed. Record the account replacement before enabling it."
                )
            connection.execute(
                "UPDATE review_agent.model_accounts SET identity_sha256 = %s, observed_at = statement_timestamp() WHERE connection_id = %s AND provider = %s",
                (account.identity_sha256, connection_id, account.provider.value),
            )
        connection.execute(
            "UPDATE review_agent.model_connections SET installed_contract = %s WHERE id = %s",
            (Jsonb(observed.contract.to_json()), connection_id),
        )
    connection.execute(
        "UPDATE review_agent.model_connections SET state = %s, revision = revision + 1, updated_at = statement_timestamp() WHERE id = %s",
        (desired.value, connection_id),
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.CONNECTION_UPDATED,
        team_id=current.team_id,
        subject=f"model-connection:{connection_id}",
        reason=reason,
        details={"state": desired.value},
        owner_only=current.team_id is None,
    )
    return get_connection(connection, scope, connection_id)


def require_drained(current: ModelConnection) -> None:
    if current.queued_jobs or current.leased_jobs or current.active_executions:
        raise ConnectionConflict(
            "Drain active reviews and cancel or finish queued reviews before replacing an account"
        )


def retire_connection(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    connection_id: int,
    expected_revision: int,
    reason: str,
) -> ModelConnection:
    current = get_connection(connection, scope, connection_id, manage=True, lock=True)
    if not current.can_configure:
        raise team_access.AccessDenied("Platform administrator access required")
    _expected(current, expected_revision)
    if (
        connection_id == SHARED_CONNECTION_ID
        or current.state is not ConnectionState.DISABLED
    ):
        raise ConnectionConflict(
            "Only a paused dedicated or additional shared connection can be retired"
        )
    require_drained(current)
    if (
        connection.execute(
            "SELECT 1 FROM review_agent.team_model_policies WHERE connection_id = %s LIMIT 1",
            (connection_id,),
        ).fetchone()
        is not None
    ):
        raise ConnectionConflict(
            "Assign the affected teams to another connection before retiring this one"
        )
    connection.execute(
        "UPDATE review_agent.model_connections SET state = 'retired', revision = revision + 1, updated_at = statement_timestamp() WHERE id = %s",
        (connection_id,),
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.CONNECTION_REMOVED,
        team_id=current.team_id,
        subject=f"model-connection:{connection_id}",
        reason=reason,
        details={"state": "retired"},
        owner_only=current.team_id is None,
    )
    return get_connection(connection, scope, connection_id)


@dataclass(frozen=True, slots=True)
class TeamModelPolicy:
    team_id: int
    revision: int
    connection_id: int | None
    provider: ModelProvider | None
    model: str | None
    reasoning_effort: str | None
    effective_provider: ModelProvider
    effective_model: str
    effective_reasoning_effort: str
    connection: ModelConnection
    max_concurrency: int


def _concurrency(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError("Review concurrency must be between 1 and 2147483647")
    return value


def team_policy(
    connection: psycopg.Connection[TupleRow], scope: AccessScope, team_id: int
) -> TeamModelPolicy:
    team_access.require_team(connection, scope, team_id)
    row = connection.execute(
        "SELECT revision, connection_id, provider, model, reasoning_effort, max_concurrency FROM review_agent.team_model_policies WHERE team_id = %s",
        (team_id,),
    ).fetchone()
    revision, connection_id, provider, model, effort, max_concurrency = (
        row if row else (0, None, None, None, None, 4)
    )
    saved = deployment_settings.latest(connection)
    defaults = (
        saved.settings if saved else DeploymentSettings.from_environment(os.environ)
    )
    managed = get_connection(connection, scope, connection_id or SHARED_CONNECTION_ID)
    return TeamModelPolicy(
        team_id,
        revision,
        connection_id,
        ModelProvider(provider) if provider else None,
        model,
        effort,
        ModelProvider(provider or defaults.model_provider),
        model or defaults.model,
        effort or defaults.reasoning_effort,
        managed,
        max_concurrency,
    )


def save_team_policy(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    team_id: int,
    connection_id: int | None,
    provider: ModelProvider | None,
    model: str | None,
    reasoning_effort: str | None,
    expected_revision: int,
    reason: str,
    max_concurrency: int | None = None,
) -> TeamModelPolicy:
    team_access.require_team(connection, scope, team_id, maintain=True)
    connection.execute(
        "SELECT id FROM review_agent.teams WHERE id = %s FOR UPDATE", (team_id,)
    )
    current = team_policy(connection, scope, team_id)
    if current.revision != expected_revision:
        raise ConnectionConflict("Team model policy changed. Reload before saving.")
    capacity = _concurrency(
        current.max_concurrency if max_concurrency is None else max_concurrency
    )
    if capacity != current.max_concurrency and not scope.is_admin:
        raise team_access.AccessDenied(
            "Only platform administrators can change team capacity"
        )
    if (current.connection_id or SHARED_CONNECTION_ID) != (
        connection_id or SHARED_CONNECTION_ID
    ) and not scope.is_admin:
        raise team_access.AccessDenied(
            "Only platform administrators can assign a connection"
        )
    managed = get_connection(
        connection, scope, connection_id or SHARED_CONNECTION_ID, lock=True
    )
    if managed.team_id is not None and managed.team_id != team_id:
        raise ConnectionConflict("A dedicated connection belongs to its owning team")
    if (
        current.connection_id or SHARED_CONNECTION_ID
    ) != managed.id and managed.state is not ConnectionState.ENABLED:
        raise ConnectionConflict("Enable and verify the connection before assigning it")
    if (provider is None, model is None, reasoning_effort is None) not in (
        (True, True, True),
        (False, False, False),
    ):
        raise ValueError(
            "Select provider, model, and reasoning together, or inherit all three"
        )
    if provider is not None and not any(
        choice.provider is provider
        and choice.model == model
        and reasoning_effort in choice.reasoning_efforts
        for choice in managed.allowed_routes
    ):
        raise ConnectionConflict(
            "This model and reasoning choice is not allowed for the connection"
        )
    if (
        current.connection_id,
        current.provider,
        current.model,
        current.reasoning_effort,
        current.max_concurrency,
    ) == (connection_id, provider, model, reasoning_effort, capacity):
        return current
    connection.execute(
        """INSERT INTO review_agent.team_model_policies (team_id, connection_id, provider, model, reasoning_effort, max_concurrency)
           VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (team_id) DO UPDATE SET
           connection_id = EXCLUDED.connection_id, provider = EXCLUDED.provider, model = EXCLUDED.model,
           reasoning_effort = EXCLUDED.reasoning_effort, max_concurrency = EXCLUDED.max_concurrency,
           revision = review_agent.team_model_policies.revision + 1,
           updated_at = statement_timestamp()""",
        (
            team_id,
            connection_id,
            provider.value if provider else None,
            model,
            reasoning_effort,
            capacity,
        ),
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.SETTINGS_UPDATED,
        team_id=team_id,
        subject=f"team-model-policy:{team_id}",
        reason=reason,
        details={
            "connection_id": connection_id,
            "provider": provider.value if provider else None,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "max_concurrency": capacity,
        },
    )
    return team_policy(connection, scope, team_id)


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    contract: review_contract.ReviewContract
    route: review_contract.ModelRoute


def resolve_model(
    connection: psycopg.Connection[TupleRow],
    *,
    provider_repository_id: int,
    installed: review_contract.ReviewContract,
) -> ResolvedModel:
    """Resolve once in the admission transaction, under the ownership barrier."""
    connection.execute("LOCK TABLE review_agent.admin_users IN SHARE MODE")
    row = connection.execute(
        """SELECT ownership.team_id, COALESCE(policy.connection_id, 1),
                  policy.provider, policy.model, policy.reasoning_effort
           FROM review_agent.repositories repository
           LEFT JOIN review_agent.team_repositories ownership ON ownership.repository_id = repository.id
           LEFT JOIN review_agent.team_model_policies policy ON policy.team_id = ownership.team_id
           WHERE repository.provider = 'github' AND repository.provider_repository_id = %s""",
        (provider_repository_id,),
    ).fetchone()
    if row is None:
        raise ModelPolicyUnavailable("Repository account assignment is unavailable")
    team_id, connection_id, provider, model, effort = row
    selected = installed
    saved = deployment_settings.latest(connection)
    if saved is not None:
        selected = review_contract.with_model_route(
            selected,
            provider=saved.settings.model_provider,
            model=saved.settings.model,
            effort=saved.settings.reasoning_effort,
        )
    if provider is not None:
        selected = review_contract.with_model_route(
            selected,
            provider=provider,
            model=model,
            effort=effort,
        )
    account = connection.execute(
        """SELECT managed.revision, managed.team_id, managed.state, account.revision, managed.installed_contract, account.identity_sha256
           FROM review_agent.model_connections managed
           JOIN review_agent.model_accounts account ON account.connection_id = managed.id
           WHERE managed.id = %s AND account.provider = %s
           FOR SHARE OF managed, account""",
        (connection_id, selected.model_provider),
    ).fetchone()
    if (
        account is None
        or account[2] not in {ConnectionState.ENABLED, ConnectionState.DISABLED}
        or (account[1] is not None and account[1] != team_id)
        or (
            (connection_id != SHARED_CONNECTION_ID or account[3] != 1)
            and account[5] is None
        )
    ):
        raise ModelPolicyUnavailable("The assigned model connection is unavailable")
    if account[4] is not None:
        runtime_contract = review_contract.parse_contract(account[4])
        if (
            runtime_contract.profile != installed.profile
            or runtime_contract.engine_bundle_sha256 != installed.engine_bundle_sha256
            or runtime_contract.profile_bundle_sha256 != installed.profile_bundle_sha256
            or runtime_contract.hermes_image != installed.hermes_image
        ):
            raise ModelPolicyUnavailable(
                "The assigned runtime requires the current review image and profile"
            )
        selected = review_contract.with_model_route(
            runtime_contract,
            provider=selected.model_provider,
            model=selected.model,
            effort=selected.reasoning_effort,
        )
    return ResolvedModel(
        selected,
        review_contract.ModelRoute(team_id, connection_id, account[0], account[3]),
    )
