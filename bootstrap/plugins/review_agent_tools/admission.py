"""Signed GitHub review admission into the PostgreSQL durable queue."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from typing import cast
import urllib.parse

from .github_webhook import verify_signature as _verify_signature
from . import review_contract
from .domain.review import JsonObject
from .postgres import jobs, review_runs
from .postgres.runtime import PostgreSQLRuntime
from .review_run_application import (
    PostgresRunRequest,
    admit_postgres_review,
)
from .settings import PostgresDatabaseUrl, ReviewAgentSettings, SettingsError
from .source_control import GitHubReadClient, GitHubReadError


DEFAULT_PATH = "/webhooks/review-agent"
DEFAULT_PORT = 8644
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
__all__ = ("verify_signature",)


class AdmissionError(ValueError):
    """The signed request does not satisfy the admission contract."""


class UnauthorizedAdmission(AdmissionError):
    """The signed request names a caller outside the trusted boundary."""


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


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    repository: str
    pr_number: int
    requester: str
    association: str
    comment_id: int


@dataclass(frozen=True, slots=True)
class PullSnapshot:
    repository_id: int
    repository: str
    number: int
    state: str
    base_sha: str
    head_sha: str


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


def _github_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return cast(Mapping[str, object], value)


def _github_repository_name(value: object) -> str:
    if not isinstance(value, str):
        raise GitHubReadError(
            "invalid_json", "GitHub returned an invalid repository name"
        )
    name = value.strip()
    parts = name.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubReadError(
            "invalid_json", "GitHub returned an invalid repository name"
        )
    return name


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
    )


def decode_request(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdmissionError("request body must be valid UTF-8 JSON") from exc


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Expose the shared verifier at the admission boundary."""
    return _verify_signature(body, signature, secret)


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


def _github_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return value.strip()


def _github_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return value


def read_pull_snapshot(
    github: GitHubReadClient, repository: str, pr_number: int
) -> PullSnapshot:
    quoted = urllib.parse.quote(repository, safe="/")
    root = _github_object(
        github.request_json(f"/repos/{quoted}/pulls/{pr_number}"),
        "GitHub pull request",
    )
    base = _github_object(root.get("base"), "pull request base")
    head = _github_object(root.get("head"), "pull request head")
    base_repository = _github_object(base.get("repo"), "base repository")
    return PullSnapshot(
        repository_id=_github_int(base_repository.get("id"), "repository id"),
        repository=_github_repository_name(base_repository.get("full_name")),
        number=_github_int(root.get("number"), "pull request number"),
        state=_github_text(root.get("state"), "pull request state"),
        base_sha=_github_text(base.get("sha"), "base sha"),
        head_sha=_github_text(head.get("sha"), "head sha"),
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


def response_body(status: str, message: str = "") -> bytes:
    value = {"status": status}
    if message:
        value["message"] = message
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
