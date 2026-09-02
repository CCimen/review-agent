"""Direct GitHub App delivery processing over the durable webhook ledger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Literal, cast

import psycopg
from psycopg.rows import TupleRow

from .. import review_contract
from .. import review_feedback_application
from ..domain.feedback import FeedbackStatus
from ..domain.review import JsonObject, JsonValue
from ..feedback_commands import restore_review_feedback_command
from ..memory_validation import ReviewMemoryError
from ..postgres import (
    decisions as postgres_decisions,
    feedback as postgres_feedback,
    github_app,
    jobs,
    registry,
    webhook_deliveries,
)
from ..postgres.runtime import PostgreSQLRuntime
from ..review_run_application import (
    PostgresRunRequest,
    admit_postgres_review_in_transaction,
)
from .gateway import (
    GitHubGatewayError,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
)
from .gateway_client import ReviewGitHubGatewayClient


logger = logging.getLogger(__name__)


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
    contract_environment: Mapping[str, str] = field(repr=False, compare=False)
    # Provider authorization and snapshot reads fit inside this lease.
    lease_duration: timedelta = timedelta(minutes=5)
    retry_delay: timedelta = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    delivery_id: int
    status: webhook_deliveries.DeliveryStatus
    reason: str | None
    run_id: int | None = None
    job_id: int | None = None


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


class GitHubAppProcessor:
    """Consume one durable App delivery without introducing another queue."""

    def __init__(
        self,
        *,
        postgres: PostgreSQLRuntime,
        gateway: ReviewGitHubGatewayClient,
        config: ProcessorConfig,
    ) -> None:
        self._postgres = postgres
        self._gateway = gateway
        self._config = config

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
            if delivery.command_category is webhook_deliveries.CommandCategory.FEEDBACK:
                return self._process_feedback(delivery, lease_owner, actor)
            return self._process_without_github(delivery, lease_owner, actor)
        except GitHubGatewayProtocolError as exc:
            logger.warning(
                "GitHub gateway protocol failure for delivery %s: %s",
                delivery.id,
                type(exc).__name__,
            )
            return self._finish(
                delivery,
                lease_owner,
                actor,
                webhook_deliveries.TerminalStatus.FAILED,
                "github_gateway_invalid_response",
            )
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
        except registry.RepositoryNameConflict:
            return self._finish(
                delivery,
                lease_owner,
                actor,
                webhook_deliveries.TerminalStatus.REJECTED,
                "repository_name_conflict",
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
        action = delivery.action
        if action in {"created", "new_permissions_accepted"}:
            installation = github_app.sync_installation(db, definition)
            if (
                action == "created"
                and definition.repository_selection
                is github_app.RepositorySelection.SELECTED
            ):
                for repository_id, full_name in _repositories(_payload(delivery)):
                    github_app.grant_repository_access(
                        db,
                        installation_id=installation.id,
                        provider_repository_id=repository_id,
                        full_name=full_name,
                        actor=actor,
                        reason="repository selected during App installation",
                        trigger_mode=github_app.TriggerMode.AUTOMATIC,
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
        raw_selection = payload.get("repository_selection")
        if raw_selection not in {"selected", "all"}:
            raise _Reject("invalid_normalized_payload")
        installation_provider_id = delivery.provider_installation_id
        if installation_provider_id is None:
            raise _Reject("invalid_normalized_payload")
        installation = github_app.get_installation_by_provider_id(
            db, installation_provider_id, for_update=True
        )
        repositories = _repositories(payload)
        if delivery.action == "added":
            if raw_selection == "all":
                return
            for repository_id, full_name in repositories:
                github_app.grant_repository_access(
                    db,
                    installation_id=installation.id,
                    provider_repository_id=repository_id,
                    full_name=full_name,
                    actor=actor,
                    reason="repository added to App installation",
                    trigger_mode=github_app.TriggerMode.AUTOMATIC,
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
        try:
            authorized = self._gateway.authorize_review_delivery(
                delivery_id=delivery.id,
                lease_owner=lease_owner,
                lease_generation=delivery.lease_generation,
            )
        except GitHubGatewayRetryable as exc:
            raise _Retry(exc.reason) from exc
        except GitHubGatewayRejected as exc:
            if exc.reason == "delivery_lease_lost":
                return ProcessingResult(delivery.id, delivery.status, exc.reason)
            raise _Reject(exc.reason) from exc
        try:
            contract = review_contract.load_packaged_contract(
                self._config.profile,
                environment=self._config.contract_environment,
            )
        except review_contract.ReviewContractError as exc:
            raise _Retry("review_profile_unavailable") from exc

        try:
            with self._postgres.transaction() as connection:
                github_app.authorize_review_admission(
                    connection,
                    provider_repository_id=authorized.provider_repository_id,
                    provider_installation_id=authorized.provider_installation_id,
                    profile_key=self._config.profile,
                )
                admitted = admit_postgres_review_in_transaction(
                    connection,
                    PostgresRunRequest(
                        provider="github",
                        provider_repository_id=authorized.provider_repository_id,
                        repository=authorized.repository,
                        pr_number=authorized.pr_number,
                        base_sha=authorized.base_sha,
                        head_sha=authorized.head_sha,
                        policy_revision=self._config.policy_revision,
                        resolved_config_schema_version=2,
                        resolved_config=cast(
                            JsonObject, review_contract.resolved_config(contract)
                        ),
                        request_key=f"github:issue-comment:{authorized.comment_id}",
                        trigger_comment_id=authorized.comment_id,
                        trigger_user=authorized.sender_login,
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
        except github_app.GitHubAppRepositoryUnauthorized as exc:
            raise _Reject("repository_not_authorized") from exc
        except (jobs.ReviewQueueFull, jobs.ReviewJobBusy) as exc:
            raise _Retry("review_queue_unavailable") from exc

        try:
            self._gateway.acknowledge_review(
                run_id=int(admitted.run.run.id),
            )
        except GitHubGatewayError as exc:
            logger.warning(
                "Review run %s was admitted without a GitHub acknowledgement: %s",
                int(admitted.run.run.id),
                type(exc).__name__,
            )
        return ProcessingResult(
            finished.id,
            finished.status,
            None,
            run_id=int(admitted.run.run.id),
            job_id=admitted.job.job.id,
        )

    def _process_feedback(
        self,
        delivery: webhook_deliveries.WebhookDelivery,
        lease_owner: str,
        actor: str,
    ) -> ProcessingResult:
        try:
            authorized = self._gateway.authorize_feedback_delivery(
                delivery_id=delivery.id,
                lease_owner=lease_owner,
                lease_generation=delivery.lease_generation,
            )
        except GitHubGatewayRetryable as exc:
            raise _Retry(exc.reason) from exc
        except GitHubGatewayRejected as exc:
            if exc.reason == "delivery_lease_lost":
                return ProcessingResult(delivery.id, delivery.status, exc.reason)
            raise _Reject(exc.reason) from exc
        payload = _payload(delivery)
        invalid_command = payload.get("reason") == "invalid_command"
        has_command = "command" in payload
        if invalid_command == has_command:
            raise _Reject("invalid_normalized_payload")
        if invalid_command:
            acknowledgement_status = "invalid"
            result_status: FeedbackStatus | None = None
        else:
            try:
                command = restore_review_feedback_command(payload.get("command"))
                result = review_feedback_application.record_postgres_feedback(
                    self._postgres,
                    event_id=f"github:issue-comment:{authorized.comment_id}",
                    repository=authorized.repository,
                    pr_number=authorized.pr_number,
                    command=command,
                    actor_user_id=authorized.sender_id,
                    actor_login=authorized.sender_login,
                    author_association=authorized.author_association,
                    authorization_version=authorized.authorization_version,
                    source_comment_id=authorized.comment_id,
                    source_comment_url=(
                        f"https://github.com/{authorized.repository}/pull/"
                        f"{authorized.pr_number}#issuecomment-{authorized.comment_id}"
                    ),
                )
            except (ReviewMemoryError, review_feedback_application.ReviewFeedbackError) as exc:
                raise _Reject("invalid_normalized_payload") from exc
            except (
                postgres_decisions.DecisionStoreError,
                postgres_feedback.FeedbackStoreError,
                psycopg.Error,
            ) as exc:
                raise _Retry("feedback_database_unavailable") from exc
            result_status = result.status
            if result_status not in {
                FeedbackStatus.RECORDED,
                FeedbackStatus.NO_MAPPING,
                FeedbackStatus.NOT_CURRENT,
                FeedbackStatus.STALE,
                FeedbackStatus.UNSUPPORTED,
            }:
                raise _Reject("invalid_feedback_outcome")
            acknowledgement_status = cast(
                Literal[
                    "recorded",
                    "no_mapping",
                    "not_current",
                    "stale",
                    "unsupported",
                ],
                result_status.value,
            )

        try:
            self._gateway.acknowledge_feedback(
                delivery_id=delivery.id,
                lease_owner=lease_owner,
                lease_generation=delivery.lease_generation,
                status=acknowledgement_status,
            )
        except GitHubGatewayRetryable as exc:
            raise _Retry(exc.reason) from exc
        except GitHubGatewayRejected as exc:
            if exc.reason == "delivery_lease_lost":
                return ProcessingResult(delivery.id, delivery.status, exc.reason)
            if result_status is not None:
                raise _Retry("feedback_acknowledgement_rejected") from exc
            raise _Reject(exc.reason) from exc
        except GitHubGatewayProtocolError as exc:
            if result_status is None:
                raise
            logger.warning(
                "Feedback for delivery %s was recorded without a GitHub "
                "acknowledgement: %s",
                delivery.id,
                type(exc).__name__,
            )

        if result_status is None:
            return self._finish(
                delivery,
                lease_owner,
                actor,
                webhook_deliveries.TerminalStatus.REJECTED,
                "invalid_command",
            )
        with self._postgres.transaction() as connection:
            finished = webhook_deliveries.finish_delivery(
                connection,
                delivery_id=delivery.id,
                lease_owner=lease_owner,
                lease_generation=delivery.lease_generation,
                status=webhook_deliveries.TerminalStatus.ACCEPTED,
                actor=actor,
            )
        return ProcessingResult(finished.id, finished.status, None)

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
