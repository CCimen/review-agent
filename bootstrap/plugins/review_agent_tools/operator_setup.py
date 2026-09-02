"""Secret-safe setup and readiness results for Review Agent operators."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict

from . import operator_application, review_contract
from .github import app_auth
from .github.gateway import (
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    OperatorAppStatus,
    OperatorSmokeResult,
)
from .github.gateway_client import ReviewGitHubGatewayClient
from .postgres.runtime import PostgreSQLRuntime
from .settings import ReviewAgentSettings


CheckStatus = Literal["ready", "error"]
OwnerType = Literal["user", "organization"]
_PLACEHOLDER_RE = re.compile(r"(?:replace|change[-_ ]?me|example|todo)", re.IGNORECASE)
_APP_NAME_RE = re.compile(r"[^a-z0-9]+")


class OperatorCapacityUnavailable(RuntimeError):
    """The deployment can retry a dry-run after active work drains."""


@dataclass(frozen=True, slots=True)
class DatabaseConnectionContract:
    """Effective credentials for one migration-owner/runtime database pair."""

    database_name: str
    runtime_password: str = field(repr=False)
    runtime_role: str


@dataclass(frozen=True, slots=True)
class _DatabaseParameters:
    database_name: str
    host: str
    password: str = field(repr=False)
    port: str
    role: str


@dataclass(frozen=True, slots=True)
class Capabilities:
    authentication: str = "github-app"
    repository_activation: tuple[str, ...] = ("explicit", "automatic")
    fork_pull_requests: bool = False
    feedback: bool = True
    repository_profiles: str = "deployment-profile-only"
    repository_guidance: str = "explicit-base-snapshot"
    advisory_only: bool = True
    trigger_mode: str = "manual"

    def to_json_obj(self) -> dict[str, object]:
        return {
            "advisory_only": self.advisory_only,
            "authentication": self.authentication,
            "feedback": self.feedback,
            "fork_pull_requests": self.fork_pull_requests,
            "repository_profiles": self.repository_profiles,
            "repository_guidance": self.repository_guidance,
            "repository_activation": list(self.repository_activation),
            "trigger_mode": self.trigger_mode,
        }


@dataclass(frozen=True, slots=True)
class OperatorCheck:
    name: str
    status: CheckStatus
    detail: str

    def to_json_obj(self) -> dict[str, str]:
        return {"detail": self.detail, "name": self.name, "status": self.status}


@dataclass(frozen=True, slots=True)
class PreflightReport:
    ready: bool
    checks: tuple[OperatorCheck, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "checks": [check.to_json_obj() for check in self.checks],
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class DryRunSmokeReport:
    active_jobs: int
    active_job_limit: int
    result: OperatorSmokeResult

    def to_json_obj(self) -> dict[str, object]:
        return {
            "active_job_limit": self.active_job_limit,
            "active_jobs": self.active_jobs,
            "dry_run": True,
            **self.result.to_mapping(),
        }


def capabilities() -> Capabilities:
    """Return the shipped product contract without consulting runtime state."""
    return Capabilities()


def _public_origin(value: str) -> str:
    normalized = value.strip().rstrip("/")
    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise ValueError("public_url must be one HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public_url must be one HTTPS origin")
    return f"https://{parsed.netloc}"


def _homepage_url(value: str) -> str:
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise ValueError("homepage_url must be one HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("homepage_url must be one HTTPS URL")
    return normalized


def _owner(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 100
        or any(character in normalized for character in "/?#")
    ):
        raise ValueError("owner must be a GitHub account name")
    return normalized


def _default_app_name(owner: str) -> str:
    suffix = _APP_NAME_RE.sub("-", owner.casefold()).strip("-") or "owner"
    return f"review-agent-{suffix}"[:34].rstrip("-")


def github_app_registration_url(
    *,
    owner: str,
    owner_type: OwnerType,
    public_url: str,
    homepage_url: str,
    app_name: str | None = None,
) -> str:
    """Build GitHub's prefilled App registration form without embedding secrets."""
    resolved_owner = _owner(owner)
    if owner_type not in {"user", "organization"}:
        raise ValueError("owner_type must be user or organization")
    origin = _public_origin(public_url)
    homepage = _homepage_url(homepage_url)
    name = (app_name or _default_app_name(resolved_owner)).strip()
    if not name or len(name) > 34:
        raise ValueError("app_name must contain 1 to 34 characters")
    path = (
        "/settings/apps/new"
        if owner_type == "user"
        else f"/organizations/{quote(resolved_owner, safe='')}/settings/apps/new"
    )
    query = urlencode(
        (
            ("name", name),
            ("url", homepage),
            ("public", "false"),
            ("request_oauth_on_install", "false"),
            ("webhook_active", "true"),
            ("webhook_url", f"{origin}/webhooks/github-app"),
            ("permissions[contents]", "read"),
            ("permissions[issues]", "write"),
            ("permissions[pull_requests]", "write"),
            ("events[]", "issue_comment"),
        )
    )
    return f"https://github.com{path}?{query}"


def _ready(name: str, detail: str) -> OperatorCheck:
    return OperatorCheck(name=name, status="ready", detail=detail)


def _error(name: str, detail: str) -> OperatorCheck:
    return OperatorCheck(name=name, status="error", detail=detail)


def _run_check(
    name: str,
    detail: str,
    operation: Callable[[], object],
) -> OperatorCheck:
    try:
        operation()
    except (OSError, TypeError, ValueError) as exc:
        return _error(name, str(exc))
    return _ready(name, detail)


def github_app_authenticator(
    environment: Mapping[str, str],
) -> app_auth.GitHubAppAuthenticator:
    """Load locally validated App credentials without exposing their values."""
    raw_app_id = environment.get("REVIEW_AGENT_GITHUB_APP_ID", "").strip()
    try:
        app_id = int(raw_app_id)
    except ValueError as exc:
        raise ValueError(
            "REVIEW_AGENT_GITHUB_APP_ID must be a positive integer"
        ) from exc
    if app_id < 1:
        raise ValueError("REVIEW_AGENT_GITHUB_APP_ID must be a positive integer")
    raw_path = (
        environment.get("REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE", "").strip()
        or environment.get("REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    )
    if not raw_path:
        raise ValueError("GitHub App private key path is required")
    private_key = app_auth.load_private_key_file(raw_path)
    return app_auth.GitHubAppAuthenticator(app_id=app_id, private_key_pem=private_key)


def _webhook_configuration(environment: Mapping[str, str]) -> None:
    value = environment.get("REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET", "").strip()
    if not value or _PLACEHOLDER_RE.search(value):
        raise ValueError("REVIEW_AGENT_GITHUB_APP_WEBHOOK_SECRET is not configured")


def _api_key_configuration(environment: Mapping[str, str]) -> None:
    value = environment.get("API_SERVER_KEY", "").strip()
    if not value or _PLACEHOLDER_RE.search(value):
        raise ValueError("API_SERVER_KEY is not configured")


def _database_parameters(value: str, *, label: str) -> _DatabaseParameters:
    try:
        parameters = conninfo_to_dict(value)
    except psycopg.Error as exc:
        raise ValueError(
            f"{label} URL is not valid PostgreSQL connection data"
        ) from exc
    if parameters.get("hostaddr") or parameters.get("service"):
        raise ValueError(
            f"{label} URL must not use hostaddr or service target selection"
        )
    host = parameters.get("host")
    database_name = parameters.get("dbname")
    role = parameters.get("user")
    password = parameters.get("password")
    if not all(
        isinstance(item, str) and item for item in (host, database_name, role, password)
    ):
        raise ValueError(
            f"{label} URL requires a host, database, username, and password"
        )
    assert isinstance(host, str)
    assert isinstance(database_name, str)
    assert isinstance(role, str)
    assert isinstance(password, str)
    raw_port = parameters.get("port", "5432")
    port = str(raw_port)
    if "," in host or "," in port:
        raise ValueError(f"{label} URL must target one PostgreSQL server")
    return _DatabaseParameters(
        database_name=database_name,
        host=host,
        password=password,
        port=port,
        role=role,
    )


def validate_database_configuration(
    settings: ReviewAgentSettings,
) -> DatabaseConnectionContract:
    """Require separate credentials for one exact owner/runtime database target."""
    owner = _database_parameters(
        settings.postgres_database_url,
        label="migration owner",
    )
    runtime = _database_parameters(
        settings.postgres_runtime_database_url,
        label="runtime",
    )
    if owner.role == runtime.role or owner.password == runtime.password:
        raise ValueError("migration owner and runtime credentials must differ")
    owner_target = (owner.host, owner.port, owner.database_name)
    runtime_target = (runtime.host, runtime.port, runtime.database_name)
    if owner_target != runtime_target:
        raise ValueError("migration owner and runtime URLs must target one database")
    return DatabaseConnectionContract(
        database_name=runtime.database_name,
        runtime_password=runtime.password,
        runtime_role=runtime.role,
    )


def preflight(
    environment: Mapping[str, str],
    *,
    bootstrap_source: Path | None = None,
) -> PreflightReport:
    """Validate local configuration and packaged behavior without network or DB I/O."""
    settings = ReviewAgentSettings(environment)
    contract_environment = review_contract.deployment_environment(environment)
    # operator_setup.py lives at bootstrap/plugins/review_agent_tools/.
    contract_source = bootstrap_source or Path(__file__).resolve().parents[2]
    checks = (
        _run_check(
            "database_configuration",
            "PostgreSQL owner and runtime URLs are structurally valid",
            lambda: validate_database_configuration(settings),
        ),
        _run_check(
            "github_app_configuration",
            "App ID and private key are locally valid",
            lambda: github_app_authenticator(environment),
        ),
        _run_check(
            "profile_contract",
            "Packaged review profile contract is valid",
            lambda: review_contract.load_packaged_contract(
                settings.profile,
                source=contract_source,
                environment=contract_environment,
            ),
        ),
        _run_check(
            "webhook_configuration",
            "GitHub App webhook secret is configured",
            lambda: _webhook_configuration(environment),
        ),
        _run_check(
            "internal_api_configuration",
            "Internal service authentication is configured",
            lambda: _api_key_configuration(environment),
        ),
    )
    return PreflightReport(
        ready=all(check.status == "ready" for check in checks),
        checks=checks,
    )


def _live_check(
    name: str,
    ready_detail: str,
    failure_detail: str,
    operation: Callable[[], object],
) -> OperatorCheck:
    try:
        operation()
    except Exception:
        return _error(name, failure_detail)
    return _ready(name, ready_detail)


def _validate_app_status(
    status: OperatorAppStatus,
) -> None:
    permissions = dict(status.permissions)
    required = {
        "contents": "read",
        "issues": "write",
        "pull_requests": "write",
    }
    allowed_names = {*required, "metadata"}
    if any(permissions.get(name) != level for name, level in required.items()):
        raise ValueError("GitHub App permissions do not match the product contract")
    if set(permissions) - allowed_names:
        raise ValueError("GitHub App has permissions outside the product contract")
    if set(status.events) != {"issue_comment"}:
        raise ValueError("GitHub App events do not match the product contract")


def _validate_installations(
    snapshot: operator_application.DeploymentHealth,
) -> None:
    access = snapshot.github_app
    if access.active_installations < 1:
        raise ValueError("no active GitHub App installation is reconciled")
    if access.invalid_active_installations:
        raise ValueError("an active installation is outside the product contract")


def _validate_repositories(
    snapshot: operator_application.DeploymentHealth,
) -> None:
    if (
        snapshot.github_app.enabled_repositories < 1
        and snapshot.github_app.automatic_installations < 1
    ):
        raise ValueError("no available repository is enabled for this profile")


def _repository_readiness_detail(
    snapshot: operator_application.DeploymentHealth,
) -> str:
    enabled = snapshot.github_app.enabled_repositories
    if enabled:
        return f"{enabled} repository(s) are enabled"
    return "Repositories will activate after the first signed /review delivery"


def _validate_queues(
    snapshot: operator_application.DeploymentHealth,
    active_job_limit: int,
) -> None:
    queue = snapshot.review_queue
    if queue.active >= active_job_limit:
        raise OperatorCapacityUnavailable(
            "review queue has reached its active job limit"
        )
    if queue.expired_leases:
        raise ValueError("review queue requires operator recovery")
    publication_queue = snapshot.publication_queue
    if publication_queue.expired_recoverable or publication_queue.expired_exhausted:
        raise ValueError("publication queue requires operator recovery")


def doctor(
    environment: Mapping[str, str],
    *,
    runtime: PostgreSQLRuntime,
    gateway: ReviewGitHubGatewayClient,
    hermes_probe: Callable[[], bool],
    hermes_home: Path | None = None,
) -> PreflightReport:
    """Run bounded, secret-safe, read-only deployment checks."""
    settings = ReviewAgentSettings(environment)
    snapshot: operator_application.DeploymentHealth | None = None
    try:
        runtime.readiness()
        snapshot = operator_application.deployment_health(
            runtime,
            profile=settings.profile,
        )
        database_check = _ready(
            "database",
            "PostgreSQL schema and operator health snapshot are readable",
        )
    except Exception:
        database_check = _error("database", "PostgreSQL readiness check failed")

    app_status: OperatorAppStatus | None = None
    try:
        app_status = gateway.operator_status()
        _validate_app_status(app_status)
        app_check = _ready(
            "github_app",
            f"GitHub App {app_status.slug} is authenticated with the required contract",
        )
    except GitHubGatewayRetryable:
        app_check = _error(
            "github_app",
            "Private gateway or GitHub API is temporarily unavailable",
        )
    except GitHubGatewayRejected:
        app_check = _error(
            "github_app",
            "GitHub rejected the configured App identity",
        )
    except GitHubGatewayProtocolError:
        app_check = _error(
            "github_app",
            "Private gateway authentication or response validation failed",
        )
    except ValueError:
        app_check = _error(
            "github_app",
            "GitHub App permissions or events do not match the product contract",
        )
    except Exception:
        app_check = _error("github_app", "GitHub App status check failed")

    checks: list[OperatorCheck] = [
        database_check,
        _live_check(
            "installed_profile",
            f"Installed profile {settings.profile} matches its signed receipt",
            "Installed review profile validation failed",
            lambda: review_contract.load_installed_contract(hermes_home),
        ),
        app_check,
        _live_check(
            "hermes",
            "Hermes API is reachable",
            "Hermes API health check failed",
            lambda: _require_true(hermes_probe()),
        ),
    ]
    if snapshot is None:
        checks.extend(
            _error(name, "PostgreSQL operator snapshot is unavailable")
            for name in ("installations", "repositories", "queues")
        )
    else:
        dead_letter_count = snapshot.review_queue.dead_letters
        dead_letter_noun = "record" if dead_letter_count == 1 else "records"
        checks.extend(
            (
                _live_check(
                    "installations",
                    (
                        f"{snapshot.github_app.active_installations} active "
                        "installation(s) are reconciled"
                    ),
                    "GitHub App installation state needs attention",
                    lambda: _validate_installations(snapshot),
                ),
                _live_check(
                    "repositories",
                    _repository_readiness_detail(snapshot),
                    "No repository is ready for this deployment profile",
                    lambda: _validate_repositories(snapshot),
                ),
                _live_check(
                    "queues",
                    (
                        f"Review queue has {snapshot.review_queue.active}/"
                        f"{settings.active_job_limit} active jobs, "
                        f"{dead_letter_count} dead-letter {dead_letter_noun}, "
                        "and no expired work"
                    ),
                    "Review or publication queues need recovery or capacity",
                    lambda: _validate_queues(snapshot, settings.active_job_limit),
                ),
            )
        )
    return PreflightReport(
        ready=all(check.status == "ready" for check in checks),
        checks=tuple(checks),
    )


def smoke_test(
    environment: Mapping[str, str],
    *,
    runtime: PostgreSQLRuntime,
    gateway: ReviewGitHubGatewayClient,
    repository: str,
    pr_number: int,
) -> DryRunSmokeReport:
    """Prove dry-run capacity plus read and publication authority without writes."""
    settings = ReviewAgentSettings(environment)
    snapshot = operator_application.deployment_health(
        runtime,
        profile=settings.profile,
    )
    _validate_queues(snapshot, settings.active_job_limit)
    result = gateway.operator_smoke(repository=repository, pr_number=pr_number)
    return DryRunSmokeReport(
        active_jobs=snapshot.review_queue.active,
        active_job_limit=settings.active_job_limit,
        result=result,
    )


def _require_true(value: bool) -> None:
    if value is not True:
        raise ValueError("probe was not ready")
