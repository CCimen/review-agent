"""Stable finding identity and occurrence values."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import NewType

from .review import ReviewRunId, resolve_review_path


FindingId = NewType("FindingId", int)
FindingOccurrenceId = NewType("FindingOccurrenceId", int)
FindingDecisionId = NewType("FindingDecisionId", int)

MAX_FINDINGS_PER_REVIEW = 200
MIN_FINGERPRINT_PREFIX = 8
MIN_CONFIDENCE = 0.85

_RULE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")
_HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class FindingDomainError(ValueError):
    """A finding value violates the persisted domain contract."""


class Severity(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class FindingCategory(StrEnum):
    SECURITY = "security"
    CORRECTNESS = "correctness"
    RELIABILITY = "reliability"
    CONTRACTS = "contracts"
    TESTS = "tests"
    MAINTAINABILITY = "maintainability"
    PERFORMANCE = "performance"
    MIGRATION = "migration"


class DecisionKind(StrEnum):
    FALSE_POSITIVE = "false_positive"
    INTENTIONAL_BY_DESIGN = "intentional_by_design"
    ACCEPTED_RISK = "accepted_risk"
    DUPLICATE = "duplicate"
    RESOLVED = "resolved"
    REOPEN = "reopen"


SUPPRESSIVE_DECISION_KINDS = frozenset(
    {
        DecisionKind.FALSE_POSITIVE,
        DecisionKind.INTENTIONAL_BY_DESIGN,
        DecisionKind.ACCEPTED_RISK,
        DecisionKind.DUPLICATE,
    }
)


_SCORE_GATE = {
    Severity.CRITICAL: 8,
    Severity.HIGH: 8,
    Severity.MEDIUM: 7,
    Severity.LOW: 7,
}

_TEXT_LIMITS = {
    "title": 160,
    "evidence": 900,
    "disproof_checks": 600,
    "impact": 700,
    "smallest_fix": 700,
}


@dataclass(frozen=True, slots=True)
class FindingInput:
    rule_id: str
    path: str
    line: int
    symbol: str
    anchor: str
    title: str
    severity: str
    category: str
    publication_score: int
    confidence: float
    evidence: str
    disproof_checks: str
    impact: str
    smallest_fix: str
    introduced_by_diff: bool


@dataclass(frozen=True, slots=True)
class FindingDefinition:
    fingerprint: str
    rule_id: str
    path: str
    line: int
    symbol: str | None
    anchor: str
    title: str
    severity: Severity
    category: FindingCategory
    publication_score: int
    confidence: float
    context_hash: str
    evidence: str
    disproof_checks: str
    impact: str
    smallest_fix: str


@dataclass(frozen=True, slots=True)
class FingerprintQuery:
    value: str
    exact: bool


@dataclass(frozen=True, slots=True)
class FindingDecisionDefinition:
    decision: DecisionKind
    reason: str
    actor: str
    adr_id: str | None
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class IntentionalDesignEvidence:
    """Exact accepted repository decision used to justify one human decision."""

    review_run_id: ReviewRunId
    review_decision_snapshot_id: int
    repository_decision_id: str
    repository_decision_metadata_hash: str
    repository_decision_path: str
    repository_decision_base_sha: str


@dataclass(frozen=True, slots=True)
class FindingDecision:
    id: FindingDecisionId
    finding_id: FindingId
    occurrence_id: FindingOccurrenceId | None
    decision: DecisionKind
    reason: str
    actor: str
    context_hash: str
    adr_id: str | None
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RepeatFinding:
    fingerprint: str
    local_reference: str
    previous_run_id: ReviewRunId
    previous_head: str
    rule_id: str
    path: str
    line: int
    symbol: str | None
    anchor: str
    title: str
    severity: Severity
    category: FindingCategory
    publication_score: int
    confidence: float
    context_hash: str
    prior_claim: str
    prior_disproof_checks: str
    prior_impact: str
    prior_smallest_fix: str


def require_unique_finding_identities(
    definitions: Sequence[FindingDefinition],
) -> None:
    """Reject a batch that contains the same canonical identity more than once."""
    if len({item.fingerprint for item in definitions}) != len(definitions):
        raise FindingDomainError("finding batch contains duplicate stable identities")


def require_explicit_decision_target(
    *,
    occurrence_id: FindingOccurrenceId | None,
    pr_number: int | None,
    local_reference: str | None,
    latest: bool,
) -> None:
    """Require exactly one complete, explicit occurrence selector."""
    selectors = sum(
        (
            occurrence_id is not None,
            pr_number is not None or local_reference is not None,
            latest,
        )
    )
    if selectors != 1 or (pr_number is None) != (local_reference is None):
        raise FindingDomainError(
            "select exactly one occurrence_id, PR-local reference, or latest target"
        )


def _single_line(value: str, *, field: str, maximum: int, required: bool = True) -> str:
    normalized = " ".join(value.strip().split())
    if required and not normalized:
        raise FindingDomainError(f"{field} is required")
    if "\x00" in normalized or len(normalized) > maximum:
        raise FindingDomainError(f"{field} exceeds {maximum} characters")
    return normalized


def _multiline(value: str, *, field: str) -> str:
    normalized = value.strip()
    maximum = _TEXT_LIMITS[field]
    if not normalized:
        raise FindingDomainError(f"{field} is required")
    if "\x00" in normalized or len(normalized) > maximum:
        raise FindingDomainError(f"{field} exceeds {maximum} characters")
    return normalized


def resolve_decision(
    *,
    decision: str,
    reason: str,
    actor: str,
    adr_id: str = "",
    expires_days: int | None = None,
    now: datetime,
) -> FindingDecisionDefinition:
    """Validate a human decision before any persistence checkout."""
    try:
        kind = DecisionKind(decision.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(sorted(item.value for item in DecisionKind))
        raise FindingDomainError(f"decision must be one of: {allowed}") from exc
    clean_reason = reason.strip()
    if not clean_reason or "\x00" in clean_reason or len(clean_reason) > 2_000:
        raise FindingDomainError("reason must contain 1 to 2000 valid characters")
    clean_actor = _single_line(actor, field="actor", maximum=200)
    clean_adr = _single_line(
        adr_id, field="adr_id", maximum=80, required=False
    ) or None
    if kind is DecisionKind.INTENTIONAL_BY_DESIGN and clean_adr is None:
        raise FindingDomainError("intentional_by_design requires an ADR id")

    expires_at: datetime | None = None
    if kind in SUPPRESSIVE_DECISION_KINDS:
        days = 180 if expires_days is None else expires_days
        if isinstance(days, bool) or days < 1 or days > 3_650:
            raise FindingDomainError("expires_days must be between 1 and 3650")
        expires_at = now + timedelta(days=days)
    elif expires_days is not None:
        raise FindingDomainError("expires_days only applies to suppressive decisions")
    return FindingDecisionDefinition(
        decision=kind,
        reason=clean_reason,
        actor=clean_actor,
        adr_id=clean_adr,
        created_at=now,
        expires_at=expires_at,
    )


def suppression_is_active(
    *,
    decision: DecisionKind,
    decision_context_hash: str,
    current_context_hash: str,
    expires_at: datetime | None,
    now: datetime,
    intentional_evidence_current: bool = False,
) -> bool:
    """Apply a suppression only to the exact context a human reviewed."""
    return (
        decision in SUPPRESSIVE_DECISION_KINDS
        and (
            decision is not DecisionKind.INTENTIONAL_BY_DESIGN
            or intentional_evidence_current
        )
        and expires_at is not None
        and expires_at > now
        and bool(decision_context_hash)
        and decision_context_hash == current_context_hash
    )


def _rule_id(value: str) -> str:
    normalized = value.strip().lower()
    if not _RULE_RE.fullmatch(normalized):
        raise FindingDomainError(
            "rule_id must be stable lower-case letters, digits, dots, dashes, or underscores"
        )
    return normalized


def resolve_finding_path(value: str) -> str:
    """Normalize the existing application path contract into durable form."""
    return resolve_review_path(value.strip().replace("\\", "/"))


def compute_fingerprint(
    *, rule_id: str, path: str, symbol: str, anchor: str
) -> str:
    """Hash stable local identity; repository scope belongs in the database key."""
    canonical = "\n".join(
        (
            _rule_id(rule_id),
            resolve_finding_path(path),
            _single_line(symbol, field="symbol", maximum=200, required=False).lower(),
            _single_line(anchor, field="anchor", maximum=240).lower(),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_context_hash(value: str) -> str:
    normalized = value.strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise FindingDomainError(
            "context_hash must be a 40 to 64 character hexadecimal hash"
        )
    return normalized


def resolve_fingerprint_query(
    value: str, *, min_prefix: int = MIN_FINGERPRINT_PREFIX
) -> FingerprintQuery:
    candidate = value.strip().lower()
    if _FINGERPRINT_RE.fullmatch(candidate):
        return FingerprintQuery(value=candidate, exact=True)
    if (
        len(candidate) < min_prefix
        or len(candidate) > 64
        or not re.fullmatch(r"[0-9a-f]+", candidate)
    ):
        raise FindingDomainError(
            f"fingerprint prefix must contain at least {min_prefix} hex characters"
        )
    return FingerprintQuery(value=candidate, exact=False)


def resolve_finding(
    item: FindingInput, *, context_hash: str
) -> FindingDefinition:
    """Apply the current reviewer admission policy before pool checkout."""
    rule_id = _rule_id(item.rule_id)
    path = resolve_finding_path(item.path)
    symbol = (
        _single_line(item.symbol, field="symbol", maximum=200, required=False).lower()
        or None
    )
    anchor = _single_line(item.anchor, field="anchor", maximum=240).lower()
    title = _single_line(item.title, field="title", maximum=_TEXT_LIMITS["title"])
    try:
        severity = Severity(item.severity.strip().title())
    except ValueError as exc:
        raise FindingDomainError(
            "severity must be one of: Critical, High, Low, Medium"
        ) from exc
    try:
        category = FindingCategory(item.category.strip().lower())
    except ValueError as exc:
        raise FindingDomainError(
            "category must be a supported reviewer category"
        ) from exc
    if (
        isinstance(item.publication_score, bool)
        or item.publication_score < _SCORE_GATE[severity]
        or item.publication_score > 10
    ):
        raise FindingDomainError(
            f"publication_score for {severity.value} must be between "
            f"{_SCORE_GATE[severity]} and 10"
        )
    if isinstance(item.confidence, bool):
        raise FindingDomainError("confidence must be between 0.85 and 1.00")
    confidence = float(item.confidence)
    if (
        not math.isfinite(confidence)
        or confidence < MIN_CONFIDENCE
        or confidence > 1.0
    ):
        raise FindingDomainError("confidence must be between 0.85 and 1.00")
    confidence = float(
        Decimal(str(confidence)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    )
    if item.introduced_by_diff is not True:
        raise FindingDomainError("introduced_by_diff must be true")
    if isinstance(item.line, bool) or item.line < 1:
        raise FindingDomainError("line must be positive")

    return FindingDefinition(
        fingerprint=compute_fingerprint(
            rule_id=rule_id,
            path=path,
            symbol=symbol or "",
            anchor=anchor,
        ),
        rule_id=rule_id,
        path=path,
        line=item.line,
        symbol=symbol,
        anchor=anchor,
        title=title,
        severity=severity,
        category=category,
        publication_score=item.publication_score,
        confidence=confidence,
        context_hash=resolve_context_hash(context_hash),
        evidence=_multiline(item.evidence, field="evidence"),
        disproof_checks=_multiline(item.disproof_checks, field="disproof_checks"),
        impact=_multiline(item.impact, field="impact"),
        smallest_fix=_multiline(item.smallest_fix, field="smallest_fix"),
    )
