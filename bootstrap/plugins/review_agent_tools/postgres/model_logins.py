"""Durable account operations; remote Hermes calls occur outside transactions."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from ..domain.access import Role
from ..model_accounts import AccountAvailability, ManagedRuntimeStatus, ModelProvider
from . import audit, model_connections as models, team_access
from .team_access import AccessScope


class LoginState(StrEnum):
    STARTING = "starting"
    PENDING = "pending"
    POLLING = "polling"
    CANCELLING = "cancelling"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class LoginOperation:
    id: UUID
    connection_id: int
    account_revision: int
    connection_revision: int
    runtime_instance: UUID
    actor_id: UUID
    actor_role: str
    team_id: int | None
    remote_session_id: str | None
    status: str
    reason: str
    expires_at: datetime
    poll_after: datetime
    finished_at: datetime | None


def _read(
    connection: psycopg.Connection[TupleRow], connection_id: int, operation_id: UUID
) -> LoginOperation:
    with connection.cursor(row_factory=class_row(LoginOperation)) as cursor:
        operation = cursor.execute(
            """SELECT id, connection_id, account_revision, connection_revision, runtime_instance,
                      actor_id, actor_role, team_id, remote_session_id, status, reason,
                      expires_at, poll_after, finished_at FROM review_agent.model_login_sessions
               WHERE id = %s AND connection_id = %s""",
            (operation_id, connection_id),
        ).fetchone()
    if operation is None:
        raise team_access.ResourceNotFound()
    return operation


def get_login(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    connection_id: int,
    operation_id: UUID,
) -> LoginOperation:
    models.get_connection(connection, scope, connection_id, manage=True)
    operation = _read(connection, connection_id, operation_id)
    if operation.actor_id != scope.user_id:
        raise team_access.AccessDenied(
            "Only the person who started this login can use its session"
        )
    return operation


def _record(
    connection: psycopg.Connection[TupleRow],
    operation: LoginOperation,
    *,
    state: LoginState,
    outcome: audit.AuditOutcome,
) -> None:
    # Completion records an already authorized remote attempt, even if its actor
    # was revoked while Hermes was responding. It never re-enables dispatch.
    scope = AccessScope(
        operation.actor_id, Role(operation.actor_role), operation.team_id
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.PROVIDER_LOGIN_CANCELLED
        if state is LoginState.CANCELLED
        else audit.AuditAction.PROVIDER_LOGIN,
        team_id=operation.team_id,
        subject=f"model-connection:{operation.connection_id}",
        reason=operation.reason,
        details={"provider": ModelProvider.CODEX.value, "status": state.value},
        owner_only=operation.team_id is None,
        operation_id=operation.id,
        outcome=outcome,
    )


def prepare_login(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    connection_id: int,
    expected_revision: int,
    reason: str,
    observed: ManagedRuntimeStatus,
) -> LoginOperation:
    current = models.get_connection(
        connection, scope, connection_id, manage=True, lock=True
    )
    if (
        current.revision != expected_revision
        or current.state is not models.ConnectionState.DISABLED
    ):
        raise models.ConnectionConflict(
            "Pause the connection and reload before starting a login"
        )
    models.require_drained(current)
    if observed.runtime_key != current.runtime_key:
        raise models.ConnectionConflict(
            "The runtime does not belong to this connection"
        )
    if any(
        account.availability
        in {
            AccountAvailability.MULTIPLE_ACCOUNTS,
            AccountAvailability.ISOLATION_REQUIRED,
        }
        for account in observed.accounts
    ):
        raise models.ConnectionConflict(
            "The runtime requires its own credential home and one account per provider"
        )
    account = next(
        account
        for account in current.accounts
        if account.provider is ModelProvider.CODEX
    )
    operation_id = uuid4()
    connection.execute(
        "UPDATE review_agent.model_connections SET state = 'authenticating', revision = revision + 1, updated_at = statement_timestamp() WHERE id = %s",
        (connection_id,),
    )
    connection.execute(
        """INSERT INTO review_agent.model_login_sessions
               (id, connection_id, provider, account_revision, connection_revision, runtime_instance,
                actor_id, actor_role, team_id, status, reason, expires_at)
           VALUES (%s, %s, 'openai-codex', %s, %s, %s, %s, %s, %s, 'starting', %s, statement_timestamp() + interval '30 minutes')""",
        (
            operation_id,
            connection_id,
            account.revision,
            current.revision + 1,
            observed.instance_id,
            scope.user_id,
            scope.role.value,
            current.team_id,
            reason,
        ),
    )
    operation = _read(connection, connection_id, operation_id)
    _record(
        connection,
        operation,
        state=LoginState.STARTING,
        outcome=audit.AuditOutcome.STARTED,
    )
    return operation


def begin_step(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    connection_id: int,
    operation_id: UUID,
    *,
    cancel: bool,
) -> LoginOperation:
    current = models.get_connection(
        connection, scope, connection_id, manage=True, lock=True
    )
    operation = get_login(connection, scope, connection_id, operation_id)
    if operation.finished_at is not None:
        return operation
    if (
        current.revision != operation.connection_revision
        or current.state is not models.ConnectionState.AUTHENTICATING
    ):
        raise models.ConnectionConflict(
            "The connection requires reconciliation before this login can continue"
        )
    if operation.status != LoginState.PENDING:
        raise models.ConnectionConflict(
            "A provider operation is already running or requires reconciliation"
        )
    if not cancel and operation.poll_after > datetime.now(timezone.utc):
        return operation
    connection.execute(
        "UPDATE review_agent.model_login_sessions SET status = %s WHERE id = %s",
        (
            LoginState.CANCELLING.value if cancel else LoginState.POLLING.value,
            operation_id,
        ),
    )
    return _read(connection, connection_id, operation_id)


def _observe_accounts(
    connection: psycopg.Connection[TupleRow],
    connection_id: int,
    runtime_key: str,
    observed: ManagedRuntimeStatus,
) -> None:
    if observed.runtime_key != runtime_key or any(
        account.availability
        not in {AccountAvailability.AVAILABLE, AccountAvailability.DISCONNECTED}
        for account in observed.accounts
    ):
        raise models.ConnectionConflict(
            "The runtime cannot verify one account per provider in its own credential home"
        )
    for account in observed.accounts:
        connection.execute(
            """UPDATE review_agent.model_accounts SET
                   revision = revision + CASE WHEN identity_sha256 IS DISTINCT FROM %s AND identity_sha256 IS NOT NULL THEN 1 ELSE 0 END,
                   quota_observed_at = CASE WHEN identity_sha256 IS DISTINCT FROM %s THEN NULL ELSE quota_observed_at END,
                   quota_wait_until = CASE WHEN identity_sha256 IS DISTINCT FROM %s THEN NULL ELSE quota_wait_until END,
                   identity_sha256 = %s, observed_at = statement_timestamp()
               WHERE connection_id = %s AND provider = %s""",
            (
                account.identity_sha256,
                account.identity_sha256,
                account.identity_sha256,
                account.identity_sha256,
                connection_id,
                account.provider.value,
            ),
        )
    connection.execute(
        "UPDATE review_agent.model_connections SET installed_contract = %s WHERE id = %s",
        (Jsonb(observed.contract.to_json()), connection_id),
    )


def complete_step(
    connection: psycopg.Connection[TupleRow],
    operation: LoginOperation,
    *,
    state: LoginState,
    remote_session_id: str | None = None,
    expires_in: int | None = None,
    poll_interval: int = 5,
    observed: ManagedRuntimeStatus | None = None,
) -> None:
    row = connection.execute(
        "SELECT revision, state, runtime_key FROM review_agent.model_connections WHERE id = %s FOR UPDATE",
        (operation.connection_id,),
    ).fetchone()
    current = _read(connection, operation.connection_id, operation.id)
    if (
        row is None
        or row[0] != operation.connection_revision
        or row[1] != models.ConnectionState.AUTHENTICATING
        or current.status != operation.status
        or current.finished_at is not None
    ):
        raise models.ConnectionConflict(
            "The provider operation changed while Hermes was responding"
        )
    if state is LoginState.APPROVED:
        if (
            observed is None
            or observed.instance_id != operation.runtime_instance
            or not any(
                account.provider is ModelProvider.CODEX
                and account.availability is AccountAvailability.AVAILABLE
                for account in observed.accounts
            )
        ):
            state = LoginState.NEEDS_ATTENTION
        else:
            # Bookkeeping after the remote operation does not depend on the
            # actor retaining access. No model calls are admitted in this state.
            try:
                _observe_accounts(connection, operation.connection_id, row[2], observed)
            except models.ConnectionConflict:
                state = LoginState.NEEDS_ATTENTION
    terminal = state not in {LoginState.PENDING, LoginState.NEEDS_ATTENTION}
    expiry = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in
        else current.expires_at
    )
    connection.execute(
        """UPDATE review_agent.model_login_sessions SET status = %s,
               remote_session_id = COALESCE(%s, remote_session_id), expires_at = %s,
               poll_after = statement_timestamp() + %s,
               finished_at = CASE WHEN %s THEN statement_timestamp() ELSE NULL END WHERE id = %s""",
        (
            state.value,
            remote_session_id,
            expiry,
            timedelta(seconds=poll_interval),
            terminal,
            operation.id,
        ),
    )
    if state is not LoginState.PENDING:
        connection.execute(
            "UPDATE review_agent.model_connections SET state = %s, revision = revision + 1, updated_at = statement_timestamp() WHERE id = %s",
            (
                models.ConnectionState.NEEDS_ATTENTION.value
                if state is LoginState.NEEDS_ATTENTION
                else models.ConnectionState.DISABLED.value,
                operation.connection_id,
            ),
        )
        _record(
            connection,
            operation,
            state=state,
            outcome=audit.AuditOutcome.SUCCEEDED
            if state in {LoginState.APPROVED, LoginState.CANCELLED}
            else audit.AuditOutcome.FAILED,
        )


def reconcile(
    connection: psycopg.Connection[TupleRow],
    scope: AccessScope,
    *,
    connection_id: int,
    expected_revision: int,
    observed: ManagedRuntimeStatus,
    restarted: bool,
    reason: str,
) -> models.ModelConnection:
    current = models.get_connection(
        connection, scope, connection_id, manage=True, lock=True
    )
    if current.revision != expected_revision or current.state in {
        models.ConnectionState.ENABLED,
        models.ConnectionState.RETIRED,
    }:
        raise models.ConnectionConflict(
            "Pause the connection and reload before reconciling accounts"
        )
    if observed.runtime_key != current.runtime_key:
        raise models.ConnectionConflict(
            "The runtime does not belong to this connection"
        )
    if restarted:
        team_access.require_owner(scope)
        same_instance = connection.execute(
            """SELECT 1 FROM review_agent.model_executions WHERE connection_id = %s AND finished_at IS NULL AND runtime_instance = %s
               UNION ALL SELECT 1 FROM review_agent.model_login_sessions WHERE connection_id = %s AND finished_at IS NULL AND runtime_instance = %s LIMIT 1""",
            (connection_id, observed.instance_id, connection_id, observed.instance_id),
        ).fetchone()
        if same_instance is not None:
            raise models.ConnectionConflict(
                "Restart the entire managed runtime and provider control before recovery"
            )
        connection.execute(
            "UPDATE review_agent.model_executions SET finished_at = statement_timestamp() WHERE connection_id = %s AND finished_at IS NULL",
            (connection_id,),
        )
        unfinished_ids = connection.execute(
            "SELECT id FROM review_agent.model_login_sessions WHERE connection_id = %s AND finished_at IS NULL",
            (connection_id,),
        ).fetchall()
        for (operation_id,) in unfinished_ids:
            operation = _read(connection, connection_id, operation_id)
            _record(
                connection,
                operation,
                state=LoginState.CANCELLED,
                outcome=audit.AuditOutcome.FAILED,
            )
        connection.execute(
            "UPDATE review_agent.model_login_sessions SET status = 'cancelled', finished_at = statement_timestamp() WHERE connection_id = %s AND finished_at IS NULL",
            (connection_id,),
        )
        current = models.get_connection(connection, scope, connection_id)
    elif current.state is not models.ConnectionState.DISABLED:
        raise models.ConnectionConflict(
            "A platform owner must reconcile the interrupted provider operation after restarting its runtime"
        )
    models.require_drained(current)
    _observe_accounts(connection, current.id, current.runtime_key, observed)
    connection.execute(
        "UPDATE review_agent.model_connections SET state = 'disabled', revision = revision + 1, updated_at = statement_timestamp() WHERE id = %s",
        (connection_id,),
    )
    audit.record(
        connection,
        scope,
        action=audit.AuditAction.CONNECTION_UPDATED,
        team_id=current.team_id,
        subject=f"model-connection:{connection_id}",
        reason=reason,
        details={"accounts_reconciled": True, "runtime_restarted": restarted},
        owner_only=current.team_id is None,
    )
    return models.get_connection(connection, scope, connection_id)
