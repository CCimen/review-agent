"""Shared authentication and bounded normalization for GitHub webhooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
from typing import cast

from .domain.review import JsonObject, JsonValue
from .feedback_contract import COMPATIBLE_TRIGGERS
from .feedback_commands import (
    FindingFeedbackCommand,
    parse_review_feedback_command,
)
from .memory_validation import ReviewMemoryError


class GitHubWebhookError(ValueError):
    """A GitHub webhook cannot be normalized without guessing."""


class UnsupportedGitHubEvent(GitHubWebhookError):
    """A validly signed delivery is outside the subscribed event contract."""


class CommandKind(StrEnum):
    REVIEW = "review"
    FINDING_FEEDBACK = "finding_feedback"
    QUALITY_FEEDBACK = "quality_feedback"
    IGNORED = "ignored"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class NormalizedWebhook:
    schema_version: int
    event: str
    action: str
    provider_installation_id: int | None
    provider_repository_id: int | None
    repository: str | None
    command_kind: CommandKind | None
    normalized: JsonObject


_INSTALLATION_ACTIONS = frozenset(
    {"created", "deleted", "new_permissions_accepted", "suspend", "unsuspend"}
)
_REPOSITORY_ACTIONS = frozenset({"added", "removed"})
_PERMISSION_LEVELS = frozenset({"none", "read", "write"})
_REVIEW_TRIGGERS = frozenset(trigger.casefold() for trigger in COMPATIBLE_TRIGGERS)


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Compare one SHA-256 HMAC without leaking timing information."""
    if not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubWebhookError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _positive(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubWebhookError(f"{field} must be a positive integer")
    return value


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise GitHubWebhookError(f"{field} must be text")
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise GitHubWebhookError(f"{field} is required")
    if len(normalized) > maximum:
        raise GitHubWebhookError(f"{field} exceeds {maximum} characters")
    return normalized


def _repository(value: object) -> tuple[int, str]:
    repository = _object(value, "repository")
    identifier = _positive(repository.get("id"), "repository.id")
    full_name = _text(repository.get("full_name"), "repository.full_name", 200)
    parts = full_name.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubWebhookError("repository.full_name must be owner/name")
    return identifier, full_name


def _repositories(value: object, field: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise GitHubWebhookError(f"{field} must be an array")
    repositories: list[JsonValue] = []
    for item in cast(list[object], value):
        repository_id, full_name = _repository(item)
        repositories.append({"full_name": full_name, "id": repository_id})
    return repositories


def _installation_id(root: Mapping[str, object]) -> int:
    installation = _object(root.get("installation"), "installation")
    return _positive(installation.get("id"), "installation.id")


def _action(root: Mapping[str, object], *, default: str = "") -> str:
    value = root.get("action", default)
    return _text(value, "action", 80).lower()


def _permission(permissions: Mapping[str, object], name: str) -> str:
    raw = permissions.get(name, "none")
    value = _text(raw, f"installation.permissions.{name}", 20).lower()
    if value not in _PERMISSION_LEVELS:
        raise GitHubWebhookError(
            f"installation.permissions.{name} has an unsupported level"
        )
    return value


def _normalize_installation(root: Mapping[str, object]) -> NormalizedWebhook:
    action = _action(root)
    if action not in _INSTALLATION_ACTIONS:
        raise GitHubWebhookError("unsupported installation action")
    installation = _object(root.get("installation"), "installation")
    identifier = _positive(installation.get("id"), "installation.id")
    account = _object(installation.get("account"), "installation.account")
    account_type = _text(account.get("type"), "installation.account.type", 40).lower()
    if account_type not in {"user", "organization"}:
        raise GitHubWebhookError("installation.account.type is unsupported")
    selection = _text(
        installation.get("repository_selection"),
        "installation.repository_selection",
        20,
    ).lower()
    if selection not in {"selected", "all"}:
        raise GitHubWebhookError("installation.repository_selection is unsupported")
    permissions = _object(
        installation.get("permissions", {}), "installation.permissions"
    )
    normalized: dict[str, JsonValue] = {
        "account_id": _positive(account.get("id"), "installation.account.id"),
        "account_login": _text(account.get("login"), "installation.account.login", 100),
        "account_type": account_type,
        "contents_permission": _permission(permissions, "contents"),
        "issues_permission": _permission(permissions, "issues"),
        "kind": "installation",
        "pull_requests_permission": _permission(permissions, "pull_requests"),
        "repository_selection": selection,
    }
    if action == "created":
        raw_repositories = root.get("repositories")
        normalized["repositories"] = (
            []
            if raw_repositories is None
            else _repositories(raw_repositories, "repositories")
        )
    return NormalizedWebhook(
        schema_version=1,
        event="installation",
        action=action,
        provider_installation_id=identifier,
        provider_repository_id=None,
        repository=None,
        command_kind=None,
        normalized=normalized,
    )


def _normalize_repositories(root: Mapping[str, object]) -> NormalizedWebhook:
    action = _action(root)
    if action not in _REPOSITORY_ACTIONS:
        raise GitHubWebhookError("unsupported installation_repositories action")
    identifier = _installation_id(root)
    field = "repositories_added" if action == "added" else "repositories_removed"
    selection = _text(
        root.get("repository_selection"),
        "repository_selection",
        20,
    ).lower()
    if selection not in {"selected", "all"}:
        raise GitHubWebhookError("repository_selection is unsupported")
    repositories = _repositories(root.get(field), field)
    return NormalizedWebhook(
        schema_version=1,
        event="installation_repositories",
        action=action,
        provider_installation_id=identifier,
        provider_repository_id=None,
        repository=None,
        command_kind=None,
        normalized={
            "kind": "installation_repositories",
            "repository_selection": selection,
            "repositories": repositories,
        },
    )


def _ignored_issue_comment(
    *,
    action: str,
    installation_id: int,
    repository_id: int,
    repository: str,
    normalized: dict[str, JsonValue],
    reason: str,
) -> NormalizedWebhook:
    normalized["reason"] = reason
    return NormalizedWebhook(
        schema_version=1,
        event="issue_comment",
        action=action,
        provider_installation_id=installation_id,
        provider_repository_id=repository_id,
        repository=repository,
        command_kind=CommandKind.IGNORED,
        normalized=normalized,
    )


def _normalize_issue_comment(root: Mapping[str, object]) -> NormalizedWebhook:
    action = _action(root)
    installation_id = _installation_id(root)
    repository_id, repository = _repository(root.get("repository"))
    issue = _object(root.get("issue"), "issue")
    comment = _object(root.get("comment"), "comment")
    sender = _object(root.get("sender"), "sender")
    normalized: dict[str, JsonValue] = {
        "author_association": _text(
            comment.get("author_association"), "comment.author_association", 80
        ).upper(),
        "comment_id": _positive(comment.get("id"), "comment.id"),
        "kind": "issue_comment",
        "pr_number": _positive(issue.get("number"), "issue.number"),
        "sender_id": _positive(sender.get("id"), "sender.id"),
        "sender_login": _text(sender.get("login"), "sender.login", 100),
        "sender_type": _text(sender.get("type"), "sender.type", 40),
    }
    if action != "created":
        return _ignored_issue_comment(
            action=action,
            installation_id=installation_id,
            repository_id=repository_id,
            repository=repository,
            normalized=normalized,
            reason="unsupported_action",
        )
    if not isinstance(issue.get("pull_request"), Mapping):
        return _ignored_issue_comment(
            action=action,
            installation_id=installation_id,
            repository_id=repository_id,
            repository=repository,
            normalized=normalized,
            reason="not_pull_request",
        )
    if str(normalized["sender_type"]).casefold() == "bot":
        return _ignored_issue_comment(
            action=action,
            installation_id=installation_id,
            repository_id=repository_id,
            repository=repository,
            normalized=normalized,
            reason="bot_sender",
        )

    raw_body = comment.get("body")
    if not isinstance(raw_body, str):
        raise GitHubWebhookError("comment.body must be text")
    body = raw_body.strip()
    if not body:
        return _ignored_issue_comment(
            action=action,
            installation_id=installation_id,
            repository_id=repository_id,
            repository=repository,
            normalized=normalized,
            reason="not_review_command",
        )
    if body.casefold() in _REVIEW_TRIGGERS:
        command_kind = CommandKind.REVIEW
    else:
        try:
            command = parse_review_feedback_command(body)
        except ReviewMemoryError:
            command_kind = CommandKind.INVALID
            normalized["reason"] = "invalid_command"
        else:
            if command is None:
                command_kind = CommandKind.IGNORED
                normalized["reason"] = "not_review_command"
            elif isinstance(command, FindingFeedbackCommand):
                command_kind = CommandKind.FINDING_FEEDBACK
                detail: dict[str, JsonValue] = {
                    "decision": command.decision,
                    "local_reference": command.local_reference,
                    "reason": command.reason,
                }
                if command.adr_id:
                    detail["adr_id"] = command.adr_id
                normalized["command"] = detail
            else:
                command_kind = CommandKind.QUALITY_FEEDBACK
                detail = {"category": command.category, "reason": command.reason}
                if command.local_reference:
                    detail["local_reference"] = command.local_reference
                normalized["command"] = detail

    return NormalizedWebhook(
        schema_version=1,
        event="issue_comment",
        action=action,
        provider_installation_id=installation_id,
        provider_repository_id=repository_id,
        repository=repository,
        command_kind=command_kind,
        normalized=normalized,
    )


def normalize_event(event: str, payload: object) -> NormalizedWebhook:
    """Extract only the bounded fields required for durable processing."""
    event_name = _text(event, "X-GitHub-Event", 80).lower()
    root = _object(payload, "payload")
    if event_name == "ping":
        return NormalizedWebhook(
            schema_version=1,
            event="ping",
            action="ping",
            provider_installation_id=None,
            provider_repository_id=None,
            repository=None,
            command_kind=None,
            normalized={"kind": "ping"},
        )
    if event_name == "installation":
        return _normalize_installation(root)
    if event_name == "installation_repositories":
        return _normalize_repositories(root)
    if event_name == "issue_comment":
        return _normalize_issue_comment(root)
    raise UnsupportedGitHubEvent("unsupported GitHub event")
