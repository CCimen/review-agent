"""Admin-only provider authentication transport backed by Hermes."""

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from .admin_auth import AdminAuth
from .hermes_control import (
    Cancellation,
    HermesControlClient,
    HermesControlConfigurationError,
    HermesControlError,
    LoginSession,
    ProviderModel,
    ProviderStatus,
    HermesRuntimeStatus,
)


class ProviderCapability(BaseModel):
    configured: bool
    detail: str


class ProviderPage(BaseModel):
    capability: ProviderCapability
    items: list[ProviderStatus]


class ProviderModelPage(BaseModel):
    capability: ProviderCapability
    items: list[ProviderModel]


class RuntimePage(BaseModel):
    capability: ProviderCapability
    runtime: HermesRuntimeStatus | None


SessionId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{22,80}$")]


def create_router(auth: AdminAuth) -> APIRouter:
    """Create the fixed Hermes provider-control routes."""
    url = os.environ.get("REVIEW_AGENT_HERMES_CONTROL_URL", "")
    token = os.environ.get("REVIEW_AGENT_HERMES_CONTROL_TOKEN", "")
    client: HermesControlClient | None = None
    if url and token:
        try:
            client = HermesControlClient(url, token)
        except HermesControlConfigurationError:
            client = None
    capability = ProviderCapability(
        configured=client is not None,
        detail=(
            "Hermes provider control is configured."
            if client is not None
            else "Hermes provider control is not configured."
        ),
    )
    router = APIRouter(
        prefix="/api/providers",
        dependencies=[Depends(auth.current_owner)],
        tags=["providers"],
    )

    def control() -> HermesControlClient:
        if client is None:
            raise HTTPException(503, capability.detail)
        return client

    def failed(error: HermesControlError) -> HTTPException:
        return HTTPException(
            502, "Hermes provider control returned an invalid response."
        )

    def providers() -> ProviderPage:
        if client is None:
            return ProviderPage(capability=capability, items=[])
        try:
            return ProviderPage(
                capability=capability,
                items=list(control().provider_statuses()),
            )
        except HermesControlError as exc:
            raise failed(exc) from exc

    def models() -> ProviderModelPage:
        if client is None:
            return ProviderModelPage(capability=capability, items=[])
        try:
            return ProviderModelPage(
                capability=capability,
                items=list(control().models()),
            )
        except HermesControlError as exc:
            raise failed(exc) from exc

    def start_login() -> LoginSession:
        try:
            return control().start_codex_login()
        except HermesControlError as exc:
            raise failed(exc) from exc

    def runtime_status() -> RuntimePage:
        if client is None:
            return RuntimePage(capability=capability, runtime=None)
        try:
            return RuntimePage(
                capability=capability, runtime=control().runtime_status()
            )
        except HermesControlError as exc:
            raise failed(exc) from exc

    def poll_login(session_id: SessionId) -> LoginSession:
        try:
            return control().poll_codex_login(session_id)
        except HermesControlError as exc:
            raise failed(exc) from exc

    def cancel_login(session_id: SessionId) -> Cancellation:
        try:
            return control().cancel_codex_login(session_id)
        except HermesControlError as exc:
            raise failed(exc) from exc

    router.add_api_route("", providers, methods=["GET"])
    router.add_api_route("/models", models, methods=["GET"])
    router.add_api_route("/runtime", runtime_status, methods=["GET"])
    router.add_api_route("/openai-codex/login", start_login, methods=["POST"])
    router.add_api_route(
        "/openai-codex/login/{session_id}", poll_login, methods=["GET"]
    )
    router.add_api_route(
        "/openai-codex/login/{session_id}/cancel",
        cancel_login,
        methods=["POST"],
    )
    return router
