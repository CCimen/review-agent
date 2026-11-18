"""The repository design evidence one review run preserves as a typed snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, cast

from . import repository_decisions
from .finding import IntentionalDesignEvidence
from .review import ReviewRunId


INDEX_PATH = ".review-agent/decisions.toml"
SNAPSHOT_SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CompletedStatus = Literal[
    "not_configured",
    "loaded",
    "unavailable",
    "invalid",
    "too_many_matches",
]
ContextStatus = Literal[
    "pending",
    "not_configured",
    "loaded",
    "unavailable",
    "invalid",
    "too_many_matches",
]


class RepositoryDecisionContextError(ValueError):
    """A stored decision snapshot violates its typed aggregate contract."""


@dataclass(frozen=True, slots=True)
class RepositoryDecisionContext:
    snapshot_id: int | None
    schema_version: int
    status: ContextStatus
    failure_code: str | None
    base_sha: str
    index_hash: str | None
    snapshot_hash: str
    decisions: tuple[repository_decisions.RepositoryDecision, ...]


def snapshot_value(context: RepositoryDecisionContext) -> dict[str, object]:
    """Return the versioned aggregate that is hashed and stored as JSONB."""
    if context.status == "pending":
        raise RepositoryDecisionContextError("pending context has no snapshot value")
    return {
        "schema_version": context.schema_version,
        "status": context.status,
        "failure_code": context.failure_code,
        "base_sha": context.base_sha,
        "index_path": INDEX_PATH,
        "index_hash": context.index_hash,
        "decisions": [
            repository_decisions.snapshot_value(decision)
            for decision in context.decisions
        ],
    }


def _snapshot_hash(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _completed(
    status: CompletedStatus,
    *,
    base_sha: str,
    failure_code: str | None = None,
    index_hash: str | None = None,
    decisions: tuple[repository_decisions.RepositoryDecision, ...] = (),
) -> RepositoryDecisionContext:
    if not _SHA_RE.fullmatch(base_sha):
        raise RepositoryDecisionContextError("decision context base SHA is invalid")
    if status == "loaded":
        if failure_code is not None or index_hash is None:
            raise RepositoryDecisionContextError(
                "loaded decision context requires an index hash and no failure"
            )
    elif status == "not_configured":
        if failure_code is not None or index_hash is not None or decisions:
            raise RepositoryDecisionContextError(
                "not-configured decision context must contain no repository data"
            )
    elif not failure_code or decisions:
        raise RepositoryDecisionContextError(
            "failed decision context requires a code and no partial decisions"
        )
    context = RepositoryDecisionContext(
        snapshot_id=None,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        status=status,
        failure_code=failure_code,
        base_sha=base_sha,
        index_hash=index_hash,
        snapshot_hash="",
        decisions=decisions,
    )
    return RepositoryDecisionContext(
        snapshot_id=None,
        schema_version=context.schema_version,
        status=context.status,
        failure_code=context.failure_code,
        base_sha=context.base_sha,
        index_hash=context.index_hash,
        snapshot_hash=_snapshot_hash(snapshot_value(context)),
        decisions=context.decisions,
    )


def pending(*, base_sha: str) -> RepositoryDecisionContext:
    """Represent a run whose optional decision snapshot has not been loaded."""
    if not _SHA_RE.fullmatch(base_sha):
        raise RepositoryDecisionContextError("decision context base SHA is invalid")
    return RepositoryDecisionContext(
        snapshot_id=None,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        status="pending",
        failure_code=None,
        base_sha=base_sha,
        index_hash=None,
        snapshot_hash="",
        decisions=(),
    )


def loaded(
    *,
    base_sha: str,
    index_hash: str,
    decisions: tuple[repository_decisions.RepositoryDecision, ...],
) -> RepositoryDecisionContext:
    """Build a complete typed snapshot after deterministic source loading."""
    return _completed(
        "loaded",
        base_sha=base_sha,
        index_hash=index_hash,
        decisions=decisions,
    )


def not_configured(*, base_sha: str) -> RepositoryDecisionContext:
    """Record that the fixed repository decision index does not exist."""
    return _completed("not_configured", base_sha=base_sha)


def failed(
    status: Literal["unavailable", "invalid", "too_many_matches"],
    *,
    base_sha: str,
    failure_code: str,
    index_hash: str | None = None,
) -> RepositoryDecisionContext:
    """Build a complete failure receipt without partial decision evidence."""
    return _completed(
        status,
        base_sha=base_sha,
        failure_code=failure_code,
        index_hash=index_hash,
    )


def unavailable(*, base_sha: str, failure_code: str) -> RepositoryDecisionContext:
    """Create an explicit no-partial-results state for optional evidence."""
    return failed("unavailable", base_sha=base_sha, failure_code=failure_code)


def restore_snapshot(
    *,
    snapshot_id: int,
    value: object,
    expected_hash: str,
) -> RepositoryDecisionContext:
    """Restore one stored aggregate and verify its content-addressed receipt."""
    if snapshot_id < 1:
        raise RepositoryDecisionContextError("decision snapshot identity is invalid")
    if not isinstance(value, Mapping):
        raise RepositoryDecisionContextError("decision snapshot payload must be an object")
    item = cast(Mapping[str, object], value)
    expected_fields = {
        "schema_version",
        "status",
        "failure_code",
        "base_sha",
        "index_path",
        "index_hash",
        "decisions",
    }
    if set(item) != expected_fields or item.get("index_path") != INDEX_PATH:
        raise RepositoryDecisionContextError("decision snapshot fields are invalid")
    if item.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise RepositoryDecisionContextError("decision snapshot schema is unsupported")
    status_value = item.get("status")
    if status_value not in {
        "not_configured",
        "loaded",
        "unavailable",
        "invalid",
        "too_many_matches",
    }:
        raise RepositoryDecisionContextError("decision snapshot status is invalid")
    status = cast(CompletedStatus, status_value)
    base_sha = item.get("base_sha")
    if not isinstance(base_sha, str) or not _SHA_RE.fullmatch(base_sha):
        raise RepositoryDecisionContextError("decision snapshot base SHA is invalid")
    failure_value = item.get("failure_code")
    if failure_value is not None and (
        not isinstance(failure_value, str)
        or re.fullmatch(r"^[a-z][a-z0-9_]{0,79}$", failure_value) is None
    ):
        raise RepositoryDecisionContextError("decision snapshot failure code is invalid")
    failure_code = failure_value
    index_value = item.get("index_hash")
    if index_value is not None and (
        not isinstance(index_value, str) or not _HASH_RE.fullmatch(index_value)
    ):
        raise RepositoryDecisionContextError("decision snapshot index hash is invalid")
    index_hash = index_value
    decision_values = item.get("decisions")
    if not isinstance(decision_values, list):
        raise RepositoryDecisionContextError("decision snapshot items must be a list")
    decisions = tuple(
        repository_decisions.restore_snapshot_value(value)
        for value in cast(list[object], decision_values)
    )
    if len(decisions) > repository_decisions.MAX_MATCHED_DECISIONS:
        raise RepositoryDecisionContextError("decision snapshot contains too many items")
    if len({decision.id for decision in decisions}) != len(decisions):
        raise RepositoryDecisionContextError("decision snapshot contains duplicate IDs")
    restored = _completed(
        status,
        base_sha=base_sha,
        failure_code=failure_code,
        index_hash=index_hash,
        decisions=decisions,
    )
    if not _HASH_RE.fullmatch(expected_hash) or restored.snapshot_hash != expected_hash:
        raise RepositoryDecisionContextError("decision snapshot hash does not match")
    return RepositoryDecisionContext(
        snapshot_id=snapshot_id,
        schema_version=restored.schema_version,
        status=restored.status,
        failure_code=restored.failure_code,
        base_sha=restored.base_sha,
        index_hash=restored.index_hash,
        snapshot_hash=restored.snapshot_hash,
        decisions=restored.decisions,
    )


def intentional_evidence(
    context: RepositoryDecisionContext,
    *,
    review_run_id: ReviewRunId,
    adr_id: str,
    finding_path: str,
) -> IntentionalDesignEvidence | None:
    """Resolve an accepted ADR from the exact source run and finding path."""
    if context.status != "loaded" or context.snapshot_id is None:
        return None
    decision = next(
        (item for item in context.decisions if item.id == adr_id),
        None,
    )
    if (
        decision is None
        or decision.status != "accepted"
        or not repository_decisions.decision_applies_to(
            decision,
            path=finding_path,
        )
    ):
        return None
    return IntentionalDesignEvidence(
        review_run_id=review_run_id,
        review_decision_snapshot_id=context.snapshot_id,
        repository_decision_id=decision.id,
        repository_decision_metadata_hash=decision.metadata_hash,
        repository_decision_path=decision.adr_path,
        repository_decision_base_sha=context.base_sha,
    )


def intentional_evidence_is_current(
    context: RepositoryDecisionContext,
    *,
    evidence: IntentionalDesignEvidence,
    finding_path: str,
) -> bool:
    """Check that recorded ADR evidence still matches the current base snapshot."""
    if context.status != "loaded" or context.snapshot_id is None:
        return False
    decision = next(
        (
            item
            for item in context.decisions
            if item.id == evidence.repository_decision_id
        ),
        None,
    )
    return bool(
        decision is not None
        and decision.status == "accepted"
        and decision.metadata_hash == evidence.repository_decision_metadata_hash
        and decision.adr_path == evidence.repository_decision_path
        and repository_decisions.decision_applies_to(
            decision,
            path=finding_path,
        )
    )


__all__ = [
    "CompletedStatus",
    "ContextStatus",
    "INDEX_PATH",
    "RepositoryDecisionContext",
    "RepositoryDecisionContextError",
    "SNAPSHOT_SCHEMA_VERSION",
    "failed",
    "intentional_evidence",
    "intentional_evidence_is_current",
    "loaded",
    "not_configured",
    "pending",
    "restore_snapshot",
    "snapshot_value",
    "unavailable",
]
