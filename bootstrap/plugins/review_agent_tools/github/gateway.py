"""Closed GitHub App operations backed by durable Review Agent authority."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import cast
import urllib.parse

from ..postgres import github_app, webhook_deliveries
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
    ReviewReadTokenService,
)


AUTHORIZE_REVIEW_DELIVERY_PATH = "/v1/review-deliveries/authorize"


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
        tokens: ReviewReadTokenService,
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
        except github_app.GitHubAppReviewReadUnauthorized as exc:
            raise GitHubGatewayRejected("repository_not_authorized") from exc
        return command

    def _provider_snapshot(self, command: _ReviewCommand) -> PullSnapshot:
        attempt = 0
        while True:
            try:
                token = self._tokens.token_for(command.provider_repository_id)
                github = self._github_factory(token.value)
                self._authorize_sender(github, command)
                return read_pull_snapshot(
                    github, command.repository, command.pr_number
                )
            except GitHubAppTokenRetryable as exc:
                raise GitHubGatewayRetryable("token_exchange_unavailable") from exc
            except GitHubAppTokenPermanent as exc:
                raise GitHubGatewayRejected("provider_authorization_denied") from exc
            except github_app.GitHubAppReviewReadUnauthorized as exc:
                raise GitHubGatewayRejected("repository_not_authorized") from exc
            except GitHubReadError as exc:
                if exc.kind == "unauthorized" and attempt == 0:
                    self._tokens.invalidate(command.provider_repository_id)
                    attempt += 1
                    continue
                if exc.kind in {"unreachable", "http_error", "rate_limited"}:
                    raise GitHubGatewayRetryable("github_read_unavailable") from exc
                if exc.kind in {"unauthorized", "forbidden"}:
                    raise GitHubGatewayRejected(
                        "provider_authorization_denied"
                    ) from exc
                raise GitHubGatewayRejected("github_read_invalid") from exc

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


def _gateway_github_client(read_token: str) -> GitHubReadClient:
    """Bound one gateway operation well inside its durable delivery lease."""
    return GitHubReadClient(
        read_token,
        request_timeout_seconds=10.0,
        max_attempts=1,
    )
