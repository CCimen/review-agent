"""Signed GitHub review admission into the PostgreSQL durable queue."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from .github_webhook import (
    CommandKind,
    GitHubWebhookError,
    UnsupportedGitHubEvent,
    normalize_event,
)
from . import review_contract
from .postgres import webhook_deliveries
from .postgres.runtime import PostgreSQLRuntime
from .settings import PostgresDatabaseUrl, ReviewAgentSettings, SettingsError


GITHUB_APP_PATH = "/webhooks/github-app"
DEFAULT_PORT = 8644

class AdmissionError(ValueError):
    """The signed request does not satisfy the admission contract."""


class GitHubAppDeliveryConflict(AdmissionError):
    """A GitHub delivery GUID was reused for different immutable input."""


class GitHubAppPayloadTooLarge(AdmissionError):
    """A signed App delivery cannot fit the durable normalized envelope."""


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    database_url: PostgresDatabaseUrl = field(repr=False)
    profile: str
    github_app_secret: str = field(repr=False)
    contract_environment: Mapping[str, str] = field(repr=False, compare=False)
    webhook_delivery_max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class WebhookReceiptResponse:
    status: str

    def to_json(self) -> bytes:
        return response_body(self.status)


def _integer_setting(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < 1:
        raise SettingsError(f"{name} must be positive")
    return value


def load_config(environment: Mapping[str, str] | None = None) -> AdmissionConfig:
    values = dict(environment if environment is not None else os.environ)
    settings = ReviewAgentSettings(values)
    github_app_secret = values.get(
        "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET", ""
    ).strip()
    if not github_app_secret:
        raise SettingsError("REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET is required")
    return AdmissionConfig(
        database_url=settings.postgres_database_url,
        profile=settings.profile,
        github_app_secret=github_app_secret,
        contract_environment=review_contract.deployment_environment(values),
        webhook_delivery_max_attempts=_integer_setting(
            values, "REVIEW_AGENT_WEBHOOK_DELIVERY_MAX_ATTEMPTS", 3
        ),
    )


def decode_request(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError("request body must be valid UTF-8 JSON") from exc


def ready_check(config: AdmissionConfig, runtime: PostgreSQLRuntime) -> dict[str, str]:
    if config.database_url != runtime.database_url:
        raise AdmissionError("admission runtime does not match its configured database")
    runtime.readiness()
    _admission_contract(config.profile, config.contract_environment)
    return {"status": "ready"}


def _admission_contract(
    profile: str, environment: Mapping[str, str]
) -> review_contract.ReviewContract:
    try:
        contract = review_contract.load_packaged_contract(
            profile,
            environment=environment,
        )
    except review_contract.ReviewContractError as exc:
        raise AdmissionError(str(exc)) from exc
    if contract.profile != profile:
        raise AdmissionError("configured profile does not match the packaged reviewer")
    return contract


def receive_github_app_delivery(
    *,
    body: bytes,
    payload: object,
    delivery_id: str,
    event: str,
    config: AdmissionConfig,
    runtime: PostgreSQLRuntime,
) -> WebhookReceiptResponse:
    """Normalize and durably commit one App delivery without external I/O."""
    try:
        normalized = normalize_event(event, payload)
    except UnsupportedGitHubEvent:
        return WebhookReceiptResponse("ignored")
    except GitHubWebhookError as exc:
        raise AdmissionError("GitHub webhook payload is invalid") from exc
    if normalized.event == "installation":
        category = webhook_deliveries.CommandCategory.INSTALLATION
    elif normalized.event == "installation_repositories":
        category = webhook_deliveries.CommandCategory.REPOSITORY_ACCESS
    elif normalized.command_kind is CommandKind.REVIEW:
        category = webhook_deliveries.CommandCategory.REVIEW
    elif normalized.command_kind in {
        CommandKind.FINDING_FEEDBACK,
        CommandKind.QUALITY_FEEDBACK,
        CommandKind.INVALID,
    }:
        category = webhook_deliveries.CommandCategory.FEEDBACK
    else:
        category = webhook_deliveries.CommandCategory.IGNORED

    try:
        with runtime.transaction() as connection:
            # GitHub's delivery deadline is ten seconds. Leave time for pool
            # checkout, commit, and the HTTP response instead of inheriting the
            # runtime's broader workload timeout.
            connection.execute("SET LOCAL statement_timeout = '5s'")
            registered = webhook_deliveries.register_delivery(
                connection,
                definition=webhook_deliveries.DeliveryDefinition(
                    delivery_guid=delivery_id,
                    event=normalized.event,
                    action=normalized.action,
                    payload_sha256=hashlib.sha256(body).hexdigest(),
                    provider_installation_id=(
                        normalized.provider_installation_id
                    ),
                    provider_repository_id=normalized.provider_repository_id,
                    repository_full_name=normalized.repository,
                    command_category=category,
                    normalized_schema_version=normalized.schema_version,
                    normalized_payload=normalized.normalized,
                ),
                max_attempts=config.webhook_delivery_max_attempts,
            )
    except webhook_deliveries.DeliveryConflict as exc:
        raise GitHubAppDeliveryConflict(str(exc)) from exc
    except webhook_deliveries.NormalizedPayloadTooLarge as exc:
        raise GitHubAppPayloadTooLarge(str(exc)) from exc
    except webhook_deliveries.WebhookDeliveryError as exc:
        raise AdmissionError(str(exc)) from exc

    status = (
        "duplicate"
        if isinstance(registered, webhook_deliveries.DuplicateDelivery)
        else "received"
    )
    return WebhookReceiptResponse(status)


def response_body(status: str) -> bytes:
    return json.dumps(
        {"status": status}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
