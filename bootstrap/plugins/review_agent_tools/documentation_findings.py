"""Validate documentation assessments against immutable server-recorded evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields
import hashlib
import json
from typing import cast

from .domain.documentation_review import (
    DocumentationAssessment,
    DocumentationOutcome,
    DocumentationResult,
    DocumentationReviewError,
    EvidenceCitation,
    EvidenceRead,
    EvidenceRole,
    FindingEvidence,
    PreviousDocumentationFinding,
    PreviousDocumentationAssessment,
    bounded_json,
    decode_citations,
    decode_path_assessment,
    validate_incomplete_reasons,
)
from .domain.finding import (
    MAX_FINDINGS_PER_REVIEW,
    FindingContent,
    FindingDefinition,
    finding_definition_hash,
    require_unique_finding_identities,
    resolve_finding_content,
)
from .domain.review import ReviewPurpose, ReviewRunId
from .postgres import (
    documentation_reviews,
    findings,
    github_app,
    jobs,
    repository_decisions,
    review_runs,
)
from .postgres.runtime import PostgreSQLRuntime
from .repository_decision_context import RepositoryDecisionContext

_CITATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "role": {"type": "string", "enum": ["head", "comparison", "policy"]},
        "path": {"type": "string", "minLength": 1, "maxLength": 500},
        "start_line": {"type": ["integer", "null"], "minimum": 1},
        "end_line": {"type": ["integer", "null"], "minimum": 1},
    },
    "required": ["role", "path", "start_line", "end_line"],
}
_CITATIONS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 32,
    "items": _CITATION_SCHEMA,
}
_CONTENT_FIELDS = tuple(field.name for field in fields(FindingContent))
_CONTENT_PROPERTIES: dict[str, object] = {
    name: {"type": "string", "maxLength": 900} for name in _CONTENT_FIELDS
}
_CONTENT_PROPERTIES.update(
    {
        "rule_id": {
            "type": "string",
            "enum": [
                "documentation.accuracy",
                "documentation.missing",
                "documentation.intent-conflict",
            ],
        },
        "line": {"type": "integer", "minimum": 1},
        "publication_score": {"type": "integer", "minimum": 7, "maximum": 10},
        "confidence": {"type": "number", "minimum": 0.85, "maximum": 1},
    }
)
_FINDING_PROPERTIES: dict[str, object] = {
    "content": {
        "type": "object",
        "additionalProperties": False,
        "properties": _CONTENT_PROPERTIES,
        "required": list(_CONTENT_FIELDS),
    },
    "document_path": {"type": "string", "minLength": 1, "maxLength": 500},
    "area_id": {"type": "string", "maxLength": 80},
    "changed_path": {"type": "string", "minLength": 1, "maxLength": 500},
    "introduced_or_worsened": {"type": "string", "minLength": 1, "maxLength": 900},
    "citations": _CITATIONS_SCHEMA,
    "adr_ids": {
        "type": "array",
        "maxItems": 32,
        "items": {"type": "string", "maxLength": 80},
    },
}
_PATH_PROPERTIES: dict[str, object] = {
    "path": {"type": "string", "minLength": 1, "maxLength": 500},
    "disposition": {"type": "string", "enum": ["aligned", "no_impact", "finding"]},
    "rationale": {"type": "string", "minLength": 1, "maxLength": 900},
    "citations": _CITATIONS_SCHEMA,
}
_PREVIOUS_PROPERTIES: dict[str, object] = {
    "local_reference": {"type": "string", "pattern": "^F[1-9][0-9]*$"},
    "verdict": {"type": "string", "enum": ["resolved", "not_checked"]},
    "rationale": {"type": "string", "minLength": 1, "maxLength": 900},
    "citations": {**_CITATIONS_SCHEMA, "minItems": 0},
}
DOCUMENTATION_ASSESSMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": MAX_FINDINGS_PER_REVIEW,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": _FINDING_PROPERTIES,
                "required": list(_FINDING_PROPERTIES),
            },
        },
        "assessments": {
            "type": "array",
            "maxItems": 512,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": _PATH_PROPERTIES,
                "required": list(_PATH_PROPERTIES),
            },
        },
        "previous_assessments": {
            "type": "array", "maxItems": MAX_FINDINGS_PER_REVIEW,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": _PREVIOUS_PROPERTIES, "required": list(_PREVIOUS_PROPERTIES)},
        },
        "incomplete_reasons": {
            "type": "array",
            "maxItems": 512,
            "items": {"type": "string", "minLength": 1, "maxLength": 500},
        },
    },
    "required": ["findings", "assessments", "incomplete_reasons"],
}


def _object(value: object, expected: Sequence[str]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(
        cast(Mapping[object, object], value)
    ) != set(expected):
        raise DocumentationReviewError(
            "documentation assessment fields are incomplete or unsupported"
        )
    return cast(dict[str, object], value)


def _text(value: object, name: str, maximum: int = 900, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or "\x00" in value
        or (not empty and not value.strip())
    ):
        raise DocumentationReviewError(f"{name} requires bounded text")
    return value


def _array(value: object, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(cast(list[object], value)) > maximum:
        raise DocumentationReviewError(
            "documentation assessment exceeds its item bound"
        )
    return cast(list[object], value)


def _content(value: object) -> FindingContent:
    item = _object(value, _CONTENT_FIELDS)
    for name in _CONTENT_FIELDS:
        if name not in {"line", "publication_score", "confidence"}:
            _text(item[name], name, empty=name == "symbol")
    if (
        type(item["line"]) is not int
        or type(item["publication_score"]) is not int
        or type(item["confidence"]) not in {int, float}
    ):
        raise DocumentationReviewError(
            "finding line, score and confidence must be numeric"
        )
    if item["rule_id"] not in {
        "documentation.accuracy",
        "documentation.missing",
        "documentation.intent-conflict",
    }:
        raise DocumentationReviewError("documentation finding rule is unsupported")
    return FindingContent(
        rule_id=cast(str, item["rule_id"]),
        path=cast(str, item["path"]),
        line=item["line"],
        symbol=cast(str, item["symbol"]),
        anchor=cast(str, item["anchor"]),
        title=cast(str, item["title"]),
        severity=cast(str, item["severity"]),
        category=cast(str, item["category"]),
        publication_score=item["publication_score"],
        confidence=float(cast(float, item["confidence"])),
        evidence=cast(str, item["evidence"]),
        disproof_checks=cast(str, item["disproof_checks"]),
        impact=cast(str, item["impact"]),
        smallest_fix=cast(str, item["smallest_fix"]),
    )


def resolve_citation(
    result: DocumentationResult, citation: EvidenceCitation
) -> EvidenceRead:
    for read in result.evidence:
        if read.path != citation.path or read.role is not citation.role:
            continue
        if citation.start_line is None:
            if read.unavailable_reason == "not_found_at_revision" or (
                read.unavailable_reason is None and read.total_lines == 0
            ):
                return read
        elif (
            read.unavailable_reason is None
            and read.start_line is not None
            and read.end_line is not None
            and citation.end_line is not None
            and read.start_line
            <= citation.start_line
            <= citation.end_line
            <= read.end_line
        ):
            return read
    raise DocumentationReviewError(
        "citation is not covered by a successful recorded read or verified absence"
    )


def _pair(
    result: DocumentationResult, citations: tuple[EvidenceCitation, ...], path: str
) -> tuple[EvidenceRead, EvidenceRead]:
    found: dict[EvidenceRole, EvidenceRead] = {}
    for citation in citations:
        if citation.path == path and citation.role in {
            EvidenceRole.COMPARISON,
            EvidenceRole.HEAD,
        }:
            found[citation.role] = resolve_citation(result, citation)
    if EvidenceRole.COMPARISON not in found or EvidenceRole.HEAD not in found:
        raise DocumentationReviewError(
            "assessment requires comparison and head evidence for the exact path"
        )
    return found[EvidenceRole.COMPARISON], found[EvidenceRole.HEAD]


def validate_assessment(
    result: DocumentationResult,
    assessment: object,
    *,
    changed_paths: frozenset[str],
    decisions: RepositoryDecisionContext,
    previous_findings: tuple[PreviousDocumentationFinding, ...] = (),
) -> tuple[DocumentationAssessment, tuple[FindingDefinition, ...], tuple[str, ...]]:
    """Validate provenance and coverage; semantic correctness remains the reviewer's judgment."""
    bounded_json(assessment)
    fields = ("findings", "assessments", "incomplete_reasons")
    if isinstance(assessment, Mapping) and "previous_assessments" in assessment:
        fields += ("previous_assessments",)
    root = _object(cast(object, assessment), fields)
    scope = result.scope
    if scope is None or result.comparison_sha is None:
        raise DocumentationReviewError(
            "semantic documentation assessment requires a recorded exact scope and comparison"
        )
    definitions: list[FindingDefinition] = []
    evidence: list[FindingEvidence] = []
    for raw in _array(root["findings"], MAX_FINDINGS_PER_REVIEW):
        item = _object(raw, tuple(_FINDING_PROPERTIES))
        content = _content(item["content"])
        document = _text(item["document_path"], "document_path", 500)
        area_id = _text(item["area_id"], "area_id", 80, empty=True)
        changed = _text(item["changed_path"], "changed_path", 500)
        introduced = _text(item["introduced_or_worsened"], "introduced_or_worsened")
        citations = decode_citations(item["citations"])
        reads = tuple(resolve_citation(result, citation) for citation in citations)
        if changed not in changed_paths:
            raise DocumentationReviewError(
                "finding change anchor is not in the exact changed inventory"
            )
        areas = [
            area
            for area in scope.areas
            if area.id == area_id and document in area.documents
        ]
        if not areas and (area_id or document not in changed_paths):
            raise DocumentationReviewError(
                "finding document must belong to its selected area or be an actual changed document"
            )
        if (
            areas
            and changed != document
            and not any(changed in area.matched_paths for area in areas)
        ):
            raise DocumentationReviewError(
                "finding change anchor must belong to the selected documentation area"
            )
        before, after = _pair(result, citations, changed)
        if (before.blob_sha, before.unavailable_reason) == (
            after.blob_sha,
            after.unavailable_reason,
        ):
            raise DocumentationReviewError(
                "finding introduction requires changed comparison/head evidence"
            )
        doc_before, doc_after = _pair(result, citations, document)
        missing = (
            doc_after.unavailable_reason == "not_found_at_revision"
            or doc_after.total_lines == 0
        )
        if missing:
            if content.rule_id != "documentation.missing":
                raise DocumentationReviewError(
                    "missing or deleted documentation requires the missing-document rule"
                )
            if doc_before.unavailable_reason == "not_found_at_revision" and not areas:
                raise DocumentationReviewError(
                    "missing-document expectation requires an explicit selected area"
                )
        elif content.path != document:
            raise DocumentationReviewError(
                "existing documentation findings must use a real document anchor"
            )
        anchors = [
            citation
            for citation in citations
            if citation.path == content.path
            and citation.start_line is not None
            and citation.end_line is not None
            and citation.start_line <= content.line <= citation.end_line
        ]
        if not anchors or (missing and content.path not in {document, changed}):
            raise DocumentationReviewError(
                "finding location must use a real cited document or changed-source line"
            )
        adr_ids = tuple(
            _text(value, "adr_id", 80) for value in _array(item["adr_ids"], 32)
        )
        known = {
            decision.id: decision
            for decision in decisions.decisions
            if decision.status == "accepted"
        }
        if len(set(adr_ids)) != len(adr_ids) or any(
            adr not in known for adr in adr_ids
        ):
            raise DocumentationReviewError(
                "finding ADR references must resolve to accepted decision metadata"
            )
        if content.rule_id == "documentation.intent-conflict" and not adr_ids:
            raise DocumentationReviewError(
                "an intent conflict requires a cited accepted ADR and a human decision request"
            )
        material = {
            "reads": sorted(
                {
                    json.dumps(
                        {
                            "path": read.path,
                            "role": read.role.value,
                            "blob_sha": read.blob_sha,
                            "absence": read.unavailable_reason
                            == "not_found_at_revision",
                            "content_sha256": read.content_sha256,
                            "start_line": read.start_line,
                            "end_line": read.end_line,
                        },
                        sort_keys=True,
                    )
                    for read in reads
                }
            ),
            "policy": scope.policy_hash,
            "decision_snapshot": decisions.snapshot_hash,
            "area": [asdict(area) for area in areas],
            "adrs": [(adr, known[adr].metadata_hash) for adr in sorted(adr_ids)],
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        definition = resolve_finding_content(content, context_hash=digest)
        definitions.append(definition)
        evidence.append(
            FindingEvidence(
                definition.fingerprint,
                finding_definition_hash(definition),
                document,
                area_id,
                changed,
                introduced,
                citations,
                adr_ids,
            )
        )
    require_unique_finding_identities(tuple(definitions))
    paths = tuple(
        decode_path_assessment(raw) for raw in _array(root["assessments"], 512)
    )
    if len({item.path for item in paths}) != len(paths):
        raise DocumentationReviewError("each path has one assessment")
    needed = set(scope.documents) | set(scope.unmapped_paths)
    for item in paths:
        if item.path not in needed:
            raise DocumentationReviewError(
                "path assessment lies outside the recorded scope"
            )
        reads = tuple(resolve_citation(result, citation) for citation in item.citations)
        matched = [
            finding
            for finding in evidence
            if item.path in {finding.document_path, finding.changed_path}
        ]
        if item.disposition == "finding" and not matched:
            raise DocumentationReviewError(
                "finding assessment requires a validated finding for that path"
            )
        if item.disposition != "finding" and matched:
            raise DocumentationReviewError(
                "a path with findings cannot claim alignment or no impact"
            )
        if item.path in scope.unmapped_paths:
            _pair(result, item.citations, item.path)
            if item.disposition not in {"no_impact", "finding"}:
                raise DocumentationReviewError(
                    "unmapped paths require a no-impact explanation or a finding"
                )
        elif item.disposition == "no_impact":
            raise DocumentationReviewError(
                "no-impact assessment is reserved for unmapped changes"
            )
        else:
            _pair(result, item.citations, item.path)
            if item.disposition == "aligned" and any(
                read.path == item.path
                and read.role is EvidenceRole.HEAD
                and read.unavailable_reason is not None
                for read in reads
            ):
                raise DocumentationReviewError(
                    "a missing selected document cannot be aligned"
                )
    reasons = list(documentation_reviews.coverage_reasons(result))
    if decisions.status not in {"loaded", "not_configured"}:
        reasons.append(
            f"Accepted repository decisions could not be assessed: {decisions.status}"
        )
    reasons.extend(
        validate_incomplete_reasons(
            tuple(
                _text(value, "incomplete reason", 500)
                for value in _array(root["incomplete_reasons"], 512)
            )
        )
    )
    all_citations = tuple(
        dict.fromkeys(
            citation for item in (*paths, *evidence) for citation in item.citations
        )
    )
    for path in sorted({path for area in scope.areas for path in area.matched_paths}):
        try:
            _pair(result, all_citations, path)
        except DocumentationReviewError:
            reasons.append(
                f"Changed behavior lacks comparison/head assessment evidence: {path}"
            )
    for path in sorted(needed - {item.path for item in paths}):
        reasons.append(f"Documentation assessment missing for {path}")
    for path in scope.documents:
        missing_with_finding = any(
            finding.document_path == path for finding in evidence
        ) and any(
            read.path == path
            and read.role is EvidenceRole.HEAD
            and read.unavailable_reason == "not_found_at_revision"
            for read in result.evidence
        )
        if (
            not missing_with_finding
            and not documentation_reviews.evidence_path_complete(
                result, path, EvidenceRole.HEAD
            )
        ):
            reasons.append(f"Documentation content was not completely read: {path}")
    previous_by_reference = {item.local_reference: item for item in previous_findings}
    prior_assessments: list[PreviousDocumentationAssessment] = []
    seen: set[str] = set()
    for raw in _array(root.get("previous_assessments", []), MAX_FINDINGS_PER_REVIEW):
        item = _object(raw, tuple(_PREVIOUS_PROPERTIES))
        reference = _text(item["local_reference"], "local_reference", 20)
        prior = previous_by_reference.get(reference)
        if prior is None or reference in seen:
            raise DocumentationReviewError("previous finding reference is unknown or repeated")
        seen.add(reference)
        verdict = _text(item["verdict"], "verdict", 20)
        if verdict not in {"resolved", "not_checked"}:
            raise DocumentationReviewError("previous finding verdict is unsupported")
        rationale = _text(item["rationale"], "rationale")
        citations = (() if item["citations"] == [] and verdict == "not_checked"
                     else decode_citations(item["citations"]))
        for citation in citations:
            resolve_citation(result, citation)
        if verdict == "resolved":
            if any(value.fingerprint == prior.fingerprint for value in definitions):
                raise DocumentationReviewError("a current finding cannot also be resolved")
            if (prior.document_path not in scope.documents
                or not any(path.path == prior.document_path and path.disposition == "aligned" for path in paths)
                or not documentation_reviews.evidence_path_complete(result, prior.document_path, EvidenceRole.HEAD)
                or not any(citation.path == prior.document_path and citation.role is EvidenceRole.HEAD for citation in citations)):
                raise DocumentationReviewError("resolution requires a fully read selected document with an aligned assessment and head citation")
        prior_assessments.append(PreviousDocumentationAssessment(
            reference, prior.fingerprint, prior.occurrence_id, verdict, rationale, citations))
    if len(reasons) > 512:
        reasons = reasons[:511] + [
            "Additional documentation coverage remains incomplete."
        ]
    return (
        DocumentationAssessment(tuple(evidence), paths, tuple(prior_assessments)),
        tuple(definitions),
        validate_incomplete_reasons(tuple(dict.fromkeys(reasons))),
    )


def finalize_documentation_assessment(
    runtime: PostgreSQLRuntime,
    *,
    run_id: int,
    job_id: int,
    lease_generation: int,
    assessment: object,
) -> DocumentationResult:
    """Fence, validate, record findings and freeze one semantic result atomically."""
    bounded_json(assessment)
    resolved_id = ReviewRunId(run_id)
    with runtime.transaction() as connection:
        scope = review_runs.get_run_scope(connection, resolved_id)
        if scope.run.purpose is not ReviewPurpose.DOCUMENTATION:
            raise DocumentationReviewError(
                "documentation assessment requires a documentation run"
            )
        github_app.authorize_documentation_review(
            connection, scope.provider_repository_id
        )
        connection.execute(
            "SELECT id FROM review_agent.pull_requests WHERE id = %s FOR NO KEY UPDATE",
            (scope.run.pull_request_id,),
        )
        review_runs.lock_run(connection, resolved_id)
        jobs.require_live_lease(
            connection,
            job_id=job_id,
            review_run_id=resolved_id,
            lease_generation=lease_generation,
        )
        result = documentation_reviews.get_result(connection, run_id=resolved_id)
        if result is None or result.frozen:
            raise DocumentationReviewError(
                "documentation evidence is missing or already frozen"
            )
        changed_rows = connection.execute(
            "SELECT path, previous_path FROM review_agent.review_run_files WHERE review_run_id = %s AND is_changed_path",
            (resolved_id,),
        ).fetchall()
        changed = frozenset(str(path) for row in changed_rows for path in row if path)
        decisions = repository_decisions.load_context(connection, run_id=resolved_id)
        receipt, definitions, reasons = validate_assessment(
            result, assessment, changed_paths=changed, decisions=decisions,
            previous_findings=documentation_reviews.previous_findings(connection, run_id=resolved_id),
        )
        documentation_reviews.store_assessment(
            connection, run_id=resolved_id, assessment=receipt
        )
        findings.record_findings(
            connection,
            run_id=resolved_id,
            expected_head_sha=result.head_sha,
            definitions=definitions,
        )
        outcome = (
            DocumentationOutcome.FINDINGS
            if definitions
            else DocumentationOutcome.INCOMPLETE
            if reasons
            else DocumentationOutcome.NO_MISMATCH_FOUND
        )
        return documentation_reviews.finalize(
            connection,
            run_id=resolved_id,
            outcome=outcome,
            semantic_inference_used=True,
            incomplete_reasons=reasons,
            coverage_complete=not reasons,
        )
