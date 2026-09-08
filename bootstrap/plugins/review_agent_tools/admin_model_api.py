"""Authenticated model connection and team model policy transport."""

from collections.abc import Mapping
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import Field, BaseModel, ConfigDict, model_validator

from . import model_connection_config
from .admin_auth import AdminAuth
from .admin_model_application import (
    ConnectionRuntime,
    ModelConnectionsApplication,
    ModelLogin,
)
from .admin_teams_api import ChangeReason
from .model_accounts import ModelProvider
from .model_quota import AccountQuota
from .postgres import model_connections as models
from .postgres.runtime import PostgreSQLRuntime
from .postgres.team_access import AccessRequest, authorized_transaction
from .review_contract import REASONING_EFFORTS


ConnectionId = Annotated[int, Path(ge=1, le=9223372036854775807)]


class ModelChoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: ModelProvider
    model: str = Field(min_length=1, max_length=200)
    reasoning_efforts: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_choice(self) -> "ModelChoiceInput":
        self.domain()
        return self

    def domain(self) -> models.ModelChoice:
        return models.ModelChoice(
            self.provider, self.model, tuple(self.reasoning_efforts)
        )


class ConnectionInput(ChangeReason):
    name: str = Field(min_length=1, max_length=80)
    max_concurrency: int | None = Field(default=None, ge=1, le=2147483647, strict=True)
    allowed_routes: list[ModelChoiceInput] = Field(
        default_factory=list[ModelChoiceInput], max_length=50
    )

    @model_validator(mode="after")
    def validate_choices(self) -> "ConnectionInput":
        if len(
            {(choice.provider, choice.model) for choice in self.allowed_routes}
        ) != len(self.allowed_routes):
            raise ValueError("Model choices must be unique")
        return self


class ConnectionCreate(ConnectionInput):
    runtime_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    team_id: int | None = Field(default=None, ge=1, le=9223372036854775807)


class ConnectionUpdate(ConnectionInput):
    expected_revision: int = Field(ge=1)


class ConnectionChange(ChangeReason):
    expected_revision: int = Field(ge=1)


class ConnectionEnabled(ConnectionChange):
    enabled: bool


class ConnectionReconcile(ConnectionChange):
    runtime_restarted: bool = False


class TeamModelUpdate(ChangeReason):
    expected_revision: int = Field(ge=0)
    max_concurrency: int | None = Field(default=None, ge=1, le=2147483647, strict=True)
    connection_id: int | None = Field(default=None, ge=1, le=9223372036854775807)
    provider: ModelProvider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: str | None = None

    @model_validator(mode="after")
    def validate_model(self) -> "TeamModelUpdate":
        if (
            self.provider is None,
            self.model is None,
            self.reasoning_effort is None,
        ) not in ((True, True, True), (False, False, False)):
            raise ValueError(
                "Select provider, model, and reasoning together, or inherit all three"
            )
        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in REASONING_EFFORTS
        ):
            raise ValueError("Reasoning effort is invalid")
        return self


def create_router(
    runtime: PostgreSQLRuntime,
    auth: AdminAuth,
    controls: Mapping[str, model_connection_config.ManagedControl],
) -> APIRouter:
    application = ModelConnectionsApplication(runtime, controls)
    router = APIRouter(tags=["model connections"])

    def runtimes(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> tuple[str, ...]:
        return application.configured_runtimes(access)

    def list_connections(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    ) -> models.ConnectionPage:
        with authorized_transaction(runtime, access) as (connection, scope):
            return models.list_connections(
                connection, scope, limit=limit, after_id=after_id
            )

    def get_connection(
        connection_id: ConnectionId,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.ModelConnection:
        return application.get(access, connection_id)

    def create_connection(
        request: ConnectionCreate,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.ModelConnection:
        if request.runtime_key not in application.configured_runtimes(access):
            raise models.ConnectionConflict(
                "A platform operator must provision this managed runtime first"
            )

        with authorized_transaction(runtime, access, write=True) as (connection, scope):
            return models.create_connection(
                connection,
                scope,
                runtime_key=request.runtime_key,
                max_concurrency=request.max_concurrency,
                name=request.name,
                team_id=request.team_id,
                allowed_routes=tuple(
                    choice.domain() for choice in request.allowed_routes
                ),
                reason=request.reason,
            )

    def quota(
        connection_id: ConnectionId,
        provider: ModelProvider,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        refresh: bool = False,
    ) -> AccountQuota:
        return application.quota(access, connection_id, provider, refresh=refresh)

    def update_connection(
        connection_id: ConnectionId,
        request: ConnectionUpdate,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.ModelConnection:
        with authorized_transaction(runtime, access, write=True) as (connection, scope):
            return models.update_connection(
                connection,
                scope,
                connection_id=connection_id,
                name=request.name,
                allowed_routes=tuple(
                    choice.domain() for choice in request.allowed_routes
                ),
                expected_revision=request.expected_revision,
                max_concurrency=request.max_concurrency,
                reason=request.reason,
            )

    def enabled(
        connection_id: ConnectionId,
        request: ConnectionEnabled,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.ModelConnection:
        return application.set_enabled(
            access,
            connection_id,
            enabled=request.enabled,
            expected_revision=request.expected_revision,
            reason=request.reason,
        )

    def reconcile(
        connection_id: ConnectionId,
        request: ConnectionReconcile,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.ModelConnection:
        return application.reconcile(
            access,
            connection_id,
            expected_revision=request.expected_revision,
            restarted=request.runtime_restarted,
            reason=request.reason,
        )

    def start_login(
        connection_id: ConnectionId,
        request: ConnectionChange,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> ModelLogin:
        return application.start_login(
            access,
            connection_id,
            expected_revision=request.expected_revision,
            reason=request.reason,
        )

    def runtime_status(
        connection_id: ConnectionId,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> ConnectionRuntime:
        return application.runtime_status(access, connection_id)

    def retire(
        connection_id: ConnectionId,
        request: ConnectionChange,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.ModelConnection:
        with authorized_transaction(runtime, access, write=True) as (connection, scope):
            return models.retire_connection(
                connection,
                scope,
                connection_id=connection_id,
                expected_revision=request.expected_revision,
                reason=request.reason,
            )

    def get_login(
        connection_id: ConnectionId,
        operation_id: UUID,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> ModelLogin:
        return application.get_login(access, connection_id, operation_id)

    def poll_login(
        connection_id: ConnectionId,
        operation_id: UUID,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> ModelLogin:
        return application.step_login(access, connection_id, operation_id, cancel=False)

    def cancel_login(
        connection_id: ConnectionId,
        operation_id: UUID,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> ModelLogin:
        return application.step_login(access, connection_id, operation_id, cancel=True)

    def team_policy(
        team_id: ConnectionId,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.TeamModelPolicy:
        with authorized_transaction(runtime, access) as (connection, scope):
            return models.team_policy(connection, scope, team_id)

    def save_team_policy(
        team_id: ConnectionId,
        request: TeamModelUpdate,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> models.TeamModelPolicy:
        with authorized_transaction(runtime, access, write=True) as (connection, scope):
            return models.save_team_policy(
                connection,
                scope,
                team_id=team_id,
                connection_id=request.connection_id,
                max_concurrency=request.max_concurrency,
                provider=request.provider,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
                expected_revision=request.expected_revision,
                reason=request.reason,
            )

    router.add_api_route("/api/model-connections/runtimes", runtimes, methods=["GET"])
    router.add_api_route("/api/model-connections", list_connections, methods=["GET"])
    router.add_api_route(
        "/api/model-connections", create_connection, methods=["POST"], status_code=201
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}", get_connection, methods=["GET"]
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}", update_connection, methods=["PATCH"]
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/enabled", enabled, methods=["POST"]
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/reconcile", reconcile, methods=["POST"]
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/runtime",
        runtime_status,
        methods=["GET"],
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/quota/{provider}",
        quota,
        methods=["GET"],
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/retire", retire, methods=["POST"]
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/login", start_login, methods=["POST"]
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/login/{operation_id}",
        get_login,
        methods=["GET"],
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/login/{operation_id}/poll",
        poll_login,
        methods=["POST"],
    )
    router.add_api_route(
        "/api/model-connections/{connection_id}/login/{operation_id}/cancel",
        cancel_login,
        methods=["POST"],
    )
    router.add_api_route(
        "/api/teams/{team_id}/model-policy", team_policy, methods=["GET"]
    )
    router.add_api_route(
        "/api/teams/{team_id}/model-policy", save_team_policy, methods=["PUT"]
    )
    return router
