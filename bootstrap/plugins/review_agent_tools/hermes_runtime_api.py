"""Native Hermes endpoints for account observations and fenced review execution."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
import hmac
import logging
import os
import re
from typing import cast
from uuid import uuid4

from aiohttp import web
import psycopg

from . import review_contract, review_tool_runtime
from .hermes_control import MANAGED_REVIEW_PATH
from .model_accounts import AccountAvailability, ModelProvider, read_accounts
from .postgres import jobs
from .postgres.runtime import PostgreSQLRuntimeError


logger = logging.getLogger(__name__)
STATUS_PATH = "/v1/review-agent/status"
_MAX_REQUEST_BYTES = 1024 * 1024


def wire_api(native: object, _adapter: object) -> None:
    """Register through Hermes' supported api_server platform-handler hook."""
    if not isinstance(native, web.Application):
        raise ValueError("Hermes API application is unavailable")
    token = os.environ.get("API_SERVER_KEY", "").strip()
    runtime_key = os.environ.get("REVIEW_AGENT_MODEL_CONNECTION", "shared")
    if not token or re.fullmatch(r"[a-z][a-z0-9-]{0,62}", runtime_key) is None:
        raise ValueError("Managed Hermes runtime configuration is invalid")
    installed = review_contract.load_installed_contract()
    instance = uuid4()
    handler = next(
        (
            route.handler
            for route in native.router.routes()
            if route.method == "POST"
            and route.resource is not None
            and route.resource.canonical == "/v1/chat/completions"
        ),
        None,
    )
    if handler is None:
        raise ValueError("Pinned Hermes chat handler is unavailable")
    chat: Callable[[web.Request], Awaitable[web.StreamResponse]] = handler

    def authorized(request: web.Request) -> bool:
        return hmac.compare_digest(
            request.headers.get("Authorization", "").encode(),
            f"Bearer {token}".encode(),
        )

    def snapshot() -> dict[str, object]:
        return {
            "runtime_key": runtime_key,
            "instance_id": str(instance),
            "contract": installed.to_json(),
            "accounts": [asdict(account) for account in read_accounts()],
        }

    async def status(request: web.Request) -> web.Response:
        if not authorized(request):
            raise web.HTTPUnauthorized()
        try:
            return web.json_response(
                await asyncio.to_thread(snapshot), headers={"Cache-Control": "no-store"}
            )
        except Exception as exc:
            logger.warning(
                "Managed Hermes account observation failed: %s", type(exc).__name__
            )
            raise web.HTTPServiceUnavailable(
                text="Managed account status is unavailable"
            ) from exc

    def activate(session: jobs.WorkerLeaseSession, body: dict[str, object]) -> bool:
        provider = ModelProvider(body.get("provider"))
        model = body.get("model")
        options = body.get("model_options")
        if (
            set(body) != {"messages", "stream", "provider", "model", "model_options"}
            or body.get("stream") is not False
            or not isinstance(model, str)
            or not 1 <= len(model) <= 200
            or not isinstance(options, dict)
            or set(cast(dict[str, object], options)) != {"reasoning"}
        ):
            raise jobs.ReviewJobError(
                "Review request does not match the worker contract"
            )
        reasoning = cast(dict[str, object], options).get("reasoning")
        if not isinstance(reasoning, dict):
            raise jobs.ReviewJobError("Review reasoning is invalid")
        reason_values = cast(dict[str, object], reasoning)
        effort = reason_values.get("effort")
        if (
            not isinstance(effort, str)
            or effort not in review_contract.REASONING_EFFORTS
            or reason_values != {"enabled": effort != "none", "effort": effort}
            or type(reason_values.get("enabled")) is not bool
        ):
            raise jobs.ReviewJobError("Review reasoning is invalid")
        accounts = read_accounts()
        if any(
            account.availability
            in {
                AccountAvailability.MULTIPLE_ACCOUNTS,
                AccountAvailability.ISOLATION_REQUIRED,
            }
            for account in accounts
        ):
            raise jobs.ReviewJobError(
                "A model connection requires its own home and one account per provider"
            )
        account = next(item for item in accounts if item.provider is provider)
        if (
            account.availability is not AccountAvailability.AVAILABLE
            or account.identity_sha256 is None
        ):
            raise jobs.ReviewJobError("The assigned provider account is unavailable")
        with review_tool_runtime.postgres_runtime().transaction() as connection:
            configuration = jobs.begin_model_execution(
                connection,
                session=session,
                runtime_key=runtime_key,
                runtime_instance=instance,
                provider=provider.value,
                model=model,
                reasoning_effort=effort,
                identity_sha256=account.identity_sha256,
            )
            if configuration is None:
                return False
            review_contract.require_matching_execution_contract(
                configuration, installed
            )
        return True

    def finished(session: jobs.WorkerLeaseSession) -> None:
        try:
            with review_tool_runtime.postgres_runtime().transaction() as connection:
                jobs.finish_model_execution(
                    connection, session=session, runtime_instance=instance
                )
        except (psycopg.Error, PostgreSQLRuntimeError) as exc:
            # Keep the durable reservation when completion could not be recorded.
            logger.warning(
                "Hermes execution completion could not be recorded: %s",
                type(exc).__name__,
            )

    async def review(request: web.Request) -> web.StreamResponse:
        if not authorized(request):
            raise web.HTTPUnauthorized()
        if (
            request.content_length is None
            or request.content_length > _MAX_REQUEST_BYTES
        ):
            raise web.HTTPRequestEntityTooLarge(
                max_size=_MAX_REQUEST_BYTES, actual_size=request.content_length or 0
            )
        try:
            session_id = request.headers.get("X-Hermes-Session-Id", "")
            session = jobs.WorkerLeaseSession.parse(session_id)
            if session is None or request.headers.get("Idempotency-Key") != session_id:
                raise jobs.ReviewJobError("An exact worker lease is required")
            body: object = await request.json()
            if not isinstance(body, dict):
                raise jobs.ReviewJobError("Review request must be an object")
            activated = await asyncio.to_thread(
                activate, session, cast(dict[str, object], body)
            )
            if not activated:
                return web.json_response(
                    {
                        "error": {
                            "code": "review_connection_paused",
                            "message": "The unstarted review was returned to its paused connection's queue.",
                        }
                    },
                    status=409,
                )
        except ValueError:
            return web.json_response(
                {
                    "error": {
                        "code": "review_assignment_changed",
                        "message": "The review lease, connection, or provider account is unavailable.",
                    }
                },
                status=409,
            )
        except (psycopg.Error, PostgreSQLRuntimeError) as exc:
            logger.warning("Hermes execution admission failed: %s", type(exc).__name__)
            raise web.HTTPServiceUnavailable(
                text="Review execution could not be admitted"
            ) from exc
        # The pinned non-streaming handler awaits its executor before returning,
        # including ordinary error responses and HTTP rejection exceptions.
        # Cancellation and unexpected exceptions do not prove inference stopped.
        try:
            response = await chat(request)
        except web.HTTPException:
            await asyncio.shield(asyncio.to_thread(finished, session))
            raise
        await asyncio.shield(asyncio.to_thread(finished, session))
        return response

    native.router.add_get(STATUS_PATH, status)
    native.router.add_post(MANAGED_REVIEW_PATH, review)
