"""Bounded documentation evidence and assessment results."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import json
import re
from typing import cast

from ..documentation_scope import (
    ChangedPath,
    DocumentationScope,
    ScopeExclusion,
    SelectedArea,
)
from .repository_paths import normalized_path
from .review import ReviewRunId


MAX_EVIDENCE_READS = 512
MAX_RESULT_JSON_BYTES = 512 * 1024
MAX_INCOMPLETE_REASONS = 512
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class DocumentationReviewError(ValueError):
    """A documentation receipt violates its provenance or lifecycle contract."""


class EvidenceRole(StrEnum):
    POLICY = "policy"
    COMPARISON = "comparison"
    HEAD = "head"


class DocumentationOutcome(StrEnum):
    NOT_NEEDED = "not_needed"
    NO_MISMATCH_FOUND = "no_mismatch_found"
    FINDINGS = "findings"
    INCOMPLETE = "incomplete"
    NOT_CONFIGURED = "not_configured"
    INVALID_CONFIGURATION = "invalid_configuration"
    UNAVAILABLE = "unavailable"


def validate_revision(value: str, *, field: str) -> str:
    if not _SHA.fullmatch(value):
        raise DocumentationReviewError(f"{field} must be an exact Git object identity")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceRead:
    path: str
    role: EvidenceRole
    revision: str
    blob_sha: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    content_sha256: str | None = None
    total_lines: int | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if normalized_path(self.path, field="evidence path") != self.path:
            raise DocumentationReviewError("evidence path must be exact")
        if type(self.role) is not EvidenceRole:
            raise DocumentationReviewError("evidence role must be typed")
        validate_revision(self.revision, field="evidence revision")
        if self.blob_sha is not None:
            validate_revision(self.blob_sha, field="evidence blob")
        if self.content_sha256 is not None and not _DIGEST.fullmatch(
            self.content_sha256
        ):
            raise DocumentationReviewError("evidence content digest must be SHA-256")
        if (self.start_line is None) != (self.end_line is None):
            raise DocumentationReviewError("evidence range requires both line bounds")
        if self.start_line is not None and self.end_line is not None:
            if (
                type(self.start_line) is not int
                or type(self.end_line) is not int
                or self.start_line < 1
                or self.end_line < self.start_line
            ):
                raise DocumentationReviewError(
                    "evidence range must be ordered positive lines"
                )
        if self.total_lines is not None:
            if (
                type(self.total_lines) is not int
                or not 0 <= self.total_lines <= 2 * 1024 * 1024
            ):
                raise DocumentationReviewError("evidence total line count is invalid")
            if self.end_line is not None and self.end_line > self.total_lines:
                raise DocumentationReviewError(
                    "evidence range exceeds the file line count"
                )
        if self.unavailable_reason is not None:
            if (
                not self.unavailable_reason.strip()
                or len(self.unavailable_reason) > 500
            ):
                raise DocumentationReviewError(
                    "unavailable reason must contain 1 to 500 characters"
                )
        elif (
            self.content_sha256 is None
            or self.blob_sha is None
            or self.total_lines is None
            or (self.total_lines > 0 and self.start_line is None)
            or (self.total_lines == 0 and self.start_line is not None)
        ):
            raise DocumentationReviewError(
                "successful evidence requires blob, content digest and exact line coverage"
            )

    def to_json_obj(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    role: EvidenceRole
    path: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True, slots=True)
class PathAssessment:
    path: str
    disposition: str
    rationale: str
    citations: tuple[EvidenceCitation, ...]


@dataclass(frozen=True, slots=True)
class FindingEvidence:
    fingerprint: str
    definition_sha256: str
    document_path: str
    area_id: str
    changed_path: str
    introduced_or_worsened: str
    citations: tuple[EvidenceCitation, ...]
    adr_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviousDocumentationFinding:
    local_reference: str
    fingerprint: str
    occurrence_id: int
    source_run_id: int
    document_path: str
    title: str
    evidence: str
    smallest_fix: str
    rule_id: str
    path: str
    symbol: str | None
    anchor: str


@dataclass(frozen=True, slots=True)
class PreviousDocumentationAssessment:
    local_reference: str
    fingerprint: str
    occurrence_id: int
    verdict: str
    rationale: str
    citations: tuple[EvidenceCitation, ...]


@dataclass(frozen=True, slots=True)
class DocumentationAssessment:
    findings: tuple[FindingEvidence, ...]
    paths: tuple[PathAssessment, ...]
    previous: tuple[PreviousDocumentationAssessment, ...] = ()

    def to_json_obj(self) -> dict[str, object]:
        return asdict(self)


def decode_citation(value: object) -> EvidenceCitation:
    item = _object(value, {"role", "path", "start_line", "end_line"})
    role, path = item["role"], item["path"]
    if not isinstance(role, str) or not isinstance(path, str):
        raise DocumentationReviewError("citation role and path must be text")
    try:
        typed_role = EvidenceRole(role)
    except ValueError as exc:
        raise DocumentationReviewError("citation role is invalid") from exc
    if normalized_path(path, field="citation path") != path:
        raise DocumentationReviewError("citation path must be exact")
    start, end = item["start_line"], item["end_line"]
    if (start is None) != (end is None) or (
        start is not None
        and (type(start) is not int or type(end) is not int or start < 1 or end < start)
    ):
        raise DocumentationReviewError(
            "citation requires a positive ordered range or verified absence"
        )
    return EvidenceCitation(typed_role, path, start, cast(int | None, end))


def decode_citations(value: object) -> tuple[EvidenceCitation, ...]:
    if not isinstance(value, list) or not 1 <= len(cast(list[object], value)) <= 32:
        raise DocumentationReviewError(
            "citations require 1 to 32 exact evidence references"
        )
    result = tuple(decode_citation(item) for item in cast(list[object], value))
    if len(set(result)) != len(result):
        raise DocumentationReviewError("citations must not repeat")
    return result


def decode_path_assessment(value: object) -> PathAssessment:
    item = _object(value, {"path", "disposition", "rationale", "citations"})
    path, disposition, rationale = item["path"], item["disposition"], item["rationale"]
    if (
        not isinstance(path, str)
        or normalized_path(path, field="assessment path") != path
    ):
        raise DocumentationReviewError("assessment path must be exact")
    if not isinstance(disposition, str) or disposition not in {
        "aligned",
        "no_impact",
        "finding",
    }:
        raise DocumentationReviewError("assessment disposition is invalid")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 900:
        raise DocumentationReviewError(
            "assessment rationale requires 1 to 900 characters"
        )
    return PathAssessment(
        path, disposition, rationale, decode_citations(item["citations"])
    )


def decode_assessment(value: object) -> DocumentationAssessment:
    root = _object(value, {"findings", "paths", "previous"})
    raw_findings, raw_paths = root["findings"], root["paths"]
    if (
        not isinstance(raw_findings, list)
        or len(cast(list[object], raw_findings)) > 200
        or not isinstance(raw_paths, list)
        or len(cast(list[object], raw_paths)) > 512
    ):
        raise DocumentationReviewError(
            "documentation assessment exceeds its item bound"
        )
    findings: list[FindingEvidence] = []
    for value in cast(list[object], raw_findings):
        item = _object(
            value,
            {
                "fingerprint",
                "definition_sha256",
                "document_path",
                "area_id",
                "changed_path",
                "introduced_or_worsened",
                "citations",
                "adr_ids",
            },
        )
        for key in ("fingerprint", "definition_sha256"):
            if not isinstance(item[key], str) or not _DIGEST.fullmatch(
                cast(str, item[key])
            ):
                raise DocumentationReviewError("finding provenance digest is invalid")
        for key in (
            "document_path",
            "area_id",
            "changed_path",
            "introduced_or_worsened",
        ):
            if not isinstance(item[key], str):
                raise DocumentationReviewError("finding provenance text is invalid")
        adr_ids = item["adr_ids"]
        if (
            not isinstance(adr_ids, list)
            or len(cast(list[object], adr_ids)) > 32
            or any(not isinstance(adr, str) for adr in cast(list[object], adr_ids))
        ):
            raise DocumentationReviewError("finding ADR references are invalid")
        findings.append(
            FindingEvidence(
                cast(str, item["fingerprint"]),
                cast(str, item["definition_sha256"]),
                cast(str, item["document_path"]),
                cast(str, item["area_id"]),
                cast(str, item["changed_path"]),
                cast(str, item["introduced_or_worsened"]),
                decode_citations(item["citations"]),
                tuple(cast(list[str], adr_ids)),
            )
        )
    previous: list[PreviousDocumentationAssessment] = []
    raw_previous = root["previous"]
    if not isinstance(raw_previous, list) or len(cast(list[object], raw_previous)) > 200:
        raise DocumentationReviewError("previous assessment exceeds its item bound")
    for value in cast(list[object], raw_previous):
        item = _object(value, {"local_reference", "fingerprint", "occurrence_id", "verdict", "rationale", "citations"})
        if (not isinstance(item["local_reference"], str)
            or not isinstance(item["fingerprint"], str)
            or not _DIGEST.fullmatch(item["fingerprint"])
            or type(item["occurrence_id"]) is not int or item["occurrence_id"] < 1
            or item["verdict"] not in ("resolved", "not_checked")
            or not isinstance(item["rationale"], str) or not 1 <= len(item["rationale"]) <= 900):
            raise DocumentationReviewError("previous assessment provenance is invalid")
        citations = item["citations"]
        previous.append(PreviousDocumentationAssessment(
            item["local_reference"], item["fingerprint"], item["occurrence_id"],
            cast(str, item["verdict"]), item["rationale"],
            () if citations == [] and item["verdict"] == "not_checked" else decode_citations(citations)))
    return DocumentationAssessment(
        tuple(findings),
        tuple(decode_path_assessment(item) for item in cast(list[object], raw_paths)),
        tuple(previous),
    )


@dataclass(frozen=True, slots=True)
class DocumentationResult:
    run_id: ReviewRunId
    base_sha: str
    comparison_sha: str | None
    head_sha: str
    scope: DocumentationScope | None
    evidence: tuple[EvidenceRead, ...]
    outcome: DocumentationOutcome | None
    semantic_inference_used: bool
    incomplete_reasons: tuple[str, ...]
    coverage_complete: bool
    frozen: bool
    assessment: DocumentationAssessment | None = None


def bounded_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_RESULT_JSON_BYTES:
        raise DocumentationReviewError("documentation receipt byte limit exceeded")
    return encoded


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DocumentationReviewError("documentation receipt must contain an object")
    result = cast(dict[str, object], value)
    if set(result) != keys:
        raise DocumentationReviewError(
            "documentation receipt fields do not match its version"
        )
    return result


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise DocumentationReviewError("documentation receipt text is invalid")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise DocumentationReviewError("documentation receipt boolean is invalid")
    return value


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise DocumentationReviewError("documentation receipt list is invalid")
    return cast(list[object], value)


def _texts(value: object) -> tuple[str, ...]:
    return tuple(_text(item) for item in _items(value))


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise DocumentationReviewError("documentation evidence line is invalid")
    return value


def decode_evidence(value: object) -> tuple[EvidenceRead, ...]:
    bounded_json(value)
    items = _items(value)
    if len(items) > MAX_EVIDENCE_READS:
        raise DocumentationReviewError("documentation evidence count limit exceeded")
    result: list[EvidenceRead] = []
    for item in items:
        row = _object(
            item,
            {
                "path",
                "role",
                "revision",
                "blob_sha",
                "start_line",
                "end_line",
                "content_sha256",
                "total_lines",
                "unavailable_reason",
            },
        )
        result.append(
            EvidenceRead(
                path=_text(row["path"]),
                role=EvidenceRole(_text(row["role"])),
                revision=_text(row["revision"]),
                blob_sha=_optional_text(row["blob_sha"]),
                start_line=_optional_integer(row["start_line"]),
                end_line=_optional_integer(row["end_line"]),
                content_sha256=_optional_text(row["content_sha256"]),
                total_lines=_optional_integer(row["total_lines"]),
                unavailable_reason=_optional_text(row["unavailable_reason"]),
            )
        )
    if len(set(result)) != len(result):
        raise DocumentationReviewError("documentation evidence contains duplicates")
    return tuple(result)


def decode_scope(value: object) -> DocumentationScope:
    bounded_json(value)
    row = _object(
        value,
        {
            "schema_version",
            "base_sha",
            "comparison_sha",
            "head_sha",
            "status",
            "active_policy",
            "policy_hash",
            "proposal_status",
            "proposal_detail",
            "changed_files",
            "areas",
            "documents",
            "exclusions",
            "unmapped_paths",
            "incomplete_reasons",
            "semantic_inference_used",
        },
    )
    if type(row["schema_version"]) is not int or row["schema_version"] != 1:
        raise DocumentationReviewError("unsupported documentation scope version")
    changed: list[ChangedPath] = []
    for item in _items(row["changed_files"]):
        entry = _object(item, {"status", "path", "previous_path"})
        changed.append(
            ChangedPath(
                _text(entry["status"]),
                _text(entry["path"]),
                _optional_text(entry["previous_path"]),
            )
        )
    areas: list[SelectedArea] = []
    for item in _items(row["areas"]):
        entry = _object(item, {"id", "intent", "matched_paths", "documents"})
        areas.append(
            SelectedArea(
                _text(entry["id"]),
                _text(entry["intent"]),
                _texts(entry["matched_paths"]),
                _texts(entry["documents"]),
            )
        )
    exclusions: list[ScopeExclusion] = []
    for item in _items(row["exclusions"]):
        entry = _object(item, {"path", "kind", "reason"})
        exclusions.append(
            ScopeExclusion(
                _text(entry["path"]), _text(entry["kind"]), _text(entry["reason"])
            )
        )
    scope = DocumentationScope(
        base_sha=validate_revision(_text(row["base_sha"]), field="scope base"),
        comparison_sha=_optional_text(row["comparison_sha"]),
        head_sha=validate_revision(_text(row["head_sha"]), field="scope head"),
        status=_text(row["status"]),
        active_policy=_boolean(row["active_policy"]),
        policy_hash=_optional_text(row["policy_hash"]),
        proposal_status=_text(row["proposal_status"]),
        proposal_detail=_optional_text(row["proposal_detail"]),
        changed_files=tuple(changed),
        areas=tuple(areas),
        documents=_texts(row["documents"]),
        exclusions=tuple(exclusions),
        unmapped_paths=_texts(row["unmapped_paths"]),
        incomplete_reasons=validate_incomplete_reasons(
            _texts(row["incomplete_reasons"])
        ),
        semantic_inference_used=_boolean(row["semantic_inference_used"]),
    )
    if scope.comparison_sha is not None:
        validate_revision(scope.comparison_sha, field="scope comparison")
    if scope.status not in {
        "scoped",
        "not_configured",
        "invalid_configuration",
        "unavailable",
        "incomplete",
    }:
        raise DocumentationReviewError("documentation scope status is invalid")
    if scope.proposal_status not in {
        "unchanged",
        "valid",
        "invalid",
        "removed",
        "unavailable",
    }:
        raise DocumentationReviewError("documentation proposal status is invalid")
    if scope.policy_hash is not None and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", scope.policy_hash
    ):
        raise DocumentationReviewError("documentation policy digest must be SHA-256")
    if scope.semantic_inference_used:
        raise DocumentationReviewError(
            "deterministic scope cannot claim semantic inference"
        )
    if len(scope.changed_files) > 3000:
        raise DocumentationReviewError("documentation changed-file limit exceeded")
    paths = [path for item in scope.changed_files for path in item.paths()]
    paths.extend(scope.documents)
    paths.extend(scope.unmapped_paths)
    paths.extend(item.path for item in scope.exclusions)
    for area in scope.areas:
        paths.extend(area.matched_paths)
        paths.extend(area.documents)
    for path in paths:
        if normalized_path(path, field="scope path") != path:
            raise DocumentationReviewError("documentation scope path must be exact")
    return scope


def validate_incomplete_reasons(value: tuple[str, ...]) -> tuple[str, ...]:
    if len(value) > MAX_INCOMPLETE_REASONS or any(
        type(item) is not str or not item.strip() or len(item) > 500 for item in value
    ):
        raise DocumentationReviewError(
            "documentation incomplete reasons exceed their bounds"
        )
    return tuple(dict.fromkeys(value))
