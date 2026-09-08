"""Versioned reporting HTTP access and console-managed application credentials."""

from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Annotated, Generic, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import admin_application
from .admin_auth import AdminAuth
from .admin_teams_api import ChangeReason
from .postgres import admin_operations, admin_reporting, integrations, quality_reporting
from .postgres.runtime import PostgreSQLRuntime
from .postgres.team_access import (
    AccessRequest,
    IntegrationAccessRequest,
)


T = TypeVar("T")
Limit = Annotated[int, Query(ge=1, le=100)]
Watermark = Annotated[int | None, Query(ge=0, le=9223372036854775807)]
Repository = Annotated[
    str | None, Query(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
]


class Report(BaseModel, Generic[T]):
    metrics_version: Literal[1] = 1
    evidence_scope: Literal["retained_records"] = "retained_records"
    generated_at: datetime
    data: T


class IntegrationInput(ChangeReason):
    name: str = Field(min_length=1, max_length=80)
    team_ids: list[Annotated[int, Field(strict=True, ge=1, le=9223372036854775807)]] = (
        Field(default_factory=list[int], max_length=100)
    )
    deployment_wide: bool = Field(default=False, strict=True)
    read_review_content: bool = Field(default=False, strict=True)
    expires_at: datetime


def create_management_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    router = APIRouter(prefix="/api/integrations", tags=["integrations"])

    def list_integrations(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        limit: Limit = 50,
    ) -> integrations.IntegrationPage:
        return admin_application.list_integrations(
            runtime, access=access, after_id=after_id, limit=limit
        )

    def create_integration(
        request: IntegrationInput,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> integrations.IssuedIntegration:
        """Return the credential once. Only its digest is retained by Review Agent."""
        try:
            return admin_application.create_integration(
                runtime,
                access=access,
                name=request.name,
                team_ids=tuple(request.team_ids),
                deployment_wide=request.deployment_wide,
                read_review_content=request.read_review_content,
                expires_at=request.expires_at,
                reason=request.reason,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def revoke_integration(
        integration_id: Annotated[int, Path(ge=1, le=9223372036854775807)],
        request: ChangeReason,
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> integrations.Integration:
        return admin_application.revoke_integration(
            runtime, access=access, integration_id=integration_id, reason=request.reason
        )

    router.add_api_route("", list_integrations, methods=["GET"])
    router.add_api_route("", create_integration, methods=["POST"], status_code=201)
    router.add_api_route(
        "/{integration_id}/revoke", revoke_integration, methods=["POST"]
    )
    return router


def create_report_router(runtime: PostgreSQLRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["integration reports"])
    bearer = HTTPBearer(
        scheme_name="IntegrationCredential",
        description="An unexpired application credential created by a platform administrator. It cannot authenticate console control operations.",
        auto_error=False,
    )

    def current_access(
        credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        team_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> IntegrationAccessRequest:
        if (
            credential is None
            or len(credential.credentials) != 47
            or re.fullmatch(r"ra1_[A-Za-z0-9_-]{43}", credential.credentials) is None
        ):
            raise HTTPException(
                401,
                "An integration credential is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return IntegrationAccessRequest(
            sha256(credential.credentials.encode()).hexdigest(), team_id
        )

    def window(start: datetime, end: datetime) -> None:
        try:
            admin_application.report_window(days=30, start=start, end=end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def overview(
        access: Annotated[IntegrationAccessRequest, Depends(current_access)],
        start: datetime,
        end: datetime,
    ) -> Report[admin_operations.Overview]:
        """Console activity and token telemetry for the UTC interval [start, end)."""
        window(start, end)
        data = admin_application.overview(runtime, access=access, start=start, end=end)
        return Report(data=data, generated_at=data.generated_at)

    def repositories(
        access: Annotated[IntegrationAccessRequest, Depends(current_access)],
        start: datetime,
        end: datetime,
        limit: Limit = 50,
        search: Annotated[str, Query(max_length=200)] = "",
        after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        watermark_id: Watermark = None,
    ) -> Report[admin_reporting.RepositoryPage]:
        """Page by ascending repository ID, preserving the first response's watermark and interval. Metrics remain live; the watermark is not a database snapshot."""
        window(start, end)
        data = admin_application.repositories(
            runtime,
            access=access,
            start=start,
            end=end,
            limit=limit,
            search=search,
            after_id=after_id,
            watermark_id=watermark_id,
        )
        return Report(data=data, generated_at=data.generated_at)

    def reviews(
        access: Annotated[IntegrationAccessRequest, Depends(current_access)],
        start: datetime,
        end: datetime,
        limit: Limit = 50,
        repository: Repository = None,
        status: admin_reporting.HistoryStatus = "all",
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
        watermark_id: Watermark = None,
    ) -> Report[admin_reporting.HistoryPage]:
        """Outcome metadata without review text. Page by descending run ID using next_cursor as before_id and retain watermark_id. Active status reports current active work, matching the console."""
        window(start, end)
        data = admin_application.history(
            runtime,
            access=access,
            start=start,
            end=end,
            limit=limit,
            repository=repository,
            status=status,
            before_id=before_id,
            watermark_id=watermark_id,
        )
        return Report(data=data, generated_at=data.generated_at)

    def review_content(
        access: Annotated[IntegrationAccessRequest, Depends(current_access)],
        run_id: Annotated[int, Path(ge=1, le=9223372036854775807)],
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> Report[admin_reporting.ReviewDetail]:
        """Published review text requires a separate content grant. Repository ownership is checked on every read; content and links have explicit truncation flags."""
        data = admin_application.review_detail(
            runtime, access=access, run_id=run_id, before_id=before_id
        )
        if data is None:
            raise HTTPException(
                404, "This review is unavailable in the integration scope"
            )
        return Report(data=data, generated_at=data.generated_at)

    def quality(
        access: Annotated[IntegrationAccessRequest, Depends(current_access)],
        start: datetime,
        end: datetime,
        repository: Repository = None,
    ) -> Report[quality_reporting.QualityReport]:
        """Recorded quality signals and their denominators, using console definitions."""
        window(start, end)
        data = admin_application.quality_report(
            runtime, access=access, start=start, end=end, days=30, repository=repository
        )
        return Report(data=data, generated_at=datetime.now(timezone.utc))

    def openapi(
        access: Annotated[IntegrationAccessRequest, Depends(current_access)],
    ) -> dict[str, object]:
        admin_application.authorize_integration_schema(runtime, access=access)
        return get_openapi(
            title="Review Agent integration reports", version="1", routes=router.routes
        )

    router.add_api_route("/overview", overview, methods=["GET"])
    router.add_api_route("/repositories", repositories, methods=["GET"])
    router.add_api_route("/reviews", reviews, methods=["GET"])
    router.add_api_route("/reviews/{run_id}/content", review_content, methods=["GET"])
    router.add_api_route("/quality", quality, methods=["GET"])
    router.add_api_route(
        "/openapi.json", openapi, methods=["GET"], include_in_schema=False
    )
    return router
