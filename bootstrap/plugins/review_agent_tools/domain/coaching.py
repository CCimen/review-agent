"""Provider-neutral reviewer-coaching input values."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final, Literal


CoachRunDecision = Literal["propose", "no_change"]
CoachInterventionOutcome = Literal[
    "accepted",
    "rejected_regression",
    "rejected_no_improvement",
    "rejected_insufficient_evidence",
    "rejected_wrong_owner",
    "withdrawn",
]
COACH_INTERVENTION_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "accepted",
        "rejected_regression",
        "rejected_no_improvement",
        "rejected_insufficient_evidence",
        "rejected_wrong_owner",
        "withdrawn",
    }
)
_EVALUATED_INTERVENTION_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"accepted", "rejected_regression", "rejected_no_improvement"}
)
_INTERVENTION_SCHEMA_VERSION: Final = 1
_SHA256_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")


class CoachingDomainError(ValueError):
    """A coaching value violates its durable evidence contract."""


@dataclass(frozen=True, slots=True)
class CoachCandidateInput:
    candidate_key: str
    target_owner: str
    suggested_route: str
    event_type: str
    independent_episode_count: int
    evidence_event_ids: tuple[str, ...]
    evidence_events_total: int


@dataclass(frozen=True, slots=True)
class CoachRunInput:
    repository: str
    source_event_set_id: str
    source_snapshot_id: str
    proposal_set_id: str
    decision: CoachRunDecision
    events_considered: int
    artifact_dir: str
    candidates: tuple[CoachCandidateInput, ...]


@dataclass(frozen=True, slots=True)
class CoachRunDefinition:
    repository: str | None
    source_event_set_id: str
    source_snapshot_id: str | None
    proposal_set_id: str
    decision: CoachRunDecision
    events_considered: int
    artifact_dir: str | None
    candidates: tuple[CoachCandidateInput, ...]


@dataclass(frozen=True, slots=True)
class CoachInterventionOutcomeInput:
    coach_candidate_id: int
    candidate_key: str
    target_owner: str
    proposal_content_hash: str
    base_contract_hash: str
    diff_hash: str
    validation_receipt_hash: str
    outcome: CoachInterventionOutcome
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class CoachInterventionOutcomeDefinition:
    coach_candidate_id: int
    intervention_key: str
    proposal_content_hash: str
    base_contract_hash: str
    diff_hash: str | None
    validation_receipt_hash: str | None
    outcome: CoachInterventionOutcome
    reason: str
    actor: str


def _single_line(
    value: str,
    *,
    field: str,
    maximum: int,
    required: bool = True,
) -> str | None:
    normalized = " ".join(value.strip().split())
    if required and not normalized:
        raise CoachingDomainError(f"{field} is required")
    if "\x00" in normalized or len(normalized) > maximum:
        raise CoachingDomainError(f"{field} exceeds {maximum} characters")
    return normalized or None


def _sha256_id(value: str, *, field: str, required: bool = True) -> str | None:
    text = _single_line(value, field=field, maximum=80, required=required)
    if text is None:
        return None
    if not _SHA256_ID_RE.fullmatch(text):
        raise CoachingDomainError(f"{field} must be a sha256:<64 hex> identifier")
    return text


def _repository(value: str) -> str | None:
    normalized = value.strip().lower()
    if not normalized:
        return None
    if not _REPOSITORY_RE.fullmatch(normalized):
        raise CoachingDomainError("repository must be owner/name")
    return normalized


def resolve_coach_repository(value: str) -> str | None:
    """Normalize an optional repository scope for coaching queries."""
    return _repository(value)


def _candidate(item: CoachCandidateInput) -> CoachCandidateInput:
    if isinstance(item.independent_episode_count, bool) or (
        item.independent_episode_count < 1
    ):
        raise CoachingDomainError("independent_episode_count must be positive")
    if isinstance(item.evidence_events_total, bool) or (
        item.evidence_events_total < len(item.evidence_event_ids)
    ):
        raise CoachingDomainError(
            "evidence_events_total must be at least the evidence_event_ids count"
        )
    evidence: set[str] = set()
    for value in item.evidence_event_ids:
        cleaned = _single_line(
            value,
            field="evidence_event_id",
            maximum=120,
        )
        if cleaned is not None:
            evidence.add(cleaned)
    if not evidence:
        raise CoachingDomainError("evidence_event_ids must not be empty")
    return CoachCandidateInput(
        candidate_key=_single_line(
            item.candidate_key, field="candidate_key", maximum=160
        )
        or "",
        target_owner=_single_line(
            item.target_owner, field="target_owner", maximum=120
        )
        or "",
        suggested_route=_single_line(
            item.suggested_route, field="suggested_route", maximum=120
        )
        or "",
        event_type=_single_line(
            item.event_type, field="event_type", maximum=120
        )
        or "",
        independent_episode_count=int(item.independent_episode_count),
        evidence_event_ids=tuple(sorted(evidence)),
        evidence_events_total=int(item.evidence_events_total),
    )


def resolve_coach_run(item: CoachRunInput) -> CoachRunDefinition:
    """Validate and normalize one immutable coach-run evidence batch."""
    if item.decision not in {"propose", "no_change"}:
        raise CoachingDomainError("coach run decision must be propose or no_change")
    if isinstance(item.events_considered, bool) or item.events_considered < 0:
        raise CoachingDomainError("events_considered must be zero or greater")
    if item.decision == "propose" and not item.candidates:
        raise CoachingDomainError("propose coach runs require at least one candidate")
    if item.decision == "no_change" and item.candidates:
        raise CoachingDomainError("no_change coach runs may not include candidates")
    candidates = tuple(_candidate(candidate) for candidate in item.candidates)
    if len({candidate.candidate_key for candidate in candidates}) != len(candidates):
        raise CoachingDomainError("coach run contains duplicate candidate keys")
    return CoachRunDefinition(
        repository=_repository(item.repository),
        source_event_set_id=_sha256_id(
            item.source_event_set_id, field="source_event_set_id"
        )
        or "",
        source_snapshot_id=_sha256_id(
            item.source_snapshot_id,
            field="source_snapshot_id",
            required=False,
        ),
        proposal_set_id=_sha256_id(
            item.proposal_set_id, field="proposal_set_id"
        )
        or "",
        decision=item.decision,
        events_considered=int(item.events_considered),
        artifact_dir=_single_line(
            item.artifact_dir,
            field="artifact_dir",
            maximum=1_000,
            required=False,
        ),
        candidates=candidates,
    )


def resolve_intervention_outcome(
    item: CoachInterventionOutcomeInput,
) -> CoachInterventionOutcomeDefinition:
    """Validate and identify one final evaluation of an exact intervention."""
    if isinstance(item.coach_candidate_id, bool) or item.coach_candidate_id < 1:
        raise CoachingDomainError("coach_candidate_id must be positive")
    if item.outcome not in COACH_INTERVENTION_OUTCOMES:
        raise CoachingDomainError("unsupported coach intervention outcome")
    candidate_key = _single_line(
        item.candidate_key, field="candidate_key", maximum=160
    ) or ""
    target_owner = _single_line(
        item.target_owner, field="target_owner", maximum=120
    ) or ""
    proposal_content_hash = _sha256_id(
        item.proposal_content_hash, field="proposal_content_hash"
    ) or ""
    base_contract_hash = _sha256_id(
        item.base_contract_hash, field="base_contract_hash"
    ) or ""
    diff_hash = _sha256_id(item.diff_hash, field="diff_hash", required=False)
    validation_receipt_hash = _sha256_id(
        item.validation_receipt_hash,
        field="validation_receipt_hash",
        required=False,
    )
    if item.outcome in _EVALUATED_INTERVENTION_OUTCOMES and (
        diff_hash is None or validation_receipt_hash is None
    ):
        raise CoachingDomainError(
            f"{item.outcome} requires diff_hash and validation_receipt_hash"
        )
    reason = _single_line(item.reason, field="reason", maximum=2_000) or ""
    actor = _single_line(item.actor, field="actor", maximum=200) or ""
    identity = {
        "schema_version": _INTERVENTION_SCHEMA_VERSION,
        "coach_candidate_id": item.coach_candidate_id,
        "candidate_key": candidate_key,
        "target_owner": target_owner,
        "base_contract_hash": base_contract_hash,
        "proposal_content_hash": proposal_content_hash,
        "diff_hash": diff_hash or "",
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    intervention_key = "sha256:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return CoachInterventionOutcomeDefinition(
        coach_candidate_id=item.coach_candidate_id,
        intervention_key=intervention_key,
        proposal_content_hash=proposal_content_hash,
        base_contract_hash=base_contract_hash,
        diff_hash=diff_hash,
        validation_receipt_hash=validation_receipt_hash,
        outcome=item.outcome,
        reason=reason,
        actor=actor,
    )
