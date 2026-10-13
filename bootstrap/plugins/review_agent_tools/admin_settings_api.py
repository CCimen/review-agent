"""Administrator settings transport with revision preconditions and audit history."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import admin_application
from .admin_auth import AdminAuth
from .admin_model_application import require_offered_models
from .hermes_control import HermesControlClient, HermesControlError
from .postgres.model_connections import ConnectionConflict
from .postgres.team_access import AccessRequest
from .deployment_settings import DeploymentSettings
from .postgres import deployment_settings as store
from .postgres.runtime import PostgreSQLRuntime


class SaveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    settings: DeploymentSettings
    reason: str = Field(min_length=1, max_length=500)


def create_router(
    runtime: PostgreSQLRuntime,
    auth: AdminAuth,
    shared_control: HermesControlClient | None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/settings", dependencies=[Depends(auth.current_owner)]
    )

    def get_settings(
        access: Annotated[AccessRequest, Depends(auth.current_scope)],
        before_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> admin_application.DeploymentSettingsPage:
        return admin_application.deployment_settings(runtime, access=access, before_id=before_id)

    def save_settings(
        body: SaveSettings, access: Annotated[AccessRequest, Depends(auth.current_scope)]
    ) -> store.SettingsRevision:
        current = admin_application.deployment_settings(
            runtime, access=access, before_id=None
        ).settings
        chosen = (body.settings.model_provider, body.settings.model)
        if chosen != (current.model_provider, current.model):
            # A deployment default the shared account does not offer would fail
            # every inheriting review, so a change must be verified first.
            if shared_control is None:
                raise HTTPException(
                    409,
                    "Hermes provider control is not configured, so a model change "
                    "cannot be verified here. Set REVIEW_AGENT_MODEL in the deployment.",
                )
            try:
                require_offered_models(shared_control, (chosen,))
            except ConnectionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except HermesControlError as exc:
                raise HTTPException(
                    502,
                    "Hermes provider control did not list the account's models; "
                    "check the shared connection before retrying.",
                ) from exc
        try:
            return admin_application.save_deployment_settings(runtime, access=access, settings=body.settings, expected_revision=body.expected_revision, reason=body.reason)
        except store.SettingsConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    router.add_api_route("", get_settings, methods=["GET"])
    router.add_api_route("", save_settings, methods=["PUT"])
    return router
