"""Authenticated HTTP transport and static frontend for operator reports."""

import os
from datetime import datetime
from uuid import UUID
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Path as PathParameter,
    Query,
    Request,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import SQLAlchemyError
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from . import (
    admin_audit_api,
    admin_model_api,
    admin_repository_requests_api,
    admin_access_api,
    admin_application,
    admin_quality_api,
    admin_run_api,
    admin_provider_api,
    admin_settings_api,
    admin_deployment_api,
    admin_teams_api,
    model_connection_config,
    integration_api,
    admin_scim,
    admin_oidc,
)
from .admin_auth import AdminAuth
from .admin_email import email_cipher
from .admin_identity_config import IdentitySettings
from .build_info import BuildInfo, read_build_info
from .postgres import admin_operations, admin_reporting
from .postgres.team_access import AccessDenied, AccessRequest, ResourceNotFound
from .postgres.teams import TeamConflict
from .postgres.model_connections import ConnectionConflict
from .hermes_control import HermesControlError
from .postgres.runtime import (
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
    PostgreSQLRuntimeRole,
)
from .settings import ReviewAgentSettings


Days = Annotated[int, Query(ge=1, le=90)]
Limit = Annotated[int, Query(ge=1, le=100)]
Repository = Annotated[
    str | None, Query(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
]


def create_app(
    runtime: PostgreSQLRuntime,
    *,
    public_url: str,
    static_dir: Path,
) -> FastAPI:
    auth = AdminAuth(
        runtime.database_url,
        public_url,
        email_secret=email_cipher(os.environ.get("REVIEW_AGENT_EMAIL_SECRET_KEY", "")),
    )
    identity = IdentitySettings.load(os.environ)
    build = read_build_info()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await run_in_threadpool(runtime.open)
            yield
        finally:
            await auth.engine.dispose()
            await run_in_threadpool(runtime.close)

    app = FastAPI(
        title="Review Agent admin",
        version="1",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    router = APIRouter(dependencies=[Depends(auth.current_user)])

    async def response_headers(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("origin") != auth.origin
            and not request.url.path.startswith("/scim/v2/")
        ):
            response = JSONResponse(
                status_code=403, content={"detail": "Request origin is not allowed"}
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def database_unavailable(_request: Request, _error: Exception) -> JSONResponse:
        if _request.url.path.startswith("/scim/v2/"):
            return admin_scim.SCIMError(
                503, "Account data is temporarily unavailable"
            ).response()
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Review data is temporarily unavailable. Retry shortly."
            },
        )

    def repositories(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        days: Days = 30,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Limit = 50,
        search: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ) -> admin_reporting.RepositoryPage:
        try:
            return admin_application.repositories(
                runtime,
                access=access,
                days=days,
                start=start,
                end=end,
                limit=limit,
                search=search,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def history(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        days: Days = 30,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Limit = 50,
        repository: Repository = None,
        status: admin_reporting.HistoryStatus = "all",
        pr_number: Annotated[int | None, Query(ge=1, le=2147483647)] = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> admin_reporting.HistoryPage:
        try:
            return admin_application.history(
                runtime,
                access=access,
                days=days,
                start=start,
                end=end,
                limit=limit,
                repository=repository,
                status=status,
                pr_number=pr_number,
                before_id=before_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def review_detail(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        run_id: Annotated[int, PathParameter(ge=1, le=9223372036854775807)],
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> admin_reporting.ReviewDetail:
        result = admin_application.review_detail(
            runtime, access=access, run_id=run_id, before_id=before_id
        )
        if result is None:
            raise HTTPException(
                404, "This review request was not found in retained history."
            )
        return result

    def pull_requests(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        days: Days = 30,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Limit = 50,
        repository: Repository = None,
        status: admin_reporting.HistoryStatus = "all",
        pr_number: Annotated[int | None, Query(ge=1, le=2147483647)] = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> admin_reporting.PullRequestPage:
        try:
            return admin_application.pull_requests(
                runtime,
                access=access,
                days=days,
                start=start,
                end=end,
                limit=limit,
                repository=repository,
                status=status,
                pr_number=pr_number,
                before_id=before_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def overview(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        days: Days = 30,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> admin_operations.Overview:
        try:
            return admin_application.overview(
                runtime, access=access, days=days, start=start, end=end
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def operations(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> admin_operations.Operations:
        return admin_application.operations(runtime, access=access)

    def events(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        limit: Limit = 50,
        worker_id: UUID | None = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> admin_operations.WorkerEventPage:
        return admin_application.events(
            runtime,
            access=access,
            worker_id=worker_id,
            before_id=before_id,
            limit=limit,
        )

    def version() -> BuildInfo:
        return build

    def openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    def health() -> dict[str, str]:
        runtime.readiness()
        return {"status": "ready"}

    def invalid_request(_request: Request, error: Exception) -> JSONResponse:
        if _request.url.path.startswith("/scim/v2/"):
            return admin_scim.SCIMError(
                400, "Invalid SCIM request", "invalidValue"
            ).response()
        if not isinstance(error, RequestValidationError):
            raise error
        # Validation errors must not echo submitted passwords or session values.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                    for item in error.errors()
                ]
            },
        )

    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    def api_docs() -> FileResponse:
        return FileResponse(static_dir / "api-docs.html")

    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[auth.hostname, "127.0.0.1", "localhost"]
    )
    app.add_middleware(admin_scim.SCIMBodyLimit)
    app.middleware("http")(response_headers)
    app.add_exception_handler(PostgreSQLRuntimeError, database_unavailable)
    app.add_exception_handler(SQLAlchemyError, database_unavailable)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(admin_scim.SCIMError, admin_scim.error_response)

    async def http_error(request: Request, error: Exception) -> Response:
        if not isinstance(error, StarletteHTTPException):
            raise error
        if request.url.path.startswith("/scim/v2/"):
            return admin_scim.SCIMError(error.status_code, str(error.detail)).response()
        return await http_exception_handler(request, error)

    app.add_exception_handler(StarletteHTTPException, http_error)

    def access_denied(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=error.status if isinstance(error, AccessDenied) else 404,
            content={"detail": str(error)},
            headers={"WWW-Authenticate": "Bearer"}
            if isinstance(error, AccessDenied)
            and error.status == 401
            and _request.url.path.startswith("/api/v1/")
            else None,
        )

    def team_conflict(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    app.add_exception_handler(AccessDenied, access_denied)
    app.add_exception_handler(ResourceNotFound, access_denied)
    app.add_exception_handler(TeamConflict, team_conflict)
    app.add_exception_handler(ConnectionConflict, team_conflict)

    def provider_unavailable(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "detail": "Managed Hermes provider control is unavailable. Check the connection status before retrying."
            },
        )

    app.add_exception_handler(HermesControlError, provider_unavailable)
    router.add_api_route("/api/repositories", repositories, methods=["GET"])
    router.add_api_route("/api/version", version, methods=["GET"])
    router.add_api_route("/api/history", history, methods=["GET"])
    router.add_api_route("/api/history/{run_id}", review_detail, methods=["GET"])
    router.add_api_route("/api/pull-requests", pull_requests, methods=["GET"])
    router.add_api_route("/api/overview", overview, methods=["GET"])
    router.add_api_route(
        "/api/operations",
        operations,
        methods=["GET"],
        dependencies=[Depends(auth.current_admin)],
    )
    router.add_api_route(
        "/api/operations/events",
        events,
        methods=["GET"],
        dependencies=[Depends(auth.current_admin)],
    )
    router.add_api_route(
        "/api/openapi.json", openapi, methods=["GET"], include_in_schema=False
    )
    router.add_api_route(
        "/api/docs", api_docs, methods=["GET"], include_in_schema=False
    )
    app.add_api_route("/healthz", health, methods=["GET"], include_in_schema=False)
    app.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/history", index, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/history/{run_id}", index, methods=["GET"], include_in_schema=False
    )
    app.add_api_route("/overview", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/operations", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/users", index, methods=["GET"], include_in_schema=False)
    for path in (
        "/teams",
        "/teams/{team_id}",
        "/repository-requests",
        "/audit",
        "/integrations",
        "/model-connections",
        "/model-connections/{connection_id}",
    ):
        app.add_api_route(path, index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/account", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/repositories", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/access", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/quality", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/settings", index, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/findings/{fingerprint}", index, methods=["GET"], include_in_schema=False
    )
    app.include_router(auth.auth_router, prefix="/api/auth")
    app.include_router(auth.router)
    app.include_router(admin_scim.create_router(auth, identity))
    app.include_router(admin_oidc.create_router(auth, identity))
    app.include_router(router)
    app.include_router(admin_quality_api.create_router(runtime, auth))
    app.include_router(admin_run_api.create_router(runtime, auth))
    controls = model_connection_config.load_controls(os.environ)
    app.include_router(
        admin_provider_api.create_router(auth, controls["shared"].client)
    )
    app.include_router(admin_model_api.create_router(runtime, auth, controls))
    app.include_router(admin_settings_api.create_router(runtime, auth))
    app.include_router(admin_deployment_api.create_router(auth))
    app.include_router(admin_access_api.create_router(runtime, auth))
    app.include_router(admin_teams_api.create_router(runtime, auth))
    app.include_router(admin_audit_api.create_router(runtime, auth))
    app.include_router(admin_repository_requests_api.create_router(runtime, auth))
    app.include_router(integration_api.create_management_router(runtime, auth))
    app.include_router(integration_api.create_report_router(runtime))
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir / "assets", check_dir=False),
        name="assets",
    )
    return app


def app_from_environment() -> FastAPI:
    settings = ReviewAgentSettings.from_environment()
    runtime = PostgreSQLRuntime(
        settings.postgres_database_url, role=PostgreSQLRuntimeRole.OPERATOR
    )
    return create_app(
        runtime,
        public_url=os.environ.get("REVIEW_AGENT_ADMIN_PUBLIC_URL", ""),
        static_dir=Path(__file__).resolve().parents[3] / "admin" / "dist",
    )
