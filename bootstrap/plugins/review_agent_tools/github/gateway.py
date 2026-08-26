"""Closed GitHub App operations backed by durable Review Agent authority."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Literal, TypeVar, cast
import urllib.parse

from .. import capacity, changed_files, memory_validation, schemas
from ..domain.review import ReviewRunId
from ..postgres import github_app, jobs, review_runs, webhook_deliveries
from ..postgres.runtime import PostgreSQLRuntime
from ..source_control import (
    GitHubReadClient,
    GitHubReadError,
    PullSnapshot,
    read_pull_snapshot,
)
from .app_auth import (
    GitHubAppTokenPermanent,
    GitHubAppTokenRetryable,
    GitHubAppTokenService,
)
from .source import (
    GitHubSourceError,
    ReviewFilePage,
    ReviewPullSource,
    ReviewSourceBytes,
    read_changed_files_page,
    read_review_diff,
    read_review_file_page,
    read_review_pull,
)


AUTHORIZE_REVIEW_DELIVERY_PATH = "/v1/review-deliveries/authorize"
READ_REVIEW_SOURCE_PATH = "/v1/review-sources/read"


class GitHubGatewayError(RuntimeError):
    """The internal GitHub gateway could not complete a fixed operation."""


class GitHubGatewayProtocolError(GitHubGatewayError):
    """A gateway request or response violated the closed operation contract."""


class GitHubGatewayRejected(GitHubGatewayError):
    """Current durable or provider authority rejects the requested operation."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GitHubGatewayRetryable(GitHubGatewayError):
    """A transient provider or internal dependency failure can be retried."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubGatewayProtocolError(f"{field} must be a positive integer")
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise GitHubGatewayProtocolError(f"{field} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise GitHubGatewayProtocolError(f"{field} is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class DeliveryLeaseIdentity:
    """The only caller-controlled authority accepted by the gateway."""

    delivery_id: int
    lease_owner: str
    lease_generation: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "DeliveryLeaseIdentity":
        expected = {"delivery_id", "lease_owner", "lease_generation"}
        if set(value) != expected:
            raise GitHubGatewayProtocolError(
                "gateway request fields do not match the authorize contract"
            )
        return cls(
            delivery_id=_positive(value.get("delivery_id"), "delivery_id"),
            lease_owner=_text(value.get("lease_owner"), "lease_owner", 120),
            lease_generation=_positive(
                value.get("lease_generation"), "lease_generation"
            ),
        )


@dataclass(frozen=True, slots=True)
class ReviewSourceRequest:
    """Closed source operation plus durable run and worker lease identity."""

    operation: Literal["pull", "changed_files", "diff", "file"]
    run_id: int
    job_id: int
    lease_generation: int
    per_page: int | None = None
    page: int | None = None
    path: str | None = None
    side: Literal["head", "base"] | None = None
    start_line: int | None = None
    max_lines: int | None = None
    max_chars: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ReviewSourceRequest":
        operation = value.get("operation")
        if not isinstance(operation, str):
            raise GitHubGatewayProtocolError("source operation must be text")
        common = {"operation", "run_id", "job_id", "lease_generation"}
        expected = {
            "pull": common,
            "changed_files": common | {"per_page", "page"},
            "diff": common,
            "file": common
            | {"path", "side", "start_line", "max_lines", "max_chars"},
        }.get(operation)
        if expected is None or set(value) != expected:
            raise GitHubGatewayProtocolError(
                "gateway request fields do not match a source operation"
            )
        request = cls(
            operation=cast(
                Literal["pull", "changed_files", "diff", "file"], operation
            ),
            run_id=_positive(value.get("run_id"), "run_id"),
            job_id=_positive(value.get("job_id"), "job_id"),
            lease_generation=_positive(
                value.get("lease_generation"), "lease_generation"
            ),
        )
        if operation == "changed_files":
            per_page = _positive(value.get("per_page"), "per_page")
            allowed_page_sizes = frozenset(
                (*changed_files.DEFAULT_PER_PAGE_SEQUENCE, 1)
            )
            if per_page not in allowed_page_sizes:
                raise GitHubGatewayProtocolError("per_page is not supported")
            page = _positive(value.get("page"), "page")
            if page > changed_files.GITHUB_PR_FILES_LIMIT:
                raise GitHubGatewayProtocolError("page exceeds the provider limit")
            return cls(
                operation="changed_files",
                run_id=request.run_id,
                job_id=request.job_id,
                lease_generation=request.lease_generation,
                per_page=per_page,
                page=page,
            )
        if operation == "file":
            raw_path = value.get("path")
            if not isinstance(raw_path, str):
                raise GitHubGatewayProtocolError("path must be text")
            try:
                path = memory_validation.normalize_path(raw_path)
            except memory_validation.ReviewMemoryError as exc:
                raise GitHubGatewayProtocolError(str(exc)) from exc
            side = value.get("side")
            if side not in {"head", "base"}:
                raise GitHubGatewayProtocolError("side must be head or base")
            start_line = _positive(value.get("start_line"), "start_line")
            max_lines = _positive(value.get("max_lines"), "max_lines")
            if max_lines > schemas.SOURCE_PAGE_MAX_LINES:
                raise GitHubGatewayProtocolError("max_lines exceeds the source page limit")
            max_chars = _positive(value.get("max_chars"), "max_chars")
            if not (
                capacity.MIN_TEXT_PAGE_CHARS
                <= max_chars
                <= capacity.DEFAULT_RESULT_MAX_CHARS
            ):
                raise GitHubGatewayProtocolError("max_chars is outside the source page limit")
            return cls(
                operation="file",
                run_id=request.run_id,
                job_id=request.job_id,
                lease_generation=request.lease_generation,
                path=path,
                side=cast(Literal["head", "base"], side),
                start_line=start_line,
                max_lines=max_lines,
                max_chars=max_chars,
            )
        return request


SourceResult = ReviewPullSource | ReviewSourceBytes | ReviewFilePage
ProviderResult = TypeVar("ProviderResult")


@dataclass(frozen=True, slots=True)
class AuthorizedReviewSnapshot:
    """Bounded provider facts verified for one live durable delivery lease."""

    provider_installation_id: int
    provider_repository_id: int
    repository: str
    pr_number: int
    comment_id: int
    sender_login: str
    base_sha: str
    head_sha: str

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> "AuthorizedReviewSnapshot":
        expected = {
            "provider_installation_id",
            "provider_repository_id",
            "repository",
            "pr_number",
            "comment_id",
            "sender_login",
            "base_sha",
            "head_sha",
        }
        if set(value) != expected:
            raise GitHubGatewayProtocolError(
                "gateway response fields do not match the authorize contract"
            )
        return cls(
            provider_installation_id=_positive(
                value.get("provider_installation_id"), "provider_installation_id"
            ),
            provider_repository_id=_positive(
                value.get("provider_repository_id"), "provider_repository_id"
            ),
            repository=_text(value.get("repository"), "repository", 260),
            pr_number=_positive(value.get("pr_number"), "pr_number"),
            comment_id=_positive(value.get("comment_id"), "comment_id"),
            sender_login=_text(value.get("sender_login"), "sender_login", 120),
            base_sha=_text(value.get("base_sha"), "base_sha", 128),
            head_sha=_text(value.get("head_sha"), "head_sha", 128),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "provider_installation_id": self.provider_installation_id,
            "provider_repository_id": self.provider_repository_id,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "comment_id": self.comment_id,
            "sender_login": self.sender_login,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
        }


@dataclass(frozen=True, slots=True)
class _ReviewCommand:
    provider_installation_id: int
    provider_repository_id: int
    repository: str
    pr_number: int
    comment_id: int
    sender_id: int
    sender_login: str


def _review_command(
    delivery: webhook_deliveries.WebhookDelivery,
) -> _ReviewCommand:
    payload = delivery.normalized_payload
    if delivery.normalized_schema_version != 1:
        raise GitHubGatewayRejected("unsupported_normalized_schema")
    if (
        delivery.command_category is not webhook_deliveries.CommandCategory.REVIEW
        or not isinstance(payload, Mapping)
        or payload.get("kind") != "issue_comment"
    ):
        raise GitHubGatewayRejected("invalid_normalized_payload")
    installation_id = delivery.provider_installation_id
    repository_id = delivery.provider_repository_id
    repository = delivery.repository_full_name
    if installation_id is None or repository_id is None or repository is None:
        raise GitHubGatewayRejected("invalid_normalized_payload")
    try:
        return _ReviewCommand(
            provider_installation_id=_positive(
                installation_id, "provider_installation_id"
            ),
            provider_repository_id=_positive(repository_id, "provider_repository_id"),
            repository=_text(repository, "repository", 260),
            pr_number=_positive(payload.get("pr_number"), "pr_number"),
            comment_id=_positive(payload.get("comment_id"), "comment_id"),
            sender_id=_positive(payload.get("sender_id"), "sender_id"),
            sender_login=_text(payload.get("sender_login"), "sender_login", 120),
        )
    except GitHubGatewayProtocolError as exc:
        raise GitHubGatewayRejected("invalid_normalized_payload") from exc


class ReviewGitHubGateway:
    """Authorize and execute fixed GitHub operations without exposing credentials."""

    def __init__(
        self,
        *,
        postgres: PostgreSQLRuntime,
        tokens: GitHubAppTokenService,
        profile: str,
        github_factory: Callable[[str], GitHubReadClient] | None = None,
    ) -> None:
        self._postgres = postgres
        self._tokens = tokens
        self._profile = _text(profile, "profile", 80)
        self._github_factory = github_factory or _gateway_github_client

    def authorize_review_delivery(
        self,
        *,
        delivery_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> AuthorizedReviewSnapshot:
        command = self._require_authority(
            delivery_id=delivery_id,
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )
        snapshot = self._provider_snapshot(command)
        self._validate_snapshot(command, snapshot)
        self._require_authority(
            delivery_id=delivery_id,
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )
        return AuthorizedReviewSnapshot(
            provider_installation_id=command.provider_installation_id,
            provider_repository_id=command.provider_repository_id,
            repository=snapshot.repository,
            pr_number=snapshot.number,
            comment_id=command.comment_id,
            sender_login=command.sender_login,
            base_sha=snapshot.base_sha,
            head_sha=snapshot.head_sha,
        )

    def read_review_source(self, request: ReviewSourceRequest) -> SourceResult:
        scope = self._require_source_authority(
            run_id=request.run_id,
            job_id=request.job_id,
            lease_generation=request.lease_generation,
        )
        if request.operation == "pull":
            def operation(github: GitHubReadClient) -> SourceResult:
                return read_review_pull(github, scope)
        elif request.operation == "changed_files":
            assert request.per_page is not None and request.page is not None
            per_page = request.per_page
            page = request.page

            def operation(github: GitHubReadClient) -> SourceResult:
                return read_changed_files_page(
                    github, scope, per_page=per_page, page=page
                )
        elif request.operation == "diff":
            def operation(github: GitHubReadClient) -> SourceResult:
                return read_review_diff(github, scope)
        else:
            assert (
                request.path is not None
                and request.side is not None
                and request.start_line is not None
                and request.max_lines is not None
                and request.max_chars is not None
            )
            path = request.path
            side = request.side
            start_line = request.start_line
            max_lines = request.max_lines
            max_chars = request.max_chars

            def operation(github: GitHubReadClient) -> SourceResult:
                return read_review_file_page(
                    github,
                    scope,
                    path=path,
                    side=side,
                    start_line=start_line,
                    max_lines=max_lines,
                    max_chars=max_chars,
                )
        result = self._provider_source(scope.provider_repository_id, operation)
        self._require_source_authority(
            run_id=request.run_id,
            job_id=request.job_id,
            lease_generation=request.lease_generation,
        )
        return result

    def _require_source_authority(
        self,
        *,
        run_id: int,
        job_id: int,
        lease_generation: int,
    ) -> review_runs.ReviewRunScope:
        try:
            with self._postgres.transaction() as connection:
                resolved_run_id = ReviewRunId(_positive(run_id, "run_id"))
                jobs.require_live_lease(
                    connection,
                    job_id=_positive(job_id, "job_id"),
                    review_run_id=resolved_run_id,
                    lease_generation=_positive(
                        lease_generation, "lease_generation"
                    ),
                )
                scope = review_runs.get_run_scope(connection, resolved_run_id)
                github_app.authorize_review_read(
                    connection,
                    scope.provider_repository_id,
                    profile_key=self._profile,
                )
                return scope
        except (
            jobs.ReviewJobError,
            review_runs.ReviewRunError,
        ) as exc:
            raise GitHubGatewayRejected("review_job_lease_lost") from exc
        except github_app.GitHubAppRepositoryUnauthorized as exc:
            raise GitHubGatewayRejected("repository_not_authorized") from exc

    def _provider_source(
        self,
        provider_repository_id: int,
        operation: Callable[[GitHubReadClient], ProviderResult],
    ) -> ProviderResult:
        attempt = 0
        while True:
            try:
                token = self._tokens.token_for(provider_repository_id)
                return operation(self._github_factory(token.value))
            except GitHubAppTokenRetryable as exc:
                raise GitHubGatewayRetryable("token_exchange_unavailable") from exc
            except GitHubAppTokenPermanent as exc:
                raise GitHubGatewayRejected("provider_authorization_denied") from exc
            except github_app.GitHubAppRepositoryUnauthorized as exc:
                raise GitHubGatewayRejected("repository_not_authorized") from exc
            except GitHubReadError as exc:
                if exc.kind == "unauthorized" and attempt == 0:
                    self._tokens.invalidate(provider_repository_id)
                    attempt += 1
                    continue
                if exc.kind in {"unreachable", "http_error", "rate_limited"}:
                    raise GitHubGatewayRetryable("github_read_unavailable") from exc
                if exc.kind in {"unauthorized", "forbidden"}:
                    raise GitHubGatewayRejected(
                        "provider_authorization_denied"
                    ) from exc
                raise GitHubGatewayRejected("github_read_invalid") from exc
            except GitHubSourceError as exc:
                raise GitHubGatewayRejected("github_read_invalid") from exc

    def _require_authority(
        self,
        *,
        delivery_id: int,
        lease_owner: str,
        lease_generation: int,
    ) -> _ReviewCommand:
        try:
            with self._postgres.transaction() as connection:
                delivery = webhook_deliveries.require_live_delivery(
                    connection,
                    delivery_id=delivery_id,
                    lease_owner=lease_owner,
                    lease_generation=lease_generation,
                )
                command = _review_command(delivery)
                github_app.authorize_review_admission(
                    connection,
                    provider_repository_id=command.provider_repository_id,
                    provider_installation_id=command.provider_installation_id,
                    profile_key=self._profile,
                )
        except (
            webhook_deliveries.DeliveryLeaseLost,
            webhook_deliveries.DeliveryNotFound,
        ) as exc:
            raise GitHubGatewayRejected("delivery_lease_lost") from exc
        except github_app.GitHubAppRepositoryUnauthorized as exc:
            raise GitHubGatewayRejected("repository_not_authorized") from exc
        return command

    def _provider_snapshot(self, command: _ReviewCommand) -> PullSnapshot:
        def operation(github: GitHubReadClient) -> PullSnapshot:
            self._authorize_sender(github, command)
            return read_pull_snapshot(github, command.repository, command.pr_number)

        return self._provider_source(command.provider_repository_id, operation)

    @staticmethod
    def _authorize_sender(
        github: GitHubReadClient, command: _ReviewCommand
    ) -> None:
        repository = urllib.parse.quote(command.repository, safe="/")
        login = urllib.parse.quote(command.sender_login, safe="")
        try:
            raw_payload = github.request_json(
                f"/repos/{repository}/collaborators/{login}/permission",
                max_bytes=65_536,
            )
        except GitHubReadError as exc:
            if exc.kind == "not_found":
                raise GitHubGatewayRejected("sender_not_authorized") from exc
            raise
        if not isinstance(raw_payload, Mapping):
            raise GitHubReadError(
                "invalid_json", "GitHub returned invalid collaborator permission"
            )
        payload = cast(Mapping[str, object], raw_payload)
        raw_user = payload.get("user")
        if not isinstance(raw_user, Mapping):
            raise GitHubReadError("invalid_json", "GitHub returned invalid user")
        user = cast(Mapping[str, object], raw_user)
        permission = payload.get("permission")
        user_id = user.get("id")
        user_login = user.get("login")
        if (
            not isinstance(permission, str)
            or permission.lower() not in {"write", "admin"}
            or type(user_id) is not int
            or user_id != command.sender_id
            or not isinstance(user_login, str)
            or user_login.casefold() != command.sender_login.casefold()
        ):
            raise GitHubGatewayRejected("sender_not_authorized")

    @staticmethod
    def _validate_snapshot(
        command: _ReviewCommand, snapshot: PullSnapshot
    ) -> None:
        if (
            snapshot.repository_id != command.provider_repository_id
            or snapshot.repository.casefold() != command.repository.casefold()
            or snapshot.number != command.pr_number
        ):
            raise GitHubGatewayRejected("pull_request_identity_mismatch")
        if snapshot.state != "open":
            raise GitHubGatewayRejected("pull_request_not_open")
        if (
            snapshot.head_repository_id != snapshot.repository_id
            or snapshot.head_repository is None
            or snapshot.head_repository.casefold() != snapshot.repository.casefold()
        ):
            raise GitHubGatewayRejected("fork_source_not_supported")


def _gateway_github_client(token: str) -> GitHubReadClient:
    """Bound one gateway operation well inside its durable delivery lease."""
    return GitHubReadClient(
        token,
        request_timeout_seconds=10.0,
        max_attempts=1,
    )
