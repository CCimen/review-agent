"""Authenticated HTTP transport and static frontend for operator reports."""

import os
from datetime import datetime
from uuid import UUID
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy.exc import SQLAlchemyError
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from . import admin_application
from .admin_auth import AdminAuth
from .postgres import admin_operations, admin_reporting
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
    auth = AdminAuth(runtime.database_url, public_url)

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
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Review data is temporarily unavailable. Retry shortly."
            },
        )

    def repositories(
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

    def pull_requests(
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
        days: Days = 30,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> admin_operations.Overview:
        try:
            return admin_application.overview(runtime, days=days, start=start, end=end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def operations() -> admin_operations.Operations:
        return admin_application.operations(runtime)

    def events(
        limit: Limit = 50,
        worker_id: UUID | None = None,
        before_id: Annotated[int | None, Query(ge=1, le=9223372036854775807)] = None,
    ) -> admin_operations.WorkerEventPage:
        return admin_application.events(
            runtime, worker_id=worker_id, before_id=before_id, limit=limit
        )

    def openapi() -> JSONResponse:
        return JSONResponse(app.openapi())

    def health() -> dict[str, str]:
        runtime.readiness()
        return {"status": "ready"}

    def invalid_request(_request: Request, error: Exception) -> JSONResponse:
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

    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[auth.hostname, "127.0.0.1", "localhost"]
    )
    app.middleware("http")(response_headers)
    app.add_exception_handler(PostgreSQLRuntimeError, database_unavailable)
    app.add_exception_handler(SQLAlchemyError, database_unavailable)
    app.add_exception_handler(RequestValidationError, invalid_request)
    router.add_api_route("/api/repositories", repositories, methods=["GET"])
    router.add_api_route("/api/history", history, methods=["GET"])
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
    app.add_api_route("/healthz", health, methods=["GET"], include_in_schema=False)
    app.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/history", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/overview", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/operations", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/users", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/account", index, methods=["GET"], include_in_schema=False)
    app.include_router(auth.auth_router, prefix="/api/auth")
    app.include_router(auth.router)
    app.include_router(router)
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
