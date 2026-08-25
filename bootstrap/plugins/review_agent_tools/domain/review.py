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


class FailureStatusDelivery(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    POSTING = "posting"
    PUBLISH_FAILED = "publish_failed"
    POSTED = "posted"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


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


class CoverageState(StrEnum):
    UNKNOWN = "unknown"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


class DiffState(StrEnum):
    UNSEEN = "unseen"
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    UNAVAILABLE = "unavailable"


class FileSide(StrEnum):
    BASE = "base"
    HEAD = "head"


class FileDomain(StrEnum):
    BACKEND = "backend"
    FRONTEND = "frontend"
    INFRASTRUCTURE = "infrastructure"
    GENERAL = "general"


class ReviewMode(StrEnum):
    NORMAL = "normal"
    MIGRATION = "migration"
    CONFIGURATION = "configuration"
    GENERATED_CONTRACT = "generated-contract"

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


@dataclass(frozen=True, slots=True)
class ChangedFileDefinition:
    path: str
    change_status: str
    previous_path: str | None
    domain: FileDomain
    review_mode: ReviewMode


@dataclass(frozen=True, slots=True)
class FileReadDefinition:
    path: str
    side: FileSide
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class DiffObservation:
    paths: tuple[str, ...]
    state: DiffState
    unavailable_reason: str


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


def resolve_review_path(value: str) -> str:
    """Validate one repository-relative path without changing its identity."""
    if not value or len(value) > 500:
        raise ReviewDomainError("path must contain at most 500 characters")
    if "\x00" in value:
        raise ReviewDomainError("path must not contain NUL")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise ReviewDomainError("path must be repository-relative and normalized")
    if "\\" in value or any(part in {".", ".."} for part in value.split("/")):
        raise ReviewDomainError("path must be repository-relative and normalized")
    return value


def classify_file_domain(path: str) -> FileDomain:
    resolved = resolve_review_path(path)
    if resolved.startswith("backend/"):
        return FileDomain.BACKEND
    if resolved.startswith("frontend/"):
        return FileDomain.FRONTEND
    if resolved.startswith(".github/") or resolved in {"compose.yaml", "Dockerfile"}:
        return FileDomain.INFRASTRUCTURE
    return FileDomain.GENERAL


def classify_review_mode(path: str, change_status: str) -> ReviewMode:
    resolved = resolve_review_path(path)
    if change_status == "removed":
        return ReviewMode.NORMAL
    if "alembic" in resolved or "migration" in resolved.lower():
        return ReviewMode.MIGRATION
    if resolved.endswith((".yaml", ".yml", ".toml", ".json")) or resolved.startswith(
        ".github/"
    ):
        return ReviewMode.CONFIGURATION
    if "generated" in resolved or resolved.endswith(".d.ts"):
        return ReviewMode.GENERATED_CONTRACT
    return ReviewMode.NORMAL


def _bounded_label(
    value: str,
    *,
    field: str,
    limit: int,
) -> str:
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ReviewDomainError(f"{field} is required")
    if "\x00" in normalized or len(normalized) > limit:
        raise ReviewDomainError(f"{field} exceeds {limit} characters")
    return normalized


def resolve_changed_file(
    *,
    path: str,
    change_status: str,
    previous_path: str | None = None,
    domain: FileDomain = FileDomain.GENERAL,
    review_mode: ReviewMode = ReviewMode.NORMAL,
) -> ChangedFileDefinition:
    """Validate changed-file metadata before a transaction is opened."""
    normalized_status = _bounded_label(
        change_status, field="change_status", limit=40
    )
    return ChangedFileDefinition(
        path=resolve_review_path(path),
        change_status=normalized_status,
        previous_path=(
            resolve_review_path(previous_path) if previous_path else None
        ),
        domain=domain,
        review_mode=review_mode,
    )


def resolve_file_read(
    *,
    path: str,
    side: FileSide,
    start_line: int,
    end_line: int,
) -> FileReadDefinition:
    """Validate one inclusive source-read range before pool checkout."""
    if (
        isinstance(start_line, bool)
        or isinstance(end_line, bool)
        or start_line < 1
        or end_line < start_line
    ):
        raise ReviewDomainError("line range must be positive and inclusive")
    return FileReadDefinition(
        path=resolve_review_path(path),
        side=side,
        start_line=start_line,
        end_line=end_line,
    )


def resolve_diff_reason(state: DiffState, unavailable_reason: str) -> str:
    """Validate the reason attached to one observed diff state."""
    reason = " ".join(unavailable_reason.strip().split())
    if state is DiffState.UNAVAILABLE:
        if not reason or "\x00" in reason or len(reason) > 80:
            raise ReviewDomainError(
                "unavailable diff observation requires a bounded reason"
            )
    elif reason:
        raise ReviewDomainError(
            "unavailable_reason requires unavailable diff state"
        )
    return reason


def resolve_diff_observation(
    *,
    paths: Sequence[str],
    state: DiffState,
    unavailable_reason: str = "",
) -> DiffObservation:
    """Validate one batch of diff evidence before pool checkout."""
    if state is DiffState.UNSEEN:
        raise ReviewDomainError("an observed diff state cannot be unseen")
    resolved_paths = tuple(resolve_review_path(path) for path in paths)
    if len(set(resolved_paths)) != len(resolved_paths):
        raise ReviewDomainError("diff observation contains duplicate paths")
    return DiffObservation(
        paths=resolved_paths,
        state=state,
        unavailable_reason=resolve_diff_reason(state, unavailable_reason),
    )


def resolve_changed_file_count(value: int) -> int:
    """Validate the provider-reported changed-file count."""
    if isinstance(value, bool) or value < 0:
        raise ReviewDomainError("changed_files_reported must be zero or greater")
    return value


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
