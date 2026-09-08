"""Admin-only HTTP transport for GitHub App access management."""

import os
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from . import operator_application, operator_setup
from .admin_auth import AdminAuth, User
from .github import app_auth, app_inventory
from .postgres import github_app, registry
from .postgres.runtime import PostgreSQLRuntime, PostgreSQLRuntimeError


class AccessCapability(BaseModel):
    configured: bool
    detail: str


class Installation(BaseModel):
    installation_id: int
    account: str
    account_type: github_app.AccountType
    repository_selection: github_app.RepositorySelection
    repository_activation: github_app.RepositoryActivationPolicy
    activation_policy_actor: str | None
    activation_policy_reason: str | None
    activation_policy_changed_at: datetime | None
    status: github_app.InstallationStatus
    contents_permission: github_app.PermissionLevel
    issues_permission: github_app.PermissionLevel
    pull_requests_permission: github_app.PermissionLevel
    updated_at: datetime


class RepositoryAccess(BaseModel):
    repository_id: int
    repository: str
    access: github_app.RepositoryAccess
    enabled: bool
    automatic_activation_blocked: bool
    trigger_mode: github_app.TriggerMode
    profile: str | None
    updated_by: str
    update_reason: str
    updated_at: datetime


class InstallationPage(BaseModel):
    capability: AccessCapability
    items: list[Installation]
    next_after_id: int | None


class RepositoryAccessPage(BaseModel):
    capability: AccessCapability
    items: list[RepositoryAccess]
    next_after_id: int | None


class AuditReason(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class InstallationApproval(AuditReason):
    policy: github_app.RepositoryActivationPolicy


class RepositoryEnablement(AuditReason):
    profile: str = Field(min_length=1, max_length=100)


class Reconciliation(BaseModel):
    installation: Installation
    repositories_seen: int
    repositories_removed: int
    repositories_enabled: int


Limit = Annotated[int, Query(ge=1, le=100)]
AfterId = Annotated[int, Query(ge=0, le=9223372036854775807)]
InstallationId = Annotated[int, Path(ge=1, le=9223372036854775807)]
RepositoryId = Annotated[int, Path(ge=1, le=9223372036854775807)]


def _installation(value: github_app.GitHubAppInstallation) -> Installation:
    return Installation(
        installation_id=value.provider_installation_id,
        account=value.account_login,
        account_type=value.account_type,
        repository_selection=value.repository_selection,
        repository_activation=value.repository_activation_policy,
        activation_policy_actor=value.activation_policy_actor,
        activation_policy_reason=value.activation_policy_reason,
        activation_policy_changed_at=value.activation_policy_changed_at,
        status=value.status,
        contents_permission=value.contents_permission,
        issues_permission=value.issues_permission,
        pull_requests_permission=value.pull_requests_permission,
        updated_at=value.updated_at,
    )


def _repository(value: github_app.RepositoryAccessState) -> RepositoryAccess:
    return RepositoryAccess(
        repository_id=value.provider_repository_id,
        repository=value.full_name,
        access=value.access_state,
        enabled=value.enabled,
        automatic_activation_blocked=value.automatic_activation_blocked,
        trigger_mode=value.trigger_mode,
        profile=value.profile_key,
        updated_by=value.updated_by,
        update_reason=value.update_reason,
        updated_at=value.updated_at,
    )


def _normalized_reason(value: str) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise HTTPException(422, "reason is required")
    return normalized


def _normalized_profile(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(422, "profile is required")
    return normalized


def create_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    """Create the access router using the deployment's configured App identity."""
    try:
        operator_setup.github_app_authenticator(os.environ)
        credentials_configured = True
    except (OSError, TypeError, ValueError):
        credentials_configured = False
    capability = AccessCapability(
        configured=credentials_configured,
        detail=(
            "GitHub App access operations are configured."
            if credentials_configured
            else "GitHub App credentials are not configured for access operations."
        ),
    )
    router = APIRouter(
        prefix="/api/access",
        dependencies=[Depends(auth.current_admin)],
        tags=["access"],
    )

    def require_authenticator() -> app_auth.GitHubAppAuthenticator:
        if not credentials_configured:
            raise HTTPException(503, capability.detail)
        try:
            return operator_setup.github_app_authenticator(os.environ)
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                503, "GitHub App credentials are invalid for access operations."
            ) from exc

    def actor_identity(actor: User) -> str:
        return f"admin:{actor.id}"

    def operation_failed(
        error: Exception,
        *,
        action: Literal["sync", "approve", "enable", "disable"],
    ) -> HTTPException:
        if isinstance(
            error,
            (
                app_auth.GitHubAppTokenRetryable,
                app_inventory.GitHubAppInventoryRetryable,
                PostgreSQLRuntimeError,
            ),
        ):
            return HTTPException(503, f"GitHub App {action} is temporarily unavailable.")
        if isinstance(
            error,
            (
                app_auth.GitHubAppConfigurationError,
                app_auth.GitHubAppTokenPermanent,
                app_inventory.GitHubAppInventoryPermanent,
            ),
        ):
            return HTTPException(502, f"GitHub App {action} failed provider validation.")
        return HTTPException(409, f"GitHub App {action} could not be completed.")

    def installations(limit: Limit = 50, after_id: AfterId = 0) -> InstallationPage:
        items = operator_application.list_github_app_installations(
            runtime,
            limit=limit,
            after_provider_installation_id=after_id,
        )
        return InstallationPage(
            capability=capability,
            items=[_installation(item) for item in items],
            next_after_id=(
                items[-1].provider_installation_id if len(items) == limit else None
            ),
        )

    def repositories(limit: Limit = 50, after_id: AfterId = 0) -> RepositoryAccessPage:
        items = operator_application.list_github_app_repositories(
            runtime,
            limit=limit,
            after_provider_repository_id=after_id,
        )
        return RepositoryAccessPage(
            capability=capability,
            items=[_repository(item) for item in items],
            next_after_id=(
                items[-1].provider_repository_id if len(items) == limit else None
            ),
        )

    def sync_installation(
        installation_id: InstallationId,
        request: AuditReason,
        actor: Annotated[User, Depends(auth.current_admin)],
    ) -> Reconciliation:
        try:
            result = operator_application.sync_github_app_installation(
                runtime,
                require_authenticator(),
                provider_installation_id=installation_id,
                actor=actor_identity(actor),
                reason=_normalized_reason(request.reason),
            )
        except HTTPException:
            raise
        except Exception as exc:
            if not isinstance(
                exc,
                (
                    app_auth.GitHubAppTokenRetryable,
                    app_auth.GitHubAppTokenPermanent,
                    app_inventory.GitHubAppInventoryRetryable,
                    app_inventory.GitHubAppInventoryPermanent,
                    github_app.GitHubAppStateError,
                    operator_application.OperatorInputError,
                    registry.RegistryError,
                    PostgreSQLRuntimeError,
                ),
            ):
                raise
            raise operation_failed(exc, action="sync") from exc
        return Reconciliation(
            installation=_installation(result.installation),
            repositories_seen=result.repositories_seen,
            repositories_removed=result.repositories_removed,
            repositories_enabled=result.repositories_enabled,
        )

    def approve_installation(
        installation_id: InstallationId,
        request: InstallationApproval,
        actor: Annotated[User, Depends(auth.current_admin)],
    ) -> Installation:
        try:
            result = operator_application.approve_github_app_installation(
                runtime,
                require_authenticator(),
                provider_installation_id=installation_id,
                policy=request.policy,
                actor=actor_identity(actor),
                reason=_normalized_reason(request.reason),
            )
        except HTTPException:
            raise
        except Exception as exc:
            if not isinstance(
                exc,
                (
                    app_auth.GitHubAppTokenRetryable,
                    app_auth.GitHubAppTokenPermanent,
                    app_inventory.GitHubAppInventoryRetryable,
                    app_inventory.GitHubAppInventoryPermanent,
                    github_app.GitHubAppStateError,
                    operator_application.OperatorInputError,
                    PostgreSQLRuntimeError,
                ),
            ):
                raise
            raise operation_failed(exc, action="approve") from exc
        return _installation(result)

    def enable_repository(
        repository_id: RepositoryId,
        request: RepositoryEnablement,
        actor: Annotated[User, Depends(auth.current_admin)],
    ) -> RepositoryAccess:
        try:
            result = operator_application.enable_github_app_repository(
                runtime,
                provider_repository_id=repository_id,
                profile=_normalized_profile(request.profile),
                actor=actor_identity(actor),
                reason=_normalized_reason(request.reason),
            )
        except HTTPException:
            raise
        except Exception as exc:
            if not isinstance(
                exc,
                (
                    github_app.GitHubAppStateError,
                    operator_application.OperatorInputError,
                    registry.RegistryError,
                    PostgreSQLRuntimeError,
                ),
            ):
                raise
            raise operation_failed(exc, action="enable") from exc
        return _repository(result)

    def disable_repository(
        repository_id: RepositoryId,
        request: AuditReason,
        actor: Annotated[User, Depends(auth.current_admin)],
    ) -> RepositoryAccess:
        try:
            result = operator_application.disable_github_app_repository(
                runtime,
                provider_repository_id=repository_id,
                actor=actor_identity(actor),
                reason=_normalized_reason(request.reason),
            )
        except HTTPException:
            raise
        except Exception as exc:
            if not isinstance(
                exc,
                (
                    github_app.GitHubAppStateError,
                    operator_application.OperatorInputError,
                    registry.RegistryError,
                    PostgreSQLRuntimeError,
                ),
            ):
                raise
            raise operation_failed(exc, action="disable") from exc
        return _repository(result)

    router.add_api_route("/installations", installations, methods=["GET"])
    router.add_api_route("/repositories", repositories, methods=["GET"])
    router.add_api_route(
        "/installations/{installation_id}/sync",
        sync_installation,
        methods=["POST"],
    )
    router.add_api_route(
        "/installations/{installation_id}/approve",
        approve_installation,
        methods=["POST"],
    )
    router.add_api_route(
        "/repositories/{repository_id}/enable",
        enable_repository,
        methods=["POST"],
    )
    router.add_api_route(
        "/repositories/{repository_id}/disable",
        disable_repository,
        methods=["POST"],
    )
    return router
