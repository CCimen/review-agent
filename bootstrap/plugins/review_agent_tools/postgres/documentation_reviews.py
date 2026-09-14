"""One bounded documentation scope and evidence record per review run."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
from typing import cast

import psycopg
from psycopg.rows import TupleRow, class_row
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from ..documentation_scope import DocumentationScope
from ..domain.documentation_review import (
    MAX_EVIDENCE_READS,
    MAX_INCOMPLETE_REASONS,
    MAX_RESULT_JSON_BYTES,
    DocumentationAssessment,
    PreviousDocumentationFinding,
    DocumentationOutcome,
    DocumentationResult,
    DocumentationReviewError,
    EvidenceRead,
    EvidenceRole,
    bounded_json,
    decode_assessment,
    decode_evidence,
    decode_scope,
    validate_incomplete_reasons,
    validate_revision,
)
from ..domain.finding import MAX_FINDINGS_PER_REVIEW
from ..domain.review import ReviewRunId


class DocumentationEvidenceLimit(DocumentationReviewError):
    """Further evidence would exceed a fixed persistence bound."""


@dataclass(frozen=True, slots=True)
class _Run:
    purpose: str
    status: str
    base_sha: str
    head_sha: str
    publication_exists: bool


@dataclass(frozen=True, slots=True)
class _ResultRow:
    review_run_id: ReviewRunId
    base_sha: str
    comparison_sha: str | None
    head_sha: str
    scope_json: str | None
    evidence_json: str
    outcome: str | None
    semantic_inference_used: bool
    incomplete_reasons_json: str
    coverage_complete: bool
    frozen_at: datetime | None
    assessment_json: str | None


def _locked_run(connection: psycopg.Connection[TupleRow], run_id: ReviewRunId) -> _Run:
    if connection.info.transaction_status != TransactionStatus.INTRANS:
        raise DocumentationReviewError(
            "documentation mutation requires an active transaction"
        )
    with connection.cursor(row_factory=class_row(_Run)) as cursor:
        cursor.execute(
            """
            SELECT run.purpose, run.status, subject.base_sha, subject.head_sha,
                   FALSE AS publication_exists
            FROM review_agent.review_runs run
            JOIN review_agent.review_subjects subject ON subject.id = run.review_subject_id
            WHERE run.id = %s
            FOR UPDATE OF run
            """,
            (run_id,),
        )
        run = cursor.fetchone()
    if run is None or run.purpose != "documentation":
        raise DocumentationReviewError(
            "documentation evidence requires a documentation run"
        )
    # Read after acquiring the run lock so a publisher that held it first is visible.
    publication = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM review_agent.publications WHERE review_run_id = %s)",
        (run_id,),
    ).fetchone()
    assert publication is not None
    return replace(run, publication_exists=bool(publication[0]))


def _writable(run: _Run, result: DocumentationResult | None) -> None:
    if (
        run.status != "running"
        or run.publication_exists
        or (result is not None and result.frozen)
    ):
        raise DocumentationReviewError(
            "documentation result is frozen or its run is no longer live"
        )


def get_result(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
) -> DocumentationResult | None:
    with connection.cursor(row_factory=class_row(_ResultRow)) as cursor:
        cursor.execute(
            """
            SELECT review_run_id, base_sha, comparison_sha, head_sha,
                   scope_json::text, evidence_json::text, outcome,
                   semantic_inference_used, incomplete_reasons_json::text,
                   coverage_complete, frozen_at, assessment_json::text
            FROM review_agent.documentation_reviews WHERE review_run_id = %s
            """,
            (run_id,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    raw_reasons: object = json.loads(row.incomplete_reasons_json)
    if not isinstance(raw_reasons, list) or any(
        not isinstance(item, str) for item in cast(list[object], raw_reasons)
    ):
        raise DocumentationReviewError("stored documentation reasons are invalid")
    result = DocumentationResult(
        run_id=row.review_run_id,
        base_sha=validate_revision(row.base_sha, field="result base"),
        comparison_sha=row.comparison_sha,
        head_sha=validate_revision(row.head_sha, field="result head"),
        scope=decode_scope(json.loads(row.scope_json))
        if row.scope_json is not None
        else None,
        evidence=decode_evidence(json.loads(row.evidence_json)),
        outcome=DocumentationOutcome(row.outcome) if row.outcome is not None else None,
        semantic_inference_used=row.semantic_inference_used,
        incomplete_reasons=validate_incomplete_reasons(
            tuple(cast(list[str], raw_reasons))
        ),
        coverage_complete=row.coverage_complete,
        frozen=row.frozen_at is not None,
        assessment=decode_assessment(json.loads(row.assessment_json))
        if row.assessment_json is not None
        else None,
    )
    if result.comparison_sha is not None:
        validate_revision(result.comparison_sha, field="result comparison")
    if result.scope is not None:
        _check_scope_refs(result, result.scope)
    for evidence in result.evidence:
        _check_evidence_revision(result, evidence)
    if result.frozen != (result.outcome is not None):
        raise DocumentationReviewError("stored documentation freeze state is invalid")
    _check_size(result)
    return result


def _required_result(
    connection: psycopg.Connection[TupleRow], run_id: ReviewRunId
) -> DocumentationResult:
    result = get_result(connection, run_id=run_id)
    if result is None:
        raise DocumentationReviewError(
            "documentation result must be initialized before evidence reads"
        )
    return result


def coverage_reasons(
    result: DocumentationResult,
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    return validate_incomplete_reasons(
        tuple(
            dict.fromkeys(
                (
                    *(
                        result.scope.incomplete_reasons
                        if result.scope is not None
                        else ()
                    ),
                    *extra,
                    *(
                        item.unavailable_reason
                        for item in result.evidence
                        if item.unavailable_reason is not None
                        and item.unavailable_reason != "not_found_at_revision"
                        and not evidence_path_complete(result, item.path, item.role)
                    ),
                    *(
                        ("comparison_base_unavailable",)
                        if result.comparison_sha is None
                        else ()
                    ),
                )
            )
        )
    )


def _check_size(result: DocumentationResult) -> None:
    try:
        reasons = (
            result.incomplete_reasons if result.frozen else coverage_reasons(result)
        )
        encoded = bounded_json(
            {
                "scope": result.scope.to_json_obj()
                if result.scope is not None
                else None,
                "evidence": [item.to_json_obj() for item in result.evidence],
                "incomplete_reasons": list(reasons),
                "assessment": result.assessment.to_json_obj()
                if result.assessment
                else None,
            }
        )
        # A bounded read refusal still needs room for its terminal coverage reason.
        if not result.frozen and len(reasons) >= MAX_INCOMPLETE_REASONS:
            raise DocumentationReviewError(
                "documentation coverage reason limit exceeded"
            )
        if (
            not result.frozen
            and len(encoded.encode("utf-8")) + 2048 > MAX_RESULT_JSON_BYTES
        ):
            raise DocumentationReviewError("documentation receipt byte limit exceeded")
    except DocumentationReviewError as exc:
        raise DocumentationEvidenceLimit(str(exc)) from exc


def initialize(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    comparison_sha: str | None,
) -> DocumentationResult:
    if comparison_sha is not None:
        validate_revision(comparison_sha, field="comparison revision")
    run = _locked_run(connection, run_id)
    result = get_result(connection, run_id=run_id)
    if result is not None:
        if result.comparison_sha != comparison_sha:
            raise DocumentationReviewError(
                "documentation comparison identity cannot be rewritten"
            )
        return result
    _writable(run, result)
    connection.execute(
        """
        INSERT INTO review_agent.documentation_reviews
            (review_run_id, base_sha, comparison_sha, head_sha)
        VALUES (%s, %s, %s, %s)
        """,
        (run_id, run.base_sha, comparison_sha, run.head_sha),
    )
    return _required_result(connection, run_id)


def _check_scope_refs(result: DocumentationResult, scope: DocumentationScope) -> None:
    if (scope.base_sha, scope.comparison_sha, scope.head_sha) != (
        result.base_sha,
        result.comparison_sha,
        result.head_sha,
    ):
        raise DocumentationReviewError(
            "documentation scope does not match its immutable run refs"
        )


def freeze_scope(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    scope: DocumentationScope,
) -> DocumentationResult:
    scope = decode_scope(scope.to_json_obj())
    run = _locked_run(connection, run_id)
    result = _required_result(connection, run_id)
    _check_scope_refs(result, scope)
    if result.scope is not None:
        if result.scope != scope:
            raise DocumentationReviewError("documentation scope cannot be rewritten")
        return result
    _writable(run, result)
    candidate = replace(result, scope=scope)
    _check_size(candidate)
    connection.execute(
        "UPDATE review_agent.documentation_reviews SET scope_json = %s WHERE review_run_id = %s",
        (Jsonb(scope.to_json_obj()), run_id),
    )
    return candidate


def _check_evidence_revision(
    result: DocumentationResult, evidence: EvidenceRead
) -> None:
    expected = {
        EvidenceRole.POLICY: result.base_sha,
        EvidenceRole.COMPARISON: result.comparison_sha,
        EvidenceRole.HEAD: result.head_sha,
    }[evidence.role]
    if expected is None or evidence.revision != expected:
        raise DocumentationReviewError(
            "documentation evidence revision does not match its role"
        )


def record_evidence(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    evidence: EvidenceRead,
) -> DocumentationResult:
    run = _locked_run(connection, run_id)
    result = _required_result(connection, run_id)
    _check_evidence_revision(result, evidence)
    if evidence in result.evidence:
        return result
    _writable(run, result)
    for previous in result.evidence:
        if (previous.path, previous.revision) != (evidence.path, evidence.revision):
            continue
        if (
            previous.unavailable_reason == "not_found_at_revision"
            and evidence.unavailable_reason is None
        ) or (
            evidence.unavailable_reason == "not_found_at_revision"
            and previous.unavailable_reason is None
        ):
            raise DocumentationReviewError(
                "documentation evidence contradicts verified absence"
            )
        if (
            (
                previous.blob_sha is not None
                and evidence.blob_sha is not None
                and previous.blob_sha != evidence.blob_sha
            )
            or (
                previous.total_lines is not None
                and evidence.total_lines is not None
                and previous.total_lines != evidence.total_lines
            )
            or (
                previous.content_sha256 is not None
                and evidence.content_sha256 is not None
                and (previous.start_line, previous.end_line)
                == (evidence.start_line, evidence.end_line)
                and previous.content_sha256 != evidence.content_sha256
            )
        ):
            raise DocumentationReviewError(
                "documentation evidence contradicts an immutable read"
            )
    if len(result.evidence) >= MAX_EVIDENCE_READS:
        raise DocumentationEvidenceLimit("documentation evidence count limit exceeded")
    candidate = replace(result, evidence=(*result.evidence, evidence))
    _check_size(candidate)
    connection.execute(
        "UPDATE review_agent.documentation_reviews SET evidence_json = %s WHERE review_run_id = %s",
        (Jsonb([item.to_json_obj() for item in candidate.evidence]), run_id),
    )
    return candidate


def evidence_path_complete(
    result: DocumentationResult, path: str, role: EvidenceRole
) -> bool:
    reads = [
        item
        for item in result.evidence
        if item.path == path and item.role is role and item.unavailable_reason is None
    ]
    if not reads:
        return False
    total = reads[0].total_lines
    if total == 0:
        return True
    covered = 0
    for item in sorted(reads, key=lambda read: read.start_line or 0):
        if (
            item.start_line is None
            or item.end_line is None
            or item.start_line > covered + 1
        ):
            return False
        covered = max(covered, item.end_line)
    return total is not None and covered >= total


def store_assessment(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    assessment: DocumentationAssessment,
) -> DocumentationResult:
    run = _locked_run(connection, run_id)
    result = _required_result(connection, run_id)
    if result.assessment == assessment:
        return result
    _writable(run, result)
    if result.assessment is not None:
        raise DocumentationReviewError("documentation assessment cannot be rewritten")
    candidate = replace(result, assessment=assessment)
    _check_size(candidate)
    connection.execute(
        "UPDATE review_agent.documentation_reviews SET assessment_json = %s WHERE review_run_id = %s",
        (Jsonb(assessment.to_json_obj()), run_id),
    )
    return candidate


def finalize(
    connection: psycopg.Connection[TupleRow],
    *,
    run_id: ReviewRunId,
    outcome: DocumentationOutcome,
    semantic_inference_used: bool,
    incomplete_reasons: tuple[str, ...] = (),
    coverage_complete: bool = False,
) -> DocumentationResult:
    if (
        type(outcome) is not DocumentationOutcome
        or type(semantic_inference_used) is not bool
        or type(coverage_complete) is not bool
    ):
        raise DocumentationReviewError(
            "documentation outcome and coverage must be typed"
        )
    run = _locked_run(connection, run_id)
    result = _required_result(connection, run_id)
    scope = result.scope
    if scope is None:
        raise DocumentationReviewError(
            "documentation scope is required before finalization"
        )
    reasons = coverage_reasons(result, incomplete_reasons)
    if coverage_complete and reasons:
        raise DocumentationReviewError(
            "incomplete documentation evidence cannot have complete coverage"
        )
    if (
        outcome
        in {
            DocumentationOutcome.NOT_NEEDED,
            DocumentationOutcome.NOT_CONFIGURED,
            DocumentationOutcome.INVALID_CONFIGURATION,
            DocumentationOutcome.UNAVAILABLE,
        }
        and semantic_inference_used
    ):
        raise DocumentationReviewError(
            "deterministic documentation outcome cannot claim semantic inference"
        )
    if outcome in {
        DocumentationOutcome.NOT_NEEDED,
        DocumentationOutcome.NO_MISMATCH_FOUND,
    }:
        if (
            scope.status != "scoped"
            or not scope.active_policy
            or reasons
            or result.comparison_sha is None
        ):
            raise DocumentationReviewError(
                "clean documentation outcome requires complete configured scope"
            )
        resolved_unmapped: set[str] = (
            {
                item.path
                for item in result.assessment.paths
                if item.disposition == "no_impact"
            }
            if result.assessment
            else set()
        )
        if set(scope.unmapped_paths) - resolved_unmapped:
            raise DocumentationReviewError(
                "unresolved unmapped paths prevent a clean documentation outcome"
            )
        if outcome is DocumentationOutcome.NOT_NEEDED:
            if scope.areas or scope.documents:
                raise DocumentationReviewError(
                    "selected documentation requires an assessment"
                )
            changed_paths = {
                path for item in scope.changed_files for path in item.paths()
            }
            if not changed_paths.issubset({item.path for item in scope.exclusions}):
                raise DocumentationReviewError(
                    "not_needed requires explicit exclusion of every changed path"
                )
            coverage_complete = True
        else:
            observed = {
                item.path
                for item in result.evidence
                if item.role is EvidenceRole.HEAD and item.unavailable_reason is None
            }
            if (
                not coverage_complete
                or not semantic_inference_used
                or not set(scope.documents).issubset(observed)
            ):
                raise DocumentationReviewError(
                    "clean documentation assessment requires verified selected coverage"
                )
    expected_status = {
        DocumentationOutcome.NOT_CONFIGURED: "not_configured",
        DocumentationOutcome.INVALID_CONFIGURATION: "invalid_configuration",
    }.get(outcome)
    if expected_status is not None and scope.status != expected_status:
        raise DocumentationReviewError(
            "documentation setup outcome does not match its scope"
        )
    if (
        outcome in {DocumentationOutcome.INCOMPLETE, DocumentationOutcome.UNAVAILABLE}
        and not reasons
    ):
        raise DocumentationReviewError(
            "incomplete documentation outcome requires a coverage reason"
        )
    candidate = replace(
        result,
        outcome=outcome,
        semantic_inference_used=semantic_inference_used,
        incomplete_reasons=reasons,
        coverage_complete=coverage_complete,
        frozen=True,
    )
    if result.frozen:
        if result != candidate:
            raise DocumentationReviewError("documentation outcome cannot be rewritten")
        return result
    _writable(run, result)
    _check_size(candidate)
    connection.execute(
        """
        UPDATE review_agent.documentation_reviews
        SET outcome = %s, semantic_inference_used = %s, incomplete_reasons_json = %s,
            coverage_complete = %s, frozen_at = clock_timestamp()
        WHERE review_run_id = %s
        """,
        (
            outcome.value,
            semantic_inference_used,
            Jsonb(list(reasons)),
            coverage_complete,
            run_id,
        ),
    )
    return candidate


def previous_findings(
    connection: psycopg.Connection[TupleRow], *, run_id: ReviewRunId,
) -> tuple[PreviousDocumentationFinding, ...]:
    """Read unresolved findings from the current posted documentation report."""
    with connection.cursor(row_factory=class_row(PreviousDocumentationFinding)) as cursor:
        rows = cursor.execute(
            """
            SELECT item.local_reference, identity.fingerprint,
                   occurrence.id AS occurrence_id,
                   occurrence.review_run_id AS source_run_id,
                   provenance.value->>'document_path' AS document_path,
                   occurrence.title, occurrence.evidence, occurrence.smallest_fix,
                   identity.rule_id, identity.path, identity.symbol, identity.anchor
            FROM review_agent.review_runs AS run
            JOIN review_agent.publications AS publication
              ON publication.pull_request_id = run.pull_request_id
             AND publication.purpose = run.purpose
            JOIN review_agent.publication_findings AS item ON item.publication_id = publication.id
            JOIN review_agent.finding_identities AS identity ON identity.id = item.finding_id
            JOIN review_agent.finding_occurrences AS occurrence ON occurrence.id = item.source_finding_occurrence_id
            JOIN review_agent.documentation_reviews AS result ON result.review_run_id = occurrence.review_run_id
            CROSS JOIN LATERAL jsonb_array_elements(result.assessment_json->'findings') AS provenance(value)
            WHERE run.id = %s AND run.purpose = 'documentation'
              AND publication.status = 'posted' AND publication.superseded_by_publication_id IS NULL
              AND publication.review_run_id <> run.id
              AND item.outcome IN ('current', 'not_checked')
              AND provenance.value->>'fingerprint' = identity.fingerprint
            ORDER BY item.local_reference
            LIMIT %s
            """, (run_id, MAX_FINDINGS_PER_REVIEW + 1),
        ).fetchall()
    if len(rows) > MAX_FINDINGS_PER_REVIEW:
        raise DocumentationReviewError("previous documentation findings exceed the review bound")
    return tuple(rows)
