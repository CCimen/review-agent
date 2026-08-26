"""Direct GitHub App delivery processing over the durable webhook ledger."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
import urllib.parse

import psycopg
from psycopg.rows import TupleRow

from .. import review_contract
from ..domain.review import JsonObject, JsonValue
from ..postgres import github_app, jobs, webhook_deliveries
from ..postgres.runtime import PostgreSQLRuntime
from ..review_run_application import (
    PostgresRunRequest,
    admit_postgres_review_in_transaction,
)
from ..source_control import GitHubReadClient, GitHubReadError
from .app_auth import (
    GitHubAppTokenPermanent,
    GitHubAppTokenRetryable,
    ReviewReadTokenService,
)


_IGNORED_REASONS = frozenset(
    {
        "bot_sender",
        "invalid_command",
        "not_pull_request",
        "not_review_command",
        "unsupported_action",
    }
)


@dataclass(frozen=True, slots=True)
class ProcessorConfig:
    profile: str
    policy_revision: str
    job_priority: int
    job_max_attempts: int
    active_job_limit: int
    # Two bounded GitHub reads plus token exchange fit inside this lease.
    lease_duration: timedelta = timedelta(minutes=5)
    retry_delay: timedelta = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class PullSnapshot:
    repository_id: int
    repository: str
    number: int
    state: str
    base_sha: str
    head_sha: str
    head_repository_id: int | None
    head_repository: str | None


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    delivery_id: int
    status: webhook_deliveries.DeliveryStatus
    reason: str | None
    run_id: int | None = None
    job_id: int | None = None


@dataclass(frozen=True, slots=True)
class _ReviewCommand:
    provider_installation_id: int
    provider_repository_id: int
    repository: str
    pr_number: int
    comment_id: int
    sender_id: int
    sender_login: str


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Retry(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _object(value: object, field: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise _Reject("invalid_normalized_payload")
    return cast(Mapping[str, JsonValue], value)


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise _Reject("invalid_normalized_payload")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Reject("invalid_normalized_payload")
    return value.strip()


def _github_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return cast(Mapping[str, object], value)


def _github_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return value.strip()


def _github_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubReadError("invalid_json", f"GitHub returned invalid {field}")
    return value


def _github_repository_name(value: object) -> str:
    name = _github_text(value, "repository name")
    parts = name.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubReadError(
            "invalid_json", "GitHub returned an invalid repository name"
        )
    return name


def read_pull_snapshot(
    github: GitHubReadClient, repository: str, pr_number: int
) -> PullSnapshot:
    """Read the exact live pull request and both repository identities."""
    quoted = urllib.parse.quote(repository, safe="/")
    root = _github_object(
        github.request_json(f"/repos/{quoted}/pulls/{pr_number}"),
        "GitHub pull request",
    )
    base = _github_object(root.get("base"), "pull request base")
    head = _github_object(root.get("head"), "pull request head")
    base_repository = _github_object(base.get("repo"), "base repository")
    raw_head_repository = head.get("repo")
    if raw_head_repository is None:
        head_repository_id = None
        head_repository = None
    else:
        head_repository_object = _github_object(raw_head_repository, "head repository")
        head_repository_id = _github_int(
            head_repository_object.get("id"), "head repository id"
        )
        head_repository = _github_repository_name(
            head_repository_object.get("full_name")
        )
    return PullSnapshot(
        repository_id=_github_int(base_repository.get("id"), "repository id"),
        repository=_github_repository_name(base_repository.get("full_name")),
        number=_github_int(root.get("number"), "pull request number"),
        state=_github_text(root.get("state"), "pull request state").lower(),
        base_sha=_github_text(base.get("sha"), "base sha"),
        head_sha=_github_text(head.get("sha"), "head sha"),
        head_repository_id=head_repository_id,
        head_repository=head_repository,
    )


def _installation_definition(
    delivery: webhook_deliveries.WebhookDelivery,
) -> github_app.InstallationDefinition:
    payload = _payload(delivery)
    installation_id = delivery.provider_installation_id
    if installation_id is None:
        raise _Reject("invalid_normalized_payload")
    try:
        return github_app.InstallationDefinition(
            provider_installation_id=installation_id,
            account_id=_positive(payload.get("account_id"), "account_id"),
            account_login=_text(payload.get("account_login"), "account_login"),
            account_type=github_app.AccountType(
                _text(payload.get("account_type"), "account_type")
            ),
            repository_selection=github_app.RepositorySelection(
                _text(payload.get("repository_selection"), "repository_selection")
            ),
            contents_permission=github_app.PermissionLevel(
                _text(payload.get("contents_permission"), "contents_permission")
            ),
            issues_permission=github_app.PermissionLevel(
                _text(payload.get("issues_permission"), "issues_permission")
            ),
            pull_requests_permission=github_app.PermissionLevel(
                _text(
                    payload.get("pull_requests_permission"),
                    "pull_requests_permission",
                )
            ),
        )
    except ValueError as exc:
        raise _Reject("invalid_normalized_payload") from exc


def _repositories(payload: Mapping[str, JsonValue]) -> tuple[tuple[int, str], ...]:
    raw = payload.get("repositories")
    if not isinstance(raw, list):
        raise _Reject("invalid_normalized_payload")
    result: list[tuple[int, str]] = []
    for value in raw:
        repository = _object(value, "repository")
        result.append(
            (
                _positive(repository.get("id"), "repository.id"),
                _text(repository.get("full_name"), "repository.full_name"),
            )
        )
    return tuple(result)


def _payload(
    delivery: webhook_deliveries.WebhookDelivery,
) -> Mapping[str, JsonValue]:
    if delivery.normalized_schema_version != 1:
        raise _Reject("unsupported_normalized_schema")
    return _object(delivery.normalized_payload, "normalized_payload")


def _review_command(
    delivery: webhook_deliveries.WebhookDelivery,
) -> _ReviewCommand:
    payload = _payload(delivery)
    if payload.get("kind") != "issue_comment":
        raise _Reject("invalid_normalized_payload")
    installation_id = delivery.provider_installation_id
    repository_id = delivery.provider_repository_id
    repository = delivery.repository_full_name
    if installation_id is None or repository_id is None or repository is None:
        raise _Reject("invalid_normalized_payload")
    return _ReviewCommand(
        provider_installation_id=installation_id,
        provider_repository_id=repository_id,
        repository=repository,
        pr_number=_positive(payload.get("pr_number"), "pr_number"),
        comment_id=_positive(payload.get("comment_id"), "comment_id"),
        sender_id=_positive(payload.get("sender_id"), "sender_id"),
        sender_login=_text(payload.get("sender_login"), "sender_login"),
    )


class GitHubAppProcessor:
    """Consume one durable App delivery without introducing another queue."""

    def __init__(
        self,
        *,
        postgres: PostgreSQLRuntime,
        tokens: ReviewReadTokenService,
        config: ProcessorConfig,
        github_factory: Callable[[str], GitHubReadClient] = GitHubReadClient,
    ) -> None:
        self._postgres = postgres
        self._tokens = tokens
        self._config = config
        self._github_factory = github_factory

    def process_next(self, *, lease_owner: str) -> ProcessingResult | None:
        """Claim and resolve one ready delivery."""
        actor = lease_owner
        with self._postgres.transaction() as connection:
            delivery = webhook_deliveries.claim_next_delivery(
                connection,
                lease_owner=lease_owner,
                lease_duration=self._config.lease_duration,
            )
        if delivery is None:
            return None

        try:
            if delivery.command_category is webhook_deliveries.CommandCategory.REVIEW:
                return self._process_review(delivery, lease_owner, actor)
            return self._process_without_github(delivery, lease_owner, actor)
        except _Retry as exc:
            return self._retry(delivery, lease_owner, actor, exc.reason)
        except _Reject as exc:
            return self._finish(
                delivery,
                lease_owner,
                actor,
                webhook_deliveries.TerminalStatus.REJECTED,
                exc.reason,
            )
        except github_app.GitHubAppStateError:
            return self._finish(
                delivery,
                lease_owner,
                actor,
                webhook_deliveries.TerminalStatus.REJECTED,
                "lifecycle_state_conflict",
            )

    def _process_without_github(
        self,
        delivery: webhook_deliveries.WebhookDelivery,
        lease_owner: str,
        actor: str,
    ) -> ProcessingResult:
        if delivery.command_category is webhook_deliveries.CommandCategory.IGNORED:
            reason = _payload(delivery).get("reason")
            if not isinstance(reason, str) or reason not in _IGNORED_REASONS:
                raise _Reject("invalid_normalized_payload")
            return self._finish(
                delivery,
                lease_owner,
                actor,
                webhook_deliveries.TerminalStatus.IGNORED,
                reason,
            )
        if delivery.command_category is webhook_deliveries.CommandCategory.FEEDBACK:
            return self._finish(
                delivery,
                lease_owner,
                actor,
                webhook_deliveries.TerminalStatus.IGNORED,
                "feedback_not_cut_over",
            )
        with self._postgres.transaction() as connection:
            if (
                delivery.command_category
                is webhook_deliveries.CommandCategory.INSTALLATION
            ):
                self._apply_installation(connection, delivery, actor)
            elif (
                delivery.command_category
                is webhook_deliveries.CommandCategory.REPOSITORY_ACCESS
            ):
                self._apply_repository_access(connection, delivery, actor)
            else:
                raise _Reject("unsupported_delivery_category")
            finished = webhook_deliveries.finish_delivery(
                connection,
                delivery_id=delivery.id,
                lease_owner=lease_owner,
                lease_generation=delivery.lease_generation,
                status=webhook_deliveries.TerminalStatus.ACCEPTED,
                actor=actor,
            )
        return ProcessingResult(finished.id, finished.status, None)

    def _apply_installation(
        self,
        connection: psycopg.Connection[TupleRow],
        delivery: webhook_deliveries.WebhookDelivery,
        actor: str,
    ) -> None:
        db = connection
        definition = _installation_definition(delivery)
        if (
            definition.repository_selection
            is not github_app.RepositorySelection.SELECTED
        ):
            raise _Reject("unsupported_selection")
        action = delivery.action
        if action in {"created", "new_permissions_accepted"}:
            installation = github_app.sync_installation(db, definition)
            if action == "created":
                for repository_id, full_name in _repositories(_payload(delivery)):
                    github_app.grant_repository_access(
                        db,
                        installation_id=installation.id,
                        provider_repository_id=repository_id,
                        full_name=full_name,
                        actor=actor,
                        reason="repository selected during App installation",
                    )
            return
        installation = github_app.get_installation_by_provider_id(
            db, definition.provider_installation_id, for_update=True
        )
        statuses = {
            "suspend": github_app.InstallationStatus.SUSPENDED,
            "unsuspend": github_app.InstallationStatus.ACTIVE,
            "deleted": github_app.InstallationStatus.DELETED,
        }
        status = statuses.get(action) if action is not None else None
        if status is None:
            raise _Reject("unsupported_installation_action")
        github_app.set_installation_status(
            db,
            installation_id=installation.id,
            status=status,
            actor=actor,
            reason=f"installation {action} delivery",
        )

    def _apply_repository_access(
        self,
        connection: psycopg.Connection[TupleRow],
        delivery: webhook_deliveries.WebhookDelivery,
        actor: str,
    ) -> None:
        db = connection
        payload = _payload(delivery)
        if payload.get("repository_selection") != "selected":
            raise _Reject("unsupported_selection")
        installation_provider_id = delivery.provider_installation_id
        if installation_provider_id is None:
            raise _Reject("invalid_normalized_payload")
        installation = github_app.get_installation_by_provider_id(
            db, installation_provider_id, for_update=True
        )
        repositories = _repositories(payload)
        if delivery.action == "added":
            for repository_id, full_name in repositories:
                github_app.grant_repository_access(
                    db,
                    installation_id=installation.id,
                    provider_repository_id=repository_id,
                    full_name=full_name,
                    actor=actor,
                    reason="repository added to App installation",
                )
            return
        if delivery.action == "removed":
            for repository_id, _ in repositories:
                github_app.remove_repository_access_for_installation(
                    db,
                    provider_repository_id=repository_id,
                    expected_provider_installation_id=installation_provider_id,
                    actor=actor,
                    reason="repository removed from App installation",
                )
            return
        raise _Reject("unsupported_repository_action")

    def _process_review(
        self,
        delivery: webhook_deliveries.WebhookDelivery,
        lease_owner: str,
        actor: str,
    ) -> ProcessingResult:
        command = _review_command(delivery)
        try:
            token = self._tokens.token_for(command.provider_repository_id)
            github = self._github_factory(token.value)
            self._authorize_sender(github, command)
            snapshot = read_pull_snapshot(github, command.repository, command.pr_number)
        except GitHubAppTokenRetryable as exc:
            raise _Retry("token_exchange_unavailable") from exc
        except GitHubAppTokenPermanent as exc:
            raise _Reject("token_exchange_rejected") from exc
        except github_app.GitHubAppReviewReadUnauthorized as exc:
            raise _Reject("repository_not_authorized") from exc
        except GitHubReadError as exc:
            if exc.kind in {"unreachable", "http_error", "unauthorized", "forbidden"}:
                raise _Retry("github_read_unavailable") from exc
            raise _Reject("github_read_invalid") from exc

        self._validate_snapshot(command, snapshot)
        try:
            contract = review_contract.load_packaged_contract(self._config.profile)
        except review_contract.ReviewContractError as exc:
            raise _Retry("review_profile_unavailable") from exc

        try:
            with self._postgres.transaction() as connection:
                github_app.authorize_review_admission(
                    connection,
                    provider_repository_id=command.provider_repository_id,
                    provider_installation_id=command.provider_installation_id,
                    profile_key=self._config.profile,
                )
                admitted = admit_postgres_review_in_transaction(
                    connection,
                    PostgresRunRequest(
                        provider="github",
                        provider_repository_id=snapshot.repository_id,
                        repository=snapshot.repository,
                        pr_number=snapshot.number,
                        base_sha=snapshot.base_sha,
                        head_sha=snapshot.head_sha,
                        policy_revision=self._config.policy_revision,
                        resolved_config_schema_version=2,
                        resolved_config=cast(
                            JsonObject, review_contract.resolved_config(contract)
                        ),
                        request_key=f"github:issue-comment:{command.comment_id}",
                        trigger_comment_id=command.comment_id,
                        trigger_user=command.sender_login,
                    ),
                    priority=self._config.job_priority,
                    max_attempts=self._config.job_max_attempts,
                    active_job_limit=self._config.active_job_limit,
                )
                finished = webhook_deliveries.finish_delivery(
                    connection,
                    delivery_id=delivery.id,
                    lease_owner=lease_owner,
                    lease_generation=delivery.lease_generation,
                    status=webhook_deliveries.TerminalStatus.ACCEPTED,
                    actor=actor,
                )
        except github_app.GitHubAppReviewReadUnauthorized as exc:
            raise _Reject("repository_not_authorized") from exc
        except (jobs.ReviewQueueFull, jobs.ReviewJobBusy) as exc:
            raise _Retry("review_queue_unavailable") from exc
        return ProcessingResult(
            finished.id,
            finished.status,
            None,
            run_id=int(admitted.run.run.id),
            job_id=admitted.job.job.id,
        )

    @staticmethod
    def _authorize_sender(github: GitHubReadClient, command: _ReviewCommand) -> None:
        repository = urllib.parse.quote(command.repository, safe="/")
        login = urllib.parse.quote(command.sender_login, safe="")
        payload = _github_object(
            github.request_json(
                f"/repos/{repository}/collaborators/{login}/permission",
                max_bytes=65_536,
            ),
            "collaborator permission",
        )
        user = _github_object(payload.get("user"), "permission user")
        permission = _github_text(payload.get("permission"), "permission").lower()
        if (
            permission not in {"write", "admin"}
            or _github_int(user.get("id"), "permission user id") != command.sender_id
            or _github_text(user.get("login"), "permission user login").casefold()
            != command.sender_login.casefold()
        ):
            raise _Reject("sender_not_authorized")

    @staticmethod
    def _validate_snapshot(command: _ReviewCommand, snapshot: PullSnapshot) -> None:
        if (
            snapshot.repository_id != command.provider_repository_id
            or snapshot.repository.casefold() != command.repository.casefold()
            or snapshot.number != command.pr_number
        ):
            raise _Reject("pull_request_identity_mismatch")
        if snapshot.state != "open":
            raise _Reject("pull_request_not_open")
        if (
            snapshot.head_repository_id != snapshot.repository_id
            or snapshot.head_repository is None
            or snapshot.head_repository.casefold() != snapshot.repository.casefold()
        ):
            raise _Reject("fork_source_not_supported")

    def _finish(
        self,
        delivery: webhook_deliveries.WebhookDelivery,
        lease_owner: str,
        actor: str,
        status: webhook_deliveries.TerminalStatus,
        reason: str,
    ) -> ProcessingResult:
        with self._postgres.transaction() as connection:
            finished = webhook_deliveries.finish_delivery(
                connection,
                delivery_id=delivery.id,
                lease_owner=lease_owner,
                lease_generation=delivery.lease_generation,
                status=status,
                actor=actor,
                failure_code=reason,
            )
        return ProcessingResult(finished.id, finished.status, reason)

    def _retry(
        self,
        delivery: webhook_deliveries.WebhookDelivery,
        lease_owner: str,
        actor: str,
        reason: str,
    ) -> ProcessingResult:
        with self._postgres.transaction() as connection:
            updated = webhook_deliveries.retry_or_fail_delivery(
                connection,
                delivery_id=delivery.id,
                lease_owner=lease_owner,
                lease_generation=delivery.lease_generation,
                actor=actor,
                failure_code=reason,
                retry_delay=self._config.retry_delay,
            )
        return ProcessingResult(updated.id, updated.status, reason)
