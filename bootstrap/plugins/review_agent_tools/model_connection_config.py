"""Operator-provisioned Hermes control endpoints; never supplied by console users."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import cast

from .hermes_control import HermesControlClient, HermesControlConfigurationError


class ConnectionConfigurationError(ValueError):
    """The server's managed connection configuration is invalid."""


@dataclass(frozen=True, slots=True)
class ManagedControl:
    key: str
    client: HermesControlClient | None = field(repr=False)


def load_controls(environment: Mapping[str, str]) -> dict[str, ManagedControl]:
    """Read a bounded startup configuration and preserve the shared default."""
    url = environment.get("REVIEW_AGENT_HERMES_CONTROL_URL", "").strip()
    token = environment.get("REVIEW_AGENT_HERMES_CONTROL_TOKEN", "").strip()
    if bool(url) != bool(token):
        raise ConnectionConfigurationError(
            "Shared Hermes control requires both its URL and token"
        )
    try:
        shared = HermesControlClient(url, token) if url else None
    except HermesControlConfigurationError as exc:
        raise ConnectionConfigurationError(
            "REVIEW_AGENT_HERMES_CONTROL_URL must be one HTTP origin"
        ) from exc
    controls = {"shared": ManagedControl("shared", shared)}
    filename = environment.get("REVIEW_AGENT_CONNECTIONS_FILE", "").strip()
    if not filename:
        return controls
    try:
        with Path(filename).open("rb") as handle:
            content = handle.read(256 * 1024 + 1)
        if len(content) > 256 * 1024:
            raise ValueError("Configuration is too large")
        raw: object = json.loads(content)
        if not isinstance(raw, list):
            raise ValueError("Configuration must be an array")
        rows = cast(list[object], raw)
        if len(rows) > 1000:
            raise ValueError("Too many connections")
        origins = {shared.base_url} if shared else set[str]()
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                raise ValueError("Invalid connection")
            row = cast(dict[str, object], raw_row)
            if set(row) != {"key", "control_url", "token_env"}:
                raise ValueError("Invalid connection fields")
            key, origin, token_env = row["key"], row["control_url"], row["token_env"]
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[a-z][a-z0-9-]{0,62}", key) is None
                or key in controls
            ):
                raise ValueError("Invalid or duplicate runtime key")
            if (
                not isinstance(origin, str)
                or not isinstance(token_env, str)
                or re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", token_env) is None
            ):
                raise ValueError("Invalid control configuration")
            secret = environment.get(token_env, "").strip()
            if not secret:
                raise ValueError("Control token is unavailable")
            client = HermesControlClient(origin, secret)
            if client.base_url in origins:
                raise ValueError(
                    "A control endpoint cannot represent multiple connections"
                )
            origins.add(client.base_url)
            controls[key] = ManagedControl(key, client)
    except (OSError, ValueError) as exc:
        raise ConnectionConfigurationError(
            "Managed Hermes connection configuration is invalid"
        ) from exc
    return controls
