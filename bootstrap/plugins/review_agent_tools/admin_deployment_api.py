"""Read container state for one server-configured Dokploy Compose application."""

import json
import os
import re
from datetime import datetime, timezone
from typing import cast
from urllib import error, parse, request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .admin_auth import AdminAuth


class DeploymentContainer(BaseModel):
    container_id: str
    service: str
    state: str
    status: str


class DeploymentStatus(BaseModel):
    configured: bool
    application: str | None = None
    dashboard_url: str | None = None
    containers: list[DeploymentContainer]
    observed_at: datetime


class DokployError(ValueError):
    """Dokploy cannot provide a bounded deployment snapshot."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _text(value: object, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise DokployError("Invalid deployment response")
    return value


class DokployDeployment:
    def __init__(self, origin: str, api_key: str, compose_id: str) -> None:
        parsed = parse.urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise DokployError("Dokploy URL must be one HTTP origin")
        self.origin = origin.rstrip("/")
        self._api_key = _text(api_key, 4096)
        self._compose_id = _text(compose_id)
        self._opener = request.build_opener(_NoRedirect())

    def _read(self, endpoint: str, parameters: dict[str, str]) -> object:
        call = request.Request(
            self.origin + "/api/" + endpoint + "?" + parse.urlencode(parameters),
            headers={"x-api-key": self._api_key, "Accept": "application/json"},
        )
        try:
            with self._opener.open(call, timeout=10) as response:
                body = response.read(524289)
        except (OSError, error.URLError) as exc:
            raise DokployError("Dokploy is unavailable") from exc
        if len(body) > 524288:
            raise DokployError("Deployment response exceeds the limit")
        try:
            return cast(object, json.loads(body))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DokployError("Invalid deployment response") from exc

    def status(self) -> DeploymentStatus:
        raw = self._read("compose.one", {"composeId": self._compose_id})
        if not isinstance(raw, dict):
            raise DokployError("Invalid deployment response")
        compose = cast(dict[str, object], raw)
        app_name = _text(compose.get("appName"), 63)
        if re.fullmatch(r"[A-Za-z0-9._-]+", app_name) is None:
            raise DokployError("Invalid deployment identity")
        app_type = compose.get("composeType")
        if app_type not in {"docker-compose", "stack"}:
            raise DokployError("Unsupported deployment type")
        parameters = {"appName": app_name, "appType": str(app_type)}
        if compose.get("serverId") is not None:
            parameters["serverId"] = _text(compose["serverId"])
        raw_rows = self._read("docker.getContainersByAppNameMatch", parameters)
        if not isinstance(raw_rows, list):
            raise DokployError("Invalid deployment containers")
        rows = cast(list[object], raw_rows)
        if len(rows) > 200:
            raise DokployError("Deployment has more than 200 container records")
        containers: list[DeploymentContainer] = []
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                raise DokployError("Invalid deployment container")
            row = cast(dict[str, object], raw_row)
            name = _text(row.get("name"))
            # appType selects Dokploy's exact Compose project label filter.
            prefix = app_name + ("-" if app_type == "docker-compose" else "_")
            service = name.removeprefix(prefix)
            containers.append(
                DeploymentContainer(
                    container_id=_text(row.get("containerId")),
                    service=service,
                    state=_text(row.get("state")),
                    status=_text(row.get("status")),
                )
            )
        return DeploymentStatus(
            configured=True,
            application=app_name,
            dashboard_url=self.origin + "/dashboard",
            containers=containers,
            observed_at=datetime.now(timezone.utc),
        )


def create_router(auth: AdminAuth) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth.current_admin)])
    values = tuple(
        os.environ.get(name, "").strip()
        for name in (
            "REVIEW_AGENT_DOKPLOY_URL",
            "REVIEW_AGENT_DOKPLOY_API_KEY",
            "REVIEW_AGENT_DOKPLOY_COMPOSE_ID",
        )
    )
    deployment = DokployDeployment(*values) if all(values) else None

    def status() -> DeploymentStatus:
        if deployment is None:
            return DeploymentStatus(
                configured=False, containers=[], observed_at=datetime.now(timezone.utc)
            )
        try:
            return deployment.status()
        except DokployError as exc:
            raise HTTPException(
                502, "Dokploy container state is unavailable. Retry shortly."
            ) from exc

    router.add_api_route("/api/deployment", status, methods=["GET"])
    return router
