"""Authenticated team administration over the shared console application layer."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from . import admin_application
from .admin_auth import AdminAuth
from .postgres import audit, teams
from .postgres.runtime import PostgreSQLRuntime
from .postgres.team_access import AccessRequest, TeamRole


TeamId = Annotated[int, Path(ge=1, le=9223372036854775807)]
Limit = Annotated[int, Query(ge=1, le=100)]


class ChangeReason(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=500)


class NewTeam(ChangeReason):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class TeamUpdate(NewTeam):
    expected_revision: int = Field(ge=1)


class TeamMemberUpdate(ChangeReason):
    email: EmailStr
    role: TeamRole


def create_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    router = APIRouter(prefix="/api/teams", tags=["teams"])

    def list_teams(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        limit: Limit = 50,
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        search: Annotated[str, Query(max_length=80)] = "",
    ) -> teams.TeamPage:
        return admin_application.list_teams(
            runtime,
            access=access,
            limit=limit,
            after_id=after_id,
            search=search.strip(),
        )

    def create_team(
        request: NewTeam, access: Annotated[AccessRequest, Depends(auth.current_scope)]
    ) -> teams.Team:
        return admin_application.create_team(
            runtime,
            access=access,
            name=request.name,
            description=request.description,
            reason=request.reason,
        )

    def get_team(
        team_id: TeamId, access: Annotated[AccessRequest, Depends(auth.current_scope)]
    ) -> teams.Team:
        return admin_application.get_team(runtime, access=access, team_id=team_id)

    def update_team(
        team_id: TeamId,
        request: TeamUpdate,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> teams.Team:
        return admin_application.update_team(
            runtime,
            access=access,
            team_id=team_id,
            name=request.name,
            description=request.description,
            expected_revision=request.expected_revision,
            reason=request.reason,
        )

    def members(
        team_id: TeamId,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        limit: Limit = 50,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ) -> teams.TeamMemberPage:
        return admin_application.team_members(
            runtime, access=access, team_id=team_id, limit=limit, offset=offset
        )

    def put_member(
        team_id: TeamId,
        request: TeamMemberUpdate,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> teams.TeamMember:
        return admin_application.put_team_member(
            runtime,
            access=access,
            team_id=team_id,
            email=request.email,
            role=request.role,
            reason=request.reason,
        )

    def remove_member(
        team_id: TeamId,
        user_id: UUID,
        request: ChangeReason,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> None:
        admin_application.remove_team_member(
            runtime,
            access=access,
            team_id=team_id,
            user_id=user_id,
            reason=request.reason,
        )

    def events(
        team_id: TeamId,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        audit_access_id: Annotated[
            UUID | None, Header(alias="X-Audit-Access-ID")
        ] = None,
        limit: Limit = 50,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> audit.AuditPage:
        return admin_application.team_events(
            runtime,
            access=access,
            team_id=team_id,
            limit=limit,
            before_id=before_id,
            access_id=audit_access_id,
        )

    router.add_api_route(
        "",
        list_teams,
        methods=["GET"],
        summary="List teams visible to the current account",
    )
    router.add_api_route(
        "",
        create_team,
        methods=["POST"],
        status_code=201,
        summary="Create a team (platform administrator)",
    )
    router.add_api_route("/{team_id}", get_team, methods=["GET"])
    router.add_api_route("/{team_id}", update_team, methods=["PATCH"])
    router.add_api_route("/{team_id}/members", members, methods=["GET"])
    router.add_api_route(
        "/{team_id}/members",
        put_member,
        methods=["PUT"],
        summary="Add or change membership by exact account email",
    )
    router.add_api_route(
        "/{team_id}/members/{user_id}/remove",
        remove_member,
        methods=["POST"],
        status_code=204,
    )
    router.add_api_route("/{team_id}/events", events, methods=["GET"])
    return router
