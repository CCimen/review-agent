"""Validation, constants, and small value helpers for review memory."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal, get_args

from . import finding as _finding

# Finding policy is owned by domain.finding; these are its string-keyed views
# for tool schemas and rendering.
SEVERITY_ORDER = tuple(severity.value for severity in _finding.Severity)
SEVERITY_PRIORITY = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}
SEVERITY_SCORE_GATES = {
    severity.value: gate for severity, gate in _finding.SCORE_GATES.items()
}
SEVERITIES = set(SEVERITY_ORDER)
MAX_FINDINGS_PER_REVIEW = _finding.MAX_FINDINGS_PER_REVIEW
FINDING_TEXT_LIMITS = _finding.TEXT_LIMITS
PRIOR_VERDICT_EVIDENCE_MAX = 320
PRIOR_VERDICTS_REQUIRING_EVIDENCE = frozenset({"resolved", "invalidated"})
PriorFindingVerdictValue = Literal[
    "resolved",
    "still_present",
    "partially_resolved",
    "invalidated",
    "suppressed",
    "not_checked",
]
PRIOR_FINDING_VERDICTS = get_args(PriorFindingVerdictValue)
REVIEW_FEEDBACK_CATEGORIES = {
    "useful",
    "too_verbose",
    "unclear",
    "too_speculative",
    "severity_too_high",
    "severity_too_low",
    "remediation_impractical",
    "missed_issue",
    "scope_confusion",
}
MIN_CONFIDENCE = _finding.MIN_CONFIDENCE
MIN_PUBLICATION_SCORE = min(SEVERITY_SCORE_GATES.values())
_ELLIPSIS = "..."


class ReviewMemoryError(ValueError):
    """Raised for invalid memory input."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    value = value or utc_now()
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ReviewMemoryError(f"{field} is required")
    if len(text) > maximum:
        raise ReviewMemoryError(f"{field} exceeds {maximum} characters")
    return text


def compact_text(value: Any, *, maximum: int = 800) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= maximum:
        return text
    return truncate_text(text, maximum=maximum)


def truncate_text(value: str, *, maximum: int) -> str:
    if maximum <= 0:
        return ""
    if len(value) <= maximum:
        return value
    if maximum <= len(_ELLIPSIS):
        return _ELLIPSIS[:maximum]
    limit = maximum - len(_ELLIPSIS)
    # Preserve length <= maximum; use a word break only when it keeps useful text.
    floor = limit // 2
    cut = max(value.rfind(" ", 0, limit + 1), value.rfind("\n", 0, limit + 1))
    if cut <= floor:
        cut = limit
    return value[:cut].rstrip() + _ELLIPSIS


def local_reference_number(value: str) -> int:
    match = re.fullmatch(r"F([1-9][0-9]*)", str(value or "").strip().upper())
    return int(match.group(1)) if match else 0


def clean_multiline(
    value: Any, *, field: str, maximum: int, required: bool = True
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ReviewMemoryError(f"{field} is required")
    if len(text) > maximum:
        raise ReviewMemoryError(f"{field} exceeds {maximum} characters")
    return text


def normalize_repository(repository: str) -> str:
    value = repository.strip().lower()
    if value.count("/") != 1 or not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", value):
        raise ReviewMemoryError("repository must be owner/name")
    return value


def normalize_path(path: str) -> str:
    value = path.strip().replace("\\", "/")
    if not value or value.startswith("/") or "\x00" in value:
        raise ReviewMemoryError("invalid repository path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReviewMemoryError("repository path may not contain traversal segments")
    if len(value) > 500:
        raise ReviewMemoryError("repository path is too long")
    return value
