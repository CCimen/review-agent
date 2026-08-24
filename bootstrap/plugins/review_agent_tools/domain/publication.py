"""Immutable publication plans and deterministic delivery payloads."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NewType, TypeAlias, cast


JsonValue: TypeAlias = (
    None | bool | int | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = Mapping[str, JsonValue]

PublicationId = NewType("PublicationId", int)
PublicationPartId = NewType("PublicationPartId", int)

_SHA256_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCAL_REFERENCE_RE = re.compile(r"^F[1-9][0-9]*$")
PUBLICATION_MARKER_PREFIX = "review-agent:canonical publication="
PublicationRenderedBlockKind = Literal[
    "header",
    "finding",
    "suggestion_help",
    "unchecked_history",
    "closed_history",
    "fix_brief",
    "feedback_help",
    "metadata",
]
PUBLICATION_RENDERED_BLOCK_KINDS = frozenset(
    {
        "header",
        "finding",
        "suggestion_help",
        "unchecked_history",
        "closed_history",
        "fix_brief",
        "feedback_help",
        "metadata",
    }
)


class PublicationDomainError(ValueError):
    """A publication value violates the persisted delivery contract."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or message


class PublicationStatus(StrEnum):
    GENERATED = "generated"
    POSTING = "posting"
    POSTED = "posted"
    PUBLISH_FAILED = "publish_failed"
    FAILED = "failed"
    STALE = "stale"


class PublicationPartType(StrEnum):
    SUMMARY = "summary"
    CONTINUATION = "continuation"
    SUGGESTION_REVIEW = "suggestion_review"


class PublicationPartStatus(StrEnum):
    PENDING = "pending"
    POSTING = "posting"
    POSTED = "posted"
    PUBLISH_FAILED = "publish_failed"
    STALE = "stale"


class PublicationFindingOutcome(StrEnum):
    CURRENT = "current"
    RESOLVED = "resolved"
    INVALIDATED = "invalidated"
    SUPPRESSED = "suppressed"
    NOT_CHECKED = "not_checked"


class PublicationReviewSide(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


@dataclass(frozen=True, slots=True)
class CanonicalPayload:
    schema_version: int
    canonical_json: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PublicationPartInput:
    part_type: PublicationPartType
    part_number: int
    payload_schema_version: int
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class PublicationFindingInput:
    finding_id: int
    source_finding_occurrence_id: int
    source_review_run_id: int
    local_reference: str
    outcome: PublicationFindingOutcome
    outcome_evidence: str | None = None


@dataclass(frozen=True, slots=True)
class IssueCommentDelivery:
    body: str


@dataclass(frozen=True, slots=True)
class InlineSuggestionDelivery:
    path: str
    body: str
    line: int
    side: PublicationReviewSide
    start_line: int | None
    start_side: PublicationReviewSide | None


@dataclass(frozen=True, slots=True)
class SuggestionReviewDelivery:
    body: str
    comments: tuple[InlineSuggestionDelivery, ...]


PublicationDelivery: TypeAlias = IssueCommentDelivery | SuggestionReviewDelivery


@dataclass(frozen=True, slots=True)
class PublicationPartDefinition:
    part_type: PublicationPartType
    part_number: int
    payload: CanonicalPayload
    delivery: PublicationDelivery


@dataclass(frozen=True, slots=True)
class PublicationFindingDefinition:
    finding_id: int
    source_finding_occurrence_id: int
    source_review_run_id: int
    local_reference: str
    outcome: PublicationFindingOutcome
    outcome_evidence: str | None


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    publication_key: str
    rendered_markdown: str
    rendered_blocks_schema_version: int
    rendered_blocks_json: str
    rendered_hash: str
    parts: tuple[PublicationPartDefinition, ...]
    findings: tuple[PublicationFindingDefinition, ...]


def _positive(value: int, *, field: str) -> int:
    if isinstance(value, bool) or value < 1:
        raise PublicationDomainError(f"{field} must be positive")
    return value


def _normalize_json(value: object, *, field: str) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise PublicationDomainError(
                f"{field} contains invalid Unicode"
            ) from exc
        return value
    if isinstance(value, float):
        raise PublicationDomainError(
            f"{field} uses a floating-point value; use an exact integer or string"
        )
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        normalized: dict[str, JsonValue] = {}
        for key, item in raw.items():
            if not isinstance(key, str):
                raise PublicationDomainError(f"{field} object keys must be strings")
            normalized[key] = _normalize_json(item, field=field)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw_sequence = cast(Sequence[object], value)
        return [_normalize_json(item, field=field) for item in raw_sequence]
    raise PublicationDomainError(f"{field} must contain only JSON values")


def _canonical_json(value: object, *, field: str) -> str:
    normalized = _normalize_json(value, field=field)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _payload(item: PublicationPartInput) -> CanonicalPayload:
    schema_version = _positive(
        item.payload_schema_version, field="payload_schema_version"
    )
    canonical = _canonical_json(item.payload, field="publication payload")
    decoded = json.loads(canonical)
    if not isinstance(decoded, dict):
        raise PublicationDomainError("publication payload must be a JSON object")
    return CanonicalPayload(
        schema_version=schema_version,
        canonical_json=canonical,
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def publication_marker(publication_key: str) -> str:
    return f"{PUBLICATION_MARKER_PREFIX}{publication_key}"


def extract_publication_key(body: str) -> str | None:
    token_index = body.find(PUBLICATION_MARKER_PREFIX)
    if token_index < 0:
        return None
    remainder = body[token_index + len(PUBLICATION_MARKER_PREFIX) :]
    parts = remainder.split()
    if not parts:
        return None
    key = parts[0].rstrip(" -\"'>")
    return key if _SHA256_ID_RE.fullmatch(key) else None


def _payload_mapping(payload_json: str) -> Mapping[str, object]:
    try:
        decoded = cast(object, json.loads(payload_json))
    except ValueError as exc:
        raise PublicationDomainError("publication payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise PublicationDomainError("publication payload must be a JSON object")
    return cast(Mapping[str, object], decoded)


def _required_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise PublicationDomainError(f"publication payload {field} is required")
    return value


def decode_publication_delivery(
    *,
    part_type: PublicationPartType,
    part_number: int,
    payload_schema_version: int,
    payload_json: str,
    publication_key: str,
) -> PublicationDelivery:
    """Decode the stable version-one provider request shape."""
    if payload_schema_version != 1:
        raise PublicationDomainError("unsupported publication payload schema version")
    payload = _payload_mapping(payload_json)
    body = _required_text(payload, "body")
    if part_type in {
        PublicationPartType.SUMMARY,
        PublicationPartType.CONTINUATION,
    }:
        if extract_publication_key(body) != publication_key or (
            f" part={part_number}/" not in body
        ):
            raise PublicationDomainError(
                "issue-comment payload requires its exact publication-part marker"
            )
        return IssueCommentDelivery(body=body)

    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list) or not raw_comments:
        raise PublicationDomainError(
            "suggestion-review payload requires inline comments"
        )
    comments: list[InlineSuggestionDelivery] = []
    for raw in cast(list[object], raw_comments):
        if not isinstance(raw, dict):
            raise PublicationDomainError("inline suggestion payload must be an object")
        item = cast(Mapping[str, object], raw)
        path = _required_text(item, "path")
        comment_body = _required_text(item, "body")
        line = item.get("line")
        side = item.get("side")
        start_line = item.get("start_line")
        start_side = item.get("start_side")
        if extract_publication_key(comment_body) != publication_key:
            raise PublicationDomainError(
                "inline suggestion requires its exact publication marker"
            )
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise PublicationDomainError("inline suggestion line must be positive")
        try:
            resolved_side = PublicationReviewSide(str(side))
            resolved_start_side = (
                PublicationReviewSide(str(start_side))
                if start_side is not None
                else None
            )
        except ValueError as exc:
            raise PublicationDomainError("inline suggestion side is invalid") from exc
        if start_line is not None and (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or start_line < 1
        ):
            raise PublicationDomainError(
                "inline suggestion start_line must be positive"
            )
        if (start_line is None) != (resolved_start_side is None):
            raise PublicationDomainError(
                "inline suggestion start_line and start_side must appear together"
            )
        comments.append(
            InlineSuggestionDelivery(
                path=path,
                body=comment_body,
                line=line,
                side=resolved_side,
                start_line=start_line,
                start_side=resolved_start_side,
            )
        )
    return SuggestionReviewDelivery(body=body, comments=tuple(comments))


def _parts(
    inputs: Sequence[PublicationPartInput], *, publication_key: str
) -> tuple[PublicationPartDefinition, ...]:
    if not inputs:
        raise PublicationDomainError("publication requires a summary part")
    identities = [(item.part_type, item.part_number) for item in inputs]
    if len(set(identities)) != len(identities):
        raise PublicationDomainError("publication contains duplicate part identities")
    summaries = [
        item
        for item in inputs
        if item.part_type is PublicationPartType.SUMMARY
    ]
    if len(summaries) != 1 or summaries[0].part_number != 1:
        raise PublicationDomainError("publication requires summary part 1")
    continuations = sorted(
        item.part_number
        for item in inputs
        if item.part_type is PublicationPartType.CONTINUATION
    )
    if continuations != list(range(2, len(continuations) + 2)):
        raise PublicationDomainError("continuation part numbers must be contiguous")
    suggestion_parts = [
        item
        for item in inputs
        if item.part_type is PublicationPartType.SUGGESTION_REVIEW
    ]
    if len(suggestion_parts) > 1 or (
        suggestion_parts and suggestion_parts[0].part_number != 1
    ):
        raise PublicationDomainError("suggestion review must use at most part 1")
    resolved: list[PublicationPartDefinition] = []
    for item in inputs:
        part_number = _positive(item.part_number, field="part_number")
        payload = _payload(item)
        resolved.append(
            PublicationPartDefinition(
                part_type=item.part_type,
                part_number=part_number,
                payload=payload,
                delivery=decode_publication_delivery(
                    part_type=item.part_type,
                    part_number=part_number,
                    payload_schema_version=payload.schema_version,
                    payload_json=payload.canonical_json,
                    publication_key=publication_key,
                ),
            )
        )
    order = {
        PublicationPartType.SUMMARY: 0,
        PublicationPartType.CONTINUATION: 1,
        PublicationPartType.SUGGESTION_REVIEW: 2,
    }
    return tuple(
        sorted(resolved, key=lambda item: (order[item.part_type], item.part_number))
    )


def _evidence(item: PublicationFindingInput) -> str | None:
    if item.outcome is PublicationFindingOutcome.CURRENT:
        if item.outcome_evidence is not None:
            raise PublicationDomainError(
                "current finding must not have outcome evidence"
            )
        return None
    evidence = " ".join((item.outcome_evidence or "").strip().split())
    if not evidence:
        raise PublicationDomainError("closed finding requires outcome evidence")
    if "\x00" in evidence:
        raise PublicationDomainError("outcome evidence must not contain NUL")
    return evidence


def _findings(
    inputs: Sequence[PublicationFindingInput],
) -> tuple[PublicationFindingDefinition, ...]:
    finding_ids = [item.finding_id for item in inputs]
    references = [item.local_reference for item in inputs]
    if len(set(finding_ids)) != len(finding_ids):
        raise PublicationDomainError("publication contains duplicate findings")
    if len(set(references)) != len(references):
        raise PublicationDomainError("publication contains duplicate local references")
    resolved: list[PublicationFindingDefinition] = []
    for item in inputs:
        if not _LOCAL_REFERENCE_RE.fullmatch(item.local_reference):
            raise PublicationDomainError("local_reference must use the F<number> form")
        resolved.append(
            PublicationFindingDefinition(
                finding_id=_positive(item.finding_id, field="finding_id"),
                source_finding_occurrence_id=_positive(
                    item.source_finding_occurrence_id,
                    field="source_finding_occurrence_id",
                ),
                source_review_run_id=_positive(
                    item.source_review_run_id, field="source_review_run_id"
                ),
                local_reference=item.local_reference,
                outcome=item.outcome,
                outcome_evidence=_evidence(item),
            )
        )
    return tuple(sorted(resolved, key=lambda item: item.local_reference))


def resolve_rendered_blocks(
    values: object,
    *,
    schema_version: int,
    rendered_markdown: str,
) -> str:
    if schema_version != 1:
        raise PublicationDomainError("rendered_blocks schema version is unsupported")
    blocks_json = _canonical_json(values, field="rendered_blocks")
    decoded: object = json.loads(blocks_json)
    if not isinstance(decoded, list) or not decoded:
        raise PublicationDomainError("rendered_blocks must be a non-empty JSON array")
    markdown_parts: list[str] = []
    for index, value in enumerate(cast(list[object], decoded)):
        if not isinstance(value, dict):
            raise PublicationDomainError(
                f"rendered_blocks[{index}] must be an object"
            )
        item = cast(dict[object, object], value)
        if set(item) != {"kind", "markdown"}:
            raise PublicationDomainError(
                f"rendered_blocks[{index}] must contain kind and markdown"
            )
        kind = item["kind"]
        markdown = item["markdown"]
        if kind not in PUBLICATION_RENDERED_BLOCK_KINDS:
            raise PublicationDomainError(
                f"rendered_blocks[{index}].kind is unsupported"
            )
        if not isinstance(markdown, str) or not markdown.strip():
            raise PublicationDomainError(
                f"rendered_blocks[{index}].markdown must be text"
            )
        markdown_parts.append(markdown.rstrip())
    reconstructed = "\n\n".join(markdown_parts).rstrip() + "\n"
    if reconstructed != rendered_markdown:
        raise PublicationDomainError(
            "rendered_blocks do not reconstruct rendered_markdown"
        )
    return blocks_json


def resolve_publication_plan(
    *,
    publication_key: str,
    rendered_markdown: str,
    rendered_blocks_schema_version: int,
    rendered_blocks: Sequence[object],
    parts: Sequence[PublicationPartInput],
    findings: Sequence[PublicationFindingInput],
) -> PublicationPlan:
    """Validate and freeze an exact publication before pool checkout."""
    if not _SHA256_ID_RE.fullmatch(publication_key):
        raise PublicationDomainError(
            "publication_key must be a sha256:<64 hex> identifier"
        )
    if not rendered_markdown:
        raise PublicationDomainError("rendered_markdown is required")
    try:
        rendered_markdown.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PublicationDomainError(
            "rendered_markdown contains invalid Unicode"
        ) from exc
    blocks_schema_version = _positive(
        rendered_blocks_schema_version,
        field="rendered_blocks_schema_version",
    )
    blocks_json = resolve_rendered_blocks(
        rendered_blocks,
        schema_version=blocks_schema_version,
        rendered_markdown=rendered_markdown,
    )
    return PublicationPlan(
        publication_key=publication_key,
        rendered_markdown=rendered_markdown,
        rendered_blocks_schema_version=blocks_schema_version,
        rendered_blocks_json=blocks_json,
        rendered_hash=hashlib.sha256(
            rendered_markdown.encode("utf-8")
        ).hexdigest(),
        parts=_parts(parts, publication_key=publication_key),
        findings=_findings(findings),
    )
