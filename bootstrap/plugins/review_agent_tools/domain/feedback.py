"""Feedback result values shared by the application and PostgreSQL owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from urllib.parse import urlsplit


class FeedbackDomainError(ValueError):
    """A feedback value violates the durable command contract."""


class FeedbackStatus(StrEnum):
    RECORDED = "recorded"
    NO_MAPPING = "no_mapping"
    NOT_CURRENT = "not_current"
    STALE = "stale"
    UNAUTHORIZED = "unauthorized"
    IGNORED = "ignored"
    UNSUPPORTED = "unsupported"


class FeedbackTriageStatus(StrEnum):
    PENDING = "pending"
    ACTIONABLE = "actionable"
    DUPLICATE = "duplicate"
    INSUFFICIENT = "insufficient"
    RESOLVED = "resolved"


class FeedbackTargetOwner(StrEnum):
    SOURCE_TOOL = "source_tool"
    COVERAGE = "coverage"
    REVIEW_RULE = "review_rule"
    PROFILE = "profile"
    REPOSITORY_DECISION = "repository_decision"
    DOCUMENTATION = "documentation"


@dataclass(frozen=True, slots=True)
class FeedbackTriageDefinition:
    status: FeedbackTriageStatus
    stable_key: str | None
    target_owner: FeedbackTargetOwner | None
    evidence_reference: str | None
    path: str | None
    category: str | None
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    status: FeedbackStatus
    event_id: str
    replayed: bool = False
    decision_id: int | None = None
    feedback_id: int | None = None
    fingerprint: str = ""
    local_reference: str = ""
    title: str = ""
    context_hash: str = ""
    adr_id: str = ""
    expires_at: str | None = None


def resolve_event_id(value: str) -> str:
    event_id = " ".join(value.strip().split())
    if not event_id or "\x00" in event_id or len(event_id) > 200:
        raise FeedbackDomainError("event_id must contain 1 to 200 valid characters")
    return event_id


def resolve_repository(value: str) -> str:
    repository = value.strip()
    if (
        repository.count("/") != 1
        or repository.startswith("/")
        or repository.endswith("/")
    ):
        raise FeedbackDomainError("repository must be owner/name")
    owner, name = repository.split("/", maxsplit=1)
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in (owner, name)):
        raise FeedbackDomainError("repository must be owner/name")
    return repository


def resolve_positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value < 1:
        raise FeedbackDomainError(f"{field} must be a positive integer")
    return value


def resolve_text(
    value: str,
    *,
    field: str,
    maximum: int,
) -> str:
    text = " ".join(value.strip().split())
    if "\x00" in text or len(text) > maximum:
        raise FeedbackDomainError(f"{field} exceeds {maximum} characters")
    return text


_STABLE_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$")


def resolve_feedback_triage(
    *,
    status: str,
    stable_key: str,
    target_owner: str,
    evidence_reference: str,
    path: str,
    category: str,
    actor: str,
    reason: str,
) -> FeedbackTriageDefinition:
    """Normalize one human-governed missed-issue triage transition."""
    try:
        resolved_status = FeedbackTriageStatus(status.strip())
    except ValueError as exc:
        raise FeedbackDomainError("triage status is invalid") from exc

    resolved_key = resolve_text(stable_key, field="stable_key", maximum=160)
    resolved_owner = resolve_text(
        target_owner,
        field="target_owner",
        maximum=80,
    )
    if resolved_status is FeedbackTriageStatus.ACTIONABLE:
        if not _STABLE_KEY_RE.fullmatch(resolved_key):
            raise FeedbackDomainError(
                "actionable triage requires a lowercase stable_key"
            )
        try:
            owner = FeedbackTargetOwner(resolved_owner)
        except ValueError as exc:
            raise FeedbackDomainError(
                "actionable triage target_owner is invalid"
            ) from exc
        key: str | None = resolved_key
    else:
        if resolved_key or resolved_owner:
            raise FeedbackDomainError(
                "only actionable triage may set stable_key and target_owner"
            )
        key = None
        owner = None

    resolved_evidence = resolve_text(
        evidence_reference,
        field="evidence_reference",
        maximum=500,
    )
    if resolved_evidence:
        parsed = urlsplit(resolved_evidence)
        if parsed.scheme != "https" or not parsed.netloc:
            raise FeedbackDomainError("evidence_reference must be an HTTPS URL")

    resolved_path = resolve_text(path, field="path", maximum=500)
    if resolved_path and (
        resolved_path.startswith("/")
        or "\\" in resolved_path
        or any(part in {"", ".", ".."} for part in resolved_path.split("/"))
    ):
        raise FeedbackDomainError("path must be a normalized repository path")

    resolved_category = resolve_text(category, field="category", maximum=80)
    if resolved_category and not _CATEGORY_RE.fullmatch(resolved_category):
        raise FeedbackDomainError("category must be a lowercase identifier")

    resolved_actor = resolve_text(actor, field="actor", maximum=200)
    if not resolved_actor:
        raise FeedbackDomainError("actor is required")
    resolved_reason = resolve_text(reason, field="reason", maximum=2000)
    if not resolved_reason:
        raise FeedbackDomainError("reason is required")

    return FeedbackTriageDefinition(
        status=resolved_status,
        stable_key=key,
        target_owner=owner,
        evidence_reference=resolved_evidence or None,
        path=resolved_path or None,
        category=resolved_category or None,
        actor=resolved_actor,
        reason=resolved_reason,
    )
