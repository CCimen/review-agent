"""Feedback result values shared by the application and PostgreSQL owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


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
