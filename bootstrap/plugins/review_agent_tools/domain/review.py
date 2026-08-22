"""Immutable review-subject values and canonicalization."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType, TypeAlias, cast


JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = Mapping[str, JsonValue]

RepositoryId = NewType("RepositoryId", int)
PullRequestId = NewType("PullRequestId", int)
ReviewSubjectId = NewType("ReviewSubjectId", int)
ReviewRunId = NewType("ReviewRunId", int)


class ReviewStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ReviewPhase(StrEnum):
    ACCEPTED = "accepted"
    FETCHING_PR = "fetching_pr"
    COLLECTING_DIFF = "collecting_diff"
    REVIEWING = "reviewing"
    RENDERING = "rendering"
    PUBLISHING = "publishing"
    POSTED = "posted"
    FAILED = "failed"
    SUPERSEDED = "superseded"

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class ReviewDomainError(ValueError):
    """A review value violates the persisted domain contract."""


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    schema_version: int
    canonical_json: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ReviewSubjectDefinition:
    base_sha: str
    head_sha: str
    policy_revision: str
    resolved_config: ResolvedConfig


def _normalize_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReviewDomainError("resolved_config must contain finite JSON values")
        return value
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        normalized: dict[str, JsonValue] = {}
        for key, item in raw.items():
            if not isinstance(key, str):
                raise ReviewDomainError("resolved_config object keys must be strings")
            normalized[key] = _normalize_json(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_sequence = cast(Sequence[object], value)
        return [_normalize_json(item) for item in raw_sequence]
    raise ReviewDomainError("resolved_config must contain only JSON values")


def _commit_sha(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _SHA_RE.fullmatch(normalized):
        raise ReviewDomainError(
            f"{field} must be a 40 to 64 character hexadecimal commit SHA"
        )
    return normalized


def _policy_revision(value: str) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ReviewDomainError("policy_revision is required")
    if len(normalized) > 120:
        raise ReviewDomainError("policy_revision exceeds 120 characters")
    return normalized


def _resolved_config(value: object, *, schema_version: int) -> ResolvedConfig:
    if isinstance(schema_version, bool) or schema_version < 1:
        raise ReviewDomainError("resolved_config_schema_version must be positive")
    normalized = _normalize_json(value)
    if not isinstance(normalized, dict):
        raise ReviewDomainError("resolved_config must be a JSON object")
    canonical_json = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ResolvedConfig(
        schema_version=schema_version,
        canonical_json=canonical_json,
        sha256=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )


def resolve_review_subject(
    *,
    base_sha: str,
    head_sha: str,
    policy_revision: str,
    resolved_config_schema_version: int,
    resolved_config: JsonObject,
) -> ReviewSubjectDefinition:
    """Validate and freeze the exact subject before a transaction is opened."""
    return ReviewSubjectDefinition(
        base_sha=_commit_sha(base_sha, field="base_sha"),
        head_sha=_commit_sha(head_sha, field="head_sha"),
        policy_revision=_policy_revision(policy_revision),
        resolved_config=_resolved_config(
            resolved_config,
            schema_version=resolved_config_schema_version,
        ),
    )


def decode_resolved_config(value: str, *, schema_version: int) -> ResolvedConfig:
    """Decode a stored JSON object through the same canonical hash contract."""
    try:
        raw = cast(object, json.loads(value))
    except (TypeError, ValueError) as exc:
        raise ReviewDomainError("stored resolved_config is not valid JSON") from exc
    return _resolved_config(raw, schema_version=schema_version)
