"""Console model connections and Hermes-owned authentication coordination."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from .hermes_control import HermesControlClient, HermesControlError, LoginSession
from .model_connection_config import ManagedControl
from .model_accounts import AccountAvailability, ModelProvider
from .postgres import model_connections as models, model_logins, team_access
from .postgres.runtime import PostgreSQLRuntime
from .postgres.team_access import AccessRequest, authorized_transaction


@dataclass(frozen=True, slots=True)
class ModelLogin:
    id: UUID
    connection_id: int
    status: model_logins.LoginState
    expires_at: datetime
    poll_interval: int = 5
    user_code: str | None = None
    verification_url: str | None = None


def _login(
    operation: model_logins.LoginOperation, challenge: LoginSession | None = None
) -> ModelLogin:
    return ModelLogin(
        operation.id,
        operation.connection_id,
        model_logins.LoginState(operation.status),
        operation.expires_at,
        challenge.poll_interval if challenge else 5,
        challenge.user_code if challenge else None,
        challenge.verification_url if challenge else None,
    )


@dataclass(frozen=True, slots=True)
class ProviderAccountStatus:
    provider: ModelProvider
    availability: AccountAvailability
    account_count: int


@dataclass(frozen=True, slots=True)
class ConnectionRuntime:
    configured: bool
    accounts: tuple[ProviderAccountStatus, ...]


class ModelConnectionsApplication:
    def __init__(
        self, runtime: PostgreSQLRuntime, controls: Mapping[str, ManagedControl]
    ) -> None:
        self.runtime = runtime
        self.controls = controls

    def control(self, runtime_key: str) -> HermesControlClient:
        configured = self.controls.get(runtime_key)
        if configured is None or configured.client is None:
            raise HermesControlError(
                "Provider control is not configured for this managed runtime"
            )
        return configured.client

    def get(
        self, access: AccessRequest, connection_id: int, *, manage: bool = False
    ) -> models.ModelConnection:
        with authorized_transaction(self.runtime, access) as (connection, scope):
            return models.get_connection(
                connection, scope, connection_id, manage=manage
            )

    def configured_runtimes(self, access: AccessRequest) -> tuple[str, ...]:
        with authorized_transaction(self.runtime, access) as (_connection, scope):
            team_access.require_owner(scope)
        return tuple(
            sorted(
                key
                for key, configured in self.controls.items()
                if configured.client is not None
            )
        )

    def runtime_status(
        self, access: AccessRequest, connection_id: int
    ) -> ConnectionRuntime:
        current = self.get(access, connection_id, manage=True)
        control = self.controls.get(current.runtime_key)
        if control is None or control.client is None:
            return ConnectionRuntime(False, ())
        observed = control.client.managed_status()
        if observed.runtime_key != current.runtime_key:
            raise models.ConnectionConflict(
                "The runtime does not belong to this connection"
            )
        # Revocation can occur while the remote request is in flight.
        self.get(access, connection_id, manage=True)
        return ConnectionRuntime(
            True,
            tuple(
                ProviderAccountStatus(
                    account.provider, account.availability, account.account_count
                )
                for account in observed.accounts
            ),
        )

    def set_enabled(
        self,
        access: AccessRequest,
        connection_id: int,
        *,
        enabled: bool,
        expected_revision: int,
        reason: str,
    ) -> models.ModelConnection:
        current = self.get(access, connection_id, manage=True)
        observed = (
            self.control(current.runtime_key).managed_status() if enabled else None
        )
        with authorized_transaction(self.runtime, access, write=True) as (
            connection,
            scope,
        ):
            return models.set_enabled(
                connection,
                scope,
                connection_id=connection_id,
                enabled=enabled,
                expected_revision=expected_revision,
                reason=reason,
                observed=observed,
            )

    def get_login(
        self, access: AccessRequest, connection_id: int, operation_id: UUID
    ) -> ModelLogin:
        with authorized_transaction(self.runtime, access) as (connection, scope):
            return _login(
                model_logins.get_login(connection, scope, connection_id, operation_id)
            )

    def start_login(
        self,
        access: AccessRequest,
        connection_id: int,
        *,
        expected_revision: int,
        reason: str,
    ) -> ModelLogin:
        current = self.get(access, connection_id, manage=True)
        control = self.control(current.runtime_key)
        observed = control.managed_status()
        with authorized_transaction(self.runtime, access, write=True) as (
            connection,
            scope,
        ):
            operation = model_logins.prepare_login(
                connection,
                scope,
                connection_id=connection_id,
                expected_revision=expected_revision,
                reason=reason,
                observed=observed,
            )
        try:
            challenge = control.start_codex_login()
        except HermesControlError:
            with self.runtime.transaction() as connection:
                model_logins.complete_step(
                    connection, operation, state=model_logins.LoginState.NEEDS_ATTENTION
                )
            raise
        with self.runtime.transaction() as connection:
            model_logins.complete_step(
                connection,
                operation,
                state=model_logins.LoginState.PENDING,
                remote_session_id=challenge.session_id,
                expires_in=challenge.expires_in,
                poll_interval=challenge.poll_interval,
            )
        with authorized_transaction(self.runtime, access) as (connection, scope):
            return _login(
                model_logins.get_login(connection, scope, connection_id, operation.id),
                challenge,
            )

    def step_login(
        self,
        access: AccessRequest,
        connection_id: int,
        operation_id: UUID,
        *,
        cancel: bool,
    ) -> ModelLogin:
        current = self.get(access, connection_id, manage=True)
        control = self.control(current.runtime_key)
        with authorized_transaction(self.runtime, access, write=True) as (
            connection,
            scope,
        ):
            operation = model_logins.begin_step(
                connection, scope, connection_id, operation_id, cancel=cancel
            )
        if (
            operation.finished_at is not None
            or operation.status == model_logins.LoginState.PENDING
        ):
            return _login(operation)
        observed = None
        try:
            if operation.remote_session_id is None:
                raise HermesControlError("Provider login session is unavailable")
            if cancel or operation.expires_at <= datetime.now(timezone.utc):
                result = control.cancel_codex_login(operation.remote_session_id)
                state = (
                    (
                        model_logins.LoginState.CANCELLED
                        if cancel
                        else model_logins.LoginState.EXPIRED
                    )
                    if result.cancelled
                    else model_logins.LoginState.NEEDS_ATTENTION
                )
            else:
                polled = control.poll_codex_login(operation.remote_session_id)
                state = (
                    model_logins.LoginState.NEEDS_ATTENTION
                    if polled.status == "error"
                    else model_logins.LoginState(polled.status)
                )
                if state is model_logins.LoginState.APPROVED:
                    observed = control.managed_status()
        except HermesControlError:
            with self.runtime.transaction() as connection:
                model_logins.complete_step(
                    connection, operation, state=model_logins.LoginState.NEEDS_ATTENTION
                )
            raise
        with self.runtime.transaction() as connection:
            model_logins.complete_step(
                connection, operation, state=state, observed=observed
            )
        return self.get_login(access, connection_id, operation_id)

    def reconcile(
        self,
        access: AccessRequest,
        connection_id: int,
        *,
        expected_revision: int,
        restarted: bool,
        reason: str,
    ) -> models.ModelConnection:
        current = self.get(access, connection_id, manage=True)
        observed = self.control(current.runtime_key).managed_status()
        with authorized_transaction(self.runtime, access, write=True) as (
            connection,
            scope,
        ):
            return model_logins.reconcile(
                connection,
                scope,
                connection_id=connection_id,
                expected_revision=expected_revision,
                observed=observed,
                restarted=restarted,
                reason=reason,
            )
