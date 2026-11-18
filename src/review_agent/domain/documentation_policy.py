"""Closed repository-owned documentation relationship contract."""

from __future__ import annotations

from dataclasses import dataclass
import tomllib
from typing import cast

from . import repository_paths


CONFIG_PATH = ".review-agent/documentation.toml"
MAX_CONFIG_BYTES = 64 * 1024
MAX_AREAS = 200
MAX_EXCLUSIONS = 200
MAX_PATTERNS_PER_ENTRY = 20
MAX_TOTAL_PATTERNS = 1_000
MAX_DOCUMENTS_PER_AREA = 20
MAX_INTENT_CHARS = 1_000
MAX_REASON_CHARS = 500
MAX_AREA_ID_CHARS = 64


class DocumentationPolicyError(ValueError):
    """Documentation policy does not match the versioned data contract."""


@dataclass(frozen=True, slots=True)
class DocumentationArea:
    id: str
    sources: tuple[str, ...]
    documents: tuple[str, ...]
    intent: str


@dataclass(frozen=True, slots=True)
class DocumentationExclusion:
    paths: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class DocumentationPolicy:
    areas: tuple[DocumentationArea, ...]
    ignore_changes: tuple[DocumentationExclusion, ...]
    ignore_documents: tuple[DocumentationExclusion, ...]


def _text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DocumentationPolicyError(f"{field} must be text")
    result = value.strip()
    if not result or "\x00" in result or len(result) > maximum:
        raise DocumentationPolicyError(
            f"{field} must contain 1 to {maximum} characters"
        )
    return result


def _list(value: object, *, field: str, maximum: int) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise DocumentationPolicyError(
            f"{field} must contain 1 to {maximum} items"
        )
    items = cast(list[object], value)
    if not items or len(items) > maximum:
        raise DocumentationPolicyError(
            f"{field} must contain 1 to {maximum} items"
        )
    return tuple(items)


def _paths(
    value: object, *, field: str, maximum: int, glob: bool
) -> tuple[str, ...]:
    items = _list(value, field=field, maximum=maximum)
    try:
        paths = tuple(
            repository_paths.simple_glob(item, field=f"{field} item")
            if glob
            else repository_paths.normalized_path(item, field=f"{field} item")
            for item in items
        )
    except repository_paths.RepositoryPathError as exc:
        raise DocumentationPolicyError(str(exc)) from exc
    if len(set(paths)) != len(paths):
        raise DocumentationPolicyError(f"{field} must not contain duplicates")
    return paths


def _exclusions(value: object, *, field: str) -> tuple[DocumentationExclusion, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DocumentationPolicyError(
            f"{field} must contain at most {MAX_EXCLUSIONS} tables"
        )
    values = cast(list[object], value)
    if len(values) > MAX_EXCLUSIONS:
        raise DocumentationPolicyError(
            f"{field} must contain at most {MAX_EXCLUSIONS} tables"
        )
    exclusions: list[DocumentationExclusion] = []
    for position, item in enumerate(values, start=1):
        if not isinstance(item, dict):
            raise DocumentationPolicyError(
                f"{field} entry {position} fields must be exactly paths and reason"
            )
        raw = cast(dict[str, object], item)
        if set(raw) != {"paths", "reason"}:
            raise DocumentationPolicyError(
                f"{field} entry {position} fields must be exactly paths and reason"
            )
        exclusions.append(
            DocumentationExclusion(
                paths=_paths(
                    raw.get("paths"),
                    field=f"{field} entry {position} paths",
                    maximum=MAX_PATTERNS_PER_ENTRY,
                    glob=True,
                ),
                reason=_text(
                    raw.get("reason"),
                    field=f"{field} entry {position} reason",
                    maximum=MAX_REASON_CHARS,
                ),
            )
        )
    return tuple(exclusions)


def parse_policy(content: str) -> DocumentationPolicy:
    """Parse version 1 documentation.toml independently of guidance and ADRs."""
    if len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise DocumentationPolicyError("documentation policy may contain at most 64 KiB")
    try:
        value = cast(dict[str, object], tomllib.loads(content))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise DocumentationPolicyError("documentation policy is not valid TOML") from exc
    if not set(value).issubset(
        {"version", "area", "ignore_changes", "ignore_documents"}
    ) or "version" not in value or "area" not in value:
        raise DocumentationPolicyError(
            "documentation policy fields must be version, area, ignore_changes, and ignore_documents"
        )
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise DocumentationPolicyError("documentation policy version must be 1")
    raw_areas = value.get("area")
    if not isinstance(raw_areas, list):
        raise DocumentationPolicyError(f"area must contain 1 to {MAX_AREAS} tables")
    area_values = cast(list[object], raw_areas)
    if not area_values or len(area_values) > MAX_AREAS:
        raise DocumentationPolicyError(f"area must contain 1 to {MAX_AREAS} tables")
    areas: list[DocumentationArea] = []
    for position, item in enumerate(area_values, start=1):
        if not isinstance(item, dict):
            raise DocumentationPolicyError(
                f"area {position} fields must be exactly id, sources, documents, and intent"
            )
        raw = cast(dict[str, object], item)
        if set(raw) != {"id", "sources", "documents", "intent"}:
            raise DocumentationPolicyError(
                f"area {position} fields must be exactly id, sources, documents, and intent"
            )
        areas.append(
            DocumentationArea(
                id=_text(
                    raw.get("id"), field=f"area {position} id", maximum=MAX_AREA_ID_CHARS
                ),
                sources=_paths(
                    raw.get("sources"), field=f"area {position} sources",
                    maximum=MAX_PATTERNS_PER_ENTRY, glob=True,
                ),
                documents=_paths(
                    raw.get("documents"), field=f"area {position} documents",
                    maximum=MAX_DOCUMENTS_PER_AREA, glob=False,
                ),
                intent=_text(
                    raw.get("intent"), field=f"area {position} intent",
                    maximum=MAX_INTENT_CHARS,
                ),
            )
        )
    ids = [area.id for area in areas]
    if len(set(ids)) != len(ids):
        raise DocumentationPolicyError("area IDs must be unique")
    ignore_changes = _exclusions(value.get("ignore_changes"), field="ignore_changes")
    ignore_documents = _exclusions(
        value.get("ignore_documents"), field="ignore_documents"
    )
    pattern_count = sum(len(area.sources) for area in areas) + sum(
        len(item.paths) for item in (*ignore_changes, *ignore_documents)
    )
    if pattern_count > MAX_TOTAL_PATTERNS:
        raise DocumentationPolicyError(
            f"documentation policy may contain at most {MAX_TOTAL_PATTERNS} path patterns"
        )
    for area in areas:
        for document in area.documents:
            if any(
                repository_paths.matches(pattern, document)
                for exclusion in ignore_documents
                for pattern in exclusion.paths
            ):
                raise DocumentationPolicyError(
                    f"area {area.id} document {document} is also ignored"
                )
    return DocumentationPolicy(tuple(areas), ignore_changes, ignore_documents)


__all__ = [
    "CONFIG_PATH", "MAX_CONFIG_BYTES", "DocumentationArea",
    "DocumentationExclusion", "DocumentationPolicy", "DocumentationPolicyError",
    "parse_policy",
]
