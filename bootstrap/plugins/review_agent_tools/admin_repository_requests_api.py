"""Team repository requests and administrator ownership decisions."""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import Field, field_validator

from . import admin_application, operator_setup
from .admin_auth import AdminAuth
from .admin_teams_api import ChangeReason, TeamId
from .domain.feedback import resolve_github_repository
from .github import app_auth, app_inventory
from .postgres import github_app, repository_requests
from .postgres.runtime import PostgreSQLRuntime
from .postgres.team_access import AccessRequest
from .settings import ReviewAgentSettings


ObjectId = Annotated[int, Path(ge=1, le=9223372036854775807)]
Limit = Annotated[int, Query(ge=1, le=100)]


class RepositorySubmission(ChangeReason):
    repository: str = Field(min_length=3, max_length=260)

    @field_validator("repository")
    @classmethod
    def repository_name(cls, value: str) -> str:
        return resolve_github_repository(value)


class RepositoryApproval(ChangeReason):
    profile: str | None = Field(
        default=None, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )


class RepositoryAssignment(ChangeReason):
    team_id: int = Field(ge=1, le=9223372036854775807)
    expected_team_id: int | None = Field(ge=1, le=9223372036854775807)


def create_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    router = APIRouter(tags=["repository requests"])

    def requests(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        status: repository_requests.RequestStatus | None = None,
        limit: Limit = 50,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> repository_requests.RepositoryRequestPage:
        return admin_application.repository_request_list(
            runtime, access=access, status=status, limit=limit, before_id=before_id
        )

    def submit(
        team_id: TeamId,
        body: RepositorySubmission,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> repository_requests.RepositoryRequest:
        return admin_application.submit_repository_request(
            runtime,
            access=access,
            team_id=team_id,
            repository=body.repository,
            reason=body.reason,
        )

    def approve(
        request_id: ObjectId,
        body: RepositoryApproval,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> repository_requests.RepositoryRequest:
        try:
            authenticator = operator_setup.github_app_authenticator(os.environ)
        except (OSError, TypeError, ValueError) as exc:
            raise HTTPException(
                503, "GitHub App credentials are not configured for approval"
            ) from exc
        try:
            return admin_application.approve_repository_request(
                runtime,
                authenticator,
                access=access,
                request_id=request_id,
                profile=body.profile or ReviewAgentSettings.from_environment().profile,
                reason=body.reason,
            )
        except (
            app_auth.GitHubAppTokenRetryable,
            app_inventory.GitHubAppInventoryRetryable,
        ) as exc:
            raise HTTPException(
                503, "GitHub verification is temporarily unavailable. Retry shortly"
            ) from exc
        except (
            app_auth.GitHubAppTokenPermanent,
            app_inventory.GitHubAppInventoryPermanent,
        ) as exc:
            raise HTTPException(
                502,
                "GitHub could not verify this repository. Check its current name and App grant",
            ) from exc
        except github_app.GitHubAppStateError as exc:
            raise HTTPException(409, str(exc)) from exc

    def reject(
        request_id: ObjectId,
        body: ChangeReason,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> repository_requests.RepositoryRequest:
        return admin_application.finish_repository_request(
            runtime,
            access=access,
            request_id=request_id,
            status=repository_requests.RequestStatus.REJECTED,
            reason=body.reason,
        )

    def withdraw(
        request_id: ObjectId,
        body: ChangeReason,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> repository_requests.RepositoryRequest:
        return admin_application.finish_repository_request(
            runtime,
            access=access,
            request_id=request_id,
            status=repository_requests.RequestStatus.WITHDRAWN,
            reason=body.reason,
        )

    def repositories(
        team_id: TeamId,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        limit: Limit = 50,
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    ) -> repository_requests.TeamRepositoryPage:
        return admin_application.team_repositories(
            runtime, access=access, team_id=team_id, limit=limit, after_id=after_id
        )

    def assign(
        repository_id: ObjectId,
        body: RepositoryAssignment,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> None:
        admin_application.assign_team_repository(
            runtime,
            access=access,
            repository_id=repository_id,
            team_id=body.team_id,
            expected_team_id=body.expected_team_id,
            reason=body.reason,
        )

    def remove(
        team_id: TeamId,
        repository_id: ObjectId,
        body: ChangeReason,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> None:
        admin_application.remove_team_repository(
            runtime,
            access=access,
            repository_id=repository_id,
            team_id=team_id,
            reason=body.reason,
        )

    router.add_api_route("/api/repository-requests", requests, methods=["GET"])
    router.add_api_route(
        "/api/teams/{team_id}/repository-requests",
        submit,
        methods=["POST"],
        status_code=201,
    )
    router.add_api_route(
        "/api/repository-requests/{request_id}/approve",
        approve,
        methods=["POST"],
        dependencies=[Depends(auth.current_admin)],
    )
    router.add_api_route(
        "/api/repository-requests/{request_id}/reject", reject, methods=["POST"]
    )
    router.add_api_route(
        "/api/repository-requests/{request_id}/withdraw", withdraw, methods=["POST"]
    )
    router.add_api_route(
        "/api/teams/{team_id}/repositories", repositories, methods=["GET"]
    )
    router.add_api_route(
        "/api/repository-ownership/{repository_id}",
        assign,
        methods=["PUT"],
        status_code=204,
    )
    router.add_api_route(
        "/api/teams/{team_id}/repositories/{repository_id}/remove",
        remove,
        methods=["POST"],
        status_code=204,
    )
    return router
