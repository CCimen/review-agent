"""Shared runtime and lifecycle adapters for model-facing review tools."""

from __future__ import annotations

import atexit
from dataclasses import dataclass
from functools import wraps
import json
import logging
import re
import threading
from typing import Any, Callable, TypeVar, cast

from . import (
    failure_codes,
    memory_validation,
    review_contract,
    review_run_application,
    settings,
)
from .domain.review import ReviewRunId
from .github.gateway import GitHubGatewayError, GitHubGatewayRejected
from .github.gateway_client import ReviewGitHubGatewayClient
from .postgres import jobs as postgres_jobs
from .postgres.runtime import (
    PostgreSQLRuntime,
    PostgreSQLRuntimeError,
    PostgreSQLRuntimeRole,
)

SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
JsonObject = dict[str, Any]
ApplicationResult = TypeVar("ApplicationResult")
_process_runtime: PostgreSQLRuntime | None = None
_process_runtime_lock = threading.Lock()
logger = logging.getLogger(__name__)
ReviewRunTerminal = review_run_application.ReviewRunTerminal


class ToolInputError(ValueError):
    pass


def worker_lease_fence(
    *, run_id_field: str = "run_id"
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Fence each mutable tool entry against the worker's current lease."""

    def decorate(handler: Callable[..., str]) -> Callable[..., str]:
        @wraps(handler)
        def fenced(args: dict[str, Any], **context: Any) -> str:
            try:
                lease = postgres_jobs.WorkerLeaseSession.parse(
                    context.get("session_id")
                )
                if lease is None:
                    raise postgres_jobs.ReviewJobError(
                        "a live review worker lease is required"
                    )
                run_id = _positive_id(args.get(run_id_field), field=run_id_field)
                with postgres_runtime().transaction() as connection:
                    postgres_jobs.require_live_lease(
                        connection,
                        job_id=lease.job_id,
                        review_run_id=ReviewRunId(run_id),
                        lease_generation=lease.lease_generation,
                    )
            except (
                ToolInputError,
                postgres_jobs.ReviewJobError,
                PostgreSQLRuntimeError,
            ) as exc:
                return error_output(f"{exc}; stop this review turn")
            except Exception:
                logger.exception("Worker lease verification failed unexpectedly")
                return error_output(
                    "worker lease could not be verified; stop this review turn"
                )
            return handler(args, **context)

        return fenced

    return decorate


def output_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def error_output(message: str) -> str:
    return output_json({"error": message})


def _positive_id(raw: Any, *, field: str) -> int:
    if isinstance(raw, bool):
        raise ToolInputError(f"{field} must be a positive integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ToolInputError(f"{field} must be a positive integer") from exc
    if value < 1:
        raise ToolInputError(f"{field} must be a positive integer")
    return value


def parse_path(raw: Any, *, required: bool = True) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value and not required:
        return ""
    try:
        return memory_validation.normalize_path(value)
    except memory_validation.ReviewMemoryError as exc:
        raise ToolInputError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class GatewaySourceSession:
    run_id: int
    lease: postgres_jobs.WorkerLeaseSession
    client: ReviewGitHubGatewayClient


def gateway_source_session(
    args: dict[str, Any],
    context: dict[str, Any],
    *,
    run_id_field: str = "run_id",
) -> GatewaySourceSession:
    run_id = _positive_id(args.get(run_id_field), field=run_id_field)
    lease = postgres_jobs.WorkerLeaseSession.parse(context.get("session_id"))
    if lease is None:
        raise ToolInputError("a live review worker lease is required")
    try:
        base_url = settings.ReviewAgentSettings.from_environment().github_gateway_url
        client = ReviewGitHubGatewayClient(base_url)
    except (settings.SettingsError, GitHubGatewayError) as exc:
        raise ToolInputError(str(exc)) from exc
    return GatewaySourceSession(run_id=run_id, lease=lease, client=client)


def source_error(exc: GitHubGatewayError) -> ToolInputError:
    if isinstance(exc, GitHubGatewayRejected) and exc.reason == "review_job_lease_lost":
        return ToolInputError("review worker lease is no longer current; stop this review turn")
    if isinstance(exc, GitHubGatewayRejected) and exc.reason == "repository_not_authorized":
        return ToolInputError("repository access is no longer enabled for this review")
    return ToolInputError("GitHub source read could not be completed")


def installed_review_contract() -> review_contract.ReviewContract:
    try:
        installed = review_contract.load_installed_contract()
    except review_contract.ReviewContractError as exc:
        raise ToolInputError(str(exc)) from exc
    configured = settings.ReviewAgentSettings.from_environment()
    if installed.profile != configured.profile:
        raise ToolInputError("configured profile does not match the installed reviewer")
    return installed


def postgres_runtime() -> PostgreSQLRuntime:
    """Open and cache one healthy reviewer pool; failed opens are never cached."""
    global _process_runtime
    if _process_runtime is not None:
        return _process_runtime
    with _process_runtime_lock:
        if _process_runtime is not None:
            return _process_runtime
        configured = settings.ReviewAgentSettings.from_environment()
        installed_review_contract()
        candidate = PostgreSQLRuntime(
            configured.postgres_database_url,
            role=PostgreSQLRuntimeRole.REVIEWER,
        )
        candidate.open()
        _process_runtime = candidate
        return candidate


def _close_postgres_runtime() -> None:
    global _process_runtime
    with _process_runtime_lock:
        runtime, _process_runtime = _process_runtime, None
    if runtime is not None:
        runtime.close()


atexit.register(_close_postgres_runtime)


def json_object_or_empty(value: Any) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def pull_request_identity(source: GatewaySourceSession) -> tuple[str, int, JsonObject]:
    try:
        result = source.client.get_review_pull(
            run_id=source.run_id,
            job_id=source.lease.job_id,
            lease_generation=source.lease.lease_generation,
        )
    except GitHubGatewayError as exc:
        raise source_error(exc) from exc
    return result.repository, result.pr_number, result.payload


def pull_base_sha(pull: dict[str, Any]) -> str:
    base_sha = (
        str(json_object_or_empty(pull.get("base")).get("sha", "")).strip().lower()
    )
    if not SHA_RE.fullmatch(base_sha):
        raise ToolInputError("GitHub did not provide a valid base SHA")
    return base_sha


def pull_head_sha(pull: dict[str, Any]) -> str:
    head_sha = (
        str(json_object_or_empty(pull.get("head")).get("sha", "")).strip().lower()
    )
    if not SHA_RE.fullmatch(head_sha):
        raise ToolInputError("GitHub did not provide a valid head SHA")
    return head_sha


def run_terminal_payload(run_id: int) -> JsonObject:
    return {
        "run_id": run_id,
        "run_state": "snapshot_superseded",
        "status": "superseded",
        "terminal": True,
        "retryable": False,
        "failure_code": failure_codes.SNAPSHOT_SUPERSEDED,
        "message": (
            "This review snapshot is no longer current, so no findings from it "
            "will be published."
        ),
        "next_action": (
            "Stop this review turn now. A newer review may already be running. "
            "If not, the developer must post /review as a new top-level PR comment."
        ),
    }


def run_subject(
    *,
    repository: str,
    pr_number: int,
    run_id: int,
) -> review_run_application.RunSubject:
    return review_run_application.RunSubject(
        repository=repository,
        pr_number=pr_number,
        run_id=run_id,
    )


def pull_snapshot(
    pull: JsonObject,
) -> review_run_application.PullSnapshot[JsonObject]:
    return review_run_application.PullSnapshot(
        payload=pull,
        base_sha=pull_base_sha(pull),
        head_sha=pull_head_sha(pull),
    )


def _load_pull_snapshot(
    source: GatewaySourceSession,
) -> review_run_application.PullSnapshot[JsonObject]:
    _, _, pull = pull_request_identity(source)
    return pull_snapshot(pull)


def load_application_snapshot(
    operation: Callable[[], ApplicationResult],
) -> ApplicationResult:
    """Translate terminal snapshots and application errors for model tools."""
    try:
        return operation()
    except ReviewRunTerminal:
        raise
    except review_run_application.ReviewRunError as exc:
        raise ToolInputError(str(exc)) from exc


def review_run_snapshot(
    *,
    source: GatewaySourceSession,
    repository: str,
    pr_number: int,
    phase: review_run_application.RunPhase,
    expected_head_sha: str | None = None,
) -> JsonObject:
    """Adapt the GitHub pull loader and failure-status effect to the run owner."""
    result = load_application_snapshot(
        lambda: review_run_application.load_live_snapshot(
            postgres_runtime(),
            run_subject(
                repository=repository,
                pr_number=pr_number,
                run_id=source.run_id,
            ),
            phase=phase,
            pull_loader=lambda: _load_pull_snapshot(source),
            expected_head_sha=expected_head_sha,
        )
    )
    return result.pull


def mark_run_failed(
    *,
    repository: str,
    pr_number: int,
    run_id: int,
    findings_count: int | None = None,
    failure_code: str = failure_codes.REVIEW_FAILED,
) -> None:
    try:
        review_run_application.fail_live_run(
            postgres_runtime(),
            run_subject(
                repository=repository,
                pr_number=pr_number,
                run_id=run_id,
            ),
            findings_count=findings_count,
            failure_code=failure_code,
        )
    except Exception as exc:
        # Keep the primary tool error public while making lost durable state
        # visible without serializing provider or database exception details.
        logger.error(
            "Review run failure state could not be persisted: "
            "run_id=%d failure_code=%s error_type=%s",
            run_id,
            failure_code,
            type(exc).__name__,
        )
