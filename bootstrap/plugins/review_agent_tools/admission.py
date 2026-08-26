"""Signed GitHub review admission into the PostgreSQL durable queue."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from typing import cast

from .github_webhook import (
    CommandKind,
    GitHubWebhookError,
    UnsupportedGitHubEvent,
    normalize_event,
)
from .github import app_processor
from . import review_contract
from .domain.review import JsonObject
from .postgres import jobs, review_runs, webhook_deliveries
from .postgres.runtime import PostgreSQLRuntime
from .review_run_application import (
    PostgresRunRequest,
    admit_postgres_review,
)
from .settings import PostgresDatabaseUrl, ReviewAgentSettings, SettingsError
from .source_control import GitHubReadClient


DEFAULT_PATH = "/webhooks/review-agent"
GITHUB_APP_PATH = "/webhooks/github-app"
DEFAULT_PORT = 8644
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Temporary compatibility exports for callers of the original admission seam.
PullSnapshot = app_processor.PullSnapshot
read_pull_snapshot = app_processor.read_pull_snapshot


class AdmissionError(ValueError):
    """The signed request does not satisfy the admission contract."""


class UnauthorizedAdmission(AdmissionError):
    """The signed request names a caller outside the trusted boundary."""


class GitHubAppDeliveryConflict(AdmissionError):
    """A GitHub delivery GUID was reused for different immutable input."""


class GitHubAppPayloadTooLarge(AdmissionError):
    """A signed App delivery cannot fit the durable normalized envelope."""


@dataclass(frozen=True, slots=True)
class AdmissionConfig:
    secret: str
    token: str
    allowed_repositories: frozenset[str]
    database_url: PostgresDatabaseUrl
    profile: str
    policy_revision: str
    active_job_limit: int
    job_max_attempts: int
    job_priority: int
    github_app_secret: str = ""
    webhook_delivery_max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    repository: str
    pr_number: int
    requester: str
    association: str
    comment_id: int


@dataclass(frozen=True, slots=True)
class AdmissionResponse:
    status: str
    run_id: int
    job_id: int

    def to_json(self) -> bytes:
        return json.dumps(
            {"job_id": self.job_id, "run_id": self.run_id, "status": self.status},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


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


def _priority_setting(environment: Mapping[str, str]) -> int:
    raw = environment.get("REVIEW_AGENT_JOB_PRIORITY", "0").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError("REVIEW_AGENT_JOB_PRIORITY must be an integer") from exc


def _repository_name(value: object) -> str:
    name = str(value or "").strip()
    parts = name.split("/")
    if len(parts) != 2 or not all(parts):
        raise AdmissionError("repository.full_name must be owner/name")
    return name


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise AdmissionError(f"{field} must be a positive integer")
    return value


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AdmissionError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def load_config(environment: Mapping[str, str] | None = None) -> AdmissionConfig:
    values = environment if environment is not None else os.environ
    settings = ReviewAgentSettings(values)
    secret = values.get("REVIEW_AGENT_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise SettingsError("REVIEW_AGENT_WEBHOOK_SECRET is required")
    token = settings.github_read_token
    if not token:
        raise SettingsError("GITHUB_READ_TOKEN is required")
    repositories = settings.allowed_repositories
    if not repositories:
        raise SettingsError(
            "REVIEW_AGENT_ALLOWED_REPOSITORIES is empty; deny by default"
        )
    return AdmissionConfig(
        secret=secret,
        token=token,
        allowed_repositories=repositories,
        database_url=settings.postgres_database_url,
        profile=settings.profile,
        policy_revision=settings.policy_revision(),
        active_job_limit=_integer_setting(values, "REVIEW_AGENT_ACTIVE_JOB_LIMIT", 100),
        job_max_attempts=_integer_setting(values, "REVIEW_AGENT_JOB_MAX_ATTEMPTS", 3),
        job_priority=_priority_setting(values),
        github_app_secret=values.get(
            "REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET", ""
        ).strip(),
        webhook_delivery_max_attempts=_integer_setting(
            values, "REVIEW_AGENT_WEBHOOK_DELIVERY_MAX_ATTEMPTS", 3
        ),
    )


def decode_request(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError("request body must be valid UTF-8 JSON") from exc


def parse_request(payload: object) -> AdmissionRequest:
    root = _object(payload, "payload")
    repository = _object(root.get("repository"), "repository")
    pull_request = _object(root.get("pull_request"), "pull_request")
    requester = _object(root.get("requester"), "requester")
    request = _object(root.get("request"), "request")
    login = str(requester.get("login") or "").strip()
    association = str(requester.get("association") or "").strip().upper()
    if not login:
        raise AdmissionError("requester.login is required")
    if association not in TRUSTED_ASSOCIATIONS:
        raise UnauthorizedAdmission("requester is not a trusted maintainer")
    return AdmissionRequest(
        repository=_repository_name(repository.get("full_name")),
        pr_number=_positive_int(pull_request.get("number"), "pull_request.number"),
        requester=login,
        association=association,
        comment_id=_positive_int(request.get("comment_id"), "request.comment_id"),
    )


def ready_check(config: AdmissionConfig, runtime: PostgreSQLRuntime) -> dict[str, str]:
    if config.database_url != runtime.database_url:
        raise AdmissionError("admission runtime does not match its configured database")
    runtime.readiness()
    _admission_contract(config.profile)
    return {"status": "ready"}


def _admission_contract(profile: str) -> review_contract.ReviewContract:
    try:
        contract = review_contract.load_packaged_contract(profile)
    except review_contract.ReviewContractError as exc:
        raise AdmissionError(str(exc)) from exc
    if contract.profile != profile:
        raise AdmissionError("configured profile does not match the packaged reviewer")
    return contract


def admit_review(
    *,
    payload: object,
    delivery_id: str,
    config: AdmissionConfig,
    github: GitHubReadClient,
    runtime: PostgreSQLRuntime,
) -> AdmissionResponse:
    request = parse_request(payload)
    if request.repository.casefold() not in config.allowed_repositories:
        raise UnauthorizedAdmission("repository is not allowlisted")
    if delivery_id.strip() != str(request.comment_id):
        raise AdmissionError("X-GitHub-Delivery must match request.comment_id")

    snapshot = read_pull_snapshot(github, request.repository, request.pr_number)
    if snapshot.repository.casefold() != request.repository.casefold():
        raise AdmissionError("GitHub base repository does not match the request")
    if snapshot.number != request.pr_number:
        raise AdmissionError("GitHub pull request number does not match the request")
    if snapshot.state != "open":
        raise AdmissionError("pull request is not open")

    contract = _admission_contract(config.profile)
    admitted = admit_postgres_review(
        runtime,
        PostgresRunRequest(
            provider="github",
            provider_repository_id=snapshot.repository_id,
            repository=snapshot.repository,
            pr_number=snapshot.number,
            base_sha=snapshot.base_sha,
            head_sha=snapshot.head_sha,
            policy_revision=config.policy_revision,
            resolved_config_schema_version=2,
            resolved_config=cast(JsonObject, review_contract.resolved_config(contract)),
            request_key=f"github:issue-comment:{request.comment_id}",
            trigger_comment_id=request.comment_id,
            trigger_user=request.requester,
        ),
        priority=config.job_priority,
        max_attempts=config.job_max_attempts,
        active_job_limit=config.active_job_limit,
    )
    status = (
        "duplicate"
        if isinstance(admitted.run, review_runs.DuplicateRun)
        or isinstance(admitted.job, jobs.DuplicateJob)
        else "accepted"
    )
    return AdmissionResponse(
        status=status,
        run_id=int(admitted.run.run.id),
        job_id=admitted.job.job.id,
    )


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


def response_body(status: str, message: str = "") -> bytes:
    value = {"status": status}
    if message:
        value["message"] = message
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
