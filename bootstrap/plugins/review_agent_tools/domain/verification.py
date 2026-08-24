"""Provider-neutral verification and reconciliation values."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import NewType, TypeVar


VerificationRunId = NewType("VerificationRunId", int)
CandidateVerificationId = NewType("CandidateVerificationId", int)
CandidateReconciliationId = NewType("CandidateReconciliationId", int)


class VerificationMode(StrEnum):
    SHADOW = "shadow"
    ADVISE = "advise"
    GATE = "gate"


class VerificationStatus(StrEnum):
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class CandidateVerdict(StrEnum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class ReconciliationDecision(StrEnum):
    PUBLISH = "publish"
    DROP = "drop"


_BUNDLE_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_Choice = TypeVar("_Choice", bound=StrEnum)


class VerificationDomainError(ValueError):
    """A verification value violates its durable contract."""


@dataclass(frozen=True, slots=True)
class VerificationRunDefinition:
    provider: str | None
    model: str | None
    mode: VerificationMode
    status: VerificationStatus
    bundle_hash: str | None
    failure_code: str | None
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CandidateVerificationDefinition:
    verdict: CandidateVerdict
    confidence: float
    counter_evidence: str | None
    notes: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationDefinition:
    final_decision: ReconciliationDecision
    reason: str | None
    created_at: datetime


def _choice(value: str, *, field: str, choice: type[_Choice]) -> _Choice:
    normalized = value.strip().lower()
    try:
        return choice(normalized)
    except ValueError as exc:
        raise VerificationDomainError(
            f"{field} must be one of: "
            f"{', '.join(sorted(item.value for item in choice))}"
        ) from exc


def _single_line(
    value: str,
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str | None:
    normalized = " ".join(value.strip().split())
    if required and not normalized:
        raise VerificationDomainError(f"{field} is required")
    if "\x00" in normalized or len(normalized) > maximum:
        raise VerificationDomainError(f"{field} exceeds {maximum} characters")
    return normalized or None


def _multiline(
    value: str,
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str | None:
    normalized = value.strip()
    if required and not normalized:
        raise VerificationDomainError(f"{field} is required")
    if "\x00" in normalized or len(normalized) > maximum:
        raise VerificationDomainError(f"{field} exceeds {maximum} characters")
    return normalized or None


def _moment(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VerificationDomainError("verification timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def resolve_verification_run(
    *,
    provider: str = "",
    model: str = "",
    mode: str = "advise",
    status: str = "completed",
    bundle_hash: str = "",
    failure_code: str = "",
    now: datetime,
) -> VerificationRunDefinition:
    """Validate one external verifier attempt before persistence checkout."""
    resolved_mode = _choice(mode, field="mode", choice=VerificationMode)
    resolved_status = _choice(
        status, field="status", choice=VerificationStatus
    )
    resolved_bundle = _single_line(
        bundle_hash, field="bundle_hash", maximum=120
    )
    if resolved_bundle is not None and not _BUNDLE_HASH_RE.fullmatch(
        resolved_bundle
    ):
        raise VerificationDomainError(
            "bundle_hash must be a sha256:<64 hex> identifier"
        )
    resolved_failure = _single_line(
        failure_code,
        field="failure_code",
        maximum=120,
        required=resolved_status
        in {VerificationStatus.UNAVAILABLE, VerificationStatus.FAILED},
    )
    if resolved_status not in {
        VerificationStatus.UNAVAILABLE,
        VerificationStatus.FAILED,
    } and resolved_failure:
        raise VerificationDomainError(
            "failure_code only applies to unavailable or failed verification"
        )
    moment = _moment(now)
    return VerificationRunDefinition(
        provider=_single_line(provider, field="provider", maximum=80),
        model=_single_line(model, field="model", maximum=120),
        mode=resolved_mode,
        status=resolved_status,
        bundle_hash=resolved_bundle,
        failure_code=resolved_failure,
        started_at=moment,
        completed_at=(
            None if resolved_status is VerificationStatus.RUNNING else moment
        ),
    )


def resolve_candidate_verification(
    *,
    verdict: str,
    confidence: float,
    counter_evidence: str = "",
    notes: str = "",
    now: datetime,
) -> CandidateVerificationDefinition:
    """Validate one candidate verdict before persistence checkout."""
    resolved_verdict = _choice(
        verdict, field="verdict", choice=CandidateVerdict
    )
    if isinstance(confidence, bool):
        raise VerificationDomainError("confidence must be a number")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise VerificationDomainError("confidence must be a number") from exc
    if not math.isfinite(confidence_value) or not 0 <= confidence_value <= 1:
        raise VerificationDomainError("confidence must be between 0 and 1")
    counter = _multiline(
        counter_evidence,
        field="counter_evidence",
        maximum=4_000,
        required=resolved_verdict is CandidateVerdict.REFUTED,
    )
    return CandidateVerificationDefinition(
        verdict=resolved_verdict,
        confidence=confidence_value,
        counter_evidence=counter,
        notes=_multiline(notes, field="notes", maximum=2_000),
        created_at=_moment(now),
    )


def resolve_reconciliation(
    *,
    final_decision: str,
    reason: str = "",
    now: datetime,
) -> ReconciliationDefinition:
    """Validate one final candidate decision before persistence checkout."""
    decision = _choice(
        final_decision,
        field="final_decision",
        choice=ReconciliationDecision,
    )
    return ReconciliationDefinition(
        final_decision=decision,
        reason=_multiline(
            reason,
            field="reason",
            maximum=4_000,
            required=decision is ReconciliationDecision.DROP,
        ),
        created_at=_moment(now),
    )
