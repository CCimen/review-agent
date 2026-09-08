"""Administrator settings transport with revision preconditions and audit history."""

from typing import Annotated
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .admin_auth import AdminAuth, User
from .deployment_settings import DeploymentSettings
from .postgres import deployment_settings as store
from .postgres.runtime import PostgreSQLRuntime


class SaveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    settings: DeploymentSettings
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True, slots=True)
class DeploymentSettingsPage:
    settings: DeploymentSettings
    revision: int
    history: tuple[store.SettingsRevision, ...]
    next_before_id: int | None
    startup_loads: tuple[store.ServiceSettingsLoad, ...]


def create_router(runtime: PostgreSQLRuntime, auth: AdminAuth) -> APIRouter:
    router = APIRouter(
        prefix="/api/settings", dependencies=[Depends(auth.current_admin)]
    )

    def get_settings(
        before_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> DeploymentSettingsPage:
        with runtime.transaction() as connection:
            current = store.latest(connection)
            revisions = store.history(connection, before_id=before_id)
            loads = store.startup_loads(connection)
        return DeploymentSettingsPage(
            current.settings if current else DeploymentSettings.from_environment(),
            current.id if current else 0,
            revisions[:20],
            revisions[19].id if len(revisions) > 20 else None,
            loads,
        )

    def save_settings(
        body: SaveSettings, user: Annotated[User, Depends(auth.current_admin)]
    ) -> store.SettingsRevision:
        try:
            with runtime.transaction() as connection:
                return store.save(
                    connection,
                    settings=body.settings,
                    expected_revision=body.expected_revision,
                    actor=f"admin:{user.id}",
                    reason=body.reason,
                )
        except store.SettingsConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    router.add_api_route("", get_settings, methods=["GET"])
    router.add_api_route("", save_settings, methods=["PUT"])
    return router
