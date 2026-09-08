"""Admin-only provider authentication transport backed by Hermes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .admin_auth import AdminAuth
from .hermes_control import (
    HermesControlClient,
    HermesControlError,
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


def create_router(auth: AdminAuth, client: HermesControlClient | None) -> APIRouter:
    """Create the fixed Hermes provider-control routes."""
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

    def runtime_status() -> RuntimePage:
        if client is None:
            return RuntimePage(capability=capability, runtime=None)
        try:
            return RuntimePage(
                capability=capability, runtime=control().runtime_status()
            )
        except HermesControlError as exc:
            raise failed(exc) from exc

    router.add_api_route("", providers, methods=["GET"])
    router.add_api_route("/models", models, methods=["GET"])
    router.add_api_route("/runtime", runtime_status, methods=["GET"])
    return router
