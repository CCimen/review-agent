"""Pure parser for deterministic PR feedback commands."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

try:
    from .feedback_contract import contains_placeholder
    from .memory_validation import (
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        local_reference_number,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from feedback_contract import contains_placeholder
    from memory_validation import (
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        local_reference_number,
    )

DecisionFeedbackValue = Literal["false_positive", "intentional_by_design"]
ReviewFeedbackCategory = Literal["missed_issue", "scope_confusion"]


@dataclass(frozen=True)
class FindingFeedbackCommand:
    kind: Literal["finding"]
    decision: DecisionFeedbackValue
    local_reference: str
    reason: str
    adr_id: str = ""


@dataclass(frozen=True)
class ReviewQualityFeedbackCommand:
    kind: Literal["review_quality"]
    category: ReviewFeedbackCategory
    reason: str
    local_reference: str = ""


FeedbackCommand = FindingFeedbackCommand | ReviewQualityFeedbackCommand

__all__ = (
    "DecisionFeedbackValue",
    "FeedbackCommand",
    "FindingFeedbackCommand",
    "ReviewFeedbackCategory",
    "ReviewQualityFeedbackCommand",
    "parse_review_feedback_command",
    "restore_review_feedback_command",
)

_TRIGGER_RE = re.compile(r"^\s*[@/]review\b", re.IGNORECASE | re.MULTILINE)
_COMMAND_RE = re.compile(r"^\s*[@/]review(?:\s+(?P<body>.*))?\s*$", re.IGNORECASE | re.DOTALL)
_ADR_RE = re.compile(r"ADR-[A-Za-z0-9][A-Za-z0-9._-]{0,76}$")
_LEADING_BECAUSE_RE = re.compile(r"^because\b(?:\s*[:,-]?\s*)?", re.IGNORECASE)


def _local_reference(value: str) -> str:
    reference = clean_text(value, field="local_reference", maximum=12).upper()
    if local_reference_number(reference) < 1:
        raise ReviewMemoryError("local_reference must look like F1, F2, ...")
    return reference


def _reason(value: str) -> str:
    reason = clean_multiline(
        _LEADING_BECAUSE_RE.sub("", value.strip(), count=1).strip(),
        field="reason",
        maximum=2000,
    )
    if contains_placeholder(reason):
        raise ReviewMemoryError("replace placeholder text before submitting feedback")
    return reason


def _adr_id(value: str) -> str:
    adr_id = clean_text(value, field="ADR id", maximum=80)
    if not _ADR_RE.fullmatch(adr_id):
        raise ReviewMemoryError("ADR id must look like ADR-123")
    return adr_id


def parse_review_feedback_command(body: str) -> FeedbackCommand | None:
    text = str(body or "").strip()
    if len(_TRIGGER_RE.findall(text)) > 1:
        raise ReviewMemoryError("one feedback command per comment is supported")

    match = _COMMAND_RE.fullmatch(text)
    if not match:
        return None
    payload = str(match.group("body") or "").strip()
    if not payload:
        return None

    verb, _, rest = payload.partition(" ")
    normalized_verb = verb.strip().lower().replace("_", "-")
    rest = rest.strip()

    if normalized_verb in {"accepted-risk", "accepted_risk"}:
        raise ReviewMemoryError("accepted risk decisions require the governance CLI")

    if normalized_verb == "false-positive":
        reference, separator, reason = rest.partition(" ")
        if not separator:
            raise ReviewMemoryError("reason is required")
        return FindingFeedbackCommand(
            kind="finding",
            decision="false_positive",
            local_reference=_local_reference(reference),
            reason=_reason(reason),
        )

    if normalized_verb == "intentional":
        reference, separator, tail = rest.partition(" ")
        if not separator:
            raise ReviewMemoryError("intentional feedback requires an ADR id")
        raw_adr, separator, reason = tail.strip().partition(" ")
        if not separator:
            raise ReviewMemoryError("intentional feedback requires an ADR id and reason")
        return FindingFeedbackCommand(
            kind="finding",
            decision="intentional_by_design",
            local_reference=_local_reference(reference),
            adr_id=_adr_id(raw_adr),
            reason=_reason(reason),
        )

    if normalized_verb == "feedback":
        category, separator, reason = rest.partition(" ")
        category = category.strip().lower().rstrip(":")
        if category == "scope":
            reference, reference_separator, scope_reason = reason.strip().partition(" ")
            if not separator or not reference_separator:
                raise ReviewMemoryError("scope feedback requires a finding reference and reason")
            return ReviewQualityFeedbackCommand(
                kind="review_quality",
                category="scope_confusion",
                local_reference=_local_reference(reference),
                reason=_reason(scope_reason),
            )
        if category != "missed":
            raise ReviewMemoryError("unknown review feedback category")
        if not separator:
            raise ReviewMemoryError("reason is required")
        if reason.startswith(":"):
            reason = reason[1:].strip()
        return ReviewQualityFeedbackCommand(
            kind="review_quality",
            category="missed_issue",
            reason=_reason(reason),
        )

    raise ReviewMemoryError("unsupported review feedback command")


def restore_review_feedback_command(value: object) -> FeedbackCommand:
    """Restore one previously normalized command without accepting new syntax."""
    if not isinstance(value, Mapping):
        raise ReviewMemoryError("normalized feedback command must be an object")
    item = cast(Mapping[str, object], value)
    reason = item.get("reason")
    if not isinstance(reason, str):
        raise ReviewMemoryError("normalized feedback reason must be text")
    if "decision" in item:
        expected = {"decision", "local_reference", "reason"}
        if "adr_id" in item:
            expected.add("adr_id")
        if set(item) != expected:
            raise ReviewMemoryError("normalized finding feedback fields are invalid")
        decision = item.get("decision")
        if decision not in {"false_positive", "intentional_by_design"}:
            raise ReviewMemoryError("normalized feedback decision is invalid")
        raw_reference = item.get("local_reference")
        if not isinstance(raw_reference, str):
            raise ReviewMemoryError("normalized feedback reference must be text")
        raw_adr = item.get("adr_id", "")
        if not isinstance(raw_adr, str):
            raise ReviewMemoryError("normalized feedback ADR id must be text")
        adr_id = _adr_id(raw_adr) if raw_adr else ""
        if (decision == "intentional_by_design") != bool(adr_id):
            raise ReviewMemoryError("normalized intentional feedback requires an ADR id")
        return FindingFeedbackCommand(
            kind="finding",
            decision=cast(DecisionFeedbackValue, decision),
            local_reference=_local_reference(raw_reference),
            reason=_reason(reason),
            adr_id=adr_id,
        )
    expected = {"category", "reason"}
    if "local_reference" in item:
        expected.add("local_reference")
    if set(item) != expected:
        raise ReviewMemoryError("normalized review feedback fields are invalid")
    category = item.get("category")
    if category not in {"missed_issue", "scope_confusion"}:
        raise ReviewMemoryError("normalized review feedback category is invalid")
    raw_reference = item.get("local_reference", "")
    if not isinstance(raw_reference, str):
        raise ReviewMemoryError("normalized feedback reference must be text")
    local_reference = _local_reference(raw_reference) if raw_reference else ""
    if (category == "scope_confusion") != bool(local_reference):
        raise ReviewMemoryError("normalized scope feedback requires a finding reference")
    return ReviewQualityFeedbackCommand(
        kind="review_quality",
        category=cast(ReviewFeedbackCategory, category),
        reason=_reason(reason),
        local_reference=local_reference,
    )
