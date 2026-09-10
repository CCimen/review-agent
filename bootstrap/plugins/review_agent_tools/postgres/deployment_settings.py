"""Append-only deployment policy revisions and explicit startup observations."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ..deployment_settings import DeploymentSettings
from .runtime import PostgreSQLRuntime


# Older saved revisions retain deployment defaults for controls introduced later.
_EXTENDED_ENV = frozenset(
    {
        "REVIEW_AGENT_JOB_PRIORITY",
        "REVIEW_AGENT_JOB_PRIORITY_AGING_SECONDS",
        "REVIEW_AGENT_JOB_RETRY_SECONDS",
        "REVIEW_AGENT_JOB_POLL_SECONDS",
        "REVIEW_AGENT_JOB_RECOVERY_SECONDS",
        "REVIEW_AGENT_JOB_RECOVERY_BATCH_SIZE",
        "REVIEW_AGENT_PUBLICATION_LEASE_SECONDS",
        "REVIEW_AGENT_PUBLICATION_HEARTBEAT_SECONDS",
        "REVIEW_AGENT_PUBLICATION_RETRY_SECONDS",
        "REVIEW_AGENT_PUBLICATION_POLL_SECONDS",
        "REVIEW_AGENT_GITHUB_APP_ADMISSION_MAX_AGE_SECONDS",
        "REVIEW_AGENT_GITHUB_APP_MAX_BODY_BYTES",
        "REVIEW_AGENT_ADMISSION_MAX_CONCURRENT_REQUESTS",
        "REVIEW_AGENT_ADMISSION_REQUEST_TIMEOUT_SECONDS",
        "REVIEW_AGENT_GITHUB_GATEWAY_MAX_CONCURRENT_REQUESTS",
    }
)


class SettingsConflict(ValueError):
    """A later save superseded the editor's snapshot."""


@dataclass(frozen=True, slots=True)
class SettingsRevision:
    id: int
    settings: DeploymentSettings
    actor: str
    reason: str
    created_at: datetime


def _revision(row: tuple[object, ...]) -> SettingsRevision:
    raw = row[1]
    if not isinstance(raw, dict):
        raise ValueError("Stored deployment settings are invalid")
    env_raw = cast(dict[str, object], raw)
    known = set(DeploymentSettings().environment())
    if not known - _EXTENDED_ENV <= set(env_raw) <= known or any(
        not isinstance(v, str) for v in env_raw.values()
    ):
        raise ValueError("Stored deployment settings are invalid")
    env = cast(dict[str, str], env_raw)
    return SettingsRevision(
        int(str(row[0])),
        DeploymentSettings.from_environment({**os.environ, **env}),
        str(row[2]),
        str(row[3]),
        cast(datetime, row[4]),
    )


def latest(connection: psycopg.Connection[TupleRow]) -> SettingsRevision | None:
    row = connection.execute(
        "SELECT id, settings, actor, reason, created_at FROM review_agent.deployment_settings ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _revision(row) if row else None


def history(
    connection: psycopg.Connection[TupleRow], *, before_id: int | None = None
) -> tuple[SettingsRevision, ...]:
    rows = connection.execute(
        "SELECT id, settings, actor, reason, created_at FROM review_agent.deployment_settings WHERE (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT 21",
        (before_id, before_id),
    ).fetchall()
    return tuple(_revision(row) for row in rows)


def save(
    connection: psycopg.Connection[TupleRow],
    *,
    settings: DeploymentSettings,
    expected_revision: int,
    actor: str,
    reason: str,
) -> SettingsRevision:
    if not reason.strip() or len(reason.strip()) > 500:
        raise ValueError("A reason of at most 500 characters is required")
    connection.execute("SELECT pg_advisory_xact_lock(204913, 20)")
    current = latest(connection)
    if (current.id if current else 0) != expected_revision:
        raise SettingsConflict("Settings changed. Reload before saving.")
    row = connection.execute(
        "INSERT INTO review_agent.deployment_settings (settings, actor, reason) VALUES (%s, %s, %s) RETURNING id, settings, actor, reason, created_at",
        (Jsonb(settings.environment()), actor, reason.strip()),
    ).fetchone()
    assert row is not None
    return _revision(row)


Service = Literal["worker", "admission", "reviewer", "gateway", "publisher", "webhook"]


@dataclass(frozen=True, slots=True)
class ServiceSettingsLoad:
    service: Service
    hostname: str
    revision: int | None
    loaded_at: datetime


def startup_loads(
    connection: psycopg.Connection[TupleRow],
) -> tuple[ServiceSettingsLoad, ...]:
    rows = connection.execute(
        "SELECT service, hostname, revision, loaded_at FROM review_agent.service_settings_loads ORDER BY loaded_at DESC LIMIT 50"
    ).fetchall()
    return tuple(
        ServiceSettingsLoad(
            cast(Service, row[0]),
            str(row[1]),
            cast(int | None, row[2]),
            cast(datetime, row[3]),
        )
        for row in rows
    )


def apply_at_startup(runtime: PostgreSQLRuntime, service: Service) -> None:
    """Load managed operational values once; model routes are frozen at admission."""
    with runtime.transaction() as connection:
        revision = latest(connection)
        if revision:
            values = revision.settings.environment()
            # Installed model configuration remains part of the verified receipt.
            for name in (
                "REVIEW_AGENT_MODEL_PROVIDER",
                "REVIEW_AGENT_MODEL",
                "REVIEW_AGENT_REASONING_EFFORT",
            ):
                del values[name]
            os.environ.update(values)
        connection.execute(
            "INSERT INTO review_agent.service_settings_loads (instance_id, service, hostname, revision) VALUES (%s, %s, %s, %s)",
            (uuid4(), service, socket.gethostname(), revision.id if revision else None),
        )
