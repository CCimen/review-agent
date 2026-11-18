"""Authenticated HTTP transport and static frontend for operator reports."""

import os
from datetime import datetime
from uuid import UUID
from collections.abc import AsyncGenerator
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

from .api import (
    audit as audit_api,
    models as models_api,
    repository_requests as repository_requests_api,
    access as access_api,
    quality as quality_api,
    runs as runs_api,
    providers as providers_api,
    settings as settings_api,
    deployment as deployment_api,
    teams as teams_api,
    integrations as integrations_api,
)
from . import application as console_application, scim as console_scim, oidc as console_oidc
from ..hermes import control_endpoints as hermes_control_endpoints
from .auth import AdminAuth
from .email import email_cipher
from .identity_config import IdentitySettings
from ..build_info import BuildInfo, read_build_info
from ..domain.review import ReviewPurpose
from ..review.contract import source_checkout
from ..postgres import (
    history as postgres_history,
    operations as postgres_operations,
    usage as postgres_usage,
)
from ..postgres.team_access import AccessDenied, AccessRequest, ResourceNotFound
from ..postgres.teams import TeamConflict
from ..postgres.model_connections import ConnectionConflict
from ..hermes.control import HermesControlError
from ..postgres.runtime import (
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
    PostgreSQLRuntimeRole,
)
from ..settings import ReviewAgentSettings


# Where admin/Dockerfile copies the built frontend in the console image.
IMAGE_STATIC_DIR = Path("/app/admin/dist")

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
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
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
            return console_scim.SCIMError(
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
    ) -> postgres_history.RepositoryPage:
        try:
            return console_application.repositories(
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
        purpose: ReviewPurpose | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Limit = 50,
        repository: Repository = None,
        status: postgres_history.HistoryStatus = "all",
        pr_number: Annotated[int | None, Query(ge=1, le=2147483647)] = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> postgres_history.HistoryPage:
        try:
            return console_application.history(
                runtime,
                access=access,
                purpose=purpose,
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
        purpose: ReviewPurpose | None = None,
    ) -> postgres_history.ReviewDetail:
        result = console_application.review_detail(
            runtime, access=access, run_id=run_id, before_id=before_id, purpose=purpose
        )
        if result is None:
            raise HTTPException(
                404, "This review request was not found in retained history."
            )
        return result

    def pull_requests(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        days: Days = 30,
        purpose: ReviewPurpose | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Limit = 50,
        repository: Repository = None,
        status: postgres_history.HistoryStatus = "all",
        pr_number: Annotated[int | None, Query(ge=1, le=2147483647)] = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> postgres_history.PullRequestPage:
        try:
            return console_application.pull_requests(
                runtime,
                access=access,
                purpose=purpose,
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
        purpose: ReviewPurpose | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> postgres_operations.Overview:
        try:
            return console_application.overview(
                runtime, access=access, days=days, start=start, end=end, purpose=purpose
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def usage(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        days: Days = 30,
        purpose: ReviewPurpose | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        dimension: postgres_usage.UsageDimension = "repository",
        sort: postgres_usage.UsageSort = "requests",
        repository: Repository = None,
        requester: Annotated[str | None, Query(max_length=200)] = None,
        search: Annotated[str, Query(max_length=200)] = "",
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        limit: Limit = 25,
    ) -> postgres_usage.UsageReport:
        """Usage for owners and admins, grouped by team, repository or GitHub login.

        A requester narrows every grouping to the reviews one GitHub login asked
        for, so a team or repository breakdown can be read for one person.
        The period selects retained requests by start time. Outcomes and all
        reported attempt tokens reflect the current snapshot, including retries
        and later reports. Teams follow current repository ownership. Null token
        totals mean no usage was reported; reported/started attempts show coverage.
        """
        try:
            return console_application.usage(
                runtime,
                access=access,
                purpose=purpose,
                days=days,
                start=start,
                end=end,
                dimension=dimension,
                sort=sort,
                repository=repository,
                requester=requester,
                search=search,
                offset=offset,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def operations(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
    ) -> postgres_operations.Operations:
        return console_application.operations(runtime, access=access)

    def events(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        limit: Limit = 50,
        worker_id: UUID | None = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> postgres_operations.WorkerEventPage:
        return console_application.events(
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
            return console_scim.SCIMError(
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
    app.add_middleware(console_scim.SCIMBodyLimit)
    app.middleware("http")(response_headers)
    app.add_exception_handler(PostgreSQLRuntimeError, database_unavailable)
    app.add_exception_handler(SQLAlchemyError, database_unavailable)
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(console_scim.SCIMError, console_scim.error_response)

    async def http_error(request: Request, error: Exception) -> Response:
        if not isinstance(error, StarletteHTTPException):
            raise error
        if request.url.path.startswith("/scim/v2/"):
            return console_scim.SCIMError(error.status_code, str(error.detail)).response()
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
        "/api/usage",
        usage,
        methods=["GET"],
        dependencies=[Depends(auth.current_admin)],
    )
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
    app.add_api_route("/usage", index, methods=["GET"], include_in_schema=False)
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
        "/documentation",
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
    app.include_router(console_scim.create_router(auth, identity))
    app.include_router(console_oidc.create_router(auth, identity))
    app.include_router(router)
    app.include_router(quality_api.create_router(runtime, auth))
    app.include_router(runs_api.create_router(runtime, auth))
    controls = hermes_control_endpoints.load_controls(os.environ)
    app.include_router(
        providers_api.create_router(auth, controls["shared"].client)
    )
    app.include_router(models_api.create_router(runtime, auth, controls))
    app.include_router(settings_api.create_router(runtime, auth))
    app.include_router(deployment_api.create_router(auth))
    app.include_router(access_api.create_router(runtime, auth))
    app.include_router(teams_api.create_router(runtime, auth))
    app.include_router(audit_api.create_router(runtime, auth))
    app.include_router(repository_requests_api.create_router(runtime, auth))
    app.include_router(integrations_api.create_management_router(runtime, auth))
    app.include_router(integrations_api.create_report_router(runtime))
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir / "assets", check_dir=False),
        name="assets",
    )
    return app


def _built_frontend() -> Path:
    """A checkout serves its own admin/dist; an installed package the image's."""
    checkout = source_checkout()
    return checkout / "admin" / "dist" if checkout is not None else IMAGE_STATIC_DIR


def app_from_environment() -> FastAPI:
    settings = ReviewAgentSettings.from_environment()
    runtime = PostgreSQLRuntime(
        settings.postgres_database_url, role=PostgreSQLRuntimeRole.OPERATOR
    )
    return create_app(
        runtime,
        public_url=os.environ.get("REVIEW_AGENT_ADMIN_PUBLIC_URL", ""),
        static_dir=_built_frontend(),
    )
